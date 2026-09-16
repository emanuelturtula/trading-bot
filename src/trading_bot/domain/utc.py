"""Lossless conversion of aware datetimes to stdlib UTC datetimes (CLAUDE.md rule 6)."""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["to_utc"]


def to_utc(value: datetime) -> datetime:
    """Return the instant ``value`` as a plain ``datetime`` whose ``tzinfo`` is ``datetime.UTC``.

    Any tz-aware ``datetime`` (including ``pd.Timestamp``) is accepted, also wall times in a DST
    fold. Naive values and ``NaT`` raise ``ValueError`` because they do not name an instant, and
    so does sub-microsecond precision, which a stdlib ``datetime`` cannot hold. Anything that is
    not a ``datetime`` raises ``TypeError``.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    # NaT has no tzinfo and raises from utcoffset(), so tzinfo is checked first.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expected a timezone-aware datetime, got a naive value")
    converted = value.astimezone(UTC)
    result = datetime(
        converted.year,
        converted.month,
        converted.day,
        converted.hour,
        converted.minute,
        converted.second,
        converted.microsecond,
        tzinfo=UTC,
    )
    # Compare with the converted value, never with ``value``: PEP 495 makes an inter-zone ``==``
    # False for wall times in a DST fold. pandas compares with nanosecond precision.
    if result != converted:
        raise ValueError("expected a datetime without sub-microsecond precision")
    return result
