# 015 — SignalEngine: the evaluation pipeline per ticker and timeframe

- **Status:** approved (the seven product questions are answered; see "User decisions",
  U1–U7, 2026-09-20)
- **Branch:** `feature/signal-engine`, from `origin/main` at `7912dbd` (v0.11.0)
- **Spec author:** tech-lead
- **Issue:** #14 (milestone M4 · Signal engine), the first of #14 → #15 → #16
- **Expected commit type:** `feat:`. The change adds the `src/trading_bot/engine/` package (the
  orchestration M4 is built on) and the `Notifier` port in `src/trading_bot/notifications/`, which
  #17 implements. No dependency, no migration and no `TB_*` variable is added; nothing is wired
  into `main.py` yet (#16 does that), so the published image only gains modules. `feat` → minor
  bump. Suggested squash subject: `feat: add the signal engine pipeline (#<n>)`.

## Goal

Turn the parts M1–M3 delivered into one run: for a timeframe and a scheduled candle close, fetch
the closed candles of every enabled ticker, evaluate its enabled rules, suppress what the cooldown
and the signal history say must not be sent again, record what fires and notify it exactly once.

- `SignalEngine.run(timeframe, now)` is the single entry point the scheduler (#15) calls, and the
  only place where the data layer, `domain/`, the repositories and the notifier meet.
- **Reprocessing a candle never creates or notifies a duplicate** (issue AC1, `CLAUDE.md` rule 5):
  the unique constraint of spec 014 stays the arbiter, and the engine commits its claim before it
  sends anything.
- **A failing ticker never stops the rest**: every market-data failure is classified, logged and
  reported, and the run continues with the next ticker.
- The run honours the global pause, and returns a `RunReport` that #15 uses to retry the tickers
  whose candle was not published yet and that F7 (#26, #27) turns into alerts.

## Out of scope

- **Scheduling** (#15): when a run happens, the delay after the candle close, the retry window for
  `CandleNotPublishedError`, catch-up after a restart, `record_run` and the heartbeat. This feature
  only builds the seams §8.1 and the hand-off list describe.
- **Wiring** (#16): nothing is built in `main.py` or the lifespan, `/health` is unchanged, and no
  module of the running application imports `engine/` yet.
- **The Telegram message and the chart** (#17, #18): this feature defines the `Notifier` port and a
  fake, never a delivery implementation, a message format, a rate limit or a chat allowlist.
- **Ticker validation** (`validate_ticker`): it belongs to `/add` (#19) and to the CLI. A run
  evaluates what the configuration says, and reports a ticker the provider rejects.
- **Persisting errors, metrics or run durations.** The run reports them in memory and in the log;
  a table, a heartbeat, an alert or an aggregation belongs to F7 (U5).
- **Backfilling missed candles.** A run evaluates the last closed candle only (D128, U2).
- **Re-notifying signals left undelivered.** Delivery is at-most-once (spec 014, D115, U5); no
  sweep of `notified_at IS NULL` rows exists here or later (U4).
- **Redacting tracebacks.** `RedactingFilter` rewrites `record.msg` only; fixing that is issue #50.
  This feature bans `exc_info`/`stack_info` in its two packages instead (D135) and changes no
  logging code.
- **New indicators, operators or rule syntax**, changes to `domain/`, `data/`, `persistence/`,
  `cli/`, `deploy/`, `scripts/`, `.github/`, `config.py`, `main.py`, `pyproject.toml`, `uv.lock`,
  `Dockerfile`, `.env.example` and `CLAUDE.md`. `docs/ARCHITECTURE.md` changes only as §13 states;
  the lead marks F4 completed in `docs/ROADMAP.md` after #16.

## Acceptance criteria

"Temporary database" means a migrated database under pytest's `tmp_path`, opened through
`tests/fixtures/database.py`. "Unit of work" means one `Database.session()` block. "Run" means one
`await SignalEngine.run(timeframe, now)`.

### The run pipeline

- [ ] **AC1 (one configuration snapshot):** a run reads the bot state, the enabled tickers of its
  timeframe and their enabled rules in **exactly one** unit of work, inside one worker thread, and
  uses that snapshot for the rest of the run. A disabled ticker, a disabled rule, a ticker of
  another timeframe and a ticker with no enabled rule produce **no** `fetch_candles` call and no
  outcome. Configuration written after the snapshot is read does not change that run.
- [ ] **AC2 (one fetch per planned ticker):** `fetch_candles(symbol, timeframe, lookback, now=now)`
  is called once per planned ticker, with the normalized symbol, the run's timeframe, `now` exactly
  as the run received it (through `to_utc`) and the `lookback` of AC3, and is never retried inside
  the engine. The returned frame is evaluated as it is: never truncated, re-sorted, re-fetched or
  modified (the frame the provider returned is bit-for-bit equal after the run).
- [ ] **AC3 (lookback planning):** `plan_lookback(rules)` is
  `min(MAX_LOOKBACK, max(max(rule.stable_warmup(), rule.cooldown_bars + 1) for rule in rules))`,
  pinned on a literal table of rules, and is a `ValueError` on an empty sequence.
- [ ] **AC4 (evaluation):** every enabled rule of the ticker is evaluated exactly once per run on
  that frame with `domain.rules.evaluator.evaluate`, in a worker thread, in the order
  `rules_for_ticker` returns (by rule name). A rule that does not trigger writes nothing, notifies
  nothing and appears in no outcome. A triggered rule yields
  `Signal(ticker=plan.symbol, timeframe=rule.timeframe, rule_id=str(stored_rule.id),
  side=rule.signal, candle_close_ts=evaluation.candle_close_ts,
  close_price=evaluation.close_price, indicator_values=evaluation.indicator_values)`.
- [ ] **AC5 (the mandatory order, spec 014 hand-off 1):** for each triggered evaluation the cooldown
  read (`latest`) and `record` happen in **one** unit of work that commits before anything is sent;
  the notifier is called with **no** session open and outside any worker thread holding one;
  `mark_notified` runs in a unit of work of its own, after the notifier returns. Only an outcome
  with `is_new=True` on that committed unit of work is notified. The order is proven by a recorded
  call log, not by inspection.

### Cooldown

- [ ] **AC6 (by candle position, D129):** `cooldown_decision` returns `ALLOWED` when the pair has no
  previous signal or when the number of frame labels in `(previous_label, evaluated_label]` is
  **greater than** `cooldown_bars`; `BLOCKED` when it is smaller or equal; `SUPERSEDED` when
  `previous_label` is **after** the evaluated label; and `ALLOWED` when it **equals** it, so the
  same candle always reaches `record`, the arbiter of rule 5. `previous_label` is
  `previous.candle_close_ts - timeframe.duration`, computed exactly. The full decision table of §5
  is pinned, `cooldown_bars=0` never blocks, and a frame whose start is later than `previous_label`
  counts every row it holds.
- [ ] **AC7 (anti look-ahead, `CLAUDE.md` rule 4):** `cooldown_decisions` passes
  `assert_no_lookahead` and `cooldown_decision` passes `assert_no_lookahead_point_in_time` against
  it, on every `Scenario` of `synthetic_candles` and on calendar-aligned frames, for several
  `cooldown_bars` and previous closes. The decision at candle `t` never changes when later candles
  are appended.
- [ ] **AC8 (never a duration division):** over a frame that spans a weekend and a holiday, the
  decisions equal the hand-written expectations of §5.2, which differ from
  `(t2 - t1) / timeframe.duration` at those gaps. No module of `engine/` divides two instants.

### Idempotency and delivery

- [ ] **AC9 (issue AC1 — reprocessing):** a second run with the same `timeframe` and `now`, on the
  same database, creates no row and sends no notification; the signal count and every stored row are
  unchanged, and the outcome is `DUPLICATE`. It holds after disposing and reopening the `Database`
  (a restart), and with the notifier failing in the first run.
- [ ] **AC10 (no resend after an interrupted delivery, spec 014 D115/U5):** when the notifier raises
  for a new signal, the row stays with `notified_at is None`, the disposition is `UNDELIVERED`, the
  run continues with the next signal and ticker, and **no later run re-sends it**: a rerun of the
  same candle gives `DUPLICATE` with no notifier call, and the next candle's signal is a new key.
- [ ] **AC11 (a successful delivery is stamped once):** after a notified signal, `notified_at` comes
  from the repositories' injected clock; a rerun never moves it; `mark_notified` raising
  `UnknownSignalError` (the row was removed between the two units of work) leaves the disposition
  `NOTIFIED`, logs one `WARNING` and does not stop the run.

### Isolation and errors

- [ ] **AC12 (a failing ticker does not stop the rest):** for each of `InvalidTickerError`,
  `NoDataError`, `ProviderDataError`, `CandleNotPublishedError` and `ProviderUnavailableError`
  raised for the first of three tickers, the other two are fetched, evaluated and notified, the
  failing ticker gets one `TickerOutcome` with the `FailureKind` and `retryable` of §9, and one log
  record of the level §10 fixes. Nothing of the failing ticker is recorded.
- [ ] **AC13 (an unexpected error is isolated but never silent):** an arbitrary `Exception` raised by
  the provider or by the evaluation of one ticker gives `FailureKind.UNEXPECTED`, one `ERROR` record
  naming the exception **class** and the ticker (never `exc_info`, never the exception text), and the
  run continues. `BaseException` (`asyncio.CancelledError` included) is never caught.
- [ ] **AC14 (loud failures propagate, spec 013 D93, spec 014 D119):** `StoredRuleError`,
  `StoredSignalError` and `CalendarRangeError` are never swallowed and never downgraded to a ticker
  failure: they propagate out of `run`, after at most one `ERROR` record, and the report is not
  returned.
- [ ] **AC15 (the configuration changed during the run):** `UntrackedTickerError`, `UnknownRuleError`
  and `TimeframeMismatchError` raised by `record` (the ticker or rule was removed or changed after
  the snapshot) give the disposition `REJECTED`, one `WARNING` naming the key, no notification, and
  the run continues with the next signal.

### Pause

- [ ] **AC16 (the global pause skips the run, U3):** with `paused_since` present, `run` performs no
  `fetch_candles`, no evaluation, no `record` and no notification, returns
  `RunReport(paused=True, tickers=())` and logs one `INFO` naming the timeframe, `now` and the pause
  instant. Removing the pause and running again behaves normally, and the candles that closed during
  the pause are never evaluated afterwards.

### Ports, report and seams

- [ ] **AC17 (the report):** `RunReport`, `TickerOutcome` and `SignalOutcome` are frozen, slotted,
  keyword-only dataclasses whose instants are aware and in UTC; `report.now` is the argument; the
  ticker outcomes are in plan order; `retryable_tickers` holds exactly the symbols whose failure was
  retryable, which is what #15 re-runs with the same `now`; `summary` is the literal one-line record
  of §10.
- [ ] **AC18 (the `Notifier` port):** `Notifier` is an async `Protocol` with one method,
  `notify(notification: SignalNotification) -> None`; `SignalNotification` carries the committed
  `StoredSignal`, the parsed `Rule` and the closed candles the decision was made on;
  `FakeNotifier` conforms to it through a `Protocol`-typed factory checked by strict mypy; the
  engine passes the frame it evaluated and never copies it, and a notifier that mutates its
  `candles` is a notifier bug, documented in the port.
- [ ] **AC19 (the SQL unit of work):** `sql_unit_of_work(database, clock=system_clock)` returns a
  `UnitOfWork` that yields the four repositories of §3 over one `Database.session()`, builds no
  engine of its own, and is the only module of `engine/` that imports SQLAlchemy or a `Sql*`
  repository. The end-to-end tests use it over a temporary database.
- [ ] **AC20 (a filtered run, the #15 seam):** `run(timeframe, now, tickers=("AAPL",))` plans only
  those symbols (normalized through `normalize_ticker`), ignores symbols that are not enabled for the
  timeframe, and behaves identically otherwise. An invalid symbol raises `ValueError` before any
  I/O; a non-sequence raises `TypeError`.

### Guards, secrets, typing, docs and scope

- [ ] **AC21 (the engine guard):** an AST scan of `engine/` and `notifications/` proves that no
  module reads the clock (`datetime.now`, `utcnow`, `today`, `time.time`), that `sqlalchemy` and
  `trading_bot.persistence.repositories.<implementation>` appear only in
  `engine/sql_unit_of_work.py`, that `telegram`, `yfinance`, `talib`, `fastapi` and
  `trading_bot.cli` appear nowhere, that no module holds mutable module-level state, and that no
  call in either package passes `exc_info` or `stack_info` (D135). `domain/` still imports neither
  `engine` nor `notifications`, and the existing purity and persistence guards stay green.
- [ ] **AC22 (secrets and logs):** no `TB_*` variable is added and no secret exists in this feature.
  With a notifier whose exception message carries a token-shaped string built at runtime
  (`"123456789" + ":" + "x" * 35`), the captured log output contains neither that string nor the
  exception text, and the record names only the exception class and the signal key.
- [ ] **AC23 (typing and gate):** `uv run python scripts/check.py` is green; strict mypy passes with
  no `Any` and no `type: ignore` in the new `src/` files and in the new fixtures; the new modules
  report 100% coverage and overall coverage does not regress.
- [ ] **AC24 (docs and scope):** `docs/ARCHITECTURE.md` changes exactly as §13 states, and
  `git diff origin/main --name-only` plus `git status --porcelain` list only the files of Design §1.

## Design

### 0. Decisions

Decisions D1–D123 are recorded in specs 003–014. This spec relies on D22 (identity stays nominal),
D33/D44 (who retries an unpublished candle), D36/D37 (the async provider port with an explicit
`now`), D69 (synchronous SQLAlchemy behind `asyncio.to_thread`), D91 (frozen records and per-unit
repositories), D92 (the injected clock), D93 (a stored document that no longer parses fails
loudly), D110 (sessions begin `BEGIN IMMEDIATE`), D112 (the idempotent `record`), D115
(at-most-once delivery), D117 (`latest` for the cooldown) and D119 (`StoredSignalError`). The new
decisions are **D124–D140**; the next free identifier after this spec is **D141**. The user
decisions of 2026-09-20 confirm D129 and D130 (U1), D128 (U2), D133 (U3), D132 (U4), D136 (U5),
D137 (U6) and the `SUPERSEDED` branch of D129 (U7).

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D124 | Layering | The engine lives in `src/trading_bot/engine/` and depends on **ports only**: `MarketDataProvider` (spec 010), the five repository `Protocol`s (specs 013, 014), the new `Notifier` and the pure functions of `domain/`. One module, `engine/sql_unit_of_work.py`, names the `Sql*` implementations so #16 has a factory to wire; `signal_engine.py` never imports SQLAlchemy | It is the same shape the rest of the project uses (`data/yahoo/factory.py`, `main.py` injects). It keeps the orchestration testable with doubles for the paths a real database cannot produce on demand (a ticker deleted between two units of work), and it leaves the engine reusable when the dashboard (#24) wants a dry run. **Rejected:** taking a `Database` and building repositories inline, which would put the ORM inside the run loop and make failure injection a monkeypatch |
| D125 | The entry point | `async def run(self, timeframe: Timeframe, now: datetime, *, tickers: Sequence[str] \| None = None) -> RunReport`. The engine **never reads a clock**: `now` is the scheduled candle close #15 fires with, normalized through `to_utc`, and every stored instant comes from the repositories' injected clock. `tickers` restricts the run to a subset of the enabled symbols | Providers already take `now` explicitly (D37) so that retries and simulated clocks stay deterministic; an engine that read the wall clock would break that at the top of the pipeline and make every test time-dependent. The subset is the seam D44 promised #15: the retry of an unpublished candle must reuse the **same** `now`, so it cannot be a fresh run |
| D126 | One configuration snapshot per run | The pause, the enabled tickers of the timeframe and their enabled rules are read in **one** unit of work at the start of the run, into frozen `TickerPlan` records; the rest of the run never reads configuration again | Spec 013's hand-off asks #14 to decide when a configuration change takes effect: the answer is "at the next run", which is what an operator editing the CLI while a run is in flight expects. One unit of work also means one `BEGIN IMMEDIATE` instead of one per ticker, and a run that cannot contradict itself halfway (a rule enabled between two tickers would otherwise be evaluated for some and not others) |
| D127 | How many candles to fetch | `lookback = min(MAX_LOOKBACK, max(max(rule.stable_warmup(), rule.cooldown_bars + 1)))` over the ticker's enabled rules | `stable_warmup` is what specs 005 and 006 require for values that do not depend on where the fetch window starts, and the maximum over the rules lets one fetch serve them all. The `cooldown_bars + 1` term is this spec's addition: the cooldown counts **positions in the evaluated frame** (D129), so a frame shorter than the window cannot tell "the previous signal is inside the cooldown" from "it is older than the frame". With the catalog's bounds the maximum is 2 255 and `MAX_COOLDOWN_BARS + 1` is 501, both far below `MAX_LOOKBACK` (5 000); the cap is kept so a future catalog cannot make the provider raise |
| D128 | Which candles are evaluated (U2) | The **last closed candle only**: one `evaluate(rule, frame)` per rule, never `evaluate_each`. Candles that closed while the bot was down, paused or failing are never evaluated later | A signal is advice about acting now; a bot that comes back after a two-day outage and sends the signals of every missed candle floods the user with advice that is no longer actionable, and the user cannot tell which one is current. Idempotency (rule 5) would not save them: those are different keys, all new. The frames still hold the history the indicators and the cooldown need. **Rejected:** backfilling since the last run, which also makes "how far back" an open-ended configuration question and turns a restart loop into a notification storm |
| D129 | Cooldown semantics (U1) | Counted **by candle position** in the evaluated frame, never by duration. With `previous_label = previous.candle_close_ts - timeframe.duration` and `bars = \|{label in the frame : previous_label < label <= evaluated_label}\|`: no previous signal → `ALLOWED`; `previous_label` after the evaluated label → `SUPERSEDED`; equal → `ALLOWED` (the duplicate must reach `record`); otherwise `ALLOWED` iff `bars > cooldown_bars`. `cooldown_bars = 0` therefore never blocks | Spec 006 fixes the meaning: "the minimum number of closed candles between two notified signals of the same rule and ticker", so `cooldown_bars` candles must lie strictly between them, which is `bars > cooldown_bars`. Spec 004 §5 forbids `(t2 - t1) / duration`, which is wrong across nights, weekends and holidays. `SUPERSEDED` guards the one case the unique constraint cannot: a run with an **older** `now` (a manual replay, a clock correction) would otherwise record and notify a candle older than one already sent. The equality case is deliberately left to `record`, so rule 5 has exactly one arbiter (D112) and the "is_new=False" path is exercised by the ordinary rerun |
| D130 | What the cooldown counts from (U1) | `latest(ticker_id, rule_id)` with `notified_only=False`: the last **recorded** signal, delivered or not | A recorded row is the durable claim that the rule fired on that candle (D115). Counting only delivered ones would turn a single delivery failure into a burst: the rule would fire again on the next candle, and again, until one got through. The cost is the mirror case — after a failed delivery the user hears nothing about that rule for `cooldown_bars` candles — which F7's "signals with `notified_at IS NULL`" alert is meant to surface |
| D131 | The write/send order | Per triggered evaluation: one unit of work does `latest` then `record` and commits; the notification is sent with no session open; `mark_notified` is a unit of work of its own. Only `is_new=True` notifies | Inherited verbatim from spec 014 (§2, D110, D112, D115). It is the only order in which a crash cannot resend: the durable insert is the claim. `BEGIN IMMEDIATE` makes the read-then-write pair race-free, and spec 014 D110 forbids holding two sessions in one thread or one session across an `await`, which is why the notification happens between two units of work and never inside one |
| D132 | What "retried without duplicating" means (U4) | The engine calls `notify` **once** per new signal. Bounded retries of the transport (backoff, rate limits) belong to the `Notifier` implementation (#17), behind that one call. If it still fails, the signal stays recorded with `notified_at` `NULL`, the outcome is `UNDELIVERED` and **no later run re-sends it** | The issue's "if notifying fails it is retried without duplicating the signal" and spec 014's at-most-once policy (U5) meet here: what is retried is the **delivery attempt inside one run**, never the decision to send, because the row is already recorded and `record` is idempotent. A sweep that re-sent `notified_at IS NULL` rows would resend whatever was delivered just before a crash — "sent" and "stamped" can never be one atomic step with Telegram — which is exactly what rule 5 forbids |
| D133 | The global pause (U3) | While `paused_since` is present the run does **nothing**: no fetch, no evaluation, no record, no notification. It returns `RunReport(paused=True)` after one `INFO` record | A pause is "stop telling me things". Evaluating and recording without notifying would fill the history with rows nobody will ever receive (at-most-once means they are never sent later) and would make F7's undelivered-signal alert fire for every paused candle. Skipping the fetch also means a paused bot makes no outbound request at all, which is what an operator pausing during a provider incident wants. The accepted consequence is that candles closed during the pause are never evaluated: after `/resume` the bot speaks about the current candle only, which is the same rule as D128 |
| D134 | Error boundaries | Three rings: **per ticker**, every `MarketDataError` and any other `Exception` raised while fetching or evaluating that ticker (its `FailureKind` in §9); **per signal**, the three "configuration changed" persistence errors and any failure of the notifier; **the run**, everything else — `StoredRuleError`, `StoredSignalError`, `CalendarRangeError`, database failures and `BaseException` propagate out of `run` | The issue requires isolation for tickers, which is where the expected operational failures are (a symbol delisted, Yahoo down, a candle not published). A corrupt stored rule or signal is not a ticker problem: it is corruption that makes the bot's reasoning unsound, and specs 013 and 014 require it to be loud rather than skipped. `CalendarRangeError` is a configuration error (spec 010), and a database failure that is not one of the three known ones means the unit-of-work contract is broken. Accepted consequence: one corrupt history row stops the run for every ticker until an operator fixes it, which is the documented price of D93 |
| D135 | Logging exceptions | No call in `engine/` or `notifications/` passes `exc_info` or `stack_info`. A failure is logged with the exception **class name**, plus the error's own message only for `MarketDataError`, whose messages spec 010 restricts to codes, symbols, timeframe codes and ISO instants | `RedactingFilter` (`logging_setup.py`) rewrites `record.msg` only: a traceback rendered by the formatter from `exc_info` is **not** redacted, and python-telegram-bot puts the bot token in the request URLs that appear in its exceptions, exactly as yfinance does with its session crumb (spec 011). A guard test (AC21) keeps it true. The gap in the filter itself is **not** fixed here: it is issue #50 (user decision, 2026-09-20), and the engine is written so it cannot depend on that fix |
| D136 | The run outcome (U5) | `RunReport`, a frozen tree of `TickerOutcome` and `SignalOutcome` values, returned by `run` and summarized in one `INFO` record. Nothing new is persisted: `bot_state` keeps its closed vocabulary of instants (D118) and no error table is added | The consumers that exist are #15 (which tickers to retry with the same `now`) and the logs; F7 owns alerting and will decide then whether an alert needs history or only the last run. A value object costs nothing, is exactly what tests assert on, and does not commit the schema to a shape F7 has not designed |
| D137 | Concurrency (U6) | Tickers are processed **sequentially**, and so are the rules of a ticker and the signals of a rule | `ProviderTransport` already allows one call in flight per provider and paces attempts at one per second (spec 011), so parallel fetches would queue there; SQLite has one writer and sessions are serialized (D110); and the Raspberry Pi is the deployment target. Sequential also makes a run deterministic, which is what the idempotency and report assertions rely on. The cost is duration — roughly one second of pacing per ticker plus the fetch — which #15 must fit before the next candle close and which a later feature can parallelize behind the same entry point |
| D138 | Where the work runs | Every unit of work runs inside `asyncio.to_thread`, one per block, and the evaluation of one ticker's rules runs in a worker thread of its own; the fetch and the notification are awaited on the event loop | D69 requires it for the blocking `Session`, and the helper that wraps it (`§8.2`) makes "one session per unit of work, never across an `await`, never two at once in one thread" structural rather than a convention. Evaluation is pure CPU work — a few milliseconds per rule on a laptop, more on a Pi — and the loop it would block also drives the Telegram poller (#20); spec 011 D67 already moved comparable work off the loop, and `REGISTRY.compute` is documented safe to call from several threads |
| D139 | What a notification carries | `Notifier.notify(SignalNotification)` where the notification holds the committed `StoredSignal` (key, side, close price, indicator values, the rule's current name and the row id), the parsed `Rule` and the **closed candles the decision was made on** | Everything a message and a chart need comes from the run that decided it: no second Yahoo request (which could return revised candles and draw a chart that contradicts the message), no second database read, and #18 gets the indicator operands from the rule so it can draw them. The frame is shared, not copied: copying it per signal would duplicate up to 2 255 rows for every rule that fires, so the port states that a notifier must treat it as read-only. **Rejected:** passing only a `StoredSignal` (forces #17 to re-fetch), or passing a pre-rendered message (puts presentation in the engine) |
| D140 | Test doubles and layout | `FakeNotifier` (recording, scriptable failures, conformance-checked) joins `tests/fixtures/`, next to `FakeMarketDataProvider` and the repository fixtures, and the multi-run end-to-end scenarios go in the new `tests/integration/` directory that `CLAUDE.md` already reserves | The unit tests keep one behaviour per test with doubles; the end-to-end tests drive a real temporary database, the real `prepare_candles` inside the fake provider and the real repositories over a stream of candle closes, which is the only place where "a restart never resends" can actually be observed. Both run under the same `tests/conftest.py` guards (no network, `TB_DATA_DIR` in `tmp_path`) |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/engine/__init__.py` | developer | New: package docstring, re-exports nothing |
| `src/trading_bot/engine/unit_of_work.py` | developer | New: `EngineRepositories`, `UnitOfWork` (§3) |
| `src/trading_bot/engine/sql_unit_of_work.py` | developer | New: `sql_unit_of_work` (§3.1) |
| `src/trading_bot/engine/planning.py` | developer | New: `TickerPlan`, `RunPlan`, `plan_lookback`, `read_plan` (§4) |
| `src/trading_bot/engine/cooldown.py` | developer | New: `CooldownDecision`, `cooldown_decision`, `cooldown_decisions` (§5) |
| `src/trading_bot/engine/results.py` | developer | New: `RunReport`, `TickerOutcome`, `SignalOutcome` and their enums (§6) |
| `src/trading_bot/engine/signal_engine.py` | developer | New: `SignalEngine` (§8) |
| `src/trading_bot/notifications/__init__.py` | developer | New: package docstring |
| `src/trading_bot/notifications/notifier.py` | developer | New: `Notifier`, `SignalNotification`, `NotificationError` (§7) |
| `tests/fixtures/notifiers.py` | developer | New: `FakeNotifier`, `NotifyCall`, `as_notifier` (§12) |
| `tests/fixtures/engine.py` | developer | New: `engine_harness` and `run_engine` (§12) |
| `tests/unit/test_engine_planning.py` | developer | TDD: T1 |
| `tests/unit/test_engine_cooldown.py` | developer | TDD: T2 |
| `tests/unit/test_engine_cooldown_lookahead.py` | developer | TDD: T3 |
| `tests/unit/test_engine_run.py` | developer | TDD: T4, T5, T6 |
| `tests/unit/test_engine_idempotency.py` | developer | TDD: T7 |
| `tests/unit/test_engine_notifications.py` | developer | TDD: T8 |
| `tests/unit/test_engine_results.py` | developer | TDD: T9 |
| `tests/unit/test_engine_guard.py` | developer | TDD: T10 |
| `tests/integration/test_signal_engine_end_to_end.py` | tester | T11 |
| `tests/unit/test_engine_properties.py` | tester | T12 |
| `tests/unit/test_engine_adversarial.py` | tester | T13 |
| `docs/ARCHITECTURE.md` | developer | §13; explicitly authorized by this spec |
| `docs/specs/015-signal-engine.md` | tech-lead | This spec |

No other file changes. In particular: no migration, no `TB_*`, no dependency, no change to
`domain/`, `data/`, `persistence/`, `cli/`, `main.py`, `config.py`, `Dockerfile`, `deploy/`,
`scripts/` or `.github/`.

### 2. Flow

```text
#15 ──▶ await engine.run(timeframe, now, tickers=None)

  to_thread: with unit_of_work() as repositories:          BEGIN IMMEDIATE (one snapshot, D126)
      state.load()            -> paused?  yes -> RunReport(paused=True), nothing else happens
      tickers.list_enabled(timeframe)
      assignments.rules_for_ticker(id, enabled_only=True)  -> RunPlan(TickerPlan, ...)

  for each TickerPlan, in order (sequential, D137):
      await provider.fetch_candles(symbol, timeframe, plan.lookback, now=now)
          MarketDataError -> classify, log, TickerOutcome(FAILED), next ticker (D134)
      to_thread: evaluate(rule, frame) for each rule        pure, no session (D138)

      for each triggered evaluation:
          to_thread: with unit_of_work() as repositories:   BEGIN IMMEDIATE
              signals.latest(ticker_id, rule_id)            -> cooldown_decision (D129, D130)
                  BLOCKED / SUPERSEDED -> outcome, nothing written, next rule
              signals.record(signal)                        -> RecordOutcome (SAVEPOINT, D112)
          (the block committed: the claim is durable)

          is_new?  await notifier.notify(SignalNotification(stored, rule, frame))   no session open
                   to_thread: with unit_of_work(): signals.mark_notified(stored.id)
          else:    outcome DUPLICATE, nothing is sent

  return RunReport(...)   ──▶ #15: record_run(timeframe, now), retry report.retryable_tickers
                                  with the same now until calendar.next_candle_close(timeframe, now)
```

### 3. `engine/unit_of_work.py`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class EngineRepositories:
    """The ports of one unit of work, as the engine sees them."""

    tickers: TickerRepository
    assignments: AssignmentRepository
    signals: SignalRepository
    state: BotStateRepository


type UnitOfWork = Callable[[], AbstractContextManager[EngineRepositories]]
```

Four ports, not the five of `tests/fixtures/repositories.py`: the engine reads rules through
`AssignmentRepository.rules_for_ticker` and never addresses one by name, so `RuleRepository` would
be a dependency nothing uses. A later consumer that needs it adds it here.

The factory is called once per unit of work and its context manager commits on a clean exit and
rolls back on any `BaseException`, exactly like `Database.session()`. The engine never nests two of
them (D110) and never holds one across an `await` (§8.2 makes both structural).

#### 3.1 `engine/sql_unit_of_work.py`

```python
def sql_unit_of_work(database: Database, *, clock: Clock = system_clock) -> UnitOfWork:
    """A ``UnitOfWork`` over ``database``: one ``Database.session()`` per block (#16 wires it)."""
```

The only module of `engine/` allowed to import `sqlalchemy` or a `Sql*` repository (AC21). It
builds no engine of its own (spec 014 hand-off for #16) and passes one clock to the four
repositories.

### 4. `engine/planning.py`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class TickerPlan:
    ticker_id: int
    symbol: str  # normalized
    timeframe: Timeframe
    rules: tuple[StoredRule, ...]  # enabled, ordered by name, at least one
    lookback: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RunPlan:
    timeframe: Timeframe
    paused_since: datetime | None
    tickers: tuple[TickerPlan, ...]

    @property
    def paused(self) -> bool: ...


def plan_lookback(rules: Sequence[Rule]) -> int:
    """``min(MAX_LOOKBACK, max(max(stable_warmup(), cooldown_bars + 1)))`` (decision D127)."""


def read_plan(
    repositories: EngineRepositories,
    timeframe: Timeframe,
    *,
    tickers: Sequence[str] | None = None,
) -> RunPlan:
    """The whole configuration of one run, read inside one unit of work (decision D126)."""
```

- `plan_lookback` takes the parsed documents (`stored.rule for stored in plan.rules`), so it stays a
  pure function of the rules and is tested on literal `Rule` values without a database.
- `read_plan` reads the bot state first and returns `RunPlan(paused_since=..., tickers=())` without
  reading any configuration when it is paused (D133).
- `tickers` is normalized with `normalize_ticker` (a `TypeError`/`ValueError` before any I/O) and
  intersected with the enabled tickers of the timeframe; a symbol that is not enabled for that
  timeframe is simply absent from the plan.
- A ticker with no enabled rule is dropped: no fetch, no outcome.
- `rules_for_ticker(..., enabled_only=True)` returns rules whose timeframe equals the ticker's, which
  the composite foreign keys of spec 013 (D86) make a schema invariant, so no filtering is needed
  here and none is written.
- `StoredRuleError` from a document that no longer parses propagates (D134).

### 5. `engine/cooldown.py`

```python
class CooldownDecision(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


def cooldown_decision(
    labels: pd.DatetimeIndex,
    *,
    timeframe: Timeframe,
    cooldown_bars: int,
    previous_close: datetime | None,
) -> CooldownDecision:
    """The decision about the **last** label of ``labels`` (the evaluated candle)."""


def cooldown_decisions(
    labels: pd.DatetimeIndex,
    *,
    timeframe: Timeframe,
    cooldown_bars: int,
    previous_close: datetime | None,
) -> tuple[CooldownDecision, ...]:
    """One ``CooldownDecision`` per label, in index order (the look-ahead reference)."""
```

`cooldown_decision(labels, ...) == cooldown_decisions(labels, ...)[-1]` and
`cooldown_decisions(labels[:n], ...) == cooldown_decisions(labels, ...)[:n]`, bitwise, the way
`evaluate` and `evaluate_each` relate (spec 007). The per-row form returns a tuple rather than a
`pd.Series` so that no annotation depends on a pandas-stubs generic parameter for a non-numeric
dtype; the look-ahead test wraps it in an object-dtype `Series` indexed by the labels, which is
what the harness compares.

#### 5.1 Semantics (D129)

With `previous_label = previous_close - timeframe.duration` (exact subtraction, never a division)
and `bars = |{label in labels[: i + 1] : previous_label < label <= labels[i]}|` at position `i`:

| Case | Decision | Engine behaviour |
|------|----------|------------------|
| `previous_close is None` | `ALLOWED` | record and, if new, notify |
| `previous_label > labels[i]` | `SUPERSEDED` | nothing is written or sent; the pair already has a later signal |
| `previous_label == labels[i]` | `ALLOWED` | `record` decides: it returns `is_new=False` and nothing is sent |
| `bars > cooldown_bars` | `ALLOWED` | record and, if new, notify |
| `bars <= cooldown_bars` | `BLOCKED` | nothing is written or sent |

- `cooldown_bars == 0` never blocks (spec 006): `bars >= 1` whenever the previous label is strictly
  earlier.
- A `previous_label` earlier than `labels[0]` counts every row of the prefix, so the decision is
  `ALLOWED` unless the frame is shorter than `cooldown_bars + 1`, which D127 prevents except when
  the provider capped the history; the conservative direction (fewer notifications) is deliberate.
- Labels are compared as instants; both arguments go through `to_utc`, and a naive `previous_close`
  raises `ValueError`. `labels` must be a non-empty, sorted, tz-aware `DatetimeIndex` (the frame's
  index, which `validate_candles` already guarantees); anything else raises `TypeError`/`ValueError`.
- The function reads no clock, no database and no candle values: only labels, a timeframe and an
  integer. It is therefore checked for look-ahead like every per-candle computation (AC7).

#### 5.2 Golden table (AC6, AC8)

`1d` candles on the NYSE grid, labels in UTC, around the 2024-07-04 holiday. The previous signal is
on the candle labelled `2024-07-01T04:00Z`, so `previous_close` is `2024-07-02T04:00Z`:

| Evaluated label | Calendar days since | `bars` | `cooldown_bars=0` | `1` | `2` |
|-----------------|---------------------|--------|-------------------|-----|-----|
| 2024-07-01T04:00Z | 0 | 0 (equal label) | `ALLOWED` | `ALLOWED` | `ALLOWED` |
| 2024-07-02T04:00Z | 1 | 1 | `ALLOWED` | `BLOCKED` | `BLOCKED` |
| 2024-07-03T04:00Z | 2 | 2 | `ALLOWED` | `ALLOWED` | `BLOCKED` |
| 2024-07-05T04:00Z | 4 | 3 | `ALLOWED` | `ALLOWED` | `ALLOWED` |
| 2024-06-28T04:00Z | −3 | — | `SUPERSEDED` | `SUPERSEDED` | `SUPERSEDED` |

The 2024-07-04 holiday and the weekend are the point: on 2024-07-05 a duration-based count would
give four "bars" where three candles closed.

### 6. `engine/results.py`

```python
class FailureKind(StrEnum):
    INVALID_TICKER = "invalid_ticker"
    NO_DATA = "no_data"
    NOT_PUBLISHED = "not_published"
    PROVIDER_DATA = "provider_data"
    UNAVAILABLE = "unavailable"
    UNEXPECTED = "unexpected"


class TickerStatus(StrEnum):
    EVALUATED = "evaluated"
    FAILED = "failed"


class SignalDisposition(StrEnum):
    NOTIFIED = "notified"  # new, recorded, committed and accepted by the notifier
    UNDELIVERED = "undelivered"  # new and recorded; the notifier failed (D132)
    DUPLICATE = "duplicate"  # the key already existed: nothing sent (rule 5)
    COOLDOWN = "cooldown"  # suppressed by cooldown_bars; nothing written
    SUPERSEDED = "superseded"  # a later signal of the pair exists; nothing written
    REJECTED = "rejected"  # the ticker or rule changed after the snapshot (D134)


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalOutcome:
    key: SignalKey
    rule_id: int
    disposition: SignalDisposition
    signal_id: int | None  # the stored row, for every disposition that has one


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerOutcome:
    ticker: str
    ticker_id: int
    status: TickerStatus
    failure: FailureKind | None  # set iff status is FAILED
    retryable: bool  # the class-level flag of the market data error (spec 010)
    rules_evaluated: int
    signals: tuple[SignalOutcome, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReport:
    timeframe: Timeframe
    now: datetime  # the scheduled close, UTC
    paused: bool
    tickers: tuple[TickerOutcome, ...]

    @property
    def notified(self) -> int: ...
    @property
    def failures(self) -> tuple[TickerOutcome, ...]: ...
    @property
    def retryable_tickers(self) -> tuple[str, ...]: ...  # what #15 re-runs with the same `now`
    @property
    def summary(self) -> str: ...  # the one-line record of §10
```

Every record is frozen, slotted, keyword-only and hashable by value; `now` goes through `to_utc`.
The report holds no exception object and no provider text, so it can be logged and, later, turned
into an alert by F7 without re-checking what is safe to print.

### 7. `notifications/notifier.py`

```python
class NotificationError(Exception):
    """A notification could not be delivered. Implementations raise this after their own retries."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalNotification:
    """Everything a message needs, gathered by the run that decided the signal (decision D139)."""

    signal: StoredSignal  # the committed row: key, side, close price, values, rule name, id
    rule: Rule  # the parsed document, for the chart's indicators (#18)
    candles: pd.DataFrame  # the closed candles the decision was made on; read-only, never copied


class Notifier(Protocol):
    """Delivers signals to the user. It never places orders and never decides what to send."""

    async def notify(self, notification: SignalNotification) -> None:
        """Deliver one signal, retrying internally as the implementation sees fit.

        Raises ``NotificationError`` when delivery ultimately failed. The engine logs it, leaves
        the signal with ``notified_at`` ``NULL`` and never re-sends it (decision D132, spec 014
        D115). Implementations must treat ``notification.candles`` as read-only and must never put
        a token, a chat id or any provider text into the exceptions they raise: the engine logs the
        exception class only (D135).
        """
```

`notifications/__init__.py` re-exports nothing and documents the package the way `data/__init__.py`
does. No implementation ships here: #17 adds `TelegramNotifier` in the same package.

### 8. `engine/signal_engine.py`

#### 8.1 Public API

```python
class SignalEngine:
    """Orchestrates one run: fetch, evaluate, suppress, record, notify (issue #14)."""

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        unit_of_work: UnitOfWork,
        notifier: Notifier,
    ) -> None: ...

    async def run(
        self,
        timeframe: Timeframe,
        now: datetime,
        *,
        tickers: Sequence[str] | None = None,
    ) -> RunReport: ...
```

- Arguments are checked before any I/O: `timeframe` must be a `Timeframe` (`TypeError`), `now` goes
  through `to_utc` (`TypeError`/`ValueError`), `tickers` must be a sequence of symbols
  `normalize_ticker` accepts (`TypeError`/`ValueError`); a `str` passed as `tickers` is a
  `TypeError`, not thirty one-letter symbols.
- One `SignalEngine` instance is reused for every run and every timeframe. It holds no mutable
  state between runs: everything a run needs lives in its locals, so #15 can only break rule 7 by
  scheduling two overlapping runs, which `max_instances=1` prevents.

#### 8.2 The run, step by step

1. **Plan.** `plan = await asyncio.to_thread(work)` where `work` opens one unit of work and calls
   `read_plan(repositories, timeframe, tickers=tickers)`. If `plan.paused`, log the `INFO` of §10
   and return `RunReport(timeframe=..., now=..., paused=True, tickers=())`.
2. **Per ticker**, in plan order:
   1. `frame = await self._provider.fetch_candles(plan.symbol, timeframe, plan.lookback, now=now)`.
      A `MarketDataError` is classified (§9), logged and turned into a `TickerOutcome(FAILED)`; the
      loop continues.
   2. `triggered = await asyncio.to_thread(evaluate_rules, plan, frame)` returns, for each rule that
      triggered, the pair `(stored_rule, evaluation)` and the number of rules evaluated. Any
      exception other than the ones of D134 is caught here and in step 1 as `UNEXPECTED`.
   3. For each triggered pair, build the `Signal` of AC4 and run **one** unit of work in a worker
      thread:
      - `previous = repositories.signals.latest(plan.ticker_id, stored_rule.id)` (D130), which is
        `None` for a pair that never fired;
      - `decision = cooldown_decision(frame.index, timeframe=timeframe,
        cooldown_bars=rule.cooldown_bars, previous_close=<the previous candle close or None>)`;
      - `BLOCKED`/`SUPERSEDED`: return the decision, write nothing;
      - otherwise `outcome = repositories.signals.record(signal)` and return it.
      The block commits on exit. `UntrackedTickerError`, `UnknownRuleError` and
      `TimeframeMismatchError` become `REJECTED` (AC15).
   4. When the outcome is `is_new=True`: `await self._notifier.notify(SignalNotification(...))` with
      **no** session open, then one more unit of work for `mark_notified(stored.id)`. A
      `NotificationError` or any other `Exception` from the notifier gives `UNDELIVERED`;
      `UnknownSignalError` from `mark_notified` gives one `WARNING` and keeps `NOTIFIED`.
   5. When it is `is_new=False`: `DUPLICATE`, nothing is sent, one `DEBUG` record with `str(key)`.
      If the stored payload differs from the evaluation just computed (a revised candle), the record
      is a `WARNING` instead (spec 014 hand-off 2).
3. **Report.** Build the `RunReport`, log `report.summary` at `INFO` and return it.

The single helper that runs a unit of work is what keeps D110 structural:

```python
async def _in_unit_of_work[T](self, work: Callable[[EngineRepositories], T]) -> T:
    def run() -> T:
        with self._unit_of_work() as repositories:
            return work(repositories)

    return await asyncio.to_thread(run)
```

`work` is a plain function of the repositories: it cannot await, cannot reach the notifier or the
provider, and cannot open a second unit of work, so "never two sessions at once in one thread" and
"never a session across an `await`" hold by construction.

### 9. Error taxonomy

| Raised by | Error | Ring | `FailureKind` / disposition | Level | Retryable |
|-----------|-------|------|-----------------------------|-------|-----------|
| `fetch_candles` | `InvalidTickerError` | ticker | `INVALID_TICKER` | `WARNING` | no |
| `fetch_candles` | `NoDataError` | ticker | `NO_DATA` | `WARNING` | no |
| `fetch_candles` | `ProviderDataError` | ticker | `PROVIDER_DATA` | `ERROR` | no |
| `fetch_candles` | `CandleNotPublishedError` | ticker | `NOT_PUBLISHED` | `INFO` | **yes** (#15 retries with the same `now`) |
| `fetch_candles` | `ProviderUnavailableError` | ticker | `UNAVAILABLE` | `WARNING` | **yes** |
| fetch or evaluation | any other `Exception` | ticker | `UNEXPECTED` | `ERROR` (class name only) | no |
| `record` | `UntrackedTickerError`, `UnknownRuleError`, `TimeframeMismatchError` | signal | `REJECTED` | `WARNING` | — |
| `notify` | `NotificationError` or any other `Exception` | signal | `UNDELIVERED` | `ERROR` (class name only) | — |
| `mark_notified` | `UnknownSignalError` | signal | stays `NOTIFIED` | `WARNING` | — |
| anywhere | `StoredRuleError`, `StoredSignalError` | run | propagates | `ERROR` | — |
| anywhere | `CalendarRangeError` | run | propagates | `ERROR` | — |
| anywhere | any other `PersistenceError`, `SQLAlchemyError`, `BaseException` | run | propagates | — | — |

`retryable` is taken from the class-level flag of `MarketDataError` (spec 010), never re-derived.
The engine itself defines no new exception class: it classifies, it does not wrap.

### 10. Logging

One logger, `trading_bot.engine`; one line per record; no `exc_info` (D135); only symbols,
timeframe codes, enum values, ISO instants, integers and `str(SignalKey)` (spec 004) ever appear.

| Level | When | Record |
|-------|------|--------|
| `INFO` | a signal was delivered | `notified AAPL\|1d\|7\|2024-07-03T05:00:00+00:00 (BUY, close 187.5)` |
| `INFO` | the run is paused | `1d run at 2024-07-05T20:00:00+00:00: skipped, paused since 2024-07-01T12:00:00+00:00` |
| `INFO` | end of every run | `report.summary`, for example `1d run at 2024-07-05T20:00:00+00:00: 12 tickers, 47 rules, 3 signals (2 notified, 1 duplicate), 1 failed (1 retryable)` |
| `INFO` | an unpublished candle | `candle not published for AAPL 1d: <the error message>; the scheduler will retry` |
| `WARNING` | a ticker failed, a rejected signal, a stored signal that vanished | the `FailureKind` or the key, plus the error message for `MarketDataError` only |
| `ERROR` | `ProviderDataError`, an unexpected ticker failure, a failed delivery | the exception **class** name plus the ticker or the key |
| `DEBUG` | duplicate, cooldown, superseded, a rule that did not trigger a fetch | `str(key)` and the decision |

The one-line summary is the record F7 will parse; its exact text is asserted from literals.

### 11. Guards and typing

`tests/unit/test_engine_guard.py` (developer) scans `engine/` and `notifications/` with an AST
walk, exercised first against synthetic snippets so a green result is not vacuous:

| Rule | Exception |
|------|-----------|
| No clock: `datetime.now`, `datetime.utcnow`, `date.today`, `time.time`, `time.monotonic` | none |
| No `sqlalchemy`, no `trading_bot.persistence.repositories.<implementation>`, no `trading_bot.persistence.database` | `engine/sql_unit_of_work.py` |
| No `telegram`, `yfinance`, `talib`, `exchange_calendars`, `fastapi`, `trading_bot.cli`, `trading_bot.config` | none |
| No mutable module-level state (everything except `__all__` and `Final` constants) | none |
| No `exc_info=` or `stack_info=` keyword in any call | none |
| `domain/` imports neither `engine` nor `notifications` | none |

Typing: strict mypy over `src/` and `tests/fixtures/`, no `Any`, no `type: ignore`. The
`Protocol`-typed factories (`as_notifier`, the provider's `as_market_data_provider`, the
repositories fixture) prove that every double conforms to the port the engine depends on.

### 12. Test fixtures

`tests/fixtures/notifiers.py` (developer):

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class NotifyCall:
    key: SignalKey
    signal_id: int
    rule_name: str
    last_label: datetime  # candles.index[-1], to prove which frame was passed


class FakeNotifier:
    """Records deliveries and raises scripted failures, first in first out. No network, no clock."""

    @property
    def calls(self) -> tuple[NotifyCall, ...]: ...
    def fail_next(self, error: Exception, *, times: int = 1) -> None: ...
    async def notify(self, notification: SignalNotification) -> None: ...


def as_notifier(notifier: FakeNotifier) -> Notifier: ...
```

The fake also asserts, on every call, that the candles it received are the frame the engine
evaluated and that no database session is open in its thread (it opens a probing connection with a
zero busy timeout, the technique spec 014 T3 uses), which is how AC5 is proven rather than assumed.

`tests/fixtures/engine.py` (developer) assembles a harness over the existing fixtures — a temporary
database, `nyse_test_calendar()`, `FakeMarketDataProvider` with `session_candles`/`provider_shaped`
frames, `FakeNotifier`, `sql_unit_of_work` with a `fixed_clock` — and offers `run_engine(harness,
timeframe, now)` running `asyncio.run`. It adds no second database fixture and no second calendar.

### 13. Documentation (`docs/ARCHITECTURE.md`)

1. A new `## Signal engine` section after `## Rule evaluation`: the run pipeline, `run`'s contract,
   the configuration snapshot, lookback planning, the cooldown table of §5.1, the write/send order,
   the error rings of §9, the report and the `Notifier` port, with one short Python example in the
   style of the other sections (it must pass `ruff format --check`).
2. `### What the engine adds`, in `## Rule evaluation`: point at the new section instead of
   describing the behaviour twice.
3. The layers table: the `engine/` row gains the cooldown, the report and the isolation rule; the
   `notifications/` row states that the port ships with #14 and the Telegram implementation with #17.
4. `## Configuration (environment variables)`: unchanged (no variable is added); the sentence that
   says so lives in this spec, not in the document.

## Test plan

Every test runs without network (the autouse guard), without the wall clock (clocks are injected,
instants are literals), without `time.sleep` and without writing outside `tmp_path`. Coroutines run
with `asyncio.run`, as the data-layer tests do.

Mandatory template cases:

- **Anti look-ahead (rule 4):** T3 covers the one new per-candle computation, the cooldown gate
  (`assert_no_lookahead` on `cooldown_decisions`, `assert_no_lookahead_point_in_time` on
  `cooldown_decision`), over every `Scenario` and on calendar-aligned frames. The other half of rule
  4 — only closed candles are evaluated — is inherited: T4 pins that the engine passes `now`
  unchanged to the provider, evaluates the returned frame as is and never re-truncates it, so the
  last row is the last closed candle by the provider's contract.
- **Idempotency (rule 5): the core of the feature.** T7 (rerun, restart, interrupted delivery, the
  configuration changing between runs) and T11/T12 (a stream of runs over a growing frame) are
  dedicated to it, and every one of them asserts both "no new row" and "no notifier call".
- **Authorization: not applicable.** The engine exposes no Telegram, API or dashboard surface, and
  the `Notifier` port carries no chat id; the allowlist is #19's and #21's.
- **Secret redaction (config and logs):** no `TB_*` and no secret is added. T8 asserts that a
  notifier exception carrying a token-shaped string (built at runtime) never reaches the log, that
  no record in the package passes `exc_info` (T10), and that every record is built from safe fields.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 planning | unit | `plan_lookback` on a literal rule table, its empty-sequence `ValueError` and the `MAX_LOOKBACK` cap; `read_plan` in one unit of work (statements counted with `before_cursor_execute`): disabled tickers, disabled rules, other timeframes and ruleless tickers absent; the `tickers` filter and its rejections; a paused state short-circuits before any configuration read; `StoredRuleError` propagates | AC1, AC3, AC16, AC20 | developer |
| T2 cooldown | unit | The table of §5.1 and the golden table of §5.2 for `cooldown_bars` 0, 1, 2 and 500; equality, `SUPERSEDED`, a previous label before the frame start, a one-row frame, a frame with gaps; argument rejections (naive instant, empty index, unsorted, wrong type) | AC6, AC8 | developer |
| T3 cooldown look-ahead | unit | `assert_no_lookahead(cooldown_decisions)` and `assert_no_lookahead_point_in_time(cooldown_decision, reference)` on every `Scenario` and on `session_candles` frames, for several previous closes and cooldowns; a deliberate "peek at the last label" cheat is detected | AC7 | developer |
| T4 the run pipeline | unit | One fetch per planned ticker with the exact arguments; the frame is evaluated unmodified (compared before and after); one evaluation per rule; the `Signal` fields of AC4; the recorded order of operations (plan, fetch, evaluate, record+commit, notify, mark_notified) with no session open during the notification; `mark_notified` stamps from the injected clock | AC2, AC4, AC5, AC11 | developer |
| T5 isolation | unit | Each `MarketDataError` class for the first of three tickers: the other two are notified, the outcome and log level of §9 are right, nothing of the failing ticker is written; an arbitrary `Exception` gives `UNEXPECTED`; `StoredRuleError`, `StoredSignalError` and `CalendarRangeError` propagate; `CancelledError` is not caught | AC12, AC13, AC14 | developer |
| T6 configuration changed mid-run | unit | With a repository double, `record` raising each of the three errors gives `REJECTED`, no notification and a continuing run; a ticker removed between the snapshot and `mark_notified` keeps `NOTIFIED` with a `WARNING` | AC11, AC15 | developer |
| T7 idempotency | unit | The same run twice (same `now`): no row, no call, `DUPLICATE`; after `dispose()` and reopening the database; after a notifier failure in the first run (the row stays `notified_at is None` and is never re-sent); the next candle is a new key and is notified; a run with an older `now` gives `SUPERSEDED` | AC9, AC10 | developer |
| T8 notifications | unit | The `SignalNotification` contents (the committed row, the parsed rule, the evaluated frame); a `NotificationError` and an arbitrary exception both give `UNDELIVERED` and let the run continue; the token-shaped message never reaches the log and no `exc_info` is emitted; `as_notifier` conformance | AC18, AC22 | developer |
| T9 the report | unit | Frozen, slotted, keyword-only, UTC instants; plan order; the derived properties; `retryable_tickers` for a mixed run; the literal `summary` for a full run and for a paused one | AC17 | developer |
| T10 guards | unit | The scanner against synthetic snippets first, then the six rules of §11 over the real packages; `domain/` imports neither package | AC21 | developer |
| T11 end to end | integration | A temporary database, the fake provider over `session_candles` frames and the fake notifier, driven over a **sequence** of candle closes of one session week: the frame grows one candle per run, several tickers and rules, one rule with `cooldown_bars=2`; asserts the notified keys, their order, the cooldown spacing by position, the stored rows and that replaying every run from the beginning notifies nothing new. Includes a restart in the middle and a paused stretch | AC5, AC9, AC16 | tester |
| T12 properties | unit (`@given`) | Over drawn candle streams, rule sets and cooldowns: no key is ever notified twice; the positional gap between two notified candles of one pair is always greater than `cooldown_bars`; every notification has a committed row with `is_new`; replaying a prefix of the runs adds nothing; the set of stored keys after any interleaving of failures equals the set of triggered candles minus the suppressed ones | AC6, AC9, AC10 | tester |
| T13 adversarial | unit | A ticker with twenty rules and one that triggers on every candle; `cooldown_bars=500` with a short history; a rule whose `stable_warmup` exceeds the available candles (never fires, no row); a frame of exactly one candle; a symbol in lower case with whitespace in the configuration; a provider returning the same frame twice; a notifier that mutates the frame it receives (the run still completes and the mutation is reported by the fixture's guard); a notifier that never returns is not covered here (that is #15's deadline) | AC2, AC12, AC13 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC23 |
| V2 coverage | `uv run pytest --cov --cov-report=term-missing`: the new `src/` modules at 100%, no regression elsewhere | AC23 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 25 s and none exceeds 3 s. Reported, never asserted | AC23 |
| V4 typing | `uv run mypy`; `git grep -n "Any"` and `git grep -n "type: ignore"` over the new files empty | AC23 |
| V5 dependencies | `git diff origin/main -- pyproject.toml uv.lock` empty | — |
| V6 idempotency stress | T7 and T11 run 20 times in a row (a shell loop), all green, with the number of runs reported | AC9, AC10 |
| V7 no stray database | The session-end guard is green and `git status --porcelain --ignored` shows no `*.db*` in the working tree | — |
| V8 docs | §13 placement and content; `uv run ruff format --check` on the new Python block | AC24 |
| V9 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC24 |
| V10 secrets and language | `python scripts/secret_scan.py --history`; an English-only review of the whole diff, log literals and test data included; no host, user, IP or absolute infrastructure path anywhere | AC22 |

**Testing rules for this feature:**

- no wall-clock reads and no elapsed-time assertions: `now` is a literal, clocks are injected;
- no `time.sleep` and no barrier: ordering is asserted from the recorded call log of the doubles;
- literal expectations written out, never recomputed with the code under test (log lines, the
  summary, cooldown tables, dispositions);
- every database comes from `tests/fixtures/database.py` under `tmp_path`;
- candle frames come from `session_candles`/`provider_shaped` (calendar-aligned) or
  `synthetic_candles` (look-ahead scenarios); values are synthetic (user decision D2);
- fake tokens are built at runtime (`"123456789" + ":" + "x" * 35`), never written as literals.

**TDD order suggested to the developer:**

1. `results.py` and `unit_of_work.py` with T9 (the value objects the rest returns);
2. `cooldown.py` with T2, then T3 (the look-ahead obligation lands before the orchestration);
3. `planning.py` with T1;
4. the fixtures (`notifiers.py`, `engine.py`), then `signal_engine.py` with T4;
5. T5, T6, T7, T8 (the error rings, then idempotency, then delivery);
6. `sql_unit_of_work.py` and T10, then the documentation.

## Risks and security

- **A run may not fit before the next candle close.** Sequential tickers plus the transport's
  one-per-second pacing put a floor of roughly one second per ticker on top of the fetch. On `1h`
  there is an hour of margin, so only a very large watchlist is at risk. Mitigation: #15 owns the
  deadline, `max_instances=1` prevents overlap, and the report tells it which tickers to retry.
  Parallelism can be added later behind the same entry point (D137).
- **A crash between recording and sending loses that notification** (spec 014 D115/U5, D132). The
  row stays visible with `notified_at` `NULL` and F7 alerts on it. It is the price of never
  resending (rule 5).
- **Tracebacks are not redacted.** `RedactingFilter` rewrites `record.msg` only, so an `exc_info`
  traceback reaches the output unfiltered, and Telegram exceptions can carry the bot token in a
  URL. The engine never passes `exc_info` (D135) and a guard test enforces it, so this feature is
  safe without a change to the filter. **Fixing the filter is out of scope here and is tracked by
  #50** ("Redact secrets in logged tracebacks, not only in the message", M5), which records both
  carriers — the Telegram token in API URLs and the yfinance session crumb — and names D135 as the
  mitigation that keeps this feature safe meanwhile. D135 is revisited once the formatter itself
  redacts tracebacks; nothing in this feature depends on that fix.
- **A corrupt stored rule or signal stops the whole run** (D134). It is the documented consequence
  of D93 and D119: a rule that silently stops firing is the worst failure for a signal bot. The
  operator sees one `ERROR` naming the row, and the CLI (`signals list`, `rules list`) is how they
  find it.
- **The cooldown depends on the fetched window.** If a provider limit caps the history below
  `cooldown_bars + 1` candles, the gate errs towards `BLOCKED` (fewer notifications), never towards
  extra ones. Documented in §5.1.
- **A ticker or rule removed mid-run** yields `REJECTED` rather than a stored signal that names the
  wrong parent (spec 014 D113/D114). Nothing is notified for it.
- **Re-adding a removed symbol** starts a new history and can be notified for a candle the removed
  ticker already was (spec 014 D113). Unchanged here.
- **Public repository.** No fixture, log line or example in this feature carries market data, a
  host, a user, a path or a token; test tokens are built at runtime.
- **Supply chain.** No dependency is added or changed.
- **Unbreakable rules.** All preserved:
  - **signal-only:** the engine decides what to *tell* the user; no order, broker, account or
    credential concept exists in it, and the `Notifier` port can only deliver a message;
  - **pure `domain/`:** the engine lives in `engine/`, imports `domain` and never the reverse, and
    the purity guard is untouched; the new guard adds the same discipline to `engine/`;
  - **closed candles only:** frames come from `prepare_candles` (spec 010), are evaluated as
    returned and are never re-truncated; the cooldown gate carries the mandatory look-ahead test;
  - **idempotency:** the unique constraint of spec 014 stays the arbiter, the claim is committed
    before anything is sent, only `is_new=True` notifies, and the rerun path is tested end to end;
  - **UTC:** `now` and every instant go through `to_utc`; the engine reads no clock at all;
  - **single worker:** the engine holds no cross-run state and never starts a task or a thread of
    its own beyond `asyncio.to_thread`; one run per timeframe is #15's `max_instances=1`;
  - **no `eval`:** rules arrive parsed from the repository and are evaluated by the #7 evaluator;
  - **English only.**

### Hand-off list for #15, #16, #17/#18 and F7

**#15 (scheduler):**

1. Call `run(timeframe, now)` with the **scheduled candle close** as `now` (`calendar.candle_slot`
   / `next_candle_close`, plus the configured delay for the *firing* time only — `now` itself must
   stay the close, or the provider's "last closed candle" check moves).
2. Retry `report.retryable_tickers` with `run(timeframe, now, tickers=...)` and the **same** `now`,
   with backoff, until a deadline no later than `calendar.next_candle_close(timeframe, now)`
   (spec 010 D44); then skip and log.
3. Call `record_run(timeframe, now)` once the run and its retries are finished, in its own unit of
   work; it is monotonic, so a late or repeated report is harmless. Decide — and state in the spec —
   whether a paused run counts as a run, since F7's "this timeframe did not run" alert reads it.
4. `max_instances=1`, `coalesce=True` and a documented misfire policy: two overlapping runs of one
   timeframe would not corrupt anything (the constraint arbitrates) but would double the provider
   load and the log noise.
5. The scheduler owns the wall clock; the engine never reads one.

**#16 (wiring):** build one `Database`, one `MarketCalendar`, one provider inside the running loop,
one notifier and one `SignalEngine` with `sql_unit_of_work(database)`; shut down in reverse order;
never a second engine or a second database handle.

**#17, #18 (Telegram):** implement `Notifier.notify`; own the delivery retries and the rate limits
behind that one call (D132); raise `NotificationError` when they give up; never put a token, a chat
id or provider text in an exception message, because the engine logs the class name only; treat
`notification.candles` as read-only and copy before drawing; read the rule's operands from
`notification.rule` for the chart, and present the candle by its label or session date, never the
nominal close (spec 004 §5).

**F7 (#26, #27):** the `RunReport` is the alert source — failed tickers by `FailureKind`, repeated
`NOT_PUBLISHED`, signals left `UNDELIVERED` and signals with `notified_at IS NULL`. If aggregation
needs history, add the table there (D136).

**#50 (redaction of tracebacks):** once the formatter redacts `exc_info` output, D135 can be
revisited; until then the ban on `exc_info`/`stack_info` in `engine/` and `notifications/` is what
keeps a bot token out of the logs, and its guard test must not be relaxed.

## User decisions

- **D1 (2026-09-14):** one PR per issue; M4 is #14 → #15 → #16, each with a branch and a PR of its
  own.
- **D2 (2026-09-14):** synthetic data only in tests. No fixture of this feature contains market
  data.

The seven product questions this spec opened were answered on **2026-09-20**. The user confirmed
the recommended answer to each, so the design above needed no change; they are recorded in full,
with the alternative that was turned down, so a later reader can trace every behaviour of a run to
a decision.

- **U1 (2026-09-20) — cooldown semantics: the recommendation.** `cooldown_bars` candles must lie
  **strictly between** two signals of the same ticker and rule (`bars > cooldown_bars`, so `0`
  means "no cooldown" and `1` means "skip the next candle"), counted **by position** in the
  evaluated frame, and measured from the last **recorded** signal whether or not it was delivered.
  `cooldown_bars` stays where spec 006 put it: a field of the rule document, per rule, in
  `[0, 500]`. **Turned down:** measuring from the last *notified* signal, which retries the rule on
  the next candle after a delivery failure but can turn one outage into a burst. (D129, D130)
- **U2 (2026-09-20) — a run evaluates only the last closed candle.** After an outage, a pause or a
  failure, the candles missed in between are never evaluated later. **Turned down:** backfilling
  every candle closed since the last run, which would make a restart send a burst of advice that is
  no longer actionable. (D128)
- **U3 (2026-09-20) — the global pause suppresses the whole run:** no request, no evaluation, no
  recording, no notification, so nothing stale is sent after `/resume`. **Turned down:** evaluating
  and recording without notifying, which fills the history with rows that will never be delivered
  (delivery is at-most-once) and makes the undelivered-signal alert useless. (D133)
- **U4 (2026-09-20) — what "if notifying fails it is retried" means: the delivery attempt, never
  the decision to send.** The `Notifier` implementation retries internally (backoff, rate limits,
  #17) behind one `notify` call; the signal is already recorded, so a retry cannot duplicate it;
  and a signal still undelivered when the run ends is **never re-sent by a later run** (spec 014
  U5, at-most-once). It stays visible in the history with `notified_at` `NULL` for F7 to alert on.
  **Turned down:** a sweep that re-sends undelivered rows, which can deliver the same signal twice
  after a crash. (D132)
- **U5 (2026-09-20) — run errors go to the structured log and to the in-memory `RunReport`** that
  #15 and F7 consume; **nothing new is persisted** in this feature. **Turned down:** an errors
  table now, before F7 has designed what it needs to alert on. (D136)
- **U6 (2026-09-20) — tickers are processed one at a time.** The provider transport already
  serializes and paces its calls, SQLite has one writer, and the target is a Raspberry Pi; the cost
  is roughly one second per ticker of pacing, which matters only for a large watchlist on the `1h`
  timeframe. **Turned down:** a bounded pool, which buys little while the transport is the
  bottleneck and makes runs non-deterministic. (D137)
- **U7 (2026-09-20) — a run whose `now` is older than an existing signal of the pair is skipped**
  (`SUPERSEDED`): nothing is recorded and nothing is sent, because notifying yesterday's signal
  today is misleading advice. **Turned down:** recording and notifying it, since its key is new and
  rule 5 does not forbid it. (D129)
- **Follow-up (2026-09-20):** the `RedactingFilter` traceback gap is **not** fixed in this PR; it is
  issue #50, and D135 (no `exc_info`/`stack_info` in `engine/` and `notifications/`) plus its guard
  test are the mitigation that keeps this feature safe meanwhile.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames, users or absolute infrastructure paths
- [ ] Tests cover the acceptance criteria and fail without the implementation
- [ ] The order is read + `record` + commit → notify → `mark_notified`, with no session open during
      the notification, and only `is_new=True` notifies
- [ ] The cooldown counts candle positions, never a duration division, and carries its look-ahead
      test
- [ ] A failing ticker does not stop the run; `StoredRuleError`, `StoredSignalError` and
      `CalendarRangeError` are never swallowed
- [ ] The engine reads no clock and holds no state between runs; every unit of work is short, in a
      worker thread, and never nested
- [ ] No `exc_info` in `engine/` or `notifications/`; no provider or notifier text in any log record
- [ ] `engine/` depends on ports only; SQLAlchemy appears in `sql_unit_of_work.py` alone
- [ ] No `Any`, no `type: ignore`; the new guard, the purity guard and the persistence guard are
      green
- [ ] Scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
