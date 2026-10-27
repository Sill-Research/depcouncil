from datetime import UTC, date, datetime, timedelta

import pytest

from depcouncil.schemas import Ecosystem, PackageRef
from depcouncil.timeline import cutoff, is_historical, known_by, today


@pytest.mark.parametrize(
    ("spec", "ecosystem", "name"),
    [
        ("pypi:Typing_Extensions", Ecosystem.PYPI, "typing-extensions"),
        ("npm:@types/node", Ecosystem.NPM, "@types/node"),
        ("maven:com.google.guava:guava", Ecosystem.MAVEN, "com.google.guava:guava"),
        ("Cargo:serde", Ecosystem.CARGO, "serde"),
    ],
)
def test_parse_package(spec: str, ecosystem: Ecosystem, name: str) -> None:
    ref = PackageRef.parse(spec)
    assert (ref.ecosystem, ref.name) == (ecosystem, name)


@pytest.mark.parametrize("spec", ["requests", "pypi:", "hex:phoenix"])
def test_parse_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        PackageRef.parse(spec)


def test_known_by_includes_the_whole_analysis_day_and_nothing_after() -> None:
    as_of = date(2022, 1, 1)
    assert known_by(datetime(2022, 1, 1, 23, 59, 59, tzinfo=UTC), as_of)
    assert not known_by(datetime(2022, 1, 2, tzinfo=UTC), as_of)
    assert not known_by(None, as_of)
    assert cutoff(as_of) == datetime(2022, 1, 2, tzinfo=UTC)


def test_only_past_dates_are_historical() -> None:
    assert is_historical(today() - timedelta(days=1))
    assert not is_historical(today())
