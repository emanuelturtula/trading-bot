# 014 — Idempotent signals and bot state

- **Status:** approved (the five product questions are answered; see "User decisions",
  U1–U5, 2026-09-19)
- **Branch:** `feature/signal-idempotency` (third and last of the stacked M3 series
  #11 → #12 → #13), from `origin/main` at `d3c67d1` (v0.10.0)
- **Spec author:** tech-lead
- **Issue:** #13 (milestone M3 · Persistence)
- **Expected commit type:** `feat:`. The change adds the `signals` and `bot_state` tables in
  revision `0003`, the signal and bot-state repositories that M4–M6 write and read through, two
  read-only CLI commands, and changes how every `Database.session()` transaction begins
  (`BEGIN IMMEDIATE`, D110). No dependency and no `TB_*` variable is added. `feat` → minor bump.
  Suggested squash subject: `feat: add idempotent signal storage and the bot state (#<n>)`.

## Goal

Enforce `CLAUDE.md` rule 5 in the database, and give the engine a place for its runtime state:

- a `signals` table whose unique constraint on `(ticker_id, rule_id, timeframe, candle_close_ts)`
  makes a duplicate signal impossible, whatever thread, process, retry or restart writes it;
- an idempotent `record` that returns the stored signal and whether this call created it, and
  that neither fails nor duplicates when two callers record the same signal at the same time
  (issue AC2), proven by a deterministic test;
- the history queries M5 and M6 read (paginated, by ticker, rule and date range) and the "latest
  signal per ticker and rule" that the cooldown of #14 needs;
- `bot_state`: the global pause, the last heartbeat and the last run per timeframe, as a closed
  vocabulary of UTC instants;
- read-only `signals list` and `state show` commands, so an operator can see what the engine
  records in M4, before Telegram (M5) and the dashboard (M6) exist.

## Out of scope

- **Deciding and sending notifications.** Which triggered evaluations become signals (cooldown,
  pause), the Telegram message, its delivery retries and what to do with a signal whose delivery
  failed belong to #14, #17 and #19. This feature stores what they decide and hands them the
  contract of §2 and the hand-off list.
- **Scheduling.** When runs happen and how a missed close is caught up after a restart are #15's;
  `bot_state` only records what #15 reports.
- **Wiring.** Nothing is wired into `main.py` or the lifespan; #16 injects the repositories.
  `/health` is unchanged and touches no table.
- **Retention and pruning.** No signal is deleted by this feature except through the cascade of a
  ticker or rule removal (D113, D121, U2, U3).
- **A `pause`/`resume` command.** The CLI only reads the bot state; pausing is Telegram's `/pause`
  (#19) and F7's (U4).
- **API cursors, presentation and time zones.** The repository cursor is a typed value; encoding
  it in a URL is #22's. Converting instants to the display zone and showing the session date of a
  `1d` candle belong to #17, #23 and #25 (spec 004 §5).
- **Changes to** `domain/`, `data/`, `deploy/`, `scripts/`, `.github/`, `main.py`, `config.py`,
  `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`, `.dockerignore`, `CLAUDE.md` and
  `docs/ROADMAP.md` (the lead marks F3 completed after the merge, as #45 did for F1 and F2).
  `Dockerfile`, `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` change only as §13 and §14 state.

## Acceptance criteria

"Temporary database" means a migrated database under pytest's `tmp_path`, opened through
`tests/fixtures/database.py` (spec 012 AC19). "Session" means one from `Database.session()`.

### Schema and migration

- [ ] **AC1 (tables):** revision `0003` creates exactly `signals` and `bot_state` with the columns,
  types, nullability, constraint names and two indexes of §3, verified by reflection and by the
  `sqlite_master` DDL compared with whitespace collapsed, and creates nothing else.
  `Base.metadata.tables` holds exactly `tickers`, `rules`, `ticker_rules`, `signals` and
  `bot_state`.
- [ ] **AC2 (signal identity is unique, rule 5):** a raw `INSERT` that repeats the four columns of
  `uq_signals_ticker_id_rule_id_timeframe_candle_close_ts` raises `IntegrityError`; a row that
  differs in any one of them — `candle_close_ts` by one microsecond included — is accepted.
- [ ] **AC3 (identifiers are never reused):** the DDL of `signals`, `tickers` and `rules` contains
  `AUTOINCREMENT`; recording after deleting the signal with the highest id yields a new id, never
  the deleted one; spec 013 AC2 keeps passing unchanged.
- [ ] **AC4 (checks):** `CHECK (timeframe IN ('1h', '4h', '1d'))`, `CHECK (side IN ('BUY', 'SELL'))`
  and `CHECK (close_price > 0)` on `signals`, and the `bot_state` key check of §3, are built from
  `Timeframe`, `Side` and `StateKey`; a raw `INSERT` violating any of them raises `IntegrityError`.
- [ ] **AC5 (foreign keys and deletes, D113):** deleting a ticker deletes its signals, and
  deleting a rule deletes its signals, and nothing else (the signals of other tickers and rules, the
  assignments of other rows and `bot_state` are untouched). A raw `INSERT` naming a missing ticker
  or rule, or a timeframe that differs from its ticker's, raises `IntegrityError`. Changing a
  rule's timeframe through `RuleRepository.replace` while it has signals but no assignment
  succeeds, and those signals keep the timeframe they were recorded with.
- [ ] **AC6 (upgrade and downgrade):** `0003.down_revision == "0002"`, one head, identifiers match
  `^[0-9]{4}$`. `run_migrations` on an empty temporary database reaches `0003`; a second run is a
  no-op with the "already at revision" record; `downgrade 0002` drops exactly the two tables and
  leaves every configuration row intact; `downgrade base` leaves `alembic_version` present with
  zero rows; `upgrade head` afterwards returns to `0003`. The revision imports no `trading_bot`
  module (the existing guard) and reports 100% coverage (D89).

### Transactions and concurrency

- [ ] **AC7 (sessions begin `IMMEDIATE`, D110):** every transaction a session opens starts with
  `BEGIN IMMEDIATE`, emitted at its first statement; a session that executes nothing emits no
  `BEGIN` at all; `engine.connect()` and `engine.begin()` keep emitting `BEGIN`. After a session's
  first statement is a read, a separate `sqlite3` connection with `timeout=0` cannot start
  `BEGIN IMMEDIATE` (`database is locked`); after a read on `engine.connect()` it can.
  `create_session_factory(engine)` still returns a `sessionmaker[Session]` with
  `expire_on_commit=False`, and `Database.engine` is still the plain engine. Spec 012's WAL-reader
  test, the migration tests and spec 013's savepoint test pass unchanged.
- [ ] **AC8 (issue AC2 — simultaneous inserts, D123):** two units of work that record the same
  signal at the same time neither raise nor duplicate: the one that recorded first reports
  `is_new=True`, the other reports `is_new=False` with the **same** stored row (same id, same
  `created_at`), and the table holds one row for that key. It holds on one `Database` handle and
  on two handles over one file (the second opened with `connect_database`, as the CLI does), for
  a write-first unit of work and for one that reads `latest` before recording. The ordered
  harness described under "Concurrency" in the test plan proves it deterministically, and its
  control, with the second handle's busy timeout at `0`, fails with `database is locked`, which
  proves the two units of work really collide.
- [ ] **AC9 (the hazard is pinned):** on a temporary database, a connection from `engine.connect()`
  (deferred `BEGIN`) that reads, then writes after a session committed in between, raises
  `OperationalError` whose driver error has `sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"`. This
  characterization test is the reason for AC7 and fails if SQLite's behaviour ever changes.

### Signal repository

- [ ] **AC10 (records and ports):** `StoredSignal`, `RecordOutcome`, `SignalCursor`, `SignalPage`,
  `LastRun` and `BotState` are frozen, slotted, keyword-only dataclasses (§6); every instant they
  carry is aware and in UTC. `SignalRepository` and `BotStateRepository` are synchronous
  `Protocol`s with the signatures of §7 and §9; `SqlSignalRepository` and `SqlBotStateRepository`
  implement them over a `Session`, and mypy proves it through the Protocol-typed factory of §15.
  No method calls `commit`, `rollback` or `close`: when the session block raises, nothing they
  wrote is visible afterwards.
- [ ] **AC11 (issue AC1 — idempotent insert, D112):** recording the same signal twice returns
  `is_new=True` and then `is_new=False` with the same stored row, and one row exists — in one
  session, in two consecutive sessions, and across a restart (the `Database` disposed and
  reopened on the same directory). The first write wins: a second recording with the same key and
  a different `side`, `close_price` or `indicator_values` returns the first row unchanged. After an
  `is_new=False` outcome the same session records another signal and commits both.
- [ ] **AC12 (key mapping, D114):** `record` resolves the ticker by `(signal.ticker,
  signal.timeframe)` and the rule by `int(signal.rule_id)`; stores `candle_close_ts` exactly as the
  signal carries it (an instant off every candle grid, with microseconds, round-trips unchanged);
  raises `ValueError` for a `rule_id` that is not the canonical decimal text of an integer in
  `[1, 2**63 - 1]` (`"042"`, `"+42"`, `"0"`, `"rule-7"`, 64 nines), `UntrackedTickerError` for an
  unknown `(symbol, timeframe)`, `UnknownRuleError` for an unknown rule and
  `TimeframeMismatchError` when the rule's timeframe differs from the signal's. Each of these
  writes nothing and leaves the session usable.
- [ ] **AC13 (payload round trip, D119):** the stored `Signal` equals the recorded one field by
  field: `close_price` and every indicator value bit for bit (`5e-324`, `-0.0` and
  `1.7976931348623157e308` included), with the indicator keys in their original order;
  `created_at` comes from the injected clock; the `indicator_values_json` text of a known signal
  equals a literal written in the test.
- [ ] **AC14 (delivery state, D115):** a recorded signal has `notified_at is None`;
  `mark_notified(id)` sets it from the injected clock and returns the updated record; a second call
  keeps the first instant; an unknown id raises `UnknownSignalError`; no other column changes.
- [ ] **AC15 (latest, D117):** `latest(ticker_id, rule_id)` returns the pair's signal with the
  greatest `candle_close_ts` whatever the insertion order, ignores every other pair, honours
  `notified_only`, and returns `None` when there is none.
- [ ] **AC16 (history, D116):** `history(...)` orders by `(candle_close_ts DESC, id DESC)`;
  filters by `ticker_id`, `rule_id`, `timeframe`, `notified` and the half-open range
  `[since, until)` on `candle_close_ts`, alone and combined; iterating with
  `before=page.next_cursor` returns every matching row exactly once for page sizes 1, 2, 3 and 50,
  and `next_cursor` is `None` on the last page, including a page that ends exactly on the last
  row. A row recorded between two pages appears later in the iteration only if it sorts after the
  cursor, and never twice. `limit` defaults to 50; `0`, `501`, `True` and a non-`int` are rejected
  (`ValueError`/`TypeError`); a naive `since`, `until` or cursor instant and `since > until` raise
  `ValueError`.
- [ ] **AC17 (count):** `count(ticker_id=..., rule_id=...)` equals the number of matching rows, `0`
  for an unknown id.
- [ ] **AC18 (a corrupt row fails loudly):** a row altered with raw SQL so that its
  `indicator_values_json` is not a JSON object of finite numbers makes every read that returns it
  raise `StoredSignalError` naming the signal id and the kind, never the stored text, raised
  `from None`; it is never skipped silently.

### Bot state

- [ ] **AC19 (closed vocabulary, D118):** `StateKey`'s values are exactly `paused_since`,
  `last_heartbeat`, `last_run.1h`, `last_run.4h` and `last_run.1d`; `StateKey.last_run(timeframe)`
  covers every `Timeframe` member; a raw `INSERT` with any other key raises `IntegrityError`.
- [ ] **AC20 (missing keys mean "never"):** on an empty table `load()` returns
  `BotState(paused_since=None, last_heartbeat=None, last_runs=())`, `paused` is `False` and
  `last_run(timeframe)` is `None` for every timeframe.
- [ ] **AC21 (pause):** `pause()` stores and returns the clock's instant; a second `pause()` returns
  the first instant and writes nothing; `resume()` returns `True` and removes the pause, and
  returns `False` when the bot was not paused.
- [ ] **AC22 (heartbeat and runs):** `record_heartbeat()` stores the clock's instant, last write
  wins (also when the clock moved backwards); `record_run(timeframe, scheduled_at)` is monotonic:
  an earlier or equal `scheduled_at` leaves the row and its `completed_at` untouched and returns
  the stored run, a later one replaces both; a naive `scheduled_at` raises `ValueError` and writes
  nothing; `last_runs` is ordered `1h`, `4h`, `1d`.

### CLI

- [ ] **AC23 (removal names the history, D122):** `tickers remove` and `rules remove` refuse with
  exit `1` while the row has assignments **or** signals, naming both counts with the literal
  messages of §10.2; with `--force` they succeed and report both; with `--dry-run` the database,
  the signals included, is byte-for-byte unchanged. A row with neither is removed without `--force`,
  as today, and the messages of a row without signals are exactly the ones spec 013 pinned.
- [ ] **AC24 (`signals list`, U4):** prints the newest signals first, one literal line per signal as
  §10.3 states, at most `--limit` (default 20, `1`–`500`, anything else exits `2`); `--ticker`,
  `--timeframe` and `--rule` filter as §10.3 states; an unknown ticker or rule exits `1` with the
  message the existing commands use; no signal prints nothing and exits `0`. It writes nothing.
- [ ] **AC25 (`state show`, U4):** prints the five literal lines of §10.4 for an empty table and for
  a fully populated one, and writes nothing.
- [ ] **AC26 (grammar and envelope):** the CLI has sixteen subcommands; `--help` works for each with
  no database and no environment; the `config` envelope is unchanged (no `signals` or
  `bot_state` section; `config export` of a database with signals is byte-identical to the export
  of the same configuration without them).
- [ ] **AC27 (no secret is printed):** spec 013 AC26 holds for all sixteen subcommands, with signals
  and bot state present in the database.

### Guards, docs and gate

- [ ] **AC28 (guards):** the persistence allowlist accepts the new files with the allowances of §12
  and nothing more; importing `trading_bot.persistence.repositories.signals`,
  `trading_bot.persistence.repositories.bot_state`, `trading_bot.persistence.signal_records` and
  `trading_bot.persistence.state` in a fresh interpreter loads none of `pandas`, `numpy`, `talib`,
  `pydantic` and `alembic`; `create_engine` appears only in `persistence/engine.py` and
  `sqlalchemy.DateTime` only in `persistence/types.py`; `domain/` still does not import
  `persistence`.
- [ ] **AC29 (typing and gate):** `uv run python scripts/check.py` is green; strict mypy passes with
  no `Any` and no `type: ignore` in the new and changed `src/` files and in
  `tests/fixtures/repositories.py` and `tests/fixtures/concurrency.py`; coverage does not regress
  and the new modules are at 100%.
- [ ] **AC30 (docs):** `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` change as §14 states;
  `## Persisted data` stops being a draft.
- [ ] **AC31 (scope):** the diff touches only the files of Design §1.

## Design

### 0. Decisions

Decisions D1–D109 are recorded in specs 003–013. This spec relies on D69 (synchronous
SQLAlchemy), D72 (pragmas and the busy timeout), D74 (`UtcDateTime`), D86 (composite timeframe
keys), D88 (transactional DDL and savepoints), D89 (coverage of revisions), D91 (repository
shape), D92 (injected clock), D93 (loud failures), D95 (the CLI never migrates), D97 (hard
deletes, immutable ticker timeframe), D99 (per-file allowances), D102 (one envelope) and D107–D109
(CLI conventions). The new decisions are D110–D123; the next free identifier after this spec is
**D124**. The user decisions of 2026-09-19 confirm D111 (U1), D113 and D122 (U2), D121 (U3),
D122 (U4) and D115 (U5).

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D110 | How a session's transaction begins | Every transaction of a session from `create_session_factory` begins with **`BEGIN IMMEDIATE`**, emitted lazily at its first statement. The factory binds its sessions to `engine.execution_options(trading_bot_begin_immediate=True)`, and the engine's existing `begin` listener emits `BEGIN IMMEDIATE` when that option is set and `BEGIN` otherwise, so `engine.connect()` and `engine.begin()` — the migrations, the CLI's revision check, spec 012's WAL reader — keep a deferred `BEGIN` | Measured (§5). In WAL mode a deferred transaction that reads and then writes cannot be rescued by the busy timeout: while another connection holds the write lock the upgrade fails at once with `SQLITE_BUSY` (SQLite skips the busy handler to avoid a deadlock), and once another connection has committed after the read it fails with `SQLITE_BUSY_SNAPSHOT`, even with the lock free. With 8 threads per key reading and then recording one signal, 217 of 280 losers failed with `database is locked` under `BEGIN` and none under `BEGIN IMMEDIATE`. #14 reads the cooldown and then records (D117), #12's repositories and CLI already read before they write, and `record` itself resolves the ticker and rule first (D114): the 5 s busy timeout spec 012 D72 relies on only works if the write lock is taken first. The existing suite (5 446 tests) passes with the change applied. **Rejected:** a write-first discipline (a convention that one added `SELECT` breaks, only under concurrency, where it is hardest to see); `BEGIN IMMEDIATE` for every connection (spec 012's WAL reader and the migration's revision read would take the write lock); an opt-in `session(write=True)` (a caller that picks wrongly gets the same latent failure, and a read through a session holds the lock for microseconds) |
| D111 | The `signals` row | The columns of §3: the identity (`ticker_id`, `rule_id`, `timeframe`, `candle_close_ts`), the payload a notification shows (`side`, `close_price`, `indicator_values_json`), `created_at` (when it was recorded, from the injected clock) and `notified_at` (when a notification was confirmed, `NULL` until then), with `AUTOINCREMENT` ids. **Not stored:** the rule name (read live from `rules.name`), the candle label (`candle_close_ts - timeframe.duration`, exact), a chart or candles. Two indexes besides the unique key: `(rule_id, candle_close_ts)` and `(candle_close_ts)` | `CLAUDE.md` lists what a Telegram message shows: ticker, timeframe, rule, close price, indicator values and a chart. The first three are the key and a join; the price and the values are exactly what `Evaluation` produces and could not be recomputed later without the same candles. `side` is stored because `rules.signal` can change through `replace`, and the history must say what was sent. A stored name would freeze a label the user may correct; the chart is rebuilt from market data by #17 and #23 (U1). Signal ids appear in cursors, API URLs and Telegram references, so a reused id would point a stale reference at another signal. The unique key already serves the ticker history, `latest` and the ticker cascade (prefix `ticker_id`); the rule index serves the rule history and the rule cascade, the time index the unfiltered newest-first page |
| D112 | Idempotent insert | `record(signal) -> RecordOutcome(stored, is_new)`. The `INSERT` runs inside `session.begin_nested()`; an `IntegrityError` rolls the savepoint back, the row with the same key is read and returned with `is_new=False`, and if no such row exists the error is re-raised. **The first write wins:** `record` never updates a stored row. `is_new` is `True` for exactly one call per key and is provisional until the caller's session commits | The unique constraint is the only arbiter that holds across threads, the CLI process, retries and restarts (spec 012 hand-off 5); a `SELECT` first would be a second, weaker arbiter. The savepoint keeps the caller's unit of work alive after the violation (D88; #12's tester showed that a failed flush otherwise poisons the session). Re-reading the key, instead of parsing the error text, tells the unique violation apart from any other `IntegrityError` without depending on SQLite's wording. First-write-wins is rule 5: the stored row is what was, or will be, notified, so a later evaluation of the same candle on revised data must not rewrite it. **Rejected:** `INSERT … ON CONFLICT DO NOTHING RETURNING`, which works on SQLite ≥ 3.35 but moves the logic into dialect-specific SQL, makes the image's SQLite version a correctness dependency and bypasses the savepoint path D88 was adopted for |
| D113 | Foreign keys and deletion | `(ticker_id, timeframe) → tickers(id, timeframe)` and `rule_id → rules(id)`, both `ON DELETE CASCADE`. The CLI's removal guard counts signals too (D122) | **No orphans:** foreign keys are enforced on every connection (D72). **No resurrection:** ticker, rule and signal ids are never reused (`AUTOINCREMENT`; spec 013 AC2 is kept), so no later row can match an old key; and a run that evaluated a ticker or rule before it was deleted gets `UntrackedTickerError` or `UnknownRuleError` from `record`, never a stored signal to notify. The composite key pins a signal's timeframe to its ticker's, whose timeframe never changes (D97), as `ticker_rules` does. The key to `rules` is deliberately **not** composite: a rule's timeframe may change once it has no assignment (D97), and its history must keep the timeframe it was produced on rather than freeze the rule after its first signal. `CASCADE` and not `RESTRICT`, because a history row without its ticker or rule can be neither displayed nor re-identified, and D108 already puts the confirmation at the surface (U2). Accepted consequence: removing and re-adding a symbol starts a new history, and the new ticker can be notified for a candle the old one already was |
| D114 | Mapping the domain key | `record` takes the domain `Signal` and resolves `ticker_id` from `(signal.ticker, signal.timeframe)`, the unique natural key. `signal.rule_id` must be the canonical decimal text of an id in `[1, 2**63 - 1]` (`str(int(text)) == text`): **spec 004's `rule_id == str(rules.id)` is confirmed.** `candle_close_ts` is stored exactly as the signal carries it — `Evaluation.candle_close_ts`, that is `nominal_close(label)` — never recomputed or snapped to a grid. The rule's current timeframe must equal the signal's | The engine holds a `Signal`, so resolving the ids here removes any chance of recording one ticker's signal under another's id. The canonical-text check rejects `"042"`, `"+42"` and preview ids such as `"rule-7"`, which would otherwise map onto an existing row or fail late, and the upper bound keeps an oversized id from reaching SQLite as an `OverflowError`. Recomputing the close would move keys whenever the calendar changes (spec 004 §5). The timeframe check keeps a new row consistent with its rule when it is written; D113 only lets history diverge afterwards |
| D115 | Delivery state | `created_at` is when the row was recorded; `notified_at` is when a notification was confirmed, `NULL` until then. `mark_notified(signal_id)` sets `notified_at` from the injected clock once and never moves it. Delivery policy, **at-most-once** (U5): only the caller that got `is_new=True` sends, after its session committed; a signal whose sending failed or was interrupted is not re-sent automatically and stays visible with `notified_at` `NULL` | Rule 5 says that restarts and reruns never resend. Across a crash that is only guaranteed if the durable insert is the claim and sending comes after it: re-sending pending rows would resend whatever was delivered just before a crash, because "sent" and "marked" can never be one atomic step with Telegram. Keeping `notified_at` apart from `created_at` lets F7 and the dashboard show undelivered signals instead of hiding them. The policy is #14's to implement; this feature fixes the contract it relies on |
| D116 | History pagination | **Keyset**, newest first, ordered by `(candle_close_ts DESC, id DESC)`. A page is `SignalPage(items, next_cursor)`; `SignalCursor(candle_close_ts, id)` marks the last item, and `before=cursor` returns the rows strictly after it in that order. Filters: `ticker_id`, `rule_id`, `timeframe`, `notified`, and `since` (inclusive) and `until` (exclusive) on `candle_close_ts`. `limit` defaults to `DEFAULT_PAGE_SIZE = 50`, at most `MAX_PAGE_SIZE = 500` | Offset pagination repeats or skips rows when a signal is recorded between two pages, and the newest page is exactly where new rows land. With a keyset no row appears twice in one iteration and every row that existed when it started appears exactly once, whatever is inserted meanwhile; `id` makes the order total when two tickers share a close. Ordering by candle rather than by insertion keeps the history in market order and agrees with `latest` and with the range filter. Filtering on `candle_close_ts` keeps one definition of "when" for a signal; turning a user's dates into those bounds (for `1d`, the nominal close is 00:00 New York of the next day) is presentation, #22's and #23's (spec 004 §5). The cap bounds the cost of one query on the Pi |
| D117 | Latest for the cooldown | `latest(ticker_id, rule_id, *, notified_only=False)` returns the pair's signal with the greatest `candle_close_ts`, or `None` | `cooldown_bars` counts closed candles between two signals of one rule and ticker (spec 006), so #14 needs the last signal's candle, not when it was inserted. `notified_only` answers the architecture's "last notified signal" if #14 decides that an undelivered signal must not start a cooldown. Reading it and recording in the **same** unit of work is race-free because of D110 |
| D118 | `bot_state` | **One row per key**: `(key, value, updated_at)`, where every value is a UTC instant (`UtcDateTime`). The keys are the closed vocabulary `StateKey` — `paused_since`, `last_heartbeat`, `last_run.1h`, `last_run.4h`, `last_run.1d` — enforced by a `CHECK` built from the enum. A missing row means "never": not paused, no heartbeat, no run of that timeframe. The global pause **is** the presence of `paused_since`. `record_run` is monotonic | Every fact the issue names is an instant (when the pause began, when a heartbeat was seen, which close a run evaluated), so one `UtcDateTime` column types all of them with no text codec, and rule 6 holds at the boundary. Independent components write independent rows — Telegram the pause, F7 the heartbeat, #15 the runs —, each with its own `updated_at`. A missing row needs no seed data, so the migration writes no clock-less timestamp. The enum-built `CHECK` makes a new timeframe fail the golden DDL until a migration adds its key, as `tickers.timeframe` does. Monotonic runs make an out-of-order or repeated report harmless. **Rejected:** one singleton row with a column per fact (a seeded row, a `CHECK (id = 1)`, an `ALTER` per new fact and three unrelated writers on one row); a text `value` (it would bypass `UtcDateTime`). A future fact that is not an instant gets a column or a table of its own |
| D119 | Indicator values | `indicator_values_json` holds `json.dumps(dict(values), ensure_ascii=False, allow_nan=False, separators=(",", ":"))`, keys in the evaluation's order. A row that no longer loads raises `StoredSignalError(signal_id, kind)` and is never skipped | The keys are spec 007's (`rsi(length=14).value`), already stable identifiers, and their order is the rule's operand order, which a notification shows. `json` writes floats with `repr`: measured to round-trip bit for bit, including `5e-324` and `-0.0`. `allow_nan=False` matches `IndicatorValues`, which holds finite values only. The loud failure follows D93 |
| D120 | Module layout | Records in two new light modules, `persistence/signal_records.py` and `persistence/state.py` (which also holds `StateKey`); implementations in `repositories/signals.py` and `repositories/bot_state.py`, allowed only `trading_bot.domain.timeframe` and `trading_bot.domain.signals` beyond the base allowlist; the two `Protocol`s join `repositories/protocols.py` | Neither repository parses a rule document, so neither needs the rule schema that brings pydantic, pandas and TA-Lib (D99): the implementations and their records load without the analysis stack, and a fresh-interpreter check keeps it so. `records.py` imports the rule schema for `StoredRule`, so the new types cannot live there: `models.py` and `types.py` need `StateKey` and must stay light. The `Protocol`s stay with the other three in the one ports module, which does load the rule schema: every consumer of the ports (#14, #19, #22) loads it anyway |
| D121 | Retention | No pruning: a signal is kept until its ticker or rule is removed (U3) | Volume is bounded by what the user is willing to receive — a few signals a day at about 400 bytes each with indexes — so years of history are megabytes. A prune command would have to prove it never deletes a row that a rerun could recreate (only rows older than the newest evaluated candle of their pair are safe), which is F7's to specify if the need ever appears |
| D122 | CLI surface | `tickers remove` and `rules remove` count signals as well as assignments: they refuse while either exists, and `--force` removes and reports both. Two **read-only** commands are added, `signals list` and `state show`. No `pause`/`resume`. The `config` envelope is unchanged | Spec 013 hand-off 1 requires the removal report to name the history that goes with the row; without counting signals, a ticker with no assignment but a year of history would vanish without `--force`. `signals list` and `state show` are the only way to see what the engine records in M4, before Telegram (#17, #19) and the dashboard (#23) — the window that justified spec 013's U3 (U4). Pausing a bot that cannot notify yet is pointless, and once Telegram exists `/pause` is the surface; F7 may add a CLI fallback. Signals are history and the bot state is runtime state, not configuration, so neither enters `config export` (spec 013 hand-off 10); the binary pre-deploy backup covers both |
| D123 | A deterministic concurrency test | Issue AC2 is proven by an **ordered two-thread harness** (see "Concurrency" in the test plan), not by a barrier and hope: A records and keeps its transaction open; B starts its unit of work; a `before_cursor_execute` listener sees B's `BEGIN IMMEDIATE` being dispatched, and only then does A commit. A **control** with B's busy timeout at `0` proves that the two units of work collide. Every wait has a 60 s guard that turns a deadlock into a failure; no elapsed time is ever asserted | A barrier test passes vacuously when the threads happen to run one after the other. The harness forces the overlap: B's transaction starts while A holds the write lock over its uncommitted row, so B either waits for A's commit or — when the commit lands between the dispatch and the lock request — finds the row at once, and both interleavings must give `is_new=False`, which is the property under test. Measured: 150 of 150 runs gave the expected outcomes on one handle and on two handles over one file, and 150 of 150 control runs were refused with `database is locked`. The production busy timeout (5 s) is three orders of magnitude above a commit, so a slow CI runner cannot turn the wait into a failure |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/persistence/engine.py` | developer | `BEGIN_IMMEDIATE_OPTION`, the option-aware `begin` listener and the session factory binding (§5) |
| `src/trading_bot/persistence/database.py` | developer | `Database.session()` docstring states D110; no behaviour change of its own |
| `src/trading_bot/persistence/models.py` | developer | `SignalRow`, `BotStateRow` (§3) |
| `src/trading_bot/persistence/types.py` | developer | `StateKeyType` (§4) |
| `src/trading_bot/persistence/signal_records.py` | developer | New: the signal records and page constants (§6.1) |
| `src/trading_bot/persistence/state.py` | developer | New: `StateKey`, `LastRun`, `BotState` (§6.2) |
| `src/trading_bot/persistence/errors.py` | developer | `UntrackedTickerError`, `UnknownSignalError`, `StoredSignalError` (§6.3) |
| `src/trading_bot/persistence/repositories/protocols.py` | developer | `SignalRepository`, `BotStateRepository` (§7, §9) |
| `src/trading_bot/persistence/repositories/signals.py` | developer | New: `SqlSignalRepository` and the indicator-values codec (§7, §8) |
| `src/trading_bot/persistence/repositories/bot_state.py` | developer | New: `SqlBotStateRepository` (§9) |
| `src/trading_bot/persistence/repositories/tickers.py`, `rules.py` | developer | `delete` docstrings mention the signal cascade; no behaviour change |
| `src/trading_bot/persistence/migrations/versions/0003_signals_and_bot_state.py` | developer | New: §11 |
| `src/trading_bot/cli/main.py` | developer | Register the two new groups (§10.1) |
| `src/trading_bot/cli/tickers.py`, `rules.py` | developer | The removal guard and report of §10.2 |
| `src/trading_bot/cli/signals.py`, `state.py` | developer | New: `signals list` (§10.3), `state show` (§10.4) |
| `Dockerfile` | developer | The two table names in the existing migration check (§13); explicitly authorized by this spec |
| `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md` | developer | §14; explicitly authorized by this spec |
| `tests/fixtures/repositories.py` | developer | §15 |
| `tests/fixtures/concurrency.py` | developer | New: the ordered race harness of "Concurrency" (§15) |
| `tests/unit/test_persistence_models.py` | developer | TDD: T1 (the two tables join the golden DDL) |
| `tests/unit/test_persistence_migration_0003.py` | developer | TDD: T2 |
| `tests/unit/test_persistence_transactions.py` | developer | TDD: T3 |
| `tests/unit/test_persistence_signal_concurrency.py` | developer | TDD: T4 |
| `tests/unit/test_persistence_signals.py` | developer | TDD: T5–T9 |
| `tests/unit/test_persistence_bot_state.py` | developer | TDD: T10 |
| `tests/unit/test_cli_tickers.py`, `test_cli_rules.py` | developer | TDD: T11 (added cases) |
| `tests/unit/test_cli_signals.py`, `test_cli_state.py` | developer | TDD: T12 |
| `tests/unit/test_cli_main.py` | developer | T12: sixteen subcommands |
| `tests/unit/test_persistence_guard.py` | developer | T13 |
| `tests/unit/test_signal_idempotency.py` | tester | T14 |
| `tests/unit/test_persistence_signal_properties.py` | tester | T15 |
| `tests/unit/test_cli_secrets.py` | tester | T16 (the new subcommands) |
| `tests/unit/test_persistence_adversarial.py` | tester | T17 (added cases) |
| `docs/specs/014-signal-idempotency.md` | tech-lead | This spec |

No changes to `domain/`, `data/`, `deploy/`, `scripts/`, `.github/`, `main.py`, `config.py`,
`pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore` or `.dockerignore`.

### 2. Flow and the contract M4 builds on

```text
#14, one run (a timeframe, the scheduled now)           persistence
  to_thread: with database.session() as s:              BEGIN IMMEDIATE (D110)
      SqlBotStateRepository(s).load().paused               bot_state
      enabled tickers and their rules                      spec 013 repositories
  fetch candles, evaluate (no session open)             -
  for each triggered evaluation:
      to_thread: with database.session() as s:          BEGIN IMMEDIATE
          latest(ticker_id, rule_id) -> cooldown           signals (read)
          record(signal) -> RecordOutcome                  SAVEPOINT, INSERT
                                                           IntegrityError -> the stored row
      (the block committed: the claim is durable)
      is_new? send (no session, no lock held)           -
              to_thread: mark_notified(stored.id)       UPDATE notified_at
  to_thread: record_run(timeframe, now)                 bot_state (monotonic)

#17 / #19 / #22 / #23 ─▶ history(...), latest(...), get(...), load()   (read-only units of work)
operator ─────────────▶ python -m trading_bot.cli signals list | state show
```

The contract, stated once and repeated in the hand-off list: **commit before notifying**, and
only the call that got `is_new=True` notifies. `is_new` computed inside a session that later
rolls back is void: that row never existed, and the next run will get `is_new=True` again. A
session is never open across an `await`, a network call or a sleep (spec 012 hand-off 7).

### 3. Schema (revision `0003`)

The DDL SQLAlchemy renders for the models with the naming convention of spec 012 §5, measured on
the prototype and laid out for reading; the golden assertions of T1 and T2 compare with
whitespace collapsed, as spec 013 T1 does.

```text
CREATE TABLE signals (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ticker_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    candle_close_ts DATETIME NOT NULL,
    side VARCHAR(4) NOT NULL,
    close_price FLOAT NOT NULL,
    indicator_values_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    notified_at DATETIME,
    CONSTRAINT uq_signals_ticker_id_rule_id_timeframe_candle_close_ts
        UNIQUE (ticker_id, rule_id, timeframe, candle_close_ts),
    CONSTRAINT fk_signals_ticker_id_timeframe_tickers
        FOREIGN KEY(ticker_id, timeframe) REFERENCES tickers (id, timeframe) ON DELETE CASCADE,
    CONSTRAINT fk_signals_rule_id_rules
        FOREIGN KEY(rule_id) REFERENCES rules (id) ON DELETE CASCADE,
    CONSTRAINT ck_signals_timeframe CHECK (timeframe IN ('1h', '4h', '1d')),
    CONSTRAINT ck_signals_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT ck_signals_close_price CHECK (close_price > 0)
);

CREATE INDEX ix_signals_rule_id_candle_close_ts ON signals (rule_id, candle_close_ts);
CREATE INDEX ix_signals_candle_close_ts ON signals (candle_close_ts);

CREATE TABLE bot_state (
    "key" VARCHAR(32) NOT NULL,
    value DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT pk_bot_state PRIMARY KEY ("key"),
    CONSTRAINT ck_bot_state_key CHECK (key IN ('paused_since', 'last_heartbeat',
        'last_run.1h', 'last_run.4h', 'last_run.1d'))
);
```

- The model classes are `SignalRow` and `BotStateRow`, defined in `persistence/models.py` (spec 012
  hand-off 1), with the constraints declared in the order above: SQLAlchemy renders them in
  declaration order, which the golden DDL pins. They declare no relationship (spec 013 models).
- Column types: `ticker_id`, `rule_id` `int`; `timeframe` `TimeframeType`; `candle_close_ts`,
  `created_at`, `notified_at` (nullable), `bot_state.value` and `bot_state.updated_at`
  `UtcDateTime`; `side` `SideType`; `close_price` `Float` (SQLite `REAL`, 8-byte IEEE, measured to
  round-trip exactly); `indicator_values_json` `Text`; `bot_state.key` `StateKeyType`.
- The `CHECK` expressions are built from `Timeframe`, `Side` and `StateKey` in `models.py`, so a new
  member changes the rendered DDL and fails T1 until a migration is written. SQLAlchemy quotes the
  column name `key` because it is an SQL keyword; SQLite accepts it unquoted in the check, as
  measured.
- `sqlite_autoincrement=True` on `signals` (D111). `tickers` and `rules` keep theirs (D113).
- No index beyond the unique key and the two of D111. `bot_state` holds at most five rows.

### 4. Column types (`persistence/types.py`)

`UtcDateTime`, `TimeframeType` and `SideType` are unchanged. One `TypeDecorator` joins them:

```python
class StateKeyType(TypeDecorator[StateKey]):
    """A ``StateKey`` stored as its text (``paused_since``, ``last_run.1d``, ...)."""

    impl = String(32)
    cache_ok = True
```

Same boundary rules as `TimeframeType` (spec 013 §4): only a `StateKey` member binds, anything else
raises `TypeError`; text read back goes through `StateKey(...)`, so a value the database should
never hold raises `ValueError` at the boundary.

### 5. Transactions: sessions begin `IMMEDIATE` (D110)

In `create_database_engine`, the existing `begin` listener becomes option-aware, and
`create_session_factory` binds its sessions to the option:

```python
BEGIN_IMMEDIATE_OPTION = "trading_bot_begin_immediate"


@event.listens_for(engine, "begin")
def _begin(connection: Connection) -> None:
    # Sessions hold the write lock from their first statement (spec 014, D110): in WAL mode a
    # deferred transaction that reads and then writes fails with SQLITE_BUSY_SNAPSHOT, which
    # the busy timeout never retries. Raw connections keep a deferred BEGIN.
    immediate = connection.get_execution_options().get(BEGIN_IMMEDIATE_OPTION) is True
    connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    writer = engine.execution_options(**{BEGIN_IMMEDIATE_OPTION: True})
    return sessionmaker(bind=writer, expire_on_commit=False)
```

`engine.execution_options(...)` returns an engine that shares the pool, the pragmas and the
listeners of the original. The sketch passes `mypy --strict` (verified); `is True` keeps the
option's `Any` out of the listener.

Measured on the prototype (SQLite 3.49.1, SQLAlchemy 2.0.54, WAL, the four pragmas, a prototype
`signals` table over the real `0001` and `0002` migrations):

| Scenario | `BEGIN` (today) | Sessions `BEGIN IMMEDIATE` |
|----------|-----------------|----------------------------|
| One thread: B reads; A records and commits; B writes | `OperationalError`, `SQLITE_BUSY_SNAPSHOT` | cannot interleave: B's read already holds the lock |
| 8 threads × 40 keys, read then record | 40 new, 63 existing, **217 `database is locked`** | 40 new, 280 existing, 0 errors |
| 8 threads × 40 keys, record first | 40 new, 280 existing, 0 errors | the same |
| After a session's first read, a second connection with `timeout=0` runs `BEGIN IMMEDIATE` | acquires the lock | refused: `database is locked` |
| Statements of `record` twice in one session | — | `BEGIN IMMEDIATE`, `SAVEPOINT`, `RELEASE SAVEPOINT`, `SAVEPOINT`, `ROLLBACK TO SAVEPOINT` |
| The whole existing suite with the change patched in (a canary proved the patch active) | 5 446 passed | 5 446 passed |

Consequences, all deliberate:

- **Sessions are serialized, reads included.** A session holds the write lock from its first
  statement to its end. Units of work are microseconds to milliseconds and never span an `await`
  (spec 012 hand-off 7), so a waiter spends at most that long inside the 5 s busy timeout.
- **Two sessions open at once in one thread** now wait for the busy timeout and then fail with
  `database is locked`. "One session per unit of work" already forbids it; T17 pins the failure so
  it is recognizable. No existing test relies on it succeeding (the only one that opens two
  sessions in one thread, spec 012's two-writers case, expects `locked` and keeps passing).
- **An idle session takes no lock and emits nothing**, not even on `commit` (measured), so
  `with database.session():` around code that ends up doing nothing costs nothing.
- **Migrations keep `engine.begin()`** and a deferred `BEGIN`: at startup the lifespan is the only
  writer in the process, and the CLI refuses a database that is not at the head revision (D95).
- **The CLI benefits without a change of its own:** every command already runs in one session, so
  its read-then-write commands now wait for the application instead of failing with a misleading
  exit `1` ("the database refused the change") when a signal is committed in between.

### 6. Records and errors

#### 6.1 `persistence/signal_records.py`

```python
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSignal:
    id: int
    ticker_id: int
    rule_id: int
    rule_name: str  # the rule's current name, read through the join (D111)
    signal: Signal  # ticker symbol, timeframe, rule_id == str(rule_id), side, close, values
    created_at: datetime
    notified_at: datetime | None

    @property
    def key(self) -> SignalKey:
        return self.signal.idempotency_key


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordOutcome:
    stored: StoredSignal
    is_new: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalCursor:
    candle_close_ts: datetime
    id: int

    @classmethod
    def of(cls, stored: StoredSignal) -> SignalCursor: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalPage:
    items: tuple[StoredSignal, ...]
    next_cursor: SignalCursor | None
```

- `SignalCursor.__post_init__` normalizes `candle_close_ts` through `to_utc` (a naive instant raises
  `ValueError`).
- Records are immutable, hashable by value and free of ORM state, so they cross back from an
  `asyncio.to_thread` worker (D91). Every instant is read through `to_utc` before it is stored in
  a record (spec 012 §6).

#### 6.2 `persistence/state.py`

```python
class StateKey(StrEnum):
    PAUSED_SINCE = "paused_since"
    LAST_HEARTBEAT = "last_heartbeat"
    LAST_RUN_1H = "last_run.1h"
    LAST_RUN_4H = "last_run.4h"
    LAST_RUN_1D = "last_run.1d"

    @classmethod
    def last_run(cls, timeframe: Timeframe) -> StateKey: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class LastRun:
    timeframe: Timeframe
    scheduled_at: datetime  # the scheduled `now` the run evaluated (spec 010)
    completed_at: datetime  # when the run was recorded as complete, from the clock


@dataclass(frozen=True, slots=True, kw_only=True)
class BotState:
    paused_since: datetime | None
    last_heartbeat: datetime | None
    last_runs: tuple[LastRun, ...]  # only the timeframes that ran, in Timeframe order

    @property
    def paused(self) -> bool: ...

    def last_run(self, timeframe: Timeframe) -> LastRun | None: ...
```

`StateKey.last_run(timeframe)` is `StateKey(f"last_run.{timeframe.value}")`; T10 pins that it
covers every `Timeframe` member, so adding a timeframe without its key fails the gate.

#### 6.3 `persistence/errors.py`

```text
PersistenceError(Exception)
├── … the eight errors of spec 013 §6.2, unchanged
├── UntrackedTickerError(symbol, timeframe)   "ticker AAPL 1d is not tracked"
├── UnknownSignalError(signal_id)             "no signal with id 12"
└── StoredSignalError(signal_id, kind)        "the stored signal 12 is not valid: indicator_values"
```

- `StoredSignalError.kind` is `indicator_values` (the text is not a JSON object of finite numbers)
  or `payload` (the domain `Signal` rejected the row's values). The message never contains the
  stored text or a value.
- `UnknownRuleError` and `TimeframeMismatchError` are reused for `record` (§7). Messages follow
  spec 013 §6.2: one English line of ids, normalized symbols and timeframe codes.

### 7. Signal repository (`repositories/protocols.py`, `repositories/signals.py`)

```python
class SignalRepository(Protocol):
    def record(self, signal: Signal) -> RecordOutcome: ...

    def get(self, signal_id: int) -> StoredSignal | None: ...

    def mark_notified(self, signal_id: int) -> StoredSignal: ...

    def latest(
        self, ticker_id: int, rule_id: int, *, notified_only: bool = False
    ) -> StoredSignal | None: ...

    def history(
        self,
        *,
        ticker_id: int | None = None,
        rule_id: int | None = None,
        timeframe: Timeframe | None = None,
        notified: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        before: SignalCursor | None = None,
    ) -> SignalPage: ...

    def count(self, *, ticker_id: int | None = None, rule_id: int | None = None) -> int: ...
```

`SqlSignalRepository(session, *, clock: Clock = system_clock)`, one per unit of work (D91).

**`record(signal)`**, in this order (D112, D114):

1. `signal` must be a `Signal` (`TypeError` otherwise); `signal.rule_id` must be canonical (D114),
   otherwise `ValueError` naming the bounded `repr()` of the text.
2. Resolve the ticker by `(signal.ticker, signal.timeframe)` — the signal's symbol is already
   normalized — or raise `UntrackedTickerError`.
3. Load the rule's `name` and `timeframe`, or raise `UnknownRuleError`; a timeframe different from
   the signal's raises `TimeframeMismatchError(ticker_timeframe=signal.timeframe,
   rule_timeframe=rule.timeframe)`.
4. Build the row: the key, `side`, `close_price`, `dump_indicator_values(...)` (§8),
   `created_at=to_utc(clock())`, `notified_at=None`.
5. `with session.begin_nested(): session.add(row); session.flush()` → `RecordOutcome(stored,
   is_new=True)`.
6. On `IntegrityError`: the savepoint is rolled back; select the row with the same four key
   columns; return it with `is_new=False`, or re-raise when there is none.

Steps 2 and 3 read before the write. That is safe only because of D110: the session holds the
write lock from step 2, so no other writer can delete the ticker or rule, or insert the key,
between the checks and the `INSERT`. The unique constraint stays the arbiter (step 6) for any
writer that does not go through a session.

**Other methods:**

- `get(signal_id)` returns the record or `None`.
- `mark_notified(signal_id)` sets `notified_at = to_utc(clock())` when it is `NULL` and flushes;
  when it is already set it writes nothing. It returns the record, or raises `UnknownSignalError`.
- `latest(ticker_id, rule_id, *, notified_only=False)`:
  `ORDER BY candle_close_ts DESC LIMIT 1` over the pair, with `notified_at IS NOT NULL` when
  `notified_only`.
- `history(...)` validates its arguments first: `limit` is an `int` and not a `bool`
  (`TypeError`), in `[1, MAX_PAGE_SIZE]` (`ValueError`); `since`, `until` and the cursor's instant
  go through `to_utc`; `since > until` raises `ValueError`. It then selects `limit + 1` rows
  ordered by `(candle_close_ts DESC, id DESC)`, with
  `(candle_close_ts, id) < (cursor.candle_close_ts, cursor.id)` when `before` is given (SQLite
  row values, measured), returns the first `limit` and sets
  `next_cursor = SignalCursor.of(items[-1])` only when the extra row exists.
- `count(...)` is `SELECT count(*)` with the same two filters.
- Every read that returns records joins `tickers` (symbol) and `rules` (name) and builds the
  domain `Signal`, so a record is complete without a second query. A row that fails to load
  raises `StoredSignalError` (§6.3) `from None`; it is never skipped, including from `history`
  and `latest`.

**Stability of the history under concurrent writes (D116):** within one iteration, no row appears
twice; every row that matched and existed when the first page was read appears exactly once; a
row recorded during the iteration appears iff it sorts after the current cursor; a row whose
ticker or rule is removed disappears; with `notified=False`, a row marked notified between two
pages may disappear from later pages. Each page is its own read unit of work.

### 8. Indicator values (`repositories/signals.py`)

```python
def dump_indicator_values(values: Mapping[str, float]) -> str:
    """The canonical text of ``signals.indicator_values_json`` (decision D119)."""
    return json.dumps(dict(values), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def load_indicator_values(signal_id: int, text: str) -> dict[str, float]:
    """Parse a stored text, raising ``StoredSignalError`` when it is no longer valid."""
```

- `load_indicator_values` rejects `NaN`/`Infinity` literals (`parse_constant`), anything that is not
  a JSON object, and any value that is not an `int` or `float` (a `bool` included); it returns the
  values as `float` in stored order. `IndicatorValues` then applies the domain's own checks.
- Measured: `{"rsi(length=14).value": 28.4, …}` with `5e-324`, `1e-300`, `-1.2345678901234567e15`
  and `-0.0` round-trips bit for bit and in order.

### 9. Bot-state repository (`repositories/protocols.py`, `repositories/bot_state.py`)

```python
class BotStateRepository(Protocol):
    def load(self) -> BotState: ...

    def pause(self) -> datetime: ...

    def resume(self) -> bool: ...

    def record_heartbeat(self) -> datetime: ...

    def record_run(self, timeframe: Timeframe, scheduled_at: datetime) -> LastRun: ...
```

`SqlBotStateRepository(session, *, clock: Clock = system_clock)`:

- `load()` reads every row once and builds `BotState`; `last_runs` follows `Timeframe` order.
- `pause()`: with a `paused_since` row, returns its value and writes nothing; otherwise inserts
  `value = updated_at = to_utc(clock())` and returns it.
- `resume()`: deletes the `paused_since` row; returns whether one existed.
- `record_heartbeat()`: upserts `last_heartbeat` with `value = updated_at = to_utc(clock())`, last
  write wins; returns the value. A heartbeat is the latest sign of life, not a maximum.
- `record_run(timeframe, scheduled_at)`: `to_utc(scheduled_at)` (naive → `ValueError`, nothing
  written); when the stored value is later than or equal to it, returns the stored run and writes
  nothing; otherwise upserts `value = scheduled_at`, `updated_at = to_utc(clock())`.
- Upserts are a `session.get` followed by an insert or an update: race-free inside a session
  (D110), with the primary key as the backstop.

### 10. CLI

#### 10.1 Grammar

Two groups join the fourteen subcommands of spec 013 §9.1, for sixteen:

```text
signals      list  [--ticker SYMBOL] [--timeframe {1h,4h,1d}] [--rule NAME] [--limit N]
state        show
```

Both are read-only: one session, nothing written, no `--dry-run` (spec 013 AC38 covers read-only
commands). The exit codes are spec 013 §9.2's, unchanged.

#### 10.2 Removal (spec 013 hand-off 1, D122)

`tickers remove` and `rules remove` count the row's assignments **and** signals (`count`) before
deleting. Wording, where the count phrase is `plural(n, "assignment")`, `plural(m, "signal")` or
both joined with ` and ` (zero counts are omitted):

```text
error: ticker AAPL 1d has 2 assignments and 14 signals; pass --force to remove them with it
error: ticker AAPL 1d has 14 signals; pass --force to remove them with it
removed ticker AAPL 1d with 2 assignments and 14 signals
dry run: would remove ticker AAPL 1d with 14 signals
```

`rules remove` uses the same phrases after its existing subject (`rule 'RSI oversold in uptrend'`).
With no signal, every line is exactly spec 013's. The counts come from the database before the
delete, and the cascade stays the database's (D113).

#### 10.3 `signals list`

- `--ticker SYMBOL` resolves the ticker by `(normalize_ticker(SYMBOL), --timeframe or 1d)` (D109);
  `--timeframe` alone filters by timeframe; `--rule NAME` resolves the rule by its unique name. A
  missing ticker or rule exits `1` with the message the existing commands already print for it
  (`error: no ticker AAPL 1d is stored`, `error: no rule named 'X' is stored`).
- `--limit` is an integer in `[1, 500]`, default `20`; anything else is a usage error (`2`).
- One `history(...)` page, newest first, one line per signal and no summary; nothing when there is
  no signal. The line starts with the canonical key string of spec 004, then the side, the rule
  name as `quoted()` prints it, the close price with `repr`, and the id and the two instants in
  ISO 8601 UTC:

```text
AAPL|1d|3|2024-01-03T05:00:00+00:00 BUY 'RSI oversold in uptrend' close 187.5 (id 12, recorded 2024-01-02T21:00:05+00:00, notified 2024-01-02T21:00:06+00:00)
AAPL|1d|3|2023-12-30T05:00:00+00:00 BUY 'RSI oversold in uptrend' close 185.25 (id 9, recorded 2023-12-29T21:00:04+00:00, not notified)
```

The key shows the nominal close on purpose: it is the identifier the engine logs (#14 logs
`str(key)`), and the CLI is an operator tool. Indicator values are not printed, so a line stays
bounded; presenting the session date is #17's and #23's job (spec 004 §5).

#### 10.4 `state show`

Five lines, always in this order:

```text
paused: no
last heartbeat: never
last run 1h: never
last run 4h: never
last run 1d: 2024-07-05T20:00:30+00:00 (completed 2024-07-05T20:01:02+00:00)
```

A paused bot prints `paused: since 2024-07-05T21:00:00+00:00`; a heartbeat prints its instant.

### 11. Revision `0003`

`uv run alembic revision --autogenerate --rev-id 0003 -m "add the signal history and the bot state
tables"`, saved as `versions/0003_signals_and_bot_state.py` with `down_revision = "0002"`. Review
the generated file before committing:

- add `sqlite_autoincrement=True` to the `signals` `op.create_table` (AC3 fails otherwise);
- declare the constraints in the order of `models.py` so the stored DDL is the golden one;
- keep the revision **self-contained**, as `0002` is: the check expressions and the column types
  are frozen as literals, and nothing is imported from `trading_bot` (the existing guard fails
  otherwise);
- `downgrade` is real: drop both indexes, then `signals`, then `bot_state`. It removes the history
  and the bot state, and nothing else;
- no `ALTER` is needed; no data is written, so there is no backfill to make idempotent (spec 012
  hand-off 11).

Coverage (D89): the migrations directory is already a coverage source, so both functions must
show as executed. Spec 012 §10.3's table gains one behavioural proof:

| File | Proof it ran | Test |
|------|--------------|------|
| `versions/0003_signals_and_bot_state.py` | after `upgrade head` both tables exist with the §3 DDL; after `downgrade 0002` neither exists and every configuration row is intact | T2 |

Rolling an image back to one that only knows `0002` fails loudly with `CommandError` (spec 012
§14). Rolling the schema back with `downgrade` drops the history, so the recovery path for a bad
deploy stays the pre-deploy backup.

### 12. Guards and typing

`tests/unit/test_persistence_guard.py` gains per-file allowances, and its "expected modules" set
gains the five new files:

| File | Allowance beyond the base allowlist |
|------|-------------------------------------|
| `signal_records.py`, `state.py`, `repositories/signals.py`, `repositories/bot_state.py` | `trading_bot.domain.timeframe`, `trading_bot.domain.signals` |

`test_only_the_named_files_may_load_the_rule_schema` is unchanged: none of the new modules may name
`domain.rules.schema`. The fresh-interpreter check is extended to the four modules of AC28
(`pandas`, `numpy`, `talib`, `pydantic`, `alembic` absent), measured possible because `errors.py`,
`models.py` and `domain/signals.py` load none of them today.

Typing: strict mypy, no `Any` and no `type: ignore` in the new and changed `src/` files and in
`tests/fixtures/repositories.py` and `tests/fixtures/concurrency.py`. The `Protocol` conformance
of both implementations is proven by the typed factory of §15.

### 13. Image check

The existing `Dockerfile` migration check gains `signals` and `bot_state` in its table set, in the
same `RUN`. It still never touches `/app/data`. `docker build` is unavailable locally (no daemon);
the arm64 build in CI and the beta deploy are the authoritative runs.

### 14. Documentation

`docs/ARCHITECTURE.md`:

1. `## Persistence`: the bullet on synchronous SQLAlchemy states D110 — sessions begin
   `BEGIN IMMEDIATE`, raw connections do not, never two sessions at once in one thread.
2. A new `### Signals and bot state` subsection after `### Configuration tables and repositories`:
   the two tables and their keys, the idempotent `record` and what `is_new` means, "commit before
   notifying", the delete cascade, the history contract (keyset, order, filters, cursor) and
   `latest`, the bot-state vocabulary and its "missing means never" rule, and one short Python
   example in the style of the other sections (it must pass `ruff format --check`).
3. `### Configuration CLI`: the two new groups, and the removal rule counting signals.
4. `## Persisted data`: `signals` and `bot_state` become real schema; "Still a draft" goes.
5. The `persistence/` row of the layers table mentions signals and the bot state.
6. `### What the engine adds`, in `## Rule evaluation`: the dedupe bullet points at
   `SignalRepository.record` (spec 014) as the arbiter.
7. `## Decisions`: the SQLite bullet gains one sentence on D110.

`docs/DEPLOYMENT.md`, in `## Operations`, next to the configuration commands, with the same
placeholder style:

```sh
# What the engine recorded, newest first, and the bot state
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli signals list --ticker AAPL --limit 10
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli state show
```

plus one line saying that `tickers remove --force` and `rules remove --force` also delete the
signal history of that row.

### 15. Test fixtures (`tests/fixtures/repositories.py`, `tests/fixtures/concurrency.py`)

- `Repositories` gains `signals: SignalRepository` and `state: BotStateRepository`, built by
  `repositories(session, clock=...)` with the same clock, so mypy proves both implementations
  conform.
- `sample_signal(ticker: StoredTicker, rule: StoredRule, *, candle_close_ts: datetime = ...,
  close_price: float = 187.5, indicator_values: Mapping[str, float] | None = None, side: Side |
  None = None) -> Signal` builds a signal for stored rows with synthetic values (user decision D2)
  and `rule_id=str(rule.id)`.
- `SNAPSHOT_QUERIES` gains `signals` and `bot_state`, so every existing and new `--dry-run`
  assertion also proves the history and the bot state are untouched.

`tests/fixtures/concurrency.py` holds the ordered race harness of "Concurrency" in the test plan
(`ordered_race` and its result type), typed because mypy covers `tests/fixtures`, so T4 and the
tester's T14 run the same code. It opens no database of its own: it takes the `Database` handles
the tests got from `tests/fixtures/database.py` (or `connect_database` on the same directory).
No second database fixture, engine or `DeclarativeBase` is added (spec 012 hand-off 8).

## Test plan

Every test is a unit test without network (the D60 guard stays autouse), without the wall clock
(clocks are injected, instants are literals) and without writing outside `tmp_path`. Mandatory
template cases:

- **Anti look-ahead: not applicable.** This feature adds no indicator, rule or per-candle
  computation. The related obligation is tested instead: `candle_close_ts` is stored exactly as the
  `Signal` carries it and never recomputed (T5 records an instant off every grid, with
  microseconds, and reads it back unchanged), so persistence cannot move a key.
- **Idempotency (rule 5): the core of this feature.** T4 (simultaneous inserts, deterministic), T5
  (twice in a session, across sessions, across a restart, first write wins) and T14 (the tester's
  retry storms and stress) are dedicated to it; T10 covers the bot state's own idempotency (a
  repeated `pause`, a repeated `record_run`).
- **Authorization: not applicable.** No Telegram, API or dashboard surface is added; the two CLI
  commands are read-only and local, and spec 013's CLI rules stand.
- **Secret redaction (config and logs):** no secret and no `TB_*` is added. T16 extends spec 013's
  secrets test to the sixteen subcommands with signals and bot state in the database; the new
  errors carry ids, symbols and codes only, and `StoredSignalError` never echoes the stored text
  (T9, T17).

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 models and DDL | unit | Reflection and golden `sqlite_master` DDL of both tables (columns, types, nullability, constraint and index names, `AUTOINCREMENT`); `Base.metadata.tables` has five names; each `CHECK` rejects a raw insert; the unique key rejects a duplicate and accepts a one-microsecond difference; ids are not reused; both cascades delete only their rows; a raw insert with a mismatched timeframe or a missing parent fails; a rule's timeframe changes while it has signals and no assignment | AC1–AC5 | developer |
| T2 revision 0003 | unit | `upgrade head` from empty reaches `0003`; a second run is a no-op with the "already" record; `downgrade 0002` drops only the two tables and keeps configuration rows; `downgrade base`; re-upgrade; single head, `^[0-9]{4}$`, `down_revision == "0002"` | AC6 | developer |
| T3 transactions | unit | Statements recorded with `before_cursor_execute`: a session emits `BEGIN IMMEDIATE`, an idle session emits nothing, `engine.connect()` and `engine.begin()` emit `BEGIN`; the zero-timeout `sqlite3` lock probe after a session read (refused) and after a raw read (acquired); the `SQLITE_BUSY_SNAPSHOT` characterization of AC9; `create_session_factory` still sets `expire_on_commit=False` | AC7, AC9 | developer |
| T4 concurrency | unit | The ordered harness of "Concurrency" below: one handle and two handles (`connect_database`), write-first and read-then-write; the control with a zero busy timeout | AC8 | developer |
| T5 record | unit | Twice in a session, in two sessions, across a restart; first write wins on `side`, `close_price` and values; the session keeps working after `is_new=False`; a failed unit of work stores nothing and a later record is new; the key mapping and every rejection of D114, each leaving the session usable; the off-grid instant round trip; payload round trip and the literal JSON text | AC11–AC13 | developer |
| T6 delivery state | unit | `notified_at` starts `None`; `mark_notified` from the clock; the second call keeps the first instant; `UnknownSignalError`; no other column moves | AC14 | developer |
| T7 latest | unit | Greatest close whatever the insertion order; other pairs ignored; `notified_only`; `None` | AC15 | developer |
| T8 history and count | unit | Order, including two tickers sharing a close split across a page boundary; each filter alone and combined; `since` inclusive and `until` exclusive at the exact bounds; page sizes 1, 2, 3, 50 concatenate to the full order; `next_cursor` `None` at the end, also on an exact fit; a row recorded between pages with a newer and with an older close; argument rejections; `count` | AC16, AC17 | developer |
| T9 corrupt rows | unit | Raw SQL makes `indicator_values_json` invalid JSON, an array, a `NaN` literal, a string value, a `bool` value: `get`, `latest` and `history` raise `StoredSignalError` with the id and kind, `from None`, with no stored text in the message or the traceback | AC18 | developer |
| T10 bot state | unit | Vocabulary literal; `StateKey.last_run` for every timeframe; the `CHECK`; empty `load()`; `pause` twice; `resume` twice; heartbeat last write wins with a clock moving backwards; `record_run` earlier, equal and later; naive `scheduled_at`; a failed unit of work leaves no state; instants in other zones are stored as the same UTC instant and compared after `to_utc` | AC19–AC22 | developer |
| T11 CLI removal | unit | For `tickers remove` and `rules remove`: signals only, assignments only, both, neither; refusal lines, `--force` reports and `--dry-run` lines are the §10.2 literals; the snapshot (signals included) is unchanged by a refusal and a dry run | AC23 | developer |
| T12 CLI read commands | unit | `signals list` literal lines and order, each filter, `--limit` bounds and default, missing ticker or rule, empty output; `state show` for an empty and a full table; both write nothing (snapshot); sixteen subcommands and `--help` for the two new ones; `config export` unchanged by signals | AC24–AC26 | developer |
| T13 guards | unit | The new allowances, first exercised against synthetic snippets so a green result is not vacuous; the expected-modules set; the fresh-interpreter checks of AC28 | AC28 | developer |
| T14 idempotency and stress | unit | 8 threads behind a barrier per key × 20 keys, for write-first and read-then-write units of work, on one and on two handles: exactly one `is_new`, no exception, one row, every outcome naming the same id; 50 sessions recording the same signal in turn; record, dispose, reopen, record; the ordered harness repeated 50 times per variant | AC8, AC11 | tester |
| T15 properties | unit (`@given`) | Over drawn signals (a pool of stored tickers and rules, closes in several zones with microseconds, extreme prices and values): record then get is a fixed point after normalizing instants to UTC; a drawn sequence of recordings leaves exactly the distinct keys, each with its first payload; paginating a drawn table with drawn page sizes, with drawn inserts between pages, never repeats a row and returns every pre-existing row once; `latest` equals Python's maximum per pair | AC11, AC13, AC15, AC16 | tester |
| T16 secrets and language | unit | Spec 013's secrets test over sixteen subcommands with signals and bot state present; no new `TB_*`; no stored text, no absolute path and no secret in any message of the new errors and commands | AC27 | tester |
| T17 adversarial | unit | `rule_id` `"042"`, `"+42"`, `"0"`, `"-1"`, `"rule-7"`, 64 nines; a lower-case symbol with whitespace in the `Signal`; 40 indicator values (20 conditions × 2 operands); zero values; `5e-324`, `1.7976931348623157e308`, `-0.0`; two sessions open in one thread with a 50 ms busy timeout fail with `database is locked` (error class only); a session reused after `is_new=False`; records reject attribute assignment; a cursor from another filter is a position, not an error | AC7, AC12, AC13, AC16 | tester |

#### Concurrency (T4, D123)

What "simultaneous" means here: the second unit of work **starts** while the first holds the write
lock over its uncommitted row. The harness forces exactly that with events, never with sleeps:

```python
GUARD_SECONDS = 60  # turns a deadlock into a failure; never compared with an elapsed time


def ordered_race(first: Database, second: Database, signal: Signal, *, read_first: bool) -> Race:
    a_recorded, b_began = threading.Event(), threading.Event()
    b_ident: list[int] = []

    def spy(conn, cursor, statement, parameters, context, executemany) -> None:
        if b_ident and threading.get_ident() == b_ident[0] and statement == "BEGIN IMMEDIATE":
            b_began.set()  # B's transaction is being dispatched while A holds the lock

    def run_a() -> RecordOutcome:
        with first.session() as session:
            outcome = SqlSignalRepository(session, clock=CLOCK_A).record(signal)
            a_recorded.set()
            assert b_began.wait(GUARD_SECONDS), "B never began its unit of work"
        return outcome  # leaving the block committed A

    def run_b() -> RecordOutcome:
        b_ident.append(threading.get_ident())
        assert a_recorded.wait(GUARD_SECONDS), "A never recorded"
        with second.session() as session:
            repository = SqlSignalRepository(session, clock=CLOCK_B)
            if read_first:
                repository.latest(TICKER_ID, RULE_ID)  # the cooldown read of #14
            return repository.record(signal)

    ...  # listen, start both threads, join with GUARD_SECONDS, remove the listener
```

- Each thread body runs inside a wrapper that stores its return value or its exception; the test
  asserts that both threads finished (`not thread.is_alive()` after the guarded join) and that no
  exception was stored.
- **Assertions:** A's outcome is `is_new=True`; B's is `is_new=False`; B's `stored` equals A's
  (same id, and `created_at` from `CLOCK_A`); `count` is `1` for that key. The winner is fixed by
  construction, so nothing depends on scheduling.
- **Variants:** `first is second` (one pool) and `second = connect_database(same directory)` (a
  second engine, as the CLI next to the application), each with `read_first` `False` and `True`.
  Both handles use `busy_timeout_ms=BUSY_TIMEOUT_MS` (5 000), not the fixture's 200 ms, so the
  wait never approaches the limit on a loaded runner.
- **Control:** the same harness with `second` opened with `busy_timeout_ms=0`, and A leaving its
  block only after B has finished. B must raise `OperationalError` matching `locked`, and the
  table holds A's row only. It proves that the harness produces a real collision, so the main
  test cannot pass vacuously.
- **What guards D110 itself** is T3 (the statement and the lock probe), not T4: a write-first
  `record` would also pass T4 under a deferred `BEGIN`. The read-then-write variant of T14 is a
  second, statistical guard (217 failures in 280 under a deferred `BEGIN`).

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC29 |
| V2 coverage | `uv run pytest tests/unit --cov --cov-report=term-missing`: the new and changed `src/` modules at 100%, `versions/0003_signals_and_bot_state.py` listed at 100%, no regression elsewhere; `git diff origin/main -- pyproject.toml` empty | AC6, AC29 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 20 s and none exceeds 3 s. Reported, never asserted | AC29 |
| V4 typing | `uv run mypy`; `git grep -n "Any"` and `git grep -n "type: ignore"` over the new and changed `src/` files and the two fixture modules empty | AC29 |
| V5 dependencies | `git diff origin/main -- pyproject.toml uv.lock` empty | — |
| V6 schema | The reflected schema and the `sqlite_master` DDL of the two tables pasted into the PR; `upgrade head`, `downgrade 0002` and `upgrade head` again on a temporary database with configuration rows present | AC1–AC6 |
| V7 image | Local `docker build` **BLOCKED** (no daemon). Local substitute: run the §13 check payload with `uv run python -c …`. Authoritative: the PR's `Docker build (arm64)` job and the beta deploy, verified by the lead. Report as BLOCKED with this justification, never as PASS | — |
| V8 concurrency | T4 and T14 run 20 times in a row (`pytest --count` is not available: a shell loop), all green, with the number of runs reported; the control variant refused in every run | AC8 |
| V9 CLI walkthrough | On a temporary database, one pasted transcript: `config import` of an example, `tickers add AAPL`, `assignments add`, `rules enable`, a signal recorded by a short Python snippet through `SqlSignalRepository`, `signals list`, `state show`, `tickers remove AAPL` (refused, naming the signal), `--dry-run --force`, `--force`. Exit codes reported; no path outside the temporary directory appears | AC23–AC25 |
| V10 no stray database | The session-end guard of spec 012 AC20 is green, and `git status --porcelain --ignored` shows no `*.db`, `*.db-wal` or `*.db-shm` in the working tree | — |
| V11 docs | §14 placement and content; `uv run ruff format --check` on the Python block of the new subsection | AC30 |
| V12 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC31 |
| V13 secrets and language | `python scripts/secret_scan.py --history`; an English-only review of the whole diff, test data and CLI lines included; no host, user, IP or absolute infrastructure path anywhere | AC27 |

**Testing rules for this feature:**

- no wall-clock or elapsed-time assertions: clocks are injected; the busy-timeout cases assert the
  error class; the 60 s guards only turn a deadlock into a failure;
- no `time.sleep` to order threads: events and the `before_cursor_execute` listener only;
- no platform-dependent expectations: the same assertions on Windows and Linux, instants compared
  after `to_utc`;
- literal expectations written out, never recomputed with the code under test (constraint names,
  DDL, JSON text, CLI lines, error messages, revision identifiers);
- every database lives under `tmp_path` and comes from `tests/fixtures/database.py`; the lock probe
  opens that same file with `sqlite3.connect(path, timeout=0, isolation_level=None)` and closes it;
- test data is synthetic (user decision D2).

**TDD order suggested to the developer:**

1. T3 (D110) — it changes how every later test's sessions behave, so it lands first and the whole
   existing suite is re-run right after it;
2. T1 → T2 (models, then the revision) with the golden DDL;
3. T5 → T6 → T7 → T8 → T9 (`record` first, then the reads);
4. T4 (the harness, once `record` exists);
5. T10 (bot state);
6. T11 → T12 (CLI), T13 (guards), the `Dockerfile` table set and the docs.

## Risks and security

- **D110 changes how every session locks.** Sessions are serialized, reads included; a unit of
  work that held a session for more than 5 s would make others fail with `database is locked`.
  Mitigation: the rules already in place (one short session per unit of work, never across an
  `await`), T3 pinning the mechanism, T17 pinning the nested-session failure so it is
  recognizable, and the whole existing suite measured green with the change.
- **A crash between recording and sending loses that notification** (D115, U5). It is the price of
  never resending (rule 5); the row stays visible with `notified_at` `NULL`, and F7 can alert on it.
- **Removing a ticker or rule deletes its history** (D113, U2). The CLI requires `--force` and names
  the count; `--dry-run` shows it first; the pre-deploy backup is the only recovery.
- **Re-adding a removed symbol** starts a new history and can notify a candle the removed ticker
  was already notified for. Documented in D113; the user has to remove and re-add within one
  candle for it to matter.
- **The nominal `1d` close in filters.** A date filter taken literally on `candle_close_ts` includes
  the previous session's daily candle (its nominal close is 00:00 New York of the next day). The
  repository documents it (D116) and #22/#23 convert user dates.
- **Rolling back after `0003`.** An older image fails loudly with `CommandError` (spec 012 §14);
  `downgrade` drops the history, so the backup stays the recovery path.
- **Stored market data in a public repository.** Test signals are synthetic (D2); no fixture,
  example or document carries real prices. The database itself lives on the Pi's volume, never in
  the repository.
- **Supply chain.** No dependency is added or changed.
- **Unbreakable rules.** All preserved:
  - signal-only: a signal row records a decision to notify; no order, broker, account or credential
    concept exists anywhere in the schema, the repositories or the CLI;
  - pure `domain/`: `persistence` imports `domain`, never the reverse; the purity guard is
    untouched;
  - closed candles only: unaffected; `candle_close_ts` is stored as the evaluation gives it;
  - idempotency: the database constraint is the arbiter, D110 makes the loser wait instead of fail,
    and T4, T5 and T14 prove both;
  - UTC: every instant is a `UtcDateTime` and every comparison normalizes first;
  - single worker: nothing new runs in the application process; D110 is what keeps its threads and
    the CLI process from failing each other;
  - no `eval`;
  - English only.

### Resolution of the inherited obligations

| Obligation (spec 012 and 013 hand-off lists, as relayed by the lead) | Resolved by |
|------|-------------|
| 1. Revision `0003`, `down_revision = "0002"`, real `downgrade`, one head, tests in both directions, self-contained | §11, AC6, T2 |
| 2. A database unique constraint on the four key columns, named by the convention, plus `IntegrityError` handling inside `begin_nested()` | §3, D112, AC2, AC11, T5; D110 makes the loser wait instead of fail, T4 |
| 3. The `ondelete` policy of both foreign keys: no orphans, no resurrected notification | D113, AC5, T1; the CLI side in D122, AC23 |
| 4. Keep `sqlite_autoincrement=True`; confirm `rule_id == str(rules.id)` | D113, D114, AC3, AC12 |
| 5. Reuse the clock, records, errors and the D91 shape; reuse both fixture modules | §6, §7, §9, §15; D120 explains the two new record modules |
| 6. `candle_close_ts` exactly as `Evaluation` gives it; every timestamp a `UtcDateTime` | D114, §3, AC12, T5 |
| 7. `bot_state` with `UtcDateTime`, the injected clock and a closed vocabulary | D118, §6.2, §9, AC19–AC22 |
| 8. #14 must not swallow `StoredRuleError` | Hand-off 4 below, extended to `StoredSignalError` |
| 9. New operational commands join the CLI package, with its exit codes, `--dry-run` and vocabulary; the envelope stays `version: 1` | §10, D122; the envelope is unchanged |
| 10. Event-loop callers wrap each unit of work in `asyncio.to_thread`, never share a session | §2, hand-off 1 below; D110 adds "never two sessions at once in one thread" |
| 11. Normalize aware instants to UTC before comparing | §6.1, §7, §9, T10, T15 |
| Spec 012 hand-offs 1 and 2: models in `models.py`, no `create_all()` in `src/` | §3; tests create nothing on `Base.metadata` |
| Spec 012 hand-off 12: new `TB_*` as `SecretStr` | None added |

### Hand-off list for M4 (#14, #15, #16), M5, M6 and F7

**#14 (engine):**

1. **Commit before notifying.** Cooldown read (`latest`) and `record` in one unit of work; leave the
   `Database.session()` block (commit); only then send, with no session open; then
   `mark_notified` in a new unit of work. Never send inside a session, never hold one across an
   `await`, never open two at once in one thread (D110).
2. **Only `is_new=True` notifies** (D115, U5). Log an existing outcome with `str(key)`; a
   stored payload that differs from the new evaluation (a revised candle) deserves a `WARNING`.
3. `UntrackedTickerError`, `UnknownRuleError` and `TimeframeMismatchError` from `record` mean the
   configuration changed during the run: skip that signal, log it, never notify it.
4. Do not swallow `StoredRuleError` (spec 013 D93) or `StoredSignalError` (D119).
5. Cooldown by position: the last signal's label is `candle_close_ts - timeframe.duration`; find it
   in the evaluated frame, and treat a label older than the frame as "cooldown passed". Decide
   whether an undelivered signal starts a cooldown (`notified_only`, D117).
6. Decide what the global pause suppresses. Recommendation: while `load().paused`, skip the run's
   evaluation and recording entirely, so nothing stale is sent after `/resume`.
7. Build `Signal.rule_id` as `str(stored_rule.id)` (D114) and pass the injected clock to the
   repositories.

**#15 (scheduler):** call `record_run(timeframe, now)` once a run is complete (retries included),
with the scheduled `now`; `record_run` is monotonic, so a late or repeated report is harmless.
Catch-up after a restart compares `load().last_run(timeframe)` with the calendar; the repository
decides nothing. Record heartbeats at the cadence F7 fixes.

**#16 (wiring):** build `SqlSignalRepository` and `SqlBotStateRepository` per unit of work from the
one `Database` of the lifespan; never a second engine.

**#17, #19 (Telegram):** a message reads `StoredSignal.signal` and `rule_name`; present the candle
as its label or session date, never the nominal close as the market close (spec 004 §5); `/pause`,
`/resume` and `/status` go through `BotStateRepository`.

**#22, #23 (API and dashboard):** encode `SignalCursor` as an opaque, validated token; convert user
dates into `candle_close_ts` bounds per timeframe (D116); the history shows the rule's current
name; keyset pagination has no page numbers; a request's session serializes with the engine, so
keep it short, and never hold a session in a test while calling the client (D110).

**F7 (operations):** heartbeat cadence and silence detection from `last_heartbeat`; an alert for
signals left with `notified_at` `NULL`; retention or a prune command if the need appears (D121);
a CLI `pause`/`resume` fallback if wanted (U4).

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; M3 stacks #11 → #12 → #13.
- **D2 (2026-09-14):** synthetic data only in tests. No fixture here contains market data.

The five product questions this spec opened were answered on **2026-09-19**. The user confirmed
the recommended answer to each, so the design above needed no change; they are recorded in full
so a later reader can trace every lifecycle rule of the history to a decision.

- **U1 (2026-09-19) — what a signal stores: the recommendation.** The key (ticker, timeframe,
  rule, candle close), the side, the close price and the indicator values — exactly what the
  Telegram message shows — plus `created_at` and `notified_at`. The rule name is read live, not
  copied, so a renamed rule shows its new name in the history; no chart or candles are stored,
  and a past signal's chart is redrawn from market data when the dashboard needs it. (D111)
- **U2 (2026-09-19) — removing a ticker or rule deletes its signal history** (the cascade), and the
  CLI requires `--force` and reports how many signals go; `--dry-run` shows it first. (D113,
  D122, AC5, AC23)
- **U3 (2026-09-19) — retention: keep every signal, no pruning.** Asked together with U2 as one
  question about the history's lifecycle; the user's answer was "keep it; it only goes with
  `--force`". Revisit in F7 only if the database ever grows. (D121)
- **U4 (2026-09-19) — CLI: `signals list` and `state show` are added now**, because M4 runs the
  engine before Telegram and the dashboard exist; `pause`/`resume` stay with Telegram (#19) and
  F7. (D122, AC24, AC25)
- **U5 (2026-09-19) — a crash between recording a signal and sending it: at-most-once.** Put to the
  user as the literal reading of `CLAUDE.md` rule 5 ("restarts or reruns do not resend it"), with
  the stated risk of losing that single notification, which stays visible in the history as not
  notified. The alternative, at-least-once, can deliver the same signal twice after a crash.
  (D115; #14 hand-offs 1 and 2)

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames, users or absolute infrastructure paths
- [ ] Tests cover the acceptance criteria and fail without the implementation
- [ ] The signal key is a database unique constraint, and `record` handles its `IntegrityError`
      inside a savepoint; no read-then-write dedupe
- [ ] Sessions begin `BEGIN IMMEDIATE`, raw connections do not; T3 pins both and the whole existing
      suite is green with it
- [ ] The concurrency test is the ordered harness with its control; no sleep, no elapsed-time
      assertion
- [ ] `upgrade head`, `downgrade 0002` and `downgrade base` tested; one head; `0003` follows `0002`;
      the DDL matches §3 including `AUTOINCREMENT`, the checks and both cascades
- [ ] Repositories never commit, return frozen records, and load no rule schema
- [ ] `candle_close_ts` is stored verbatim; every timestamp is a `UtcDateTime`
- [ ] The CLI removal names the signals it deletes; the two read commands write nothing; the
      envelope is unchanged
- [ ] No `Any`, no `type: ignore`; the isolation guard, the purity guard and the structural guards
      are green
- [ ] Scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
