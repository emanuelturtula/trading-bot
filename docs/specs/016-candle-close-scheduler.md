# 016 — Scheduler aligned to candle close and market hours

- **Status:** approved (the seven product questions are answered; see "User decisions", U1–U7,
  2026-09-20)
- **Branch:** `feature/candle-close-scheduler`, from `origin/main` at `1f1ae15` (v0.12.0)
- **Spec author:** tech-lead
- **Issue:** #15 (milestone M4 · Signal engine), the second of #14 → #15 → #16
- **Expected commit type:** `feat:`. The change adds the `src/trading_bot/scheduler/` package, two
  non-secret `TB_*` settings and one runtime dependency (`apscheduler` 3.11), which changes the
  published image. No migration. Nothing is wired into `main.py` yet (#16 does that), so the
  running application still starts no job. `feat` → minor bump. Suggested squash subject:
  `feat: add the candle-close scheduler (#<n>)`.

## Goal

Fire `SignalEngine.run` at the right moment and exactly once per closed candle: at the real
session close of each timeframe plus a configured delay, only on days and hours the market is
open, never twice for the same candle and never two runs of the same timeframe at once.

- The **market calendar** (spec 009) owns when a candle closes; the scheduler adds the delay,
  turns it into a firing time and keeps recomputing it. Nominal closes are never used (spec 004).
- The engine reads no clock (spec 015, D125): the scheduler passes the **scheduled candle close**
  as `now`, never the firing time, so the provider's "last closed candle" check does not move.
- The scheduler owns the bounded retry window for the tickers whose candle the provider has not
  published yet (spec 010, D44), always with the **same** `now`.
- It reports each completed run with `record_run(timeframe, now)` (spec 014, D118), so F7 can
  detect a timeframe that stopped running.

## Out of scope

- **Wiring** (#16): nothing is built in `main.py` or in the FastAPI lifespan, `/health` is
  unchanged and no module of the running application imports `scheduler/` yet. This feature only
  ships the package, its settings and the hand-off list §14.
- **The engine pipeline** (#14, merged): what a run does, the cooldown, idempotency, the report
  and the error rings are spec 015's and are not reopened. This feature only calls `run` and reads
  `RunReport`.
- **Backfilling candles missed while the process was down, paused or failing.** A run always
  evaluates the last closed candle only (spec 015, D128/U2). The startup catch-up of §8.3 is not a
  backfill: it offers **one** run for the candle that is closed right now, never a sequence.
- **Re-sending signals left undelivered.** Delivery is at-most-once (spec 014, D115; spec 015,
  D132); the scheduler never sweeps `notified_at IS NULL` rows and never re-notifies.
- **The heartbeat, alerting and metrics** (F7, #26, #27): `record_heartbeat` is not called here and
  no alert is emitted; `RunReport` and the records of §11 are the sources F7 will read.
- **Telegram, the API and the dashboard**: no command, endpoint or user-facing string is added, so
  no chat allowlist or authorization surface exists in this feature.
- **A second provider, a second calendar, a second engine or a persistent job store.**
- **New indicators, rules, migrations or changes to** `domain/`, `data/`, `persistence/`, `cli/`,
  `api/`, `notifications/`, `deploy/`, `scripts/`, `.github/` and `docs/ROADMAP.md` (the lead marks
  F4 completed after #16). `docs/ARCHITECTURE.md` changes only as §13 states, and `engine/` changes
  only as D158 states.

## Acceptance criteria

"Fire time" means the instant a job is scheduled for, `slot.close_time + close_delay`. "Scheduled
close" means the `now` the engine receives. "Simulated clock" means the injected `Clock` and
`Sleep` doubles of §12: no test reads the wall clock and no test sleeps.

### The schedule

- [ ] **AC1 (fire time):** for every timeframe and every session of the test calendar,
  `next_fire(calendar, timeframe, after=..., delay=...)` returns the first `CandleSlot` whose
  `close_time + delay` is **at or after** `after`, together with that instant. The close is the
  calendar's real close: `nominal_close` appears nowhere in `scheduler/` (grep, part of AC20).
- [ ] **AC2 (calendar days):** the golden table of §7.2 is pinned literally for `1h`, `4h` and
  `1d`: a regular session (including its truncated final `1h` slot), the first and last fire of a
  session, a weekend, the 2024-07-04 holiday, the 2024-07-03 half day, the EST→EDT change
  (2024-03-08 → 2024-03-11) and the EDT→EST change (2024-11-01 → 2024-11-04). `1d` fires after the
  **session close**, never at midnight and never at the next session's open (U6).
- [ ] **AC3 (progress and no look-ahead, `CLAUDE.md` rule 4):** over a full year of the test
  calendar, the sequence produced by feeding each fire time back as `after` (plus one microsecond)
  is strictly increasing, visits every slot of the grid exactly once in label order (except the
  slots AC4 skips), never lands on a closed day, and satisfies
  `slot.close_time + delay == fire` and `calendar.closed_candles(timeframe, fire, 1)[-1] == slot`
  for every element: at the firing instant the candle the run is about is already closed, for
  `delay` values `0 s`, `120 s` and `900 s`.
- [ ] **AC4 (slots that are never published):** with `should_run=skip_unpublished_hours(calendar)`,
  the truncated final `1h` slot of an early-close session (`is_unpublished_hour`, spec 011 D65) is
  not fired for, and no other slot is skipped — in particular the truncated 15:30–16:00 ET slot of
  a regular session is fired for. `skip_unpublished_hours` answers `True` for every `4h` and `1d`
  slot without raising.
- [ ] **AC5 (outside the calendar):** when the calendar cannot answer (the instant or the next
  close is outside its coverage), `next_fire` raises `CalendarRangeError`, and
  `CandleCloseTrigger.get_next_fire_time` turns it into `None` plus **one** `ERROR` record naming
  the timeframe and the instant, so APScheduler drops that job instead of raising inside its loop.

### One run per fire

- [ ] **AC6 (the scheduled close is the `now`, spec 015 hand-off 1):** a fire calls
  `engine.run(timeframe, slot.close_time)` exactly once with the close of
  `calendar.closed_candles(timeframe, clock(), 1)[-1]`, an aware UTC instant that is **at or
  before** `clock()` and is never the firing time. A clock that has moved far past several closes
  still runs only the last closed candle (D128, no backfill).
- [ ] **AC7 (exactly once per close, issue AC "exactly once"):** the runner reads
  `state.last_run(timeframe)` before the run and returns `ALREADY_RUN` without calling the engine
  when `scheduled_at >= slot.close_time`; after a successful run (retries included) it calls
  `record_run(timeframe, slot.close_time)` **once**, in a unit of work of its own. Two fires for
  the same close, in any order and including a startup catch-up that coincides with a scheduled
  fire, produce exactly one engine run.
- [ ] **AC8 (the misfire window):** a fire whose `clock()` is later than
  `close + close_delay + misfire_grace` returns `STALE`, calls no engine and records no run; one
  inside the window runs normally. The same rule governs the startup catch-up (AC15).
- [ ] **AC9 (the bounded retry window, spec 010 D44):** when a report has
  `retryable_tickers`, the runner re-runs `engine.run(timeframe, close, tickers=<those symbols>)`
  with the **same** `close`, waiting the policy's backoff `(30 s, 60 s, 120 s, 240 s)` between
  attempts through the injected `sleep`, stopping as soon as a report has no retryable ticker, and
  never starting an attempt at or after the deadline
  `min(calendar.next_candle_close(timeframe, close), first_attempt_started + retry_window)`.
  Symbols still unresolved when the budget ends give **one** `WARNING` naming them and appear in
  `RunAttempt.unresolved`. Nothing is retried when the report has no retryable ticker.
- [ ] **AC10 (never two concurrent runs, issue AC):** a second `run_timeframe` for a timeframe
  whose run is in flight returns `BUSY` immediately, calls no engine and logs one `WARNING`; runs
  of **different** timeframes never overlap (the recorded call log of a fake engine shows no
  interleaving), and the global lock is released while the retry backoff sleeps, so a `1d` fire
  is not blocked for minutes by a `1h` retry window. The APScheduler jobs carry `max_instances=1`
  and `coalesce=True` (AC14) as the second line of defence.
- [ ] **AC11 (the global pause):** with `paused_since` present the engine returns
  `RunReport(paused=True)`; the runner performs no retry, still calls `record_run` (U4) and returns
  `RAN`. The candles closed during the pause are never evaluated afterwards.
- [ ] **AC12 (no exception escapes to APScheduler):** any `Exception` raised by the engine, the
  calendar or the repositories inside `run_timeframe` is caught, logged as **one** `ERROR` naming
  the exception **class** and the timeframe (never `exc_info`, never the exception text), returns
  `FAILED`, records **no** run, and leaves the scheduler firing normally at the next close. Only
  `BaseException` — `asyncio.CancelledError` included — propagates, and it is never caught. This is
  a security requirement, not only a robustness one: APScheduler logs an escaped job exception with
  `logger.exception`, and `RedactingFilter` does not redact tracebacks (issue #50, spec 015 D135).
- [ ] **AC13 (the run outcome):** `RunAttempt` and `FireStatus` are a frozen, slotted,
  keyword-only dataclass and a `StrEnum`; the attempt carries the timeframe, the scheduled close,
  the status, the number of engine calls, the last `RunReport` (or `None`) and the unresolved
  symbols, and holds no exception object and no provider text.

### The service

- [ ] **AC14 (the jobs):** `start()` adds exactly one job per timeframe of `timeframes`, with id
  `candle-close.<timeframe>`, a `CandleCloseTrigger` for that timeframe, `max_instances=1`,
  `coalesce=True`, `misfire_grace_time=int(policy.misfire_grace.total_seconds())`, the default
  in-memory job store and `timezone=UTC`; `start()` on a running service raises `RuntimeError` and
  adds nothing. `next_fire_times()` reports one aware UTC instant (or `None`) per timeframe.
- [ ] **AC15 (restart in the middle of a session, issue AC):** at `start()` each timeframe is
  offered one immediate catch-up run, which the AC7 and AC8 rules accept or reject. Pinned with a
  simulated clock: no recorded run and a fresh close inside the window → one run; a recorded run
  equal to the last closed candle → none; a recorded run older than the last closed candle but the
  close already outside the window → none; a restart between two closes of a session (the last
  candle already run) → none, and the next scheduled fire is the next close.
- [ ] **AC16 (shutdown):** `await aclose()` stops new fires, makes a retry window in flight abort
  before its next sleep, waits for the run in flight for at most `policy.shutdown_timeout` (one
  `WARNING` if it does not finish), is idempotent, and afterwards no code path of the package can
  open a unit of work — so #16 can dispose the database right after. `aclose()` without `start()`
  is a no-op.
- [ ] **AC17 (it really fires):** one test starts a real `AsyncIOScheduler` through
  `SchedulerService` with a stub trigger that fires almost immediately, waits on an
  `asyncio.Event` for the runner to be called, and proves that `aclose()` stops further calls. No
  assertion on durations.

### Configuration, dependency, guards and scope

- [ ] **AC18 (the settings):** `TB_CANDLE_CLOSE_DELAY_SECONDS` (int, default 120, `[0, 900]`) and
  `TB_SCHEDULER_MISFIRE_GRACE_SECONDS` (int, default 900, `[60, 3600]`) are read by `Settings`,
  reject out-of-range and non-integer values, are **not secrets** (absent from `secret_values()`,
  present in their `repr`) and appear commented in `.env.example` with their default. No secret is
  added by this feature.
- [ ] **AC19 (the dependency):** `apscheduler>=3.11,<4` is added to `[project].dependencies`, the
  lock file is updated in the same commit, the resolved transitive packages are reported by the
  tester, and the runtime stage of the `Dockerfile` gains an import smoke check that fails the
  arm64 build if the package or the scheduler package cannot be imported. `pandas-ta` is not added
  and no other dependency changes.
- [ ] **AC20 (the guard):** an AST scan of `scheduler/` proves that no module reads the clock
  directly (`datetime.now`, `datetime.utcnow`, `date.today`, `time.time`, `time.monotonic`: the
  clock is always the injected one), that `apscheduler` is imported only by `trigger.py` and
  `service.py`, that `trading_bot.data.yahoo` is imported only by `slots.py`, that `sqlalchemy`,
  `trading_bot.persistence.repositories.<implementation>`, `trading_bot.persistence.database`,
  `telegram`, `yfinance`, `talib`, `fastapi`, `trading_bot.config` and `trading_bot.cli` are
  imported nowhere, that `nominal_close` and `time.sleep` appear nowhere, that no module holds
  mutable module-level state and that no call passes `exc_info` or `stack_info`. `domain/` imports
  nothing from `scheduler/`, and the existing purity, persistence and engine guards stay green.
- [ ] **AC21 (typing, gate and coverage):** `uv run python scripts/check.py` is green; strict mypy
  passes with no `Any` and no `type: ignore` in the new `src/` files and fixtures (the APScheduler
  import is made typeable through `pyproject.toml` overrides, D156); the new modules report 100%
  coverage and overall coverage does not regress.
- [ ] **AC22 (docs and scope):** `docs/ARCHITECTURE.md` changes exactly as §13 states, and
  `git diff origin/main --name-only` plus `git status --porcelain` list only the files of Design §1.

## Design

### 0. Decisions

Decisions D1–D140 are recorded in specs 003–015. This spec relies on D19 (half-open boundaries and
`next_candle_close` strictly after `now`), D21 (canonical labels), D22 (identity stays nominal),
D28 (regular hours only), D33/D44 (who retries an unpublished candle), D58/D65 (the half-day `4h`
and `1h` slots), D69 (synchronous SQLAlchemy behind `asyncio.to_thread`), D110 (one session per
unit of work, never two at once in one thread), D118 (`bot_state` and the monotonic `record_run`),
D125 (`run(timeframe, now)` with the scheduled close), D128 (the last closed candle only), D133
(the pause), D135 (no `exc_info`) and D136 (`RunReport`). The new decisions are **D141–D158**; the
next free identifier after this spec is **D159**. The user decisions of 2026-09-20 confirm D142
(U6), D144 (U4), D145 (U2), D149 (U3, U5), D150 (U7) and D153 (U1).

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D141 | Layering | `scheduler/` depends on **ports only**: a `SignalRunner` `Protocol` (what `SignalEngine.run` offers), the `UnitOfWork` of spec 015, `MarketCalendar`, the injected `Clock` and `Sleep`, and a `SlotPredicate`. The schedule arithmetic is a pure function (`next_fire`) that does not import APScheduler; APScheduler appears only in `trigger.py` and `service.py`, and the Yahoo quirk only in `slots.py` | It is the shape the rest of the project uses, and it is what makes the feature testable: the whole schedule, the retry window and the concurrency rules are exercised without starting a scheduler, and the library can be replaced by rewriting two modules. A guard test (AC20) keeps the boundary honest |
| D142 | The trigger (U6) | One **custom `BaseTrigger`** (`CandleCloseTrigger`) per timeframe, computing `slot.close_time + close_delay` from the calendar, instead of `cron`/`interval` triggers or a one-shot job that reschedules itself | The grid is irregular by nature — holidays, half days, DST, a final slot truncated to 30 minutes — and no cron expression describes it; a self-rescheduling one-shot job would reimplement `coalesce`, `max_instances` and the misfire policy the issue asks for. A trigger keeps APScheduler in charge of those and keeps one job with a stable id for `/status` (#21) |
| D143 | Where `now` comes from | The job carries **no** fire time. At each fire the runner derives the candle from the injected clock: `slot = calendar.closed_candles(timeframe, clock(), 1)[-1]` and `now = slot.close_time` | APScheduler 3 does not hand the scheduled run time to the job, and deriving it has a better property anyway: a coalesced burst, a fire delayed by a busy loop and the startup catch-up all evaluate the **last closed candle**, which is exactly D128. One code path serves the scheduled fire, the catch-up and any future manual trigger, and the value passed to the engine is always a real close of the calendar |
| D144 | Exactly once (U4) | Before running, the runner reads `bot_state.last_run(timeframe)` and skips when `scheduled_at >= slot.close_time`; `record_run(timeframe, close)` is written only **after** the run and its retries finish, never before and never when the run failed | The issue asks for "exactly once", and `max_instances=1` alone cannot give it: it says nothing about a catch-up that coincides with a scheduled fire, about a clock stepped backwards by NTP, or about two fires separated by a restart. The state row is the durable record of what already ran, it is monotonic (D118), and the check costs one short unit of work per fire. The engine's unique constraint remains the arbiter of rule 5 underneath, so a duplicate run would be harmless anyway — this makes it also silent and cheap |
| D145 | How many jobs | **One job per `Timeframe` member, always**, not only for timeframes that have enabled tickers (U2) | The engine already turns an empty configuration into a no-op: `read_plan` reads the state and the enabled tickers in one short unit of work and returns an empty plan, with no request and no notification. Making the job set depend on the configuration would need either an event from the writer — impossible, since the management CLI runs in **another process** — or polling, and it would add a window in which a ticker added at 10:05 is ignored until the jobs are rebuilt. The cost of the chosen answer is about ten short reads per day |
| D146 | Misfire policy | `coalesce=True` and `misfire_grace_time = TB_SCHEDULER_MISFIRE_GRACE_SECONDS` (default 15 minutes) on every job, **and** the same window applied by the runner itself: a fire arriving later than `close + close_delay + misfire_grace` returns `STALE` and does nothing | Coalescing is the only correct answer with D128: several missed fires describe the same "evaluate the last closed candle" work, and running it once is enough. The grace window bounds how stale advice may be: a deploy or a restart of a few minutes still gets its run, while a bot that comes back the next morning does not send yesterday's daily signal as if it were fresh. Applying the window in the runner as well covers the startup catch-up, which APScheduler knows nothing about, and keeps one number to reason about |
| D147 | Catch-up at startup | `start()` adds, besides the recurring job, **one** immediate one-shot job per timeframe that calls the same runner. The D144 and D146 rules accept or reject it. There is no other catch-up | The job store is in memory (D148), so after a restart APScheduler has no missed fire to replay: without this, a container restarted at 21:10 UTC would ignore the daily candle that closed at 21:00 and wait a full day. One offered run per timeframe, filtered by the same two rules as any other fire, is the smallest thing that covers "restart in the middle of a session" (issue AC) without ever becoming a backfill |
| D148 | No persistent job store | The default `MemoryJobStore`; the trigger is never pickled and no job survives the process | A persistent store would have to pickle a `CandleCloseTrigger` holding a 100-year `MarketCalendar`, and it would resurrect jobs built by an older version of the code. The durable state the bot needs across restarts is already in `bot_state` (D118) and is what D144 and D147 read |
| D149 | The retry window | The runner retries only `report.retryable_tickers`, with the **same** `now`, with the fixed backoff `(30 s, 60 s, 120 s, 240 s)` and a deadline `min(calendar.next_candle_close(timeframe, close), started + retry_window)` (`retry_window` 10 minutes). `CandleNotPublishedError` and `ProviderUnavailableError` are retried the same way, because the report only distinguishes `retryable` (U3) | Spec 010 D44 assigned this window to #15 and fixed the hard bound (never overlap the next run). The extra `retry_window` bound is this spec's: `next_candle_close` is up to a day away for `1d` and up to eighteen hours away for the last `1h` slot of a session, and retrying a delisted-looking symbol for that long is noise, not resilience. A fixed backoff table instead of a fourth `TB_*` variable keeps the operator's surface at two knobs; it is a literal of `SchedulerPolicy`, so a later feature can make it configurable without changing a signature |
| D150 | Slots never published (U7) | A `SlotPredicate` injected into the trigger and the runner decides whether a slot is fired for at all. `scheduler/slots.py` provides `skip_unpublished_hours(calendar)`, the only module that knows about the provider, and #16 wires it | Spec 011 (D65, hand-off) says Yahoo never publishes the 12:30–13:00 ET half hour of an early-close session as hourly data, and asks #15 to skip it instead of retrying. Not firing at all is strictly better than firing and suppressing the retry: no request, no failure, no log noise, three times a year. Keeping the predicate injected keeps `trigger.py` and `runner.py` provider-agnostic, and a second provider only replaces one adapter |
| D151 | No `is_open` gate | Market hours are honoured by construction — every fire time comes from a slot of the calendar grid — so the scheduler never calls `is_open` to decide whether to run | It is the trap the issue's "only during market sessions" invites: the fire for the last candle of a session happens **after** the close by definition (close plus the delay), so an `is_open` gate would silently drop the most interesting candle of every day, and on `1d` every single run |
| D152 | Injected clock and sleep | `Clock` (reused from `persistence/clock.py`) and `type Sleep = Callable[[float], Awaitable[None]]`, both injected, defaulting to `system_clock` and `asyncio.sleep`. `time.sleep` is banned by the guard | The issue asks for an injectable clock. Reusing the existing `Clock` avoids a second definition of the same type, and injecting the sleep makes the retry window's timing assertions exact and instantaneous: tests record the waits instead of enduring them. Only APScheduler's own loop reads the wall clock, which is why the schedule logic is a pure function tested apart from it |
| D153 | Configuration (U1) | Two non-secret integers in `config.py`: `TB_CANDLE_CLOSE_DELAY_SECONDS` and `TB_SCHEDULER_MISFIRE_GRACE_SECONDS`. `scheduler/` receives a validated `SchedulerPolicy` and never imports `trading_bot.config` | Same shape as the rest of the project: settings are read at the edge and injected. It keeps the package importable and testable without an environment, and lets #16 (or a future CLI dry run) build a policy from anywhere. Both values are operational, not secret, so they are plain integers with bounds, never `SecretStr` |
| D154 | Logging | One logger, `trading_bot.scheduler`; one line per record; no `exc_info`/`stack_info`; only timeframe codes, ISO instants, symbols, enum values and integers. Every `Exception` is caught inside the job (D155) so APScheduler never logs one | D135's reason applies unchanged — `RedactingFilter` rewrites `record.msg` only, so a traceback is not redacted (#50) — and it applies with more force here: APScheduler's executor logs an escaped job exception **with** its traceback, which would print anything the engine or a notifier put in an exception, including a bot token in a request URL |
| D155 | Error boundaries | `run_timeframe` catches `Exception`, logs one `ERROR` with the class name and returns `FAILED`; it never catches `BaseException`. A failed run records nothing, so the next fire (or the next startup) may run that candle again if it is still inside the window | The scheduler is the process's heartbeat: one bad run — a corrupt stored rule (spec 013 D93), a database error, a calendar gap — must not stop the other timeframes or the following closes. Cancellation is not an error: it is how #16 shuts the bot down, and swallowing it would hang the lifespan |
| D156 | The dependency | `apscheduler>=3.11,<4`, pure Python. mypy is taught about it with `follow_untyped_imports` and `untyped_calls_exclude` in `pyproject.toml`, exactly as `exchange_calendars` and `yfinance` already are; no `type: ignore` is added. APScheduler 4 is not used | 3.11 is the version the issue names and the one with `AsyncIOScheduler`, `max_instances`, `coalesce` and `misfire_grace_time` as the roadmap assumes; 4.x is a different, still unreleased API. The library ships no type information, and the project already has a documented pattern for that, so strict mypy stays strict everywhere else |
| D157 | Test doubles | `tests/fixtures/scheduler.py`: `FakeSignalRunner` (records calls, scripts reports and failures, can block on an event), `ManualClock` (advances only when a test or the recording `sleep` says so), `recording_sleep` and `scheduler_harness`. The end-to-end test reuses `engine_harness` (spec 015 §12) with the real engine and a temporary database | The same split spec 015 used: unit tests with doubles for one behaviour each, and one integration test where the real engine, the real repositories and the real `prepare_candles` meet the schedule. `ManualClock` is what makes "a week of closes" a millisecond-long test |
| D158 | Shared unit-of-work helper | `SignalEngine._in_unit_of_work` becomes a module-level `in_unit_of_work(unit_of_work, work)` in `engine/unit_of_work.py`; the method delegates to it and `scheduler/state.py` uses it for its two reads and writes | D110's discipline ("one session per block, never across an `await`, never two at once in one thread") must have exactly one implementation; a second copy in `scheduler/` is a second place to get it wrong. The move is mechanical: no behaviour changes and no existing engine test changes |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/scheduler/__init__.py` | developer | New: package docstring, re-exports nothing |
| `src/trading_bot/scheduler/policy.py` | developer | New: `SchedulerPolicy`, `DEFAULT_POLICY`, bounds (§6) |
| `src/trading_bot/scheduler/trigger.py` | developer | New: `SlotPredicate`, `run_every_slot`, `next_fire`, `CandleCloseTrigger` (§7) |
| `src/trading_bot/scheduler/slots.py` | developer | New: `skip_unpublished_hours` (§7.3) |
| `src/trading_bot/scheduler/state.py` | developer | New: `read_last_run`, `record_run` over a `UnitOfWork` (§9) |
| `src/trading_bot/scheduler/runner.py` | developer | New: `SignalRunner`, `FireStatus`, `RunAttempt`, `TimeframeRunner` (§8) |
| `src/trading_bot/scheduler/service.py` | developer | New: `SchedulerService`, `job_id` (§10) |
| `src/trading_bot/engine/unit_of_work.py` | developer | `in_unit_of_work` moved here (D158) |
| `src/trading_bot/engine/signal_engine.py` | developer | The private method delegates to it; no behaviour change (D158) |
| `src/trading_bot/config.py` | developer | Two settings (§6.1) |
| `.env.example` | developer | The two settings, commented, with their default |
| `pyproject.toml`, `uv.lock` | developer | `apscheduler>=3.11,<4` and the two mypy overrides (D156) |
| `Dockerfile` | developer | One import smoke check in the runtime stage (§13) |
| `tests/fixtures/scheduler.py` | developer | New: the doubles of §12 |
| `tests/unit/test_scheduler_policy.py` | developer | TDD: T1 |
| `tests/unit/test_scheduler_trigger.py` | developer | TDD: T2, T3 |
| `tests/unit/test_scheduler_runner.py` | developer | TDD: T4, T5, T6, T7 |
| `tests/unit/test_scheduler_service.py` | developer | TDD: T8, T9 |
| `tests/unit/test_scheduler_guard.py` | developer | TDD: T10 |
| `tests/unit/test_config.py` | developer | Extended with the two settings (T1) |
| `tests/integration/test_scheduler_end_to_end.py` | tester | T11 |
| `tests/unit/test_scheduler_properties.py` | tester | T12 |
| `tests/unit/test_scheduler_adversarial.py` | tester | T13 |
| `docs/ARCHITECTURE.md` | developer | §13; explicitly authorized by this spec |
| `docs/specs/016-candle-close-scheduler.md` | tech-lead | This spec |

No other file changes. In particular: no migration, no change to `domain/`, `data/`,
`persistence/`, `notifications/`, `cli/`, `api/`, `main.py`, `deploy/`, `scripts/`, `.github/`,
`docs/ROADMAP.md` or `CLAUDE.md`.

### 2. Flow

```text
APScheduler (AsyncIOScheduler, UTC, memory job store)
  job "candle-close.1h"  CandleCloseTrigger(1h) ─┐
  job "candle-close.4h"  CandleCloseTrigger(4h) ─┼─▶ get_next_fire_time ─▶ slot.close_time + delay
  job "candle-close.1d"  CandleCloseTrigger(1d) ─┘        (skipping slots the predicate rejects)

fire ──▶ await runner.run_timeframe(timeframe)          max_instances=1, coalesce=True

  slot  = calendar.closed_candles(timeframe, clock(), 1)[-1]        the last closed candle (D143)
  close = slot.close_time                                           the engine's `now`

  should_run(slot)?                       no  -> SLOT_SKIPPED
  per-timeframe lock free?                no  -> BUSY      (never two runs of one timeframe)
  clock() <= close + delay + grace?       no  -> STALE     (misfire window, D146)
  state.last_run(timeframe) < close?      no  -> ALREADY_RUN                       (D144)

  attempt 0:   global lock ──▶ await engine.run(timeframe, close)  ──▶ RunReport
  while report.retryable_tickers and attempts left and clock() + backoff < deadline:
      await sleep(backoff)                     the global lock is NOT held while sleeping
      global lock ──▶ await engine.run(timeframe, close, tickers=pending)

  record_run(timeframe, close)                one unit of work, after everything   (D144)
  return RunAttempt(...)
```

### 3. What the scheduler does not decide

| Question | Owner |
|----------|-------|
| Which tickers and rules a run evaluates, and whether a signal is new | `SignalEngine` (spec 015): one configuration snapshot per run |
| Whether a candle is closed, and the real close of a slot | `MarketCalendar` (spec 009) |
| Whether the provider has published a candle | `prepare_candles` (spec 010): `CandleNotPublishedError` |
| Whether a signal may be sent twice | The unique constraint of spec 014 |
| Whether the bot is paused | The engine, from `bot_state` (spec 015, D133) |

The scheduler decides **when** and **how often**, and nothing else.

### 4. `scheduler/__init__.py`

Package docstring only; it re-exports nothing, like `data/` and `engine/`.

### 5. Reused types

| Type | From | Used for |
|------|------|----------|
| `Timeframe` | `domain/timeframe.py` | The job set and every code path |
| `MarketCalendar`, `CandleSlot`, `CalendarRangeError` | `domain/market_calendar/sessions.py` | Closes, slots, coverage errors |
| `RunReport` | `engine/results.py` | `retryable_tickers`, `paused`, `summary` |
| `UnitOfWork`, `EngineRepositories`, `in_unit_of_work` | `engine/unit_of_work.py` | `last_run` and `record_run` |
| `LastRun`, `StateKey` | `persistence/state.py` | The run state |
| `Clock`, `system_clock` | `persistence/clock.py` | The injected clock (D152) |
| `is_unpublished_hour` | `data/yahoo/provider.py` | Only inside `slots.py` (D150) |

### 6. `scheduler/policy.py`

```python
MAX_CLOSE_DELAY: Final = timedelta(minutes=15)
MIN_MISFIRE_GRACE: Final = timedelta(minutes=1)
MAX_MISFIRE_GRACE: Final = timedelta(hours=1)
DEFAULT_RETRY_BACKOFF: Final = (
    timedelta(seconds=30),
    timedelta(seconds=60),
    timedelta(seconds=120),
    timedelta(seconds=240),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SchedulerPolicy:
    """When a run fires, how late it may still run and how long unpublished candles are retried."""

    close_delay: timedelta = timedelta(seconds=120)
    misfire_grace: timedelta = timedelta(minutes=15)
    retry_backoff: tuple[timedelta, ...] = DEFAULT_RETRY_BACKOFF
    retry_window: timedelta = timedelta(minutes=10)
    shutdown_timeout: timedelta = timedelta(seconds=10)

    def __post_init__(self) -> None: ...


DEFAULT_POLICY: Final = SchedulerPolicy()
```

Validation (`ValueError`, one message per rule, `TypeError` for a non-`timedelta`):

- `timedelta(0) <= close_delay <= MAX_CLOSE_DELAY`. The cap is **not** arbitrary: two consecutive
  `1h` closes can be **30 minutes** apart (the final slot of a session is truncated, 15:30–16:00 ET),
  so a larger delay could push a fire past the next close;
- `MIN_MISFIRE_GRACE <= misfire_grace <= MAX_MISFIRE_GRACE`. A zero grace would reject every fire,
  because a fire always arrives some milliseconds after its scheduled instant (D146);
- `retry_backoff` is a tuple of strictly positive, non-decreasing `timedelta`s (it may be empty,
  which disables retries);
- `retry_window >= timedelta(0)` and `shutdown_timeout > timedelta(0)`;
- every value is a whole number of seconds, so fire times stay exact and logs stay readable.

#### 6.1 `config.py`

```python
candle_close_delay_seconds: int = Field(default=120, ge=0, le=900)
scheduler_misfire_grace_seconds: int = Field(default=900, ge=60, le=3600)
```

Neither is a secret: they are plain integers, they stay out of `secret_values()` and they may be
overridden per environment through the operator's environment file. No deploy file changes
(`deploy/compose.yml` passes the environment through unchanged and both have defaults). #16 builds
`SchedulerPolicy(close_delay=timedelta(seconds=settings.candle_close_delay_seconds), ...)`.

> **`TB_SCHEDULER_MISFIRE_GRACE_SECONDS` is the only thing between "the bot restarted" and "that
> candle is lost forever."** There is no backfill (spec 015, D128/U2): a run always evaluates the
> last closed candle and nothing else, and the job store is in memory (D148), so after a restart
> APScheduler has no missed fire to replay. The single rule
> `clock() <= close + close_delay + misfire_grace` decides both how late a delayed fire may still
> run (D146) and whether the startup catch-up runs at all (D147). With the default of 900 s, a
> deploy, a container restart or a reboot of up to fifteen minutes keeps the candle; anything
> longer means that candle is never evaluated and its signals are never sent. Lowering this value
> narrows the recovery window, raising it lets the bot send advice about an older candle; the
> tests pin both sides of the boundary (AC8, AC15) so the behaviour can never drift silently.

### 7. `scheduler/trigger.py`

```python
type SlotPredicate = Callable[[CandleSlot], bool]  # True: this slot is evaluated


def run_every_slot(slot: CandleSlot) -> bool:
    """The default predicate: every candle of the grid is evaluated."""


def next_fire(
    calendar: MarketCalendar,
    timeframe: Timeframe,
    *,
    after: datetime,
    delay: timedelta,
    should_run: SlotPredicate = run_every_slot,
) -> tuple[CandleSlot, datetime]:
    """The first slot whose fire time (``close_time + delay``) is at or after ``after``."""


class CandleCloseTrigger(BaseTrigger):
    """An APScheduler trigger that fires at each candle close of one timeframe, plus the delay."""

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        timeframe: Timeframe,
        delay: timedelta,
        should_run: SlotPredicate = run_every_slot,
    ) -> None: ...

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime
    ) -> datetime | None: ...

    def __str__(self) -> str: ...  # "candle close 1h + 120s"
```

#### 7.1 How `next_fire` works

1. `after` and the arguments are checked (`to_utc` for the instant, `TypeError` for the rest).
2. The first candidate close is the smallest close **at or after** `after - delay`:
   `calendar.next_candle_close(timeframe, after - delay - resolution)`, where `resolution` is one
   microsecond, because `next_candle_close` is strictly after its argument (spec 009, D19).
3. The slot of that close is `calendar.closed_candles(timeframe, close, 1)[-1]`, whose
   `close_time` is exactly that close.
4. While `should_run(slot)` is false, advance with `calendar.next_candle_close(timeframe, close)`.
   After 64 consecutive rejections the function raises `ValueError`: a predicate that rejects
   everything must fail loudly instead of walking a century of sessions.
5. It returns `(slot, close + delay)`. `CalendarRangeError` propagates: it means the calendar
   cannot answer, which is a configuration problem (#16 builds a 100-year calendar).

`CandleCloseTrigger.get_next_fire_time` calls `next_fire` with
`after = max(now, previous_fire_time + resolution)` when a previous fire exists, and `after = now`
otherwise, so the sequence always advances even when APScheduler asks with a `now` that has not
moved. It catches `CalendarRangeError` and `ValueError`, logs one `ERROR` and returns `None`
(AC5): raising inside APScheduler's loop would take the scheduler down with it.

#### 7.2 Golden fire times (AC2)

NYSE, 2024, `delay = 120 s`, `should_run = skip_unpublished_hours(calendar)`. Instants are UTC.
Regular sessions are 13:30–20:00 in EDT and 14:30–21:00 in EST; 2024-07-03 is a half day
(13:30–17:00) and 2024-07-04 a holiday.

| `after` | `1h` | `4h` | `1d` |
|---------|------|------|------|
| 2024-07-01T13:00:00Z (before the open) | 07-01T14:32:00Z | 07-01T17:32:00Z | 07-01T20:02:00Z |
| 2024-07-01T19:45:00Z (the truncated 19:30–20:00Z slot is fired for) | 07-01T20:02:00Z | 07-01T20:02:00Z | 07-01T20:02:00Z |
| 2024-07-01T20:02:00Z (exactly a fire time: "at or after") | 07-01T20:02:00Z | 07-01T20:02:00Z | 07-01T20:02:00Z |
| 2024-07-01T20:02:00.000001Z | 07-02T14:32:00Z | 07-02T17:32:00Z | 07-02T20:02:00Z |
| 2024-07-03T15:00:00Z (half day) | 07-03T15:32:00Z | 07-03T17:02:00Z | 07-03T17:02:00Z |
| 2024-07-03T16:35:00Z (its final `1h` slot is skipped, D150) | 07-05T14:32:00Z | 07-03T17:02:00Z | 07-03T17:02:00Z |
| 2024-07-03T17:05:00Z (2024-07-04 is a holiday) | 07-05T14:32:00Z | 07-05T17:32:00Z | 07-05T20:02:00Z |
| 2024-03-08T21:05:00Z (EST, the weekend of the DST change) | 03-11T14:32:00Z | 03-11T17:32:00Z | 03-11T20:02:00Z |
| 2024-11-01T20:05:00Z (EDT, the weekend of the DST change) | 11-04T15:32:00Z | 11-04T18:32:00Z | 11-04T21:02:00Z |

The two DST rows are the point of the exercise: the same "first fire of the next session" moves by
an hour in UTC and by nothing in New York, and no arithmetic in `scheduler/` knows about it.

Every value above was computed with the §7.1 algorithm against `build_nyse_calendar(2024-01-01,
2024-12-31)` while this spec was written, so the table is a specification of the result, not a
guess; the developer pins these instants as literals and must not regenerate them with the code
under test.

#### 7.3 `scheduler/slots.py`

```python
def skip_unpublished_hours(calendar: MarketCalendar) -> SlotPredicate:
    """A predicate that skips the `1h` slots Yahoo never publishes (spec 011, D65)."""
```

The returned predicate answers `False` only for a `1h` slot with
`is_unpublished_hour(slot, calendar=calendar)`, and `True` for every `4h` and `1d` slot without
calling it (`is_unpublished_hour` raises for other timeframes). It is the only module of the
package that imports the provider; #16 injects it into the service.

### 8. `scheduler/runner.py`

```python
class SignalRunner(Protocol):
    """What the scheduler needs from the engine (spec 015, Design 8.1)."""

    async def run(
        self, timeframe: Timeframe, now: datetime, *, tickers: Sequence[str] | None = None
    ) -> RunReport: ...


type Sleep = Callable[[float], Awaitable[None]]


class FireStatus(StrEnum):
    RAN = "ran"  # the engine ran; the run was recorded
    ALREADY_RUN = "already_run"  # bot_state already holds this close (decision D144)
    BUSY = "busy"  # a run of this timeframe is in flight (issue AC)
    STALE = "stale"  # the fire arrived outside the misfire window (decision D146)
    SLOT_SKIPPED = "slot_skipped"  # the predicate rejects this slot (decision D150)
    STOPPING = "stopping"  # aclose() was called
    FAILED = "failed"  # an Exception escaped the engine; nothing was recorded


@dataclass(frozen=True, slots=True, kw_only=True)
class RunAttempt:
    timeframe: Timeframe
    scheduled_close: datetime | None  # None when the slot could not be determined
    status: FireStatus
    engine_calls: int
    report: RunReport | None  # the last report, when the engine ran
    unresolved: tuple[str, ...]  # retryable symbols left when the budget ended


class TimeframeRunner:
    """One fire of one timeframe: derive the candle, run the engine, retry, record the run."""

    def __init__(
        self,
        *,
        engine: SignalRunner,
        calendar: MarketCalendar,
        unit_of_work: UnitOfWork,
        policy: SchedulerPolicy = DEFAULT_POLICY,
        clock: Clock = system_clock,
        sleep: Sleep = asyncio.sleep,
        should_run: SlotPredicate = run_every_slot,
    ) -> None: ...

    async def run_timeframe(
        self, timeframe: Timeframe, *, catch_up: bool = False
    ) -> RunAttempt: ...

    def stop(self) -> None:
        """Refuse new runs and make a retry window in flight give up before its next sleep."""

    async def drain(self, timeout: timedelta) -> bool:
        """Wait for the run in flight; ``False`` when it was still running at the timeout."""

    @property
    def in_flight(self) -> tuple[Timeframe, ...]:
        """The timeframes whose run has not finished, which the shutdown ``WARNING`` names."""
```

#### 8.1 The steps of one fire

`catch_up=True` changes **only what is logged** (the `INFO` record of §11): a catch-up fire goes
through exactly the same checks as a scheduled one, which is what keeps D147 a single code path.

1. **Stopping?** `stop()` was called → `STOPPING`, nothing else happens.
2. **The candle (D143).** `slot = calendar.closed_candles(timeframe, clock(), 1)[-1]`;
   `close = slot.close_time`. A `CalendarRangeError` here is an ordinary failure (§8.4).
3. **The predicate (D150).** `should_run(slot)` false → `SLOT_SKIPPED`, one `DEBUG`.
4. **The timeframe lock (AC10).** One `asyncio.Lock` per timeframe, acquired **without waiting**
   (`locked()` then `acquire()`, both inside the same synchronous step, so no `await` can
   interleave between them): held → `BUSY` and one `WARNING`. It is released in a `finally`.
5. **The window (D146).** `clock() > close + close_delay + misfire_grace` → `STALE`, one `WARNING`.
6. **Already run (D144).** `read_last_run(...)`; `scheduled_at >= close` → `ALREADY_RUN`, one
   `DEBUG`.
7. **The attempts.** `deadline = min(calendar.next_candle_close(timeframe, close), started +
   retry_window)` where `started = clock()`. For attempt `i` (0 is the full run):
   - acquire the **global** run lock (shared by every timeframe, D149), `await engine.run(...)`,
     release it;
   - `pending = report.retryable_tickers`; empty → leave the loop;
   - no backoff left, or `stop()` was called, or `clock() + backoff[i] >= deadline` → leave the
     loop with `pending` as `unresolved` and one `WARNING` naming the symbols;
   - `await sleep(backoff[i].total_seconds())` **with no lock held**, then attempt `i + 1` with
     `tickers=pending`.
8. **Record (D144).** `record_run(timeframe, close)` in its own unit of work; return
   `RunAttempt(status=RAN, ...)` with the last report. A paused report skips step 7's loop (there
   is nothing retryable) and is still recorded (U4).

#### 8.2 Concurrency (AC10)

Two locks, and both are needed:

| Lock | Scope | Held during | On contention |
|------|-------|-------------|---------------|
| Per timeframe | One `asyncio.Lock` per `Timeframe` | The whole sequence, retries and sleeps included | The fire is **skipped** (`BUSY`), never queued (U5) |
| Global | One `asyncio.Lock` for the process | One `engine.run` call | The fire **waits**; its deadline still applies |

The per-timeframe lock is what the issue asks for. The global lock exists because the three
timeframes close **at the same instant** at every session close (16:00 ET is a `1h`, a `4h` and a
`1d` close), and three simultaneous runs would contend on one SQLite writer (`BEGIN IMMEDIATE`,
spec 014 D110) and on a provider transport that already serializes its calls (spec 011): waiting is
free, and interleaving buys nothing. It is released while the retry backoff sleeps so a `1h` retry
window cannot hold the `1d` run for minutes.

#### 8.3 Deadlines and units of work

`read_last_run` and `record_run` each open **one** unit of work in a worker thread, before and
after the engine call, never around it, so "never two sessions at once in one thread" (D110) holds:
the engine opens its own and this module never holds one across an `await`.

#### 8.4 Errors (D155)

| Raised by | Handling |
|-----------|----------|
| `Exception` from `engine.run`, the calendar, the repositories or the predicate | One `ERROR` naming the exception **class**, the timeframe and the close; `FAILED`; **no** `record_run`; the scheduler keeps firing |
| `BaseException`, `asyncio.CancelledError` included | Propagates untouched; the locks are released in `finally`; nothing is recorded |

No record passes `exc_info` or `stack_info`, and no exception text is logged (D154): a
`MarketDataError` message is safe by spec 010, but a notifier exception is not, and the engine
already logged what was safe.

> **No `Exception` may escape `run_timeframe` into APScheduler.** This is a secret-handling
> requirement, not a robustness preference. APScheduler's executor logs an escaped job exception
> with `logger.exception`, which renders the traceback through the formatter; `RedactingFilter`
> rewrites `record.msg` only, so **a traceback is not redacted** (issue #50). A notifier exception
> can carry the bot token inside a request URL (spec 015, D135) and a provider exception can carry
> Yahoo's session crumb (spec 011), and either could reach the run through the engine. The
> `except Exception` in `run_timeframe`, the ban on `exc_info`/`stack_info` and the rule that only
> the exception **class** name is logged are therefore all three enforced by the guard test (AC12,
> AC20) and must not be relaxed before #50 lands. `BaseException` is the only thing that leaves the
> job, and cancellation carries no message.

### 9. `scheduler/state.py`

```python
async def read_last_run(unit_of_work: UnitOfWork, timeframe: Timeframe) -> LastRun | None:
    """The last completed run of ``timeframe``, in one unit of work (decision D144)."""


async def record_run(
    unit_of_work: UnitOfWork, timeframe: Timeframe, scheduled_close: datetime
) -> LastRun:
    """Record a completed run; ``record_run`` is monotonic, so a repeat writes nothing."""
```

Both go through `in_unit_of_work` (D158) and use `BotStateRepository` only. The instant written as
`completed_at` comes from the repositories' injected clock (spec 013, D92), not from the
scheduler's.

### 10. `scheduler/service.py`

```python
JOB_ID_PREFIX: Final = "candle-close"


def job_id(timeframe: Timeframe) -> str:
    """``candle-close.1h``: the stable job id of one timeframe."""


class SchedulerService:
    """Owns the ``AsyncIOScheduler`` and one candle-close job per timeframe (issue #15)."""

    def __init__(
        self,
        *,
        runner: TimeframeRunner,
        calendar: MarketCalendar,
        policy: SchedulerPolicy = DEFAULT_POLICY,
        should_run: SlotPredicate = run_every_slot,
        timeframes: Sequence[Timeframe] = tuple(Timeframe),
        scheduler: BaseScheduler | None = None,  # tests inject; production builds AsyncIOScheduler
    ) -> None: ...

    def start(self) -> None:
        """Add the jobs and start firing. Must be called from inside the running event loop."""

    async def aclose(self) -> None:
        """Stop firing, let a retry window give up and wait for the run in flight."""

    @property
    def running(self) -> bool: ...

    def next_fire_times(self) -> tuple[tuple[Timeframe, datetime | None], ...]:
        """One aware UTC instant per timeframe, for ``/status`` (#21) and the tests."""
```

- `start()` builds `AsyncIOScheduler(timezone=UTC)` when none was injected, adds one recurring job
  per timeframe (`id=job_id(tf)`, `args=[tf]`, `trigger=CandleCloseTrigger(...)`,
  `max_instances=1`, `coalesce=True`, `misfire_grace_time=int(policy.misfire_grace.total_seconds())`,
  `replace_existing=False`), then one immediate catch-up job per timeframe
  (`id=f"{job_id(tf)}.catchup"`, `trigger="date"`, `run_date=<the scheduler's own now>`) as D147
  requires. Calling it twice raises `RuntimeError`.
- The job callable is `runner.run_timeframe`, a coroutine function, which `AsyncIOExecutor` awaits
  on the running loop. It never raises (D155), so APScheduler never logs a traceback (D154).
- `aclose()`: `runner.stop()` → `scheduler.pause()` → `await runner.drain(
  policy.shutdown_timeout)` → `scheduler.shutdown(wait=False)`, one `WARNING` per timeframe still
  in flight if the drain times out. Idempotent, and a no-op before `start()`.

  **Why the pause comes first (verified during implementation, 2026-09-20).** The draft of this
  spec ordered `shutdown(wait=False)` before the drain and asked the developer to verify what
  APScheduler 3.11 does to a coroutine job in flight. It **cancels** it —
  `AsyncIOExecutor.shutdown` cannot honour `wait=True` for a task — and `run_coroutine_job` then
  logs that cancellation with `logger.exception`, an **unredacted traceback** (issue #50), which
  is precisely what D154 exists to prevent. Pausing stops new fires without touching the run in
  flight, the drain is what waits for it, and the shutdown comes last, when there is normally
  nothing left to cancel. The APScheduler behaviour is pinned by a test of its own, so a library
  upgrade that changes it fails the gate. The contract AC16 asserts is unchanged: after
  `aclose()` returns, no path of the package can open a unit of work — unless the drain timed
  out, which is exactly what the `WARNING` reports, and which #16 must treat as the one case
  where disposing the database immediately is unsafe.
- The in-memory job store (D148) is the default and is never replaced.

### 11. Logging

One logger, `trading_bot.scheduler`; one line per record; no `exc_info` (D154); only timeframe
codes, ISO instants, symbols, enum values and integers appear. The engine keeps logging its own
`report.summary` (spec 015, §10), so the scheduler does not repeat it.

| Level | When | Record |
|-------|------|--------|
| `INFO` | a run finished with retries | `1h run for 2024-07-05T20:00:00+00:00: resolved 2 tickers after 2 retries` |
| `INFO` | the catch-up ran at startup | `1d catch-up run for 2024-07-05T20:00:00+00:00 (last run 2024-07-03T20:00:00+00:00)` |
| `WARNING` | the retry budget ended | `1h run for 2024-07-05T20:00:00+00:00: gave up on 2 tickers after 4 attempts (AAPL, MSFT)` |
| `WARNING` | a fire was skipped | `1h fire for 2024-07-05T20:00:00+00:00 skipped: busy` / `: stale by 1830s` |
| `WARNING` | the shutdown drain timed out | `scheduler shutdown: a 1h run was still in flight after 10s` |
| `ERROR` | a run failed, or the trigger cannot answer | `1h run for 2024-07-05T20:00:00+00:00 failed: StoredRuleError` / `1h trigger: no next fire after 2099-12-31T00:00:00+00:00 (CalendarRangeError)` |
| `DEBUG` | a fire that did nothing, and each retry wait | `1h fire for ... skipped: already run` / `1h retry 2 in 120s` |

Every literal above is asserted from a literal in the tests, never rebuilt with the code under
test. Two readings are fixed here so they are not re-litigated: the retry index is **0-based**
(`retry 2` waits `retry_backoff[2]`, which is what makes the two examples above consistent with
`DEFAULT_RETRY_BACKOFF`), and `stale by Ns` measures lateness from the **fire time**,
`clock() - (close + close_delay)`, which is the quantity the misfire rule compares with
`misfire_grace`.

### 12. Test doubles (`tests/fixtures/scheduler.py`, developer)

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class RunCall:
    timeframe: Timeframe
    now: datetime
    tickers: tuple[str, ...] | None
    started_at: datetime  # the ManualClock reading, to prove runs never overlap
    finished_at: datetime


class FakeSignalRunner:
    """Records calls, answers scripted ``RunReport``s and raises scripted failures, FIFO."""

    @property
    def calls(self) -> tuple[RunCall, ...]: ...
    def script(self, report: RunReport, *, times: int = 1) -> None: ...
    def fail_next(self, error: Exception, *, times: int = 1) -> None: ...
    def block_on(self, event: asyncio.Event) -> None: ...  # to hold a run in flight
    async def run(self, timeframe, now, *, tickers=None) -> RunReport: ...


def as_signal_runner(runner: FakeSignalRunner) -> SignalRunner: ...  # conformance, strict mypy


class ManualClock:
    """A clock that only moves when a test or the recording sleep moves it."""

    def __call__(self) -> datetime: ...
    def advance(self, delta: timedelta) -> None: ...


def recording_sleep(clock: ManualClock) -> tuple[Sleep, list[float]]:
    """A ``Sleep`` that records the waits and advances ``clock`` instead of sleeping."""
```

The doubles may add whatever a test needs to stay deterministic — `ManualClock.set` (a clock
stepped backwards is an NTP correction) and `FakeSignalRunner.wait_until_called(n)` (waiting on
an event instead of polling or sleeping) are part of this feature — as long as nothing here reads
the wall clock or sleeps.

`scheduler_harness(...)` assembles a `TimeframeRunner` over `nyse_test_calendar()`, a
`FakeSignalRunner`, a `ManualClock`, the recording sleep and a unit of work over a temporary
database (`tests/fixtures/database.py`), reusing `tests/fixtures/repositories.py`. No new database
fixture, no new calendar, no network and no wall-clock read.

### 13. Documentation and the image

`docs/ARCHITECTURE.md` (developer), exactly:

1. A new `## Scheduler` section after `## Signal engine`: the job set, the fire time
   (`real close + TB_CANDLE_CLOSE_DELAY_SECONDS`), the misfire and coalesce policy, the startup
   catch-up, the "exactly once" rule, the retry window with the same `now`, the two locks, the
   `is_open` trap (D151) and the skipped half-day slot, with one short Python example in the style
   of the other sections (it must pass `ruff format --check`).
2. The layers table: the `scheduler/` row gains the trigger, the misfire policy, "exactly once per
   close" and "one instance per process".
3. `## Configuration (environment variables)`: two rows for the new variables, both marked as
   operational (not secrets).
4. `## Decisions`: one bullet for APScheduler 3 with a calendar-driven custom trigger, in the style
   of the `exchange_calendars` and yfinance bullets, naming what is isolated (two modules) and why
   4.x is not used.
5. The `## Market data` → "Unpublished candles" table: the #15 row becomes the implemented
   behaviour (the same `now`, the backoff, the two deadline bounds).

`Dockerfile` (developer), in the runtime stage next to the existing smoke checks:

```dockerfile
# Fail the build if the scheduler cannot be imported on this platform (APScheduler and its deps).
RUN ["python", "-c", "import trading_bot.scheduler.service; raise SystemExit(0)"]
```

## Test plan

Every test runs without network (the autouse guard), without reading the wall clock (the `Clock` is
injected and every instant is a literal), without `time.sleep` or `asyncio.sleep` of real duration
(the `Sleep` is injected) and without writing outside `tmp_path`. Coroutines run with
`asyncio.run`, as the data and engine tests do.

Mandatory template cases:

- **Anti look-ahead (rule 4):** the scheduler computes nothing per candle, so the obligation takes
  the form AC3 pins: **no fire time is ever earlier than the real close of the candle the run is
  about**, and the candle the runner derives at the firing instant is always already closed
  (`closed_candles(timeframe, fire, 1)[-1] == slot`), over a full year and for three delays. T2 and
  T12 also pin that the schedule uses the calendar's real closes, never `nominal_close`, which
  would fire a day late for `1d` and skip the last `1h` candle of every session (spec 004 §5).
- **Idempotency (rule 5):** T4 and T8 pin "exactly once per close" (`last_run` guard, coalesced
  fires, a catch-up coinciding with a scheduled fire, a clock stepped backwards), and T11 proves it
  end to end against a real database: replaying a week of closes, restarting in the middle of a
  session and re-firing a close notify nothing twice.
- **Authorization: not applicable.** The scheduler exposes no Telegram, API or dashboard surface
  and handles no chat id; the allowlists are #19's and #21's.
- **Secret redaction (config and logs):** T1 pins that neither new setting is a secret, that both
  stay out of `secret_values()` and that they are documented in `.env.example`. T7 pins that a
  failing engine whose exception message carries a token-shaped string built at runtime
  (`"123456789" + ":" + "x" * 35`) produces a record with the exception **class** only, that the
  string never reaches the captured output, and that no call in the package passes `exc_info`
  (T10) — which also keeps APScheduler from printing a traceback for an escaped job exception.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 policy and settings | unit | `SchedulerPolicy` bounds (delay above 15 min, negative delay, zero grace, grace above an hour, a decreasing or non-positive backoff, fractional seconds, wrong types) and the `DEFAULT_POLICY` literals; `Settings` reads both variables from the environment, rejects out-of-range and non-integer values, keeps the documented defaults, excludes them from `secret_values()`; `.env.example` lists both | AC18 | developer |
| T2 fire times | unit | The golden table of §7.2 for the three timeframes and both predicates; the "at or after" boundary to the microsecond; delay `0` and `900 s`; the first and last fire of a session; `1d` fires at the session close, never at midnight; `CalendarRangeError` at the edge of coverage; the 64-rejection `ValueError` | AC1, AC2, AC4, AC5 | developer |
| T3 schedule properties | unit | Feeding each fire back as `after + 1 µs` over a year: strictly increasing, one fire per slot in label order, never on a closed day, `close + delay == fire`, `closed_candles(tf, fire, 1)[-1] == slot`; the trigger's `get_next_fire_time` agrees with `next_fire` and always advances, including when APScheduler asks twice with the same `now`; a `CalendarRangeError` becomes `None` plus one `ERROR` | AC1, AC3, AC5 | developer |
| T4 one run per fire | unit | `now` is the scheduled close and never the clock reading; one engine call; `record_run` once, after the run, with the close; `ALREADY_RUN` when the state holds that close; `STALE` outside the window and `RAN` just inside it; `SLOT_SKIPPED`; a clock far past several closes still runs only the last closed candle; the `RunAttempt` fields and the log literals of §11 | AC6, AC7, AC8, AC13 | developer |
| T5 the retry window | unit | Only `retryable_tickers` are retried, with the same `now` and the exact backoff recorded by the fake sleep; the loop stops on a clean report; the deadline bounds by `next_candle_close` (the last `1h` slot of a session, whose next close is the next morning) and by `retry_window`; the give-up `WARNING` and `unresolved`; an empty backoff disables retries; a paused report is never retried and is still recorded | AC9, AC11 | developer |
| T6 concurrency | unit | A second `run_timeframe` of the same timeframe while one is in flight returns `BUSY` with no engine call and one `WARNING`; two different timeframes never overlap (the recorded call log has no interleaving) and are both served; the global lock is free while the retry backoff sleeps (a second timeframe runs during a `1h` retry window); the locks are released after a failure and after cancellation | AC10 | developer |
| T7 errors | unit | Each of `StoredRuleError`, a database error and an arbitrary `Exception` gives one `ERROR` with the class name only, `FAILED`, no `record_run`, and the next fire runs normally; the token-shaped message never reaches the output; `asyncio.CancelledError` propagates and is never turned into `FAILED`; a `CalendarRangeError` while deriving the candle is an ordinary failure | AC12, AC18 | developer |
| T8 the service | unit | `start()` adds one job per timeframe with the pinned id, trigger, `max_instances`, `coalesce` and `misfire_grace_time`, plus one catch-up job each; a second `start()` raises; `next_fire_times()`; the four catch-up scenarios of AC15 including a restart in the middle of a session; `aclose()` stops firing, aborts a retry window, drains, warns on timeout, is idempotent and is a no-op before `start()` | AC14, AC15, AC16 | developer |
| T9 it really fires | unit | A real `AsyncIOScheduler` through `SchedulerService` with a stub trigger firing almost immediately: the runner is called (awaited on an `asyncio.Event`, `asyncio.wait_for` with a generous timeout), and no further call arrives after `aclose()`. No duration assertion | AC17 | developer |
| T10 guard | unit | The scanner against synthetic snippets first, then every rule of AC20 over the real package; `domain/` imports nothing from `scheduler/`; the engine, purity and persistence guards stay green after the D158 move | AC20 | developer |
| T11 end to end | integration | The real `SignalEngine` over `engine_harness` (temporary database, fake provider on `session_candles` frames, fake notifier) driven by `TimeframeRunner` at the fire times of `next_fire` over one session week with a `ManualClock`: every closed candle is evaluated once, the notified keys and their order are asserted, a ticker whose candle is not published is retried with the same `now` and notified exactly once when it appears, a restart in the middle of a session (a new service over the same database) notifies nothing twice, a paused stretch sends nothing and is not replayed afterwards, and `bot_state` ends with the expected `last_run` per timeframe | AC6, AC7, AC9, AC11, AC15 | tester |
| T12 properties | unit (`@given`) | Over drawn instants, delays, timeframes and predicates: the fire sequence is strictly increasing and covers the grid exactly once; every fire is at or after its close; no fire falls on a weekend or a holiday; `next_fire` is idempotent when fed its own result; the fire times computed forwards from a point equal those computed from any earlier point, truncated | AC1, AC3, AC4 | tester |
| T13 adversarial | unit | Delay `0` and delay `900 s` against the 30-minute gap of a session's final `1h` slot; a clock stepped backwards (NTP) → `ALREADY_RUN`, never a second run; a clock stepped forward past several closes → only the last closed candle; a run that overruns the next close → the next fire is `BUSY` and the recorded runs show the gap; a calendar whose coverage ends tomorrow → the job stops with one `ERROR` and the other timeframes keep firing; an engine that never returns → `aclose()` still returns after the drain timeout with one `WARNING`; a predicate that rejects everything → `ValueError`; `timeframes=()` → no job | AC5, AC8, AC10, AC16 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC21 |
| V2 coverage | `uv run pytest --cov --cov-report=term-missing`: the new `src/` modules at 100%, no regression elsewhere | AC21 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 20 s and none exceeds 3 s. Reported, never asserted. T9 is the only test that waits on real time and must stay under 1 s | AC21 |
| V4 typing | `uv run mypy`; `git grep -n "Any"` and `git grep -n "type: ignore"` over the new files empty; the two `pyproject.toml` overrides are the only typing concession | AC21 |
| V5 dependency | `git diff origin/main -- pyproject.toml uv.lock` reviewed line by line: `apscheduler` and its transitive packages only, reported with `uv tree --package apscheduler`, with their licences; the lock is regenerated with `uv lock`, never by hand | AC19 |
| V6 image | `docker build` for linux/arm64 green with the new smoke check (CI on the PR); the image gains no wheel that needs compiling | AC19 |
| V7 determinism | T2–T13 run 20 times in a row (a shell loop), all green, with the number of runs reported: no test may depend on timing | AC17 |
| V8 no stray database | The session-end guard is green and `git status --porcelain --ignored` shows no `*.db*` in the working tree | — |
| V9 docs | §13 placement and content; `uv run ruff format --check` on the new Python block | AC22 |
| V10 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC22 |
| V11 secrets and language | `python scripts/secret_scan.py --history`; an English-only review of the whole diff, log literals and test data included; no host, user, IP or absolute infrastructure path anywhere | AC18 |

**Testing rules for this feature:**

- no wall-clock reads and no elapsed-time assertions: `now` is always a literal or a `ManualClock`
  reading, and the only test that touches real time is T9, which waits on an event;
- no `time.sleep`, no `asyncio.sleep` of real duration and no barrier: ordering is asserted from
  the recorded call log of the doubles;
- literal expectations written out, never recomputed with the code under test (fire times, log
  lines, backoff sequences, statuses);
- every database comes from `tests/fixtures/database.py` under `tmp_path`;
- calendars come from `tests/fixtures/calendars.py` and frames from `session_candles`;
- fake tokens are built at runtime (`"123456789" + ":" + "x" * 35`), never written as literals.

**TDD order suggested to the developer:**

1. `policy.py` and the two settings with T1 (the values everything else reads);
2. `trigger.py` (`next_fire` first, then the trigger) with T2 and T3, and `slots.py` with T2;
3. the fixtures (`scheduler.py`), then `state.py` and the D158 move;
4. `runner.py` with T4, then T5, T6 and T7;
5. `service.py` with T8 and T9;
6. `test_scheduler_guard.py` (T10), the dependency, the `Dockerfile` check and the documentation.

## Risks and security

- **The delay is a guess about Yahoo.** A candle can be published but not final right after its
  close (spec 010, spec 011 "live-row merge"), and no data shows it. `TB_CANDLE_CLOSE_DELAY_SECONDS`
  is the only mitigation; too small a value evaluates a bar that is still moving, too large a value
  delays the advice. The default (120 s) is a starting point, the variable exists so it can be
  tuned per environment without a release, and U1 asks the user to confirm it.
- **A run that does not fit before the next close.** Sequential tickers plus the transport's pacing
  put a floor of about one second per ticker (spec 015, D137); a watchlist large enough to overrun
  an hour would make the next `1h` fire `BUSY` and skip that candle. The skip is visible in
  `bot_state` (a gap in `last_run`) and in the `WARNING`; F7 can alert on it. Parallelism stays
  available behind the same entry point.
- **The retry window competes with the next run.** The deadline has two bounds precisely so it
  cannot: the next close of the timeframe, and ten minutes. The pessimistic case is the last `1h`
  slot of a session, whose next close is the next morning; the ten-minute bound is what stops it.
- **A close missed while the process was down is lost** unless the restart lands inside the
  misfire window: `TB_SCHEDULER_MISFIRE_GRACE_SECONDS` (default 900 s) is the **only** thing
  between "the bot restarted" and "that candle is never evaluated", because there is no backfill
  (D128/U2) and the job store is in memory (D148). That is deliberate — the alternative is sending
  advice about a candle that closed hours ago — and it is the number the operator tunes if deploys
  or reboots grow longer. F7's missing-run alert reads `bot_state` and is what makes a lost close
  visible after the fact.
- **A clock stepped by NTP.** Backwards: the `last_run` guard turns a repeated fire into a no-op.
  Forwards: only the last closed candle runs, and the intermediate ones are never evaluated (D128).
  Both are pinned by T13.
- **APScheduler is new third-party code in the image.** It is pure Python, MIT-licensed and widely
  used, it is pinned below 4.0 (a different API), it is confined to two modules behind a pure
  schedule function, Dependabot will track it and the arm64 build has a smoke check. Nothing about
  the schedule depends on the library being correct: `next_fire` is ours and fully tested.
- **A traceback could leak a secret.** APScheduler logs an escaped job exception with
  `logger.exception`, and `RedactingFilter` rewrites `record.msg` only (issue #50). The runner
  therefore catches every `Exception` (D155) and the package never passes `exc_info` (D154), both
  enforced by the guard test. This must not be relaxed before #50 lands.
- **Two instances would double everything.** `CLAUDE.md` rule 7 is a deployment property, not a
  code one: this feature holds up its end with `max_instances=1`, one service per process and a
  memory job store; #16 must build exactly one `SchedulerService` and uvicorn must keep
  `--workers 1`.
- **Public repository.** No fixture, log line or example in this feature carries market data, a
  host, a user, a path or a token; the two new settings are operational integers, never secrets,
  and test tokens are built at runtime.
- **Unbreakable rules.** All preserved:
  - **signal-only:** the scheduler decides *when* the bot thinks, never what it buys; no order,
    broker, account or credential concept exists in it;
  - **pure `domain/`:** `scheduler/` imports `domain` and never the reverse; the calendar is
    injected and the guard forbids the rest;
  - **closed candles only:** every fire time is at or after the real close of the candle it is
    about, and the candle passed to the engine is always closed at the firing instant (AC3);
    `nominal_close` is banned from the package;
  - **idempotency:** the `last_run` guard plus `coalesce=True` plus the engine's unique constraint;
    a repeated fire writes nothing and sends nothing;
  - **UTC:** every instant is aware and in UTC (`to_utc` at the boundaries), the scheduler runs on
    `timezone=UTC` and the only local time zone in sight is the calendar's, inside `domain/`;
  - **single worker:** one service, one job per timeframe, `max_instances=1`, a per-timeframe lock
    and a global run lock;
  - **no `eval`:** nothing in this feature parses or executes a rule;
  - **English only.**

### Hand-off list for #16, F7 and later work

**#16 (wiring):**

1. Build, in the lifespan and **inside the running loop**, in this order: the `Database` (already
   there), the `MarketCalendar` (`build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY,
   NYSE_LAST_SUPPORTED_DAY)`), the provider, the notifier, the `SignalEngine` with
   `sql_unit_of_work(database)`, the `TimeframeRunner` and one `SchedulerService`. Shut down in
   reverse order: `await service.aclose()` **before** `provider.aclose()` and before
   `database.dispose()`. `aclose()` returns once the run in flight has finished **or** the
   `shutdown_timeout` expired; in the second case it logs a `WARNING` naming the timeframe, and a
   worker thread may still be inside a unit of work, so #16 decides there whether to wait longer
   before `database.dispose()` rather than assuming the drain always succeeds.
2. Build the policy from the settings:
   `SchedulerPolicy(close_delay=timedelta(seconds=settings.candle_close_delay_seconds),
   misfire_grace=timedelta(seconds=settings.scheduler_misfire_grace_seconds))`. A `ValueError` from
   the policy must abort startup, like any other configuration error.
3. Inject `should_run=skip_unpublished_hours(calendar)` (D150). Without it the `1h` job fires three
   times a year for a candle Yahoo never publishes.
4. Never build a second scheduler, a second engine, a second calendar or a second database handle,
   and keep uvicorn at `--workers 1` (rule 7).
5. `/health` may report `service.next_fire_times()`; deciding that is #16's.

**F7 (#26, #27):** `bot_state.last_run(timeframe)` compared with
`calendar.closed_candles(timeframe, now, 1)[-1].close_time` is the missing-run alert (spec 009
hand-off); the `WARNING` records of §11 (`busy`, `stale`, `gave up on N tickers`) are the other
signals, and `RunReport` stays the source for ticker-level failures. The heartbeat cadence is F7's
and is not called here.

**#21 (`/status`):** `service.next_fire_times()` and `bot_state.last_run` are what the command
shows; present them in the user's time zone and never as a nominal close (spec 004 §5).

**A second exchange or provider:** the calendar is already injected per service; a second exchange
means a second calendar and a job set per calendar, and `slots.py` is the only provider-specific
module to replace.

## User decisions

- **D1 (2026-09-14):** one PR per issue; M4 is #14 → #15 → #16, each with a branch and a PR of its
  own.
- **D2 (2026-09-14):** synthetic data only in tests. No fixture of this feature contains market
  data.

The seven product questions this spec opened were answered on **2026-09-20**. The user confirmed
the recommended answer to each, so the design above needed no change; they are recorded in full,
with the alternative that was turned down, so a later reader can trace every timing behaviour of
the bot to a decision.

- **U1 (2026-09-20) — the close delay:** `TB_CANDLE_CLOSE_DELAY_SECONDS`, default **120 s**, range
  `[0, 900]`, **one value for the three timeframes**. A larger value means steadier data (Yahoo may
  still be consolidating the bar, and yfinance can merge a live trade into the bar that just
  closed) and later advice; the 15-minute cap is not a taste, it is the 30-minute gap between the
  last two `1h` closes of a session. **Turned down:** one delay per timeframe (for example 60 s for
  `1h` and 300 s for `1d`), more precise but a third setting and a mapping to validate, for a value
  that can be tuned per environment without a release. (D153, §6)
- **U2 (2026-09-20) — one job per timeframe, always**, not only for timeframes that currently have
  enabled tickers. An empty run costs one short database read, and the engine already turns an
  empty configuration into a no-op. **Turned down:** building the job set from the configuration
  and rebuilding it on a timer, which needs polling — the management CLI runs in **another
  process**, so no event can reach the app — and adds a window in which a ticker added at 10:05 is
  ignored until the next rebuild. (D145)
- **U3 (2026-09-20) — the retry of unpublished candles is fixed in code:** backoff 30 s, 60 s,
  120 s, 240 s (four retries, about eight minutes), bounded by `next_candle_close(timeframe, now)`
  **and** by a ten-minute budget, applied to every retryable failure (an unpublished candle and a
  provider outage alike), with **no** new `TB_*` variable. **Turned down:** a
  `TB_CANDLE_RETRY_WINDOW_SECONDS` setting, one more knob for a value that is unlikely to need
  tuning before F7 shows how often it triggers; it stays a literal of `SchedulerPolicy`, so making
  it configurable later changes no signature. (D149)
- **U4 (2026-09-20) — `record_run` is called for every completed run**, a paused one and one with
  no enabled ticker included. The scheduler fired and finished its cycle, and the pause is already
  visible in `paused_since`, so a paused bot must not also look dead to F7's "this timeframe
  stopped running" alert. The consequence is that after `/resume` the candle that closed during the
  pause is not re-run, which is exactly spec 015's U3. **Turned down:** recording only runs that
  evaluated something, which makes a long pause indistinguishable from a dead scheduler. (D144,
  AC11)
- **U5 (2026-09-20) — a run that overruns the next close makes that fire skipped** (`BUSY`), with
  one `WARNING` and a visible gap in `bot_state` for F7. **Turned down:** queueing the fire, which
  would evaluate a candle that is no longer the last one (against D128) and would build a backlog a
  Raspberry Pi never works off. (D149, §8.2)
- **U6 (2026-09-20) — the daily candle runs right after the session close** (16:00 New York, about
  22:00 in Spain), which is what "fire at `next_candle_close + delay`" means and what makes the
  advice actionable at the next open. **Turned down:** a morning schedule for `1d`, which is a
  separate feature (a digest), not a delay, and would send advice about a candle that closed
  sixteen hours earlier. (D142, AC2)
- **U7 (2026-09-20) — on a half day the truncated last `1h` candle is not fired for at all.** Yahoo
  never publishes the 12:30–13:00 New York half hour as hourly data (spec 011, D65), so no request,
  no failure and no log noise is produced for it, about three days a year; the `4h` and `1d`
  candles of that day fire normally at the early close. **Turned down:** firing and letting the run
  report an unpublished candle without retrying, which produces one failed run per ticker on those
  days. (D150, AC4)

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames, users or absolute infrastructure paths
- [ ] Tests cover the acceptance criteria and fail without the implementation
- [ ] Fire times come from the calendar's **real** closes plus the delay; `nominal_close` appears
      nowhere in `scheduler/`, and no fire precedes the close of the candle it is about
- [ ] The engine receives the **scheduled close** as `now`, never the firing time, and a retry
      reuses the same `now` with only the retryable symbols
- [ ] Exactly once per close: the `last_run` guard, `coalesce=True`, `max_instances=1`, the
      per-timeframe lock, and `record_run` only after a completed run
- [ ] **No `Exception` escapes `run_timeframe` into APScheduler**, which would log it with
      `logger.exception`: `RedactingFilter` does not redact tracebacks (#50), and a notifier or
      provider exception can carry a bot token or a session crumb. No `exc_info`/`stack_info`
      anywhere in the package, and only the exception **class** name is logged
- [ ] **`TB_SCHEDULER_MISFIRE_GRACE_SECONDS` is the only recovery path after a restart** (there is
      no backfill and the job store is in memory): the single rule
      `clock() <= close + close_delay + misfire_grace` governs both the delayed fire and the
      startup catch-up, and both sides of that boundary are pinned by tests
- [ ] Every unit of work is short, in a worker thread and never open across the engine call
- [ ] The clock and the sleep are injected; no test reads the wall clock or sleeps
- [ ] `scheduler/` depends on ports only; APScheduler lives in two modules and the provider quirk
      in one
- [ ] The new settings are non-secret, bounded, documented in `.env.example` and in
      `docs/ARCHITECTURE.md`
- [ ] The dependency is pinned below 4.0, locked, pure Python and covered by the image smoke check
- [ ] No `Any`, no `type: ignore`; the new guard and the existing guards are green
- [ ] Scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
