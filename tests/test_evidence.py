"""The evidence layer answers as of a date and hides everything after it."""

import re
from datetime import date

import httpx
import pytest

from depcouncil import Evidence, PackageRef
from depcouncil import evidence as evidence_module
from depcouncil.data import SourceUnavailable, gitlab
from depcouncil.evidence import IssueFlow, ProjectSnapshot, ScorecardReport, Severity, Withheld
from depcouncil.timeline import today
from tests.fakes import FakeSources, vuln

pytestmark = pytest.mark.anyio

WIDGET = PackageRef.parse("pypi:widget")
AS_OF = date(2022, 1, 1)
CRITICAL_V3 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"  # 9.8
MODERATE_V4 = "CVSS:4.0/AV:N/AC:H/AT:N/PR:L/UI:A/VC:N/VI:N/VA:H/SC:N/SI:N/SA:H"  # 6.8


async def test_releases_stop_at_the_analysis_date(evidence: Evidence) -> None:
    history = await evidence.releases(WIDGET, AS_OF)

    assert history.total_releases == 5
    assert history.latest_release and history.latest_release.version == "2.2.0rc1"
    assert history.latest_release.prerelease
    assert history.latest_stable_release and history.latest_stable_release.version == "2.1.0"
    assert history.days_since_last_release == 61
    assert history.new_major_versions_last_730_days == ["2.0.0"]


async def test_aliases_merge_into_one_advisory_dated_by_its_earliest_record(
    evidence: Evidence,
) -> None:
    record = await evidence.vulnerabilities(WIDGET, AS_OF)
    by_id = {a.id: a for a in record.advisories}

    # The GHSA record was published after the date, but its PYSEC alias was public before it.
    merged = by_id["CVE-2021-0001"]
    assert set(merged.aliases) == {"GHSA-aaaa-aaaa-aaaa", "PYSEC-2021-1"}
    assert merged.disclosed == date(2021, 5, 1)
    assert merged.severity is Severity.HIGH
    assert merged.days_to_fix == 123
    assert merged.summary == "Widget parser overflow"


async def test_fixes_and_advisories_after_the_date_are_hidden(evidence: Evidence) -> None:
    record = await evidence.vulnerabilities(WIDGET, AS_OF)
    by_id = {a.id: a for a in record.advisories}

    assert set(by_id) == {"CVE-2021-0001", "GHSA-cccc-cccc-cccc"}  # not the 2022 CRITICAL one
    unfixed = by_id["GHSA-cccc-cccc-cccc"]  # its fix, 3.0.0, shipped in 2023
    assert unfixed.fix_released is None
    assert unfixed.fixed_versions == []
    assert unfixed.affects_latest_release is True
    assert (record.unfixed, record.affecting_latest_release) == (1, 1)


async def test_cvss_rates_advisories_that_lack_a_ghsa_severity(
    evidence: Evidence, sources: FakeSources
) -> None:
    sources.vulns = [
        vuln("RUSTSEC-2021-1", "2021-01-01T00:00:00Z", cvss=(("CVSS_V3", CRITICAL_V3),)),
        vuln(
            "RUSTSEC-2021-2",
            "2021-02-01T00:00:00Z",
            cvss=(("CVSS_V4", MODERATE_V4), ("CVSS_V3", "not a vector")),
        ),
        vuln(
            "GHSA-eeee-eeee-eeee",
            "2021-03-01T00:00:00Z",
            severity="LOW",
            cvss=(("CVSS_V3", CRITICAL_V3),),
        ),
        vuln("PYSEC-2021-9", "2021-04-01T00:00:00Z"),
    ]

    by_id = {a.id: a for a in await evidence.known_advisories(WIDGET, AS_OF)}

    assert (by_id["RUSTSEC-2021-1"].severity, by_id["RUSTSEC-2021-1"].cvss_score) == (
        Severity.CRITICAL,
        9.8,
    )
    assert (by_id["RUSTSEC-2021-2"].severity, by_id["RUSTSEC-2021-2"].cvss_score) == (
        Severity.MODERATE,
        6.8,
    )
    assert by_id["GHSA-eeee-eeee-eeee"].severity is Severity.LOW  # GHSA's own label wins
    assert (by_id["PYSEC-2021-9"].severity, by_id["PYSEC-2021-9"].cvss_score) == (
        Severity.UNKNOWN,
        None,
    )


