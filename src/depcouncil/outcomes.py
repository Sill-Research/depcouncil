"""Backtesting: what happened to a package after the analysis date, and whether the council's
call held up.

An outcome is measured from the same sources the council reads, over the horizon that follows
the analysis date, without any model calls. It counts as adverse when the package went dormant
(no release in the horizon and no commit in its last 180 days), or when a high or critical
advisory disclosed in the horizon went more than 90 days without a fix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from datetime import date, timedelta

from pydantic import BaseModel

from depcouncil.council import CouncilEvent, CouncilResult, run_council
from depcouncil.data import SourceUnavailable
from depcouncil.evidence import Advisory, Evidence, Severity, open_evidence
from depcouncil.schemas import Outcome, PackageRef, PastDecision, Rating, Score
from depcouncil.settings import Settings
from depcouncil.timeline import today

DEFAULT_HORIZON_DAYS = 540
SLOW_FIX_DAYS = 90
DORMANT_DAYS = 180


class BacktestResult(BaseModel):
    council: CouncilResult
    outcome: Outcome
    score: Score


def score(rating: Rating, *, adverse: bool) -> Score:
    """Grade a rating against the outcome: adopting with conditions into trouble is half right."""
    if rating is Rating.AVOID:
        return Score.HIT if adverse else Score.MISS
    if not adverse:
        return Score.HIT
    return Score.PARTIAL if rating is Rating.ADOPT_WITH_CONDITIONS else Score.MISS


async def measure_outcome(
    package: PackageRef | str,
    *,
    as_of: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    evidence: Evidence,
) -> Outcome:
    """What happened to the package in the ``horizon_days`` after ``as_of``."""
    pkg = PackageRef.parse(package) if isinstance(package, str) else package
    end = as_of + timedelta(days=horizon_days)
    if end > today():
        raise ValueError(
            f"the {horizon_days}-day horizon after {as_of} ends on {end}, which hasn't happened "
            "yet; use an earlier analysis date or a shorter horizon"
        )
    releases, advisories, commits = await asyncio.gather(
        evidence.known_releases(pkg, end),
        evidence.known_advisories(pkg, end),
        _commits_before(evidence, pkg, end),
    )
    new_releases = [r for r in releases if r.published > as_of]
    new_advisories = [a for a in advisories if a.disclosed > as_of]
    severe = [a for a in new_advisories if a.severity in (Severity.HIGH, Severity.CRITICAL)]
    slow = [a for a in severe if _days_unfixed(a, end) > SLOW_FIX_DAYS]
    dormant = not new_releases and not commits

    findings = [
        f"releases in the horizon: {len(new_releases)}",
        f"advisories disclosed in the horizon: {len(new_advisories)} "
        f"({len(severe)} high or critical)",
    ]
    if commits is not None:
        findings.append(f"commits in the last {DORMANT_DAYS} days of the horizon: {commits}")
    if dormant:
        findings.append(
            f"dormant: no release in the horizon and no commits in its last {DORMANT_DAYS} days"
        )
    findings += [_slow_fix_finding(a, end) for a in slow]

    return Outcome(
        package=pkg,
        start=as_of,
        end=end,
        releases=len(new_releases),
        commits_last_180_days=commits,
        dormant=dormant,
        advisories=len(new_advisories),
        severe_advisories=len(severe),
        slow_or_unfixed_severe=[a.id for a in slow],
        adverse=dormant or bool(slow),
        findings=findings,
    )


async def backtest(
    package: PackageRef | str,
    *,
    as_of: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    intended_use: str = "",
    settings: Settings | None = None,
    evidence: Evidence | None = None,
    past_decisions: Sequence[PastDecision] = (),
    on_event: Callable[[CouncilEvent], None] | None = None,
) -> BacktestResult:
    """Convene the council as of a past date, then grade its call against what happened next.

    ``past_decisions`` are passed to ``run_council``; recall them for ``as_of`` so that none of
    their outcomes postdates it.
    """
    settings = settings or Settings()
    pkg = PackageRef.parse(package) if isinstance(package, str) else package
    async with AsyncExitStack() as stack:
        if evidence is None:
            evidence = await stack.enter_async_context(open_evidence(settings))
        # Measure first: it costs no model calls, and fails fast on a horizon still to come.
        outcome = await measure_outcome(
            pkg, as_of=as_of, horizon_days=horizon_days, evidence=evidence
        )
        council = await run_council(
            pkg,
            as_of=as_of,
            intended_use=intended_use,
            settings=settings,
            evidence=evidence,
            past_decisions=past_decisions,
            on_event=on_event,
        )
    return BacktestResult(
        council=council,
        outcome=outcome,
        score=score(council.decision.rating, adverse=outcome.adverse),
    )


async def _commits_before(evidence: Evidence, pkg: PackageRef, end: date) -> int | None:
    """Commits in the ``DORMANT_DAYS`` before ``end``; ``None`` without a readable repository."""
    try:
        activity = await evidence.commit_activity(pkg, end, window_days=DORMANT_DAYS)
    except SourceUnavailable:
        return None
    return activity.commits_in_window


def _days_unfixed(advisory: Advisory, end: date) -> int:
    if advisory.days_to_fix is not None:
        return advisory.days_to_fix
    return (end - advisory.disclosed).days


def _slow_fix_finding(advisory: Advisory, end: date) -> str:
    days = _days_unfixed(advisory, end)
    status = f"fixed after {days} days" if advisory.fix_released else f"unfixed after {days} days"
    return f"{advisory.id} ({advisory.severity}, disclosed {advisory.disclosed}): {status}"
