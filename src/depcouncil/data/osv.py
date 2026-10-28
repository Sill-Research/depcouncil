"""OSV (osv.dev): security advisories for a package, aggregated across vulnerability databases.

https://google.github.io/osv.dev/post-v1-query/
"""

from datetime import datetime
from typing import Annotated, cast

import httpx
from cvss import CVSS2, CVSS3, CVSS4, CVSSError  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BeforeValidator

from depcouncil.data.http import SourceModel, parse, request
from depcouncil.schemas import PackageRef

SOURCE = "OSV"
_QUERY_URL = "https://api.osv.dev/v1/query"
_MAX_PAGES = 10
_CVSS = {"CVSS_V2": CVSS2, "CVSS_V3": CVSS3, "CVSS_V4": CVSS4}


def _text_or_none(value: object) -> object:
    # `database_specific` is free-form per database; read its fields only when they are strings.
    return value if isinstance(value, str) else None


class OsvEvent(SourceModel):
    introduced: str | None = None
    fixed: str | None = None
    last_affected: str | None = None


class OsvRange(SourceModel):
    type: str = ""
    events: list[OsvEvent] = []


class OsvPackage(SourceModel):
    name: str = ""
    ecosystem: str = ""


class OsvAffected(SourceModel):
    package: OsvPackage | None = None
    ranges: list[OsvRange] = []
    versions: list[str] = []


class OsvDatabaseSpecific(SourceModel):
    severity: Annotated[str | None, BeforeValidator(_text_or_none)] = None
    nvd_published_at: Annotated[datetime | None, BeforeValidator(_text_or_none)] = None


class OsvSeverity(SourceModel):
    type: str = ""
    score: str = ""

    def cvss(self) -> tuple[float, str] | None:
        """The CVSS base score and its rating ("Low" to "Critical"), or ``None`` when this is not
        a CVSS vector that can be scored."""
        calculator = _CVSS.get(self.type)
        if calculator is None:
            return None
        try:
            vector = calculator(self.score)
        except CVSSError:
            return None
        if vector.base_score is None:
            return None
        # The library is untyped; `severities()` gives the base, temporal and environmental ratings.
        rating = cast("str", vector.severities()[0])
        return float(vector.base_score), rating


class OsvVulnerability(SourceModel):
    id: str
    summary: str = ""
    details: str = ""
    aliases: list[str] = []
    published: datetime | None = None
    withdrawn: datetime | None = None
    severity: list[OsvSeverity] = []
    database_specific: OsvDatabaseSpecific | None = None
    affected: list[OsvAffected] = []


class _QueryPage(SourceModel):
    vulns: list[OsvVulnerability] = []
    next_page_token: str | None = None


class OsvClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def vulnerabilities(self, pkg: PackageRef) -> list[OsvVulnerability]:
        """Every record that names the package, from every database OSV aggregates.

        One vulnerability often appears several times (GHSA, PYSEC, ...) under ids that alias
        each other; merging them is the caller's job.
        """
        records: list[OsvVulnerability] = []
        query: dict[str, object] = {
            "package": {"name": pkg.name, "ecosystem": pkg.ecosystem.osv_ecosystem}
        }
        for _ in range(_MAX_PAGES):
            response = await request(self._http, SOURCE, "POST", _QUERY_URL, json=query)
            page = parse(_QueryPage, response, SOURCE)
            records.extend(page.vulns)
            if not page.next_page_token:
                break
            query["page_token"] = page.next_page_token
        return records
