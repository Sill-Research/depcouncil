"""GitLab (gitlab.com): commit history (queried by date window) and project metadata.

https://docs.gitlab.com/api/commits/

GitLab gives no total for a commit query, so commits are counted by paging, up to
``commit_count_limit``. Without a token, project metadata leaves out the archive flag and the
open issue count.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from pydantic import Field, RootModel

from depcouncil.data.http import SourceModel, parse, request

SOURCE = "GitLab"
_BASE = "https://gitlab.com/api/v4"
_PAGE_SIZE = 100
_MAX_PAGES = 10


class GitLabCommit(SourceModel):
    id: str
    author_name: str = ""
    author_email: str = ""
    committed_date: datetime | None = None

    @property
    def author_key(self) -> str:
        """Who wrote the commit: their git email, else their name."""
        return self.author_email or self.author_name or self.id

    @property
    def committed_at(self) -> datetime | None:
        return self.committed_date


class _CommitPage(RootModel[list[GitLabCommit]]):
    pass


class GitLabProject(SourceModel):
    path_with_namespace: str
    created_at: datetime | None = None
    archived: bool | None = None
    stargazers_count: int = Field(default=0, validation_alias="star_count")
    forks_count: int = 0
    open_issues_count: int | None = None


class GitLabClient:
    def __init__(self, http: httpx.AsyncClient, *, token: str | None = None) -> None:
        self._http = http
        self._headers = {"PRIVATE-TOKEN": token} if token else {}

    @property
    def commit_count_limit(self) -> int | None:
        return _PAGE_SIZE * _MAX_PAGES

    async def repository(self, path: str) -> GitLabProject:
        """Present-day metadata for the project at ``path``, e.g. ``inkscape/extensions``."""
        return parse(GitLabProject, await self._get(_project(path)), SOURCE)

    async def last_commit(self, path: str, *, until: datetime) -> GitLabCommit | None:
        commits, _ = await self._commits(path, since=None, until=until, per_page=1, page=1)
        return commits[0] if commits else None

    async def count_commits(self, path: str, *, since: datetime, until: datetime) -> int:
        """Commits in the window, counted page by page up to ``commit_count_limit``."""
        count, page = 0, 1
        for _ in range(_MAX_PAGES):
            commits, next_page = await self._commits(
                path, since=since, until=until, per_page=_PAGE_SIZE, page=page
            )
            count += len(commits)
            if next_page is None:
                break
            page = next_page
        return count

    async def recent_commits(
        self, path: str, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[GitLabCommit]:
        """Up to ``limit`` of the newest commits in the window."""
        commits, _ = await self._commits(path, since=since, until=until, per_page=limit, page=1)
        return commits

    async def _commits(
        self, path: str, *, since: datetime | None, until: datetime, per_page: int, page: int
    ) -> tuple[list[GitLabCommit], int | None]:
        """One page of the default branch's commits in the window, and the next page's number."""
        # GitLab's `until` includes its own second; its timestamps are whole seconds.
        params: dict[str, str | int] = {
            "until": _iso(until - timedelta(seconds=1)),
            "per_page": per_page,
            "page": page,
        }
        if since is not None:
            params["since"] = _iso(since)
        response = await self._get(f"{_project(path)}/repository/commits", params=params)
        next_page = response.headers.get("x-next-page", "")
        return parse(_CommitPage, response, SOURCE).root, int(next_page) if next_page else None

    async def _get(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        return await request(
            self._http,
            SOURCE,
            "GET",
            _BASE + path,
            params=params,
            headers=self._headers,
            rate_limit_hint="set GITLAB_TOKEN to raise the limit",
        )


def _project(path: str) -> str:
    return f"/projects/{quote(path, safe='')}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
