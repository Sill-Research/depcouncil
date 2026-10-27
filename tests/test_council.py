"""The whole council, on TestModel and FunctionModel against the fake sources."""

from datetime import date, timedelta

import pytest
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from depcouncil import Dimension, Evidence, Rating, Settings, run_council
from depcouncil.agents import chair
from depcouncil.council import AnalystReported, CouncilEvent, DecisionMade, TurnTaken
from depcouncil.timeline import today

pytestmark = pytest.mark.anyio

OFFLINE = Settings(quick_model="test", deep_model="test")


async def test_council_runs_end_to_end(evidence: Evidence) -> None:
    events: list[CouncilEvent] = []
    settings = OFFLINE.model_copy(update={"debate_rounds": 2})

    result = await run_council(
        "pypi:widget",
        as_of=date(2022, 1, 1),
        settings=settings,
        evidence=evidence,
        on_event=events.append,
    )

    assert set(result.reports) == set(Dimension)
    assert [turn.speaker for turn in result.debate] == ["advocate", "skeptic"] * 2
    assert sum(isinstance(e, AnalystReported) for e in events) == 3
    assert sum(isinstance(e, TurnTaken) for e in events) == 4
    assert isinstance(events[-1], DecisionMade)
    assert result.usage.tool_calls > 0
    assert result.models == {"quick": "test", "deep": "test"}


async def test_chair_rules_on_the_reports_and_the_debate(evidence: Evidence) -> None:
    prompts: list[str] = []

    def ruling(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.extend(
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        decision = {
            "rating": "avoid",
            "confidence": "medium",
            "rationale": "r",
            "deciding_factors": ["f"],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, decision)])

    with chair.override(model=FunctionModel(ruling)):
        result = await run_council(
            "pypi:widget", as_of=date(2022, 1, 1), settings=OFFLINE, evidence=evidence
        )

    assert result.decision.rating is Rating.AVOID
    (prompt,) = prompts
    assert "<analyst_reports>" in prompt and "<supply_chain>" in prompt
    assert "<debate>" in prompt and "<speaker>skeptic</speaker>" in prompt


async def test_council_refuses_a_future_date(evidence: Evidence) -> None:
    with pytest.raises(ValueError, match="future"):
        await run_council(
            "pypi:widget", as_of=today() + timedelta(days=1), settings=OFFLINE, evidence=evidence
        )
