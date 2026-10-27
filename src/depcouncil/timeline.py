"""Point-in-time rules: what was knowable on the analysis date.

Every dated fact the council sees passes through these helpers, so a review dated in the past
never sees a release, advisory or commit from after that date.
"""

from datetime import UTC, date, datetime, time, timedelta


def today() -> date:
    """Today's date in UTC, the timezone every source stamps its records in."""
    return datetime.now(UTC).date()


def cutoff(as_of: date) -> datetime:
    """The first instant after the analysis date: anything stamped before it was knowable."""
    return datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC)


def known_by(stamp: datetime | None, as_of: date) -> bool:
    """Whether something stamped ``stamp`` existed by the end of ``as_of``.

    An unstamped record never counts: nothing proves it is not from after the date.
    """
    if stamp is None:
        return False
    stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    return stamp < cutoff(as_of)


def is_historical(as_of: date) -> bool:
    """Whether a run is dated before today, so present-day snapshots would leak its future."""
    return as_of < today()
