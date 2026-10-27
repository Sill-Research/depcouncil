"""The dependencies every council agent runs with, and the instructions they all share."""

from dataclasses import dataclass
from datetime import date

from pydantic_ai import RunContext

from depcouncil.evidence import Evidence
from depcouncil.schemas import PackageRef


@dataclass(frozen=True)
class CouncilDeps:
    """What a run is about. Tools read the package and date from here, never from model input."""

    package: PackageRef
    as_of: date
    intended_use: str
    evidence: Evidence
    max_tool_rounds: int = 4


def describe_review(ctx: RunContext[CouncilDeps]) -> str:
    """Instructions naming the package, the analysis date and the intended use."""
    deps = ctx.deps
    return (
        f"Package under review: {deps.package}\n"
        f"Analysis date: {deps.as_of.isoformat()}. Judge the package as it stood then, and do not "
        "use anything you know about it from after that date.\n"
        f"Intended use: {deps.intended_use or 'not stated; assume a long-lived production service'}"
    )
