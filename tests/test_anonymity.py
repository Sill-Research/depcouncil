"""Anonymized reviews: the debaters and the chair never learn the package's name."""

from dataclasses import replace
from datetime import date

import pytest
from pydantic_ai import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from depcouncil import Dimension, Evidence, PackageRef, PastDecision, Rating, Score, Settings
from depcouncil.agents import ANALYSTS, CouncilDeps, advocate, chair, skeptic
from depcouncil.anonymity import identifiers, named_in, redact
from depcouncil.council import run_council

pytestmark = pytest.mark.anyio

OFFLINE = Settings(quick_model="test", deep_model="test")
WIDGET = PackageRef.parse("pypi:widget")
NAMES = identifiers(WIDGET, "acme/widget")


def past(package: str, lesson: str) -> PastDecision:
    return PastDecision(
        package=package,
        analysis_date=date(2019, 1, 1),
        intended_use="",
        rating=Rating.ADOPT,
        confidence="high",
        deciding_factors=[],
        outcome_known_on=date(2020, 6, 24),
        adverse_outcome=True,
        what_happened=["dormant"],
        score=Score.MISS,
        lesson=lesson,
    )


def report_call(info: AgentInfo, summary: str) -> ModelResponse:
    report = {"summary": summary, "strengths": [], "concerns": [], "risk": "low"}
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])


@pytest.fixture
def anonymous(evidence: Evidence) -> CouncilDeps:
    return CouncilDeps(WIDGET, date(2022, 1, 1), "", evidence, identifiers=NAMES)


def test_identifiers_cover_the_name_and_the_repository() -> None:
    assert NAMES == ("pypi:widget", "widget", "acme/widget")
    guava = PackageRef.parse("maven:com.google.guava:guava")
    assert identifiers(guava) == ("maven:com.google.guava:guava", "com.google.guava:guava", "guava")


def test_names_match_as_whole_words_in_any_case() -> None:
    text = "Widget's parser, a fork at github.com/acme/widget, unlike widget-extra or widgets."
    assert named_in(text, NAMES) == ["Widget", "acme/widget"]
    assert named_in(
        "typing_extensions 4.0", identifiers(PackageRef.parse("pypi:typing-extensions"))
    )
    assert named_in("nothing to see", ()) == []


def test_redact_replaces_every_spelling_with_the_alias() -> None:
    assert (
        redact("Use Widget (pypi:widget, acme/widget), not widget-extra.", NAMES)
        == "Use the package (the package, the package), not widget-extra."
    )


async def test_an_analyst_that_names_the_package_is_asked_to_rewrite(
    anonymous: CouncilDeps,
) -> None:
    retries: list[str] = []
    instructions: list[str | None] = []

    def analyst(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        instructions.append(info.instructions)
        # Pydantic AI answers the output tool call with the validator's `ModelRetry` message.
        retries.extend(
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart | RetryPromptPart)
        )
        return report_call(info, "Widget is healthy." if not retries else "The package is healthy.")

    result = await ANALYSTS[Dimension.MAINTENANCE].run(
        "Report.", deps=anonymous, model=FunctionModel(analyst)
    )

    assert result.output.summary == "The package is healthy."
    (retry,) = retries
    assert "anonymized" in retry and "Widget" in retry
    assert (
        "do not write any of these anywhere in your report: pypi:widget, widget, acme/widget"
        in (instructions[0] or "")
    )


async def test_a_report_that_keeps_naming_the_package_is_redacted_at_last(
    anonymous: CouncilDeps,
) -> None:
    def stubborn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return report_call(info, "Widget, from acme/widget, is healthy.")

    result = await ANALYSTS[Dimension.MAINTENANCE].run(
        "Report.", deps=anonymous, model=FunctionModel(stubborn)
    )

    assert result.output.summary == "the package, from the package, is healthy."
    assert result.usage.requests == 3  # the report and two rewrites


async def test_a_named_review_leaves_reports_alone(anonymous: CouncilDeps) -> None:
    def analyst(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return report_call(info, "Widget is healthy.")

    result = await ANALYSTS[Dimension.MAINTENANCE].run(
        "Report.", deps=replace(anonymous, identifiers=()), model=FunctionModel(analyst)
    )

    assert result.output.summary == "Widget is healthy."


@pytest.mark.parametrize("anonymize", [True, False])
async def test_only_an_anonymized_review_hides_the_name_from_the_council(
    evidence: Evidence, anonymize: bool
) -> None:
    seen: dict[str, str] = {}

    def recorder(name: str, respond: ModelResponse) -> FunctionModel:
        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen[name] = f"{info.instructions}\n{messages}"
            return respond

        return FunctionModel(model)

    def ruling(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["chair"] = f"{info.instructions}\n{messages}"
        decision = {
            "rating": "avoid",
            "confidence": "low",
            "rationale": "r",
            "deciding_factors": [],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, decision)])

    turn = ModelResponse(parts=[TextPart("An argument.")])
    with (
        advocate.override(model=recorder("advocate", turn)),
        skeptic.override(model=recorder("skeptic", turn)),
        chair.override(model=FunctionModel(ruling)),
    ):
        result = await run_council(
            WIDGET,
            as_of=date(2022, 1, 1),
            settings=OFFLINE,
            evidence=evidence,
            past_decisions=[past("pypi:widget", "widget went dormant"), past("npm:gadget", "ok")],
            anonymize=anonymize,
        )

    assert result.anonymized is anonymize
    assert set(seen) == {"advocate", "skeptic", "chair"}
    for name, context in seen.items():
        assert ("widget" in context.lower()) is not anonymize, name
    expected = "an anonymized PyPI package" if anonymize else "Package under review: pypi:widget"
    assert expected in seen["chair"]
    if anonymize:
        assert [(p.package, p.lesson) for p in result.past_decisions] == [
            ("the package", "the package went dormant"),
            ("npm:gadget", "ok"),
        ]
