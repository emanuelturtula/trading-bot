# 012 — SQLite persistence: engine, pragmas and Alembic migrations

- **Status:** approved (rulings R1–R3 recorded in "Implementation notes" after `[impl]`)
- **Branch:** `feature/sqlite-persistence` (first of the stacked M3 series #11 → #12 → #13)
- **Spec author:** tech-lead
- **Issue:** #11 (milestone M3 · Persistence)
- **Expected commit type:** `feat:`. The change adds two runtime dependencies (`sqlalchemy`, `alembic`), one `TB_*` variable, the `src/trading_bot/persistence/` package with the first Alembic revision, and the application lifespan that applies migrations at startup. It changes the published image (dependencies and a build-time smoke check) and the running app (startup behaviour). `feat` → minor bump. Suggested squash subject: `feat: add the SQLite persistence layer with Alembic migrations (#<n>)`.

## Goal

Give the bot a production-ready database on the Raspberry Pi, with nothing in it yet:

- one SQLite file at `TB_DATA_DIR/trading_bot.db`, the exact path `deploy/deploy.py` backs up before every deploy;
- one engine per process with the pragmas that make SQLite safe for this app (WAL, `foreign_keys=ON`, a busy timeout, durable commits), applied where they cannot be forgotten;
- Alembic migrations that ship inside the wheel and are applied at startup, before the scheduler (#15) and the Telegram poller (#19) exist;
- the `DeclarativeBase`, the constraint naming convention and the UTC column type that #12 and #13 extend;
- a test fixture that gives every later feature a migrated, throwaway database without touching a real one.

## Out of scope

- **Business tables and repositories.** Tickers, rules, assignments and the import/export CLI: #12. Signals, the idempotency unique constraint and `bot_state`: #13. This feature ships an **empty** baseline migration (decision D78) and no ORM model.
- **Consumers.** Engine (#14), scheduler (#15), provider and calendar wiring (#16), Telegram (#19), API and dashboard (#22, #23). The lifespan added here only opens and closes the database.
- **Backup, restore and retention.** `deploy/deploy.py` already backs up before each deploy and keeps the last 10 attempts; this feature only guarantees that it finds the file, and never modifies `deploy/`.
- **A second backend or a second database.** SQLite only, one file, one writer process (`CLAUDE.md` rule 7).
- **Async SQLAlchemy, `aiosqlite`, pools tuned for concurrency, read replicas, encryption at rest.** See D69.
- **`/status`, metrics, heartbeats and schema reporting to users** (F7, #25). `/health` is unchanged (D79).
- **Changes to** `domain/`, `data/`, `docs/ROADMAP.md`, `CLAUDE.md`, `.github/`, `scripts/`, `deploy/`, `.gitignore`, `.dockerignore`. `Dockerfile`, `pyproject.toml`, `uv.lock`, `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` change only as §9 and §12 state.

## Acceptance criteria

"Temporary database" means a file under pytest's `tmp_path`. "Container path" means the POSIX path the app computes when `TB_DATA_DIR=/app/data`, the value `deploy/compose.yml` sets.

### Configuration

- [ ] **AC1 (`TB_DATA_DIR`):** `Settings.data_dir` is a `Path` with default `Path("data")`, read from `TB_DATA_DIR`, stored as an absolute, user-expanded, resolved path; an empty or whitespace-only value raises `ValidationError`. It is **not** a secret: it is absent from `Settings.secret_values()`, and `repr(settings)`, `str(settings)` and `model_dump_json()` may contain it. `.env.example` documents it (ruling R3) as a **commented** line carrying the default, next to the other non-secret variables, so copying the file to `.env` pins no value and local development keeps the default.
- [ ] **AC2 (database path):** `DATABASE_FILENAME == "trading_bot.db"`, `database_path(data_dir) == data_dir / DATABASE_FILENAME`, and the file name is a constant with no `TB_*` override. With `TB_DATA_DIR=/app/data` the container path, as a `PurePosixPath`, equals the `DATABASE` constant of `deploy/deploy.py`, and `deploy/compose.yml` sets `TB_DATA_DIR` to that same directory. One test asserts all three together (T13).

### Engine, pragmas and sessions

- [ ] **AC3 (engine factory):** `create_database_engine(path, *, busy_timeout_ms=BUSY_TIMEOUT_MS)` builds the URL with `URL.create("sqlite+pysqlite", database=str(path))` (so spaces and `#` in a development path are safe), creates the parent directory with `mode=0o700, parents=True, exist_ok=True`, never enables `echo`, and creates no file until the first connection.
- [ ] **AC4 (pragmas on every connection):** every connection handed out by an engine from the factory reports `journal_mode=wal`, `foreign_keys=1`, `busy_timeout=<busy_timeout_ms>` and `synchronous=2` (`FULL`). Verified on the first connection, on a connection taken from a second thread, and on a connection opened after `engine.dispose()`; the listener is registered inside the factory, so no caller can create an engine without it.
- [ ] **AC5 (foreign keys are enforced):** inserting a row that violates a foreign key raises `IntegrityError` on an engine from the factory, and the same insert succeeds on an engine built with a bare `create_engine` (the control that proves the pragma, not SQLite, is what rejects it).
- [ ] **AC6 (sessions):** `create_session_factory(engine)` returns a `sessionmaker[Session]` with `expire_on_commit=False`. `Database.session()` commits on a clean exit, rolls back and re-raises on any `BaseException` (including `KeyboardInterrupt` and `asyncio.CancelledError`), and always closes the session.
- [ ] **AC7 (`Database` handle):** `open_database(data_dir, *, busy_timeout_ms=...)` creates the engine, applies migrations and returns a frozen `Database(engine, session_factory)`; if migrations fail it disposes the engine before re-raising. `Database.dispose()` disposes the engine, and after it no `-wal` or `-shm` file is left next to a cleanly closed database.

### UTC columns and metadata

- [ ] **AC8 (`UtcDateTime`):** a `TypeDecorator[datetime]` over `DateTime` with `cache_ok = True` that binds through `to_utc` (`domain/utc.py`) and stores a naive UTC value, and returns values with `tzinfo=datetime.UTC`.
  - aware values in any zone round-trip to the same instant; `None` round-trips to `None`;
  - naive values, `NaT`, non-`datetime` values and sub-microsecond precision are rejected with the `ValueError`/`TypeError` of `to_utc` (wrapped by SQLAlchemy in `StatementError`), and nothing is written;
  - the stored text always carries six fractional digits, so `ORDER BY` in SQL matches chronological order (T11 checks it on drawn instants).
- [ ] **AC9 (metadata):** `Base` is a `DeclarativeBase` whose `metadata` uses exactly the `NAMING_CONVENTION` of §5, `models.py` re-exports `Base`, and `Base.metadata.tables` is empty in this feature.

### Alembic

- [ ] **AC10 (layout and packaging):** `alembic_config()` returns a `Config` that needs no `alembic.ini` and no particular working directory: the test changes into an unrelated temporary directory first. `ScriptDirectory.from_config(alembic_config())` finds exactly one head; every revision identifier matches `^[0-9]{4}$`; the revision chain is linear and `0001` has `down_revision is None`.
- [ ] **AC11 (`upgrade head` / `downgrade base`):** on a temporary database,
  - `run_migrations(engine)` returns the head revision, creates `alembic_version` with exactly that revision and creates **no** other table;
  - a second `run_migrations` on the same database is a no-op: the revision is unchanged, `alembic_version` still holds one row, and the log record is the "already at revision" one of §7.3;
  - `command.downgrade(config, "base")` leaves `alembic_version` present with zero rows and no application table, and `current_revision(engine)` returns `None`;
  - `upgrade head` after that downgrade returns to the head revision.
- [ ] **AC12 (`env.py`):** it takes the connection from `config.attributes["connection"]` when present and otherwise builds one from `Settings`; it configures `target_metadata is Base.metadata` and `render_as_batch=True`; it raises `RuntimeError` in offline mode (`--sql`), with an English message; it never calls `fileConfig` and never puts a URL into the `Config`.
- [ ] **AC13 (unknown revision):** a database whose `alembic_version` holds a revision the package does not contain makes `run_migrations` raise `alembic.util.exc.CommandError` (the image-rollback case of §14), and no table is created or dropped.

### Startup and `/health`

- [ ] **AC14 (lifespan):** `create_app(settings)` registers a lifespan that, in this order, opens the database (migrations included) and stores the handle on `app.state.database`, and on shutdown disposes it. Nothing else runs before the migration.
  - `create_app(settings)` on its own, and a `TestClient` used **without** the context manager, open no connection and create no file;
  - with the context manager, `client.app.state.database` is a `Database` whose revision is the head.
- [ ] **AC15 (empty and migrated volumes, issue AC1):** starting on an empty directory and starting again on the same directory both succeed, leave the revision at head and leave exactly one row in `alembic_version`. The first start logs the "upgraded" record of §7.3 and the second the "already at revision" one.
- [ ] **AC16 (failure aborts startup):** when the migration fails (unknown revision, unreadable file, a directory where the file should be), entering the lifespan raises, `app.state` holds no database, and the engine is disposed. The container therefore never becomes healthy and `deploy/deploy.py` rolls back.
- [ ] **AC17 (`/health` unchanged):** the response body is still exactly `{"status", "version", "environment"}`, it is produced without any database access, and it contains neither the data directory nor the database path or file name.
- [ ] **AC18 (nothing leaks into the logs):** a full startup and shutdown over a data directory whose name contains a unique marker emits no log record containing that marker, the database file name or the URL scheme, from any logger. The single `INFO` record of §7.3 names the revision only.

### Test fixtures and isolation

- [ ] **AC19 (shared fixture, for #12 and #13):** `tests/fixtures/database.py` exposes `temporary_database(path, *, busy_timeout_ms=...)`, a context manager yielding a migrated `Database` on that path and disposing it afterwards, and the pytest fixture `database` built on `tmp_path`. It is typed (`tests/fixtures` is in mypy's `files`) and is used by every test in this feature that needs a database.
- [ ] **AC20 (no real database is touched):** an autouse fixture in `tests/conftest.py` points `TB_DATA_DIR` at a directory under pytest's session temporary directory for every test, and a session-scoped check asserts that no `trading_bot.db*` file exists anywhere in the working tree when the suite ends (`.git`, `.venv`, `.hypothesis` and cache directories pruned).

### Packaging, deploy, docs and gate

- [ ] **AC21 (dependencies):** `pyproject.toml` adds `sqlalchemy>=2.0` and `alembic>=1.13` to `[project].dependencies` and nothing else; `uv.lock` is regenerated; no dev dependency is added. Every added distribution has a `linux/aarch64` wheel for CPython 3.12.
- [ ] **AC22 (image):** the `Dockerfile` gains one build-time check that runs `upgrade head` on a throwaway database under `/tmp` inside the image and asserts the head revision and `journal_mode=wal`; it never writes to `/app/data`. The arm64 build in CI is the authoritative run.
- [ ] **AC23 (wheel contents):** a wheel built from the branch contains `trading_bot/persistence/migrations/env.py`, `trading_bot/persistence/migrations/script.py.mako` and `trading_bot/persistence/migrations/versions/0001_baseline.py`; `git check-ignore` reports none of them as ignored (the `.gitignore` `data/` trap of spec 010 D66 does not apply here, and `.gitignore` is **not** modified).
- [ ] **AC24 (isolation guard):** an AST guard over `src/trading_bot/persistence/` allows only the standard library, `sqlalchemy`, `alembic`, `trading_bot.domain.utc` and `trading_bot.persistence.*`; `trading_bot.config` is allowed in `migrations/env.py` only; `pandas`, `numpy`, `talib`, `yfinance`, `exchange_calendars`, `fastapi` and `trading_bot.data` are rejected everywhere. Importing `trading_bot.persistence.engine` in a fresh interpreter pulls neither `pandas` nor `alembic` into `sys.modules`. The `domain/` purity guard keeps passing unchanged: no domain module imports `persistence`.
- [ ] **AC25 (typing and gate):** `uv run python scripts/check.py` is green; strict mypy passes with no `Any` and no `type: ignore` in the new `src/` files and in `tests/fixtures/database.py`; coverage does not regress and the new modules are fully covered, **except** `migrations/env.py` and `versions/0001_baseline.py`, which Alembic loads under module names outside the package and which coverage therefore does not report at all: they are verified behaviourally as §10.3 states (ruling R1). No `[tool.coverage.run]` and no `[tool.coverage.report]` entry is added.
- [ ] **AC26 (docs):** `docs/ARCHITECTURE.md` gains the `## Persistence` section of §12, the `TB_DATA_DIR` row and the decision bullet; `docs/DEPLOYMENT.md` gains the two-line database note of §12; `.env.example` gains the commented `TB_DATA_DIR` line of §3 and still contains no value. No other documentation changes.
- [ ] **AC27 (scope):** the diff touches only the files of Design §1.

## Design

### 0. Decisions

Decisions D1–D68 are recorded in specs 003–011; this spec relies on D19 and D21 (canonical labels), D36 (async ports with an injected `now`) and D67 (blocking work off the event loop). The new decisions are technical; the next free identifier after this spec is **D84**.

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D69 | Sync or async | **Synchronous SQLAlchemy 2** (`Engine`, `Session`, `sessionmaker`). No `create_async_engine`, no `aiosqlite`. Callers that live in the event loop (#14, #19, #22) wrap their database work in `asyncio.to_thread`, as D67 already does for post-fetch processing | The app is one process with one writer (`CLAUDE.md` rule 7), and SQLite work is microseconds of C code, not network latency: there is nothing to overlap. `aiosqlite` is a thread wrapper around the same blocking driver, so it buys concurrency we do not have and adds a dependency and a second session API. Alembic's migration path is synchronous anyway (its async recipe runs it inside `run_sync`), and the async ORM needs `greenlet` on every call path, which complicates tracebacks. Sessions are cheap to create per unit of work, which is exactly what `asyncio.to_thread` requires. **Rejected:** async SQLAlchemy, which would force every repository in #12 and #13 to be async before any caller needs it |
| D70 | Layout | New package `src/trading_bot/persistence/` with `engine.py`, `database.py`, `base.py`, `models.py`, `types.py`, `migrator.py` and the `migrations/` directory (`env.py`, `script.py.mako`, `versions/`). The package re-exports nothing from `__init__.py`; the Alembic **code** module is `migrator.py` so it cannot collide with the `migrations/` directory | Mirrors `domain/` and `data/`: consumers import the module they need, so importing `Base` never drags Alembic or Mako in. Keeping `migrations/` inside the installed package is what makes the migrations ship in the wheel and in the image (§9). A module named `migrations.py` next to a directory named `migrations/` is ambiguous for the import system; the name is fixed here so #12 does not have to rename it later |
| D71 | Configuration | One new variable, **`TB_DATA_DIR`** (`Path`, default `data`, resolved to an absolute path, **not secret**, never logged deliberately). The file name is the module constant **`DATABASE_FILENAME = "trading_bot.db"`**, with no environment override, and `BUSY_TIMEOUT_MS = 5000` is a constant too | `deploy/compose.yml` already sets `TB_DATA_DIR=/app/data` and `deploy/deploy.py` hard-codes `/app/data/trading_bot.db`. One directory variable plus one constant file name means the two can only agree; a `TB_DATABASE_PATH` override would let an operator move the file where the pre-deploy backup does not look, and the backup would silently copy nothing. The default `data` resolves under the working directory, which `.gitignore` already covers, so local development never writes into a tracked path. A busy timeout per environment would only let beta and prod drift; the factory takes it as a keyword so tests can shorten it |
| D72 | Pragmas | `create_database_engine` registers a `connect` listener that runs, on **every** new DBAPI connection, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=<ms>` and `PRAGMA synchronous=FULL`. The listener is registered inside the factory, and the factory is the only supported way to build an engine | `foreign_keys` and `busy_timeout` are per connection and reset on every new one, so a module-level "run it once" would silently degrade as soon as the pool opens another. Measured with SQLAlchemy 2.0.54: a file database uses `QueuePool` with `check_same_thread=False`, so connections are reused across threads and the listener is the only place that sees all of them. WAL lets the pre-deploy backup read while the app writes. `synchronous=FULL` costs one fsync per commit, invisible at a handful of commits per candle close, and protects the Pi's SD card against losing the last committed signals on a power cut — losing them would resend notifications (rule 5) |
| D73 | Sessions | **`Database`**, a frozen handle holding the engine and a `sessionmaker(expire_on_commit=False)`, obtained only through **`open_database(data_dir)`**, which also applies migrations. `Database.session()` is a unit-of-work context manager (commit, rollback on `BaseException`, always close). No `scoped_session`, no session or connection shared between threads or across an `await` | Making migration part of "get a database" means no caller can forget it, and the handle is what #12, #14, #19 and #22 will be injected with (the same wiring style as `MarketDataProvider`). `expire_on_commit=False` keeps loaded objects readable after the commit, so a notifier can format a signal without a surprise `SELECT` or a `DetachedInstanceError`. A thread-local `scoped_session` would hide lifetimes in a process that mixes the event loop with `asyncio.to_thread` workers; one session per unit of work is what that model requires. Rolling back on `BaseException` also covers `asyncio.CancelledError` at shutdown and the tests' `NetworkAccessError` (D60) |
| D74 | Timestamps | **`UtcDateTime`**, a `TypeDecorator` over `DateTime` that binds through `to_utc` and stores a naive UTC value, returning values with `tzinfo=UTC`. Every timestamp column of #12 and #13 uses it; plain `DateTime` is banned | SQLite has no timestamp type and pysqlite returns naive values, so `DateTime(timezone=True)` silently gives back a naive datetime and rule 6 is lost at the first read. Reusing `to_utc` keeps one definition of "an instant" across the domain, the API and the database, and rejects naive values and sub-microsecond precision at the boundary instead of storing a shifted key. Measured: SQLAlchemy always renders six fractional digits (`2024-01-02 05:00:00.123456`), so text ordering equals chronological ordering and `ORDER BY`, `MIN`/`MAX` and range filters are correct. **Rejected: epoch integers**, which sort just as well but make the deploy backup and any manual inspection unreadable and need a conversion in every ad hoc query |
| D75 | Naming and batch mode | `Base.metadata` carries the `NAMING_CONVENTION` of §5, and `env.py` configures `render_as_batch=True` | SQLite names unnamed constraints itself, and its `ALTER TABLE` support is so limited that Alembic emulates changes by copying the table; both make an unnamed constraint impossible to drop in a later migration. Fixing the convention before the first real table (#12) means every index, unique constraint and foreign key has a deterministic, reviewable name, and batch mode is available from day one instead of being retrofitted when a column has to change |
| D76 | Alembic configuration | At runtime the `Config` is **built in code** (`alembic_config()`: an empty `Config` whose `script_location` is the packaged `migrations/` directory, resolved from `__file__`). A root **`alembic.ini`** exists for the developer CLI only (`revision --autogenerate`, `history`), with `prepend_sys_path = src`, `file_template` and a `ruff format` post-write hook. No URL is written in either | The image copies only `pyproject.toml`, `uv.lock` and `src/`, so a runtime that needed `alembic.ini` would work in development and fail in the container. Measured: a programmatic `Config` runs `upgrade head` and `downgrade base` from an unrelated working directory. Keeping the URL out of both configurations means a migration can never connect to a database the app did not choose, and no path ends up in Alembic's logging configuration. The post-write hook keeps generated revisions `ruff format` clean, which the gate requires. **Rejected:** `[tool.alembic]` in `pyproject.toml`, which recent Alembic supports but which would pin us to that version range for a file only developers use |
| D77 | Startup | Migrations run **first in the FastAPI lifespan**, synchronously, before anything else is created; there is no flag to skip them. A failure propagates: the lifespan never yields, uvicorn exits non-zero, the container never becomes healthy and `deploy/deploy.py` restores the previous deployment | The issue requires migrations before the scheduler and Telegram start, and the lifespan is the only place that runs once per process with a single worker. Nothing else is scheduled at that moment, so blocking the loop for a few milliseconds is free and `asyncio.to_thread` would only add a failure mode; runtime queries are a different case (D69). A `TB_RUN_MIGRATIONS=0` escape hatch would let a production container serve on an old schema, which is exactly the state this feature exists to prevent. Startup is the only writer, so no lock is needed (rule 7) |
| D78 | Baseline migration | The first revision, **`0001_baseline.py`, is empty** (an `upgrade` and a `downgrade` that create nothing). Revision identifiers are explicit, zero-padded four-digit numbers (`alembic revision --rev-id 0002 …`), and a test asserts a single head | An empty `versions/` directory would make `upgrade head` a no-op that creates no `alembic_version` table, so the wiring this feature delivers would be untested and the "already migrated volume" case would not exist. The baseline also anchors `down_revision` for #12: two stacked branches that both wrote `down_revision = None` would create two heads and a broken upgrade. Alembic's random hexadecimal identifiers make review, stacking and ordering harder to read for no benefit in a repository with a single linear history |
| D79 | `/health` | **Unchanged.** No database query, no new field. The applied revision goes to one `INFO` log record; exposing it to users is F7's `/status` | `/health` drives the container healthcheck and, through it, the automatic rollback. A probe there would turn a transient `database is locked` into a container restart that kills the single Telegram poller (rule 7) — the opposite of what the probe is for. It would also add no information: the app cannot answer `/health` at all unless the startup migration succeeded (D77), so a healthy beta **is** the evidence for issue AC1. Keeping the body at three fields also keeps the only public endpoint free of infrastructure detail |
| D80 | Issue AC2 evidence | A test (T13) asserts, in one place, that the application's container path, `deploy/deploy.py`'s `DATABASE` constant and `deploy/compose.yml`'s `TB_DATA_DIR` agree; the beta deploy confirms it (§13) | Docker is off locally, so the only honest local evidence is the cross-check of the three sources that must agree. It fails loudly if anyone later renames the file or the directory, which is the real risk: a silent rename makes the pre-deploy backup copy nothing while every deploy still reports success |
| D81 | Test isolation | An autouse fixture points `TB_DATA_DIR` at pytest's session temporary directory; every database in tests is created under `tmp_path` through `tests/fixtures/database.py`; a session-scoped check asserts no `trading_bot.db*` file was created in the working tree | Same reasoning as the network guard (D60): a rule that is only written down is eventually broken by an accidental `get_settings()` or `open_database(settings.data_dir)` in a test, and `*.db` is `.gitignore`d, so neither gitleaks nor CI would notice the stray file. The session temporary directory is created once, so the guard costs nothing per test |
| D82 | Image check | A build-time `RUN` applies `upgrade head` to a throwaway database under `/tmp` and asserts the head revision and `journal_mode=wal` | Same reasoning as D25, D62 and D66: if the migrations or the `.mako` template were left out of the wheel, or SQLAlchemy could not load on arm64, the failure would otherwise appear for the first time as a beta container that never becomes healthy. Running it on `/tmp` keeps the image layer free of a database file and never touches the data volume |
| D83 | Offline mode | `env.py` supports **online migrations only**; `--sql` raises `RuntimeError`. The connection comes from `config.attributes["connection"]`, falling back to an engine built from `Settings` for the developer CLI | Nobody generates SQL scripts for a single-file SQLite database on a Pi, and the offline branch would be untested code that needs a URL in the configuration, which D76 removes. Passing the connection is Alembic's documented way to share one transaction with the caller, and it is what makes the startup path and the tests exercise the same code |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/config.py` | developer | `data_dir` field and its validator (§3) |
| `src/trading_bot/main.py` | developer | Lifespan wiring (§8) |
| `src/trading_bot/persistence/__init__.py` | developer | Package docstring: engine, sessions, metadata, migrations; no re-exports |
| `src/trading_bot/persistence/engine.py` | developer | §4.1, §4.2 |
| `src/trading_bot/persistence/database.py` | developer | §4.3 |
| `src/trading_bot/persistence/base.py` | developer | §5 |
| `src/trading_bot/persistence/models.py` | developer | §5 (re-exports `Base`; #12 and #13 add models here) |
| `src/trading_bot/persistence/types.py` | developer | §6 |
| `src/trading_bot/persistence/migrator.py` | developer | §7.2, §7.3 |
| `src/trading_bot/persistence/migrations/env.py` | developer | §7.4 |
| `src/trading_bot/persistence/migrations/script.py.mako` | developer | §7.1 |
| `src/trading_bot/persistence/migrations/versions/0001_baseline.py` | developer | §7.5 |
| `alembic.ini` | developer | §7.1, CLI only; explicitly authorized by this spec |
| `.env.example` | developer | Two added lines: a comment and the commented `TB_DATA_DIR` default (§3, ruling R3); explicitly authorized by this spec |
| `pyproject.toml`, `uv.lock` | developer | `sqlalchemy`, `alembic` (§9) |
| `Dockerfile` | developer | The §9 smoke check; explicitly authorized by this spec |
| `tests/fixtures/database.py` | developer | §10.1 |
| `tests/conftest.py` | developer | The §10.2 autouse guards |
| `tests/unit/test_persistence_config.py` | developer | TDD: T1 |
| `tests/unit/test_persistence_engine.py` | developer | TDD: T2, T3 |
| `tests/unit/test_persistence_types.py` | developer | TDD: T4, T5 |
| `tests/unit/test_persistence_migrations.py` | developer | TDD: T6, T7, T8 |
| `tests/unit/test_persistence_startup.py` | developer | TDD: T9, T10 |
| `tests/unit/test_persistence_fixtures.py` | developer | TDD: T12 |
| `tests/unit/test_persistence_guard.py` | developer | TDD: T14 |
| `tests/deploy/test_deploy.py` | developer | One added test: T13 |
| `tests/unit/test_persistence_properties.py` | tester | T11 |
| `tests/unit/test_persistence_adversarial.py` | tester | T15 |
| `docs/ARCHITECTURE.md` | developer | §12; explicitly authorized by this spec |
| `docs/DEPLOYMENT.md` | developer | §12; explicitly authorized by this spec |
| `docs/specs/012-sqlite-persistence.md` | tech-lead | This spec |

No changes to `domain/`, `data/`, `deploy/`, `scripts/`, `.github/`, `.gitignore` or `.dockerignore`.

### 2. Layout and startup flow

```text
main.create_app(settings)
  └─ lifespan (startup)
       open_database(settings.data_dir)                       persistence/database.py
         1 database_path(data_dir)        -> <data_dir>/trading_bot.db
         2 create_database_engine(path)   -> engine + connect listener (WAL, FK, busy, sync)
         3 run_migrations(engine)         -> alembic upgrade head (one transaction)
         4 create_session_factory(engine) -> sessionmaker(expire_on_commit=False)
       app.state.database = Database(engine, session_factory)
  └─ lifespan (shutdown)
       database.dispose()                 -> pool closed, WAL checkpointed
```

`domain/` never imports `persistence`; `persistence` imports `domain.utc` only (`to_utc`). `migrations/env.py` is the single module allowed to read `Settings`, and only on the developer-CLI path.

### 3. Configuration (`config.py`)

```python
data_dir: Path = Path("data")
```

- Read from `TB_DATA_DIR` through the existing `env_prefix`. A field validator strips the text, rejects an empty or whitespace-only value with a message naming `data_dir`, and returns `Path(value).expanduser().resolve()`; the default goes through the same validator, so `Settings().data_dir` is always absolute.
- Not a secret: it is not added to `secret_values()`. It is infrastructure detail, so nothing logs it on purpose (§8) and no endpoint returns it (AC17).
- Behaviour per environment:

| Where | Value | Result |
|-------|-------|--------|
| Development (Windows, local) | default `data`, documented as a commented line in `.env.example` | `<working directory>/data/trading_bot.db`, already covered by the `.gitignore` rules `data/` and `*.db` |
| Tests | the autouse guard of §10.2 | pytest's session temporary directory; individual tests use `tmp_path` |
| Container (beta and prod) | `TB_DATA_DIR=/app/data` from `deploy/compose.yml` | `/app/data/trading_bot.db`, on the named `data` volume, owned by the `app` user created in the `Dockerfile` |

### 4. Engine, sessions and the database handle

#### 4.1 `persistence/engine.py`

```python
DATABASE_FILENAME = "trading_bot.db"
BUSY_TIMEOUT_MS = 5000


def database_path(data_dir: Path) -> Path: ...


def create_database_engine(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Engine: ...


def create_session_factory(engine: Engine) -> sessionmaker[Session]: ...
```

- The URL is `URL.create("sqlite+pysqlite", database=str(path))`: measured to survive spaces and `#` in a development path, which a hand-built `sqlite:///{path}` string does not.
- The parent directory is created with `mode=0o700, parents=True, exist_ok=True` (an existing directory keeps its mode, so the container's `/app/data` is untouched).
- `echo` is never set, so no statement or path reaches the logs through SQLAlchemy.
- Defaults measured with SQLAlchemy 2.0.54 and deliberately not overridden: a file database uses `QueuePool` with `check_same_thread=False`. Connections are therefore reused across threads, one at a time, which is what `asyncio.to_thread` callers need.

#### 4.2 Pragmas

`create_database_engine` registers, on the engine it returns:

```python
@event.listens_for(engine, "connect")
def _set_pragmas(connection: DBAPIConnection, _entry: ConnectionPoolEntry) -> None: ...
```

| Pragma | Value | Why |
|--------|-------|-----|
| `journal_mode` | `WAL` | Readers (the pre-deploy backup) do not block the writer; persisted in the file and re-asserted per connection |
| `foreign_keys` | `ON` | Off by default in SQLite and reset on every connection; #12 and #13 rely on it |
| `busy_timeout` | `BUSY_TIMEOUT_MS` (5 000) | A second writer (a `to_thread` worker) waits instead of failing immediately |
| `synchronous` | `FULL` | Durability on a Raspberry Pi SD card; a handful of commits per candle close makes the fsync cost irrelevant |

The listener signature uses `sqlalchemy.engine.interfaces.DBAPIConnection` and `sqlalchemy.pool.ConnectionPoolEntry`, which pass strict mypy without `Any` (verified on the prototype).

#### 4.3 `persistence/database.py`

```python
@dataclass(frozen=True, slots=True)
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]

    @contextmanager
    def session(self) -> Iterator[Session]: ...

    def dispose(self) -> None: ...


def open_database(data_dir: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Database: ...
```

- `session()` yields a new `Session`, commits on a clean exit, rolls back and re-raises on any `BaseException`, and closes in `finally`.
- `open_database` creates the engine, calls `run_migrations(engine)` and, if that raises, disposes the engine before re-raising, so a failed startup leaks no pool.
- `dispose()` closes the pool. Measured: once the last connection closes, SQLite checkpoints and removes the `-wal` and `-shm` files, which keeps the read-only backup of `deploy/deploy.py` simple.

### 5. Metadata (`base.py`, `models.py`)

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

`models.py` contains only `from trading_bot.persistence.base import Base` and `__all__ = ["Base"]`. Its docstring states the rule #12 and #13 must follow: **every ORM model is defined in `models.py` or imported by it**, because `env.py` reads `Base.metadata` through that module and `--autogenerate` only sees what is imported.

### 6. UTC columns (`types.py`)

```python
class UtcDateTime(TypeDecorator[datetime]):
    """Aware UTC datetimes stored as naive UTC values (CLAUDE.md rule 6)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None: ...

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None: ...
```

- Bind: `None` stays `None`; anything else goes through `to_utc(value)` (so a naive value, `NaT`, a non-`datetime` and sub-microsecond precision raise, and a `pd.Timestamp` is accepted) and is stored with `tzinfo` removed.
- Result: `None` stays `None`; anything else gets `tzinfo=UTC`.
- Measured with SQLAlchemy 2.0.54: values are stored as `YYYY-MM-DD HH:MM:SS.ffffff`, always with six fractional digits, so lexicographic order is chronological order and `ORDER BY`, `MIN`/`MAX` and `BETWEEN` behave.
- Obligation handed to #12 and #13: every timestamp column uses `UtcDateTime`; a plain `DateTime` or a string timestamp is rejected in review.
- **Comparing instants (found while testing T11).** `==` between two aware datetimes with **different** `tzinfo` objects returns `False` when one side is a wall time in a DST-ambiguous fold, even though both name the same instant: PEP 495 makes the comparison conservative. Values read back from the database always carry `datetime.UTC`, so any comparison against a value built in another zone must normalize first (`to_utc(expected)` or `expected.astimezone(UTC)`). This is a property of the operator, not of `UtcDateTime`, and it bites in tests and in any future filter built from a local time.

### 7. Alembic

#### 7.1 Layout

```text
alembic.ini                                     developer CLI only, never read at runtime
src/trading_bot/persistence/migrations/
    env.py                                      online migrations
    script.py.mako                              revision template
    versions/0001_baseline.py                   empty baseline (D78)
```

`alembic.ini` holds `script_location = src/trading_bot/persistence/migrations`, `prepend_sys_path = src`, a `file_template` of the form `<rev>_<slug>`, a `[post_write_hooks]` entry running `ruff format`, and **no** `sqlalchemy.url` and no logging configuration. It is not copied into the image (the `Dockerfile` copies `src/`, `pyproject.toml` and `uv.lock` only).

`script.py.mako` is the stock template with typed module variables (`revision: str`, `down_revision: str | None`) and `-> None` on `upgrade`/`downgrade`, because strict mypy checks `src/`. Verified on the prototype: such a revision and the `env.py` below pass `mypy --strict`.

#### 7.2 `migrator.py`

```python
def alembic_config() -> Config: ...


def head_revision() -> str: ...


def current_revision(engine: Engine) -> str | None: ...


def run_migrations(engine: Engine) -> str: ...
```

- `alembic_config()` builds an empty `Config` and sets `script_location` to `Path(__file__).resolve().parent / "migrations"`. No file is read, so the working directory is irrelevant (measured).
- `head_revision()` reads `ScriptDirectory.from_config(...).get_heads()` and raises `RuntimeError` unless there is exactly one head, naming the heads it found.
- `run_migrations(engine)` reads the current revision, opens `engine.begin()`, puts the connection in `config.attributes["connection"]`, calls `command.upgrade(config, "head")`, reads the revision again and returns it (never `None`).
- Nothing here reads `Settings` or the wall clock, and no path appears in any message.

**Known limitation: DDL is not covered by that transaction (ruling R2).** pysqlite's legacy transaction control opens a transaction before a DML statement only, so a migration's first `CREATE TABLE` runs in autocommit; everything from the first write onwards, the `alembic_version` stamp included, is transactional. A multi-statement DDL migration that fails halfway can therefore leave part of its schema applied and the revision unstamped. It fails **loudly**, not silently: the next `upgrade head` re-runs the same revision and stops on the object that already exists, so the container never becomes healthy and `deploy/deploy.py` rolls back, with the pre-deploy backup as the recovery path.

#11 accepts this: the baseline revision creates nothing, so the exposure is exactly zero, and the fix changes transaction control for **every** connection in the process, which belongs in the feature that also writes the first real DDL and the tests for it. `engine.begin()` stays as designed: it is already correct for the stamp, and it is what the fix completes. The obligation is hand-off 13 for #12, which must either adopt the documented recipe (a `connect` listener setting `isolation_level = None` plus an explicit `BEGIN` on the `begin` event, with tests that a transaction is really open, that a failed multi-statement migration leaves no partial DDL, and that the §4.2 pragmas still apply on every connection) or record a decision explaining why not. **Resolved in spec 013 (decision D88): the recipe is adopted, with the measurements and the three tests it asked for.**

#### 7.3 Logging

One `INFO` record per startup on the `trading_bot.persistence.migrator` logger, and nothing else:

```text
database schema upgraded from empty to 0001
database schema already at revision 0001
```

Alembic's own `INFO` records (`Context impl SQLiteImpl.`, `Running upgrade  -> 0001`) pass through the application handler and its redaction filter; they carry no path. `fileConfig` is never called, so Alembic never installs handlers of its own.

#### 7.4 `env.py`

```text
1 offline mode (context.is_offline_mode())      -> RuntimeError("offline migrations are not supported")
2 connection = context.config.attributes.get("connection")
3 none?    -> build one from Settings via create_database_engine (developer CLI path only)
4 context.configure(connection=…, target_metadata=Base.metadata, render_as_batch=True,
                    compare_type=True)
5 with context.begin_transaction(): context.run_migrations()
```

`target_metadata` comes from `trading_bot.persistence.models`, not from `base`, so future model modules are imported by the time `--autogenerate` compares.

#### 7.5 The baseline revision

`0001_baseline.py` has `revision = "0001"`, `down_revision = None` and an `upgrade`/`downgrade` pair whose bodies are docstrings only. Measured on the prototype: `upgrade head` on an empty file creates `alembic_version` holding `0001` and no other table; a second run is a no-op; `downgrade base` leaves `alembic_version` **present with zero rows** (the table is not dropped) and `current_revision` returns `None`.

### 8. Startup wiring (`main.py`)

`main.py` currently has no lifespan. `create_app` builds one that closes over `settings`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = open_database(settings.data_dir)
    app.state.database = database
    try:
        yield
    finally:
        database.dispose()
```

- `open_database` is called directly, not through `asyncio.to_thread`: at that point nothing else is scheduled on the loop, and a thread would only add a failure mode (D77). Runtime queries in #12 and later do use `asyncio.to_thread` (D69).
- On failure the exception propagates out of the lifespan: uvicorn reports that startup failed and exits, the healthcheck never passes and `deploy/deploy.py` restores the previous deployment. The application adds no `try/except` that would swallow it and no record that names the path; the third-party message is enough to diagnose and carries no credential (SQLite has none).
- `create_app` itself stays side-effect free: building the app, and a `TestClient` used without its context manager, open no connection and create no file (AC14). The existing `/health` tests keep working unchanged.
- `app.state.database` is set, never read, in this feature; #12 adds the typed FastAPI dependency in `api/`.

### 9. Packaging: dependencies, wheel and image

- `pyproject.toml`: `sqlalchemy>=2.0` and `alembic>=1.13` in `[project].dependencies`. Measured resolution today: `sqlalchemy 2.0.54`, `alembic 1.20.0`, plus `mako`, `markupsafe`, `greenlet` and `typing-extensions`; all publish `linux/aarch64` CPython 3.12 wheels. Both ship `py.typed`, so no mypy override is expected; if a release ever drops it, add a `follow_untyped_imports` override as spec 009 D24 did, never `ignore_missing_imports`.
- Wheel: hatchling packages `src/trading_bot`, including non-Python files, except what `.gitignore` excludes (spec 010 D66). Nothing under `persistence/` matches an ignore rule, so `.gitignore` is **not** modified; AC23 verifies the wheel content instead of assuming it.
- `Dockerfile`: one added check, in the style of the existing ones, after the yfinance check:

```text
# Fail the build if the migrations are missing from the installed wheel or cannot be applied.
RUN ["python", "-c", "<upgrade head on a throwaway database under /tmp, assert head and WAL>"]
```

  It uses `tempfile.mkdtemp()`, asserts that the current revision equals `head_revision()` and that `PRAGMA journal_mode` is `wal`, and removes the directory. It never touches `/app/data`.

### 10. Tests: fixtures and isolation

#### 10.1 `tests/fixtures/database.py` (reused by #12 and #13)

```python
@contextmanager
def temporary_database(path: Path, *, busy_timeout_ms: int = 200) -> Iterator[Database]: ...


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]: ...
```

`temporary_database` opens a migrated `Database` on `path` and disposes it on exit; the fixture yields one under `tmp_path`. The short default busy timeout keeps a deadlocked test from hanging for five seconds. The module is typed (mypy covers `tests/fixtures`) and is the **only** place later features create a database in tests.

#### 10.2 Isolation guards (`tests/conftest.py`)

- An autouse fixture sets `TB_DATA_DIR` to a directory under pytest's session temporary directory for every test, so an accidental `get_settings()` or `open_database(settings.data_dir)` can never reach the developer's real directory. It uses `tmp_path_factory.getbasetemp()`, so it creates nothing per test.
- A session-scoped autouse check asserts, at the end of the session, that no `trading_bot.db*` file exists under the repository root, pruning `.git`, `.venv`, `.hypothesis`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache` and `__pycache__`.

#### 10.3 Coverage of the Alembic scripts (ruling R1)

Alembic loads `env.py` and each revision by path, under module names outside the package (`env_py`, `0001_baseline_py`), so `[tool.coverage.run] source = ["trading_bot"]` never associates them with the measured package: they appear in **no** report line, not even at 0%. This is accepted for #11, and no coverage configuration is added, because a percentage would answer a weaker question than the tests already answer: a migration is correct when applying and reverting it produces the expected schema, which T7 and T8 assert directly, not when its lines were executed.

The verification is therefore behavioural and must be **named**, so it can never become vacuous. Each file is proven to have executed by an assertion that fails if the file is missing, empty or wrong:

| File | Proof it ran | Test |
|------|--------------|------|
| `versions/0001_baseline.py` | after `run_migrations`, `alembic_version` holds `0001` and no other table exists; after `downgrade base`, `current_revision` is `None` | T7 |
| `migrations/env.py` | offline mode raises the `RuntimeError` of §7.4; a connection passed in `config.attributes` is the one the migration runs on (same transaction); the `Settings` fallback creates the database at `TB_DATA_DIR`; an unknown stored revision raises `CommandError` | T8 |

An `omit` or `exclude_lines` entry for these paths is forbidden: it would turn "invisible" into "excused". Obligation handed to #12 and #13 (hand-off 15): with real `upgrade`/`downgrade` bodies, first re-measure whether the tracer sees the files; if it does, add the narrowest `[tool.coverage.run]` entry that reports them and record it as a decision; if it does not, extend this table with one behavioural proof per revision, asserting the resulting schema in both directions. **Resolved in spec 013 (decision D89): the tracer misses the files because `source` matches a package name against the module name Alembic loads them under, not because the bodies are empty; adding the directory `src/trading_bot/persistence/migrations` to `[tool.coverage.run] source` reports both files at 100%, and the behavioural proofs stay.**

### 11. Typing and guards

- Strict mypy, no `Any`, no `type: ignore` in the new `src/` files and in `tests/fixtures/database.py` (spec 004 convention). `sessionmaker[Session]`, `TypeDecorator[datetime]` and the typed `connect` listener were verified against `mypy --strict` on the prototype.
- `tests/unit/test_persistence_guard.py` (T14) is an AST guard over the package, mirroring `test_domain_purity.py`: an import allowlist (standard library, `sqlalchemy`, `alembic`, `trading_bot.domain.utc`, `trading_bot.persistence.*`; `trading_bot.config` in `migrations/env.py` only), plus a subprocess check that importing `trading_bot.persistence.engine` in a fresh interpreter pulls neither `pandas` nor `alembic` into `sys.modules`.
- The `domain/` purity guard is untouched and must keep passing: `domain/` never imports `persistence`.

### 12. Documentation

`docs/ARCHITECTURE.md`:

1. A new `## Persistence` section immediately before `## Persisted data (draft)`: the file location and the fixed name, WAL, `foreign_keys`, the busy timeout and `synchronous` and where they are applied, the synchronous-engine decision, `Database`/`open_database`/`Database.session()`, `UtcDateTime` and the naming convention, migrations at startup and the packaged Alembic layout, and how to add a revision (`--rev-id` with the next number, a real `downgrade`, tests). One short Python example in the style of the other sections; it must pass `ruff format --check`.
2. The `persistence/` row of the layers table: migrations applied at startup and the pragmas.
3. A `TB_DATA_DIR` row in `## Configuration (environment variables)`: "Directory of the SQLite database and other runtime data (`/app/data` in the container)".
4. One bullet in `## Decisions` summarizing D69, D72, D74 and D77, extending the existing "SQLite" bullet.

`docs/DEPLOYMENT.md`, in `## Operations`, two lines: the database lives in the `data` volume at the path `deploy/deploy.py` backs up, and from the second deploy onwards `current.json` records the `backup` it took (the confirmation of issue AC2, §13).

### 13. Verifying issue AC2 without Docker

1. **Local (authoritative for the code):** T13 asserts that the application's container path, `deploy/deploy.py`'s `DATABASE` and `deploy/compose.yml`'s `TB_DATA_DIR` agree.
2. **Beta, first deploy of the branch:** `/health` on port 8082 answers `ok` with the new version, which proves the migration ran before the app served (issue AC1). Optional confirmation from the Pi:

```text
docker compose --project-name trading-bot-beta exec app python -c \
  "import sqlite3; c = sqlite3.connect('file:/app/data/trading_bot.db?mode=ro', uri=True); \
   print(c.execute('select version_num from alembic_version').fetchall(), \
         c.execute('pragma journal_mode').fetchone())"
```

   Expected: `[('0001',)] ('wal',)`.
3. **Beta, any later deploy of the branch:** `<deploy root>/beta/current.json` has a non-null `backup`, and that attempt directory holds `database.sqlite3`. The first deploy reports no backup by design (there was no database yet), so this evidence comes from the second and later runs. No host, user or absolute path goes into the PR: report it as "beta backup recorded, attempt `<id>`".

### 14. Forward compatibility

- **Rollback after a migration.** Restoring an older image against a database migrated by a newer one makes startup fail with `alembic.util.exc.CommandError: Can't locate revision identified by '<rev>'` (measured), which is loud and non-destructive. The recovery path is the pre-deploy backup in `attempts/<id>/database.sqlite3`. From #12 on, migrations that drop or rename must follow expand/contract if a rollback has to keep working.
- **Label and key stability.** `UtcDateTime` stores the exact instant it is given, so the canonical labels of spec 009 D21 and the signal keys of spec 004 survive a round trip unchanged (T4, T11).
- **One engine per process.** `open_database` is called once, in the lifespan. #12 and #13 must inject the handle, never build a second engine or a second `DeclarativeBase`.

## Test plan

Every test is a unit test without network (the D60 guard stays autouse), without the wall clock (instants are literals) and without writing outside `tmp_path`. Mandatory template cases:

- **Anti look-ahead:** not applicable. This feature adds no indicator, no rule and no per-candle computation; `persistence/` contains no candle logic. The related obligation it does carry — an instant must survive storage unchanged, or signal keys would move — is covered by T4 and T11.
- **Idempotency (rule 5):** T7 pins that a second `run_migrations` on a migrated database changes nothing and logs the "already at revision" record, and T10 pins the same across two application startups on the same directory. Signal-level idempotency belongs to #13, and §14 hands it the constraint obligations.
- **Authorization:** not applicable. No Telegram, API or dashboard surface is added; T9 pins that `/health` keeps exactly its three fields.
- **Secret redaction (config and logs):** T1 pins that `data_dir` is absent from `secret_values()` and that the settings dump exposes no secret; T9 pins that no log record of a full startup and shutdown contains the data-directory marker, the file name or the URL scheme, and that `/health` never echoes them; T15 checks that a failed startup puts no path into the application's own records.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 configuration | unit | `TB_DATA_DIR` default, override, `expanduser`, resolution to an absolute path, empty and whitespace rejection; `data_dir` absent from `secret_values()`; `database_path` composition; the `DATABASE_FILENAME` constant | AC1, AC2 | developer |
| T2 engine and pragmas | unit | `URL.create` for a path with a space and a `#`; parent directory created `0o700`; no file before the first connection; the four pragmas on the first connection, from a second thread and after `dispose()`; `busy_timeout_ms` keyword honoured; `echo` off | AC3, AC4 | developer |
| T3 foreign keys and sessions | unit | `IntegrityError` on a foreign-key violation with the factory engine and the bare-`create_engine` control; `session()` commits, rolls back on `Exception` and on `BaseException`, always closes; `expire_on_commit=False` keeps attributes readable after commit; `open_database` disposes the engine when the migration fails | AC5, AC6, AC7 | developer |
| T4 `UtcDateTime` | unit | Round trip for UTC, `America/New_York`, a fixed offset and a `pd.Timestamp`; `None`; naive, `NaT`, non-`datetime` and sub-microsecond rejected with nothing written; stored text has six fractional digits; `ORDER BY` order equals Python order; `cache_ok` | AC8 | developer |
| T5 metadata | unit | `Base.metadata.naming_convention` equals `NAMING_CONVENTION` literally; a sample table built in the test on its own `MetaData` with the convention gets the expected index, unique, check, foreign-key and primary-key names; `models.Base is base.Base`; `Base.metadata.tables` is empty | AC9 | developer |
| T6 Alembic layout | unit | `alembic_config()` under `monkeypatch.chdir(tmp_path)`; exactly one head; identifiers match `^[0-9]{4}$`; linear chain; `0001.down_revision is None`; `head_revision()` raises when two heads are simulated | AC10 | developer |
| T7 upgrade and downgrade | unit | `run_migrations` on an empty file: return value, `alembic_version` content, no other table; second run a no-op with the "already" record; `downgrade base` leaves an empty `alembic_version` and no application table; re-upgrade returns to head; both `INFO` messages literally with `caplog` | AC11 | developer |
| T8 `env.py` | unit | Offline mode raises `RuntimeError`; a passed connection is used (same transaction); the `Settings` fallback builds the database at `TB_DATA_DIR`; `target_metadata is Base.metadata`; `render_as_batch=True`; an unknown revision raises `CommandError` and creates nothing | AC12, AC13 | developer |
| T9 startup and `/health` | unit | Lifespan order (migration before `app.state.database`); `create_app` and a bare `TestClient` create no file; state holds a `Database` at head; shutdown disposes it and leaves no `-wal`/`-shm`; `/health` body unchanged and produced with no connection; no log record contains the directory marker, the file name or the URL scheme | AC14, AC17, AC18 | developer |
| T10 empty and migrated volumes | unit | Two consecutive startups on the same directory: both succeed, one `alembic_version` row, head unchanged, first log "upgraded" and second "already"; a startup over a database created beforehand by `temporary_database` | AC15 | developer |
| T11 properties | unit (`@given`) | Over aware datetimes drawn in `[1970, 2100]` in several zones: the `UtcDateTime` round trip preserves the instant exactly, storage does not depend on the input zone, and sorting rows by the column matches sorting the instants in Python | AC8 | tester |
| T12 shared fixture | unit | `temporary_database` yields a migrated `Database`, disposes it on exit and on an exception, and writes only under the given path; the `database` fixture works twice in one session on different `tmp_path`s | AC19 | developer |
| T13 deploy path cross-check | unit (`tests/deploy`) | `PurePosixPath(database_path(Path("/app/data")).as_posix()) == PurePosixPath(deploy.DATABASE)`, and `deploy/compose.yml` sets `TB_DATA_DIR` to `/app/data`; the compose file is read as text (no YAML dependency) and the test fails if the key is absent | AC2 | developer |
| T14 isolation guard | unit | The AST allowlist over `src/trading_bot/persistence/`, with the `env.py` exception, checked first against synthetic snippets so a green result is not vacuous; the fresh-interpreter import check; `domain/` does not import `persistence` | AC24 | developer |
| T15 adversarial | unit | A data directory that is a file; a database file full of garbage (`DatabaseError`, no partial state, path absent from our records); a read-only directory (skipped where the mode cannot be enforced); two engines writing the same file with `busy_timeout_ms=50` (error class asserted, never elapsed time); a WAL reader seeing committed rows while a write transaction is open; a 4 000-character `TB_DATA_DIR`; a path with a newline; `open_database` called twice on one path; `session()` re-entered; `Database` frozen (assignment raises) | AC4–AC8, AC16 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC25 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot --cov-branch --cov-report=term-missing`: 100% for the new `src/` modules **except** `migrations/env.py` and `versions/0001_baseline.py`, which the report does not list at all (§10.3); no regression elsewhere; `git diff origin/main -- pyproject.toml` shows no `[tool.coverage.*]` change. The two unreported files are verified by the §10.3 behavioural assertions, which the tester names one by one | AC25 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 10 s and none exceeds 2 s. Reported, never asserted | AC25 |
| V4 typing | `uv run mypy`; `git grep -n "Any"` and `git grep -n "type: ignore"` over the new `src/` files and `tests/fixtures/database.py` empty | AC25 |
| V5 dependencies | `git diff origin/main -- pyproject.toml` shows only the two added dependencies; `uv.lock` adds only their transitive closure; each added distribution has a `linux/aarch64` CPython 3.12 wheel (checked on the PyPI file list) | AC21 |
| V6 wheel | `uv build --wheel --out-dir dist` (`dist/` is git-ignored) and `python -m zipfile -l` list `env.py`, `script.py.mako` and `versions/0001_baseline.py`; `git check-ignore -v` exits 1 for each of them; `git diff origin/main -- .gitignore .dockerignore` empty | AC23 |
| V7 image | The local `docker build` is **BLOCKED** (no daemon). Local substitute: run the §9 check command with `uv run python -c …`. Authoritative: the PR's `Docker build (arm64)` job and the beta deploy with `/health`, verified by the lead. Report it as BLOCKED with this justification, never as PASS | AC22 |
| V8 deploy agreement | T13 green, plus a manual read of `deploy/deploy.py` and `deploy/compose.yml` confirming neither file changed (`git diff origin/main -- deploy/` empty) | AC2 |
| V9 no stray database | The session-end guard is green, and `git status --porcelain --ignored` shows no `*.db`, `*.db-wal` or `*.db-shm` in the working tree after the suite | AC20 |
| V10 docs | §12 placement and content; `uv run ruff format --check` on the Python block of the new `## Persistence` section; `.env.example` gains only the commented `TB_DATA_DIR` line and still holds no value (`git diff origin/main -- .env.example`) | AC26 |
| V11 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC27 |
| V12 secrets and language | `python scripts/secret_scan.py --history`; an English-only review of the whole diff, including test data and log messages; no host, user, IP or absolute infrastructure path in the spec, the code, the tests or the PR text | — |

**Testing rules for this feature:**

- no wall-clock or elapsed-time assertions (the busy-timeout test asserts the error class only);
- no platform-dependent expectations: CI is Linux and development is Windows, so compare POSIX forms (`PurePosixPath`, `Path.as_posix()`) and skip the read-only-directory case where the mode cannot be enforced;
- literal expectations written out, never recomputed with the code under test (revision identifiers, pragma values and log messages are literals);
- every database lives under `tmp_path` and is created through `tests/fixtures/database.py`;
- `caplog` assertions filter on the `trading_bot.persistence.migrator` logger, except the "no leak" check of T9, which scans records from every logger.

**TDD order suggested to the developer:**

1. T1 (configuration and paths) and T13 (deploy cross-check): the contract with `deploy/`;
2. T2 → T3 (engine, pragmas, sessions);
3. T4 → T5 (`UtcDateTime`, metadata);
4. T6 → T7 → T8 (Alembic layout, upgrade/downgrade, `env.py`);
5. T12 (the shared fixture, extracted as soon as T7 needs it);
6. T9 → T10 (lifespan, empty and migrated volumes);
7. T14 (guard), the `Dockerfile` check, docs.

## Risks and security

- **A migration fails on the Pi.** Startup aborts, the container never becomes healthy and `deploy/deploy.py` restores the previous deployment; the pre-deploy backup is in `attempts/<id>/database.sqlite3`. The baseline migration is empty, so this feature's own risk is minimal; #12 and #13 inherit the obligation to test `upgrade head` **and** `downgrade base` on a temporary database before every merge.
- **A multi-statement DDL migration is not atomic** (ruling R2, §7.2). With pysqlite's legacy transaction control the first `CREATE TABLE` of a revision runs in autocommit, so a failure halfway can leave partial DDL with the revision unstamped. Zero exposure in #11 (the baseline creates nothing) and loud, not silent, when it happens: the retry stops on the existing object, the deploy rolls back and the backup is the recovery path. Hand-off 13 makes the fix part of #12, together with its first real DDL.
- **Rolling an image back after a migration.** The older image cannot recognize the stored revision and fails loudly (`CommandError`, measured) instead of writing against an unexpected schema. Mitigation: expand/contract from #12 on, plus the backup. Documented in §14 and handed to #12 and #13.
- **Concurrent access.** One process, one writer (rule 7); within it, WAL plus a 5 s busy timeout covers a `to_thread` worker overlapping another. The pre-deploy backup opens a second, read-only connection from another process: measured to work while the app holds the file open and after a clean shutdown, because `dispose()` checkpoints and removes the `-wal`. After a hard crash a leftover `-wal` cannot be recovered by a read-only connection; `deploy/deploy.py` only backs up a container it has verified running, so that case is a failed deploy stage, never silent data loss.
- **File permissions in the container.** The image creates `/app/data` owned by the `app` user and the named volume inherits that ownership, so SQLite can create the `-wal` and `-shm` files next to the database. If a volume ever ends up root-owned, startup fails with `unable to open database file` and the deploy rolls back — visible, not silent. The engine factory creates missing directories with mode `0o700` and never changes the mode of an existing one.
- **Durability against SD-card corruption.** `synchronous=FULL` plus WAL protects committed transactions against a power cut; it does not protect against a failing card. The deploy backup is the recovery path, and F7 (#26) may add an integrity alert.
- **Secret leakage.** The database holds no credential and no new secret setting is added. `TB_DATA_DIR` is infrastructure detail, not a secret, so it is a `Path`, not a `SecretStr`; nothing logs it deliberately, `/health` never returns it, and T9 pins both. An uncaught startup exception from SQLAlchemy or Alembic may contain the file path, which is the intended diagnostic and carries no credential; in the container that path is `/app/data/trading_bot.db`, which the public repository already documents.
- **Public repository.** The spec, the code, the tests and the PR text use placeholders (`<DEPLOY_HOST>`, `<deploy root>`) and never a host, user, IP or absolute path of the infrastructure. Test databases are temporary files with fixed names.
- **Supply chain.** Two new runtime dependencies, both long-lived and widely used, both shipping `py.typed` and `linux/aarch64` wheels. They enter Dependabot's scope afterwards like every other dependency.
- **Ignored paths.** Spec 010 D66 showed that `.gitignore` can silently remove files from the wheel. Nothing under `persistence/` matches a rule today, so `.gitignore` is untouched, and AC23 verifies the wheel content instead of trusting it. Any future directory named `data` under `src/` needs the same treatment.
- **Cost on the Raspberry Pi.** Opening the database and applying an empty migration takes a few milliseconds, and the startup blocks the loop for that time, before anything else is scheduled. Evidence only, never asserted.
- **Unbreakable rules.** All preserved:
  - signal-only: no order, broker or credential concept anywhere in the schema or the code;
  - pure `domain/`: `persistence` imports `domain.utc`, never the reverse; the purity guard is untouched and T14 adds an isolation guard for the new package;
  - closed candles only: unaffected (no candle logic);
  - idempotency: T7 and T10 for the migration itself; the signal constraint is handed to #13;
  - UTC: `UtcDateTime` is the only accepted timestamp column type and it goes through `to_utc`;
  - single worker: the lifespan runs once per process, and D77 explains why no lock is needed;
  - no `eval`;
  - English only.

### Hand-off list for #12 (tickers, rules, assignments) and #13 (signals, `bot_state`)

1. Define every ORM model in `persistence/models.py` or import it there; `env.py` reads `Base.metadata` through that module and `--autogenerate` only sees what is imported.
2. Never call `Base.metadata.create_all()` in `src/`: schema changes go through a migration. Tests create throwaway tables on their own `MetaData`, never on `Base.metadata`.
3. Every timestamp column uses `UtcDateTime` (D74). Store `candle_close_ts` exactly as `Evaluation.candle_close_ts` gives it (spec 004: `timeframe.nominal_close(label)`), never a recomputed value.
4. New revisions: `alembic revision --autogenerate --rev-id <next four digits> -m "<English message>"`, `down_revision` = the previous head, a single head, a real `downgrade`, and tests for `upgrade head` **and** `downgrade base` on a temporary database. Use batch mode for any `ALTER`.
5. #13's idempotency (rule 5) is a **database** unique constraint on `(ticker_id, rule_id, timeframe, candle_close_ts)`, named by the convention of §5, plus `IntegrityError` handling; a read-then-write check is not enough with a retrying scheduler.
6. Foreign keys are enforced (D72), so deletes need explicit `ondelete`/cascade rules, and a test must cover the delete path of every relationship.
7. Database work called from the event loop runs in `asyncio.to_thread` (D69), one `Database.session()` per unit of work; never share a `Session` across threads or across an `await`.
8. Use `tests/fixtures/database.py`; do not add a second database fixture, a second engine or a second `DeclarativeBase`.
9. #12's import/export CLI writes only inside `TB_DATA_DIR` or a path given on the command line, never prints a secret, and reuses `dump_rule` (spec 006) so stored rule JSON stays canonical.
10. #12 decides how the rule document is stored (`Text` with `json.dumps(dump_rule(rule))`, or a `JSON` column) and records the choice as a decision, keeping spec 006's canonical form and the stability it guarantees.
11. If a migration ever backfills data, it must be idempotent and safe to re-run after a rollback.
12. Any new `TB_*` follows D71: secrets as `SecretStr`, and no path override for the database file.
13. **Make migrations atomic before the first real DDL** (ruling R2, §7.2): either add the `isolation_level = None` plus explicit `BEGIN` recipe to `create_database_engine`, with tests that a transaction is really open, that a failed multi-statement migration leaves no partial DDL and that the §4.2 pragmas still apply on every connection, or record a decision explaining why not. Do not ship a multi-statement DDL revision without one of the two. **Resolved in spec 013 (D88): recipe adopted.**
14. **Normalize before comparing instants** (§6): a value read from the database carries `datetime.UTC`, and `==` against a value built in another zone returns `False` for a DST-ambiguous wall time. Compare `to_utc(expected)` (or `expected.astimezone(UTC)`), never the raw value, in repositories, in filters built from a local time and in tests.
15. **Re-measure the coverage of migration scripts** (ruling R1, §10.3) once `upgrade`/`downgrade` have real bodies: add the narrowest `[tool.coverage.run]` entry that makes them visible if the tracer sees them, otherwise extend the §10.3 table with one behavioural proof per revision. Never `omit` or `exclude_lines` them. **Resolved in spec 013 (D89): the directory is added to `[tool.coverage.run] source`.**

## Implementation notes

Recorded on 2026-09-17, after `[impl]` and before `[test]` finished. These three rulings answer questions the developer raised; they are part of the contract, so the tester verifies them and a later change needs a spec update.

- **R1 — coverage of the Alembic scripts: accepted as measured, no coverage configuration.** `migrations/env.py` and `versions/0001_baseline.py` appear in no coverage line because Alembic loads them under module names outside the measured package. §10.3 replaces the percentage with a named behavioural proof per file, AC25 and V2 are amended to match, and hand-off 15 makes #12 re-measure once the revisions have real bodies. An `omit` or `exclude_lines` entry for these paths stays forbidden. **No code change.**
- **R2 — pysqlite transactional DDL: accepted for #11, fixed in #12.** The exposure is zero for an empty baseline, and the fix (`isolation_level = None` plus an explicit `BEGIN`) changes transaction control for every connection in the process, so it belongs with the first real DDL migration and its tests. §7.2 documents the limitation, the Risks section carries it, and hand-off 13 makes it mandatory for #12. `run_migrations` keeps `engine.begin()`. The developer's note may stay in the test docstring, pointing at §7.2. **No code change.**
- **R3 — `.env.example`: yes, add `TB_DATA_DIR`.** It is the discoverability surface for local development and already lists the non-secret variables. It is added as a **commented** line carrying the default, so copying the file to `.env` pins nothing. `.env.example` is now in Design §1, AC26 and V10. **One code change, authorized here:** the developer adds those lines and nothing else to that file.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; M3 stacks #11 → #12 → #13.
- **D2 (2026-09-14):** synthetic data only in tests. No fixture in this feature contains market data at all.
- No pending user decisions: every decision in this spec (D69–D83) is technical. Two of them are worth the lead's awareness because they change what an operator sees, and both are recorded as decisions rather than questions: `/health` keeps exactly its three fields (D79), and there is no environment variable to skip migrations (D77).

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames, users or absolute infrastructure paths
- [x] Tests cover the acceptance criteria and fail without the implementation
- [x] Pragmas verified on every connection, including after `dispose()` and from a second thread
- [x] `upgrade head` and `downgrade base` tested on a temporary database; a single head; identifiers `^[0-9]{4}$`
- [x] Migrations run before anything else in the lifespan, and a failure aborts startup
- [x] No test touches a database outside `tmp_path`; the session-end guard is green
- [x] No `Any` and no `type: ignore` in the new files; the isolation guard and the `domain/` purity guard are green
- [x] R1: no `[tool.coverage.*]` change, and each §10.3 behavioural proof exists and fails without its file
- [x] R2: §7.2 limitation documented, `engine.begin()` kept, hand-off 13 present
- [x] R3: `.env.example` gains only the commented `TB_DATA_DIR` line and still holds no value
- [x] Scope limited to Design §1
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)

### Review record

Reviewed on 2026-09-17: **APPROVE**, after one round of requested changes. The review covered the branch `feature/sqlite-persistence` against `origin/main` (`2f87938`), the uncommitted diff and every untracked file.

- **Gate.** `uv run python scripts/check.py` green, run twice by the tech-lead (before and after the fixes): ruff, format, strict mypy, 5 053 passed, 14 skipped, 99.97% coverage, no leaks. The only uncovered line is the pre-existing `config.py` `get_settings` body.
- **Independent evidence.** A wheel built from the branch contains `migrations/env.py`, `script.py.mako` and `versions/0001_baseline.py` (AC23); `uv.lock` adds only `alembic`, `sqlalchemy`, `greenlet`, `mako` and `markupsafe`, each with a CPython 3.12 `linux/aarch64` or pure-Python wheel, so the arm64 image needs no compiler (AC21); the `Dockerfile` check payload runs locally with exit 0 (V7 substitute, `docker build` BLOCKED without a daemon); the `## Persistence` example of `docs/ARCHITECTURE.md` runs as written.
- **Structural checks.** `create_engine` is called in exactly one place in `src/`, inside the factory that registers the pragma listener, so no connection can miss them; `sqlalchemy.DateTime` appears only as the `impl` of `UtcDateTime`; `app.state.database` is assigned only after `open_database` returns; the changed and untracked files are exactly Design §1; the only non-ASCII character in the new code is `§`, which tracked files already use.
- **Findings, all fixed before approval:**
  1. **MEDIUM** — `migrator.run_migrations`'s docstring claimed the whole upgrade was transactional, contradicting §7.2 (R2) and the feature's own test docstring. It now states what the caller's transaction covers, that DDL before the first DML runs in autocommit, and where the ruling lives.
  2. **LOW** — the legacy `alembic.migration` and `alembic.environment` import shims, replaced by `alembic.runtime.*`.
  3. **LOW** — the `/health` test asserted on the `connect` event, which a probe reusing the pooled connection would not fire. It now records statements (`before_cursor_execute`) and pool checkouts, and the developer proved it by mutation: a temporary `SELECT 1` in `/health` fails the new assertion and would have passed the old one.
- **Handed to #12 and #13:** hand-off items 13 (migration atomicity before the first real DDL), 14 (normalize instants before comparing) and 15 (re-measure migration-script coverage). Two guard tests are also worth adding once models exist: `create_engine` only in `persistence/engine.py`, and `sqlalchemy.DateTime` only in `persistence/types.py`.
