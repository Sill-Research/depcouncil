"""The evidence layer answers as of a date and hides everything after it."""

from datetime import date

import pytest

from depcouncil import Evidence, PackageRef
from depcouncil.evidence import ProjectSnapshot, ScorecardReport, Severity, Withheld
from depcouncil.timeline import today
from tests.fakes import FakeSources

pytestmark = pytest.mark.anyio

WIDGET = PackageRef.parse("pypi:widget")
AS_OF = date(2022, 1, 1)


async def test_releases_stop_at_the_analysis_date(evidence: Evidence) -> None:
    history = await evidence.releases(WIDGET, AS_OF)

    assert history.total_releases == 5
    assert history.latest_release and history.latest_release.version == "2.2.0rc1"
    assert history.latest_release.prerelease
    assert history.latest_stable_release and history.latest_stable_release.version == "2.1.0"
    assert history.days_since_last_release == 61
    assert history.new_major_versions_last_730_days == ["2.0.0"]


async def test_aliases_merge_into_one_advisory_dated_by_its_earliest_record(
    evidence: Evidence,
) -> None:
    record = await evidence.vulnerabilities(WIDGET, AS_OF)
    by_id = {a.id: a for a in record.advisories}

    # The GHSA record was published after the date, but its PYSEC alias was public before it.
    merged = by_id["CVE-2021-0001"]
    assert set(merged.aliases) == {"GHSA-aaaa-aaaa-aaaa", "PYSEC-2021-1"}
    assert merged.disclosed == date(2021, 5, 1)
    assert merged.severity is Severity.HIGH
    assert merged.days_to_fix == 123
    assert merged.summary == "Widget parser overflow"


async def test_fixes_and_advisories_after_the_date_are_hidden(evidence: Evidence) -> None:
    record = await evidence.vulnerabilities(WIDGET, AS_OF)
    by_id = {a.id: a for a in record.advisories}

    assert set(by_id) == {"CVE-2021-0001", "GHSA-cccc-cccc-cccc"}  # not the 2022 CRITICAL one
    unfixed = by_id["GHSA-cccc-cccc-cccc"]  # its fix, 3.0.0, shipped in 2023
    assert unfixed.fix_released is None
    assert unfixed.fixed_versions == []
    assert unfixed.affects_latest_release is True
    assert (record.unfixed, record.affecting_latest_release) == (1, 1)


async def test_withdrawn_advisories_count_only_until_withdrawn(evidence: Evidence) -> None:
    before = await evidence.known_advisories(WIDGET, date(2021, 2, 15))
    after = await evidence.known_advisories(WIDGET, AS_OF)

    assert "GHSA-dddd-dddd-dddd" in {a.id for a in before}
    assert "GHSA-dddd-dddd-dddd" not in {a.id for a in after}


async def test_advisory_lookup_by_alias_and_not_past_the_date(evidence: Evidence) -> None:
    detail = await evidence.advisory(WIDGET, AS_OF, "ghsa-aaaa-aaaa-aaaa")

    assert detail is not None and detail.details.startswith("Parsing a crafted widget")
    assert await evidence.advisory(WIDGET, AS_OF, "GHSA-bbbb-bbbb-bbbb") is None


async def test_commit_activity_asks_github_for_the_window_before_the_date(
    evidence: Evidence, sources: FakeSources
) -> None:
    activity = await evidence.commit_activity(WIDGET, AS_OF, window_days=365)

    assert activity.commits_in_window == 3
    assert activity.last_commit == date(2021, 12, 20)
    assert activity.authors_in_window == 2
    assert activity.top_author_share == 0.67
    commit_queries = [r for r in sources.requests if r.url.path.endswith("/commits")]
    assert {r.url.params["until"] for r in commit_queries} == {"2022-01-02T00:00:00Z"}


async def test_present_day_snapshots_are_withheld_from_historical_runs(evidence: Evidence) -> None:
    assert isinstance(await evidence.project_snapshot(WIDGET, AS_OF), Withheld)
    assert isinstance(await evidence.scorecard(WIDGET, AS_OF), Withheld)

    snapshot = await evidence.project_snapshot(WIDGET, today())
    assert isinstance(snapshot, ProjectSnapshot) and snapshot.stars == 1200
    assert isinstance(await evidence.scorecard(WIDGET, today()), ScorecardReport)


async def test_supply_chain_reads_the_latest_stable_release(evidence: Evidence) -> None:
    metadata = await evidence.version_metadata(WIDGET, AS_OF)
    footprint = await evidence.dependency_footprint(WIDGET, AS_OF)

    assert (metadata.version, metadata.licenses) == ("2.1.0", ["MIT"])
    assert footprint.direct_dependencies == ["attrs", "six"]
    assert (footprint.direct_count, footprint.transitive_count) == (2, 1)
    assert footprint.note  # resolved against today's registry, and says so


async def test_each_raw_record_is_fetched_once(evidence: Evidence, sources: FakeSources) -> None:
    await evidence.vulnerabilities(WIDGET, AS_OF)
    await evidence.vulnerabilities(WIDGET, date(2023, 1, 1))
    await evidence.releases(WIDGET, AS_OF)

    assert sum(r.url.host == "api.osv.dev" for r in sources.requests) == 1
    assert sum(r.url.path.endswith("/packages/widget") for r in sources.requests) == 1
