"""Code hosts: where a package's source repository lives, and what is read from it.

deps.dev names a version's source repository as ``host/path``. Evidence reads commits and
repository metadata through ``CodeHost``, which the GitHub and GitLab clients both implement.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Self


@dataclass(frozen=True)
class RepoRef:
    """A source repository: ``github.com/psf/requests`` is host ``github.com``, path
    ``psf/requests``."""

    host: str
    path: str

    @classmethod
    def parse(cls, key: str) -> Self | None:
        """Parse a deps.dev project key, ``<host>/<path>``; ``None`` if it has no path."""
        host, _, path = key.partition("/")
        return cls(host, path) if host and path else None

    def __str__(self) -> str:
        return f"{self.host}/{self.path}"


class HostCommit(Protocol):
    @property
    def author_key(self) -> str:
        """Who wrote the commit: an account name, else a git email or name."""
        ...

    @property
    def committed_at(self) -> datetime | None: ...


class HostRepository(Protocol):
    """Present-day repository metadata. A field is ``None`` when the host withholds it."""

    @property
    def created_at(self) -> datetime | None: ...

    @property
    def archived(self) -> bool | None: ...

    @property
    def stargazers_count(self) -> int: ...

    @property
    def forks_count(self) -> int: ...

    @property
    def open_issues_count(self) -> int | None: ...


class CodeHost(Protocol):
    """Commits and metadata of repositories on one host, addressed by path.

    Commit windows run from ``since`` up to, but not including, ``until``. A host that cannot
    count past some number of commits sets ``commit_count_limit``; a count that reaches it is
    a lower bound.
    """

    @property
    def commit_count_limit(self) -> int | None: ...

    async def repository(self, path: str, /) -> HostRepository: ...

    async def last_commit(self, path: str, /, *, until: datetime) -> HostCommit | None: ...

    async def count_commits(self, path: str, /, *, since: datetime, until: datetime) -> int: ...

    async def recent_commits(
        self, path: str, /, *, since: datetime, until: datetime, limit: int = 100
    ) -> Sequence[HostCommit]: ...
