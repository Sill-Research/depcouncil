"""Markdown views of council results, outcomes and backtests."""

from collections.abc import Sequence

from depcouncil.council import CouncilResult
from depcouncil.outcomes import BacktestResult, Outcome, Score
from depcouncil.schemas import Rating

_RATINGS = {
    Rating.ADOPT: "Adopt",
    Rating.ADOPT_WITH_CONDITIONS: "Adopt with conditions",
    Rating.AVOID: "Avoid",
}


def council_markdown(result: CouncilResult) -> str:
    decision = result.decision
    use = f" · intended use: {result.intended_use}" if result.intended_use else ""
    lines = [
        f"# {result.package}: {_RATINGS[decision.rating]}",
        f"*{decision.confidence.capitalize()} confidence · reviewed as of {result.as_of}{use}*",
        "",
        decision.rationale,
        *_bullets("Deciding factors", decision.deciding_factors),
        *_bullets("Conditions", decision.conditions),
        *_bullets("Revisit if", decision.revisit_if),
        "",
        "## Analyst reports",
    ]
    for dimension, report in result.reports.items():
        lines += ["", f"### {dimension.label.capitalize()}: {report.risk} risk", "", report.summary]
        lines += _bullets("Strengths", report.strengths)
        lines += _bullets("Concerns", report.concerns)
        lines += _bullets("Data gaps", report.data_gaps)
    lines += ["", "## Debate"]
    lines += [f"\n**{turn.speaker.capitalize()}:** {turn.argument}" for turn in result.debate]
    usage = result.usage
    models = ", ".join(f"{tier} {model}" for tier, model in result.models.items())
    lines += [
        "",
        "---",
        f"*{models} · {usage.requests} model requests · {usage.tool_calls} tool calls · "
        f"{usage.input_tokens:,} input and {usage.output_tokens:,} output tokens*",
    ]
    return "\n".join(lines)


def outcome_markdown(outcome: Outcome) -> str:
    verdict = "Adverse" if outcome.adverse else "No adverse outcome"
    return "\n".join(
        [
            f"## {outcome.package}, {outcome.start} to {outcome.end}: {verdict}",
            "",
            *(f"- {finding}" for finding in outcome.findings),
        ]
    )


def backtest_markdown(results: Sequence[BacktestResult]) -> str:
    lines = [
        "| Package | Rating | Outcome | Score |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        outcome = "adverse" if result.outcome.adverse else "fine"
        rating = _RATINGS[result.council.decision.rating]
        lines.append(f"| {result.council.package} | {rating} | {outcome} | {result.score} |")
    hits = sum(r.score is Score.HIT for r in results) + 0.5 * sum(
        r.score is Score.PARTIAL for r in results
    )
    lines += ["", f"**Score: {hits:g} / {len(results)}** (a partial counts as half)"]
    return "\n".join(lines)


def _bullets(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return []
    return ["", f"**{title}**", "", *(f"- {item}" for item in items)]
