"""A fake of deps.dev, OSV and GitHub for one package, served through ``httpx.MockTransport``."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote

import httpx

REPO = "acme/widget"
_PACKAGE_PATH = "/v3/systems/PYPI/packages/widget"


@dataclass
class FakeSources:
    """Canned responses for ``pypi:widget``.

    The GitHub commits endpoint honours ``since``, ``until`` and paging the way the real one
    does, so tests check the window the evidence layer asks for instead of trusting it.
    """

    versions: dict[str, str]
    vulns: list[dict[str, object]] = field(default_factory=list)
    commits: list[tuple[str, str]] = field(default_factory=list)  # (commit time, author login)
    licenses: list[str] = field(default_factory=lambda: ["MIT"])
    dependencies: list[tuple[str, str]] = field(default_factory=list)  # (name, DIRECT|INDIRECT)
    repo: str | None = REPO
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.raw_path.decode().partition("?")[0])
        match request.url.host:
            case "api.deps.dev":
                return self._depsdev(path)
            case "api.osv.dev":
                return httpx.Response(200, json={"vulns": self.vulns})
            case "api.github.com":
                return self._github(request, path)
            case _:
                return httpx.Response(404)

    def _depsdev(self, path: str) -> httpx.Response:
        if path == _PACKAGE_PATH:
            versions = [
                {"versionKey": _key(v), "publishedAt": t, "isDeprecated": False}
                for v, t in self.versions.items()
            ]
            return httpx.Response(200, json={"versions": versions})
        if path.startswith(f"{_PACKAGE_PATH}/versions/"):
            version, _, suffix = path.removeprefix(f"{_PACKAGE_PATH}/versions/").partition(":")
            if version not in self.versions:
                return httpx.Response(404)
            if suffix == "dependencies":
                nodes = [{"versionKey": _key(version), "relation": "SELF"}] + [
                    {
                        "versionKey": {"system": "PYPI", "name": name, "version": "1.0"},
                        "relation": rel,
                    }
                    for name, rel in self.dependencies
                ]
                return httpx.Response(200, json={"nodes": nodes})
            related = (
                [{"projectKey": {"id": f"github.com/{self.repo}"}, "relationType": "SOURCE_REPO"}]
                if self.repo
                else []
            )
            return httpx.Response(
                200,
                json={
                    "versionKey": _key(version),
                    "publishedAt": self.versions[version],
                    "licenses": self.licenses,
                    "relatedProjects": related,
                },
            )
        if self.repo and path == f"/v3/projects/github.com/{self.repo}":
            scorecard = {
                "date": "2026-01-01T00:00:00Z",
                "overallScore": 7.5,
                "checks": [{"name": "Maintained", "score": 10}],
            }
            return httpx.Response(
                200, json={"projectKey": {"id": f"github.com/{self.repo}"}, "scorecard": scorecard}
            )
        return httpx.Response(404)

    def _github(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"/repos/{self.repo}":
            return httpx.Response(
                200,
                json={
                    "full_name": self.repo,
                    "archived": False,
                    "created_at": "2019-01-01T00:00:00Z",
                    "stargazers_count": 1200,
                    "forks_count": 80,
                    "open_issues_count": 14,
                },
            )
        if path != f"/repos/{self.repo}/commits":
            return httpx.Response(404)
        params = request.url.params
        until = _time(params["until"])
        since = _time(params["since"]) if "since" in params else None
        matching = sorted(
            (
                (stamp, author)
                for stamp, author in self.commits
                if _time(stamp) < until and (since is None or _time(stamp) >= since)
            ),
            reverse=True,
        )
        per_page, page = int(params.get("per_page", "30")), int(params.get("page", "1"))
        chunk = matching[(page - 1) * per_page : page * per_page]
        body = [
            {
                "sha": f"{stamp}-{author}",
                "commit": {
                    "author": {"name": author, "email": f"{author}@example.com", "date": stamp},
                    "committer": {"name": author, "email": f"{author}@example.com", "date": stamp},
                },
                "author": {"login": author},
            }
            for stamp, author in chunk
        ]
        last_page = math.ceil(len(matching) / per_page)
        headers = {}
        if last_page > 1:
            url = f"https://api.github.com/repos/{self.repo}/commits?per_page={per_page}"
            headers["link"] = f'<{url}&page={last_page}>; rel="last"'
        return httpx.Response(200, json=body, headers=headers)


def widget() -> FakeSources:
    """A package whose history has something to hide from a review dated 2022-01-01.

    Known by then: five releases (latest stable 2.1.0), a HIGH advisory published under two ids
    whose earliest record dates it 2021-05-01 (fixed in 2.1.0 after 123 days), and a MODERATE
    advisory with no fix yet. Only later: release 3.0.0, a CRITICAL advisory, and two commits.
    """
    return FakeSources(
        versions={
            "1.0.0": "2020-01-10T00:00:00Z",
            "1.1.0": "2020-06-01T00:00:00Z",
            "2.0.0": "2021-03-01T00:00:00Z",
            "2.1.0": "2021-09-01T00:00:00Z",
            "2.2.0rc1": "2021-11-01T00:00:00Z",
            "3.0.0": "2023-02-01T00:00:00Z",
        },
        vulns=[
            _vuln(
                "PYSEC-2021-1",
                "2021-05-01T00:00:00Z",
                aliases=["CVE-2021-0001", "GHSA-aaaa-aaaa-aaaa"],
                fixed="2.1.0",
                versions=["1.0.0", "1.1.0", "2.0.0"],
            ),
            _vuln(
                "GHSA-aaaa-aaaa-aaaa",
                "2022-03-01T00:00:00Z",
                aliases=["CVE-2021-0001", "PYSEC-2021-1"],
                severity="HIGH",
                fixed="2.1.0",
                summary="Widget parser overflow",
                details="Parsing a crafted widget overflows a buffer.",
            ),
            _vuln(
                "GHSA-cccc-cccc-cccc",
                "2021-10-01T00:00:00Z",
                severity="MODERATE",
                fixed="3.0.0",
                versions=["2.0.0", "2.1.0"],
            ),
            _vuln(
                "GHSA-bbbb-bbbb-bbbb", "2022-06-01T00:00:00Z", severity="CRITICAL", fixed="3.0.0"
            ),
            _vuln(
                "GHSA-dddd-dddd-dddd",
                "2021-02-01T00:00:00Z",
                severity="LOW",
                withdrawn="2021-03-01T00:00:00Z",
            ),
        ],
        commits=[
            ("2021-06-01T10:00:00Z", "alice"),
            ("2021-08-01T10:00:00Z", "alice"),
            ("2021-12-20T10:00:00Z", "bob"),
            ("2022-02-01T10:00:00Z", "alice"),
            ("2023-01-15T10:00:00Z", "carol"),
            ("2023-05-01T10:00:00Z", "alice"),
        ],
        dependencies=[("six", "DIRECT"), ("attrs", "DIRECT"), ("idna", "INDIRECT")],
    )


def _vuln(
    vuln_id: str,
    published: str,
    *,
    aliases: list[str] | None = None,
    severity: str | None = None,
    fixed: str | None = None,
    versions: list[str] | None = None,
    withdrawn: str | None = None,
    summary: str = "",
    details: str = "",
) -> dict[str, object]:
    events: list[dict[str, str]] = [{"introduced": "0"}]
    if fixed:
        events.append({"fixed": fixed})
    record: dict[str, object] = {
        "id": vuln_id,
        "published": published,
        "aliases": aliases or [],
        "summary": summary,
        "details": details,
        "affected": [
            {
                "package": {"name": "widget", "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": events}],
                "versions": versions or [],
            }
        ],
    }
    if severity:
        record["database_specific"] = {"severity": severity}
    if withdrawn:
        record["withdrawn"] = withdrawn
    return record


def _key(version: str) -> dict[str, str]:
    return {"system": "PYPI", "name": "widget", "version": version}


def _time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)
