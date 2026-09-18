"""The shared database fixture (spec 012, T12, AC19, AC20)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from tests.conftest import REPO_ROOT, stray_database_files
from tests.fixtures.database import database, temporary_database  # noqa: F401 - pytest fixture
from trading_bot.config import Settings
from trading_bot.persistence.database import Database
from trading_bot.persistence.engine import DATABASE_FILENAME
from trading_bot.persistence.migrator import current_revision, head_revision

SEEN_PATHS: set[str] = set()


def record_disposals(engine: Engine, disposed: list[Engine]) -> None:
    event.listen(engine, "engine_disposed", disposed.append)


def test_the_context_manager_yields_a_migrated_database(tmp_path: Path) -> None:
    disposed: list[Engine] = []

    with temporary_database(tmp_path / "state") as handle:
        record_disposals(handle.engine, disposed)
        assert isinstance(handle, Database)
        assert current_revision(handle.engine) == head_revision()
        assert disposed == []

    assert len(disposed) == 1


def test_the_context_manager_writes_only_under_the_given_path(tmp_path: Path) -> None:
    target = tmp_path / "state"

    with temporary_database(target):
        pass

    assert [path.name for path in tmp_path.iterdir()] == ["state"]
    assert [path.name for path in target.iterdir()] == [DATABASE_FILENAME]


def test_the_context_manager_disposes_the_database_on_an_exception(tmp_path: Path) -> None:
    disposed: list[Engine] = []

    with pytest.raises(RuntimeError, match="boom"), temporary_database(tmp_path) as handle:  # noqa: PT012
        record_disposals(handle.engine, disposed)
        raise RuntimeError("boom")

    assert len(disposed) == 1


@pytest.mark.parametrize("run", [1, 2])
def test_the_fixture_gives_every_test_its_own_database(
    database: Database,  # noqa: F811 - the imported fixture
    run: int,
) -> None:
    SEEN_PATHS.add(str(database.engine.url.database))

    assert current_revision(database.engine) == head_revision()
    assert len(SEEN_PATHS) == run


def test_each_fixture_database_was_a_different_file() -> None:
    assert len(SEEN_PATHS) == 2


def test_every_test_runs_with_an_isolated_data_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The autouse guard of ``tests/conftest.py`` (spec 012, AC20)."""
    data_dir = Settings(_env_file=None).data_dir  # type: ignore[call-arg]

    assert data_dir == tmp_path_factory.getbasetemp().resolve()
    assert data_dir != Path.cwd() / "data"


def test_the_stray_database_check_detects_a_file_and_prunes_caches(tmp_path: Path) -> None:
    """Control: the session-end guard is not vacuous (spec 012, AC20)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / f"{DATABASE_FILENAME}-wal").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / DATABASE_FILENAME).write_text("", encoding="utf-8")

    assert stray_database_files(tmp_path) == [f"src/{DATABASE_FILENAME}-wal"]


def test_no_database_file_is_in_the_working_tree_right_now() -> None:
    assert stray_database_files(REPO_ROOT) == []
