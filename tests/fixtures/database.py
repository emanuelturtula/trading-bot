"""Throwaway migrated databases for tests (spec 012, Design 10.1).

Every test that needs a database goes through this module, so no test can reach a real one:
``temporary_database`` opens a migrated ``Database`` on a directory of its own and disposes it
afterwards, and the ``database`` fixture does the same under pytest's ``tmp_path``. Features
#12 and #13 reuse them instead of adding a second fixture, a second engine or a second base.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from trading_bot.persistence.database import Database, open_database

# Short on purpose: a deadlocked test fails quickly instead of waiting five seconds.
TEST_BUSY_TIMEOUT_MS = 200


@contextmanager
def temporary_database(
    path: Path, *, busy_timeout_ms: int = TEST_BUSY_TIMEOUT_MS
) -> Iterator[Database]:
    """Yield a migrated ``Database`` whose data directory is ``path``, disposing it on exit."""
    database = open_database(path, busy_timeout_ms=busy_timeout_ms)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    """A migrated database under the test's own ``tmp_path``."""
    with temporary_database(tmp_path / "database") as handle:
        yield handle
