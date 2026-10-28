"""The ``depcouncil`` command line: ``analyze``, ``outcome``, ``backtest`` and ``settle``.

When ``DEPCOUNCIL_MEMORY_PATH`` is set, ``analyze`` and ``backtest`` show the chair the past
decisions known by the analysis date and remember the new one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date, datetime
from typing import Annotated, Any, NoReturn

import pydantic_ai
import typer
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_ai import AgentRunError, ModelAPIError, UserError
from rich.console import Console
from rich.markdown import Markdown

from depcouncil.council import (
    AnalystReported,
    CouncilEvent,
    CouncilResult,
    DecisionMade,
    TurnTaken,
    run_council,
)
from depcouncil.evidence import open_evidence
from depcouncil.memory import Memory, MemoryEntry, reflect, settle
from depcouncil.outcomes import DEFAULT_HORIZON_DAYS, BacktestResult, backtest, measure_outcome
from depcouncil.render import (
    backtest_markdown,
    council_markdown,
    outcome_markdown,
    settlement_markdown,
)
from depcouncil.schemas import PackageRef, PastDecision
from depcouncil.settings import Settings
from depcouncil.timeline import today

app = typer.Typer(
    help="A council of AI agents that decides whether to adopt an open-source dependency.",
    no_args_is_help=True,
    add_completion=False,
)
out = Console()
err = Console(stderr=True)

Package = Annotated[
    str, typer.Argument(help="ecosystem:name, e.g. pypi:requests, npm:express, cargo:serde.")
]
AsOf = Annotated[
    datetime | None,
    typer.Option(
        "--as-of",
        formats=["%Y-%m-%d"],
        help="Review the package as it stood on this date. Defaults to today.",
    ),
]
RequiredAsOf = Annotated[
    datetime,
    typer.Option(
        "--as-of", formats=["%Y-%m-%d"], help="The past date to review the package as of."
    ),
]
Horizon = Annotated[
    int,
    typer.Option(min=30, help="Days after the analysis date over which to measure the outcome."),
]
Use = Annotated[
    str,
    typer.Option("--use", help="How you plan to use the package; risks are weighed against it."),
]
Rounds = Annotated[
    int | None,
    typer.Option(
        min=1,
        max=5,
        help="Advocate/skeptic exchanges. Defaults to DEPCOUNCIL_DEBATE_ROUNDS, else 1.",
    ),
]
Json = Annotated[bool, typer.Option("--json", help="Print the full result as JSON.")]
Anonymize = Annotated[
    bool,
    typer.Option(
        "--anonymize/--named",
        help="Keep the package's name and repository from the debaters and the chair, so they "
        "judge the evidence rather than what they remember of the package.",
    ),
]


@app.callback()
def _startup() -> None:
    load_dotenv()  # Provider keys such as ANTHROPIC_API_KEY may live in .env.
    pydantic_ai.BANNER_ENABLED = False


@app.command()
def analyze(
    package: Package,
    as_of: AsOf = None,
    use: Use = "",
    rounds: Rounds = None,
    anonymize: Anonymize = False,
    json: Json = False,
) -> None:
    """Convene the council on a package and print its decision."""
    pkg, settings = _package(package), _settings(rounds)
    memory, day = _memory(settings), as_of.date() if as_of else today()

    async def convene() -> CouncilResult:
        result = await run_council(
            pkg,
            as_of=day,
            intended_use=use,
            settings=settings,
            past_decisions=_recall(memory, pkg, day),
            anonymize=anonymize,
            on_event=_report_progress,
        )
        if memory:
            entry = MemoryEntry.of(result)
            memory.add(entry)
            err.print(f"  [green]✓[/] remembered the decision; settle it after {entry.due}")
        return result

    with err.status("The council is in session..."):
        result = _run(convene())
    if json:
        out.print_json(result.model_dump_json())
    else:
        out.print(Markdown(council_markdown(result)))


@app.command()
def outcome(
    package: Package,
    as_of: RequiredAsOf,
    horizon_days: Horizon = DEFAULT_HORIZON_DAYS,
    json: Json = False,
) -> None:
    """Measure what happened to a package after a date. Makes no model calls."""
    pkg, settings = _package(package), _settings(None)

    async def measure() -> Any:
        async with open_evidence(settings) as evidence:
            return await measure_outcome(
                pkg, as_of=as_of.date(), horizon_days=horizon_days, evidence=evidence
            )

    result = _run(measure())
    if json:
        out.print_json(result.model_dump_json())
    else:
        out.print(Markdown(outcome_markdown(result)))


@app.command(name="backtest")
def backtest_command(
    packages: Annotated[list[str], typer.Argument(help="One or more ecosystem:name packages.")],
    as_of: RequiredAsOf,
    horizon_days: Horizon = DEFAULT_HORIZON_DAYS,
    use: Use = "",
    rounds: Rounds = None,
    anonymize: Anonymize = True,
    json: Json = False,
) -> None:
    """Review packages as of a past date, then score each call against what happened next."""
    refs, settings = [_package(p) for p in packages], _settings(rounds)
    memory, day = _memory(settings), as_of.date()

    async def run(ref: PackageRef) -> BacktestResult:
        result = await backtest(
            ref,
            as_of=day,
            horizon_days=horizon_days,
            intended_use=use,
            settings=settings,
            past_decisions=_recall(memory, ref, day),
            anonymize=anonymize,
            on_event=_report_progress,
        )
        if memory:
            entry = MemoryEntry.of(result.council, horizon_days=horizon_days)
            memory.add(await reflect(entry, result.outcome, settings=settings))
            err.print("  [green]✓[/] remembered the decision with its outcome and a lesson")
        return result

    results: list[BacktestResult] = []
    for ref in refs:
        with err.status(f"Backtesting {ref}..."):
            results.append(_run(run(ref)))
        err.print(f"[bold]{ref}[/]: {results[-1].council.decision.rating} -> {results[-1].score}")
    if json:
        out.print_json(data=[r.model_dump(mode="json") for r in results])
    else:
        for result in results:
            out.print(Markdown(outcome_markdown(result.outcome)))
        out.print(Markdown(backtest_markdown(results)))


@app.command(name="settle")
def settle_command(json: Json = False) -> None:
    """Measure remembered decisions whose horizon has ended, and draw a lesson from each."""
    settings = _settings(None)
    memory = _memory(settings)
    if memory is None:
        _fail("memory is off; set DEPCOUNCIL_MEMORY_PATH to the file decisions are kept in")
    with err.status("Settling remembered decisions..."):
        settled = _run(settle(memory, settings=settings))
    if json:
        out.print_json(data=[entry.model_dump(mode="json") for entry in settled])
    elif settled:
        out.print(Markdown(settlement_markdown(settled)))
    else:
        next_due = min((entry.due for entry in memory.pending()), default=None)
        err.print(
            f"No remembered decision is due yet; the next is due on {next_due}."
            if next_due
            else "No remembered decision is pending."
        )


def _recall(memory: Memory | None, pkg: PackageRef, as_of: date) -> list[PastDecision]:
    if memory is None:
        return []
    past = memory.recall(pkg, as_of)
    err.print(f"  [green]✓[/] past decisions recalled as of {as_of}: {len(past)}")
    return past


def _report_progress(event: CouncilEvent) -> None:
    match event:
        case AnalystReported(dimension=dimension, report=report):
            err.print(f"  [green]✓[/] {dimension.label} analyst reported {report.risk} risk")
        case TurnTaken(turn=turn):
            err.print(f"  [green]✓[/] {turn.speaker} made their case")
        case DecisionMade(decision=decision):
            err.print(f"  [green]✓[/] the chair ruled: {decision.rating}")


def _package(spec: str) -> PackageRef:
    try:
        return PackageRef.parse(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _settings(rounds: int | None) -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        _fail(f"invalid DEPCOUNCIL_* setting:\n{exc}")
    return settings.model_copy(update={"debate_rounds": rounds}) if rounds else settings


def _memory(settings: Settings) -> Memory | None:
    return Memory(settings.memory_path) if settings.memory_path else None


def _run[T](work: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(work)
    except (ValueError, UserError) as exc:
        _fail(str(exc))
    except ModelAPIError as exc:
        _fail(f"the model request failed: {exc}")
    except AgentRunError as exc:
        _fail(f"an agent run failed: {exc}")


def _fail(message: str) -> NoReturn:
    err.print(f"[red]error:[/] {message}")
    raise typer.Exit(1)
