"""The advocate and the skeptic, who argue the analysts' findings in alternating turns, and the
chair, who weighs both sides and makes the call.

None of them has tools: they work only from the reports, so the debate cannot drift onto
evidence the analysts never checked against the analysis date.
"""

from collections.abc import Mapping, Sequence

from pydantic_ai import Agent, format_as_xml

from depcouncil.agents.deps import CouncilDeps, describe_review
from depcouncil.schemas import AnalystReport, DebateTurn, Decision, Dimension, PastDecision

_GROUND_RULES = """\
Use only facts from the analyst reports and the debate so far; do not add what you know about \
the package from elsewhere. Keep each turn under 250 words."""

advocate = Agent(
    name="advocate",
    deps_type=CouncilDeps,
    output_type=str,
    instructions=[
        """\
You are the advocate on a council deciding whether a team should adopt an open-source package. \
Argue for adopting it. Build the strongest honest case from the analyst reports: cite their \
findings, propose concrete safeguards for the risks they raise, and answer the skeptic's latest \
points directly.""",
        _GROUND_RULES,
        describe_review,
    ],
)

skeptic = Agent(
    name="skeptic",
    deps_type=CouncilDeps,
    output_type=str,
    instructions=[
        """\
You are the skeptic on a council deciding whether a team should adopt an open-source package. \
Argue against adopting it. Surface the risks the advocate underplays, explain how they would \
hurt the intended use, and test whether the advocate's safeguards really contain them.""",
        _GROUND_RULES,
        describe_review,
    ],
)

chair = Agent(
    name="chair",
    deps_type=CouncilDeps,
    output_type=Decision,
    instructions=[
        """\
You chair a council deciding whether a team should adopt an open-source package. Weigh the \
analyst reports and the debate, then make the call. Judge the arguments on the evidence behind \
them, not on how forcefully they were made, and let data gaps lower your confidence rather \
than your rating.""",
        """\
Past decisions, when listed, are the council's earlier calls and what happened after them. Let \
their lessons shape how you weigh similar evidence, but rule on this package's own record.""",
        describe_review,
    ],
    retries=2,
)


def debate_prompt(
    reports: Mapping[Dimension, AnalystReport], transcript: Sequence[DebateTurn], *, speaker: str
) -> str:
    opponent = "skeptic" if speaker == "advocate" else "advocate"
    task = (
        "Open the debate."
        if not transcript
        else f"Answer the {opponent}'s latest turn and press your case."
    )
    return _briefing(reports, transcript) + f"\n\n{task}"


def chair_prompt(
    reports: Mapping[Dimension, AnalystReport],
    transcript: Sequence[DebateTurn],
    past_decisions: Sequence[PastDecision] = (),
) -> str:
    sections = [_briefing(reports, transcript)]
    if past_decisions:
        sections.append(format_as_xml(list(past_decisions), root_tag="past_decisions"))
    return "\n\n".join(sections) + "\n\nMake the council's decision."


def _briefing(reports: Mapping[Dimension, AnalystReport], transcript: Sequence[DebateTurn]) -> str:
    sections = [format_as_xml({str(d): r for d, r in reports.items()}, root_tag="analyst_reports")]
    if transcript:
        sections.append(format_as_xml(list(transcript), root_tag="debate", item_tag="turn"))
    return "\n\n".join(sections)
