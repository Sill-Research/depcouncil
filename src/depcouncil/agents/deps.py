"""The dependencies every council agent runs with, and the instructions they all share."""

from dataclasses import dataclass
from datetime import date

from pydantic_ai import RunContext

from depcouncil.anonymity import ALIAS
from depcouncil.evidence import Evidence
from depcouncil.schemas import PackageRef


@dataclass(frozen=True)
class CouncilDeps:
    """What a run is about. Tools read the package and date from here, never from model input.

    ``identifiers`` are the spellings of the package's name that an anonymized review keeps from
    the debaters and the chair; they are empty when the review is not anonymized.
    """

    package: PackageRef
    as_of: date
    intended_use: str
    evidence: Evidence
    max_tool_rounds: int = 4
    identifiers: tuple[str, ...] = ()

    @property
    def anonymous(self) -> bool:
        return bool(self.identifiers)


def describe_review(ctx: RunContext[CouncilDeps]) -> str:
    """Instructions naming the package, the analysis date and the intended use, for the debaters
    and the chair. An anonymized review names only the package's ecosystem."""
    deps = ctx.deps
    if not deps.anonymous:
        return _describe(deps, str(deps.package))
    ecosystem = deps.package.ecosystem.osv_ecosystem
    return _describe(
        deps,
        f'an anonymized {ecosystem} package, called "{ALIAS}". Its name is withheld so that it is '
        "judged on the evidence alone; do not try to identify it.",
    )


def describe_analyst_review(ctx: RunContext[CouncilDeps]) -> str:
    """``describe_review`` for the analysts, who always know the package: their tool results name
    it anyway. In an anonymized review, their reports must not."""
    deps = ctx.deps
    review = _describe(deps, str(deps.package))
    if not deps.anonymous:
        return review
    return (
        f"{review}\nThis review is anonymized: the council that reads your report is not told the "
        f'package\'s name. Refer to it only as "{ALIAS}", and do not write any of these anywhere '
        f"in your report: {', '.join(deps.identifiers)}."
    )


def _describe(deps: CouncilDeps, package: str) -> str:
    return (
        f"Package under review: {package}\n"
        f"Analysis date: {deps.as_of.isoformat()}. Judge the package as it stood then, and do not "
        "use anything you know about it from after that date.\n"
        f"Intended use: {deps.intended_use or 'not stated; assume a long-lived production service'}"
    )