async def test_withdrawn_advisories_count_only_until_withdrawn(evidence: Evidence) -> None:
    before = await evidence.known_advisories(WIDGET, date(2021, 2, 15))
    after = await evidence.known_advisories(WIDGET, AS_OF)

    assert "GHSA-dddd-dddd-dddd" in {a.id for a in before}
    assert "GHSA-dddd-dddd-dddd" not in {a.id for a in after}


async def test_advisory_lookup_by_alias_and_not_past_the_date(evidence: Evidence) -> None:
    detail = await evidence.advisory(WIDGET, AS_OF, "ghsa-aaaa-aaaa-aaaa")

    assert detail is not None and detail.details.startswith("Parsing a crafted widget")
    assert await evidence.advisory(WIDGET, AS_OF, "GHSA-bbbb-bbbb-bbbb") is None


async def test_commit_activity_asks_github_for_the_window_before_the_date(
    evidence: Evidence, sources: FakeSources
) -> None:
    activity = await evidence.commit_activity(WIDGET, AS_OF, window_days=365)

    assert activity.commits_in_window == 3
    assert activity.last_commit == date(2021, 12, 20)
    assert activity.authors_in_window == 2
    assert activity.top_author_share == 0.67
    commit_queries = [r for r in sources.requests if r.url.path.endswith("/commits")]
    assert {r.url.params["until"] for r in commit_queries} == {"2022-01-02T00:00:00Z"}


async def test_issue_activity_counts_only_what_had_happened_by_the_date(
    evidence: Evidence, sources: FakeSources
) -> None:
    activity = await evidence.issue_activity(WIDGET, AS_OF)

    # Opened in 2021: three of each. By the date, one issue closed (after 10 days) and one pull
    # request merged (after 2); the two resolved in 2022 count as open on the date.
    assert activity.issues == IssueFlow(
        opened_in_window=3,
        share_resolved_by_date=0.33,
        median_days_to_resolve=10.0,
        open_on_date=2,
        sampled=3,
    )
    assert activity.pull_requests == IssueFlow(
        opened_in_window=3,
        share_resolved_by_date=0.33,
        median_days_to_resolve=2.0,
        open_on_date=1,
        sampled=3,
    )
    searches = [r.url.params["q"] for r in sources.requests if r.url.path == "/search/issues"]
    stamps = {stamp for q in searches for stamp in re.findall(r"\d{4}-\d\d-\d\dT[\d:]+Z", q)}
    assert max(stamps) == "2022-01-01T23:59:59Z"


async def test_issue_activity_counts_past_its_sample(
    evidence: Evidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_module, "ISSUE_SAMPLE", 1)

    activity = await evidence.issue_activity(WIDGET, AS_OF)

    # The one sampled issue is still open, so the share comes from a separate count.
    assert (activity.issues.sampled, activity.issues.share_resolved_by_date) == (1, 0.33)
    assert activity.issues.median_days_to_resolve is None


async def test_commit_activity_reads_a_gitlab_repository(
    evidence: Evidence, sources: FakeSources
) -> None:
    sources.repo_host = "gitlab.com"

    activity = await evidence.commit_activity(WIDGET, AS_OF, window_days=365)

    assert activity.repository == "gitlab.com/acme/widget"
    assert (activity.commits_in_window, activity.last_commit) == (3, date(2021, 12, 20))
    assert (activity.authors_in_window, activity.top_author_share) == (2, 0.67)
    assert activity.note is None
    commit_queries = [r for r in sources.requests if r.url.path.endswith("/repository/commits")]
    assert {r.url.params["until"] for r in commit_queries} == {"2022-01-01T23:59:59Z"}
    assert not any(r.url.host == "api.github.com" for r in sources.requests)


