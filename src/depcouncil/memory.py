"""The council's memory: the decisions it made, what happened after them, and the lessons drawn.

Each decision is one line of JSON. A backtest's decision is stored settled, with its outcome and
the reflector's lesson; a live decision is stored pending until its horizon has passed and
``settle`` measures it. A later line about the same review (package, analysis date and intended
use) replaces an earlier one, so a rerun or a settlement never counts a decision twice.

Recall applies the point-in-time rule to memory: a review dated ``as_of`` is shown only the
decisions whose outcome was known by then, so a backtest cannot learn from an outcome that had
not happened yet. The lessons themselves are written by a model after the fact, which leaks no
more than the model's own training data already does.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import date, timedelta
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelSettings

from depcouncil.agents import reflection_prompt, reflector
from depcouncil.council import CouncilResult
from depcouncil.evidence import Evidence, open_evidence
from depcouncil.outcomes import DEFAULT_HORIZON_DAYS, measure_outcome, score
from depcouncil.schemas import Decision, Outcome, PackageRef, PastDecision, Score
from depcouncil.settings import Settings
from depcouncil.timeline import today

SAME_PACKAGE_RECALL = 5
OTHER_PACKAGE_RECALL = 3


class Settlement(BaseModel):
    """What happened after a decision, how the call scored, and the lesson drawn from it."""

    outcome: Outcome
    score: Score
    lesson: str


class MemoryEntry(BaseModel):
    """One council decision, and once its horizon has passed, its settlement."""

    package: PackageRef
    as_of: date
    intended_use: str
    horizon_days: int
    decision: Decision
    models: dict[str, str]
    anonymized: bool = False
    settlement: Settlement | None = None

    @classmethod
    def of(cls, result: CouncilResult, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> Self:
        """A pending entry for the council's decision, due ``horizon_days`` after its date."""
        return cls(
            package=result.package,
            as_of=result.as_of,
            intended_use=result.intended_use,
            horizon_days=horizon_days,
            decision=result.decision,
            models=result.models,
            anonymized=result.anonymized,
        )

    @property
    def due(self) -> date:
        """The last day of the horizon: from then on the outcome can be measured."""
        return self.as_of + timedelta(days=self.horizon_days)

    @property
    def review(self) -> tuple[PackageRef, date, str]:
        """What identifies the review: a later entry with the same one replaces this one."""
        return (self.package, self.as_of, self.intended_use)


class Memory:
    """The council's decisions in a JSON Lines file, created with its directory on first write."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def entries(self) -> list[MemoryEntry]:
        """The latest entry for every review on record, in the order they were last written."""
        if not self.path.exists():
            return []
        latest: dict[tuple[PackageRef, date, str], MemoryEntry] = {}
        with self.path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    entry = MemoryEntry.model_validate_json(line)
                except ValidationError as exc:
                    raise ValueError(f"{self.path}, line {number}: not a memory entry") from exc
                latest.pop(entry.review, None)  # so the replacement moves to the end
                latest[entry.review] = entry
        return list(latest.values())

    def add(self, entry: MemoryEntry) -> None:
        """Store ``entry``, replacing any earlier entry for the same review."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(entry.model_dump_json() + "\n")

    def pending(self) -> list[MemoryEntry]:
        """Decisions whose outcome has not been measured yet."""
        return [entry for entry in self.entries() if entry.settlement is None]

    def recall(self, pkg: PackageRef, as_of: date) -> list[PastDecision]:
        """Settled decisions whose outcome was known by ``as_of``: up to ``SAME_PACKAGE_RECALL``
        of the package's own, then up to ``OTHER_PACKAGE_RECALL`` others, most recent first.

        An outcome is known once its horizon's last day has passed, the same day granularity
        as ``timeline.known_by``.
        """
        known = [
            (entry, settled)
            for entry in self.entries()
            if (settled := entry.settlement) is not None and settled.outcome.end <= as_of
        ]
        known.sort(key=lambda pair: pair[1].outcome.end, reverse=True)
        own = [pair for pair in known if pair[0].package == pkg][:SAME_PACKAGE_RECALL]
        others = [pair for pair in known if pair[0].package != pkg][:OTHER_PACKAGE_RECALL]
        return [_past_decision(entry, settled) for entry, settled in own + others]


async def reflect(
    entry: MemoryEntry, outcome: Outcome, *, settings: Settings | None = None
) -> MemoryEntry:
    """``entry`` settled against ``outcome``: scored, with a lesson from the reflector.

    The settled entry is returned, not stored; pass it to ``Memory.add``.
    """
    settings = settings or Settings()
    graded = score(entry.decision.rating, adverse=outcome.adverse)
    result = await reflector.run(
        reflection_prompt(entry.decision, outcome, intended_use=entry.intended_use, score=graded),
        model=settings.quick_model,
        model_settings=ModelSettings(thinking=settings.quick_effort),
    )
    settlement = Settlement(outcome=outcome, score=graded, lesson=result.output)
    return entry.model_copy(update={"settlement": settlement})


async def settle(
    memory: Memory, *, settings: Settings | None = None, evidence: Evidence | None = None
) -> list[MemoryEntry]:
    """Measure, reflect on and store every pending decision whose horizon has ended.

    Each is stored as soon as it is settled, so an interrupted pass keeps what it finished.
    """
    due = [entry for entry in memory.pending() if entry.due <= today()]
    if not due:
        return []
    settings = settings or Settings()
    settled: list[MemoryEntry] = []
    async with AsyncExitStack() as stack:
        if evidence is None:
            evidence = await stack.enter_async_context(open_evidence(settings))
        for entry in due:
            outcome = await measure_outcome(
                entry.package,
                as_of=entry.as_of,
                horizon_days=entry.horizon_days,
                evidence=evidence,
            )
            done = await reflect(entry, outcome, settings=settings)
            memory.add(done)
            settled.append(done)
    return settled


def _past_decision(entry: MemoryEntry, settled: Settlement) -> PastDecision:
    return PastDecision(
        package=str(entry.package),
        analysis_date=entry.as_of,
        intended_use=entry.intended_use,
        rating=entry.decision.rating,
        confidence=entry.decision.confidence,
        deciding_factors=entry.decision.deciding_factors,
        outcome_known_on=settled.outcome.end,
        adverse_outcome=settled.outcome.adverse,
        what_happened=settled.outcome.findings,
        score=settled.score,
        lesson=settled.lesson,
    )
