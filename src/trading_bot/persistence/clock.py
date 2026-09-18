"""The clock the repositories write timestamps with (spec 013, D92).

SQLite's ``CURRENT_TIMESTAMP`` is naive text with second resolution and would bypass
``UtcDateTime`` and CLAUDE.md rule 6, and a column default would hide the clock inside the ORM,
which the project avoids everywhere else (spec 005 D36). Repositories take the clock as an
argument instead: tests inject a fixed one and assert literal instants, so no test reads the
wall clock and the Windows gate agrees with the Linux CI.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

__all__ = ["Clock", "system_clock"]

type Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The current instant, aware and in UTC."""
    return datetime.now(UTC)
