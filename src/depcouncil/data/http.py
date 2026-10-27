"""HTTP plumbing shared by the data sources: one error type, and bodies parsed into models."""

from collections.abc import Mapping

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError


class SourceUnavailable(Exception):
    """A data source could not answer: an unknown package, a rate limit, or a network failure."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


class SourceModel(BaseModel):
    """Base for API response models: immutable, and tolerant of fields this project doesn't read."""

    model_config = ConfigDict(frozen=True, extra="ignore")


async def request(
    http: httpx.AsyncClient,
    source: str,
    method: str,
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json: object = None,
    headers: Mapping[str, str] | None = None,
    allow_status: frozenset[int] = frozenset(),
    rate_limit_hint: str = "",
) -> httpx.Response:
    """Send a request, turning transport failures and error statuses into ``SourceUnavailable``."""
    try:
        response = await http.request(method, url, params=params, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(source, f"request failed ({type(exc).__name__})") from exc
    if response.is_success or response.status_code in allow_status:
        return response
    if response.status_code == 404:
        raise SourceUnavailable(source, "not found")
    if response.status_code in (403, 429):
        hint = f"; {rate_limit_hint}" if rate_limit_hint else ""
        raise SourceUnavailable(source, f"rate limited (HTTP {response.status_code}){hint}")
    raise SourceUnavailable(source, f"HTTP {response.status_code}")


def parse[M: BaseModel](model: type[M], response: httpx.Response, source: str) -> M:
    """Validate a response body against ``model``."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise SourceUnavailable(
            source, f"unexpected response shape ({exc.error_count()} errors)"
        ) from exc
