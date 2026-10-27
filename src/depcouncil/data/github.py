"""GitHub: commit history (queried by date window) and repository metadata.

https://docs.github.com/en/rest/commits/commits
"""

from datetime import UTC, datetime

import httpx
from pydantic import RootModel

from depcouncil.data.http import SourceModel, parse, request

SOURCE = "GitHub"
_BASE = "https://api.github.com"
_RATE_LIMIT_HINT = "set GITHUB_TOKEN to raise the limit from 60 to 5,000 requests an hour"


class GitHubUser(SourceModel):
    login: str


class GitSignature(SourceModel):
    name: str = ""
    email: str = ""
    date: datetime


class GitCommitBody(SourceModel):
    author: GitSignature | None = None
    committer: GitSignature | None = None


class Commit(SourceModel):
    sha: str
    commit: GitCommitBody
    author: GitHubUser | None = None

    @property
    def author_key(self) -> str:
        """Who wrote the commit: their GitHub login, else their git email or name."""
        if self.author is not None:
            return self.author.login
        signature = self.commit.author
        return (signature.email or signature.name) if signature else self.sha

    @property
    def committed_at(self) -> datetime | None:
        signature = self.commit.committer or self.commit.author
        return signature.date if signature else None


class _CommitPage(RootModel[list[Commit]]):
    pass


class Repository(SourceModel):
    full_name: str
    archived: bool = False
    created_at: datetime | None = None
    stargazers_count: int = 0
    forks_count: int = 0
    open_issues_count: int = 0


class GitHubClient:
    def __init__(self, http: httpx.AsyncClient, *, token: str | None = None) -> None:
        self._http = http
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def repository(self, repo: str) -> Repository:
        """Present-day repository metadata for ``owner/name``."""
        return parse(Repository, await self._get(f"/repos/{repo}"), SOURCE)

    async def last_commit(self, repo: str, *, until: datetime) -> Commit | None:
        commits, _ = await self._commits(repo, since=None, until=until, per_page=1)
        return commits[0] if commits else None

    async def count_commits(self, repo: str, *, since: datetime, until: datetime) -> int:
        # With one commit per page, the number of the last page is the number of commits.
        commits, last_page = await self._commits(repo, since=since, until=until, per_page=1)
        return last_page if last_page is not None else len(commits)

    async def recent_commits(
        self, repo: str, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[Commit]:
        """Up to ``limit`` of the newest commits in the window."""
        commits, _ = await self._commits(repo, since=since, until=until, per_page=limit)
        return commits

    async def _commits(
        self, repo: str, *, since: datetime | None, until: datetime, per_page: int
    ) -> tuple[list[Commit], int | None]:
        """One page of commits in the window, and the last page's number when there are more."""
        params: dict[str, str | int] = {"until": _iso(until), "per_page": per_page}
        if since is not None:
            params["since"] = _iso(since)
        # GitHub answers 409 for a repository with no commits.
        response = await self._get(f"/repos/{repo}/commits", params=params, allow_status={409})
        if response.status_code == 409:
            return [], 0
        commits = parse(_CommitPage, response, SOURCE).root
        last = response.links.get("last", {}).get("url")
        return commits, int(httpx.URL(last).params.get("page", "0")) if last else None

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        allow_status: set[int] | None = None,
    ) -> httpx.Response:
        return await request(
            self._http,
            SOURCE,
            "GET",
            _BASE + path,
            params=params,
            headers=self._headers,
            allow_status=frozenset(allow_status or ()),
            rate_limit_hint=_RATE_LIMIT_HINT,
        )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
