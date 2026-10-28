"""Point-in-time evidence about a package, assembled from deps.dev, OSV and GitHub.

Every query takes the date to answer as of and returns only what was knowable on it: releases
published by then, advisories disclosed by then (with the fixes released by then), and commits
made by then. Present-day snapshots, such as star counts, the OpenSSF Scorecard, and archive or
deprecation flags, describe the package's future when seen from a past date, so a historical
query gets a ``Withheld`` marker in their place.

Two approximations remain, and each result that carries one says so in a ``note``: advisory
text and severity come from today's records of a vulnerability that was public by the date,
and deps.dev resolves dependency graphs against today's registry.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from enum import StrEnum
from importlib.metadata import version as installed_version
from typing import Any, cast

import httpx
from pydantic import BaseModel

from depcouncil.data import DepsDevClient, GitHubClient, OsvClient, SourceUnavailable
from depcouncil.data.depsdev import DependencyGraph, Package, VersionDetail
from depcouncil.data.github import Repository
from depcouncil.data.osv import OsvAffected, OsvVulnerability
from depcouncil.schemas import PackageRef, normalize_name
from depcouncil.settings import Settings
from depcouncil.timeline import cutoff, is_historical, known_by

ADVISORY_NOTE = (
    "Advisory text and severity come from today's records of vulnerabilities that were "
    "public by the analysis date."
)
RESOLVED_TODAY_NOTE = (
    "Dependencies are resolved against today's registry; an install on the analysis date "
    "may have resolved a different set."
)
_SNAPSHOT_WITHHELD = (
    "Present-day snapshot, not knowable on {as_of}; withheld from this historical review."
)
_PRERELEASE = re.compile(r"(?i)(alpha|beta|rc|dev|pre|preview|canary|next|snapshot|\d[abc]\d)")
_MAJOR = re.compile(r"v?(\d+)")


class Withheld(BaseModel):
    """Data that exists today but was not knowable on the analysis date."""

    withheld: str


class Unavailable(BaseModel):
    """Data a source could not provide."""

    unavailable: str


class Release(BaseModel):
    version: str
    published: date
    prerelease: bool


class ReleaseHistory(BaseModel):
    as_of: date
    total_releases: int
    first_release: date | None
    latest_release: Release | None
    latest_stable_release: Release | None
    days_since_last_release: int | None
    releases_last_365_days: int
    new_major_versions_last_730_days: list[str]
    recent_releases: list[Release]


class Severity(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


_SEVERITY_LABELS = {
    "LOW": Severity.LOW,
    "MODERATE": Severity.MODERATE,
    "MEDIUM": Severity.MODERATE,
    "HIGH": Severity.HIGH,
    "CRITICAL": Severity.CRITICAL,
}


class Advisory(BaseModel):
    id: str
    aliases: list[str]
    summary: str
    severity: Severity
    cvss_score: float | None
    disclosed: date
    fix_released: date | None
    fixed_versions: list[str]
    days_to_fix: int | None
    affects_latest_release: bool | None


class AdvisoryDetail(Advisory):
    details: str
    note: str = ADVISORY_NOTE


class VulnerabilityRecord(BaseModel):
    as_of: date
    known_advisories: int
    disclosed_last_365_days: int
    by_severity: dict[Severity, int]
    unfixed: int
    affecting_latest_release: int
    median_days_to_fix: float | None
    advisories: list[Advisory]
    note: str = ADVISORY_NOTE


class CommitActivity(BaseModel):
    as_of: date
    repository: str
    window_days: int
    commits_in_window: int
    last_commit: date | None
    days_since_last_commit: int | None
    authors_in_window: int
    top_author_share: float | None
    sampled_commits: int
    repository_created: date | None


class VersionMetadata(BaseModel):
    version: str
    published: date
    licenses: list[str]
    slsa_provenance: bool
    attestations: int
    source_repository: str | None


class DependencyFootprint(BaseModel):
    version: str
    direct_count: int
    transitive_count: int
    direct_dependencies: list[str]
    resolution_error: str | None
    note: str | None


class ProjectSnapshot(BaseModel):
    repository: str
    stars: int
    forks: int
    open_issues: int
    archived: bool
    latest_release_deprecated: bool
    deprecation_reason: str | None


class ScorecardReport(BaseModel):
    repository: str
    overall_score: float
    measured: date | None
    checks: dict[str, int]


class Evidence:
    """Facts about packages as of a given date, fetching each raw record at most once.

    One instance serves one council run (and, in a backtest, the outcome measured after it);
    concurrent analysts asking for the same record share one request.
    """

    def __init__(self, http: httpx.AsyncClient, *, github_token: str | None = None) -> None:
        self._depsdev = DepsDevClient(http)
        self._osv = OsvClient(http)
        self._github = GitHubClient(http, token=github_token)
        self._memo: dict[tuple[str, ...], asyncio.Future[Any]] = {}

    async def known_releases(self, pkg: PackageRef, as_of: date) -> list[Release]:
        """Every release published by the date, oldest first."""
        package = await self._package(pkg)
        dated = sorted(
            (v for v in package.versions if v.published_at and known_by(v.published_at, as_of)),
            key=lambda v: v.published_at or datetime.min,
        )
        return [
            Release(
                version=v.version,
                published=v.published_at.date(),
                prerelease=_is_prerelease(v.version),
            )
            for v in dated
            if v.published_at
        ]

    async def releases(self, pkg: PackageRef, as_of: date) -> ReleaseHistory:
        releases = await self.known_releases(pkg, as_of)
        latest = releases[-1] if releases else None
        latest_stable = next((r for r in reversed(releases) if not r.prerelease), None)
        return ReleaseHistory(
            as_of=as_of,
            total_releases=len(releases),
            first_release=releases[0].published if releases else None,
            latest_release=latest,
            latest_stable_release=latest_stable,
            days_since_last_release=(as_of - latest.published).days if latest else None,
            releases_last_365_days=sum((as_of - r.published).days < 365 for r in releases),
            new_major_versions_last_730_days=_new_majors(releases, as_of, within_days=730),
            recent_releases=releases[-10:][::-1],
        )

    async def known_advisories(self, pkg: PackageRef, as_of: date) -> list[Advisory]:
        """Advisories disclosed by the date, one per vulnerability, newest first."""
        return [advisory for advisory, _ in await self._advisories(pkg, as_of)]

    async def vulnerabilities(self, pkg: PackageRef, as_of: date) -> VulnerabilityRecord:
        advisories = await self.known_advisories(pkg, as_of)
        fix_days = [a.days_to_fix for a in advisories if a.days_to_fix is not None]
        return VulnerabilityRecord(
            as_of=as_of,
            known_advisories=len(advisories),
            disclosed_last_365_days=sum((as_of - a.disclosed).days < 365 for a in advisories),
            by_severity=dict(Counter(a.severity for a in advisories)),
            unfixed=sum(a.fix_released is None for a in advisories),
            affecting_latest_release=sum(a.affects_latest_release is True for a in advisories),
            median_days_to_fix=statistics.median(fix_days) if fix_days else None,
            advisories=advisories[:15],
        )

    async def advisory(
        self, pkg: PackageRef, as_of: date, advisory_id: str
    ) -> AdvisoryDetail | None:
        """One advisory by its id or any alias, or ``None`` if none was disclosed by the date."""
        wanted = advisory_id.strip().upper()
        for advisory, details in await self._advisories(pkg, as_of):
            if wanted in {advisory.id.upper(), *(alias.upper() for alias in advisory.aliases)}:
                return AdvisoryDetail(**advisory.model_dump(), details=details[:4000])
        return None

    async def commit_activity(
        self, pkg: PackageRef, as_of: date, *, window_days: int = 365
    ) -> CommitActivity:
        repo = await self._require_repository(pkg, as_of)
        end = cutoff(as_of)
        start = end - timedelta(days=window_days)
        last, count, sample, meta = await asyncio.gather(
            self._github.last_commit(repo, until=end),
            self._github.count_commits(repo, since=start, until=end),
            self._github.recent_commits(repo, since=start, until=end),
            self._repository(repo),
        )
        authors = Counter(commit.author_key for commit in sample)
        last_date = last.committed_at.date() if last and last.committed_at else None
        return CommitActivity(
            as_of=as_of,
            repository=repo,
            window_days=window_days,
            commits_in_window=count,
            last_commit=last_date,
            days_since_last_commit=(as_of - last_date).days if last_date else None,
            authors_in_window=len(authors),
            top_author_share=round(max(authors.values()) / len(sample), 2) if sample else None,
            sampled_commits=len(sample),
            repository_created=meta.created_at.date() if meta.created_at else None,
        )

    async def version_metadata(self, pkg: PackageRef, as_of: date) -> VersionMetadata:
        """License and release integrity of the latest release by the date (fixed at publish)."""
        current = await self._current_release(pkg, as_of)
        detail = await self._version_detail(pkg, current.version)
        return VersionMetadata(
            version=current.version,
            published=current.published,
            licenses=detail.licenses,
            slsa_provenance=bool(detail.slsa_provenances),
            attestations=len(detail.attestations),
            source_repository=detail.source_repository(),
        )

    async def dependency_footprint(self, pkg: PackageRef, as_of: date) -> DependencyFootprint:
        current = await self._current_release(pkg, as_of)
        graph = await self._dependencies(pkg, current.version)
        direct = sorted({n.version_key.name for n in graph.nodes if n.relation == "DIRECT"})
        transitive = {n.version_key.name for n in graph.nodes if n.relation == "INDIRECT"}
        return DependencyFootprint(
            version=current.version,
            direct_count=len(direct),
            transitive_count=len(transitive),
            direct_dependencies=direct[:40],
            resolution_error=graph.error or None,
            note=RESOLVED_TODAY_NOTE if is_historical(as_of) else None,
        )

    async def project_snapshot(self, pkg: PackageRef, as_of: date) -> ProjectSnapshot | Withheld:
        """Community size and archive/deprecation status, as they stand today."""
        if is_historical(as_of):
            return Withheld(withheld=_SNAPSHOT_WITHHELD.format(as_of=as_of))
        repo = await self._require_repository(pkg, as_of)
        meta, package, current = await asyncio.gather(
            self._repository(repo), self._package(pkg), self._current_release(pkg, as_of)
        )
        version = next((v for v in package.versions if v.version == current.version), None)
        deprecated = version is not None and version.is_deprecated
        return ProjectSnapshot(
            repository=repo,
            stars=meta.stargazers_count,
            forks=meta.forks_count,
            open_issues=meta.open_issues_count,
            archived=meta.archived,
            latest_release_deprecated=deprecated,
            deprecation_reason=(version.deprecated_reason or None)
            if version and deprecated
            else None,
        )

    async def scorecard(self, pkg: PackageRef, as_of: date) -> ScorecardReport | Withheld:
        """The repository's latest OpenSSF Scorecard."""
        if is_historical(as_of):
            return Withheld(withheld=_SNAPSHOT_WITHHELD.format(as_of=as_of))
        repo = await self._require_repository(pkg, as_of)
        project = await self._once(("project", repo), lambda: self._depsdev.project(repo))
        if project.scorecard is None:
            raise SourceUnavailable("deps.dev", f"no OpenSSF Scorecard for {repo}")
        card = project.scorecard
        return ScorecardReport(
            repository=repo,
            overall_score=card.overall_score,
            measured=card.date.date() if card.date else None,
            checks={check.name: check.score for check in card.checks},
        )

    async def _advisories(self, pkg: PackageRef, as_of: date) -> list[tuple[Advisory, str]]:
        """Each vulnerability disclosed by the date, merged across its aliases, with its details."""
        records, package, releases = await asyncio.gather(
            self._osv_records(pkg), self._package(pkg), self.known_releases(pkg, as_of)
        )
        published = {v.version: v.published_at for v in package.versions if v.published_at}
        latest = next((r.version for r in reversed(releases) if not r.prerelease), None)
        found: list[tuple[Advisory, str]] = []
        for group in _alias_groups(records):
            live = [r for r in group if not known_by(r.withdrawn, as_of)]
            disclosed = min(_disclosure_times(live), default=None)
            if disclosed is None or not known_by(disclosed, as_of):
                continue
            fixes = sorted(
                (stamp, version)
                for version in _fixed_versions(live, pkg)
                if (stamp := published.get(version)) is not None and known_by(stamp, as_of)
            )
            fix_released = fixes[0][0] if fixes else None
            cvss_scores = [scored[0] for record in live if (scored := _cvss(record))]
            advisory = Advisory(
                id=_primary_id(live),
                aliases=sorted(_all_ids(live) - {_primary_id(live)}),
                summary=next((r.summary for r in live if r.summary), ""),
                severity=max(map(_severity, live), key=lambda s: s.rank),
                cvss_score=max(cvss_scores, default=None),
                disclosed=disclosed.date(),
                fix_released=fix_released.date() if fix_released else None,
                fixed_versions=[version for _, version in fixes],
                days_to_fix=max(0, (fix_released - disclosed).days) if fix_released else None,
                affects_latest_release=_affects(live, pkg, latest),
            )
            details = max((r.details for r in live), key=len, default="")
            found.append((advisory, details))
        return sorted(found, key=lambda pair: pair[0].disclosed, reverse=True)

    async def _current_release(self, pkg: PackageRef, as_of: date) -> Release:
        """The latest stable release published by the date (else the latest prerelease)."""
        releases = await self.known_releases(pkg, as_of)
        if not releases:
            raise SourceUnavailable("deps.dev", f"no release of {pkg} was published by {as_of}")
        return next((r for r in reversed(releases) if not r.prerelease), releases[-1])

    async def source_repository(self, pkg: PackageRef, as_of: date) -> str | None:
        """The GitHub ``owner/name`` that the latest release by the date names as its source."""
        current = await self._current_release(pkg, as_of)
        return (await self._version_detail(pkg, current.version)).source_repository()

    async def _require_repository(self, pkg: PackageRef, as_of: date) -> str:
        repo = await self.source_repository(pkg, as_of)
        if repo is None:
            raise SourceUnavailable("GitHub", f"no GitHub source repository is linked to {pkg}")
        return repo

    async def _package(self, pkg: PackageRef) -> Package:
        return await self._once(("package", str(pkg)), lambda: self._depsdev.package(pkg))

    async def _version_detail(self, pkg: PackageRef, version: str) -> VersionDetail:
        return await self._once(
            ("version", str(pkg), version), lambda: self._depsdev.version(pkg, version)
        )

    async def _dependencies(self, pkg: PackageRef, version: str) -> DependencyGraph:
        return await self._once(
            ("dependencies", str(pkg), version), lambda: self._depsdev.dependencies(pkg, version)
        )

    async def _osv_records(self, pkg: PackageRef) -> list[OsvVulnerability]:
        return await self._once(("osv", str(pkg)), lambda: self._osv.vulnerabilities(pkg))

    async def _repository(self, repo: str) -> Repository:
        return await self._once(("repository", repo), lambda: self._github.repository(repo))

    async def _once[T](self, key: tuple[str, ...], fetch: Callable[[], Awaitable[T]]) -> T:
        """Run ``fetch`` once per key; concurrent and later callers share its result or error."""
        future = self._memo.get(key)
        if future is None:
            future = asyncio.ensure_future(fetch())
            self._memo[key] = future
        return cast("T", await future)


