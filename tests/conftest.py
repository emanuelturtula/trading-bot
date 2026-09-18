"""Shared pytest configuration.

The ``trading-bot`` Hypothesis profile inherits the active default, so Hypothesis' built-in CI
profile (derandomized, no example database) still applies on CI.

Every test runs under the network guard (spec 011, Design 13.6): outbound connections, remote
name resolution and ``curl_cffi`` requests raise ``NetworkAccessError``, while loopback
connections, ``socket.socketpair()`` and ``asyncio.run`` keep working. Never disable it.

Every test also runs with ``TB_DATA_DIR`` pointing at pytest's session temporary directory
(spec 012, D81), so an accidental ``get_settings()`` or ``open_database(settings.data_dir)``
can never reach a real database, and a session-scoped check fails the run if any test left a
``trading_bot.db*`` file in the working tree. Databases are created through
``tests/fixtures/database.py`` and live under ``tmp_path``. Never disable these either.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from tests.fixtures.database import database  # noqa: F401 - re-exported as a pytest fixture
from tests.fixtures.network_guard import install_network_guard
from trading_bot.persistence.engine import DATABASE_FILENAME

settings.register_profile("trading-bot", parent=settings.default, max_examples=50, deadline=None)
settings.load_profile("trading-bot")

REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNED_DIRECTORIES = frozenset(
    {".git", ".venv", ".hypothesis", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)


def stray_database_files(root: Path) -> list[str]:
    """Return the ``trading_bot.db*`` files under ``root``, with cache directories pruned."""
    found: list[str] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in PRUNED_DIRECTORIES]
        found.extend(
            (Path(directory) / name).relative_to(root).as_posix()
            for name in filenames
            if name.startswith(DATABASE_FILENAME)
        )
    return sorted(found)


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block network access for the whole test (decision D60)."""
    install_network_guard(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point ``TB_DATA_DIR`` at the session temporary directory (spec 012, D81)."""
    monkeypatch.setenv("TB_DATA_DIR", str(tmp_path_factory.getbasetemp()))


@pytest.fixture(scope="session", autouse=True)
def no_database_in_the_working_tree() -> Iterator[None]:
    """Fail the session if a test created a database outside pytest's temporary directory."""
    yield
    strays = stray_database_files(REPO_ROOT)
    assert strays == [], f"tests created database files in the working tree: {strays}"
