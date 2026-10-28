"""The council's workflow: three analysts in parallel, an advocate/skeptic debate, then the chair.

The order never changes, so it is plain async Python rather than a graph: an
``asyncio.TaskGroup`` runs the analysts side by side and a loop runs the debate. Each step hands
the next one only typed outputs, never a shared mutable state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ModelSettings, RunUsage, UsageLimits

from depcouncil.agents import (
    ANALYSTS,
    CouncilDeps,
    advocate,
    chair,
    chair_prompt,
    debate_prompt,
    skeptic,
)
from depcouncil.anonymity import identifiers, redact_past_decision
from depcouncil.data import SourceUnavailable
from depcouncil.evidence import Evidence, open_evidence
from depcouncil.schemas import (
    AnalystReport,
    DebateTurn,
    Decision,
    Dimension,
    PackageRef,
    PastDecision,
)
from depcouncil.settings import Settings
from depcouncil.timeline import today

_SPEAKERS: tuple[tuple[Literal["advocate", "skeptic"], Agent[CouncilDeps, str]], ...] = (
    ("advocate", advocate),
    ("skeptic", skeptic),
)


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    @classmethod
    def of(cls, usage: RunUsage) -> Usage:
        return cls(
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tool_calls=usage.tool_calls,
        )


class CouncilResult(BaseModel):
    """The council's decision and everything behind it."""

    package: PackageRef
    as_of: date
    intended_use: str
    anonymized: bool
    reports: dict[Dimension, AnalystReport]
    debate: list[DebateTurn]
    past_decisions: list[PastDecision]
    decision: Decision
    models: dict[str, str]
    usage: Usage


@dataclass(frozen=True)
class AnalystReported:
    dimension: Dimension
    report: AnalystReport


@dataclass(frozen=True)
class TurnTaken:
    turn: DebateTurn


@dataclass(frozen=True)
class DecisionMade:
    decision: Decision


type CouncilEvent = AnalystReported | TurnTaken | DecisionMade


@dataclass(frozen=True)
class _Tier:
    model: str
    settings: ModelSettings


async def run_council(
    package: PackageRef | str,
    *,
    as_of: date | None = None,
    intended_use: str = "",
    settings: Settings | None = None,
    evidence: Evidence | None = None,
    past_decisions: Sequence[PastDecision] = (),
    anonymize: bool = False,
    on_event: Callable[[CouncilEvent], None] | None = None,
) -> CouncilResult:
    """Convene the council on a package and return its decision.

    Args:
        package: The package, or its ``ecosystem:name`` spec.
        as_of: Review the package as it stood on this date; defaults to today.
        intended_use: How the team plans to use the package; risks are weighed against it.
        settings: Models and limits; read from the environment when omitted.
        evidence: A data source to reuse, as a backtest does; a fresh one is opened when omitted.
        past_decisions: Earlier decisions and their lessons, shown to the chair. Recall them
            with ``Memory.recall`` for the same date, so that none of their outcomes postdates it.
        anonymize: Keep the package's name and repository from the debaters and the chair, so
            they judge the evidence rather than what they remember of the package.
        on_event: Called as each report, debate turn and the decision arrive.
    """
    settings = settings or Settings()
    pkg = PackageRef.parse(package) if isinstance(package, str) else package
    as_of = as_of or today()
    if as_of > today():
        raise ValueError(f"the analysis date {as_of} is in the future")
    emit = on_event or _ignore
    quick = _Tier(settings.quick_model, ModelSettings(thinking=settings.quick_effort))
    deep = _Tier(settings.deep_model, ModelSettings(thinking=settings.deep_effort))
    usage = RunUsage()

    async with AsyncExitStack() as stack:
        if evidence is None:
            evidence = await stack.enter_async_context(open_evidence(settings))
        names = await _identifiers(pkg, as_of, evidence) if anonymize else ()
        deps = CouncilDeps(pkg, as_of, intended_use, evidence, settings.max_tool_rounds, names)
        past = [redact_past_decision(p, names) for p in past_decisions]
        reports = await _brief(deps, quick, usage, emit)
        transcript = await _debate(deps, reports, quick, settings.debate_rounds, usage, emit)
        ruling = await chair.run(
            chair_prompt(reports, transcript, past),
            deps=deps,
            model=deep.model,
            model_settings=deep.settings,
        )
        usage.incr(ruling.usage)
        emit(DecisionMade(ruling.output))

    return CouncilResult(
        package=pkg,
        as_of=as_of,
        intended_use=intended_use,
        anonymized=anonymize,
        reports=reports,
        debate=transcript,
        past_decisions=past,
        decision=ruling.output,
        models={"quick": quick.model, "deep": deep.model},
        usage=Usage.of(usage),
    )


async def _brief(
    deps: CouncilDeps, tier: _Tier, usage: RunUsage, emit: Callable[[CouncilEvent], None]
) -> dict[Dimension, AnalystReport]:
    """Run every analyst at once and collect their reports."""
    # Tool rounds, the report itself, and two retries for a report that fails validation.
    limits = UsageLimits(request_limit=deps.max_tool_rounds + 3)

    async def report(dimension: Dimension) -> AnalystReport:
        result = await ANALYSTS[dimension].run(
            f"Investigate the {dimension.label} of {deps.package} and file your report.",
            deps=deps,
            model=tier.model,
            model_settings=tier.settings,
            usage_limits=limits,
        )
        usage.incr(result.usage)
        emit(AnalystReported(dimension, result.output))
        return result.output

    try:
        async with asyncio.TaskGroup() as group:
            tasks = {dimension: group.create_task(report(dimension)) for dimension in Dimension}
    except ExceptionGroup as failed:
        # One analyst failing cancels the rest; surface its error rather than the group.
        raise failed.exceptions[0] from failed
    return {dimension: task.result() for dimension, task in tasks.items()}


async def _debate(
    deps: CouncilDeps,
    reports: Mapping[Dimension, AnalystReport],
    tier: _Tier,
    rounds: int,
    usage: RunUsage,
    emit: Callable[[CouncilEvent], None],
) -> list[DebateTurn]:
    """Alternate advocate and skeptic turns, the advocate opening, for ``rounds`` exchanges."""
    transcript: list[DebateTurn] = []
    for _ in range(rounds):
        for speaker, agent in _SPEAKERS:
            result = await agent.run(
                debate_prompt(reports, transcript, speaker=speaker),
                deps=deps,
                model=tier.model,
                model_settings=tier.settings,
            )
            usage.incr(result.usage)
            turn = DebateTurn(speaker=speaker, argument=result.output)
            transcript.append(turn)
            emit(TurnTaken(turn))
    return transcript


async def _identifiers(pkg: PackageRef, as_of: date, evidence: Evidence) -> tuple[str, ...]:
    """The spellings an anonymized review withholds, including the source repository if known."""
    try:
        repository = await evidence.source_repository(pkg, as_of)
    except SourceUnavailable:
        repository = None  # the name itself is still withheld
    return identifiers(pkg, repository)


def _ignore(_event: CouncilEvent) -> None:
    pass
