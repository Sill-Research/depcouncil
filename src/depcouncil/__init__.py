"""A council of AI agents that decides whether to adopt an open-source dependency."""

from importlib.metadata import version

from depcouncil.council import CouncilResult, run_council
from depcouncil.evidence import Evidence, open_evidence
from depcouncil.outcomes import BacktestResult, Outcome, Score, backtest, measure_outcome
from depcouncil.schemas import AnalystReport, Decision, Dimension, Ecosystem, PackageRef, Rating
from depcouncil.settings import Settings

__version__ = version("depcouncil")

__all__ = [
    "AnalystReport",
    "BacktestResult",
    "CouncilResult",
    "Decision",
    "Dimension",
    "Ecosystem",
    "Evidence",
    "Outcome",
    "PackageRef",
    "Rating",
    "Score",
    "Settings",
    "backtest",
    "measure_outcome",
    "open_evidence",
    "run_council",
]
