"""The analysts' tools, driven by ``FunctionModel`` so each test scripts the model's moves."""

from dataclasses import replace
from datetime import date

import pytest
from pydantic import BaseModel
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from depcouncil import Dimension, Evidence, PackageRef, open_evidence
from depcouncil.agents import ANALYSTS, CouncilDeps
from depcouncil.evidence import AdvisoryDetail, ReleaseHistory, Unavailable
from depcouncil.settings import Settings
from tests.fakes import widget

pytestmark = pytest.mark.anyio

REPORT = {"summary": "s", "strengths": [], "concerns": [], "risk": "low"}
DATE_LIKE = {"date", "as_of", "since", "until", "day", "package", "name"}


@pytest.fixture
def deps(evidence: Evidence) -> CouncilDeps:
    return CouncilDeps(PackageRef.parse("pypi:widget"), date(2022, 1, 1), "", evidence)


def report(info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])


def returns(messages: list[ModelMessage]) -> dict[str, object]:
    """Tool results by tool call id."""
    return {
        part.tool_call_id: part.content
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


async def test_no_tool_lets_the_model_choose_the_date_or_package(deps: CouncilDeps) -> None:
    schemas: dict[str, dict[str, object]] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        schemas.update({tool.name: tool.parameters_json_schema for tool in info.function_tools})
        return report(info)

    for analyst in ANALYSTS.values():
        await analyst.run("Report.", deps=deps, model=FunctionModel(model))

    assert len(schemas) == 8
    for name, schema in schemas.items():
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        assert not DATE_LIKE & set(properties), name


async def test_tools_answer_as_of_the_date_in_deps(deps: CouncilDeps) -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("release_history", {}, tool_call_id="r")])
        return report(info)

    result = await ANALYSTS[Dimension.MAINTENANCE].run(
        "Report.", deps=deps, model=FunctionModel(model)
    )

    history = returns(result.all_messages())["r"]
    assert isinstance(history, ReleaseHistory)
    assert history.latest_stable_release and history.latest_stable_release.version == "2.1.0"


async def test_a_future_advisory_id_looks_the_same_as_an_unknown_one(deps: CouncilDeps) -> None:
    calls = {
        "future": "GHSA-bbbb-bbbb-bbbb",
        "unknown": "GHSA-zzzz-zzzz-zzzz",
        "known": "cve-2021-0001",
    }

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("advisory_details", {"advisory_id": advisory}, tool_call_id=key)
                    for key, advisory in calls.items()
                ]
            )
        return report(info)

    result = await ANALYSTS[Dimension.SECURITY].run(
        "Report.", deps=deps, model=FunctionModel(model)
    )

    results = returns(result.all_messages())
    future, unknown = results["future"], results["unknown"]
    assert isinstance(future, Unavailable) and isinstance(unknown, Unavailable)
    assert future.unavailable.replace(calls["future"], "ID") == unknown.unavailable.replace(
        calls["unknown"], "ID"
    )
    assert isinstance(results["known"], AdvisoryDetail)


async def test_advisory_details_is_hidden_when_nothing_was_disclosed(deps: CouncilDeps) -> None:
    offered: set[str] = set()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.update(tool.name for tool in info.function_tools)
        return report(info)

    no_vulns = widget()
    no_vulns.vulns = []
    async with open_evidence(Settings(), transport=no_vulns.transport()) as evidence:
        await ANALYSTS[Dimension.SECURITY].run(
            "Report.", deps=replace(deps, evidence=evidence), model=FunctionModel(model)
        )

    assert offered == {"vulnerability_record", "security_practices"}


async def test_tools_are_withdrawn_after_max_tool_rounds(deps: CouncilDeps) -> None:
    def tool_happy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(tool.name == "release_history" for tool in info.function_tools):
            return ModelResponse(parts=[ToolCallPart("release_history", {})])
        return report(info)

    result = await ANALYSTS[Dimension.MAINTENANCE].run(
        "Report.", deps=replace(deps, max_tool_rounds=2), model=FunctionModel(tool_happy)
    )

    assert result.usage.requests == 3  # two tool rounds, then the report
    assert isinstance(result.output, BaseModel)
