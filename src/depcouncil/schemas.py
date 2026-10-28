"""Typed values shared across the council: the package under review, every agent's output, and
what happened after a decision.

The agent output models double as the schemas the models are asked to fill, so their field
descriptions are written for the model as much as for the reader.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field


class Ecosystem(StrEnum):
    """A package registry, named as on the command line (``pypi:requests``)."""

    PYPI = "pypi"
    NPM = "npm"
    CARGO = "cargo"
    GO = "go"
    MAVEN = "maven"
    NUGET = "nuget"
    RUBYGEMS = "rubygems"

    @property
    def depsdev_system(self) -> str:
        """The registry's name in the deps.dev API."""
        return self.value.upper()

    @property
    def osv_ecosystem(self) -> str:
        """The registry's name in the OSV schema."""
        return _OSV_ECOSYSTEMS[self]


_OSV_ECOSYSTEMS = {
    Ecosystem.PYPI: "PyPI",
    Ecosystem.NPM: "npm",
    Ecosystem.CARGO: "crates.io",
    Ecosystem.GO: "Go",
    Ecosystem.MAVEN: "Maven",
    Ecosystem.NUGET: "NuGet",
    Ecosystem.RUBYGEMS: "RubyGems",
}


class PackageRef(BaseModel):
    """A package in a registry, e.g. ``pypi:requests`` or ``maven:com.google.guava:guava``."""

    model_config = ConfigDict(frozen=True)

    ecosystem: Ecosystem
    name: str = Field(min_length=1)

    @classmethod
    def parse(cls, spec: str) -> Self:
        """Parse ``<ecosystem>:<name>``; the name may itself contain colons (Maven)."""
        ecosystem, sep, name = spec.strip().partition(":")
        name = name.strip()
        if not sep or not name:
            raise ValueError(f"expected <ecosystem>:<name>, e.g. pypi:requests; got {spec!r}")
        try:
            registry = Ecosystem(ecosystem.lower())
        except ValueError:
            choices = ", ".join(Ecosystem)
            raise ValueError(f"unknown ecosystem {ecosystem!r}; choose from {choices}") from None
        return cls(ecosystem=registry, name=normalize_name(registry, name))

    def __str__(self) -> str:
        return f"{self.ecosystem}:{self.name}"


def normalize_name(ecosystem: Ecosystem, name: str) -> str:
    """A registry's canonical spelling of a package name (PEP 503 for PyPI)."""
    if ecosystem is Ecosystem.PYPI:
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


class Dimension(StrEnum):
    """An aspect of a package that one analyst reviews."""

    MAINTENANCE = "maintenance"
    SECURITY = "security"
    SUPPLY_CHAIN = "supply_chain"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class RiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class AnalystReport(BaseModel):
    """An analyst's findings on one dimension of the package."""

    summary: str = Field(
        description="Two to four sentences giving the overall read on this dimension."
    )
    strengths: list[str] = Field(
        description="Findings that support adopting, each naming the figure or record it rests on."
    )
    concerns: list[str] = Field(
        description="Findings that argue against adopting, each naming the figure or record "
        "it rests on."
    )
    risk: RiskLevel = Field(description="How much risk this dimension adds for the intended use.")
    data_gaps: list[str] = Field(
        default_factory=list,
        description="Data that was missing, unavailable or withheld for the analysis date, "
        "and that could change the conclusion.",
    )


class DebateTurn(BaseModel):
    speaker: Literal["advocate", "skeptic"]
    argument: str


class Rating(StrEnum):
    ADOPT = "adopt"
    ADOPT_WITH_CONDITIONS = "adopt_with_conditions"
    AVOID = "avoid"


class Decision(BaseModel):
    """The council's final call on adopting the package."""

    rating: Rating = Field(
        description="adopt: no material unmitigated risk for the intended use. "
        "adopt_with_conditions: real risks that concrete safeguards contain. "
        "avoid: risks that safeguards cannot contain, or strong evidence of neglect or insecurity."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="Low when data gaps leave material questions open."
    )
    rationale: str = Field(
        description="One paragraph weighing the strongest arguments from both sides of the debate."
    )
    deciding_factors: list[str] = Field(
        description="The two to four findings that decided the rating."
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="Safeguards required if the package is adopted, such as version pinning, "
        "monitoring or a fallback plan. Empty when the rating is avoid.",
    )
    revisit_if: list[str] = Field(
        default_factory=list, description="Future signals that should trigger a fresh review."
    )


class Outcome(BaseModel):
    """What happened to a package over the horizon after an analysis date."""

    package: PackageRef
    start: date
    end: date
    releases: int
    commits_last_180_days: int | None
    dormant: bool
    advisories: int
    severe_advisories: int
    slow_or_unfixed_severe: list[str]
    adverse: bool
    findings: list[str]


class Score(StrEnum):
    HIT = "hit"
    PARTIAL = "partial"
    MISS = "miss"


class PastDecision(BaseModel):
    """A decision the council made earlier, what happened after it, and the lesson drawn from it.

    The chair of a later review sees only those whose outcome was known by its analysis date.
    """

    package: str
    analysis_date: date
    intended_use: str
    rating: Rating
    confidence: Literal["low", "medium", "high"]
    deciding_factors: list[str]
    outcome_known_on: date
    adverse_outcome: bool
    what_happened: list[str]
    score: Score
    lesson: str
