"""Applying the packaged Alembic migrations (spec 012, Design 7.2, 7.3).

The configuration is built in code: the image copies only ``pyproject.toml``, ``uv.lock`` and
``src/``, so a runtime that needed ``alembic.ini`` would work in development and fail in the
container. The root ``alembic.ini`` exists for the developer CLI only. No URL is written into
either configuration, so a migration can never connect to a database the caller did not choose.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    """Return the runtime configuration: the packaged revisions and nothing else."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    """Return the single head revision, or raise when the history forked."""
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {sorted(heads)}")
    return heads[0]


def current_revision(engine: Engine) -> str | None:
    """Return the revision stored in the database, or ``None`` when it has none yet."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def run_migrations(engine: Engine) -> str:
    """Upgrade the database to the head revision and return it.

    The upgrade runs on one connection of ``engine`` inside ``engine.begin()``, and Alembic
    shares that transaction instead of opening its own. **The transaction does not cover every
    statement**: pysqlite only opens a transaction before the first DML statement, so DDL
    executed before it, such as the ``CREATE TABLE alembic_version`` of a first migration, runs
    in autocommit and survives a rollback. Everything from that first DML statement onwards,
    the revision stamp included, is transactional. A migration that fails halfway can therefore
    leave the DDL it had already applied; the recovery path is the pre-deploy backup. Spec 012
    §7.2 and hand-off 13 hold the ruling: #12 either adopts the ``isolation_level = None`` plus
    explicit ``BEGIN`` recipe, with tests, or records a decision explaining why not.

    An unknown stored revision raises ``CommandError``: an older image refuses a database
    migrated by a newer one instead of writing against it.
    """
    before = current_revision(engine)
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    after = current_revision(engine)
    if after is None:
        raise RuntimeError("the migration recorded no revision")
    if after == before:
        logger.info("database schema already at revision %s", after)
    else:
        logger.info("database schema upgraded from %s to %s", before or "empty", after)
    return after
