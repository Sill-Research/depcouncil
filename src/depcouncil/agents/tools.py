"""The data tools the analysts call, grouped into one toolset per analyst.

Every tool reads the package and the analysis date from the run's deps rather than from its
arguments, so a model cannot ask for data past the date it is analysing. Source failures come
back as ``Unavailable`` values for the model to report as data gaps, not as exceptions.
"""

from collections.abc import Awaitable
from typing import Annotated

from pydantic import Field
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from depcouncil.agents.deps import CouncilDeps
from depcouncil.data import SourceUnavailable
from depcouncil.evidence import (
    AdvisoryDetail,
    CommitActivity,
    DependencyFootprint,
    ProjectSnapshot,
    ReleaseHistory,
    ScorecardReport,
    Unavailable,
    VersionMetadata,
    VulnerabilityRecord,
    Withheld,
)

_maintenance = FunctionToolset[CouncilDeps]()
_security = FunctionToolset[CouncilDeps]()
_supply_chain = FunctionToolset[CouncilDeps]()


async def _answer[T](query: Awaitable[T]) -> T | Unavailable:
    try:
        return await query
    except SourceUnavailable as exc:
        return Unavailable(unavailable=str(exc))


@_maintenance.tool
async def release_history(ctx: RunContext[CouncilDeps]) -> ReleaseHistory | Unavailable:
    """Releases published up to the analysis date: cadence, recency and major-version churn."""
    return await _answer(ctx.deps.evidence.releases(ctx.deps.package, ctx.deps.as_of))


@_maintenance.tool
async def commit_activity(
    ctx: RunContext[CouncilDeps], window_days: Annotated[int, Field(ge=30, le=1095)] = 365
) -> CommitActivity | Unavailable:
    """Commits to the source repository in the window before the analysis date: volume, recency,
    and how concentrated the work is among authors.

    Args:
        window_days: How many days before the analysis date to look back, from 30 to 1095.
    """
    deps = ctx.deps
    return await _answer(
        deps.evidence.commit_activity(deps.package, deps.as_of, window_days=window_days)
    )


@_maintenance.tool
async def project_snapshot(
    ctx: RunContext[CouncilDeps],
) -> ProjectSnapshot | Withheld | Unavailable:
    """Community size (stars, forks, open issues) and archive or deprecation status as they stand
    today. Withheld when the analysis date is in the past."""
    return await _answer(ctx.deps.evidence.project_snapshot(ctx.deps.package, ctx.deps.as_of))


@_security.tool
async def vulnerability_record(ctx: RunContext[CouncilDeps]) -> VulnerabilityRecord | Unavailable:
    """Security advisories disclosed by the analysis date, merged across vulnerability databases:
    their severity, how fast fixes shipped, and which were still unfixed or affected the latest
    release."""
    return await _answer(ctx.deps.evidence.vulnerabilities(ctx.deps.package, ctx.deps.as_of))


async def _when_advisories_known(
    ctx: RunContext[CouncilDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Offer `advisory_details` only when an advisory was disclosed by the analysis date."""
    try:
        known = await ctx.deps.evidence.known_advisories(ctx.deps.package, ctx.deps.as_of)
    except SourceUnavailable:
        return None
    return tool_def if known else None


@_security.tool(prepare=_when_advisories_known)
async def advisory_details(
    ctx: RunContext[CouncilDeps], advisory_id: str
) -> AdvisoryDetail | Unavailable:
    """The full text of one advisory listed by `vulnerability_record`.

    Args:
        advisory_id: The advisory's id or any of its aliases, e.g. a CVE or GHSA id.
    """
    deps = ctx.deps
    try:
        detail = await deps.evidence.advisory(deps.package, deps.as_of, advisory_id)
    except SourceUnavailable as exc:
        return Unavailable(unavailable=str(exc))
    if detail is None:
        # A plain answer rather than `ModelRetry`: a model that keeps guessing would otherwise
        # exhaust its retries and fail the whole council. The answer is the same whether the id
        # is unknown or disclosed after the date, so a guess at a future advisory learns nothing.
        return Unavailable(
            unavailable=f"No advisory {advisory_id!r} is on record for this package as of the "
            "analysis date. Use an id listed by vulnerability_record."
        )
    return detail


@_security.tool
async def security_practices(
    ctx: RunContext[CouncilDeps],
) -> ScorecardReport | Withheld | Unavailable:
    """The repository's OpenSSF Scorecard as it stands today: 0-10 per check (-1 when a check does
    not apply). Withheld when the analysis date is in the past."""
    return await _answer(ctx.deps.evidence.scorecard(ctx.deps.package, ctx.deps.as_of))


@_supply_chain.tool
async def release_metadata(ctx: RunContext[CouncilDeps]) -> VersionMetadata | Unavailable:
    """License, SLSA provenance and attestations of the latest release by the analysis date."""
    return await _answer(ctx.deps.evidence.version_metadata(ctx.deps.package, ctx.deps.as_of))


@_supply_chain.tool
async def dependency_footprint(ctx: RunContext[CouncilDeps]) -> DependencyFootprint | Unavailable:
    """The direct and transitive dependencies that the latest release by the analysis date pulls
    in."""
    return await _answer(ctx.deps.evidence.dependency_footprint(ctx.deps.package, ctx.deps.as_of))


def _within_tool_rounds(
    ctx: RunContext[CouncilDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Offer the data tools only for the first ``max_tool_rounds`` model turns.

    After that the analyst's only option is to file its report, so a model that keeps calling
    tools finishes with what it has instead of running into the request limit.
    """
    return tool_defs if ctx.run_step <= ctx.deps.max_tool_rounds else []


MAINTENANCE_TOOLS: AbstractToolset[CouncilDeps] = _maintenance.prepared(_within_tool_rounds)
SECURITY_TOOLS: AbstractToolset[CouncilDeps] = _security.prepared(_within_tool_rounds)
SUPPLY_CHAIN_TOOLS: AbstractToolset[CouncilDeps] = _supply_chain.prepared(_within_tool_rounds)