async def test_gitlab_commit_counts_stop_at_the_page_limit(
    evidence: Evidence, sources: FakeSources, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources.repo_host = "gitlab.com"
    monkeypatch.setattr(gitlab, "_PAGE_SIZE", 1)
    monkeypatch.setattr(gitlab, "_MAX_PAGES", 2)

    activity = await evidence.commit_activity(WIDGET, AS_OF, window_days=365)

    assert activity.commits_in_window == 2
    assert activity.note and "at least" in activity.note


async def test_gitlab_snapshots_need_a_token_for_the_archive_flag(sources: FakeSources) -> None:
    sources.repo_host = "gitlab.com"
    snapshots: dict[str | None, object] = {}
    for token in (None, "glpat-test"):
        async with httpx.AsyncClient(transport=sources.transport()) as http:
            snapshots[token] = await Evidence(http, gitlab_token=token).project_snapshot(
                WIDGET, today()
            )

    anonymous, signed_in = snapshots[None], snapshots["glpat-test"]
    assert isinstance(anonymous, ProjectSnapshot) and isinstance(signed_in, ProjectSnapshot)
    assert (anonymous.stars, anonymous.archived, anonymous.open_issues) == (300, None, None)
    assert (signed_in.archived, signed_in.open_issues) == (False, 5)


async def test_repositories_elsewhere_are_named_but_not_read(
    evidence: Evidence, sources: FakeSources
) -> None:
    sources.repo_host = "bitbucket.org"

    metadata = await evidence.version_metadata(WIDGET, AS_OF)
    assert metadata.source_repository == "bitbucket.org/acme/widget"
    with pytest.raises(SourceUnavailable, match=r"bitbucket\.org"):
        await evidence.commit_activity(WIDGET, AS_OF)


async def test_issue_activity_is_read_only_from_github(
    evidence: Evidence, sources: FakeSources
) -> None:
    sources.repo_host = "gitlab.com"

    with pytest.raises(SourceUnavailable, match="only for GitHub"):
        await evidence.issue_activity(WIDGET, AS_OF)


async def test_present_day_snapshots_are_withheld_from_historical_runs(evidence: Evidence) -> None:
    assert isinstance(await evidence.project_snapshot(WIDGET, AS_OF), Withheld)
    assert isinstance(await evidence.scorecard(WIDGET, AS_OF), Withheld)

    snapshot = await evidence.project_snapshot(WIDGET, today())
    assert isinstance(snapshot, ProjectSnapshot) and snapshot.stars == 1200
    assert isinstance(await evidence.scorecard(WIDGET, today()), ScorecardReport)


async def test_supply_chain_reads_the_latest_stable_release(evidence: Evidence) -> None:
    metadata = await evidence.version_metadata(WIDGET, AS_OF)
    footprint = await evidence.dependency_footprint(WIDGET, AS_OF)

    assert (metadata.version, metadata.licenses) == ("2.1.0", ["MIT"])
    assert footprint.direct_dependencies == ["attrs", "six"]
    assert (footprint.direct_count, footprint.transitive_count) == (2, 1)
    assert footprint.note  # resolved against today's registry, and says so


async def test_each_raw_record_is_fetched_once(evidence: Evidence, sources: FakeSources) -> None:
    await evidence.vulnerabilities(WIDGET, AS_OF)
    await evidence.vulnerabilities(WIDGET, date(2023, 1, 1))
    await evidence.releases(WIDGET, AS_OF)

    assert sum(r.url.host == "api.osv.dev" for r in sources.requests) == 1
    assert sum(r.url.path.endswith("/packages/widget") for r in sources.requests) == 1
