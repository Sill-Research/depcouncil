"""The ``depcouncil`` command line: ``analyze``, ``outcome`` and ``backtest``."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from typing import Annotated, Any, NoReturn

import pydantic_ai
import typer
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_ai import AgentRunError, ModelAPIError, UserError
from rich.console import Console
from rich.markdown import Markdown

from depcouncil.council import AnalystReported, CouncilEvent, DecisionMade, TurnTaken, run_council
from depcouncil.evidence import open_evidence
from depcouncil.outcomes import DEFAULT_HORIZON_DAYS, BacktestResult, backtest, measure_outcome
from depcouncil.render import backtest_markdown, council_markdown, outcome_markdown
from depcouncil.schemas import PackageRef
from depcouncil.settings import Settings

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
    json: Json = False,
) -> None:
    """Convene the council on a package and print its decision."""
    pkg, settings = _package(package), _settings(rounds)
    with err.status("The council is in session..."):
        result = _run(
            run_council(
                pkg,
                as_of=as_of.date() if as_of else None,
                intended_use=use,
                settings=settings,
                on_event=_report_progress,
            )
        )
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
    json: Json = False,
) -> None:
    """Review packages as of a past date, then score each call against what happened next."""
    refs, settings = [_package(p) for p in packages], _settings(rounds)
    results: list[BacktestResult] = []
    for ref in refs:
        with err.status(f"Backtesting {ref}..."):
            results.append(
                _run(
                    backtest(
                        ref,
                        as_of=as_of.date(),
                        horizon_days=horizon_days,
                        intended_use=use,
                        settings=settings,
                        on_event=_report_progress,
                    )
                )
            )
        err.print(f"[bold]{ref}[/]: {results[-1].council.decision.rating} -> {results[-1].score}")
    if json:
        out.print_json(data=[r.model_dump(mode="json") for r in results])
    else:
        for result in results:
            out.print(Markdown(outcome_markdown(result.outcome)))
        out.print(Markdown(backtest_markdown(results)))


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
