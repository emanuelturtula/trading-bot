"""Alembic layout, upgrade/downgrade and ``env.py`` (spec 012, T6, T7, T8, AC10-AC13)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Engine

from trading_bot.persistence import migrator
from trading_bot.persistence.base import Base
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import (
    alembic_config,
    current_revision,
    head_revision,
    run_migrations,
)

MIGRATOR_LOGGER = "trading_bot.persistence.migrator"
REVISION_ID = re.compile(r"^[0-9]{4}$")
BASELINE = "0001"
HEAD = "0002"
CONFIGURATION_TABLES = ["rules", "ticker_rules", "tickers"]


def table_names(engine: Engine) -> list[str]:
    """The tables SQLite holds, without the internal ones (``sqlite_sequence``)."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name"
        )
        return [str(name) for name in rows.scalars().all()]


def stored_revisions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        return [str(value) for value in rows.scalars().all()]


def migrator_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == MIGRATOR_LOGGER]


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


# --- T6: layout (AC10) ---------------------------------------------------------------------


def test_the_configuration_needs_no_ini_file_and_no_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD]


def test_every_revision_identifier_is_four_digits_and_the_chain_is_linear() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    revisions = list(script.walk_revisions())

    assert [revision.revision for revision in revisions] == [HEAD, BASELINE]
    for revision in revisions:
        assert REVISION_ID.match(revision.revision), revision.revision
        assert revision.down_revision is None or REVISION_ID.match(str(revision.down_revision))
        assert not isinstance(revision.down_revision, tuple)  # no merge points
    assert script.get_revision(BASELINE).down_revision is None


def test_the_head_revision_is_the_latest_one() -> None:
    assert head_revision() == HEAD


def test_two_heads_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stacked branch that forgot ``down_revision`` must fail loudly, not pick a head."""
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    for identifier in ("0001", "0002"):
        (versions / f"{identifier}_branch.py").write_text(
            f'"""branch"""\n\nrevision = "{identifier}"\ndown_revision = None\n\n\n'
            "def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n",
            encoding="utf-8",
        )
    forked = Config()
    forked.set_main_option("script_location", str(tmp_path / "migrations"))
    monkeypatch.setattr(migrator, "alembic_config", lambda: forked)

    with pytest.raises(RuntimeError, match="0001"):
        head_revision()


def test_the_configuration_carries_no_database_url() -> None:
    config = alembic_config()

    assert config.get_main_option("sqlalchemy.url", None) is None
    assert config.config_file_name is None


# --- T7: upgrade and downgrade (AC11) ------------------------------------------------------


def test_the_baseline_creates_only_the_version_table(tmp_path: Path) -> None:
    """The baseline still creates nothing of its own: the first tables arrive with ``0002``."""
    engine = fresh_engine(tmp_path)
    config = alembic_config()
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, BASELINE)

        assert current_revision(engine) == BASELINE
        assert stored_revisions(engine) == [BASELINE]
        assert table_names(engine) == ["alembic_version"]
    finally:
        engine.dispose()


def test_the_first_upgrade_reaches_the_head_revision(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger=MIGRATOR_LOGGER):
            revision = run_migrations(engine)

        assert revision == HEAD
        assert current_revision(engine) == HEAD
        assert stored_revisions(engine) == [HEAD]
        assert table_names(engine) == ["alembic_version", *CONFIGURATION_TABLES]
        assert migrator_messages(caplog) == ["database schema upgraded from empty to 0002"]
    finally:
        engine.dispose()


def test_a_second_upgrade_is_a_no_op(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger=MIGRATOR_LOGGER):
            revision = run_migrations(engine)

        assert revision == HEAD
        assert stored_revisions(engine) == [HEAD]
        assert table_names(engine) == ["alembic_version", *CONFIGURATION_TABLES]
        assert migrator_messages(caplog) == ["database schema already at revision 0002"]
    finally:
        engine.dispose()


def test_downgrade_to_base_and_back_to_head(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        config = alembic_config()

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")

        assert table_names(engine) == ["alembic_version"]
        assert stored_revisions(engine) == []
        assert current_revision(engine) is None

        assert run_migrations(engine) == HEAD
        assert stored_revisions(engine) == [HEAD]
    finally:
        engine.dispose()


def test_the_current_revision_of_an_untouched_database_is_none(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        assert current_revision(engine) is None
    finally:
        engine.dispose()


# --- T8: env.py (AC12, AC13) ---------------------------------------------------------------


def test_offline_migrations_are_refused(tmp_path: Path) -> None:
    config = alembic_config()

    with pytest.raises(RuntimeError, match="offline"):
        command.upgrade(config, "head", sql=True)

    assert not list(tmp_path.iterdir())


def test_the_passed_connection_carries_the_whole_migration(tmp_path: Path) -> None:
    """The caller owns the transaction, so rolling it back undoes the whole migration.

    Since spec 013 D88 handed transaction control to SQLAlchemy, that covers the DDL too: not
    even ``alembic_version`` survives the rollback, so a migration that fails halfway leaves
    the database exactly as it was.
    """
    engine = fresh_engine(tmp_path)
    config = alembic_config()
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

            inside = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            transaction.rollback()

        assert inside == HEAD
        assert table_names(engine) == []
        assert current_revision(engine) is None
    finally:
        engine.dispose()


def test_the_environment_configures_the_metadata_and_batch_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, object] = {}
    configure = EnvironmentContext.configure

    def spy(self: EnvironmentContext, **kwargs: object) -> None:
        recorded.update(kwargs)
        configure(self, **kwargs)

    monkeypatch.setattr(EnvironmentContext, "configure", spy)
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
    finally:
        engine.dispose()

    assert recorded["target_metadata"] is Base.metadata
    assert recorded["render_as_batch"] is True


def test_without_a_connection_the_environment_falls_back_to_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The developer CLI path: ``alembic upgrade head`` with no caller-supplied connection."""
    data_dir = tmp_path / "cli"
    monkeypatch.setenv("TB_DATA_DIR", str(data_dir))

    command.upgrade(alembic_config(), "head")

    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=200)
    try:
        assert stored_revisions(engine) == [HEAD]
    finally:
        engine.dispose()


def test_an_unknown_stored_revision_fails_loudly(tmp_path: Path) -> None:
    """The image-rollback case: an older image must refuse a newer schema (spec 012, §14)."""
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE alembic_version SET version_num = '9999'")

        with pytest.raises(CommandError, match="9999"):
            run_migrations(engine)

        assert table_names(engine) == ["alembic_version", *CONFIGURATION_TABLES]
        assert stored_revisions(engine) == ["9999"]
    finally:
        engine.dispose()


def test_the_environment_never_configures_logging_or_a_url() -> None:
    source = (Path(migrator.MIGRATIONS_DIR) / "env.py").read_text(encoding="utf-8")

    assert "fileConfig" not in source
    assert "sqlalchemy.url" not in source


def test_a_migration_that_stamps_no_revision_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_migrations`` returns the applied revision, never ``None``."""
    monkeypatch.setattr(migrator.command, "upgrade", lambda config, revision: None)
    engine = fresh_engine(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="no revision"):
            run_migrations(engine)
    finally:
        engine.dispose()