@asynccontextmanager
async def open_evidence(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncGenerator[Evidence]:
    """An ``Evidence`` over a fresh HTTP client, closed on exit. Tests pass a mock ``transport``."""
    token = settings.github_token.get_secret_value() if settings.github_token else None
    headers = {"User-Agent": f"depcouncil/{installed_version('depcouncil')}"}
    async with httpx.AsyncClient(
        timeout=settings.http_timeout, headers=headers, transport=transport, follow_redirects=True
    ) as http:
        yield Evidence(http, github_token=token)


def _is_prerelease(version: str) -> bool:
    return bool(_PRERELEASE.search(version))


def _new_majors(releases: list[Release], as_of: date, *, within_days: int) -> list[str]:
    """The first stable release of each new major version published within the window."""
    seen: set[int] = set()
    new: list[str] = []
    for release in releases:
        match = _MAJOR.match(release.version)
        if release.prerelease or match is None or (major := int(match.group(1))) in seen:
            continue
        if seen and (as_of - release.published).days <= within_days:
            new.append(release.version)
        seen.add(major)
    return new


def _alias_groups(records: list[OsvVulnerability]) -> list[list[OsvVulnerability]]:
    """Group records that describe one vulnerability: ids linked through any chain of aliases."""
    parent: dict[str, str] = {}

    def root(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for record in records:
        for alias in record.aliases:
            parent[root(alias)] = root(record.id)
    groups: defaultdict[str, list[OsvVulnerability]] = defaultdict(list)
    for record in records:
        groups[root(record.id)].append(record)
    return list(groups.values())


def _disclosure_times(records: Iterable[OsvVulnerability]) -> Iterable[datetime]:
    """When each record says the vulnerability became public: its own date, or NVD's if earlier."""
    for record in records:
        if record.published:
            yield record.published
        if record.database_specific and record.database_specific.nvd_published_at:
            yield record.database_specific.nvd_published_at


def _all_ids(records: Iterable[OsvVulnerability]) -> set[str]:
    return {name for record in records for name in (record.id, *record.aliases)}


def _primary_id(records: Iterable[OsvVulnerability]) -> str:
    """The most recognizable id: a CVE, else a GHSA, else the first in sort order."""
    ids = sorted(_all_ids(records))
    return next((i for prefix in ("CVE-", "GHSA-") for i in ids if i.startswith(prefix)), ids[0])


def _severity(record: OsvVulnerability) -> Severity:
    """The record's severity label (GHSA's), else the rating of its highest CVSS score."""
    label = record.database_specific.severity if record.database_specific else None
    if (labelled := _SEVERITY_LABELS.get((label or "").upper())) is not None:
        return labelled
    scored = _cvss(record)
    return _SEVERITY_LABELS.get(scored[1].upper(), Severity.UNKNOWN) if scored else Severity.UNKNOWN


def _cvss(record: OsvVulnerability) -> tuple[float, str] | None:
    """The record's highest CVSS base score and its rating, if it has a vector that scores."""
    return max((scored for entry in record.severity if (scored := entry.cvss())), default=None)


def _affected_entries(
    records: Iterable[OsvVulnerability], pkg: PackageRef
) -> Iterable[OsvAffected]:
    for record in records:
        for affected in record.affected:
            package = affected.package
            if (
                package is not None
                and package.ecosystem == pkg.ecosystem.osv_ecosystem
                and normalize_name(pkg.ecosystem, package.name) == pkg.name
            ):
                yield affected


def _fixed_versions(records: Iterable[OsvVulnerability], pkg: PackageRef) -> set[str]:
    return {
        event.fixed
        for affected in _affected_entries(records, pkg)
        for span in affected.ranges
        if span.type in ("ECOSYSTEM", "SEMVER")
        for event in span.events
        if event.fixed
    }


def _affects(records: list[OsvVulnerability], pkg: PackageRef, version: str | None) -> bool | None:
    """Whether ``version`` is listed as affected, or ``None`` when the records list no versions."""
    listed = {v for affected in _affected_entries(records, pkg) for v in affected.versions}
    if version is None or not listed:
        return None
    return version in listed
