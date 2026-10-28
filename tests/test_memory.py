"""The council's memory: decisions stored, settled, and recalled once their outcome was known."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from depcouncil import (
    Decision,
    Evidence,
    Memory,
    MemoryEntry,
    Outcome,
    PackageRef,
    Rating,
    Score,
    Settings,
    backtest,
    reflect,
    run_council,
    settle,
)
from depcouncil.agents import chair, reflector
from depcouncil.memory import OTHER_PACKAGE_RECALL, SAME_PACKAGE_RECALL, Settlement
from depcouncil.timeline import today

pytestmark = pytest.mark.anyio

OFFLINE = Settings(quick_model="test", deep_model="test")
WIDGET = PackageRef.parse("pypi:widget")
ADOPT = Decision(rating=Rating.ADOPT, confidence="high", rationale="r", deciding_factors=["f"])


def entry(
    spec: str, as_of: date, *, horizon_days: int = 540, lesson: str | None = None
) -> MemoryEntry:
    """A decision to adopt, settled as an adverse outcome when given a ``lesson``."""
    pkg = PackageRef.parse(spec)
    pending = MemoryEntry(
        package=pkg,
        as_of=as_of,
        intended_use="",
        horizon_days=horizon_days,
        decision=ADOPT,
        models={"quick": "test", "deep": "test"},
    )
    if lesson is None:
        return pending
    outcome = Outcome(
        package=pkg,
        start=as_of,
        end=pending.due,
        releases=0,
        commits_last_180_days=0,
        dormant=True,
        advisories=0,
        severe_advisories=0,
        slow_or_unfixed_severe=[],
        adverse=True,
        findings=["dormant"],
    )
    settlement = Settlement(outcome=outcome, score=Score.MISS, lesson=lesson)
    return pending.model_copy(update={"settlement": settlement})


def user_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "memory" / "decisions.jsonl")


def test_recall_sees_only_outcomes_known_by_the_date(memory: Memory) -> None:
    known = entry("pypi:widget", date(2019, 1, 1), lesson="known")  # settled 2020-06-24
    memory.add(known)
    memory.add(entry("pypi:widget", date(2021, 1, 1), lesson="not yet"))  # settled 2022-06-25
    memory.add(entry("pypi:widget", date(2018, 1, 1)))  # never settled

    assert [p.lesson for p in memory.recall(WIDGET, date(2022, 1, 1))] == ["known"]
    assert [p.lesson for p in memory.recall(WIDGET, known.due)] == ["known"]
    assert memory.recall(WIDGET, known.due - timedelta(days=1)) == []


def test_recall_puts_the_package_first_and_keeps_the_most_recent(memory: Memory) -> None:
    for year in range(2010, 2018):
        memory.add(entry("pypi:widget", date(year, 1, 1), lesson=f"widget {year}"))
        memory.add(entry("npm:gadget", date(year, 1, 1), lesson=f"gadget {year}"))

    recalled = memory.recall(WIDGET, date(2022, 1, 1))

    own = [f"widget {year}" for year in range(2017, 2017 - SAME_PACKAGE_RECALL, -1)]
    others = [f"gadget {year}" for year in range(2017, 2017 - OTHER_PACKAGE_RECALL, -1)]
    assert [p.lesson for p in recalled] == own + others
    assert recalled[0].package == "pypi:widget" and recalled[0].score is Score.MISS


def test_a_later_entry_replaces_the_earlier_one_for_the_same_review(memory: Memory) -> None:
    memory.add(entry("pypi:widget", date(2019, 1, 1)))
    memory.add(entry("npm:gadget", date(2019, 1, 1)))
    memory.add(entry("pypi:widget", date(2019, 1, 1), lesson="settled"))

    entries = Memory(memory.path).entries()

    assert [(str(e.package), e.settlement is not None) for e in entries] == [
        ("npm:gadget", False),
        ("pypi:widget", True),
    ]
    assert [str(e.package) for e in memory.pending()] == ["npm:gadget"]


def test_a_corrupt_line_names_the_file_and_the_line(memory: Memory) -> None:
    memory.add(entry("pypi:widget", date(2019, 1, 1)))
    with memory.path.open("a") as file:
        file.write('{"package": \n')

    with pytest.raises(ValueError, match="line 2"):
        memory.entries()


async def test_settle_measures_only_decisions_whose_horizon_has_ended(
    memory: Memory, evidence: Evidence
) -> None:
    memory.add(entry("pypi:widget", date(2022, 1, 1)))  # due 2023-06-25
    memory.add(entry("pypi:widget", today() - timedelta(days=10)))

    (settled,) = await settle(memory, settings=OFFLINE, evidence=evidence)

    assert settled.as_of == date(2022, 1, 1) and settled.settlement
    assert settled.settlement.outcome.adverse  # the slow fix for the 2022 critical advisory
    assert settled.settlement.score is Score.MISS
    assert [e.as_of for e in memory.pending()] == [today() - timedelta(days=10)]
    assert await settle(memory, settings=OFFLINE, evidence=evidence) == []


async def test_the_reflector_sees_the_decision_and_what_happened() -> None:
    prompts: list[str] = []

    def lesson(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.extend(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Dormancy outweighed a clean record.")])

    settled = entry("pypi:widget", date(2019, 1, 1), lesson="")
    assert settled.settlement
    with reflector.override(model=FunctionModel(lesson)):
        reflected = await reflect(
            entry("pypi:widget", date(2019, 1, 1)), settled.settlement.outcome
        )

    assert reflected.settlement and reflected.settlement.score is Score.MISS
    assert reflected.settlement.lesson == "Dormancy outweighed a clean record."
    (prompt,) = prompts
    assert "<score>miss</score>" in prompt and "<rating>adopt</rating>" in prompt
    assert "<findings>" in prompt and "dormant" in prompt


async def test_the_chair_learns_from_an_earlier_backtest(
    memory: Memory, evidence: Evidence
) -> None:
    earlier = await backtest(
        WIDGET, as_of=date(2021, 1, 1), horizon_days=365, settings=OFFLINE, evidence=evidence
    )
    memory.add(
        await reflect(
            MemoryEntry.of(earlier.council, horizon_days=365), earlier.outcome, settings=OFFLINE
        )
    )
    prompts: list[str] = []

    def ruling(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.extend(user_prompts(messages))
        decision = {
            "rating": "avoid",
            "confidence": "low",
            "rationale": "r",
            "deciding_factors": [],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, decision)])

    assert memory.recall(WIDGET, date(2021, 12, 31)) == []  # its outcome is known 2022-01-01
    past = memory.recall(WIDGET, date(2022, 1, 1))
    with chair.override(model=FunctionModel(ruling)):
        result = await run_council(
            WIDGET, as_of=date(2022, 1, 1), settings=OFFLINE, evidence=evidence, past_decisions=past
        )

    assert [p.analysis_date for p in result.past_decisions] == [date(2021, 1, 1)]
    (prompt,) = prompts
    assert "<past_decisions>" in prompt and "<analysis_date>2021-01-01</analysis_date>" in prompt
