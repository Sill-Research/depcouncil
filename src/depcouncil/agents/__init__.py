"""The council's agents. Each is defined once at import time with no model: the council passes
the configured model per run, which is also what lets tests swap in Pydantic AI's test models."""

from depcouncil.agents.analysts import ANALYSTS
from depcouncil.agents.debate import advocate, chair, chair_prompt, debate_prompt, skeptic
from depcouncil.agents.deps import CouncilDeps

__all__ = [
    "ANALYSTS",
    "CouncilDeps",
    "advocate",
    "chair",
    "chair_prompt",
    "debate_prompt",
    "skeptic",
]
