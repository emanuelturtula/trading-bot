# 009 — Market calendar and trading hours (NYSE)

- **Status:** approved
- **Branch:** `feature/market-calendar` (from `origin/main` at `0c4ac8b`, v0.5.1; first of the stacked M2 series #9 → #8 → #10)
- **Spec author:** tech-lead
- **Issue:** #9 (milestone M2 · Market data)
- **Expected commit type:** `feat:`. The change adds a public runtime API in `src/trading_bot/domain/market_calendar/` (the session calendar that #8, #10, #15 and #26 build on) and a runtime dependency (`exchange-calendars`) that changes the published image. `feat` → minor bump. Suggested squash subject: `feat: add the NYSE market calendar (#<n>)`.

## Goal

Answer, with pure and injectable code, the time questions the bot asks about the US equity market: whether the market is open at an instant, the regular session of a date (holidays and half days included, in UTC), the real close of the candle a provider labels `X` for `1h`, `4h` and `1d`, when the next candle closes, and which candles are closed at `now`. The scheduler (#15) uses it to fire, the data layer (#8) to drop the in-progress candle, and #10 to resample `4h` bars and size lookbacks. It replaces `nominal_close` for every closedness and scheduling decision, as `docs/ARCHITECTURE.md` → "Nominal candle close" requires, without changing signal identity.

## Out of scope

- **Removing the open candle** (`drop_open_candle`), provider label normalization and the policy for rows the calendar rejects: #8 (Design §9 fixes what #8 can rely on).
- **yfinance, `4h` resampling and lookback sizing code:** #10. This spec only provides the grid and the lookup they use.
- **Scheduling, `TB_CANDLE_CLOSE_DELAY_SECONDS`, misfire policy:** #15. **Wiring** the calendar into `main.py` or the lifespan: #16. Nothing in the running app imports the calendar in this feature.
- **Signal identity.** `Timeframe.nominal_close` and `Evaluation.candle_close_ts` do not change (decision D22).
- **Presentation** of sessions or closes in a display time zone (#17, #25).
- Exchanges other than NYSE, per-ticker calendars, 24/7 assets, pre-market and after-hours sessions, intraday breaks, overnight sessions, and timeframes other than `1h`, `4h` and `1d` (decisions D27 and D28).
- New `TB_*` variables, migrations, `CLAUDE.md`, `docs/ROADMAP.md`, `.github/`, `scripts/`, `deploy/`, `main.py`, `config.py`, `logging_setup.py`.

## Acceptance criteria

Timestamps written `…Z` are UTC. "ET" is `America/New_York`. "Toy calendar" is the hand-built calendar of Design §7; "NYSE calendar" is `build_nyse_calendar(date(2021, 1, 1), date(2027, 12, 31))`.

### Model (`domain/market_calendar/sessions.py`)

- [ ] **AC1 (`Session`):** a frozen, slotted, keyword-only dataclass with `day: date`, `open_time: datetime` and `close_time: datetime`.
  - `day` must be a `date` that is not a `datetime` (`TypeError` otherwise);
  - `open_time` and `close_time` go through `to_utc` (spec 004), so they are stored as stdlib `datetime` in `datetime.UTC`; naive values, `NaT` and sub-microsecond `pd.Timestamp` raise `ValueError`, non-datetimes raise `TypeError`;
  - `open_time >= close_time` raises `ValueError`.
- [ ] **AC2 (`MarketCalendar` construction):** a frozen, slotted, keyword-only dataclass with `name`, `timezone`, `first_day`, `last_day` and `sessions` (Design §3).
  - `name` must be a `str` (`TypeError`) of 1–16 printable ASCII characters without whitespace (`ValueError`); `timezone` must be a `datetime.tzinfo` (`TypeError`); `first_day` and `last_day` must be dates that are not datetimes (`TypeError`), with `date.min < first_day <= last_day < date.max` (`ValueError`); `sessions` must be a `tuple` whose items are all `Session` (`TypeError` for a `list` or any other item);
  - `ValueError` when session days are not strictly increasing (duplicates or unsorted), when a session day is outside `[first_day, last_day]`, or when a session's `open_time` or `close_time`, converted to `timezone`, is not on its `day` (overnight sessions are unsupported);
  - an empty `sessions` tuple is valid;
  - `coverage_start` is 00:00 of `first_day` in `timezone` and `coverage_end` is 00:00 of the day after `last_day`, both as stdlib UTC datetimes. Toy calendar literals: `2024-03-06T05:00:00Z` and `2024-03-15T04:00:00Z` (the span crosses the DST change).
- [ ] **AC3 (argument contract):** for every query method,
  - `timeframe` must be a `Timeframe` member: the plain string `"1h"` raises `TypeError`;
  - instants (`ts`, `now`, `label`, `start`, `end`) go through `to_utc` with its `TypeError`/`ValueError` rules, and the same instant given as stdlib UTC, a fixed offset, a `ZoneInfo("America/New_York")` datetime (both folds where relevant) or a `pd.Timestamp` in any zone gives equal results;
  - days must be dates that are not datetimes (`datetime(2024, 7, 3)` raises `TypeError`);
  - an instant is inside the calendar when `coverage_start <= t < coverage_end` (for `candle_slots` bounds, `<= coverage_end`), and a day when `first_day <= day <= last_day`; otherwise `CalendarRangeError`. Toy calendar: `is_open` at `2024-03-06T05:00:00Z` answers, at `2024-03-06T04:59:59.999999Z` and at `2024-03-15T04:00:00Z` it raises;
  - `CalendarRangeError` and `CandleLabelError` subclass `CalendarError`, which subclasses `ValueError`. `CalendarRangeError` carries `calendar` (the name), `first_day` and `last_day`.
- [ ] **AC4 (`session_bounds`):** returns the `Session` whose `day` equals `day`, `None` for a day inside the span without a session (weekend, holiday, or a toy day without session), and raises `CalendarRangeError` outside the span.
- [ ] **AC5 (`is_open`):** `True` exactly when `open_time <= t < close_time` for some session (half-open, decision D19). Literal boundaries on the NYSE calendar for the half day 2024-07-03: `13:29:59.999999Z` False, `13:30:00Z` True, `16:59:59.999999Z` True, `17:00:00Z` False; any instant on 2024-07-04 (holiday) and 2024-07-06 (Saturday) False.
- [ ] **AC6 (candle grid):** the slots of a session with open `O`, close `C` are defined by Design §4.2 (intraday slots anchored at `O`, the last one shortened to `C`; one `1d` slot labelled 00:00 of the session day in `timezone`).
  - `candle_slots(timeframe, start, end)` returns every slot with `start <= label < end`, in increasing label order; `start > end` raises `ValueError` (checked before the range); `start == end` returns `()`;
  - within a session, intraday slots tile it exactly: the first `open_time` is `O`, each `close_time` equals the next `open_time`, the last `close_time` is `C`, every slot but the last lasts exactly `timeframe.duration` and the last lasts at most that;
  - the toy calendar's late-open day 2024-03-13 (10:00–15:00 ET) has `1h` labels `14:00Z … 18:00Z` (five full slots) and `4h` slots `14:00Z→18:00Z` and `18:00Z→19:00Z`;
  - the NYSE calendar matches the grid table of Design §5.4 literally, and over the labels of 2024 (`start = 2024-01-01T05:00:00Z`, `end = 2025-01-01T05:00:00Z`) it has 1 755 `1h`, 501 `4h` and 252 `1d` slots.
- [ ] **AC7 (`candle_slot`):** returns the slot whose `label` equals the given label exactly, and for every slot `s` returned by `candle_slots`, `candle_slot(s.timeframe, s.label) == s`. Any other label inside the calendar raises `CandleLabelError` with `timeframe`, `label` (UTC) and `kind`, decided in this order (Design §4.3):
  1. `not_a_session`: the label's date in `timezone` has no session;
  2. `outside_session` (intraday timeframes only): the date has a session, but the label is before its open or at or after its close;
  3. `off_grid`: otherwise.

  The kinds table of Design §5.5 is verified literally on the NYSE calendar. Labels outside the coverage raise `CalendarRangeError`, not `CandleLabelError`.
- [ ] **AC8 (`next_candle_close`):** returns the smallest `close_time` of any slot of `timeframe` that is **strictly greater** than `now`. It raises `CalendarRangeError` when `now` is outside the coverage or no slot closes after `now` inside the calendar (toy calendar: `now = 2024-03-13T19:00:00Z`, the last close, raises for every timeframe). The table of Design §5.3 is verified literally on the NYSE calendar.
- [ ] **AC9 (`closed_candles`):** returns the last `count` slots of `timeframe` whose `close_time <= now`, oldest first.
  - `count == 0` returns `()`; a negative `count` raises `ValueError`; a non-`int` or a `bool` raises `TypeError`;
  - `CalendarRangeError` when `now` is outside the coverage or fewer than `count` slots close by `now` inside the calendar (toy calendar: `closed_candles(D1, 2024-03-07T21:00:00Z, 1)` is the 2024-03-07 slot and `count=2` raises; `count=10**9` raises without hanging);
  - consistency with AC8 for every `now` whose last closed slot exists: `last = closed_candles(tf, now, 1)[-1]` satisfies `last.close_time <= now < next_candle_close(tf, now)` and `next_candle_close(tf, last.close_time) == next_candle_close(tf, now)`; `closed_candles(tf, now, n)` is the suffix of `closed_candles(tf, now, n + 1)`;
  - the `closed_candles` rows of Design §5.3 are verified literally.
- [ ] **AC10 (value semantics and messages):**
  - assigning any field of `Session`, `CandleSlot` or `MarketCalendar` raises `dataclasses.FrozenInstanceError`; the three types are equal by value and hashable, and two calendars built from equal arguments are equal;
  - `repr(calendar)` does not list sessions: it contains the name, both days and the session count, and is shorter than 200 characters for the full NYSE supported range;
  - every error message is a single line (`"\n" not in str(error)`) shorter than 300 characters, built from the calendar name, timeframe codes, ISO dates and ISO timestamps only.

### NYSE calendar (`domain/market_calendar/nyse.py`)

- [ ] **AC11 (builder contract):** `build_nyse_calendar(first_day, last_day)` returns a `MarketCalendar` with `name == "XNYS"`, `str(timezone) == "America/New_York"`, the given days and every NYSE regular session in `[first_day, last_day]`.
  - non-date or datetime arguments raise `TypeError`; `first_day > last_day` or a day outside `[NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY]` = `[2000-01-01, 2099-12-31]` raises `ValueError`;
  - any valid span works, including one day (`2025-01-09` to `2025-01-09`: zero sessions; `2024-07-03` to `2024-07-03`: one half-day session) and a span without sessions (`2024-07-06` to `2024-07-07`), although the library itself rejects them (Design §6);
  - **deterministic and range-independent:** two builds of the same span are equal, and the sessions of `build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))` equal the 2024 sessions of the NYSE calendar;
  - it never reads the clock and never uses the library's global registry: the purity guard passes and `git grep -n "get_calendar" src` matches nothing.
- [ ] **AC12 (holidays and half days):** on the NYSE calendar, literally:
  - the full-year tables of Design §5.1 for 2024, 2025 and 2026: the set of weekdays without a session, the set of sessions closing at 13:00 ET (every other session closes at 16:00 ET and every session opens at 09:30 ET), and the session counts 252, 250 and 251;
  - the ad hoc closure 2025-01-09 (national day of mourning) has no session, while 2025-01-08 and 2025-01-10 are regular sessions;
  - the observance cases of Design §5.1: 2021-12-31 is a regular session (New Year's Day 2022 fell on a Saturday and is not observed), 2021-12-24 and 2027-12-24 are closed, 2022-06-20 is closed (Juneteenth on a Sunday), 2027-06-18 is closed (Juneteenth on a Saturday) and 2027-07-05 is closed.
- [ ] **AC13 (daylight saving time):** on the NYSE calendar, literally:
  - the DST table of Design §5.2: `open_time`, `close_time` and the `1d` label on the last session before and the first session after each US change in 2024 and 2025;
  - the ET wall-clock grid is unchanged across a change while the UTC grid moves by one hour (the `1h` labels of 2024-03-08 and 2024-03-11 in Design §5.4);
  - the `next_candle_close` rows of Design §5.3 that cross the 2024-03-08 → 2024-03-11 weekend;
  - the ambiguous wall time `2024-11-03T01:30` ET with `fold=0` and `fold=1` is closed for both, gives `next_candle_close(H1, …) == 2024-11-04T15:30:00Z` and a last closed `1d` slot for session 2024-11-01; the nonexistent wall time `2024-03-10T02:30` ET is closed;
  - identity is not the real close: for the `1d` slot of 2024-03-08, `Timeframe.D1.nominal_close(slot.label) == 2024-03-09T05:00:00Z` while `slot.close_time == 2024-03-08T21:00:00Z` (decision D22).

### Purity, typing, dependencies, image, docs and gate

- [ ] **AC14 (purity and import weight):**
  - `sessions.py` passes the purity guard with the default allowlist, extended globally with `bisect` (decision D26); `nyse.py` additionally imports `exchange_calendars`, allowed for that file only; neither reads the clock or holds module-level mutable state;
  - importing `trading_bot.domain.market_calendar.sessions` in a fresh interpreter leaves `pandas`, `numpy` and `exchange_calendars` out of `sys.modules`; importing `trading_bot.domain.market_calendar.nyse` loads all three (control).
- [ ] **AC15 (typing):**
  - `uv run mypy` (strict) passes with one new override: `module = ["exchange_calendars", "exchange_calendars.*"]` with `follow_untyped_imports = true` (decision D24), and no other mypy configuration change;
  - the extra-flag command of Design §8 passes;
  - `typing.Any` is absent from `src/trading_bot/domain/market_calendar` (the spec 004 `git grep`) and there is no `# type: ignore` in the new files;
  - every new module except `__init__.py` defines `__all__` with exactly the names of Design §3 and §6.
- [ ] **AC16 (dependencies):**
  - `uv add exchange-calendars` writes `"exchange-calendars>=4.13.2"` to `[project.dependencies]`, and `uv.lock` only gains entries: no locked version changes;
  - `uv export --no-dev --no-hashes --locked --no-emit-project` differs from the same command on `0c4ac8b` only by: added `exchange-calendars==4.13.2`, `korean-lunar-calendar==0.4.0`, `pyluach==2.3.0` and `toolz==1.1.0`; `tzdata==2026.4` losing its `sys_platform == 'emscripten' or sys_platform == 'win32'` marker; and the corresponding `# via` lines (`numpy`, `pandas` and `tzdata` gain `exchange-calendars`). The tech-lead simulated this on a scratch copy of `pyproject.toml` and `uv.lock`;
  - that export resolves for Python 3.12 on `aarch64-manylinux_2_28` with `--only-binary :all:` (V6); every added package is a `py3-none-any` wheel.
- [ ] **AC17 (image):** the `Dockerfile` runtime stage gains the calendar smoke check of Design §8, and the PR's `Docker build (arm64)` job is green. The lead verifies the beta deploy and `/health`.
- [ ] **AC18 (docs):** `docs/ARCHITECTURE.md` gains a `## Market calendar` section between `## Domain models` and `## Indicators` matching Design §10; the `domain/` row of the Layers table, the "Closedness and scheduling" obligation under "Nominal candle close" and the `## Decisions` list are updated as §10 says; `src/trading_bot/domain/__init__.py` mentions the subpackage. Python blocks pass `ruff format --check`.
- [ ] **AC19 (gate, coverage and budget):** `uv run python scripts/check.py` is green; `sessions.py` and `nyse.py` have 100% line and branch coverage; the new tests need no network, read no wall clock, add at most **10 s** to `pytest` and no single new test takes more than **2 s** (evidence from `--durations`, reported, never asserted).
- [ ] **AC20 (scope):** only the files of Design §1 change compared with `0c4ac8b`, plus this spec.

## Design

### 0. Decisions

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D15 | Location | `domain/market_calendar/`: `sessions.py` (the model and every query, standard library only) and `nyse.py` (the only module that imports `exchange_calendars`). The package re-exports nothing | Closedness is a domain concept (rule 4) and every consumer (`data/`, `engine/`, `scheduler/`, dashboard) may import `domain/`. The queries take the instant as an argument and read immutable data, so they are pure (rule 3). The library is isolated in one module, like TA-Lib in `talib_kernels.py` (spec 005), so it can be replaced by rewriting `nyse.py`. `data/` was rejected because the scheduler would depend on the network layer; a new top-level package would add a layer for two modules. The module is not named `calendar.py`, which reads like the standard library module. |
| D16 | Calendar source | `exchange-calendars` 4.13.2 (Design §2) | Correct for every golden case, including the 2025-01-09 closure and the observance rules; maintained; Apache-2.0; pure-Python wheels; compatible with the locked pandas 3.0.5 and numpy 2.5.3. `pandas_market_calendars` depends on it (larger supply chain for no gain); an in-house table moves holiday rules and ad hoc closures into our maintenance. |
| D17 | Snapshot | `build_nyse_calendar` converts the library schedule once into an immutable `MarketCalendar` of UTC sessions; queries are binary searches in plain Python; no library object is kept. The calendar is built by the caller and injected (no module-level instance) | Queries stay independent of the library API and of pandas, `sessions.py` imports nothing heavy (AC14), tests of the query logic use hand-built calendars, and a library change can only affect the builder. `get_calendar("XNYS")` is avoided: it caches instances in a module-level registry and defaults its bounds from the wall clock. |
| D18 | Coverage | Every calendar covers an explicit span and raises `CalendarRangeError` outside it (fail closed). The builder supports 2000-01-01 to 2099-12-31 | Answering "closed" or "open" beyond known data would be a silent guess. The bounds are explicit arguments, so no clock is read. Recommended wiring (#16): build the full supported range once at startup (0.52 s and about 7 MB on the development machine; no clock read and no horizon to watch). |
| D19 | Boundaries | Half-open intervals: a session is `[open_time, close_time)`, a candle is closed when `close_time <= now`, and `next_candle_close` is strictly after `now` | At exactly 16:00:00 ET the market is closed and the daily candle is closed, which is what a scheduler firing at the close needs; strict `>` guarantees progress when #15 recomputes from the close it just used. One convention for every query keeps AC9's consistency properties exact. |
| D20 | Candle grid | Intraday slots anchored at the session open, the last one shortened to the close (`1h`: 09:30 … 15:30 ET, the last lasting 30 minutes; `4h`: 09:30–13:30 and 13:30–16:00; half day: one `4h` slot 09:30–13:00); one `1d` slot per session. Confirmed as decision D31 | Matches the bars Yahoo publishes for `1h` (labels 09:30 … 15:30), the session-aligned `4h` of `docs/ARCHITECTURE.md` and #10, and loses no trading time on half days. Arithmetic is done in UTC from the actual open, so DST (which changes at 02:00 ET on Sundays) never splits a slot, and an ad hoc late open still yields a consistent grid (toy day 2024-03-13). |
| D21 | Labels | The canonical label is the slot open for `1h`/`4h`, and 00:00 of the session day in exchange time for `1d` (the yfinance convention). `candle_slot` matches labels exactly and classifies rejections (`not_a_session`, `outside_session`, `off_grid`) | #8 must fix the label convention before #13 persists keys (ARCHITECTURE obligation); these are the labels yfinance already produces, so normalization is minimal. Exact matching makes a provider that labels daily bars at 00:00 UTC fail loudly (`off_grid`) instead of silently mapping every bar to the previous session. The kinds let #8 treat an after-hours row (`outside_session`) differently from a bar on a day the calendar thinks is a holiday (`not_a_session`). |
| D22 | Identity | The calendar never feeds `candle_close_ts`: signals keep `timeframe.nominal_close(label)` | Spec 004 §5: a real close from calendar data would change the keys of stored signals whenever the library corrects a half day. The calendar decides only closedness and scheduling. |
| D23 | Argument contract | Instants through `to_utc` (aware only; naive, `NaT` and sub-microsecond values rejected); days as `date` but never `datetime`; `Timeframe` members only; `count` as `int` but not `bool` | Rule 6 and the spec 004 conventions. `datetime` is a subclass of `date`, and accepting it for a day would silently drop a time zone. Clocks in the bot are stdlib `datetime.now(UTC)` values (microseconds), so rejecting nanoseconds costs callers nothing; provider labels are whole seconds. |
| D24 | Typing | mypy override `follow_untyped_imports = true` for `exchange_calendars` | The package ships inline annotations but no `py.typed`. Following it gives real types (`schedule` is a `DataFrame`, `tz` a `ZoneInfo`), so misuse of the library is caught; `ignore_missing_imports` would type the class as `Any`. Measured: strict mypy and the extra flags pass either way; no stub package exists. |
| D25 | Image check | A build-time smoke `RUN` in the `Dockerfile` that builds a one-month NYSE calendar and checks the 2024-07-03 half-day close | Nothing in the running app imports the calendar yet, so neither the arm64 build nor `/health` would notice a broken install or missing zone data. Same pattern as the TA-Lib check (spec 005). |
| D26 | Purity allowlist | `bisect` joins the global exact-module allowlist of the purity guard | It is a pure standard-library search with no state or I/O, like `math`; a per-file allowance would add noise for a module any domain code may reasonably use. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/market_calendar/__init__.py` | developer | Package docstring: the model in `sessions`, the NYSE builder in `nyse`, no re-exports |
| `src/trading_bot/domain/market_calendar/sessions.py` | developer | Errors, `Session`, `CandleSlot`, `MarketCalendar` (§3–§4) |
| `src/trading_bot/domain/market_calendar/nyse.py` | developer | `build_nyse_calendar` and its constants (§6) |
| `src/trading_bot/domain/__init__.py` | developer | Docstring package map mentions `market_calendar` (§10) |
| `pyproject.toml`, `uv.lock` | developer | `uv add exchange-calendars` and the mypy override (AC15, AC16) |
| `Dockerfile` | developer | Smoke check (§8); explicitly authorized by this spec |
| `tests/fixtures/calendars.py` | developer | Shared helpers for this feature and #8, #10, #15 (§7) |
| `tests/unit/test_market_calendar_sessions.py` | developer | TDD: T1–T8 (toy calendar) |
| `tests/unit/test_market_calendar_nyse.py` | developer | TDD: T9–T11 (NYSE calendar) |
| `tests/unit/test_domain_purity.py` | developer | T12: new modules in the scanned set, `bisect` allowed, `exchange_calendars` allowed in `nyse.py` only, fresh-interpreter checks |
| `tests/unit/test_market_calendar_properties.py` | tester | T13 |
| `tests/unit/test_market_calendar_adversarial.py` | tester | T14 |
| `docs/ARCHITECTURE.md` | developer | §10; explicitly authorized by this spec |
| `docs/specs/009-market-calendar.md` | tech-lead | This spec |

No new Protocols, no migrations and no `TB_*` variables.

### 2. Calendar source evaluation

Evidence gathered by the tech-lead on 2026-09-16 on scratch environments (never on the project files): PyPI metadata, `uv pip compile --python-version 3.12 --python-platform aarch64-manylinux_2_28 --only-binary :all:`, a scratch copy of `pyproject.toml`/`uv.lock`, a prototype of §3–§6 with strict mypy, and a session-by-session comparison of both libraries.

| Criterion | `exchange-calendars` 4.13.2 | `pandas-market-calendars` 5.4.0 | In-house rule table |
|-----------|-----------------------------|---------------------------------|---------------------|
| Correctness | Every golden case of §5, including 2025-01-09, the Saturday New Year rule and Juneteenth observance | Identical sessions to `exchange-calendars` for 2000–2030; differs only in six pre-2007 special opens and one 2005 close | Ours to get right: ten regular holidays with observance exceptions, Good Friday (Easter computus), three early-close rules with weekday exceptions, ad hoc closures (2001, 2004, 2007, 2012, 2018, 2025) |
| Maintenance | Community fork of Quantopian's `trading_calendars` (since 2021); 8 releases from 2025-07 to 2026-03, last 2026-03-10 | Active; last release 2026-05-27 | Yearly review by us; ad hoc closures added by hand when announced |
| Supply chain | Adds `exchange-calendars`, `pyluach` 2.3.0 (MIT), `korean-lunar-calendar` 0.4.0 (MIT), `toolz` 1.1.0 (BSD-3); makes `tzdata` unconditional | Everything on the left **plus** itself (it depends on `exchange-calendars`) | None |
| Import and build cost | `nyse.py` import about 1.0 s cold (pandas and the 91 calendar modules the package imports eagerly); build 2021–2027 about 0.1 s, 2000–2099 0.52 s (25 116 sessions, about 7 MB retained) | Imports `exchange_calendars` eagerly as well, plus its own modules | Negligible |
| pandas 3.0.5 / numpy 2.5.3 | Resolves with the lock unchanged; no warnings while building (warnings as errors) | Resolves | n/a |
| License | Apache-2.0 | MIT | n/a |
| linux/arm64, Python 3.12 | `py3-none-any` wheels for the package and every new transitive dependency; the whole runtime export resolves binary-only for `aarch64-manylinux_2_28` | `py3-none-any` | n/a |
| Typing | Inline annotations, no `py.typed` (D24) | No `py.typed` | Ours |

Supply-chain notes (the `pandas-ta` precedent): hashes are pinned in `uv.lock` and Dependabot bumps are reviewed. `korean-lunar-calendar` 0.4.0 was released on 2026-06-15 after four years without releases; the tech-lead diffed its wheel against 0.3.1: standard library imports only (`datetime`, `numbers`), no network, process, file or `exec` use, a memoization refactor plus input validation. Its constructor reads `date.today()`, but it is only instantiated inside Korean-exchange functions that `nyse.py` never calls. `pyluach` and `toolz` import only standard library modules. All four are imported when `exchange_calendars` is imported, because the package imports every exchange calendar eagerly.

**Data horizon.** `XNYSExchangeCalendar.bound_min()` and `bound_max()` are `None`: regular holidays and regular early closes are generated from rules for any year, while ad hoc closures (days of mourning, weather, emergencies) are hard-coded and known only up to the installed release (4.13.2 includes 2025-01-09). The library module computes default bounds from the wall clock at import time; `nyse.py` always passes explicit bounds, so those defaults never affect a result. Outside the span it was built for, a `MarketCalendar` raises `CalendarRangeError` (D18). What happens when a closure is announced after the installed release is in Risks.

### 3. Public API: `domain/market_calendar/sessions.py`

```python
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum

from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "CalendarError",
    "CalendarRangeError",
    "CandleLabelError",
    "CandleLabelErrorKind",
    "CandleSlot",
    "MarketCalendar",
    "Session",
]


class CalendarError(ValueError):
    """Base class of calendar errors."""


class CalendarRangeError(CalendarError):
    """A day or instant outside the calendar, or history or future the calendar does not hold."""

    calendar: str  # MarketCalendar.name
    first_day: date
    last_day: date


class CandleLabelErrorKind(StrEnum):
    NOT_A_SESSION = "not_a_session"
    OUTSIDE_SESSION = "outside_session"
    OFF_GRID = "off_grid"


class CandleLabelError(CalendarError):
    """A label inside the calendar that is not a label of the timeframe's session grid."""

    kind: CandleLabelErrorKind
    timeframe: Timeframe
    label: datetime  # stdlib UTC


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    day: date  # the session date in exchange time
    open_time: datetime  # stdlib UTC
    close_time: datetime  # stdlib UTC


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleSlot:
    """One candle of the session grid. Built by MarketCalendar; fields are stdlib UTC."""

    timeframe: Timeframe
    session_day: date
    label: datetime  # the canonical provider label (D21)
    open_time: datetime
    close_time: datetime  # the real close


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketCalendar:
    name: str
    timezone: tzinfo
    first_day: date
    last_day: date
    sessions: tuple[Session, ...]

    @property
    def coverage_start(self) -> datetime: ...  # 00:00 of first_day in timezone, stdlib UTC
    @property
    def coverage_end(self) -> datetime: ...  # 00:00 of the day after last_day, stdlib UTC

    def session_bounds(self, day: date) -> Session | None: ...
    def is_open(self, ts: datetime) -> bool: ...
    def next_candle_close(self, timeframe: Timeframe, now: datetime) -> datetime: ...
    def candle_slot(self, timeframe: Timeframe, label: datetime) -> CandleSlot: ...
    def candle_slots(
        self, timeframe: Timeframe, start: datetime, end: datetime
    ) -> tuple[CandleSlot, ...]: ...
    def closed_candles(
        self, timeframe: Timeframe, now: datetime, count: int
    ) -> tuple[CandleSlot, ...]: ...
```

- `coverage_start` and `coverage_end` are derived, read-only and not constructor arguments (a property as shown, or an `init=False` field excluded from comparison; the developer chooses). The internal lookup tuples (opens, closes, day starts) are `init=False`, `compare=False`, `repr=False` fields filled in `__post_init__` with `object.__setattr__`, the spec 004 pattern.
- `repr` is custom and bounded (AC10). `CandleSlot` does not validate: only `MarketCalendar` builds it. `Session` validates (AC1) because callers build it.
- `sessions.py` imports only `__future__`, `bisect`, `dataclasses`, `datetime`, `enum`, `typing` and `trading_bot.domain.*` (it needs no `zoneinfo`: the time zone is injected).

### 4. Semantics

#### 4.1 Coverage and lookups

- An instant is answerable when `coverage_start <= t < coverage_end`; a day when `first_day <= day <= last_day` (AC3). Days inside the span without a session are known closed days, not errors.
- `is_open`, `session_bounds`, `candle_slot` and `next_candle_close` are binary searches over the session tuples (`bisect`); none iterates over all sessions. `closed_candles` walks back from `now` and materializes at most the slots it returns plus one session. No test asserts elapsed time (§ Test plan).
- The label's date in exchange time (`label.astimezone(timezone).date()`) is only needed to classify `CandleLabelError` kinds.

#### 4.2 Candle grid

For a session with open `O`, close `C` and session day `d`:

| Timeframe | Slots | `label` | `open_time` | `close_time` |
|-----------|-------|---------|-------------|--------------|
| `1h`, `4h` (duration `D`) | `k = 0, 1, …` while `O + k·D < C` | `O + k·D` | `O + k·D` | `min(O + (k+1)·D, C)` |
| `1d` | one | 00:00 of `d` in `timezone`, as UTC | `O` | `C` |

- Arithmetic is on UTC datetimes. A regular NYSE session has 7 `1h` slots and 2 `4h` slots; a half day has 4 and 1.
- `candle_slots(timeframe, start, end)` selects by **label**, so for `1d` the range must contain 00:00 ET of the day: `[09:00 ET, 17:00 ET)` on a session day contains no `1d` label.

#### 4.3 Label classification

`candle_slot(timeframe, label)`, after the argument checks and the coverage check:

1. Look for the slot whose label equals `label` exactly (intraday: the session with `open_time <= label < close_time` and `(label - open_time) % duration == 0`; `1d`: a session whose day start equals `label`). Found → return it.
2. Otherwise, if the label's date in `timezone` has no session → `not_a_session`.
3. Otherwise, for `1h`/`4h`, if the label is before that session's open or at or after its close → `outside_session`.
4. Otherwise → `off_grid`.

Messages read like `2024-07-03T14:00:00+00:00 is not a 1h candle label in XNYS (off_grid)`.

#### 4.4 Errors

| Situation | Result |
|-----------|--------|
| Wrong argument type (non-`Timeframe`, `datetime` as a day, non-`datetime` instant, non-`int` or `bool` count) | `TypeError` |
| Naive instant, `NaT`, sub-microsecond `pd.Timestamp` | `ValueError` (from `to_utc`) |
| `start > end`, negative `count`, invalid constructor data | `ValueError` |
| Day or instant outside the calendar; no slot closing after `now`; fewer than `count` closed slots | `CalendarRangeError` |
| A label inside the calendar that is not on the grid | `CandleLabelError` with its kind |

Messages are single-line English built from the calendar name, timeframe codes and ISO values; no caller text is echoed (every argument is a date, datetime, enum or integer).

### 5. NYSE golden tables

All values were produced by the prototype on `exchange-calendars` 4.13.2 and match the NYSE published schedules; `pandas-market-calendars` 5.4.0 agrees on every session in these years.

#### 5.1 Holidays and half days (AC12)

| Year | Sessions | Weekdays without a session | Sessions closing at 13:00 ET |
|------|----------|----------------------------|------------------------------|
| 2024 | 252 | 01-01, 01-15, 02-19, 03-29, 05-27, 06-19, 07-04, 09-02, 11-28, 12-25 | 07-03, 11-29, 12-24 |
| 2025 | 250 | 01-01, **01-09**, 01-20, 02-17, 04-18, 05-26, 06-19, 07-04, 09-01, 11-27, 12-25 | 07-03, 11-28, 12-24 |
| 2026 | 251 | 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25 | 11-27, 12-24 |

Observance cases: 2021-12-24 closed (Christmas on a Saturday, observed Friday); 2021-12-31 regular session (New Year's Day 2022 on a Saturday is not observed on Friday); 2022-06-20 closed (Juneteenth on a Sunday); 2027-06-18 closed (Juneteenth on a Saturday); 2027-07-05 closed (Independence Day on a Sunday); 2027-12-24 closed (Christmas on a Saturday).

#### 5.2 Daylight saving time (AC13)

| Session | ET offset | `open_time` | `close_time` | `1d` label |
|---------|-----------|-------------|--------------|------------|
| 2024-03-08 Fri | −05:00 | 14:30Z | 21:00Z | 2024-03-08T05:00Z |
| 2024-03-11 Mon | −04:00 | 13:30Z | 20:00Z | 2024-03-11T04:00Z |
| 2024-11-01 Fri | −04:00 | 13:30Z | 20:00Z | 2024-11-01T04:00Z |
| 2024-11-04 Mon | −05:00 | 14:30Z | 21:00Z | 2024-11-04T05:00Z |
| 2025-03-07 Fri | −05:00 | 14:30Z | 21:00Z | 2025-03-07T05:00Z |
| 2025-03-10 Mon | −04:00 | 13:30Z | 20:00Z | 2025-03-10T04:00Z |
| 2025-10-31 Fri | −04:00 | 13:30Z | 20:00Z | 2025-10-31T04:00Z |
| 2025-11-03 Mon | −05:00 | 14:30Z | 21:00Z | 2025-11-03T05:00Z |

Half days in UTC: 2024-07-03 and 2025-07-03 `13:30Z–17:00Z`; 2024-11-29, 2024-12-24, 2025-11-28, 2025-12-24, 2026-11-27 and 2026-12-24 `14:30Z–18:00Z`.

#### 5.3 `next_candle_close` and `closed_candles` (AC8, AC9)

| `now` | Why | `1h` | `4h` | `1d` |
|-------|-----|------|------|------|
| 2024-01-02T14:30:00Z | exactly the open | 2024-01-02T15:30Z | 2024-01-02T18:30Z | 2024-01-02T21:00Z |
| 2024-01-02T15:30:00Z | exactly a `1h` close: strictly after | 2024-01-02T16:30Z | 2024-01-02T18:30Z | 2024-01-02T21:00Z |
| 2024-01-06T12:00:00Z | Saturday | 2024-01-08T15:30Z | 2024-01-08T18:30Z | 2024-01-08T21:00Z |
| 2024-03-08T20:59:59.999999Z | just before the close | 2024-03-08T21:00Z | 2024-03-08T21:00Z | 2024-03-08T21:00Z |
| 2024-03-08T21:00:00Z | close before the DST weekend | 2024-03-11T14:30Z | 2024-03-11T17:30Z | 2024-03-11T20:00Z |
| 2024-03-28T20:00:00Z | close before Good Friday | 2024-04-01T14:30Z | 2024-04-01T17:30Z | 2024-04-01T20:00Z |
| 2024-07-03T16:59:59.999999Z | inside the last half-day slot | 2024-07-03T17:00Z | 2024-07-03T17:00Z | 2024-07-03T17:00Z |
| 2024-07-03T17:00:00Z | half-day close, then a holiday | 2024-07-05T14:30Z | 2024-07-05T17:30Z | 2024-07-05T20:00Z |
| 2024-11-01T19:59:59.999999Z | inside the last (30-minute) `1h` slot | 2024-11-01T20:00Z | 2024-11-01T20:00Z | 2024-11-01T20:00Z |
| 2024-12-24T18:00:00Z | half-day close, then Christmas | 2024-12-26T15:30Z | 2024-12-26T18:30Z | 2024-12-26T21:00Z |
| 2025-01-08T21:00:00Z | close before the 2025-01-09 closure | 2025-01-10T15:30Z | 2025-01-10T18:30Z | 2025-01-10T21:00Z |

| Call | Result (label → `close_time`), oldest first |
|------|---------------------------------------------|
| `closed_candles(H1, 2024-07-05T14:00:00Z, 3)` | 2024-07-03T14:30Z → 15:30Z, 15:30Z → 16:30Z, 16:30Z → 17:00Z |
| `closed_candles(H1, 2024-07-05T14:30:00Z, 1)` | 2024-07-05T13:30Z → 14:30Z |
| `closed_candles(H4, 2024-07-05T14:00:00Z, 1)` | 2024-07-03T13:30Z → 17:00Z |
| `closed_candles(D1, 2024-07-05T14:00:00Z, 2)` | 2024-07-02T04:00Z → 20:00Z, 2024-07-03T04:00Z → 17:00Z |
| `closed_candles(D1, 2024-03-11T19:59:59.999999Z, 1)` | 2024-03-08T05:00Z → 21:00Z |
| `closed_candles(D1, 2024-03-11T20:00:00Z, 1)` | 2024-03-11T04:00Z → 20:00Z |

#### 5.4 Grid by day (AC6)

`label → close_time` in UTC, hours and minutes on the session date.

| Session | `1h` | `4h` | `1d` |
|---------|------|------|------|
| 2024-01-02 (regular, EST) | 14:30→15:30, 15:30→16:30, 16:30→17:30, 17:30→18:30, 18:30→19:30, 19:30→20:30, 20:30→21:00 | 14:30→18:30, 18:30→21:00 | 05:00 (open 14:30) → 21:00 |
| 2024-03-08 (regular, EST) | 14:30→15:30, 15:30→16:30, 16:30→17:30, 17:30→18:30, 18:30→19:30, 19:30→20:30, 20:30→21:00 | 14:30→18:30, 18:30→21:00 | 05:00 (open 14:30) → 21:00 |
| 2024-03-11 (regular, EDT) | 13:30→14:30, 14:30→15:30, 15:30→16:30, 16:30→17:30, 17:30→18:30, 18:30→19:30, 19:30→20:00 | 13:30→17:30, 17:30→20:00 | 04:00 (open 13:30) → 20:00 |
| 2024-07-03 (half day, EDT) | 13:30→14:30, 14:30→15:30, 15:30→16:30, 16:30→17:00 | 13:30→17:00 | 04:00 → 17:00 |
| 2024-11-29 (half day, EST) | 14:30→15:30, 15:30→16:30, 16:30→17:30, 17:30→18:00 | 14:30→18:00 | 05:00 → 18:00 |

#### 5.5 Label classification (AC7)

| Timeframe | Label | ET | Kind |
|-----------|-------|----|------|
| `1h` | 2024-07-03T14:00:00Z | 10:00, inside the session | `off_grid` |
| `4h` | 2024-01-02T16:30:00Z | 11:30, a `1h` label but not a `4h` one | `off_grid` |
| `1h` | 2024-01-02T13:00:00Z | 08:00, pre-market | `outside_session` |
| `1h` | 2024-01-02T21:00:00Z | 16:00, the close | `outside_session` |
| `1h` | 2024-07-03T17:00:00Z | 13:00, the half-day close | `outside_session` |
| `1h` | 2024-07-04T14:30:00Z | 10:30 on a holiday | `not_a_session` |
| `1d` | 2024-07-03T13:30:00Z | 09:30, the open of a session day | `off_grid` |
| `1d` | 2024-07-03T00:00:00Z | 20:00 on 2024-07-02 (a daily label at 00:00 UTC) | `off_grid` |
| `1d` | 2024-07-04T04:00:00Z | 00:00 on a holiday | `not_a_session` |
| `1d` | 2024-07-06T04:00:00Z | 00:00 on a Saturday | `not_a_session` |

### 6. `domain/market_calendar/nyse.py`

```python
from datetime import date
from typing import Final

from trading_bot.domain.market_calendar.sessions import MarketCalendar

__all__ = [
    "NYSE_CALENDAR_NAME",
    "NYSE_FIRST_SUPPORTED_DAY",
    "NYSE_LAST_SUPPORTED_DAY",
    "build_nyse_calendar",
]

NYSE_CALENDAR_NAME: Final = "XNYS"  # ISO 10383 market identifier
NYSE_FIRST_SUPPORTED_DAY: Final = date(2000, 1, 1)
NYSE_LAST_SUPPORTED_DAY: Final = date(2099, 12, 31)


def build_nyse_calendar(first_day: date, last_day: date) -> MarketCalendar: ...
```

- Import the class directly: `from exchange_calendars.exchange_calendar_xnys import XNYSExchangeCalendar`. Never `get_calendar` (D17).
- The library rejects `start == end` (`ValueError`) and spans without sessions (`NoSessionsError`), both verified. Build the library calendar for `[first_day, last_day + 14 days]`, which always contains a session inside the supported range, and keep the sessions whose day is `<= last_day`. The prototype verified that padding does not change any session in the overlap (AC11 range independence).
- Read `schedule` (`open` and `close` are UTC `pd.Timestamp` columns; the index holds session dates) and `tz`; convert each timestamp with `to_utc` (they are whole minutes) and the index label with `.date()`. XNYS has no intraday breaks; the builder ignores the break columns.
- `timezone` is the library's `tz` (`ZoneInfo("America/New_York")`). `MarketCalendar` validation (AC2) catches any session that does not open and close on its own day.
- The supported range: 2000 is the start of the range cross-checked against `pandas-market-calendars` and far older than any history the bot fetches (the largest `stable_warmup` in the catalog is about 2 250 daily candles, about nine years); 2099 keeps the build under a second. Beyond the release date of the library every year is an extrapolation of the rules anyway (§2).

### 7. Test fixtures: `tests/fixtures/calendars.py`

Type-checked (it is under `tests/fixtures`), no network, no clock:

```python
NEW_YORK: Final = ZoneInfo("America/New_York")
NYSE_TEST_FIRST_DAY: Final = date(2021, 1, 1)
NYSE_TEST_LAST_DAY: Final = date(2027, 12, 31)


def utc(text: str) -> datetime:
    """``utc("2024-07-03T17:00")``: an ISO wall time read as a stdlib UTC datetime."""


def wall_session(day: date, opens: time, closes: time, *, timezone: tzinfo = NEW_YORK) -> Session:
    """A session from exchange wall-clock times on ``day``."""


def toy_calendar() -> MarketCalendar:
    """The hand-built calendar below: every model test runs without the library."""


def nyse_test_calendar() -> MarketCalendar:
    """build_nyse_calendar(NYSE_TEST_FIRST_DAY, NYSE_TEST_LAST_DAY), built once per process."""
```

The toy calendar, `name="TOY"`, `timezone=NEW_YORK`, `first_day=2024-03-06`, `last_day=2024-03-14`:

| Day | Session (ET) | UTC | Purpose |
|-----|--------------|-----|---------|
| 2024-03-06 Wed | none | — | coverage starts on a day without a session |
| 2024-03-07 Thu | 09:30–16:00 | 14:30Z–21:00Z | regular (EST) |
| 2024-03-08 Fri | 09:30–13:00 | 14:30Z–18:00Z | half day (EST) |
| 2024-03-09, 03-10 | none | — | weekend with the DST change |
| 2024-03-11 Mon | 09:30–16:00 | 13:30Z–20:00Z | regular (EDT) |
| 2024-03-12 Tue | none | — | weekday holiday |
| 2024-03-13 Wed | 10:00–15:00 | 14:00Z–19:00Z | ad hoc late open and early close: the grid anchors at the open |
| 2024-03-14 Thu | none | — | coverage ends with no session after the last close |

`nyse_test_calendar` may use `functools.cache` (tests are not subject to the domain purity guard). #8, #10 and #15 reuse these helpers.

### 8. Typing, purity guard, dependency and image

- **Dependency:** `uv add exchange-calendars` (AC16). No other dependency change; `tzdata` stays transitive.
- **mypy override** in `pyproject.toml`, after `[tool.mypy]`:

  ```toml
  [[tool.mypy.overrides]]
  module = ["exchange_calendars", "exchange_calendars.*"]
  follow_untyped_imports = true
  ```

- **Extra-flag command** (spec 004 AC14, as extended by spec 006 §2), for this branch:

  ```bash
  uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated \
    src/trading_bot/domain/*.py src/trading_bot/domain/indicators \
    src/trading_bot/domain/market_calendar
  ```

  The tech-lead ran it on the prototype of §3–§6: green.
- **Purity guard** (`tests/unit/test_domain_purity.py`): add `bisect` to `_ALLOWED_EXACT_MODULES` (plus a scanner self-test line); add `"market_calendar/nyse.py": ("exchange_calendars",)` to `_EXTRA_PREFIXES_BY_FILE` and to the test pinning that mapping; add the three new files to the expected scanned set; add `trading_bot.domain.market_calendar.sessions` to the lightweight modules and `trading_bot.domain.market_calendar.nyse` to the controls, with the fresh-interpreter report extended to `exchange_calendars` (AC14).
- **Dockerfile:** in the runtime stage, right after the TA-Lib smoke check and before `USER app`:

  ```dockerfile
  # Fail the build if the NYSE calendar cannot be built on this platform (library and zone data).
  RUN ["python", "-c", "from datetime import UTC, date, datetime; from trading_bot.domain.market_calendar.nyse import build_nyse_calendar; s = build_nyse_calendar(date(2024, 7, 1), date(2024, 7, 31)).session_bounds(date(2024, 7, 3)); raise SystemExit(0 if s is not None and s.close_time == datetime(2024, 7, 3, 17, 0, tzinfo=UTC) else 1)"]
  ```

  Verified locally against the prototype (exit code 0). Zone data comes from the `tzdata` wheel when the base image has no system zone files.

### 9. Forward compatibility

| Issue | How it uses #9 |
|-------|----------------|
| #8 | `drop_open_candle(candles, timeframe, now, *, calendar)` takes the calendar as an injected argument (there is no module-level instance). A trailing row is closed when `calendar.candle_slot(timeframe, label).close_time <= now`. Normalization maps provider labels to the canonical labels of D21 and decides the policy for `CandleLabelError` rows by `kind` (for example an after-hours `outside_session` row), and for `CalendarRangeError`. |
| #10 | `4h` bars: group `1h` bars by the `4h` slot whose `[open_time, close_time)` contains each `1h` label, from `calendar.candle_slots(Timeframe.H4, start, end)`; a hand-computed case follows §5.4 (2024-01-02: the `1h` bars labelled 14:30Z, 15:30Z, 16:30Z and 17:30Z form the first `4h` bar and those labelled 18:30Z, 19:30Z and 20:30Z the second; 2024-07-03: its four `1h` bars form one). Lookback: `calendar.closed_candles(timeframe, now, n)[0].open_time` is the earliest instant to fetch (`session_day` for `1d`), clamped to the Yahoo intraday history limit. |
| #14 | Receives frames whose last row is closed by the calendar; `candle_close_ts` stays nominal (D22). |
| #15 | Fire at `calendar.next_candle_close(timeframe, now) + delay`; recompute the next fire time from the close just used (strictly after, D19); `is_open` and `session_bounds` for market-hours checks; `closed_candles(timeframe, now, 1)` after a restart to find the last close that should have run. Simulated clocks pass `now` explicitly. |
| #16 | Builds `build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)` once in the lifespan and injects it (D18); the import cost (about 1 s cold) happens at startup, not at the first run. |
| #17, #25 | Show the `1d` `session_day` or the slot `close_time` converted to the display time zone, never the nominal close. |
| #26 | Missing-run detection compares the last recorded run with `closed_candles(timeframe, now, 1)[-1].close_time`. |

### 10. `docs/ARCHITECTURE.md`

- **Layers table, `domain/` row:** add "the market calendar (`MarketCalendar`: NYSE sessions, candle grid and real closes)".
- **"Nominal candle close" → "Obligations for later work" → "Closedness and scheduling":** keep the rule and add that the real closes come from the market calendar (`candle_slot`, `next_candle_close`, `closed_candles`), with a link to the new section.
- **New `## Market calendar` section** between `## Domain models` and `## Indicators`, short, with:
  1. what it answers, where it lives (D15) and that it is built once and injected, with a short example (`build_nyse_calendar`, `session_bounds`, `is_open`, `next_candle_close`, `candle_slot`);
  2. coverage and `CalendarRangeError`, the supported range and the recommended wiring (D18);
  3. half-open boundaries (D19);
  4. the candle grid and labels (D20, D21): the §5.4 rows for a regular day and a half day, and the label classification kinds;
  5. that `candle_close_ts` stays nominal (D22);
  6. the data horizon and what happens with closures announced after the installed library release (§2, Risks);
  7. a link to this spec.
- **`## Decisions`:** a bullet "`exchange_calendars` for NYSE sessions", with the reasons of D16 and its isolation in `nyse.py`.
- **`src/trading_bot/domain/__init__.py` docstring:** the `market_calendar` subpackage holds the session calendar (spec 009); only its `nyse` module imports `exchange_calendars`.

## Test plan

All tests are unit tests without network and without the wall clock: every instant is a literal, and the NYSE calendar is built for explicit days. Mandatory template cases:

- **Anti look-ahead:** the harness does not apply. The calendar returns no per-candle values computed from a frame, and its answers depend only on the instant passed in and on immutable session data. The rule 4 property that does apply is tested instead: a candle counts as closed at `now` if and only if `close_time <= now`, and closedness is monotonic in `now` (AC9, T13).
- **Idempotency:** the calendar does not feed signal identity; T11 pins that the `1d` nominal close differs from the real close and that nominal closes are unchanged (AC13, D22).
- **Authorization** and **secret redaction:** N/A. No Telegram, API, configuration or logging change, and no secret is involved. Messages contain no caller-provided text (AC10).

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 `Session` | unit | Field types and UTC normalization; `date` vs `datetime`; naive, `NaT`, nanosecond and non-datetime values; `open_time >= close_time` | AC1 | developer |
| T2 construction | unit | Toy calendar builds; every validation error of AC2 (name, `tzinfo`, days, `tuple` type, duplicates, unsorted, day outside the span, overnight session); empty sessions; `coverage_start`/`coverage_end` literals across DST | AC2 | developer |
| T3 arguments and coverage | unit | `"1h"` rejected; `datetime` as a day; exact coverage edges; representation independence (stdlib UTC, fixed offset, `ZoneInfo`, `pd.Timestamp`); error hierarchy and `CalendarRangeError` attributes | AC3 | developer |
| T4 sessions and `is_open` | unit | `session_bounds` for sessions, closed days and out-of-span days; half-open boundaries on the toy half day | AC4, AC5 | developer |
| T5 grid | unit | Toy grid for the regular, half and late-open days; tiling; `candle_slots` label selection, `start > end`, `start == end`, `end == coverage_end` | AC6 | developer |
| T6 `candle_slot` | unit | Round trip over every toy slot; each kind on toy labels; out-of-coverage label raises `CalendarRangeError` | AC7 | developer |
| T7 `next_candle_close` | unit | Toy literals: exact open, exact close, weekend, holiday, late open, last close raises | AC8 | developer |
| T8 `closed_candles` and value semantics | unit | Toy literals; `count` 0, negative, `bool`, `float`; not enough history raises; consistency with `next_candle_close` on the toy days; frozen fields, equality, hashing, bounded `repr`, single-line messages | AC9, AC10 | developer |
| T9 NYSE builder | unit | Name, time zone, supported range and its `TypeError`/`ValueError`; one-day and sessionless spans; determinism; range independence (2024 build vs the 2021–2027 build) | AC11 | developer |
| T10 NYSE holidays and half days | unit | The §5.1 year tables for 2024–2026 (computed by walking every day of the year), 2025-01-09 and the observance cases | AC12 | developer |
| T11 NYSE DST, grid and queries | unit | §5.2, §5.3, §5.4 and §5.5 literally; the fold and gap wall times; nominal vs real `1d` close | AC5–AC9, AC13 | developer |
| T12 purity guard | unit | §8 guard changes; fresh-interpreter import weight for `sessions` and `nyse` | AC14 | developer |
| T13 properties | unit (`@given`) | Over instants drawn in the NYSE test span (microsecond resolution) and every timeframe: AC9 consistency; `next_candle_close` non-decreasing in `now` and always `> now`; `closed_candles` suffix property; `candle_slot` round trip of the last closed slot; `is_open(t)` equals "some `1h` slot contains `t`"; each session tiled by its intraday slots; results equal for the same instant in a fixed offset, `ZoneInfo` and `pd.Timestamp` | AC3, AC6–AC9 | tester |
| T14 adversarial | unit | Wall times in both DST folds and in the spring gap; instants one microsecond around every boundary of a half day; `count=10**9`; calendars with zero sessions (every query raises or answers closed as specified); a toy calendar with a fixed-offset `tzinfo`; constructor abuse (a list of sessions, a non-`Session` item, `first_day == date.min`, `last_day == date.max`); `datetime` subclasses and `pd.Timestamp` days rejected; message bounds; queries on `candle_slots` bounds equal to `coverage_start`/`coverage_end` | AC1–AC10 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC19 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot.domain --cov-branch --cov-report=term-missing`: 100% for `market_calendar/sessions.py` and `market_calendar/nyse.py`, no regression elsewhere | AC19 |
| V3 budget | `uv run pytest --durations=40 -q`: new tests add at most 10 s, none above 2 s. Reported, never asserted | AC19 |
| V4 typing | `uv run mypy`; the §8 extra-flag command; the spec 004 `git grep` for `Any` over `src/trading_bot/domain/market_calendar` empty; `git grep -n "type: ignore" src/trading_bot/domain/market_calendar` empty; `git diff 0c4ac8b -- pyproject.toml` shows only the dependency line and the override | AC15, AC16 |
| V5 runtime dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch compared with the same command in a temporary `git worktree` of `0c4ac8b` (in the scratch directory, removed afterwards): only the AC16 differences. If a Dependabot bump reaches `main` first, compare against the rebased base and report it | AC16 |
| V6 arm64 wheels | `uv export --no-dev --no-hashes --locked --no-emit-project \| uv pip compile - --python-version 3.12 --python-platform aarch64-manylinux_2_28 --only-binary :all:` resolves all 25 runtime packages (measured on the prototype) | AC16 |
| V7 runtime-only install | `UV_PROJECT_ENVIRONMENT=<scratch>/venv uv sync --locked --no-dev`, then that interpreter runs the §8 smoke command: the calendar builds with runtime dependencies only. Do not use `uv run --no-dev` on the project environment | AC16, AC17 |
| V8 docs | Section placement and content against §10; `uv run ruff format --check` on the Python blocks | AC18 |
| V9 scope | `git diff 0c4ac8b --name-only` plus `git status --porcelain` list only the §1 files | AC20 |
| V10 secrets and language | `python scripts/secret_scan.py --history`; English-only review of the diff | — |
| V11 Docker | **BLOCKED locally** (the daemon is not running), and this change **does** alter the image. V7 is the local substitute. The authoritative checks are the PR's `Docker build (arm64)` job, which runs the §8 smoke check, and the beta deploy with `/health` verified by the lead. Report it as BLOCKED with this justification, not as PASS | AC17 |

**Testing rules for this feature** (spec 007 §14): no wall-clock or elapsed-time assertions; no platform-dependent expectations (the zone database comes from the system on Linux and from `tzdata` on Windows; America/New_York rules are identical in both for these years); literal expectations are written out, never recomputed with the code under test; the NYSE calendar is built once per test module or through `nyse_test_calendar()`.

**TDD order suggested to the developer:** dependency and mypy override (isolated lock diff) → T12 guard changes → T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 (all on the toy calendar) → `nyse.py` with T9 → T10 → T11 → `tests/fixtures/calendars.py` extraction → `Dockerfile` → docs.

## Risks and security

- **Closures announced after the installed library release.** The calendar would treat the day as a session. #15 would run at the expected closes, the provider would publish no new candle, #8 would find the last row already closed and #14 would re-evaluate a candle whose signal key already exists, so nothing is sent twice (rule 5); #26 may report stale data. An unknown ad hoc early close delays the last candles of that day until the regular close. Mitigations: Dependabot bumps of `exchange-calendars` (NYSE usually announces such days some days in advance, weather closures sometimes only the day before), and a golden table per year that a library correction would visibly change.
- **Rule changes and DST law.** A new NYSE holiday (as Juneteenth in 2022) or a change to US daylight saving time needs library and `tzdata` updates; the golden tables for past years stay valid and detect regressions. The Docker base image and the `tzdata` wheel may carry different zone database versions; zone rules for New York have been stable since 2007.
- **Supply chain.** Four new pure-Python packages load in the process when `nyse.py` is imported (§2). Mitigations: hashes pinned in `uv.lock`, reviewed Dependabot updates, the library isolated behind `nyse.py`, and the in-house table kept as the fallback if the maintainers disappear (the model does not depend on the library).
- **Horizon cliff.** A calendar built for a short span raises `CalendarRangeError` when time passes its end. Mitigation: the recommended wiring builds the full supported range (D18); the error is loud, never a silent "closed".
- **Label convention.** If #8 or #10 normalize labels differently from D21, `candle_slot` raises `off_grid` instead of producing wrong closes, which surfaces the mistake before #13 persists keys.
- **Import and build cost on the Raspberry Pi.** About 1 s to import and 0.5 s to build on the development machine, several times more on the Pi. It happens once at startup in #16; nothing imports the calendar in this feature, so startup does not change now. The image grows by five wheels (the four new packages plus `tzdata`, now installed on Linux too), under 1 MB in total.
- **Sensitive data.** None: no tokens, hosts, IPs, users, `TB_*` variables, migrations or `secrets.env` changes.
- **Unbreakable rules.** Signal-only (no order concepts), pure `domain/` (T12), closed candles (this feature provides the real closes; the anti look-ahead harness is N/A with the stated reason), idempotency (identity untouched, D22), UTC (all outputs in UTC, exchange time only inside the model), single worker (untouched), no `eval`, English only: all preserved.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; for M2 the lead stacks #9 → #8 → #10.
- **D2 (2026-09-14):** synthetic fixtures only in M1; provider fixtures arrive with #10 (refined by D29: no real Yahoo data is committed). This feature needs no provider data: the NYSE calendar is generated offline.
- Earlier project decisions that apply: yfinance stocks and ETFs first; timeframe per ticker with default `1d`.

The M2 decisions below were settled on 2026-09-17 for the whole milestone (#9, #8, #10). D27–D30 are the user's answers. D31–D34 are the tech-lead's recommended defaults, adopted by the lead without asking the user; the user can still override them, and an override needs a spec update before the affected feature is implemented. Each decision names the spec that uses it, so specs 010 (#8) and 011 (#10) can cite them.

| ID | Source | Decision | Used by |
|----|--------|----------|---------|
| D27 | User | **Markets and instruments.** v1 accepts only US-listed `EQUITY` and `ETF` instruments quoted in USD, and every ticker uses the single NYSE calendar (NYSE, Nasdaq, NYSE Arca, NYSE American and Cboe listings share NYSE hours and holidays). Indices (`^GSPC`), mutual funds, crypto, currencies, futures and non-US listings are rejected. Per-exchange calendars and 24/7 assets are later features | **This spec** (one `MarketCalendar`, NYSE only); #10 `validate_ticker` |
| D28 | User | **Trading hours.** Regular hours only: 09:30–16:00 ET, 13:00 on half days. Pre-market and after-hours trading is ignored for candles, closedness and scheduling | **This spec** (sessions and grid, D20); #8 (after-hours rows are `outside_session`); #10; #15 |
| D29 | User | **Provider fixtures.** Recorded fixtures keep Yahoo's real response structure, columns, dtypes and timestamps, with synthetic prices and volumes. No real Yahoo data is committed | #10 |
| D30 | User | **Price adjustment.** Split-adjusted prices only, without dividend adjustment (`auto_adjust=False`) | #10 |
| D31 | Lead default | **`4h` candles.** Session-anchored `4h` candles, 09:30–13:30 and 13:30–16:00 ET, with one shortened 09:30–13:00 ET candle on half days | **This spec** (D20, AC6, §4.2, §5.4); #10 resampling |
| D32 | Lead default | **Bad provider rows.** Drop an invalid row with a warning (including rows the calendar rejects); fail only when the last closed candle is affected | #8 |
| D33 | Lead default | **Candle not yet published.** When the provider has not published the candle that just closed, retry with backoff for a bounded window, then skip the run and log it | #8, #15 |
| D34 | Lead default | **`4h` candle with missing `1h` bars.** Build it from the `1h` bars that exist and log the gap | #10 |

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria, and T1–T11 fail without the implementation
- [x] Runtime dependency diff is exactly AC16; arm64 resolution (V6) and the image smoke check (AC17)
- [x] No `Any` and no `type: ignore` in `domain/market_calendar`; purity guard green (T12)
- [x] Scope limited to Design §1
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)

Reviewed on 2026-09-17 (first round): approved. `scripts/check.py` is green (3 584 passed, 10 skipped; total coverage 99.96%, the only missed line is the existing one in `config.py`). The extra-flag mypy command of §8 passes, the `Any`, `get_calendar` and clock-read greps over `domain/market_calendar` are empty, and gitleaks finds nothing in the uncommitted files. The runtime export differs from `0c4ac8b` exactly as AC16 states, it resolves for `aarch64-manylinux_2_28` with wheels only, and both `Dockerfile` smoke commands exit 0 in a runtime-only environment (the Docker build itself stays with CI). The `## Market calendar` example in `docs/ARCHITECTURE.md` runs and is formatted. Eight runtime mutations each fail the calendar tests: closes included in `is_open`, a non-strict `next_candle_close`, swapped `outside_session`/`off_grid` precedence, an unshortened last slot, the in-progress slot counted as closed, `1d` labels at the session open, an inclusive `candle_slots` end for `1d`, and the builder keeping padding sessions.

Accepted deviations:

- instants are type-checked before `to_utc`, and messages name the offending type only when it is a short ASCII identifier (`utc.py` is unchanged);
- `CalendarRangeError` and `CandleLabelError` implement `__reduce__`, so pickling keeps their fields;
- `date` and `str` subclasses are stored as plain `date` and `str`, as `to_utc` does for datetimes;
- `tests/fixtures/calendars.py` also exports `TOY_FIRST_DAY` and `TOY_LAST_DAY`, and three purity-guard tests were renamed to match their wider content;
- every `TypeError`/`ValueError` argument check runs before `CalendarRangeError`; `closed_candles` with `count=0` still checks the coverage, and `candle_slots` with `start == end` returns `()` only inside the closed coverage (AC6 read together with AC3);
- the "fewer than `count`" message does not echo `count`, so a huge integer cannot make it unbounded;
- `hash(MarketCalendar)` is the default dataclass hash, linear in the number of sessions and not cached; no consumer uses calendars as keys.

Non-blocking notes: `tests/unit/test_market_calendar_properties.py` carries one `# type: ignore[operator]` in a helper that mypy does not check (typing the argument as `Callable[..., object]` removes it); `nyse.py` keeps its own copy of the private `_plain_day` helper. The status stays `approved`, as for specs 002–008.
