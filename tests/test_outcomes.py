from datetime import date

import pytest

from depcouncil import Rating, Score, Settings, backtest, measure_outcome, open_evidence
from depcouncil.outcomes import score
from depcouncil.timeline import today
from tests.fakes import FakeSources

pytestmark = pytest.mark.anyio

AS_OF = date(2022, 1, 1)


@pytest.mark.parametrize(
    ("rating", "adverse", "expected"),
    [
        (Rating.ADOPT, False, Score.HIT),
        (Rating.ADOPT, True, Score.MISS),
        (Rating.ADOPT_WITH_CONDITIONS, False, Score.HIT),
        (Rating.ADOPT_WITH_CONDITIONS, True, Score.PARTIAL),
        (Rating.AVOID, True, Score.HIT),
        (Rating.AVOID, False, Score.MISS),
    ],
)
def test_score(rating: Rating, adverse: bool, expected: Score) -> None:
    assert score(rating, adverse=adverse) is expected


async def test_a_slow_fix_for_a_critical_advisory_is_adverse(sources: FakeSources) -> None:
    async with open_evidence(Settings(), transport=sources.transport()) as evidence:
        outcome = await measure_outcome("pypi:widget", as_of=AS_OF, evidence=evidence)

    assert outcome.end == date(2023, 6, 25)
    assert (outcome.releases, outcome.commits_last_180_days, outcome.dormant) == (1, 2, False)
    assert outcome.slow_or_unfixed_severe == ["GHSA-bbbb-bbbb-bbbb"]  # fixed after 245 days
    assert outcome.adverse


async def test_no_release_and_no_recent_commits_is_dormant(sources: FakeSources) -> None:
    sources.versions.pop("3.0.0")
    sources.vulns = []
    sources.commits = [(stamp, who) for stamp, who in sources.commits if stamp < "2022-06"]
    async with open_evidence(Settings(), transport=sources.transport()) as evidence:
        outcome = await measure_outcome("pypi:widget", as_of=AS_OF, evidence=evidence)

    assert outcome.dormant and outcome.adverse
    assert outcome.slow_or_unfixed_severe == []


async def test_a_horizon_that_has_not_ended_is_refused(sources: FakeSources) -> None:
    async with open_evidence(Settings(), transport=sources.transport()) as evidence:
        with pytest.raises(ValueError, match="hasn't happened yet"):
            await measure_outcome("pypi:widget", as_of=today(), evidence=evidence)


async def test_backtest_grades_the_council(sources: FakeSources) -> None:
    settings = Settings(quick_model="test", deep_model="test")
    async with open_evidence(settings, transport=sources.transport()) as evidence:
        result = await backtest("pypi:widget", as_of=AS_OF, settings=settings, evidence=evidence)

    assert result.outcome.adverse
    assert result.score is score(result.council.decision.rating, adverse=True)
    assert result.council.anonymized  # backtests are anonymized unless asked otherwise
