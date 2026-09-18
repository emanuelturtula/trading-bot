# 013 — Ticker, rule and assignment repositories, and the import/export CLI

- **Status:** approved (the four product questions are answered; see "User decisions", U1–U4)
- **Branch:** `feature/config-repositories` (second of the stacked M3 series #11 → #12 → #13)
- **Spec author:** tech-lead
- **Issue:** #12 (milestone M3 · Persistence)
- **Expected commit type:** `feat:`. The change adds the first real tables and migration, the
  repositories every later feature reads its configuration through, and a new command-line entry
  point. It adds no dependency and no `TB_*` variable. `feat` → minor bump. Suggested squash
  subject: `feat: add the configuration repositories and the management CLI (#<n>)`.
- **Wider than the issue text, by user decision U3:** the issue scopes the CLI to
  `rules import|export`; the user extended it to tickers and assignments because M4 (#14–#16)
  lands before Telegram (#19). See "User decisions" and D101.

## Goal

Give the bot a persistent configuration: which tickers it watches, which rules it evaluates and
which rule applies to which ticker, plus a way to manage all three on the Raspberry Pi before
Telegram and the dashboard exist.

- three tables (`tickers`, `rules`, `ticker_rules`) in the second Alembic revision, with the
  constraint names of spec 012 §5 and the first migration that actually creates something;
- repositories behind `Protocol`s, returning frozen records, so #14, #19 and #22 depend on a
  contract and not on the ORM;
- a rule can only be stored if it validates against the M1 schema (`CLAUDE.md` rule 8), and a rule
  can only be assigned to a ticker with the same timeframe (spec 006 D11);
- `python -m trading_bot.cli`, run with `docker exec` on the Pi: tickers, rules and assignments as
  single commands, plus `config import|export` for the whole configuration in one readable file,
  which doubles as a human-readable backup next to the binary one the deploy already takes;
- example rules in `docs/examples/rules/`, disabled, that the same CLI can import.

## Out of scope

- **Signals and `bot_state`** (#13), including the idempotency unique constraint and the signal
  history: see the hand-off list at the end.
- **Consumers.** The engine (#14), the scheduler (#15), the calendar and provider wiring (#16),
  Telegram (#19) and the API and dashboard (#22, #23) are the callers; this feature wires nothing
  into `main.py` and adds no endpoint. `/health` is unchanged and still touches no table.
- **Ticker validation against the provider.** The CLI and `TickerRepository` store a normalized
  symbol; asking Yahoo whether it exists (`validate_ticker`, spec 011) belongs to a surface that can
  wait on the network (#19, #22), not to the database layer or to a command that must work offline.
  A typo is therefore accepted and shows up as a provider error at the first run; `tickers remove`
  is the fix.
- **Deleting through `config import`.** Import merges, it never deletes (D104): there is no
  `--prune` and no "make the database look exactly like this file". Restoring an exact state is the
  job of the binary backup `deploy/deploy.py` already takes.
- **An interactive or long-running CLI.** No shell, no watch mode, no output format other than
  plain text and the export file; the dashboard (#23) is the interactive surface.
- **A second engine, a second `DeclarativeBase`, a second database fixture, `create_all()` in
  `src/`, async SQLAlchemy.** Spec 012 D69, D70, D73 and its hand-off items 1, 2 and 8 stand.
- **Changes to** `domain/`, `data/`, `deploy/`, `scripts/`, `.github/`, `.gitignore`,
  `.dockerignore`, `.env.example`, `CLAUDE.md` and `docs/ROADMAP.md`. `pyproject.toml`,
  `Dockerfile`, `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` change only as §11, §13 and §14
  state.

## Acceptance criteria

"Temporary database" means a migrated database under pytest's `tmp_path`, opened through
`tests/fixtures/database.py` (spec 012 AC19).

### Schema and migration

- [ ] **AC1 (tables):** revision `0002` creates exactly `tickers`, `rules` and `ticker_rules` with
  the columns, types, nullability, constraint names and single index of §3, verified by reflection,
  and creates nothing else. `Base.metadata.tables` holds exactly those three names.
- [ ] **AC2 (identity is never reused):** `tickers` and `rules` are created with
  `sqlite_autoincrement=True`; the `sqlite_master` DDL of both contains `AUTOINCREMENT`, and
  inserting a row after deleting the row with the highest id yields a **new** id, never the deleted
  one.
- [ ] **AC3 (codes are checked by the database):** the `sqlite_master` DDL carries
  `CHECK (timeframe IN ('1h', '4h', '1d'))` on `tickers` and `rules` and
  `CHECK (signal IN ('BUY', 'SELL'))` on `rules`; a raw `INSERT` with any other code raises
  `IntegrityError`. The expressions are built from `Timeframe` and `Side`, so a new member fails
  the golden-DDL assertion instead of silently diverging from the database.
- [ ] **AC4 (uniqueness):** `uq_tickers_symbol_timeframe` makes `(symbol, timeframe)` unique, so the
  same symbol may be tracked on two timeframes; `uq_rules_name` makes `name` unique with SQLite's
  default binary comparison (`'daily'` and `'DAILY'` are two rules, D85).
- [ ] **AC5 (timeframes cannot diverge, D11):** an assignment whose ticker and rule timeframes
  differ is rejected **by the database**: a raw `INSERT` into `ticker_rules` that bypasses the
  repository raises `IntegrityError`, and so does an `UPDATE` of either parent's `timeframe` while
  an assignment exists.
- [ ] **AC6 (cascades):** deleting a ticker or a rule deletes its `ticker_rules` rows and nothing
  else; one test covers each delete path.
- [ ] **AC7 (upgrade and downgrade):** `run_migrations` on an empty temporary database reaches
  `0002`; a second run is a no-op with the "already at revision" record; `downgrade base` drops the
  three tables and leaves `alembic_version` present with zero rows; `upgrade head` afterwards
  returns to `0002`. `0002.down_revision == "0001"`, there is exactly one head and every identifier
  matches `^[0-9]{4}$`.
- [ ] **AC8 (migrations are atomic, hand-off 13):** with the recipe of §5.2, inside
  `engine.begin()` the driver connection reports `in_transaction is True` after a `CREATE TABLE`;
  a multi-statement migration that fails halfway leaves **no** table and no revision stamp; the
  four pragmas of spec 012 §4.2 still hold on the first connection, on a connection from a second
  thread and on one opened after `dispose()`; and a `begin_nested()` savepoint rolls back
  independently of its outer transaction.

### Records, repositories and validation

- [ ] **AC9 (records):** `StoredTicker`, `StoredRule` and `Assignment` are frozen, slotted,
  keyword-only dataclasses (§6). `StoredRule.rule` is a parsed `Rule`, and every timestamp is an
  aware UTC `datetime`.
- [ ] **AC10 (Protocols):** `TickerRepository`, `RuleRepository` and `AssignmentRepository` are
  synchronous `Protocol`s (D69) with the signatures of §7; `SqlTickerRepository`,
  `SqlRuleRepository` and `SqlAssignmentRepository` implement them over a `Session`, and mypy
  proves the conformance through a Protocol-typed factory in `tests/fixtures/repositories.py`.
- [ ] **AC11 (the unit of work belongs to the caller):** no repository method calls `commit`,
  `rollback` or `close`. When the `Database.session()` block raises, nothing the repositories wrote
  is visible in a later session.
- [ ] **AC12 (an invalid rule is never persisted, issue AC1):** `RuleRepository.add` and `replace`
  take a parsed `Rule`, serialize it with `dump_rule` and re-parse the serialized text before the
  row is flushed; a mismatch raises `StoredRuleError` and writes nothing. Every path that accepts
  untrusted input (the CLI) goes through `parse_rule`. There is no `eval` and no dynamic import.
- [ ] **AC13 (the stored document is canonical):** for every payload of
  `tests/fixtures/rules.valid_payloads()`, the stored text equals
  `json.dumps(dump_rule(rule), ensure_ascii=False, allow_nan=False, separators=(",", ":"))` and
  `parse_rule(stored) == rule`. The derived `name`, `signal` and `timeframe` columns always equal
  the parsed document's values.
- [ ] **AC14 (a corrupt stored document fails loudly):** a document written with raw SQL that no
  longer parses makes the repository raise `StoredRuleError` naming the row id and the problem
  kinds, never echoing the document; it is never skipped silently (D93).
- [ ] **AC15 (duplicates):** `TickerRepository.add` for an existing `(symbol, timeframe)` raises
  `DuplicateTickerError` and `RuleRepository.add` for an existing name raises
  `DuplicateRuleNameError`, both **before** flushing, so the session stays usable and the caller can
  continue its unit of work.
- [ ] **AC16 (assignments, D11):** `AssignmentRepository.assign` raises `TimeframeMismatchError`
  naming both codes when the timeframes differ, `UnknownTickerError`/`UnknownRuleError` for a
  missing id, and is idempotent: assigning twice leaves one row with its original `created_at` and
  returns it. `RuleRepository.replace` with a different timeframe raises `AssignedTimeframeError`
  while the rule has assignments, and succeeds once it has none.
- [ ] **AC17 (timestamps, D92):** `created_at` and `updated_at` come from an injected
  `Clock`; `add` sets both to the same instant; a `replace` or `set_enabled` that changes nothing
  leaves `updated_at` untouched; every value round-trips through `UtcDateTime` unchanged and is
  compared after normalizing to UTC (spec 012 hand-off 14).
- [ ] **AC18 (symbols):** every symbol goes through `normalize_ticker` (spec 004) on the way in and
  on lookup, so `" aapl "` finds `AAPL`; a malformed symbol raises the domain's `ValueError` and
  writes nothing.
- [ ] **AC19 (deterministic order):** `list_all()` returns tickers ordered by `(symbol, timeframe)`
  and rules ordered by `name`, both compared in SQL and in Python; `list_enabled(timeframe=...)`
  filters without changing the order.

### CLI

- [ ] **AC20 (entry point and grammar):** `python -m trading_bot.cli` runs in a subprocess with the
  fourteen subcommands of §9.1; `--help` works for the program and for every subcommand with no
  database, no `TB_*` beyond the defaults and no network; an unknown option or subcommand exits `2`
  (argparse) and a successful command exits `0`.
- [ ] **AC21 (exit codes):** the table of §9.2 is honoured by every subcommand: `0` success, `1` the
  request was rejected and **nothing was written** (invalid input, a row that does not exist, a row
  that already exists, a refused conflict, a missing `--force`), `2` usage, `3` the environment is
  unusable (data directory, database, schema revision or output file).
- [ ] **AC22 (the CLI never migrates, D95):** it opens the database with `connect_database`, which
  applies no migration and raises `SchemaMismatchError` when the file is absent or its revision is
  not the head; the CLI reports that in English and exits `3`, leaving `alembic_version` untouched
  and creating no database file.
- [ ] **AC23 (`config import` is all-or-nothing and idempotent):** the whole file is validated and
  fully resolved before any write and applied in one `Database.session()`, in the order tickers →
  rules → assignments; a single invalid item anywhere leaves the database unchanged and exits `1`.
  Re-importing the same file writes nothing new: with `skip` every existing item is reported
  `skipped`, with `replace` every unchanged item is reported `unchanged`, no `updated_at` moves and
  every id is the same.
- [ ] **AC24 (`config export`):** the file matches the envelope of §9.3: `version` `1`, then the
  present sections `tickers`, `rules` and `assignments`, tickers sorted by `(symbol, timeframe)`,
  rules by name and assignments by `(symbol, timeframe, rule)`; each rule document is the canonical
  `dump_rule` output with its `enabled` flag; UTF-8, `\n` line endings and a trailing newline,
  identical on Windows and Linux. `-` writes to stdout; an existing file is refused (`1`) unless
  `--force`. Export → import into an empty database → export again is byte-identical, assignments
  included.
- [ ] **AC25 (the CLI writes nothing else):** a full session of every subcommand creates exactly the
  database file and the file given on the command line; nothing outside `TB_DATA_DIR`, `--data-dir`
  and that path is created or modified, and the caps of §9.4 reject an oversized or over-long file
  before any parse.
- [ ] **AC26 (no secret is printed):** with every secret setting populated with values built at
  runtime, no line any subcommand writes to stdout or stderr and no log record of the run contains
  one of `Settings.secret_values()`; no message contains a rule document or an absolute database
  path.

### Examples, guards, docs and gate

- [ ] **AC27 (example rules):** every file in `docs/examples/rules/` is a valid `config import`
  file whose every rule carries `"enabled": false`, parses with `parse_rule`, and has a name unique
  across the directory; importing each of them into a temporary database creates the rules
  **disabled** and creates no ticker and no assignment. A `README.md` explains the format, the CLI
  invocation and carries "Not financial advice."
- [ ] **AC28 (guards):** the persistence isolation guard keeps passing with the per-file allowlist
  of §12; `trading_bot.persistence.models`, `engine`, `database`, `types` and `base` import neither
  `pandas`, `numpy`, `talib` nor `alembic` in a fresh interpreter; `create_engine` appears only in
  `persistence/engine.py` and `sqlalchemy.DateTime` only in `persistence/types.py`; `domain/` still
  does not import `persistence`.
- [ ] **AC29 (coverage of the revisions, hand-off 15):** `pyproject.toml` adds
  `src/trading_bot/persistence/migrations` to `[tool.coverage.run] source` and nothing else; both
  revisions and `migrations/env.py` appear in the coverage report at 100%; no `omit` and no
  `exclude_lines` entry is added.
- [ ] **AC30 (typing and gate):** `uv run python scripts/check.py` is green; strict mypy passes with
  no `Any` and no `type: ignore` in the new `src/` files and in the new `tests/fixtures` module;
  coverage does not regress.
- [ ] **AC31 (docs):** `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` change as §14 states, and the
  "Persisted data (draft)" list stops being a draft for the three tables. No other documentation
  changes.
- [ ] **AC32 (scope):** the diff touches only the files of Design §1.

### Ticker, rule and assignment commands

Added when user decision U3 widened the CLI; the numbering continues after AC32 so every criterion
referenced elsewhere keeps its identifier.

- [ ] **AC33 (ticker commands):** `tickers add|remove|enable|disable|list` behave as §9.5 states:
  the symbol goes through `normalize_ticker`, `--timeframe` defaults to `1d` (spec 004), adding an
  existing `(symbol, timeframe)` exits `1` naming both, a missing row exits `1`, `enable`/`disable`
  on a row already in that state is reported and exits `0`, and `list` prints one stable line per
  ticker ordered by `(symbol, timeframe)`, or nothing when there is none.
- [ ] **AC34 (rule commands):** `rules enable|disable|remove|list` address a rule by its unique
  name (D85), report it bounded and `repr()`-escaped, and never print the document. `rules list`
  prints name, timeframe, signal, state and the assignment count.
- [ ] **AC35 (assignment commands):** `assignments add|remove|list`; `add` is idempotent (a second
  run reports `unchanged` and exits `0`), and a timeframe mismatch exits `1` with the message of
  §9.6 naming the rule's timeframe and the ticker's, never an `IntegrityError` traceback. `remove`
  on a missing assignment exits `1`.
- [ ] **AC36 (removal with assignments, D108):** `tickers remove` and `rules remove` refuse with
  exit `1` while the row has assignments, naming how many; with `--force` they succeed and report
  the assignments that were removed with it. Nothing else is deleted, and the count comes from the
  database before the delete.
- [ ] **AC37 (`--dry-run` everywhere, D107):** every mutating subcommand accepts `--dry-run`, does
  the whole unit of work and rolls it back: the database is byte-for-byte unchanged (row counts,
  ids, `updated_at` and `sqlite_sequence`), the report uses the `would …` form and carries no id,
  and a request that a real run would reject exits `1` in a dry run too.
- [ ] **AC38 (one transaction per command):** every mutating subcommand opens exactly one
  `Database.session()` and writes everything in it; an error anywhere in the command leaves nothing
  behind. Read-only subcommands open a session and write nothing.

## Design

### 0. Decisions

Decisions D1–D83 are recorded in specs 003–012; this spec relies on D8 and D11 (rule name and
timeframe, spec 006), D36 (injected clock), and D69–D78 and D81 (spec 012). D84–D100 were taken
with the spec; D101–D109 follow from the user's answers U1–U4 of 2026-09-18. The next free
identifier after this spec is **D110**.

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D84 | Schema | The three tables of §3: surrogate integer ids, `symbol`/`timeframe` unique on `tickers`, `name` unique on `rules`, the canonical document in `definition_json`, `enabled` on both, `created_at` everywhere and `updated_at` on `rules`; `ticker_rules` is the join table with `(ticker_id, rule_id)` as its primary key | Matches the draft of `docs/ARCHITECTURE.md` and the issue. Surrogate ids keep the signal key of #13 short and stable while a user renames a rule; the natural keys stay unique so the CLI, Telegram and the dashboard can address a row by name or symbol. `updated_at` is meaningful only where a row's content can change: a ticker row is `(symbol, timeframe)` plus a flag |
| D85 | Rule names are unique | `name` is **globally unique**, compared exactly as stored (SQLite's binary collation): `'daily'` and `'DAILY'` are two different rules, and a name that differs only in Unicode normalization form is a different name. The name stays the stripped 1–80 printable characters of spec 006 D8; no normalization is applied | The name is the handle the user types in Telegram (`/rules`), reads in every notification and matches on in an import file: two rules called "RSI oversold" make every one of those ambiguous, and the import would have no conflict key. Case-insensitive uniqueness was **rejected**: SQLite's `NOCASE` folds ASCII only, so `Café`/`CAFÉ` would collide while `cafe`/`CAFE` would not — an arbitrary rule to explain to a user. Normalizing to NFC was rejected too: it would rewrite the name the user sent, and the stored document would stop being exactly `dump_rule(rule)` (D87). The risk is documented instead |
| D86 | D11 is a schema invariant | `ticker_rules` carries a `timeframe` column and two **composite** foreign keys, `(ticker_id, timeframe) → tickers(id, timeframe)` and `(rule_id, timeframe) → rules(id, timeframe)`, both `ON DELETE CASCADE`, backed by `UNIQUE (id, timeframe)` on each parent. The repository still pre-checks and raises `TimeframeMismatchError` with both codes | Spec 006 D11 deferred enforcement here, and "enforced in the repository" alone is one forgotten call away from a rule being evaluated on the wrong candles. Measured on SQLite 3 with `foreign_keys=ON`: a mismatched insert, an `UPDATE` of `tickers.timeframe` and an `UPDATE` of `rules.timeframe` while an assignment exists all raise `FOREIGN KEY constraint failed`, and deleting either parent cascades. The pre-check exists because `IntegrityError` is a poor message for a user and because it keeps the session usable |
| D87 | Rule storage | `definition_json` is `Text NOT NULL` holding `json.dumps(dump_rule(rule), ensure_ascii=False, allow_nan=False, separators=(",", ":"))`. Before flushing, the repository re-parses that text and compares it with the rule it was given; a mismatch raises `StoredRuleError` and writes nothing | Spec 012 hand-off 10 asked for the choice. `Text` keeps spec 006's canonical form byte for byte, which is what makes a stored rule stable when a catalog default changes and what makes "did this rule change?" a string comparison (D96's idempotent import). A SQLite `JSON` column would be the same `TEXT` with a different label and would tempt future code into `json_extract` queries against a document whose shape only pydantic understands. `ensure_ascii=False` keeps an accented name readable in a manual `sqlite3` session; `allow_nan=False` guarantees the text is standard JSON. The re-parse costs about a millisecond on a write path used a handful of times a day and turns "an invalid rule is never persisted" into a runtime check rather than a convention |
| D88 | Transactional DDL (hand-off 13) | **Adopted.** `create_database_engine` sets `dbapi_connection.isolation_level = None` in its existing `connect` listener and registers a `begin` listener that emits `BEGIN`, so SQLAlchemy, not pysqlite, controls transactions | Measured on the prototype, with and without the recipe: without it, the driver reports `in_transaction is False` after a `CREATE TABLE` inside `engine.begin()` and a failure halfway leaves both created tables behind; with it, the transaction is open and the failure leaves the database empty. The four pragmas still apply on every connection (they run in the `connect` listener, before any `BEGIN`, which is also what keeps `PRAGMA journal_mode=WAL` legal). This feature ships the first multi-statement DDL, so spec 012 R2 required a decision. The same change fixes savepoints, which #13 needs to catch an `IntegrityError` on the signal unique constraint without losing its unit of work. **Rejected:** shipping DDL under autocommit and relying on the pre-deploy backup |
| D89 | Coverage of revisions (hand-off 15) | Measured: the tracer's blindness has nothing to do with empty bodies. `[tool.coverage.run] source = ["trading_bot"]` is a **package name**, so coverage matches candidate files by module name, and Alembic loads `env.py` and each revision under names outside the package (`env_py`, `0001_baseline_py`). Adding the directory `src/trading_bot/persistence/migrations` to `source` makes both files appear; over the whole unit suite they report **100%** and the total is unchanged. That entry is added; no `omit` and no `exclude_lines` | It is the narrowest entry that answers the question, it is a path and not an exclusion, and from now on a revision with an untested branch shows as missing lines instead of being invisible. Spec 012 §10.3's behavioural proofs stay valuable and are kept; they are no longer the only evidence |
| D90 | Derived columns | `rules.name`, `rules.signal` and `rules.timeframe` duplicate values that live in `definition_json`. They are written **only** by the repository, from the parsed rule, in the same statement as the document | The engine (#14) selects the rules of a timeframe, Telegram lists rules by name and D85/D86 need `name` and `timeframe` as real columns for their constraints. Parsing every document to filter would load pydantic and TA-Lib for a `SELECT`. They cannot drift: the repository is the only writer, it derives them from the same `Rule` object, and AC13 pins the equality for every fixture payload |
| D91 | Repository shape | Synchronous `Protocol`s (D69) whose implementations take a **`Session`**, one repository instance per unit of work, returning frozen records and never committing, rolling back or closing | Mirrors `MarketDataProvider`: the port is a `Protocol`, the implementation is injected. Taking a `Session` rather than the `Database` keeps "one `Database.session()` per unit of work" (spec 012 hand-off 7) in the caller, where the transaction boundary belongs, and lets one block use all three repositories atomically, which the CLI import needs. Frozen records instead of ORM instances mean no lazy load after the session closes, no accidental mutation reaching the database and no session affinity when the result crosses back from `asyncio.to_thread` |
| D92 | Timestamps | `Clock = Callable[[], datetime]` in `persistence/clock.py`, with `system_clock()` returning `datetime.now(UTC)`; every repository takes `clock: Clock = system_clock` and writes `created_at`/`updated_at` explicitly. No `server_default`, no `func.now()`, no Python column default | SQLite's `CURRENT_TIMESTAMP` is naive text with second resolution and would bypass `UtcDateTime` and rule 6. A column default would hide the clock inside the ORM, which the project avoids everywhere else (D36): tests inject a fixed clock and assert literal instants, so no test reads the wall clock — the gate runs on Windows and CI on Linux |
| D93 | A stored document that no longer parses | The repository raises `StoredRuleError(rule_id, problems)` and never skips the row | Silently skipping it would stop the signals of that rule without telling anyone, which is the worst failure for a signal bot; the loud failure is visible at the next run and the fix is a data migration, as spec 006 already requires when a catalog parameter is renamed. #14 must not swallow it (hand-off) |
| D94 | CLI technology and output | `argparse` from the standard library, package `trading_bot/cli/` with `__main__.py`, so `python -m trading_bot.cli` works inside the container. Commands write through an injected `TextIO` (`stream.write(...)`), never `print`, and `main()` re-configures `sys.stdout`/`sys.stderr` with `errors="backslashreplace"` when they support it | No new dependency (click, typer) for two subcommands, and `python -m` needs no console-script entry point, which the image would otherwise have to install. Injected streams make every message assertable with `io.StringIO` and keep ruff's `T20` (no `print` in `src/`) enabled instead of adding a per-file ignore. `backslashreplace` is what stops an accented rule name from raising `UnicodeEncodeError` on a Windows console; inside the container the streams are UTF-8 |
| D95 | The CLI does not migrate | `connect_database(data_dir)` opens the engine and the session factory **without** running migrations and raises `SchemaMismatchError` unless the stored revision is the head; `open_database` (the app, spec 012 D77) is unchanged | Migrations belong to the one process that owns the database at startup (rule 7). A CLI that migrated could upgrade production from a stray container and would make "the schema is applied by the app I can roll back" false. Refusing early also gives the operator an actionable message instead of a `no such table` from three layers down |
| D96 | Import and export format | The envelope of §9.3, whose sections D102 widened to tickers, rules and assignments; a rule item is `{"enabled": <bool>, "rule": <document>}`. Import is **all-or-nothing** in one transaction, validated in full before the first write, with `--on-conflict skip\|replace\|fail` on the rule name and a `--dry-run` that writes nothing. A `replace` that would change nothing leaves the row and its `updated_at` untouched | A rule document cannot carry `enabled` (spec 006 rejects unknown keys), so the flag needs an envelope; `version` lets a later format be recognized instead of half-read. All-or-nothing is what makes "an invalid rule is never persisted" true for a file and makes a failed import safe to retry. Not writing on a no-op `replace` is what makes re-importing a file a true no-op (rule 5's analogue for configuration) and keeps `updated_at` meaningful |
| D97 | Deletes and timeframe changes | `delete` is a hard delete; `ticker_rules` rows cascade (D86). A ticker's timeframe is **not** updatable: `(symbol, timeframe)` is its identity, and changing it means deleting and re-creating. A rule's timeframe may change through `replace` only while the rule has no assignment | Nothing in the app needs to preserve a deleted ticker, and a soft delete would give every query a filter to forget. Making the timeframe immutable removes the "revalidate every assignment" path spec 006 D11 hinted at, and the composite foreign key would refuse the update anyway: the repository turns it into a typed error rather than an `IntegrityError` |
| D98 | Default flags | A new ticker is `enabled=True`; a new rule is `enabled=False` | Adding a ticker is an explicit act of "watch this". A rule is a strategy: it must be reviewed before it can notify, which is also why the examples ship disabled (issue scope) and why an import file states the flag for every rule |
| D99 | Where the repositories live | In `trading_bot/persistence/repositories/`, as `CLAUDE.md`'s structure says, so `persistence/` keeps models, repositories and migrations together. The isolation guard gains **per-file** allowances: the rule repository may import `trading_bot.domain.rules.*`, the models and types may import `trading_bot.domain.timeframe` and `trading_bot.domain.signals` | Measured: `trading_bot.domain.signals` and `trading_bot.domain.timeframe` pull nothing heavy, while `trading_bot.domain.rules.schema` pulls pydantic, pandas, numpy and TA-Lib through the indicator catalog. Keeping the allowance per file preserves what the guard was written for: `Base`, the engine and the models stay importable by Telegram and the API without loading the analysis stack, and the one module that must parse rules is named explicitly in the guard and in review |
| D100 | Example rules | One JSON file per example under `docs/examples/rules/`, each a complete import file with `"enabled": false`, plus a `README.md` with the format, the `docker exec` invocation and the "Not financial advice." disclaimer. A test imports every file into a temporary database | Documentation that is executed cannot drift: a catalog change that invalidates an example fails the gate instead of failing on the Pi. Shipping them as import files, not as fragments, means the documented command is literally the one the user runs. Disabled by default because an enabled example would start notifying the moment a ticker is assigned |
| D101 | CLI scope (U3) | The CLI manages **tickers, rules and assignments**: the fourteen subcommands of §9.1, not only `rules import\|export` as the issue text says | M4 (#14–#16) merges before Telegram (#19), so a rules-only CLI would leave a window in which the bot can evaluate rules but a ticker can only be created by editing SQLite by hand on the Pi — exactly the kind of manual step that makes the pre-deploy backup the only safety net. The repositories exist anyway; the extra surface is argument parsing and its tests. Recorded as a user decision so the widening is traceable against the issue |
| D102 | One envelope, one command pair (U4) | `config import\|export` is the only bulk pair, and its envelope carries the optional sections `tickers`, `rules` and `assignments`. A section that is **absent** means "do not touch it", so a rules-only file stays valid and `docs/examples/rules/*.json` keep working unchanged. `version` stays `1` | Symmetry was the requirement: one format read and written by one pair of commands. Keeping `rules import\|export` as a second pair that reads the same envelope but ignores two sections would be a trap the day someone exports a whole configuration and re-imports it with the narrow command. `version` does not move because nothing has ever shipped: the field exists to recognize a future **incompatible** shape, and adding optional keys to a format no release has produced is not one. **Rejected:** `version: 2`, which would make every example file, written in this same PR, immediately legacy |
| D103 | The file has no database ids | Items are addressed by natural key: a ticker by `(symbol, timeframe)`, a rule by `name`, an assignment by `(symbol, timeframe, rule)` | Ids are per database and are not preserved by an import (D84 surrogate keys, D96 merge semantics), so a file carrying them would be wrong the moment it is restored anywhere but its origin. Natural keys are also what a human editing the file reads, which is the point of a readable backup. It is the same reason the export is not a dump |
| D104 | Import merges, never deletes | `config import` creates and, under `replace`, updates; it never removes a ticker, rule or assignment that the file does not mention. There is no `--prune` | A configuration file is edited by hand, and a forgotten section would silently delete every ticker the bot watches. "Make it look exactly like this" is a restore, and a restore has a better tool: the binary backup the deploy takes before every deploy. The limitation is documented in the README and in `docs/DEPLOYMENT.md` so nobody expects mirroring |
| D105 | Conflict policy covers the sections | `--on-conflict skip\|replace\|fail` (default `skip`, U2) applies to tickers (on `enabled`) and rules (on the document and `enabled`). An assignment that already exists is always `unchanged`, under every policy | An assignment has no payload beyond its own existence, so there is nothing to conflict about; making it fail under `fail` would break the common case of re-importing a file whose tickers and rules are untouched. `skip` never destroys what the user edited on the Pi, and `replace` is the explicit "the file wins" |
| D106 | Resolution happens before writing | The import plans in one pass over the whole file: envelope, then every rule through `parse_rule`, then every assignment resolved against the file **and** the database. An assignment naming a ticker or rule that is in neither, or whose timeframes differ, is a validation error reported with its path, before the first write | It is what makes "all-or-nothing" true for the cases a user actually hits, and it turns D86's `IntegrityError` into the readable message of §9.6. Resolving against the file too is what lets one file create a ticker, a rule and their assignment in a single run |
| D107 | `--dry-run` runs and rolls back | Every mutating subcommand accepts `--dry-run`; it performs the whole unit of work and then raises a private sentinel exception so `Database.session()` rolls back (spec 012 D73 rolls back on any `BaseException`). Report lines use `would create`, `would replace`, `would skip`, `would remove` and carry no id | A dry run that only re-checked the plan would miss exactly what is worth checking: the unique constraints, the composite foreign key of D86 and the re-parse guard of D87. Running for real and rolling back exercises all of them, and `sqlite_sequence` rolls back too, so no id is burnt. The sentinel lives in the CLI: the `persistence/` contract of #11 is not touched. Ids are omitted from the lines because the ones the run saw will not exist afterwards |
| D108 | Removal with assignments needs `--force` | `tickers remove` and `rules remove` refuse while the row has assignments, naming the count; `--force` deletes and reports the assignments that went with it. The repositories keep the plain hard delete, and the cascade stays the database's (D86, D97) | The schema cascade is right — an assignment without both ends is meaningless — but a command that silently removes rows the user did not name is not. Putting the guard in the CLI keeps the repository honest for #19 and #22, which will make the same decision for their own surface |
| D109 | Addressing and output | `--timeframe` defaults to `1d` (spec 004 hand-off), rules are addressed by their unique name, output is plain text with one line per item and no `--json`, and a row that is missing or already present exits `1` | `1d` is the project default timeframe, so the common command stays short. `--json` would double the output surface and its tests for a machine reader that `config export` already serves better. Exit `1` for both "not there" and "already there" keeps the code table small and matches the shell convention of "the command did nothing" |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/persistence/models.py` | developer | The three ORM models (§3) |
| `src/trading_bot/persistence/types.py` | developer | `TimeframeType`, `SideType` (§4) |
| `src/trading_bot/persistence/records.py` | developer | `StoredTicker`, `StoredRule`, `Assignment` (§6.1) |
| `src/trading_bot/persistence/errors.py` | developer | The error hierarchy (§6.2) |
| `src/trading_bot/persistence/clock.py` | developer | `Clock`, `system_clock` (D92) |
| `src/trading_bot/persistence/engine.py` | developer | The transactional-DDL recipe (§5.2) |
| `src/trading_bot/persistence/database.py` | developer | `connect_database` (§9.9) |
| `src/trading_bot/persistence/repositories/__init__.py` | developer | Package docstring; no re-exports |
| `src/trading_bot/persistence/repositories/protocols.py` | developer | The three `Protocol`s (§7) |
| `src/trading_bot/persistence/repositories/tickers.py` | developer | `SqlTickerRepository` |
| `src/trading_bot/persistence/repositories/rules.py` | developer | `SqlRuleRepository`, the document codec (§8) |
| `src/trading_bot/persistence/repositories/assignments.py` | developer | `SqlAssignmentRepository` |
| `src/trading_bot/persistence/migrations/versions/0002_*.py` | developer | §5.1 |
| `src/trading_bot/cli/__init__.py`, `__main__.py` | developer | §9: package docstring and `raise SystemExit(main(sys.argv[1:]))` |
| `src/trading_bot/cli/main.py` | developer | §9.1, §9.2: the parser, the dispatch, the session helper and `_emit` |
| `src/trading_bot/cli/tickers.py`, `rules.py`, `assignments.py` | developer | §9.5, §9.6: the single-row subcommands |
| `src/trading_bot/cli/config.py` | developer | §9.7: `config import` and `config export` |
| `src/trading_bot/cli/files.py` | developer | §9.3, §9.4: the envelope reader and writer |
| `pyproject.toml` | developer | One added `[tool.coverage.run] source` entry (§11); explicitly authorized by this spec |
| `Dockerfile` | developer | The §13 build-time check; explicitly authorized by this spec |
| `docs/examples/rules/*.json`, `docs/examples/rules/README.md` | developer | §10 |
| `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md` | developer | §14; explicitly authorized by this spec |
| `tests/fixtures/repositories.py` | developer | §15.1 |
| `tests/unit/test_persistence_models.py` | developer | TDD: T1 |
| `tests/unit/test_persistence_migration_0002.py` | developer | TDD: T2, T3 |
| `tests/unit/test_persistence_tickers.py` | developer | TDD: T4 |
| `tests/unit/test_persistence_rules.py` | developer | TDD: T5, T6 |
| `tests/unit/test_persistence_assignments.py` | developer | TDD: T7 |
| `tests/unit/test_cli_config_export.py` | developer | TDD: T8 |
| `tests/unit/test_cli_config_import.py` | developer | TDD: T9 |
| `tests/unit/test_cli_main.py` | developer | TDD: T10 |
| `tests/unit/test_cli_tickers.py` | developer | TDD: T17 |
| `tests/unit/test_cli_rules.py` | developer | TDD: T18 |
| `tests/unit/test_cli_assignments.py` | developer | TDD: T19 |
| `tests/unit/test_examples_rules.py` | developer | TDD: T11 |
| `tests/unit/test_persistence_guard.py` | developer | T16: the per-file allowlist and the two new guards |
| `tests/unit/test_config_idempotency.py` | tester | T12 |
| `tests/unit/test_persistence_repository_properties.py` | tester | T13 |
| `tests/unit/test_cli_secrets.py` | tester | T14 |
| `tests/unit/test_persistence_adversarial.py` | tester | T15 (added cases) |
| `docs/specs/013-config-repositories.md` | tech-lead | This spec |
| `docs/specs/012-sqlite-persistence.md` | tech-lead | Two pointer lines marking hand-offs 13 and 15 resolved here |

No changes to `domain/`, `data/`, `deploy/`, `scripts/`, `.github/`, `main.py`, `config.py`,
`.env.example`, `.gitignore` or `.dockerignore`.

### 2. Flow

```text
docs/examples/rules/*.json ─┐
operator file or stdin ─────┴─▶ trading_bot.cli.config import
                                  1 read (caps: bytes, item count)      cli/files.py
                                  2 envelope check (optional sections)  cli/files.py
                                  3 parse_rule per rule (domain, pure)  spec 006
                                  4 resolve + plan, nothing written yet cli/config.py
                                       tickers   create | replace | skip
                                       rules     create | replace | skip
                                       assignments   create | unchanged
                                  5 connect_database -> revision check  persistence/database.py
                                  6 one Database.session(), in order:
                                       tickers -> rules -> assignments  persistence/repositories
                                       --dry-run: sentinel -> rollback
                                  7 report per item + summary           injected TextIO

operator command ──────────────▶ trading_bot.cli.{tickers,rules,assignments}
                                  the same steps 5 to 7 for one row

#14 / #19 / #22 ─▶ asyncio.to_thread(unit_of_work)
                     with database.session() as session:
                         SqlTickerRepository(session).list_enabled()
                         SqlAssignmentRepository(session).rules_for_ticker(...)
                   ◀─ frozen records (no session affinity)
```

Nothing in this feature runs inside the event loop: the CLI is a process of its own and `main.py`
is untouched. The obligation for the callers is D69 plus spec 012 hand-off 7.

### 3. Schema (revision `0002`)

The DDL below is what SQLAlchemy renders for the models with the naming convention of spec 012 §5
(measured on the prototype); the golden-DDL assertions of T1 compare against it.

```text
CREATE TABLE tickers (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_tickers_symbol_timeframe UNIQUE (symbol, timeframe),
    CONSTRAINT uq_tickers_id_timeframe UNIQUE (id, timeframe),
    CONSTRAINT ck_tickers_timeframe CHECK (timeframe IN ('1h', '4h', '1d'))
);

CREATE TABLE rules (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(80) NOT NULL,
    signal VARCHAR(4) NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    definition_json TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_rules_name UNIQUE (name),
    CONSTRAINT uq_rules_id_timeframe UNIQUE (id, timeframe),
    CONSTRAINT ck_rules_timeframe CHECK (timeframe IN ('1h', '4h', '1d')),
    CONSTRAINT ck_rules_signal CHECK (signal IN ('BUY', 'SELL'))
);

CREATE TABLE ticker_rules (
    ticker_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT pk_ticker_rules PRIMARY KEY (ticker_id, rule_id),
    CONSTRAINT fk_ticker_rules_ticker_id_timeframe_tickers
        FOREIGN KEY (ticker_id, timeframe) REFERENCES tickers (id, timeframe) ON DELETE CASCADE,
    CONSTRAINT fk_ticker_rules_rule_id_timeframe_rules
        FOREIGN KEY (rule_id, timeframe) REFERENCES rules (id, timeframe) ON DELETE CASCADE
);

CREATE INDEX ix_ticker_rules_rule_id ON ticker_rules (rule_id);
```

- The model classes are `TickerRow`, `RuleRow` and `TickerRuleRow` (declarative, `Mapped[...]`),
  defined in `persistence/models.py` so `--autogenerate` sees them (spec 012 hand-off 1). The `Row`
  suffix keeps `RuleRow` from shadowing the domain's `Rule` in every module that handles both.
- `sqlite_autoincrement=True` on `tickers` and `rules` (D84, AC2): without it SQLite reuses the
  rowid of a deleted last row, and #13's signal identity is `(ticker_id, rule_id, …)`, so a reused
  id would let a new rule inherit the notification history of a deleted one.
- The `CHECK` expressions are built from the enums (`", ".join(f"'{m.value}'" for m in Timeframe)`),
  so adding a member changes the rendered DDL and fails T1 until a migration is written.
- Indexes: the unique constraints already index `(symbol, timeframe)` and `name`, and the primary
  key indexes `(ticker_id, rule_id)`. Only the reverse lookup of `tickers_for_rule` needs an index
  of its own. The tables hold tens of rows; no other index is added.
- `ticker_rules.created_at` records when the assignment was made; there is no `updated_at`, because
  an assignment has nothing to update (D84).

### 4. Column types (`persistence/types.py`)

`UtcDateTime` is unchanged and is the type of **every** timestamp column (spec 012 D74). Two
`TypeDecorator`s join it, both `cache_ok = True`:

```python
class TimeframeType(TypeDecorator[Timeframe]):
    """A ``Timeframe`` stored as its canonical code (``1h``, ``4h``, ``1d``)."""

    impl = String(2)
    cache_ok = True


class SideType(TypeDecorator[Side]):
    """A ``Side`` stored as ``BUY`` or ``SELL``."""

    impl = String(4)
    cache_ok = True
```

- Bind: a `Timeframe`/`Side` member becomes `member.value`; anything else raises `TypeError`, so a
  raw string cannot reach the column through the ORM.
- Result: the text goes through `Timeframe.parse` / `Side(...)`, so a value the database should
  never hold raises `UnknownTimeframeError` (a `ValueError`) at the boundary instead of flowing on
  as a string that compares unequal to every member.
- The `CHECK` constraints of §3 are the same rule on the database side, for a manual `sqlite3`
  session on the Pi.

### 5. Migration and transactional DDL

#### 5.1 Revision `0002`

`uv run alembic revision --autogenerate --rev-id 0002 -m "add the ticker, rule and assignment
tables"`, with `down_revision = "0001"` and a single head. Review the generated file before
committing:

- `--autogenerate` does not emit dialect keyword arguments: add `sqlite_autoincrement=True` to both
  `op.create_table` calls (AC2 fails otherwise);
- `downgrade` is real: `drop_index`, then `drop_table("ticker_rules")`, `drop_table("rules")`,
  `drop_table("tickers")`, in that order (foreign keys are enforced);
- no `ALTER` is needed here; any future one uses `op.batch_alter_table` (spec 012 D75);
- the revision must be `ruff format` clean (the `alembic.ini` post-write hook does it) and must not
  import what it does not use.

Rolling back to an image that only knows `0001` fails loudly with `CommandError` (spec 012 §14);
rolling the **schema** back with `downgrade` drops the configuration, so the recovery path for a
bad deploy stays the pre-deploy backup, never a downgrade on a live database.

#### 5.2 The recipe (hand-off 13, D88)

In `create_database_engine`, inside the existing `connect` listener and next to it:

```python
@event.listens_for(engine, "connect")
def _set_pragmas(connection: DBAPIConnection, _entry: ConnectionPoolEntry) -> None:
    # pysqlite opens a transaction only before the first DML statement, so DDL would run in
    # autocommit and a migration that fails halfway would leave part of its schema behind.
    # Handing transaction control to SQLAlchemy also makes SAVEPOINT work (#13).
    connection.isolation_level = None
    ...  # the four pragmas of spec 012 Design 4.2, unchanged


@event.listens_for(engine, "begin")
def _begin(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")
```

Measured on the prototype, with the pragmas in place:

| | Without the recipe | With the recipe |
|---|---|---|
| `in_transaction` after a `CREATE TABLE` inside `engine.begin()` | `False` | `True` |
| Tables left by a two-statement migration that raises | both | none |
| `journal_mode`, `foreign_keys`, `busy_timeout`, `synchronous` | applied | applied |
| A successful unit of work | commits | commits |

The pragmas keep working because the `connect` listener runs before any `BEGIN`; in particular
`PRAGMA journal_mode=WAL` is illegal inside a transaction, which is the failure mode to watch for
if the listener order is ever changed. `run_migrations` keeps `engine.begin()` unchanged.

### 6. Records and errors

#### 6.1 `persistence/records.py`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class StoredTicker:
    id: int
    symbol: str
    timeframe: Timeframe
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredRule:
    id: int
    rule: Rule
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @property
    def name(self) -> str:
        return self.rule.name


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    ticker_id: int
    rule_id: int
    timeframe: Timeframe
    created_at: datetime
```

Records are what leaves the session: immutable, hashable by value, free of ORM state and safe to
return from an `asyncio.to_thread` worker. `StoredRule.rule` is already parsed, so a caller never
has to touch `definition_json`.

#### 6.2 `persistence/errors.py`

```text
PersistenceError(Exception)
├── DuplicateTickerError(symbol, timeframe)
├── DuplicateRuleNameError(name)
├── UnknownTickerError(ticker_id)
├── UnknownRuleError(rule_id)
├── TimeframeMismatchError(ticker_timeframe, rule_timeframe)
├── AssignedTimeframeError(rule_id, assignment_count, stored, requested)
├── StoredRuleError(rule_id, problems)
└── SchemaMismatchError(current, expected)
```

- `PersistenceError` is **not** a `ValueError`, for the same reason as `MarketDataError` (spec 010):
  an `except ValueError` meant for programming errors must not swallow a storage failure. Argument
  errors keep coming from the domain (`normalize_ticker`, `Timeframe.parse`) as `ValueError` or
  `TypeError`.
- Every message is one English line built from ids, normalized symbols, timeframe codes and a
  `repr()`-escaped, truncated rule name. A rule **document** never appears in a message;
  `StoredRuleError` carries the `RuleProblem` kinds and paths of spec 006, which are already
  bounded and safe to log.
- `SchemaMismatchError` names the two revisions only, never a path.

### 7. Repositories (`persistence/repositories/`)

Protocols in `protocols.py`, implementations next to them. Every implementation takes the session
(and optionally a clock) and is cheap to build per unit of work.

```python
class TickerRepository(Protocol):
    def add(self, symbol: str, timeframe: Timeframe, *, enabled: bool = True) -> StoredTicker: ...

    def get(self, ticker_id: int) -> StoredTicker | None: ...

    def get_by_symbol(self, symbol: str, timeframe: Timeframe) -> StoredTicker | None: ...

    def list_all(self) -> tuple[StoredTicker, ...]: ...

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredTicker, ...]: ...

    def set_enabled(self, ticker_id: int, enabled: bool) -> StoredTicker: ...

    def delete(self, ticker_id: int) -> bool: ...


class RuleRepository(Protocol):
    def add(self, rule: Rule, *, enabled: bool = False) -> StoredRule: ...

    def get(self, rule_id: int) -> StoredRule | None: ...

    def get_by_name(self, name: str) -> StoredRule | None: ...

    def list_all(self) -> tuple[StoredRule, ...]: ...

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredRule, ...]: ...

    def replace(self, rule_id: int, rule: Rule, *, enabled: bool | None = None) -> StoredRule: ...

    def set_enabled(self, rule_id: int, enabled: bool) -> StoredRule: ...

    def delete(self, rule_id: int) -> bool: ...


class AssignmentRepository(Protocol):
    def assign(self, ticker_id: int, rule_id: int) -> Assignment: ...

    def unassign(self, ticker_id: int, rule_id: int) -> bool: ...

    def list_all(self) -> tuple[Assignment, ...]: ...

    def rules_for_ticker(
        self, ticker_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredRule, ...]: ...

    def tickers_for_rule(
        self, rule_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredTicker, ...]: ...
```

Behaviour that the tests pin:

- **Never commits** (AC11). `add` flushes to obtain the generated id and nothing more.
- **Symbols** go through `normalize_ticker` in `add` and `get_by_symbol`.
- **Duplicates** are detected with a `SELECT` before the insert, so the typed error leaves the
  session usable (AC15); the unique constraints remain the backstop and are tested with raw SQL.
- **`delete`** returns `True` when a row was deleted and `False` when there was none; the cascade is
  the database's (D86), never a Python loop.
- **`set_enabled`/`replace`** raise `UnknownTickerError`/`UnknownRuleError` for a missing id, and do
  not touch `updated_at` when nothing changes (AC17): the comparison is on the serialized document
  and the flag, not on object identity.
- **`replace`** with a different timeframe raises `AssignedTimeframeError` while assignments exist
  (D97).
- **`assign`** is idempotent (AC16) and pre-checks both timeframes (D86).
- **`enabled_only`** filters the returned side only: `rules_for_ticker(id, enabled_only=True)`
  returns the enabled rules assigned to that ticker whatever the ticker's own flag is.
- **Ordering** is explicit in SQL (D84, AC19); `rules_for_ticker` orders by rule name and
  `tickers_for_rule` by `(symbol, timeframe)`.
- A row whose `definition_json` no longer parses raises `StoredRuleError` (D93), including from
  `list_*`.

### 8. Rule documents

`persistence/repositories/rules.py` owns the two-way codec and is the only module in `persistence/`
allowed to import `trading_bot.domain.rules.*` (D99):

```python
def dump_rule_document(rule: Rule) -> str:
    """The canonical text stored in ``rules.definition_json`` (spec 006, decision D87)."""
    return json.dumps(dump_rule(rule), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def load_rule_document(rule_id: int, document: str) -> Rule:
    """Parse a stored document, raising ``StoredRuleError`` when it is no longer valid."""
```

- On write: `text = dump_rule_document(rule)`, then `load_rule_document(0, text) == rule` must hold
  (AC12); otherwise `StoredRuleError` is raised and nothing is written.
- On read: `load_rule_document` wraps `parse_rule`; a `RuleValidationError` becomes
  `StoredRuleError(rule_id, problems)` raised `from None`, so the traceback carries no document.
- The derived columns are `rule.name`, `rule.signal` and `rule.timeframe` (D90).

### 9. The CLI (`trading_bot/cli/`)

#### 9.1 Grammar

```text
python -m trading_bot.cli [--data-dir DIR] <group> <command> [options]

tickers      list
             add     <symbol> [--timeframe {1h,4h,1d}] [--disabled] [--dry-run]
             remove  <symbol> [--timeframe {1h,4h,1d}] [--force] [--dry-run]
             enable  <symbol> [--timeframe {1h,4h,1d}] [--dry-run]
             disable <symbol> [--timeframe {1h,4h,1d}] [--dry-run]

rules        list
             remove  <name> [--force] [--dry-run]
             enable  <name> [--dry-run]
             disable <name> [--dry-run]

assignments  list
             add     <symbol> <rule-name> [--timeframe {1h,4h,1d}] [--dry-run]
             remove  <symbol> <rule-name> [--timeframe {1h,4h,1d}] [--dry-run]

config       export  [--force] <file>
             import  [--on-conflict {skip,replace,fail}] [--dry-run] <file>
```

- Fourteen subcommands (D101): five for tickers, four for rules, three for assignments and the
  `config` pair. There is no `rules import|export` pair: `config` is the single bulk format (D102),
  and a rules-only file stays valid input for it.
- There is deliberately no `rules add`: a rule is a JSON document with nested conditions, not
  something to type as command-line arguments, and `parse_rule` must stay the single entry point
  (AC12). Rules are created by `config import` and removed by `rules remove`. Assignments have no
  `enable`/`disable` because they carry no flag.
- `<file>` is `-` for stdout (export) or stdin (import).
- `<symbol>` goes through `normalize_ticker`; `<rule-name>` is the rule's unique name (D85) and
  usually needs shell quoting, which the documented examples show.
- `--timeframe` defaults to `1d` (spec 004 hand-off; D109).
- `--data-dir` defaults to `Settings().data_dir`, resolved **after** parsing, so `--help` needs no
  environment.
- `--on-conflict` defaults to `skip` (U2).
- `main(argv: Sequence[str], *, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int` is the
  testable entry point; `__main__.py` is `raise SystemExit(main(sys.argv[1:]))`.
- `main` calls `configure_logging(settings.log_level, settings.secret_values())` before anything
  else, so any library record is redacted (AC26).
- Every mutating command opens exactly one `Database.session()` (AC38); `--dry-run` rolls it back
  (D107).

#### 9.2 Exit codes

| Code | Meaning |
|------|---------|
| `0` | The command succeeded, including a no-op (`enable` on an already enabled row) and a clean `--dry-run` |
| `1` | The request was rejected and **nothing was written**: an unreadable, oversized or invalid file, an invalid or duplicated rule, a row that does not exist, a row that already exists, a timeframe mismatch, a refused conflict, or a removal that needs `--force` |
| `2` | Usage error (argparse) |
| `3` | The environment is unusable: the data directory or database cannot be opened, the schema is not at the head revision, or the output file cannot be written |

#### 9.3 File format

Every section is optional and an **absent** section means "leave it alone" (D102, D104), so a
rules-only file is a valid whole-configuration file, which is what `docs/examples/rules/` ships.

```json
{
  "version": 1,
  "tickers": [{"symbol": "AAPL", "timeframe": "1d", "enabled": true}],
  "rules": [
    {
      "enabled": false,
      "rule": {
        "name": "RSI oversold in uptrend",
        "signal": "BUY",
        "timeframe": "1d",
        "conditions": {
          "all": [
            {
              "left": {"indicator": "rsi", "params": {"length": 14}, "output": "value"},
              "op": "crosses_below",
              "right": {"value": 30.0}
            }
          ]
        },
        "cooldown_bars": 5
      }
    }
  ],
  "assignments": [{"symbol": "AAPL", "timeframe": "1d", "rule": "RSI oversold in uptrend"}]
}
```

- The envelope accepts exactly `version`, `tickers`, `rules` and `assignments`. A ticker item
  accepts exactly `symbol`, `timeframe` (optional, default `1d`) and `enabled` (optional, default
  `true`); a rule item exactly `enabled` (optional, default `false`) and `rule`; an assignment item
  exactly `symbol`, `timeframe` (optional, default `1d`) and `rule`. Any other key, a wrong type or
  `version != 1` is rejected with a path (`assignments[2].rule`) and a one-line English message.
- **No database id appears anywhere** (D103): items are addressed by natural key, so a file is
  portable between databases.
- `rule` goes to `parse_rule` unchanged: the CLI never builds a `Rule` any other way, never uses
  `eval` and never trusts the file.
- Duplicates inside one file — two tickers with the same `(symbol, timeframe)`, two rules with the
  same name, two identical assignments — are rejected before any write.
- Export writes `version` and then only the sections that have items, tickers sorted by
  `(symbol, timeframe)`, rules by name and assignments by `(symbol, timeframe, rule)`, with
  `indent=2`, `ensure_ascii=False`, `\n` line endings and a trailing newline.

#### 9.4 Caps

`MAX_IMPORT_BYTES = 1_048_576` and `MAX_IMPORT_ITEMS = 500` over the three sections together, both
checked before parsing: spec 006's 64 KiB cap protects a document only when it is parsed from text,
and the CLI hands `parse_rule` an already decoded mapping.

#### 9.5 Ticker and rule commands

| Command | Behaviour |
|---------|-----------|
| `tickers add` | `TickerRepository.add(symbol, timeframe, enabled=not --disabled)`; an existing `(symbol, timeframe)` exits `1` naming both (`DuplicateTickerError`); a malformed symbol exits `1` with the domain's message |
| `tickers remove` | Refuses with exit `1` while assignments exist, naming the count, unless `--force` (D108); a missing row exits `1` |
| `tickers enable` / `disable` | `set_enabled`; a row already in that state is reported `unchanged` and exits `0` |
| `tickers list` | One line per ticker ordered by `(symbol, timeframe)`: symbol, timeframe, state, assignment count. No output when there is none |
| `rules enable` / `disable` / `remove` | By unique name; `remove` follows the same `--force` rule as tickers, because removing a rule removes its assignments too |
| `rules list` | One line per rule ordered by name: name, timeframe, signal, state, assignment count. The rule **document** is never printed; `config export` is the way to read it |

Rule names are echoed `repr()`-escaped and truncated, as spec 006's bounds already guarantee they
are printable and at most 80 characters.

#### 9.6 Assignment commands and the timeframe message

`assignments add` resolves both ends, then calls `AssignmentRepository.assign`. D86 makes a
mismatch impossible in the database; the CLI turns it into a readable line before the write:

```text
error: rule 'RSI oversold in uptrend' is evaluated on 1d, but ticker AAPL is tracked on 1h
```

Inside `config import` the same message is prefixed with the item path
(`assignments[2]: rule '…' is evaluated on …`). A missing ticker or rule names the one that is
missing, never both at once, and an `IntegrityError` traceback never reaches the operator.
`assignments add` twice reports `unchanged` and exits `0` (D105); `assignments remove` on a missing
assignment exits `1`. `assignments list` prints one line per assignment ordered by
`(symbol, timeframe, rule)`.

#### 9.7 `config import` and `config export`

Import is one pass of resolution and then one transaction (D106):

1. read and decode, apply the caps of §9.4;
2. check the envelope and every item's shape;
3. `parse_rule` every rule document;
4. resolve every assignment against the **file and** the database, checking both timeframes;
5. plan each item as `create`, `replace`, `skip` or `unchanged` against `--on-conflict`;
6. apply in one `Database.session()`, in the order tickers → rules → assignments;
7. report, then commit — or roll back when `--dry-run` (D107).

Any rejection in steps 1 to 5 exits `1` with nothing written. `--on-conflict fail` turns the first
existing ticker or rule into such a rejection; an existing assignment is `unchanged` under every
policy (D105). Import never deletes (D104).

Export reads the three tables in one session and writes the envelope of §9.3.

#### 9.8 Reporting

One line per item plus one summary, on stdout; nothing is coloured and nothing is localized:

```text
created ticker AAPL 1d (enabled)
created rule 'RSI oversold in uptrend' (id 1, 1d, BUY, disabled)
skipped rule 'MACD bearish crossover': a rule with that name already exists (id 2)
created assignment AAPL 1d -> 'RSI oversold in uptrend'
imported <file>: 1 ticker, 2 rules, 1 assignment (3 created, 0 replaced, 0 unchanged, 1 skipped)
```

A dry run uses the conditional form and no id, so nothing suggests a row that will not exist:

```text
dry run: would create ticker AAPL 1d (enabled)
dry run: nothing was written
```

The verbs are a closed vocabulary: `created`, `replaced` (a `config import` conflict resolved in
favour of the file), `enabled`, `disabled`, `skipped`, `removed` and `unchanged`, each with its
`would …` form. The state suffix is dropped when the verb already names it (`enabled rule 'X'
(id 1, 1d, BUY)`) and kept for a no-op (`unchanged rule 'X' (id 1, 1d, BUY, enabled)`).

Errors go to stderr as `error: <message>`, with the item path when there is one. The only path ever
echoed is the one the operator typed; the resolved database path is never printed. Nothing else
reaches stderr: `main` lowers the `alembic` logger to `WARNING` after `configure_logging`, because
Alembic's records describe a migration and this process only reads the stored revision (D95) —
without it every command would print its plugin and context chatter, including a line claiming
non-transactional DDL, which D88 made false.

#### 9.9 Opening the database

```python
def connect_database(data_dir: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Database:
    """Open an already-migrated database, applying no migration (decision D95)."""
```

It raises `SchemaMismatchError(current=None, …)` when the database file does not exist, **before**
building the engine, so the CLI never creates an empty database on a machine where the application
has never run. Otherwise it builds the engine through `create_database_engine`, compares
`current_revision(engine)` with `head_revision()`, disposes the engine and raises
`SchemaMismatchError` on any difference, and returns a `Database` when they agree. The CLI maps
that error to exit code `3` and a message telling the operator to start the application first; the
message names the two revisions, never a path.

Running the CLI while the app is running is supported: WAL plus the busy timeout cover a second
writer, and the import is one short transaction.

### 10. Example rules (`docs/examples/rules/`)

One `config import` file per example, named with a slug matching its rule, plus `README.md`. The
set confirmed by the user (U1):

| File | Rule | Signal | Timeframe | Conditions |
|------|------|--------|-----------|------------|
| `rsi-oversold-in-uptrend.json` | RSI oversold in uptrend | `BUY` | `1d` | `rsi(14)` crosses below `30` **and** `close > sma(200)` |
| `macd-bearish-crossover.json` | MACD bearish crossover | `SELL` | `1d` | `macd.macd` crosses below `macd.signal` **and** `close < ema(50)` |
| `bollinger-breakout-on-volume.json` | Bollinger breakout on volume | `BUY` | `1d` | `close` crosses above `bbands.upper` **and** `volume > volume_sma(20)` |

All three were checked against the M1 schema while writing this spec: they parse, they are already
in canonical form, and their `warmup()`/`stable_warmup()` are 200/200, 50/229 and 21/21, so each
one can fire on a ticker with a year of daily history.

Every file carries `"enabled": false` (D98, D100), and none carries a `tickers` or `assignments`
section: importing an example changes nothing the bot evaluates until the operator assigns it.
`README.md` states the format of §9.3, the invocation of §14, the four commands that take an
example from imported to live (`config import`, `tickers add`, `assignments add`, `rules enable`),
that the examples are disabled and must be reviewed and enabled deliberately, and the disclaimer
"Not financial advice." T11 imports each file, so an example that stops validating fails the gate.

### 11. Coverage of the migration scripts (hand-off 15, D89)

`pyproject.toml`:

```toml
[tool.coverage.run]
source = ["trading_bot", "src/trading_bot/persistence/migrations"]
branch = true
```

Measured on this branch's baseline: with only `trading_bot`, `env.py` and `0001_baseline.py` appear
in **no** report line (coverage matches a package name against the module name, and Alembic loads
them as `env_py` and `0001_baseline_py`); with the directory added, the whole unit suite reports
both at **100%** and the total is unchanged. Spec 012 §10.3's behavioural proofs are kept and
extended with one per revision, so the evidence does not depend on the tracer alone:

| File | Proof it ran | Test |
|------|--------------|------|
| `versions/0002_*.py` | after `upgrade head` the three tables exist with the §3 DDL; after `downgrade base` none of them does and `alembic_version` is empty | T2 |

No `omit` and no `exclude_lines` entry is added, here or anywhere.

### 12. Guards and typing

`tests/unit/test_persistence_guard.py` keeps its structure and gains per-file allowances (D99):

| File | Added allowance |
|------|-----------------|
| `models.py`, `types.py` | `trading_bot.domain.timeframe`, `trading_bot.domain.signals` |
| `errors.py` | the above plus `trading_bot.domain.rules.errors` (which imports nothing heavy) |
| `records.py`, `repositories/*.py` | the above plus `trading_bot.domain.rules.schema` |

`_FORBIDDEN_EVERYWHERE` is unchanged (`pandas`, `numpy`, `talib`, `yfinance`, `exchange_calendars`,
`fastapi`, `trading_bot.data` as direct imports). Two subprocess checks are added or extended:

- importing `trading_bot.persistence.models` (and `base`, `types`, `engine`, `database`) in a fresh
  interpreter pulls neither `pandas`, `numpy`, `talib` nor `alembic` into `sys.modules`, so the
  Telegram and API layers keep loading the schema without the analysis stack;
- `trading_bot.persistence.repositories.rules` **does** load pydantic and the indicator catalog;
  that cost is deliberate, named in D99 and confined to that module.

Two structural guards suggested by the #11 review are added: `create_engine` appears only in
`persistence/engine.py`, and `sqlalchemy.DateTime` only in `persistence/types.py`.

Typing: strict mypy, no `Any` and no `type: ignore` in the new `src/` files or in
`tests/fixtures/repositories.py`. `Mapped[...]`, `TypeDecorator[Timeframe]` and the `Protocol`s are
all expressible; the Protocol conformance of the three implementations is checked by mypy through
the typed factory of §15.1 and by the CLI's own annotations.

### 13. Image check

The existing `Dockerfile` migration check (spec 012 D82) gains two assertions in the same `RUN`:
after `upgrade head` on a throwaway database under `/tmp`, the tables `tickers`, `rules` and
`ticker_rules` exist; and `python -m trading_bot.cli --help` exits `0`. It still never touches
`/app/data`. `docker build` is unavailable locally (no daemon); the arm64 build in CI and the beta
deploy are the authoritative runs.

### 14. Documentation

`docs/ARCHITECTURE.md`:

1. `## Persistence` gains a `### Configuration tables and repositories` subsection: the three
   tables with their constraints, the D11 composite foreign key, the canonical document, the
   `Protocol`s and their synchronous contract, the frozen records, the injected clock, and one
   short Python example in the style of the other sections (it must pass `ruff format --check`).
2. A `### Configuration CLI` subsection: the fourteen subcommands, the envelope with its three
   optional sections, the exit codes, the all-or-nothing and merge-never-delete rules, `--dry-run`,
   the fact that the CLI never migrates, and a pointer to `docs/examples/rules/`.
3. `## Persisted data (draft)`: the three lines become the real schema and stop being a draft;
   `signals` and `bot_state` stay marked as #13.
4. The `persistence/` row of the layers table mentions the repositories and the CLI.
5. The `## Rule model` bullet that says "#12 rejects an assignment whose timeframes differ" becomes
   a statement of fact pointing at this spec.

`docs/DEPLOYMENT.md`, in `## Operations`, a short block with the placeholder style already in use,
plus one line saying that the export is a readable companion to the binary pre-deploy backup and
that an import merges and never deletes (D104):

```sh
# Load a configuration file from the workstation, checking it first
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import --dry-run - < config.json
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import - < config.json

# Day-to-day management
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli tickers add AAPL --timeframe 1d
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli assignments add AAPL "RSI oversold in uptrend"
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli rules enable "RSI oversold in uptrend"

# Readable backup of the whole configuration
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli config export -
```

No host, user, IP or absolute infrastructure path appears; `/app/data` is already documented. The
examples use `-` for stdin and stdout so no file has to be copied into the container.

### 15. Test fixtures

#### 15.1 `tests/fixtures/repositories.py` (reused by #13, #14, #19, #22)

```python
@dataclass(frozen=True, slots=True)
class Repositories:
    tickers: TickerRepository
    rules: RuleRepository
    assignments: AssignmentRepository


def repositories(session: Session, *, clock: Clock | None = None) -> Repositories: ...


def fixed_clock(start: datetime, step: timedelta = timedelta(seconds=1)) -> Clock: ...


def sample_rule(name: str = "Sample rule", **overrides: JsonValue) -> Rule: ...
```

The annotations are the `Protocol`s, so mypy proves that each implementation conforms (mypy covers
`tests/fixtures`). `fixed_clock` returns a deterministic sequence built from literal instants: no
test reads the wall clock. `sample_rule` builds on `tests/fixtures/rules.py` instead of adding new
payloads.

Every database in the tests comes from `tests/fixtures/database.py` (spec 012 AC19, hand-off 8); no
second fixture, engine or base is added.

## Test plan

Every test is a unit test without network (the D60 guard stays autouse), without the wall clock
(instants are literals and clocks are injected) and without writing outside `tmp_path`. Mandatory
template cases:

- **Anti look-ahead: not applicable.** This feature adds no indicator, no rule evaluation and no
  per-candle computation; it stores a rule document and never reads a candle. The related
  obligations it does carry are tested instead: a stored document re-parses to an equal rule (T6,
  T13) and a stored instant round-trips exactly (T13), so neither signal identity nor rule meaning
  moves through storage.
- **Idempotency (rule 5's analogue for configuration):** T12 is dedicated to it — importing the same
  file twice changes no row, no id and no `updated_at`; `assign` twice leaves one row; a `replace`
  or `set_enabled` that changes nothing does not move `updated_at`; export → import → export is
  byte-identical. Signal-level idempotency stays #13's, with the hand-off below.
- **Authorization: not applicable** (no Telegram, API or dashboard surface is added). The analogue
  is tested: the CLI refuses to run against a schema that is not at the head revision (T10), never
  migrates it, and writes only the file it was given (T14).
- **Secret redaction (config and logs):** T14 populates every secret setting with values built at
  runtime (`"123456789" + ":" + "x" * 35`) and asserts that no CLI output line and no log record of
  a full import and export contains any of them; that no new `TB_*` exists; and that no message
  contains a rule document or the resolved database path.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 models and DDL | unit | Reflection of the three tables: columns, types, nullability, unique constraints, check constraints, foreign keys with `ondelete`, index names; the `sqlite_master` DDL matches §3 with whitespace collapsed and contains `AUTOINCREMENT`; ids are not reused after a delete; `Base.metadata.tables` holds exactly three names | AC1–AC4 | developer |
| T2 revision 0002 | unit | `upgrade head` on an empty temporary database reaches `0002` and creates the three tables; a second run is a no-op with the "already" record; `downgrade base` drops all three and empties `alembic_version`; re-upgrade returns to head; single head, `^[0-9]{4}$`, `down_revision == "0001"` | AC7, AC29 | developer |
| T3 transactional DDL | unit | `in_transaction` inside `engine.begin()` after a `CREATE TABLE`; a throwaway Alembic script directory under `tmp_path` whose revision creates a table and then raises leaves no table and no stamp; the four pragmas on the first connection, from a second thread and after `dispose()`; a `begin_nested()` savepoint rolls back alone | AC8 | developer |
| T4 ticker repository | unit | `add` (defaults, normalization, both timeframes of one symbol), `get`, `get_by_symbol`, `list_all` ordering, `list_enabled` with and without a timeframe, `set_enabled` (including the no-op), `delete` returning `True`/`False`, `DuplicateTickerError` before any flush, a malformed symbol raising `ValueError` and writing nothing, the cascade to `ticker_rules`, and that a failed unit of work stores nothing | AC6, AC11, AC15, AC18, AC19 | developer |
| T5 rule repository | unit | `add` (disabled by default, derived columns, both timestamps from the injected clock), `get`, `get_by_name`, ordering, `list_enabled(timeframe)`, `replace` (document and flag, `updated_at` moves only on a real change), `set_enabled`, `delete` and its cascade, `DuplicateRuleNameError`, `UnknownRuleError`, `AssignedTimeframeError` | AC9, AC13, AC15, AC17, AC19 | developer |
| T6 documents | unit | For every `valid_payloads()` entry: the stored text equals the §8 serialization and re-parses equal; the re-parse guard rejects a tampered serialization without writing; a document written with raw SQL that no longer parses raises `StoredRuleError` naming the id, with no document in the message or the traceback | AC12–AC14 | developer |
| T7 assignments and D11 | unit | `assign` with equal timeframes; `TimeframeMismatchError` with both codes; the database backstop through raw SQL (`IntegrityError`) for an insert and for an `UPDATE` of either parent's timeframe; `UnknownTickerError`/`UnknownRuleError`; `assign` twice is idempotent; `unassign`; `rules_for_ticker`/`tickers_for_rule` with and without `enabled_only`, and their order; both delete paths | AC5, AC6, AC16 | developer |
| T8 `config export` | unit | Envelope shape and key order; the three sections and their orderings; absent sections when a table is empty; canonical documents and `enabled` flags; assignments by natural key with no id anywhere; UTF-8 with `\n` and a trailing newline; `-` to stdout; refusal to overwrite without `--force`; an unwritable destination exits `3`; an empty database exports `version` only | AC21, AC24 | developer |
| T9 `config import` | unit | A file with all three sections applied in order in one transaction; created, replaced, unchanged and skipped reporting for each `--on-conflict` value; an assignment resolved against the file and against the database; the summary line; `--dry-run` writes nothing, rolls back and exits `0`; an invalid item anywhere leaves the database untouched and exits `1` with its path; duplicates inside the file; an assignment naming a missing ticker or rule, and one whose timeframes differ; a rules-only file (the examples) and a tickers-only file; envelope rejections (`version`, unknown keys, wrong types, not an object); the byte and item caps; stdin; import never deletes | AC12, AC21, AC23, AC25 | developer |
| T10 CLI wiring | unit | `python -m trading_bot.cli` in a subprocess; `--help` for the program and for all fourteen subcommands with no database and no environment; unknown option, unknown group and unknown subcommand exit `2`; `--data-dir` honoured; an unmigrated or absent database exits `3` with an English message, leaves `alembic_version` untouched and creates no file; a data directory that is a file exits `3`; every mutating command opens exactly one session | AC20–AC22, AC38 | developer |
| T11 examples | unit | Every file under `docs/examples/rules/` is a valid envelope, parses, carries `"enabled": false`, has no `tickers` or `assignments` section, has a name unique across the directory and a file name matching its slug; importing each into a temporary database creates the rules disabled and no ticker; `README.md` exists and contains the disclaimer | AC27 | developer |
| T12 idempotency | unit | Importing the same whole-configuration file twice with `skip` and with `replace`: identical rows, ids and `updated_at` in all three tables, and the expected per-item report on the second run; `assignments add` twice; a no-op `replace`, `enable` and `disable`; export → import into an empty database → export is byte-identical, assignments included | AC23, AC24, AC35 | tester |
| T13 properties | unit (`@given`) | Over drawn rule payloads (`tests/fixtures/rule_strategies.py`) and symbols: store → load → `dump_rule` is a fixed point; the parsed rule equals the original; timestamps round-trip exactly across zones and compare only after normalizing to UTC (spec 012 hand-off 14); `list_all` order equals Python's sort | AC13, AC17, AC19 | tester |
| T14 secrets, paths and language | unit | Secrets built at runtime are absent from every CLI line and log record of a session that exercises all fourteen subcommands; no new `TB_*`; no rule document, resolved database path or absolute path in any message; the session creates exactly the database file and the file given on the command line | AC25, AC26 | tester |
| T15 adversarial | unit | A file that is not JSON, invalid UTF-8, an oversized file, 501 items, a name of 80 and of 81 characters, names differing only in case and in Unicode normalization, a rule name that needs shell quoting, a symbol with whitespace and lower case, `NaN` in a document, an assignments section repeating one pair, a directory given as the import file, an unwritable export path (skipped where the mode cannot be enforced), two writers with `busy_timeout_ms=50` (error class only, never elapsed time), a session reused after an error, records rejecting attribute assignment | AC15, AC21, AC25 | tester |
| T16 guards | unit | The per-file allowlist, exercised against synthetic snippets first so a green result is not vacuous; the fresh-interpreter import checks of §12; `create_engine` only in `engine.py` and `sqlalchemy.DateTime` only in `types.py`; `domain/` does not import `persistence` | AC28 | developer |
| T17 ticker commands | unit | `tickers add` (defaults, `--disabled`, normalization, an existing pair, a malformed symbol), `remove` (missing row, with and without assignments, `--force` and its report), `enable`/`disable` including the no-op, `list` empty and populated with its literal lines and order; every exit code; `--dry-run` on each of them leaves the database and `sqlite_sequence` unchanged | AC21, AC33, AC36, AC37 | developer |
| T18 rule commands | unit | `rules enable`/`disable`/`remove`/`list` by name: the literal lines and order, the assignment count, the `--force` rule, a missing name, a name needing quoting, and that no line ever contains the rule document; `--dry-run` on each | AC21, AC34, AC36, AC37 | developer |
| T19 assignment commands | unit | `assignments add` (both timeframes equal, idempotent second run, missing ticker, missing rule, timeframe mismatch with the literal §9.6 message), `remove` (present and missing), `list` order and lines; `--dry-run` on each; no `IntegrityError` text reaches the operator | AC21, AC35, AC37 | developer |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC30 |
| V2 coverage | `uv run pytest tests/unit --cov --cov-report=term-missing`: the new `src/` modules at 100%, `migrations/env.py` and both revisions listed and at 100%, no regression elsewhere; `git diff origin/main -- pyproject.toml` shows only the added `source` entry | AC29, AC30 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 15 s and none exceeds 2 s. Reported, never asserted | AC30 |
| V4 typing | `uv run mypy`; `git grep -n "Any"` and `git grep -n "type: ignore"` over the new `src/` files and `tests/fixtures/repositories.py` empty | AC30 |
| V5 dependencies | `git diff origin/main -- pyproject.toml uv.lock`: no dependency added or changed | — |
| V6 schema | The reflected schema and the `sqlite_master` DDL pasted into the PR (three tables, no host or path); `upgrade head` and `downgrade base` run twice in a row on a temporary database | AC1–AC7 |
| V7 image | Local `docker build` **BLOCKED** (no daemon). Local substitute: run the §13 check payload with `uv run python -c …`. Authoritative: the PR's `Docker build (arm64)` job and the beta deploy, verified by the lead. Report as BLOCKED with this justification, never as PASS | — |
| V8 CLI walkthrough | On a database under a temporary directory, in one pasted transcript: `config import docs/examples/rules/<file>.json`, `tickers add AAPL`, `assignments add AAPL "<rule>"`, `rules enable "<rule>"`, `rules list`, `assignments list`, `config export -`, then `tickers remove AAPL` (refused) and with `--force`. Exit codes reported for each; no path outside the temporary directory appears | AC20, AC24, AC27, AC33–AC36 |
| V9 no stray database | The session-end guard of spec 012 AC20 is green and `git status --porcelain --ignored` shows no `*.db`, `*.db-wal` or `*.db-shm` in the working tree after the suite | — |
| V10 docs | §14 placement and content; `uv run ruff format --check` on the Python block of the new subsections; `docs/examples/rules/README.md` carries the disclaimer | AC31 |
| V11 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC32 |
| V12 secrets and language | `python scripts/secret_scan.py --history`; an English-only review of the whole diff, including example rule names, CLI messages and test data; no host, user, IP or absolute infrastructure path anywhere | — |
| V13 dry run | For one command of each group and for `config import`: run with `--dry-run`, then compare a full dump of the three tables plus `sqlite_sequence` before and after. Reported as identical, with the command list | AC37 |

**Testing rules for this feature:**

- no wall-clock or elapsed-time assertions: clocks are injected and the busy-timeout case asserts
  the error class only;
- no platform-dependent expectations: files are read and written as UTF-8 with `newline="\n"`,
  comparisons use `PurePosixPath`/`as_posix()`, and the unwritable-path case is skipped where the
  mode cannot be enforced;
- literal expectations written out, never recomputed with the code under test (constraint names,
  DDL fragments, exit codes, report lines and revision identifiers are literals);
- every database lives under `tmp_path` and comes from `tests/fixtures/database.py`;
- rule payloads come from `tests/fixtures/rules.py`; no new hand-written rule JSON in test modules
  beyond what an envelope needs.

**TDD order suggested to the developer:**

1. T3 (the transactional-DDL recipe) — it changes the engine every later test uses;
2. T1 → T2 (models, then the revision) with the golden DDL;
3. T4 → T5 → T6 (tickers, rules, documents);
4. T7 (assignments and D11), including the raw-SQL backstops;
5. T10 (the parser, dispatch and the `connect_database` refusal), then T17 → T18 → T19: the
   single-row commands are the smallest surface over the repositories and give `--dry-run` and the
   report vocabulary before the bulk path needs them;
6. T8 → T9 (`config export`, then `config import`, which reuses everything above);
7. T11 (examples), T16 (guards), the `Dockerfile` check, the coverage entry and the docs.

## Risks and security

- **The first destructive downgrade.** `downgrade` drops the three tables and with them the
  configuration. It exists because spec 012 requires a real one and because the tests need it, not
  as an operational procedure: the recovery path for a bad deploy stays the pre-deploy backup in
  `attempts/<id>/database.sqlite3`. From here on, a migration that drops or renames follows
  expand/contract if a rollback has to keep working.
- **Rolling an image back after `0002`.** The older image cannot recognize the revision and fails
  loudly with `CommandError` (spec 012 §14), so it never writes against an unknown schema.
- **The recipe changes transaction control for every connection** (D88). It is the documented
  SQLAlchemy recipe for pysqlite and was measured here: the pragmas still apply, a unit of work
  still commits, savepoints now work and a failed multi-statement migration leaves nothing behind.
  The failure mode to watch is a future `PRAGMA journal_mode` moved inside a transaction, which
  SQLite rejects; T3 keeps the pragma assertions.
- **Identifier reuse** would let a new rule inherit a deleted one's notification history through
  #13's signal key. `sqlite_autoincrement=True` prevents it and AC2 pins it; #13 must not drop it.
- **Deleting a ticker or a rule** removes its assignments (D86). #13 decides what happens to the
  signal history and inherits the note that a ticker deleted and re-created inside the same candle
  could be notified again for that candle.
- **Rule names are compared by code point** (D85): two names that differ only in Unicode
  normalization form are two rules, and the user sees two identical-looking entries. Accepted and
  documented; normalizing would rewrite user text and break the byte stability of the stored
  document.
- **A stored document that stops parsing** stops the bot loudly (D93) instead of silently dropping
  a rule. The trigger is a catalog change, which spec 006 already says requires migrating stored
  rules; this feature makes the consequence visible.
- **The CLI reads an operator-supplied file.** It is bounded before parsing (§9.4), goes through
  `parse_rule` as the only entry point, uses no `eval` and no dynamic import, writes only to the
  path it was given, and prints no resolved path and no secret. A file is not a trusted input just
  because a human copied it.
- **Two processes on one database.** The CLI runs beside the app with WAL and the busy timeout; it
  never migrates (D95) and every command is one short transaction. Rule 7 is untouched: the
  scheduler and the Telegram poller still live in one process.
- **A configuration change takes effect on the next run, not immediately.** The CLI writes to the
  database while the application holds its own sessions; whether a running engine notices a new
  ticker within the current candle is #14's business (it reads its configuration per run). Nothing
  here pushes a change into the running process, and the operator sees no confirmation from the app.
  Worth a line in `docs/DEPLOYMENT.md` and worth pinning when #14 lands.
- **`config import` merges and never deletes** (D104). An operator who edits an exported file,
  deletes a section and re-imports it will find the removed items still there. The README and
  `docs/DEPLOYMENT.md` say so, `config export` after an import shows the real state, and the
  explicit `remove` commands are the way to delete.
- **`--force` on a removal is the only destructive path in the CLI.** It is never implied, it names
  the assignments it will take with it, and `--dry-run` shows the same report without writing
  (D107, D108). Deleting a rule after #13 lands will also delete its signal history, which is why
  hand-off item 1 asks #13 to decide that explicitly.
- **Public repository.** The example rules contain no market data, no host, user or path; their
  `README.md` carries "Not financial advice." The spec, the code and the PR text use placeholders.
- **Supply chain.** No dependency is added or changed.
- **Unbreakable rules.** All preserved:
  - signal-only: the schema has no order, broker, account or credential concept, and the CLI can
    only read and write configuration;
  - pure `domain/`: `persistence` imports `domain`, never the reverse; the purity guard is
    untouched and the isolation guard gains only the per-file allowances of D99;
  - closed candles only: unaffected (no candle logic);
  - idempotency: T12 for configuration; the signal constraint stays #13's, with the hand-off below;
  - UTC: every timestamp column is `UtcDateTime` and every comparison normalizes first;
  - single worker: nothing new runs in the application process;
  - no `eval`: rules are JSON validated by the M1 schema, and the CLI never builds a rule any other
    way;
  - English only.

### Hand-off list for #13 (signals and `bot_state`)

1. `signals` references `tickers(id)` and `rules(id)`; choose and test the `ondelete` behaviour of
   both, and record the decision. Deleting a ticker or a rule must not leave orphan rows and must
   not be able to resurrect a notification (see the identifier-reuse note). The CLI already has
   `tickers remove` and `rules remove` behind `--force` (D108), so #13 must also decide what their
   report says about the signals that go with the row, and extend the `--force` message if history
   is deleted.
2. **Do not remove `sqlite_autoincrement=True`** from `tickers` or `rules`: the signal key depends
   on ids never being reused. Spec 004 §9 maps `rule_id` to `str(rules.id)`; confirm that mapping
   in #13, because changing it later re-notifies every stored signal.
3. The unique constraint `(ticker_id, rule_id, timeframe, candle_close_ts)` follows the naming
   convention of spec 012 §5. Catch its `IntegrityError` inside a `session.begin_nested()`
   savepoint: D88 makes savepoints work, and without one the failed flush would end the whole unit
   of work.
4. Revision `0003` with `down_revision = "0002"`, a real `downgrade`, a single head and tests in
   both directions. The transactional-DDL recipe is already in place, and the coverage `source`
   entry of D89 already covers new revisions, so an untested branch in `0003` shows as missing
   lines.
5. Reuse `persistence/clock.py` (D92), `persistence/records.py`, `persistence/errors.py` (extend
   `PersistenceError`) and the repository shape of D91: a `Session`, no commit, frozen records.
   Store `candle_close_ts` exactly as `Evaluation.candle_close_ts` gives it (spec 012 hand-off 3).
6. `bot_state` is a key/value table; its timestamps use `UtcDateTime` and its writes use the
   injected clock. Keep the keys an explicit, closed vocabulary rather than free text.
7. #14 must not swallow `StoredRuleError` (D93): a rule that no longer parses is an operational
   failure to surface, not a rule to skip.
8. Consumers in the event loop wrap every unit of work in `asyncio.to_thread` (D69) and never share
   a `Session` across threads or across an `await`.
9. The CLI is a `python -m trading_bot.cli` package with one module per group (§1). If #13 adds an
   operational command (for example replaying or purging signals), it goes in a new module beside
   them, keeps the exit-code table of §9.2, `--dry-run` (D107) and the report vocabulary of §9.8,
   and never migrates the database (D95).
10. `config import|export` is the one bulk format (D102). A new section in the envelope is an
    additive change that keeps `version: 1`; a change to an existing section's shape is what the
    version field is for.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; M3 stacks #11 → #12 → #13.
- **D2 (2026-09-14):** synthetic data only in tests. No fixture here contains market data.

The four product questions this spec opened were answered on **2026-09-18**. Two of them widened
the scope beyond the text of issue #12, which is why they are recorded here in full: a later reader
comparing the issue with the delivered CLI must find the reason without archaeology.

- **U1 (2026-09-18) — example rules: the three proposed.** `RSI oversold in uptrend`,
  `MACD bearish crossover` and `Bollinger breakout on volume`, all `1d`, all disabled, with a
  `README.md` carrying "Not financial advice." (§10, D100, T11.)
- **U2 (2026-09-18) — import conflicts: `skip` by default,** with `replace` and `fail` available.
  Re-running an import is therefore a no-op and never overwrites a rule the user edited on the Pi.
  (D105, §9.7, T9, T12.)
- **U3 (2026-09-18) — the CLI covers rules, tickers and assignments**, not rules only. The reason
  given to the user and accepted by them: **M4 (engine and scheduler, #14–#16) lands before Telegram
  (#19)**, so with a rules-only CLI there would be a window in which the bot can evaluate rules but
  a ticker could only be created by hand-editing SQLite on the Raspberry Pi. The issue text
  (`rules import|export`) is therefore superseded, and the extra surface — ticker, rule and
  assignment commands, their exit codes, their conflict handling, the readable timeframe-mismatch
  error and the `--force` rule for removing a row that has assignments — is specified with the same
  depth as the bulk path. (D101, D107–D109, §9.1, §9.5, §9.6, AC33–AC38, T17–T19.)
- **U4 (2026-09-18) — export is the whole configuration:** tickers, rules and assignments in one
  file, usable as a readable configuration backup next to the binary one the deploy already takes.
  Import accepts that same envelope, so there is one format and one command pair (`config
  import|export`); sections are optional, so rules-only files — including every example — stay
  valid, and `version` stays `1` because no release has ever produced the narrow shape. Import
  merges and never deletes. (D102–D104, D106, §9.3, §9.7, AC23, AC24, T8, T9.)

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames, users or absolute infrastructure paths
- [ ] Tests cover the acceptance criteria and fail without the implementation
- [ ] An invalid rule cannot be persisted through any path, and no `eval` exists
- [ ] The D11 timeframe rule is enforced by the database, not only by the repository
- [ ] `upgrade head` and `downgrade base` tested on a temporary database; a single head; `0002`
      follows `0001`; the DDL matches §3 including `AUTOINCREMENT` and the check constraints
- [ ] The transactional-DDL recipe is in place with its three tests, and the pragmas still apply
- [ ] Repositories never commit, never share a session and return frozen records
- [ ] The CLI never migrates, never prints a secret or a resolved path, and writes only the file it
      was given
- [ ] Every mutating subcommand is one transaction, supports `--dry-run` and leaves the database
      byte-identical when it is used
- [ ] `config import` merges and never deletes; no database id appears in an exported file
- [ ] A timeframe mismatch and a removal that needs `--force` are readable English lines, never a
      raw `IntegrityError`
- [ ] Coverage reports both revisions and `env.py`; no `omit` and no `exclude_lines`
- [ ] No `Any`, no `type: ignore`; the isolation guard, the purity guard and the two new structural
      guards are green
- [ ] Scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
