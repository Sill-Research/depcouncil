"""The three analysts. Each investigates one dimension of the package with its own data tools
and files a structured ``AnalystReport``; the council runs them concurrently."""

from pydantic_ai import Agent
from pydantic_ai.toolsets import AbstractToolset

from depcouncil.agents.deps import CouncilDeps, describe_review
from depcouncil.agents.tools import MAINTENANCE_TOOLS, SECURITY_TOOLS, SUPPLY_CHAIN_TOOLS
from depcouncil.schemas import AnalystReport, Dimension

_BRIEF = """\
You are an analyst briefing a council that decides whether a team should adopt an open-source \
package. Gather evidence with your tools, then file your report.

- Base every finding on a figure or record your tools returned, and name it.
- When a tool reports data as withheld or unavailable, list it under data gaps instead of \
filling the gap from memory.
- Weigh what you find against the intended use: a risk that matters for a long-lived \
production service may not matter for a one-off script."""

_FOCUS = {
    Dimension.MAINTENANCE: """\
Your dimension is maintenance: is the package actively and sustainably maintained? Weigh release \
cadence and recency, commit volume and recency, how concentrated the work is among authors (a \
bus-factor risk), and, when available, community size and any archive or deprecation status.""",
    Dimension.SECURITY: """\
Your dimension is security: what does the package's vulnerability record say about the risk of \
adopting it? Weigh how many advisories there were and how severe, how quickly fixes shipped, \
anything unfixed or affecting the latest release, and, when available, the OpenSSF Scorecard. \
Read the details of the advisories that matter most.""",
    Dimension.SUPPLY_CHAIN: """\
Your dimension is supply chain: what does adopting the package bring in with it? Weigh the \
license and its fit with the intended use, the size of the direct and transitive dependency \
footprint, and release integrity (SLSA provenance and attestations).""",
}

_TOOLSETS: dict[Dimension, AbstractToolset[CouncilDeps]] = {
    Dimension.MAINTENANCE: MAINTENANCE_TOOLS,
    Dimension.SECURITY: SECURITY_TOOLS,
    Dimension.SUPPLY_CHAIN: SUPPLY_CHAIN_TOOLS,
}


def _analyst(dimension: Dimension) -> Agent[CouncilDeps, AnalystReport]:
    return Agent(
        name=f"{dimension}_analyst",
        deps_type=CouncilDeps,
        output_type=AnalystReport,
        instructions=[_BRIEF, _FOCUS[dimension], describe_review],
        toolsets=[_TOOLSETS[dimension]],
        retries=2,
    )


ANALYSTS: dict[Dimension, Agent[CouncilDeps, AnalystReport]] = {
    dimension: _analyst(dimension) for dimension in Dimension
}
