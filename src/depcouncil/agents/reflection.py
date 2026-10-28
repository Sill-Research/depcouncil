"""The reflector, who looks back on a decision once its outcome is known and draws a lesson.

It is not on the council: it runs after the fact, and its lessons reach the chairs of later
reviews through the council's memory (``depcouncil.memory``). It is never told the package's
name, so that its lessons rest on the decision and the outcome rather than on what the model
remembers of the package after the outcome's horizon.
"""

from pydantic_ai import Agent, format_as_xml

from depcouncil.schemas import Decision, Outcome, Score

reflector = Agent(
    name="reflector",
    output_type=str,
    instructions="""\
You look back on a decision your council made about adopting an open-source package, now that \
its outcome is known. The outcome was adverse if the package went dormant or left a high or \
critical advisory unfixed for more than 90 days. The call was graded a hit if the rating fit \
the outcome, a partial if the package was adopted with conditions and the outcome was adverse, \
and a miss otherwise.

Write two to four sentences of plain prose, with no lists or headings, covering in order:
1. Whether the outcome bore the rating out, citing the figures that show it.
2. Which deciding factors held up and which misled.
3. One concrete lesson for the next review of a similar package.

Use only the decision and the outcome given; do not add what you know about the package from \
elsewhere. The lesson is stored and shown to the chairs of later reviews, so make every word \
count.""",
)


def reflection_prompt(
    decision: Decision, outcome: Outcome, *, intended_use: str, score: Score
) -> str:
    review = {
        "package": f"a {outcome.package.ecosystem.osv_ecosystem} package, name withheld",
        "analysis_date": outcome.start,
        "intended_use": intended_use or "not stated",
        "outcome_known_on": outcome.end,
        "score": score,
    }
    sections = [
        format_as_xml(review, root_tag="review"),
        format_as_xml(decision, root_tag="decision"),
        format_as_xml(outcome.model_dump(exclude={"package"}), root_tag="outcome"),
    ]
    return "\n\n".join(sections) + "\n\nWrite the lesson."
