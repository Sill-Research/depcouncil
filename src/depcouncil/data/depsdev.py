"""deps.dev (Open Source Insights): versions, licenses, dependency graphs and project metadata.

https://docs.deps.dev/api/v3/
"""

from datetime import datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from depcouncil.data.http import SourceModel, parse, request
from depcouncil.schemas import PackageRef

SOURCE = "deps.dev"
_BASE = "https://api.deps.dev/v3"


class _DepsDevModel(SourceModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class VersionKey(_DepsDevModel):
    system: str
    name: str
    version: str


class PackageVersion(_DepsDevModel):
    version_key: VersionKey
    published_at: datetime | None = None
    is_deprecated: bool = False
    deprecated_reason: str = ""

    @property
    def version(self) -> str:
        return self.version_key.version


class Package(_DepsDevModel):
    versions: list[PackageVersion] = []


class ProjectKey(_DepsDevModel):
    id: str


class RelatedProject(_DepsDevModel):
    project_key: ProjectKey
    relation_type: str = ""


class _Opaque(_DepsDevModel):
    """A record this project only counts, never reads."""


class VersionDetail(_DepsDevModel):
    version_key: VersionKey
    published_at: datetime | None = None
    licenses: list[str] = []
    related_projects: list[RelatedProject] = []
    slsa_provenances: list[_Opaque] = []
    attestations: list[_Opaque] = []

    def source_repository(self) -> str | None:
        """The GitHub ``owner/name`` the registry metadata names as this version's source."""
        for project in self.related_projects:
            key = project.project_key.id
            if project.relation_type == "SOURCE_REPO" and key.startswith("github.com/"):
                return key.removeprefix("github.com/")
        return None


class ScorecardCheck(_DepsDevModel):
    name: str
    score: int


class Scorecard(_DepsDevModel):
    date: datetime | None = None
    overall_score: float
    checks: list[ScorecardCheck] = []


class Project(_DepsDevModel):
    project_key: ProjectKey
    scorecard: Scorecard | None = None


class DependencyNode(_DepsDevModel):
    version_key: VersionKey
    relation: str


class DependencyGraph(_DepsDevModel):
    nodes: list[DependencyNode] = []
    error: str = ""


class DepsDevClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def package(self, pkg: PackageRef) -> Package:
        """Every published version, with its publish time and present-day deprecation flag."""
        return await self._get(Package, self._package_path(pkg))

    async def version(self, pkg: PackageRef, version: str) -> VersionDetail:
        return await self._get(VersionDetail, self._version_path(pkg, version))

    async def dependencies(self, pkg: PackageRef, version: str) -> DependencyGraph:
        """The resolved dependency graph of one version, resolved against today's registry."""
        return await self._get(DependencyGraph, f"{self._version_path(pkg, version)}:dependencies")

    async def project(self, repository: str) -> Project:
        """Present-day metadata for a GitHub repository, including its OpenSSF Scorecard."""
        return await self._get(Project, f"/projects/{quote(f'github.com/{repository}', safe='')}")

    @staticmethod
    def _package_path(pkg: PackageRef) -> str:
        return f"/systems/{pkg.ecosystem.depsdev_system}/packages/{quote(pkg.name, safe='')}"

    @classmethod
    def _version_path(cls, pkg: PackageRef, version: str) -> str:
        return f"{cls._package_path(pkg)}/versions/{quote(version, safe='')}"

    async def _get[M: BaseModel](self, model: type[M], path: str) -> M:
        return parse(model, await request(self._http, SOURCE, "GET", _BASE + path), SOURCE)
