# 010 — MarketDataProvider: protocol, normalization and open-candle removal

- **Status:** approved
- **Branch:** `feature/market-data-provider` (stacked on `feature/market-calendar`, #9; second of the stacked M2 series #9 → #8 → #10)
- **Spec author:** tech-lead
- **Issue:** #8 (milestone M2 · Market data)
- **Expected commit type:** `feat:`. The change adds public runtime API: two pure modules in `src/trading_bot/domain/` and the new `src/trading_bot/data/` package with the provider port that #10, #14, #15 and #19 build on. It adds no runtime dependency, so the published image only gains modules and one build-time smoke check (§14). `feat` → minor bump. Suggested squash subject: `feat: add the market data provider contract and candle normalization (#<n>)`.

## Goal

Give every candle source one async contract (`MarketDataProvider`) and one shared, pure path from whatever a provider returns to the frame the engine may evaluate:

- normalized to the spec 004 candle contract, with the canonical labels of spec 009 D21;
- stripped of bad rows, with a report (decision D32);
- stripped of every candle that is not closed at an injected `now`, by the real session closes of #9;
- guaranteed to end at the last closed candle, or failing with a typed, retryable error (D33).

Typed errors let the engine, the scheduler and Telegram tell an invalid ticker from missing data and from an unavailable provider. A fake provider lets #14 and #15 test without network.

## Out of scope

- **yfinance** (the dependency, `YFinanceProvider`, `4h` resampling, Yahoo history limits, retries with backoff and jitter, timeouts, internal rate limiting, recorded fixtures): #10. This spec fixes the contract #10 implements and the pieces it reuses (§12).
- **The bounded retry window for unpublished candles** and its configuration: #15 (with #14). #8 only detects the case (§9).
- **The engine:** choosing `lookback` from the rules, evaluating, cooldown and deduplication (#14). **Wiring** the provider and the calendar into the lifespan: #16. Nothing in the running app imports the new modules in this feature.
- **Persisting** `TickerInfo` (#12). **Rendering** errors for users in Telegram, the API or the dashboard (#19, #22, #23).
- **Repairing** bad rows. Decision D32 drops them; no value is ever modified.
- Multi-ticker fetches, caching of fetched frames, per-exchange calendars, non-USD or non-US instruments, pre-market and after-hours data (decisions D27 and D28).
- New dependencies, `TB_*` variables, migrations, `main.py`, `config.py`, `logging_setup.py`, `.dockerignore`, `docs/ROADMAP.md`, `CLAUDE.md`, `.github/`, `scripts/`, `deploy/`. `.gitignore` and `Dockerfile` change only as §14 states.

## Acceptance criteria

Timestamps written `…Z` are UTC and "ET" is `America/New_York`. "NYSE calendar" is `nyse_test_calendar()` from `tests/fixtures/calendars.py` (spec 009 §7: 2021-01-01 to 2027-12-31), and "toy calendar" is `toy_calendar()` from the same module. "Grid frame" means `session_candles(calendar, timeframe, start, end)` (§10.1): one valid candle for every slot with `start <= label < end`.

### Candle normalization (`domain/candle_normalization.py`)

- [ ] **AC1 (API and value types):**
  - the module exports exactly the names of §3 (`__all__`);
  - `NormalizedCandles` is a frozen, slotted, keyword-only dataclass with `eq=False` (it holds a frame);
  - `DroppedRow` is a frozen, slotted, keyword-only dataclass, equal by value and hashable;
  - `normalize_candles(raw, timeframe, *, calendar)` raises `TypeError` when `raw` is not a `pd.DataFrame`, `timeframe` is not a `Timeframe` member (the string `"1h"` included) or `calendar` is not a `MarketCalendar`.
- [ ] **AC2 (structural errors):**
  - each case of §3.1 raises `CandleNormalizationError`, a `ValueError`, with the stated `kind` and `column`, checked in the table's order (the first failing check raises);
  - the message is a single line under 300 characters and echoes provider-controlled text (dtype names) only as a truncated `repr()` of at most 32 characters.
- [ ] **AC3 (canonical frame):** for any accepted input, `result.candles`:
  - passes `validate_candles`;
  - has an index of dtype `datetime64[us, UTC]` named `None`, strictly increasing;
  - has exactly the `OHLCV_COLUMNS` columns, all `float64`;
  - is a new object (`result.candles is not raw`), and `raw` is unchanged (compared with a deep copy taken before the call).

  Specifically:
  - columns match case-insensitively after stripping surrounding whitespace, and other columns (`Adj Close`, `Dividends`, `Stock Splits`, non-string labels) are ignored;
  - integer, float and nullable numeric columns (`int64`, `float32`, `Int64`, `Float64`) are cast to `float64`, with `pd.NA` becoming NaN;
  - the same candles given in `America/New_York`, `UTC` or a fixed `-04:00` offset, with index units `s`, `ms`, `us` or `ns`, in any row order, give frames equal under `pd.testing.assert_frame_equal(check_exact=True)` with equal index dtypes;
  - normalization is idempotent: `normalize_candles(result.candles, …)` returns an equal frame and `dropped == ()`;
  - an input with zero rows and a valid structure gives an empty canonical frame and `dropped == ()`.
- [ ] **AC4 (dropped rows):**
  - the golden raw frame of §3.4 on the NYSE calendar (`1h`) gives literally the `dropped` tuple and the kept rows of that table;
  - its `ns` variant with an extra row labelled `2024-07-03 11:30:00.000000001` ET reports that row as `off_grid`;
  - every dropped row appears exactly once, in input position order, with the first matching reason of §3.3;
  - every `CandleLabelErrorKind` value is also a `DropReason` value.
- [ ] **AC5 (prefix closure, rule 4):** `assert_no_lookahead(lambda df: normalize_candles(df, tf, calendar=cal).candles, raw, max_cuts=len(raw))` passes for each timeframe on a sorted raw frame with unique labels that mixes valid rows with rows of every row-level reason except `missing_timestamp`, `duplicate` and `conflicting_duplicate`. The harness only accepts unique, increasing labels, which `NaT` and repeated labels break; T4 covers those three reasons instead.

### Open-candle removal (`domain/closed_candles.py`)

- [ ] **AC6 (contract):** `drop_open_candle(candles, timeframe, now, *, calendar)` performs these checks in order:
  - `TypeError` for a non-`Timeframe` or a non-`MarketCalendar`;
  - `to_utc(now)`, with its `TypeError`/`ValueError`;
  - `validate_candles(candles)`, with its `TypeError`/`CandleValidationError`;
  - `CalendarRangeError` from `calendar.closed_candles(timeframe, now, 1)` when `now` is outside the coverage or no slot has closed by `now` (toy calendar: `now = 2024-03-07T15:00:00Z` raises for every timeframe, even with an empty frame).

  It returns `candles.iloc[:k]`, a new object with the same dtypes and index unit, where `k` is the number of rows labelled at or before the label of the last slot closed at `now`. `candles` is never modified.
- [ ] **AC7 (golden table):** on grid frames of every timeframe with labels in `[2023-12-27T00:00:00Z, 2024-11-06T00:00:00Z)` on the NYSE calendar, the last kept label for each `now` of §4.2 is literally the one in the table. These rows cover exact closes and one microsecond before them, weekends, holidays, a half day and both DST changes.
- [ ] **AC8 (boundaries, past `now` and rejected labels):**
  - a row whose slot closes exactly at `now` is kept, and one closing one microsecond after `now` is dropped (D19);
  - for a `now` in the past, every later row is dropped, not only the in-progress candle;
  - when no row is closed, the result is an empty frame with the input's columns and index dtype;
  - rows labelled after the last closed slot are dropped whatever their label;
  - the last returned row goes through `calendar.candle_slot` and raises the calendar's error for a rejected label, with the kinds and cases of §4.3.
- [ ] **AC9 (anti look-ahead, rule 4):**
  - `assert_no_lookahead(lambda df: drop_open_candle(df, tf, now, calendar=cal), frame, max_cuts=len(frame))` passes on grid frames from `2024-06-24T00:00:00Z` to `2024-07-10T00:00:00Z` for every timeframe and for `now` in `2024-07-03T17:00:00Z`, `2024-07-05T14:30:00Z` and `2024-07-09T20:00:00Z`;
  - two control functions raise `LookaheadError` with kind `result_appeared`: one that always drops the last row, and one that keeps rows labelled before the frame's last label.

### Data layer (`src/trading_bot/data/`)

- [ ] **AC10 (errors):**
  - the hierarchy, attributes and `retryable` flags are exactly those of §5;
  - every error is a `MarketDataError` and none is a `ValueError`;
  - constructors normalize `ticker` with `normalize_ticker` and convert labels with `to_utc`, and reject a message that contains `"\n"` or `"\r"` or is longer than 300 characters with `ValueError`;
  - the messages of §5 contain the stated elements;
  - `ProviderDataError.kind` must match `[a-z_]{1,32}`.
- [ ] **AC11 (`TickerInfo` and the D27 policy):**
  - `TickerInfo` validates and normalizes as §6 states (`TypeError` for wrong types, `ValueError` for bad values); it is frozen, equal by value and hashable;
  - `parse_ticker` raises `InvalidTickerError(reason=MALFORMED)` for text that `normalize_ticker` rejects, and `TypeError` for a non-`str`;
  - `ensure_supported` returns the same object for supported instruments and raises the §6.2 reasons literally, in the stated order.
- [ ] **AC12 (`CandleRequest` and `MAX_LOOKBACK`):**
  - `CandleRequest` validates in the order ticker, timeframe, lookback, now, as §7.1 states;
  - `MAX_LOOKBACK == 5_000`;
  - a test computes `REGISTRY.stable_warmup` at every indicator's maximum parameters (`macd` with `fast=100, slow=200, signal=100`) and asserts that the largest value plus one (a crossover) is at most `MAX_LOOKBACK`. Today it is 2 255, from `ema` `length=500`.
- [ ] **AC13 (Protocol):**
  - `MarketDataProvider` is a `typing.Protocol` (not `runtime_checkable`) with exactly the two async methods of §7.2;
  - `tests/fixtures/fake_provider.py` holds a typed function that returns a `FakeMarketDataProvider` as a `MarketDataProvider`, so strict mypy checks structural conformance.
- [ ] **AC14 (`candle_window`):** returns `CandleWindow(first=slots[0], last=slots[-1])` for `slots = calendar.closed_candles(request.timeframe, request.now, request.lookback)`, and the rows of §8.1 match literally. `CalendarRangeError` propagates.
- [ ] **AC15 (`prepare_candles`):**
  - it runs the steps of §8.2 in order;
  - the outcome table of §8.3 is verified literally (returned labels or error type, `reason`, `expected_label`, `last_label`, `kind`);
  - `ProviderDataError` is raised `from None` (`__cause__ is None` and `__suppress_context__` is true);
  - the logging of §8.4 on the golden frame is verified literally (logger name, levels, message text and record order), and no other record is emitted.
- [ ] **AC16 (fake provider):** `FakeMarketDataProvider` behaves as §10.2 states:
  - calls are recorded;
  - scripted failures are raised first-in, first-out;
  - a missing frame raises `NoDataError`;
  - `fetch_candles` output equals `prepare_candles` on the stored frame;
  - `validate_ticker` covers `malformed`, `not_found` and the D27 policy;
  - each call yields to the event loop once;
  - its output passes `assert_closed_candles`.
- [ ] **AC17 (shared test fixtures):**
  - `session_candles`, `provider_shaped` and `assert_closed_candles` behave as §10.1 and §10.3 state;
  - `assert_closed_candles` raises `AssertionError` for each violation listed there, with an explicit `raise`, not a bare `assert`.

### Purity, typing, docs, gate and scope

- [ ] **AC18 (purity):**
  - `domain/candle_normalization.py` and `domain/closed_candles.py` pass the purity guard with the default allowlist: no per-file allowance, no clock, no module-level mutable state;
  - `git grep -n -E "\.now\(|utcnow|today\(|time\.time\(|monotonic\(" src/trading_bot/data` matches nothing (providers never read the wall clock, D36);
  - no module under `src/trading_bot/data/` imports a network library.
- [ ] **AC19 (typing):**
  - `uv run mypy` (strict) passes with no configuration change;
  - the extra-flag command of §11 passes;
  - `typing.Any` (the spec 004 `git grep`) and `# type: ignore` are absent from the new `src/` files and from the new fixtures;
  - every new module except `__init__.py` defines `__all__` with exactly the names of §3–§8.
- [ ] **AC20 (docs):**
  - `docs/ARCHITECTURE.md` gains a `## Market data` section right after `## Market calendar` and updates the rows and paragraphs listed in §13;
  - `src/trading_bot/domain/__init__.py` mentions the two new modules;
  - `src/trading_bot/data/__init__.py` has a package docstring;
  - Python blocks pass `ruff format --check`.
- [ ] **AC21 (gate, coverage and budget):**
  - `uv run python scripts/check.py` is green;
  - every new module under `src/` has 100% line and branch coverage;
  - the new tests need no network, read no wall clock, add at most **10 s** to `pytest`, and no single new test takes more than **2 s** (evidence from `--durations`, reported, never asserted).
- [ ] **AC22 (scope):**
  - only the files of §1 change compared with the `feature/market-calendar` commit this branch starts from, plus this spec;
  - `pyproject.toml` and `uv.lock` are unchanged.
- [ ] **AC23 (package tracked, linted and shipped, §14):**
  - `.gitignore` gains exactly one line, `!src/trading_bot/data/`, right after the existing `data/` line, and nothing else changes;
  - `git check-ignore -v src/trading_bot/data/pipeline.py` exits 1 (not ignored), and every file of `src/trading_bot/data/` appears in `git status --porcelain` as untracked or added;
  - `git check-ignore data/x deploy/data/x tests/fixtures/data/x` prints all three paths and exits 0 (still ignored);
  - `uv run ruff check --show-files .` lists the five `src/trading_bot/data/*.py` files, so the gate's `ruff check .` and `ruff format --check .` steps cover the package;
  - the `Dockerfile` runtime stage gains the one-line import smoke check of §14, and the PR's `Docker build (arm64)` job is green.

## Design

### 0. Decisions

Decisions D1–D34 are recorded in specs 003–009, and this spec relies on D19, D21, D22 and D27–D34. The new decisions are technical.

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D35 | Layout | **Pure frame operations live in `domain/`:** `candle_normalization.py` (`normalize_candles`) and `closed_candles.py` (`drop_open_candle`). **The provider port and the I/O-facing glue live in the new `data/` package:** `errors.py`, `tickers.py`, `provider.py` and `pipeline.py`. `pipeline.py` is the only module that logs. Test doubles and shared helpers live in `tests/fixtures/` | Both functions take a frame, a timeframe, an instant and the injected calendar, and return a frame: no I/O, no clock. Closedness is a domain rule (rule 4, like the calendar in D15), and the rows they keep are what the evaluator sees, so they get the purity guard and the look-ahead obligations of `docs/ARCHITECTURE.md` at no cost. D32 requires warnings, and `domain/` cannot log, so normalization **returns a report** (`DroppedRow`) and the data layer logs it. The port, the error types and the ticker policy are provider-facing and are imported by #14, #15, #19 and #22 through `data/`. Rejected: putting everything in `data/`, which would leave the two pure functions outside the guard and the look-ahead requirement. Also rejected: a per-provider copy of the policy, which would let the fake and yfinance drift apart. |
| D36 | Protocol | **Async methods, with an explicit keyword-only `now`:** `async def fetch_candles(ticker, timeframe, lookback, *, now)` and `async def validate_ticker(ticker)`. Providers never read the wall clock | The app is one asyncio process (FastAPI, `AsyncIOScheduler`, python-telegram-bot 22 is async). yfinance blocks, so #10 runs its calls in `asyncio.to_thread` and waits with `asyncio.sleep` between retries, which lets #16 cancel a run at shutdown instead of waiting on a sleeping thread, and keeps rate limiting a loop-local primitive with no locks. `now` extends the issue signature. It is the "injected clock": the provider needs it to plan the window, drop the open candle and know which candle must be last (D33), and one `now` per run keeps simulated-clock tests (#15) and retries deterministic. mypy rejects a sync implementation and one without `now` (verified on the prototype). |
| D37 | Lookback | `lookback` is a **count of closed candles** ending at the last slot closed at `now`, `1 <= lookback <= MAX_LOOKBACK = 5_000`. The calendar turns it into a time window with `candle_window` (`closed_candles(timeframe, now, lookback)`); a provider returns at most `lookback` rows, and fewer when the history is shorter or a provider limit caps it | Rules express warmup in candles (`rule.stable_warmup()`), and counting calendar days would be wrong across nights, weekends and holidays (spec 004 §5). The largest `stable_warmup` in the catalog is 2 254 (`ema` `length=500`), 2 255 with a crossover (measured), so 5 000 leaves room and stays within the NYSE builder's supported range: 5 000 candles closed by 2024-01-02 start on 2004-02-20 (`1d`), 2014-01-10 (`4h`) and 2021-02-26 (`1h`), all measured. An explicit bound turns a runaway value into a `ValueError` instead of a calendar walk. |
| D38 | Errors | `MarketDataError(Exception)` with a class-level `retryable` flag and five subclasses (§5): `InvalidTickerError` (with a `reason`), `NoDataError`, `ProviderDataError`, `CandleNotPublishedError` (retryable) and `ProviderUnavailableError` (retryable, with a `failure` kind and `retry_after`). None subclasses `ValueError`. Messages are built only from normalized tickers, timeframe codes, enum values and ISO timestamps; provider errors never chain third-party exceptions | Callers branch on class (#10 retries only `ProviderUnavailableError`, #15 retries only `CandleNotPublishedError`) and on `retryable` (#26 alert categories). Not being a `ValueError` keeps a caller's `except ValueError` for programming errors (`to_utc`, `Timeframe`) from swallowing provider failures. Chained HTTP exceptions can carry URLs whose query strings hold Yahoo's session crumb, and `logger.exception` would print them past the redaction filter, which only knows Telegram-token shapes and configured secrets. |
| D39 | Tickers | `TickerInfo(symbol, name, asset_type, exchange, currency)` with closed enums (`AssetType` includes the unsupported types; `Exchange` has ISO 10383 codes for the D27 venues plus `OTHER`). **One shared `ensure_supported(info)` enforces D27**, and every provider calls it before `validate_ticker` returns | The policy lives in one tested function instead of in each provider, and the fake rejects exactly what production rejects. `TickerInfo` can describe unsupported instruments, so the policy can be tested on values and #19 can explain a rejection ("asset type index"). OTC markets map to `Exchange.OTHER` and are rejected: D27 lists exchange listings (NYSE, Nasdaq, NYSE Arca, NYSE American, Cboe), and OTC quotes are not listings. |
| D40 | Canonical frame | `normalize_candles` converts the index to UTC with unit `us`, name `None`, sorted; selects the five columns case-insensitively and ignores the rest; accepts integer and float dtypes (nullable included) and casts them to `float64`; rejects anything structurally ambiguous with `CandleNormalizationError` | Providers produce different units: the yfinance epoch-seconds path yields `s`, while strings and `Timestamp`s yield `us` in pandas 3.0.5 (measured). A fixed unit makes normalization idempotent and makes two providers return identical frames for identical data. `us` is lossless for every label that survives (sub-microsecond labels are never on the grid), matches `to_utc` and signal identity (spec 004), and matches `synthetic_candles`. A naive index is rejected, never localized: guessing a zone would silently shift every key. |
| D41 | Bad rows | Implements D32 with twelve `DropReason`s in a fixed order (§3.3): label reasons first (`missing_timestamp`, `outside_calendar`, the three calendar kinds), then value reasons, then duplicates among the survivors. Identical copies of a label keep the first copy (`duplicate`); differing copies are all dropped (`conflicting_duplicate`) | Label reasons first means the report names the cause the fix needs (an after-hours bar is `outside_session` even if its values are also broken). Values before duplicates keeps a valid bar when the provider also sent a provisional NaN copy of it. Dropping every conflicting copy follows D32: nothing tells which copy is right. Each rule is row-local or compares rows with the same label, so the output is prefix-closed (AC5, verified with the harness on the prototype). |
| D42 | `drop_open_candle` | Keeps the rows labelled at or before the label of the last slot closed at `now` (`calendar.closed_candles(timeframe, now, 1)`) with one binary search, drops every later row, and checks only the **last returned** label with `calendar.candle_slot` | For canonical labels, "label ≤ the last closed label" is exactly "real close ≤ `now`" (D19, half-open), because slot closes increase with labels. Any row labelled after the last closed slot is dropped, whatever its label, so no candle that opened after the last close can survive. The last returned row is the candle the engine evaluates and whose label becomes `candle_close_ts`, so a rejected label there fails loudly; validating every label is `normalize_candles`' job and would repeat it at O(n) calendar lookups. Dropping all later rows (not only one) keeps a past `now` correct for simulations and gives the look-ahead property directly (AC9). |
| D43 | Last candle | `prepare_candles` requires the last returned row to be the last slot closed at `now`. Otherwise it raises `CandleNotPublishedError` (`invalid` when a dropped row had that label, `missing` otherwise), or `NoDataError` when nothing closed remains and nothing was dropped at that label. Dropped rows log one record per reason: `WARNING` when labelled at or before the last closed slot (or `NaT`), `DEBUG` when later, and exact duplicates always at `DEBUG` | This is D32 ("fail only if the last closed candle is affected") and D33 ("not yet published") as one typed, retryable failure. The engine can never re-evaluate a stale frame by accident: a closure the calendar library does not know yet (spec 009 Risks) now surfaces as a logged retryable error instead of a silent re-evaluation. Rows after the last closed slot belong to the in-progress candle and would otherwise produce a warning on every intraday run. |
| D44 | D33 split | **#8 detects** (`CandleNotPublishedError`, the Protocol contract). **#10 does not retry it** (its internal retries cover `ProviderUnavailableError` only). **#15 owns the bounded window** (§9) | Publication delays last minutes and transport retries last seconds, so mixing them in one loop would hold a provider call for minutes and hide which one was exhausted. The scheduler already owns the close delay, the next close and "never two concurrent runs". |
| D45 | Test doubles | `FakeMarketDataProvider` in `tests/fixtures/fake_provider.py` runs the **real** `prepare_candles` on stored raw frames, with scripted failures and recorded calls. Shared helpers: `session_candles`, `provider_shaped` and `assert_closed_candles` | A fake that skips the pipeline would let engine tests pass on frames production can never return. Scripted `MarketDataError`s cover the engine's isolation and retry paths. `synthetic_candles` labels are not on the NYSE grid (its `1d` labels are 00:00 UTC, `off_grid`), so engine tests need calendar-aligned frames. `assert_closed_candles` gives #10 and #14 one executable statement of the return contract. |
| D46 | Async tests | Tests call coroutines with `asyncio.run(...)` | No new dev dependency (pytest-asyncio) and no reliance on the anyio plugin that FastAPI happens to install. The fake never needs a running loop between calls. |
| D66 | Packaging (added in implementation) | Re-include the package with `!src/trading_bot/data/` right after `data/` in `.gitignore`, and add a build-time import smoke check to the `Dockerfile` (§14). `.dockerignore` stays unchanged | The `data/` rule (local runtime data) matches at any depth, so the package could not be committed, the gate's ruff steps skipped it, and hatchling left it out of any wheel built next to the repository's `.gitignore` (measured). The negation keeps every runtime data directory ignored. Nothing imports the package at startup until #16, so without the smoke check neither the image build nor `/health` would notice it missing; same reasoning as spec 009 D25. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/candle_normalization.py` | developer | §3 |
| `src/trading_bot/domain/closed_candles.py` | developer | §4 |
| `src/trading_bot/domain/__init__.py` | developer | Docstring package map mentions both modules (§13) |
| `src/trading_bot/data/__init__.py` | developer | Package docstring: the provider port, errors, ticker policy and pipeline; no re-exports |
| `src/trading_bot/data/errors.py` | developer | §5 |
| `src/trading_bot/data/tickers.py` | developer | §6 |
| `src/trading_bot/data/provider.py` | developer | §7 |
| `src/trading_bot/data/pipeline.py` | developer | §8 |
| `tests/fixtures/session_candles.py` | developer | `session_candles`, `provider_shaped` (§10.1) |
| `tests/fixtures/fake_provider.py` | developer | `FakeMarketDataProvider`, `FetchCall`, conformance function (§10.2) |
| `tests/fixtures/provider_contract.py` | developer | `assert_closed_candles` (§10.3) |
| `tests/unit/test_candle_normalization.py` | developer | TDD: T1–T4 |
| `tests/unit/test_closed_candles.py` | developer | TDD: T5–T6 |
| `tests/unit/test_market_data_lookahead.py` | developer | TDD: T7 |
| `tests/unit/test_market_data_errors.py` | developer | TDD: T8 |
| `tests/unit/test_tickers.py` | developer | TDD: T9 |
| `tests/unit/test_market_data_pipeline.py` | developer | TDD: T10–T11 |
| `tests/unit/test_fake_market_data_provider.py` | developer | TDD: T12–T13 |
| `tests/unit/test_domain_purity.py` | developer | T14: the two new modules in the expected scanned set, with no allowance |
| `tests/unit/test_market_data_properties.py` | tester | T15 |
| `tests/unit/test_market_data_adversarial.py` | tester | T16 |
| `docs/ARCHITECTURE.md` | developer | §13; explicitly authorized by this spec |
| `.gitignore` | developer | One added line, `!src/trading_bot/data/`, right after `data/` (§14); explicitly authorized by this spec |
| `Dockerfile` | developer | The one-line import smoke check of §14; explicitly authorized by this spec |
| `docs/specs/010-market-data-provider.md` | tech-lead | This spec |

No migrations, no `TB_*` variables and no dependency changes. `tests/fixtures/calendars.py` (#9) is reused unchanged.

### 2. Data flow

```text
provider (#10)                 data/pipeline.py                         domain/ (pure)
raw frame ──────────────────▶ prepare_candles(raw, request, calendar)
                                 1 normalize_candles ─────────────────▶ canonical frame + DroppedRow report
                                 2 last = closed_candles(tf, now, 1)
                                 3 log the report (WARNING / DEBUG)
                                 4 drop_open_candle ──────────────────▶ rows closed at now
                                 5 last row == last.label?  no → CandleNotPublishedError | NoDataError
                                 6 keep the last `lookback` rows
engine (#14) ◀── frame: valid, canonical labels, all closed, ends at the last closed candle
```

### 3. `domain/candle_normalization.py`

```python
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "CandleNormalizationError",
    "DropReason",
    "DroppedRow",
    "NormalizationErrorKind",
    "NormalizedCandles",
    "normalize_candles",
]


class NormalizationErrorKind(StrEnum):
    INDEX_TYPE = "index_type"
    NAIVE_INDEX = "naive_index"
    MULTIINDEX_COLUMNS = "multiindex_columns"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    MISSING_COLUMN = "missing_column"
    NON_NUMERIC_COLUMN = "non_numeric_column"


class CandleNormalizationError(ValueError):
    kind: NormalizationErrorKind
    column: str | None  # the canonical column name, when the kind is about one column


class DropReason(StrEnum):
    MISSING_TIMESTAMP = "missing_timestamp"
    OUTSIDE_CALENDAR = "outside_calendar"
    NOT_A_SESSION = "not_a_session"
    OUTSIDE_SESSION = "outside_session"
    OFF_GRID = "off_grid"
    MISSING_VALUE = "missing_value"
    INFINITE_VALUE = "infinite_value"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    INCONSISTENT_RANGE = "inconsistent_range"
    DUPLICATE = "duplicate"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"


@dataclass(frozen=True, slots=True, kw_only=True)
class DroppedRow:
    position: int  # 0-based row position in the raw frame
    label: pd.Timestamp | None  # the label in UTC (input unit kept); None for NaT
    reason: DropReason


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class NormalizedCandles:
    candles: pd.DataFrame  # the canonical frame (§3.2)
    dropped: tuple[DroppedRow, ...]  # in input position order


def normalize_candles(
    raw: pd.DataFrame, timeframe: Timeframe, *, calendar: MarketCalendar
) -> NormalizedCandles: ...
```

#### 3.1 Structural checks

They run in this order after the argument type checks. The first failing check raises `CandleNormalizationError`.

| # | Kind | Fails when | `column` | Test cases (at least) |
|---|------|------------|----------|-----------------------|
| 1 | `index_type` | the index is not a `pd.DatetimeIndex` | `None` | `RangeIndex`; an index of ISO strings; `PeriodIndex` |
| 2 | `naive_index` | the index has no time zone | `None` | naive index; naive empty index |
| 3 | `multiindex_columns` | the columns are a `MultiIndex` (the yfinance multi-ticker `download` shape) | `None` | `(Price, Ticker)` columns |
| 4 | `ambiguous_column` | two string labels match the same canonical name after `strip().lower()` | that name | `Close` and `close`; `Volume` and ` volume ` |
| 5 | `missing_column` | a canonical name matches no label (the first missing one in `OHLCV_COLUMNS` order) | that name | no `Volume`; only `Adj Close` for the close; no columns at all → `"open"` |
| 6 | `non_numeric_column` | a matched column's dtype is not an integer or float dtype (`pd.api.types.is_integer_dtype` or `is_float_dtype`), bool and nullable `boolean` included | that name | `object` with numbers; `string`; `bool`; `boolean`; `datetime64`; `category` |

Messages: `"<kind>: <description>"`, one line. They name the canonical column and, for `non_numeric_column`, the dtype as a truncated `repr()`. Raw column labels are never echoed (provider-controlled text). An empty raw frame with a valid structure passes these checks.

#### 3.2 Canonical frame

- **Index:** `raw.index.tz_convert("UTC")` for the kept rows, `.as_unit("us")`, name `None`, `freq` `None`, sorted by label with a stable sort.
- **Columns:** exactly `OHLCV_COLUMNS`, built from the matched columns with `astype("float64")` and `pd.NA` → NaN, before any row check. The column index name is `None`.
- The result is built from new arrays; `raw` is never modified (no `inplace`, no assignment into `raw`). `validate_candles` is called on the result as a postcondition, and any failure there is a bug that propagates.
- Implementation guidance (not an AC): classify labels without one calendar call per valid row. For example, build the set of grid labels with one `calendar.candle_slots(timeframe, start, end)` call over the span of labels inside the coverage, and call `calendar.candle_slot` only for labels outside that set, to learn their kind. No test asserts elapsed time.

#### 3.3 Row reasons and precedence

Each row gets the **first** matching reason, and rows with a reason are dropped:

| Order | Reason | The row… |
|-------|--------|----------|
| 1 | `missing_timestamp` | has a `NaT` label |
| 2 | `outside_calendar` | has a label outside `[calendar.coverage_start, calendar.coverage_end)` |
| 3 | `off_grid` | has a label with sub-microsecond precision (never a grid label; checked without the calendar, because `to_utc` rejects such values) |
| 4 | `not_a_session`, `outside_session`, `off_grid` | has a label that `calendar.candle_slot(timeframe, label)` rejects, with the `CandleLabelError` kind as the reason |
| 5 | `missing_value` | has NaN in any of the five values |
| 6 | `infinite_value` | has ±inf in any of the five values |
| 7 | `non_positive_price` | has `open`, `high`, `low` or `close` `<= 0` |
| 8 | `negative_volume` | has `volume < 0` |
| 9 | `inconsistent_range` | has `high < low`, or `open` or `close` outside `[low, high]` |
| 10 | `duplicate` | shares its label with an earlier surviving row, and all surviving copies of that label have equal values (`==` on all five); the first copy is kept |
| 11 | `conflicting_duplicate` | shares its label with another surviving row, and the copies differ in some value; every copy gets this reason |

Declaration order of `DropReason` is the log order of §8.4: the precedence order, with `off_grid` declared once.

#### 3.4 Golden raw frame

NYSE calendar, timeframe `1h`. The raw frame is shaped like yfinance's `Ticker.history(auto_adjust=False)`:

- **Index:** `datetime64[s, America/New_York]` named `"Datetime"`.
- **Columns:** `Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`, with `Volume` as `int64`.
- **Valid values:** `Open 100.0, High 101.0, Low 99.0, Close 100.5, Adj Close = Close, Volume 1000, Dividends 0.0, Stock Splits 0.0`.

Verified on the prototype:

| Position | Label (ET) | Change from valid | Label (UTC) | Result |
|----------|------------|-------------------|-------------|--------|
| 0 | 2024-07-02 09:30 | — | 2024-07-02T13:30Z | kept |
| 1 | 2024-07-02 10:30 | `Close` NaN | 14:30Z | `missing_value` |
| 2 | 2024-07-02 11:30 | — | 15:30Z | kept |
| 3 | 2024-07-02 12:30 | `High` 98.0 | 16:30Z | `inconsistent_range` |
| 4 | 2024-07-02 13:30 | — | 17:30Z | kept |
| 5 | 2024-07-02 13:30 | — (same values as position 4) | 17:30Z | `duplicate` |
| 6 | 2024-07-02 14:30 | — | 18:30Z | `conflicting_duplicate` |
| 7 | 2024-07-02 14:30 | `Close` 100.25 | 18:30Z | `conflicting_duplicate` |
| 8 | 2024-07-02 15:30 | `Volume` -1 | 19:30Z | `negative_volume` |
| 9 | 2024-07-02 16:00 | — | 20:00Z | `outside_session` (the close) |
| 10 | 2024-07-02 10:00 | — | 14:00Z | `off_grid` |
| 11 | 2024-07-03 08:00 | — | 2024-07-03T12:00Z | `outside_session` (pre-market) |
| 12 | 2024-07-03 09:30 | `Open` 0.0 | 13:30Z | `non_positive_price` |
| 13 | 2024-07-03 10:30 | `High` inf | 14:30Z | `infinite_value` |
| 14 | 2024-07-03 11:30 | — | 15:30Z | kept |
| 15 | 2024-07-03 12:30 | — | 16:30Z | kept |
| 16 | 2024-07-04 10:30 | — | 2024-07-04T14:30Z | `not_a_session` (holiday) |
| 17 | `NaT` | — | — | `missing_timestamp` |
| 18 | 2020-12-31 10:30 | — | 2020-12-31T15:30Z | `outside_calendar` |
| 19 | 2024-07-02 11:00 | `Close` NaN | 2024-07-02T15:00Z | `off_grid` (label reasons first) |
| 20 | 2024-07-02 09:30 | `Close` NaN | 13:30Z | `missing_value` (values before duplicates: position 0 is kept) |

The result has five rows labelled `2024-07-02T13:30Z`, `15:30Z`, `17:30Z`, `2024-07-03T15:30Z` and `16:30Z`, all with `open 100.0, high 101.0, low 99.0, close 100.5, volume 1000.0`. `dropped` lists positions 1, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19 and 20, in that order.

### 4. `domain/closed_candles.py`

```python
from datetime import datetime

import pandas as pd

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe

__all__ = ["drop_open_candle"]


def drop_open_candle(
    candles: pd.DataFrame, timeframe: Timeframe, now: datetime, *, calendar: MarketCalendar
) -> pd.DataFrame: ...
```

#### 4.1 Semantics

1. Argument checks in the AC6 order.
2. `last = calendar.closed_candles(timeframe, now, 1)[-1]`: the slot with the greatest `close_time <= now`.
3. `k = candles.index.searchsorted(last.label, side="right")`, and the result is `candles.iloc[:k]`.
4. If `k > 0`, `calendar.candle_slot(timeframe, result.index[-1])` must succeed; its `CandleLabelError`, `CalendarRangeError` or `ValueError` (a sub-microsecond label, from `to_utc`) propagates.

"Open candle" in the name means every row not closed at `now`: the in-progress candle, and for a `now` in the past, every later candle. The function works on labels, so it does not depend on candle values, frame length or rows after the cut (AC9). Precondition: labels are canonical (`normalize_candles` guarantees it), and only the last returned label is checked (D42). No test asserts what happens to a rejected label earlier in the frame.

#### 4.2 Golden table (AC7)

The last kept label → its real close, for grid frames with labels in `[2023-12-27T00:00:00Z, 2024-11-06T00:00:00Z)` on the NYSE calendar. Month and day are omitted when they equal those of `now`. Verified on the prototype, and consistent with spec 009 §5.3.

| `now` | Why | `1h` | `4h` | `1d` |
|-------|-----|------|------|------|
| 2024-07-02T19:59:59.999999Z | 1 µs before a regular close (EDT) | 18:30Z → 19:30Z | 13:30Z → 17:30Z | 07-01T04:00Z → 20:00Z |
| 2024-07-02T20:00:00Z | exactly the close: kept (D19) | 19:30Z → 20:00Z | 17:30Z → 20:00Z | 07-02T04:00Z → 20:00Z |
| 2024-07-03T16:59:59.999999Z | inside the last half-day slot | 15:30Z → 16:30Z | 07-02T17:30Z → 20:00Z | 07-02T04:00Z → 20:00Z |
| 2024-07-03T17:00:00Z | half-day close | 16:30Z → 17:00Z | 13:30Z → 17:00Z | 07-03T04:00Z → 17:00Z |
| 2024-07-04T15:00:00Z | holiday | 07-03T16:30Z → 17:00Z | 07-03T13:30Z → 17:00Z | 07-03T04:00Z → 17:00Z |
| 2024-07-05T13:30:00Z | exactly the next open: nothing new closed | 07-03T16:30Z → 17:00Z | 07-03T13:30Z → 17:00Z | 07-03T04:00Z → 17:00Z |
| 2024-07-05T14:30:00Z | first `1h` close after the holiday | 13:30Z → 14:30Z | 07-03T13:30Z → 17:00Z | 07-03T04:00Z → 17:00Z |
| 2024-07-06T12:00:00Z | Saturday | 07-05T19:30Z → 20:00Z | 07-05T17:30Z → 20:00Z | 07-05T04:00Z → 20:00Z |
| 2024-07-08T13:29:59.999999Z | Monday before the open | 07-05T19:30Z → 20:00Z | 07-05T17:30Z → 20:00Z | 07-05T04:00Z → 20:00Z |
| 2024-03-08T20:59:59.999999Z | 1 µs before a close (EST) | 19:30Z → 20:30Z | 14:30Z → 18:30Z | 03-07T05:00Z → 21:00Z |
| 2024-03-08T21:00:00Z | close before the spring DST weekend | 20:30Z → 21:00Z | 18:30Z → 21:00Z | 03-08T05:00Z → 21:00Z |
| 2024-03-11T14:29:59.999999Z | first EDT session, before its first close | 03-08T20:30Z → 21:00Z | 03-08T18:30Z → 21:00Z | 03-08T05:00Z → 21:00Z |
| 2024-03-11T14:30:00Z | first `1h` close in EDT | 13:30Z → 14:30Z | 03-08T18:30Z → 21:00Z | 03-08T05:00Z → 21:00Z |
| 2024-03-11T20:00:00Z | first EDT close | 19:30Z → 20:00Z | 17:30Z → 20:00Z | 03-11T04:00Z → 20:00Z |
| 2024-11-01T20:00:00Z | close before the fall DST weekend | 19:30Z → 20:00Z | 17:30Z → 20:00Z | 11-01T04:00Z → 20:00Z |
| 2024-11-04T15:29:59.999999Z | first EST session, before its first close | 11-01T19:30Z → 20:00Z | 11-01T17:30Z → 20:00Z | 11-01T04:00Z → 20:00Z |
| 2024-11-04T15:30:00Z | first `1h` close in EST | 14:30Z → 15:30Z | 11-01T17:30Z → 20:00Z | 11-01T04:00Z → 20:00Z |
| 2024-01-02T14:30:00Z | open after the New Year holiday and weekend | 2023-12-29T20:30Z → 21:00Z | 2023-12-29T18:30Z → 21:00Z | 2023-12-29T05:00Z → 21:00Z |
| 2024-01-02T15:30:00Z | first close of 2024 | 14:30Z → 15:30Z | 2023-12-29T18:30Z → 21:00Z | 2023-12-29T05:00Z → 21:00Z |

On a `1h` grid frame from `2024-07-02T00:00:00Z` to `2024-07-06T00:00:00Z` (18 rows), `now = 2024-07-02T19:59:59.999999Z` keeps 6 rows and `2024-07-02T20:00:00Z` keeps 7. A `1h` grid frame for 2024-07-05 alone at `now = 2024-07-05T14:00:00Z` gives an empty frame with the five `float64` columns and a `datetime64[us, UTC]` index.

#### 4.3 Rejected labels and edge cases (AC8)

The table uses the NYSE calendar unless it says otherwise. Each frame is valid for `validate_candles` and ends with the stated row.

| Timeframe | Last row of the frame | `now` | Result |
|-----------|-----------------------|-------|--------|
| `1h` | `2024-07-02T14:00:00Z` (10:00 ET) | `2024-07-02T20:00:00Z` | `CandleLabelError`, kind `off_grid` |
| `1h` | `2024-07-02T12:00:00Z` (08:00 ET) | `2024-07-02T20:00:00Z` | `CandleLabelError`, kind `outside_session` |
| `1d` | `2024-07-03T00:00:00Z` (a daily label at 00:00 UTC) | `2024-07-03T17:00:00Z` | `CandleLabelError`, kind `off_grid` |
| `1d` | `2024-07-04T04:00:00Z` (holiday) | `2024-07-05T21:00:00Z` | `CandleLabelError`, kind `not_a_session` |
| `1h` | the 7 grid rows of 2024-07-02, then `2024-07-02T21:00:00Z` (17:00 ET, after the last closed slot) | `2024-07-02T20:00:00Z` | the after-hours row is dropped, no error; 7 of 8 rows kept, the last labelled `19:30Z` |
| `1d` | the 11 grid rows from 2024-06-24 to 2024-07-09 | `2024-06-28T20:00:00Z` (a past `now`) | 5 rows kept, the last labelled `2024-06-28T04:00Z`; every later row is dropped |
| any | empty frame | toy `2024-03-07T15:00:00Z` (no slot closed yet) | `CalendarRangeError` |
| any | any valid frame | NYSE `2028-01-01T05:00:00Z` (exactly `coverage_end`: 00:00 New York time after 2027-12-31) | `CalendarRangeError` |
| any | any valid frame | NYSE `2028-01-02T00:00:00Z` (after `coverage_end`) | `CalendarRangeError` |
| any | any valid frame | NYSE `2021-01-01T04:59:59.999999Z` (one microsecond before `coverage_start`, `2021-01-01T05:00:00Z`) | `CalendarRangeError` |

`now = 2028-01-01T00:00:00Z` is still inside the NYSE test calendar (19:00 ET on 2027-12-31, a regular session because New Year's Day 2028 falls on a Saturday) and does not raise.

### 5. `data/errors.py`

```python
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "CandleNotPublishedError",
    "InvalidTickerError",
    "InvalidTickerReason",
    "MarketDataError",
    "NoDataError",
    "ProviderDataError",
    "ProviderFailure",
    "ProviderUnavailableError",
    "UnpublishedReason",
]


class MarketDataError(Exception):
    retryable: ClassVar[bool] = False
    ticker: str | None  # normalized with normalize_ticker
    timeframe: Timeframe | None

    def __init__(
        self, message: str, *, ticker: str | None = None, timeframe: Timeframe | None = None
    ) -> None: ...


class InvalidTickerReason(StrEnum):
    MALFORMED = "malformed"
    NOT_FOUND = "not_found"
    UNSUPPORTED_ASSET_TYPE = "unsupported_asset_type"
    UNSUPPORTED_EXCHANGE = "unsupported_exchange"
    UNSUPPORTED_CURRENCY = "unsupported_currency"


class InvalidTickerError(MarketDataError):
    reason: InvalidTickerReason

    def __init__(
        self, reason: InvalidTickerReason, message: str, *, ticker: str | None = None
    ) -> None: ...


class NoDataError(MarketDataError): ...


class ProviderDataError(MarketDataError):
    kind: str  # a NormalizationErrorKind value, or a provider code chosen by #10; [a-z_]{1,32}

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        ticker: str | None = None,
        timeframe: Timeframe | None = None,
    ) -> None: ...


class UnpublishedReason(StrEnum):
    MISSING = "missing"  # the provider returned no row for the candle
    INVALID = "invalid"  # the row was dropped by normalization


class CandleNotPublishedError(MarketDataError):
    retryable: ClassVar[bool] = True
    reason: UnpublishedReason
    expected_label: datetime  # stdlib UTC: the label of the last slot closed at now
    last_label: datetime | None  # stdlib UTC: the last closed candle available, if any

    def __init__(
        self,
        reason: UnpublishedReason,
        *,
        ticker: str,
        timeframe: Timeframe,
        expected_label: datetime,
        last_label: datetime | None,
    ) -> None: ...


class ProviderFailure(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"


class ProviderUnavailableError(MarketDataError):
    retryable: ClassVar[bool] = True
    failure: ProviderFailure
    retry_after: timedelta | None  # >= 0 when set

    def __init__(
        self,
        failure: ProviderFailure,
        *,
        ticker: str | None = None,
        timeframe: Timeframe | None = None,
        retry_after: timedelta | None = None,
    ) -> None: ...
```

| Class | `retryable` | Raised when | Message contains |
|-------|-------------|-------------|------------------|
| `InvalidTickerError` | no | the ticker text is malformed, unknown to the provider, or rejected by D27 (§6.2) | the reason and, when known, the normalized ticker. For `malformed`, the bounded `repr()` echo of `normalize_ticker`'s message |
| `NoDataError` | no | no candle closed by `now` survives normalization, and no dropped row has the expected label (§8.3) | ticker, timeframe code, ISO `now` |
| `ProviderDataError` | no | the provider's response cannot be normalized (`CandleNormalizationError`), or #10 finds unusable metadata | ticker, timeframe code, `kind` |
| `CandleNotPublishedError` | yes | the last slot closed at `now` has no valid row (D43) | ticker, timeframe code, reason, ISO `expected_label`, ISO `last_label` or `none` |
| `ProviderUnavailableError` | yes | transport failure, timeout, rate limit, server error, or a malformed response #10 treats as transient | `failure` value, ticker and timeframe code when known, `retry_after` seconds when set |

- Constructors validate:
  - `ticker` through `normalize_ticker` (`ValueError` for bad values);
  - labels through `to_utc`;
  - `retry_after >= timedelta(0)`;
  - `kind` against `[a-z_]{1,32}`;
  - the message rule of AC10.

  These are programming errors in the code building the error.
- **Chain hygiene (D38):** errors raised while handling a third-party exception use `raise … from None`. `prepare_candles` does this for `CandleNormalizationError`, and #10 must do it for every library and network exception. `str()` of a `MarketDataError` never includes URLs, query strings, headers, cookies, response bodies or third-party exception text.
- The module imports only the standard library and `trading_bot.domain.timeframe`, `trading_bot.domain.signals` and `trading_bot.domain.utc`: no pandas.

### 6. `data/tickers.py`

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "SUPPORTED_ASSET_TYPES",
    "SUPPORTED_CURRENCY",
    "AssetType",
    "Exchange",
    "TickerInfo",
    "ensure_supported",
    "parse_ticker",
]


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    MUTUAL_FUND = "mutual_fund"
    CRYPTOCURRENCY = "cryptocurrency"
    CURRENCY = "currency"
    FUTURE = "future"
    OPTION = "option"
    OTHER = "other"


class Exchange(StrEnum):
    """Listing venue as an ISO 10383 market identifier code; OTHER for any venue v1 rejects."""

    NYSE = "XNYS"
    NASDAQ = "XNAS"
    NYSE_ARCA = "ARCX"
    NYSE_AMERICAN = "XASE"
    CBOE_BZX = "BATS"
    OTHER = "OTHER"  # OTC markets, non-US exchanges and unknown provider codes


SUPPORTED_ASSET_TYPES: Final = frozenset({AssetType.EQUITY, AssetType.ETF})
SUPPORTED_CURRENCY: Final = "USD"


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerInfo:
    symbol: str
    name: str
    asset_type: AssetType
    exchange: Exchange
    currency: str


def parse_ticker(text: str) -> str: ...
def ensure_supported(info: TickerInfo) -> TickerInfo: ...
```

#### 6.1 `TickerInfo` validation

| Field | Rule | Error |
|-------|------|-------|
| `symbol` | through `normalize_ticker` and stored normalized (`" aapl "` → `"AAPL"`) | `TypeError` / `ValueError` |
| `name` | a `str`; stored stripped; 1–120 characters, all printable (`str.isprintable()`, so no line breaks, tabs, control, zero-width or bidirectional-override characters); non-ASCII letters allowed (`"Nestlé"`) | `TypeError` / `ValueError` |
| `asset_type` | an `AssetType` member (`"equity"` raises) | `TypeError` |
| `exchange` | an `Exchange` member (`"XNAS"` raises) | `TypeError` |
| `currency` | a `str` of 1–8 ASCII letters, case preserved (`"GBp"` stays distinct from `"GBP"`) | `TypeError` / `ValueError` |

Error messages never echo `name` (provider text); they state the rule and the length.

#### 6.2 `parse_ticker` and `ensure_supported`

- `parse_ticker(text)` returns `normalize_ticker(text)`. Its `ValueError` becomes `InvalidTickerError(MALFORMED)` raised `from None`, and a non-`str` raises `TypeError`.
- `ensure_supported(info)` returns `info` itself, or raises `InvalidTickerError(ticker=info.symbol)`. The first failing check wins:

| # | Check | Reason | Example (`TickerInfo` fields that matter) |
|---|-------|--------|--------------------------------------------|
| 1 | `asset_type in SUPPORTED_ASSET_TYPES` | `unsupported_asset_type` | `^GSPC` `index`; `VFIAX` `mutual_fund`; `BTC-USD` `cryptocurrency`; `EURUSD=X` `currency`; `ES=F` `future` |
| 2 | `exchange is not Exchange.OTHER` | `unsupported_exchange` | an `equity` quoted in `USD` on `OTHER` (OTC); `RELIANCE.NS` `equity` on `OTHER` in `INR` (the exchange check wins) |
| 3 | `currency == SUPPORTED_CURRENCY` | `unsupported_currency` | an `etf` on `NYSE_ARCA` in `EUR` |
| — | all pass | returned | `AAPL` `equity` `NASDAQ` `USD`; `SPY` `etf` `NYSE_ARCA` `USD`; `BRK-B` `equity` `NYSE` `USD` |

### 7. `data/provider.py`

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

import pandas as pd

from trading_bot.data.tickers import TickerInfo
from trading_bot.domain.timeframe import Timeframe

__all__ = ["MAX_LOOKBACK", "CandleRequest", "MarketDataProvider"]

MAX_LOOKBACK: Final = 5_000


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleRequest:
    ticker: str
    timeframe: Timeframe
    lookback: int
    now: datetime


class MarketDataProvider(Protocol):
    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame: ...

    async def validate_ticker(self, ticker: str) -> TickerInfo: ...
```

#### 7.1 `CandleRequest`

Validated in `__post_init__` in this order, with normalized values stored:

1. `ticker` through `parse_ticker`: `TypeError`, or `InvalidTickerError(MALFORMED)`.
2. `timeframe` must be a `Timeframe` member: `TypeError`.
3. `lookback` must be an `int` but not a `bool` (`TypeError`), in `[1, MAX_LOOKBACK]` (`ValueError`).
4. `now` through `to_utc`, stored as a stdlib UTC `datetime`: `TypeError`/`ValueError`.

Frozen, equal by value, hashable.

#### 7.2 The provider contract (docstrings of the Protocol)

`fetch_candles(ticker, timeframe, lookback, *, now)`:

- validates its arguments with `CandleRequest` before any I/O;
- returns a frame that passes `assert_closed_candles(frame, request, calendar=…)` (§10.3). The frame passes `validate_candles` with a `datetime64[us, UTC]` index; every label is a canonical slot label (spec 009 D21) whose slot closed at or before `now`; the last label is the last slot closed at `now`; the frame has between 1 and `lookback` rows (the last `lookback` closed candles, fewer only when the history is shorter or a provider limit caps it, which the provider logs);
- raises only:
  - `TypeError`/`ValueError` for invalid arguments;
  - `InvalidTickerError` (`malformed`, `not_found`);
  - `NoDataError`, `CandleNotPublishedError`, `ProviderDataError`, `ProviderUnavailableError`;
  - `CalendarRangeError` when the injected calendar does not cover the window (a configuration error, propagated unchanged).

  Any other exception is a provider bug.
- must build its result with `prepare_candles` (§8), so every provider shares normalization, open-candle removal, the last-candle rule and logging.

`validate_ticker(ticker)`:

- parses with `parse_ticker`, and returns `ensure_supported(info)` for an instrument the provider knows;
- raises only `TypeError`, `InvalidTickerError` (any reason), `ProviderDataError` and `ProviderUnavailableError`.

Both methods are safe to call concurrently from one event loop, and never read the wall clock (D36).

### 8. `data/pipeline.py`

```python
import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from trading_bot.data.provider import CandleRequest
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar

__all__ = ["CandleWindow", "candle_window", "prepare_candles"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleWindow:
    first: CandleSlot  # the oldest of the `lookback` closed slots
    last: CandleSlot  # the last slot closed at `now`

    @property
    def start(self) -> datetime: ...  # first.open_time

    @property
    def end(self) -> datetime: ...  # last.close_time


def candle_window(request: CandleRequest, *, calendar: MarketCalendar) -> CandleWindow: ...


def prepare_candles(
    raw: pd.DataFrame,
    request: CandleRequest,
    *,
    calendar: MarketCalendar,
    logger: logging.Logger | None = None,  # default: logging.getLogger("trading_bot.data.pipeline")
) -> pd.DataFrame: ...
```

Both functions raise `TypeError` for a non-`CandleRequest` or a non-`MarketCalendar`.

#### 8.1 `candle_window` (AC14)

The window is the time span a provider must cover to return `lookback` candles; for `1d`, providers that request by date use `first.session_day` and `last.session_day`. Rows verified on the prototype (NYSE calendar):

| Request (`timeframe`, `now`, `lookback`) | `first.label` | `first.session_day` | `start` | `last.label` | `end` |
|------------------------------------------|---------------|---------------------|---------|--------------|-------|
| `1h`, `2024-07-05T14:30:00Z`, 5 | `2024-07-03T13:30Z` | 2024-07-03 | `2024-07-03T13:30Z` | `2024-07-05T13:30Z` | `2024-07-05T14:30Z` |
| `1d`, `2024-07-08T12:00:00Z`, 3 | `2024-07-02T04:00Z` | 2024-07-02 | `2024-07-02T13:30Z` | `2024-07-05T04:00Z` | `2024-07-05T20:00Z` |
| `4h`, `2024-03-11T20:00:00Z`, 4 | `2024-03-08T14:30Z` | 2024-03-08 | `2024-03-08T14:30Z` | `2024-03-11T17:30Z` | `2024-03-11T20:00Z` |

#### 8.2 `prepare_candles` steps (AC15)

1. `normalized = normalize_candles(raw, request.timeframe, calendar=calendar)`. A `CandleNormalizationError` becomes `ProviderDataError(error.kind.value, …, ticker, timeframe)` raised `from None`. `TypeError` propagates.
2. `last = calendar.closed_candles(request.timeframe, request.now, 1)[-1]`. `CalendarRangeError` propagates.
3. Log `normalized.dropped` (§8.4).
4. `closed = drop_open_candle(normalized.candles, request.timeframe, request.now, calendar=calendar)`.
5. If `closed` is non-empty and `closed.index[-1] == last.label`, go to step 6. Otherwise:
   - a dropped row has `label == last.label` → `CandleNotPublishedError(INVALID)`;
   - else `closed` is empty → `NoDataError`;
   - else → `CandleNotPublishedError(MISSING)`.

   In both `CandleNotPublishedError` cases, `expected_label = last.label` and `last_label = closed.index[-1]` (or `None` when `closed` is empty).
6. Return `closed.iloc[-request.lookback:]`.

`raw` is never modified, and two calls with equal arguments return equal frames.

#### 8.3 Outcome table (AC15)

NYSE calendar, `1d`, `ticker="AAPL"`, `lookback=2`, `now = 2024-07-05T20:00:30Z` (the last closed slot is labelled `2024-07-05T04:00Z`). "Grid" is `session_candles` for labels in `[2024-06-03T00:00:00Z, 2024-07-10T00:00:00Z)`, which includes the 2024-07-08 and 2024-07-09 rows after `now`. Verified on the prototype:

| Raw frame | Result |
|-----------|--------|
| grid | labels `2024-07-03T04:00Z`, `2024-07-05T04:00Z` |
| grid without the 2024-07-05 row | `CandleNotPublishedError`: `missing`, expected `2024-07-05T04:00Z`, last `2024-07-03T04:00Z` |
| grid with `close` NaN on 2024-07-05 | `CandleNotPublishedError`: `invalid`, expected `2024-07-05T04:00Z`, last `2024-07-03T04:00Z` |
| grid with a second 2024-07-05 row whose `close` differs | `CandleNotPublishedError`: `invalid`, last `2024-07-03T04:00Z` |
| grid with an identical second 2024-07-05 row | labels `2024-07-03T04:00Z`, `2024-07-05T04:00Z` |
| only the 2024-07-08 and 2024-07-09 rows | `NoDataError` |
| zero rows, valid structure | `NoDataError` |
| only the 2024-07-05 row, with `close` NaN | `CandleNotPublishedError`: `invalid`, last `None` |
| grid with a `RangeIndex` | `ProviderDataError`, `kind == "index_type"`, `__cause__ is None` |
| grid, `lookback=5 000` | all 23 grid rows up to 2024-07-05 (fewer than `lookback` is not an error) |

#### 8.4 Logging (AC15)

- One record per reason, in `DropReason` declaration order, through `logger` (default `trading_bot.data.pipeline`).
- For each reason, the rows labelled at or before `last.label`, or `NaT`, give a `WARNING` record, and later rows give a `DEBUG` record, in that order. `duplicate` rows always give a single `DEBUG` record.
- Message, with `%`-style arguments: `"dropped %d %s candle rows for %s %s (first %s, last %s)"`, with the count, the reason value, the normalized ticker, the timeframe code, and the smallest and largest non-`NaT` labels of the group in ISO 8601 (`"none"` when every label is `NaT`).
- Records contain nothing else: no frame values, no raw column labels, no provider text.

On the §3.4 frame with `request = CandleRequest(ticker="AAPL", timeframe=H1, lookback=3, now=2024-07-03T17:00:30Z)`, the result has labels `2024-07-02T17:30Z`, `2024-07-03T15:30Z` and `2024-07-03T16:30Z`, and the records are, in order:

| Level | Message |
|-------|---------|
| WARNING | `dropped 1 missing_timestamp candle rows for AAPL 1h (first none, last none)` |
| WARNING | `dropped 1 outside_calendar candle rows for AAPL 1h (first 2020-12-31T15:30:00+00:00, last 2020-12-31T15:30:00+00:00)` |
| DEBUG | `dropped 1 not_a_session candle rows for AAPL 1h (first 2024-07-04T14:30:00+00:00, last 2024-07-04T14:30:00+00:00)` |
| WARNING | `dropped 2 outside_session candle rows for AAPL 1h (first 2024-07-02T20:00:00+00:00, last 2024-07-03T12:00:00+00:00)` |
| WARNING | `dropped 2 off_grid candle rows for AAPL 1h (first 2024-07-02T14:00:00+00:00, last 2024-07-02T15:00:00+00:00)` |
| WARNING | `dropped 2 missing_value candle rows for AAPL 1h (first 2024-07-02T13:30:00+00:00, last 2024-07-02T14:30:00+00:00)` |
| WARNING | `dropped 1 infinite_value candle rows for AAPL 1h (first 2024-07-03T14:30:00+00:00, last 2024-07-03T14:30:00+00:00)` |
| WARNING | `dropped 1 non_positive_price candle rows for AAPL 1h (first 2024-07-03T13:30:00+00:00, last 2024-07-03T13:30:00+00:00)` |
| WARNING | `dropped 1 negative_volume candle rows for AAPL 1h (first 2024-07-02T19:30:00+00:00, last 2024-07-02T19:30:00+00:00)` |
| WARNING | `dropped 1 inconsistent_range candle rows for AAPL 1h (first 2024-07-02T16:30:00+00:00, last 2024-07-02T16:30:00+00:00)` |
| DEBUG | `dropped 1 duplicate candle rows for AAPL 1h (first 2024-07-02T17:30:00+00:00, last 2024-07-02T17:30:00+00:00)` |
| WARNING | `dropped 2 conflicting_duplicate candle rows for AAPL 1h (first 2024-07-02T18:30:00+00:00, last 2024-07-02T18:30:00+00:00)` |

The pipeline emits no other record: there is no summary record (settled in implementation, see Implementation notes).

### 9. Unpublished candles: the D33 split (D44)

| Owner | Responsibility |
|-------|----------------|
| #8 (this spec) | Detect: `prepare_candles` raises `CandleNotPublishedError` (retryable, with `expected_label`) instead of returning a frame that ends before the last closed slot; the Protocol forbids stale frames; the fake provider reproduces it from a stored frame without the last row |
| #10 | Never retries `CandleNotPublishedError` internally; its transport retries (backoff, jitter, timeout) apply to `ProviderUnavailableError` only |
| #15 (with #14) | The bounded window. The engine run for a timeframe retries the tickers that raised `CandleNotPublishedError`, with backoff, **reusing the same scheduled `now`** (so the expected slot does not move), until a deadline **no later than `calendar.next_candle_close(timeframe, now)`** (so attempts never overlap the next run, and `max_instances=1` holds). Then it skips those tickers and logs. The window length and backoff are #15 configuration (for example a `TB_*` seconds variable). #26 may alert on repeated skips |

Limitation: a candle can be published but not final right after its close, when Yahoo has not consolidated its last trades. The data cannot show this; `TB_CANDLE_CLOSE_DELAY_SECONDS` (#15) is the mitigation.

### 10. Test fixtures

#### 10.1 `tests/fixtures/session_candles.py`

```python
def session_candles(
    calendar: MarketCalendar, timeframe: Timeframe, start: datetime, end: datetime, *, seed: int = 0
) -> pd.DataFrame:
    """Seeded RANDOM_WALK candles on the calendar grid, one per slot with start <= label < end."""


def provider_shaped(
    candles: pd.DataFrame,
    *,
    timezone: tzinfo = NEW_YORK,
    unit: Literal["s", "ms", "us", "ns"] = "s",
    index_name: str = "Datetime",
) -> pd.DataFrame:
    """The candles as a yfinance-like source returns them (synthetic values only, decision D29)."""
```

- `session_candles` takes the values of `synthetic_candles(len(slots), seed=seed, timeframe=timeframe)` and replaces the index with the slot labels as `datetime64[us, UTC]`, so the result is a canonical frame. No slots gives an empty canonical frame. Timestamp features of the `GAPS` scenario do not apply, which is why there is no `scenario` parameter.
- `provider_shaped` returns:
  - **index:** converted to `timezone`, with `unit`, named `index_name`;
  - **columns:** `Open, High, Low, Close, Adj Close (= Close), Volume (np.rint → int64), Dividends (0.0), Stock Splits (0.0)`.

  Round trip: `normalize_candles(provider_shaped(c), tf, calendar=cal).candles` equals `c.assign(volume=np.rint(c["volume"]))` with `dropped == ()`.

#### 10.2 `tests/fixtures/fake_provider.py`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class FetchCall:
    ticker: str  # normalized
    timeframe: Timeframe
    lookback: int
    now: datetime  # stdlib UTC


class FakeMarketDataProvider:
    """In-memory MarketDataProvider for engine and scheduler tests: no network, no clock."""

    def __init__(self, *, calendar: MarketCalendar) -> None: ...

    @property
    def fetch_calls(self) -> tuple[FetchCall, ...]: ...

    @property
    def validate_calls(self) -> tuple[str, ...]: ...  # the texts passed, as given

    def add_ticker(self, info: TickerInfo) -> None: ...  # replaces an entry with the same symbol

    def set_candles(self, ticker: str, timeframe: Timeframe, raw: pd.DataFrame) -> None: ...

    def fail_next(
        self,
        error: MarketDataError,
        *,
        ticker: str,
        method: Literal["fetch_candles", "validate_ticker"] = "fetch_candles",
        times: int = 1,
    ) -> None: ...

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame: ...

    async def validate_ticker(self, ticker: str) -> TickerInfo: ...


def as_market_data_provider(provider: FakeMarketDataProvider) -> MarketDataProvider:
    """Returns its argument; strict mypy checks that the fake conforms to the Protocol (AC13)."""
```

- `set_candles` stores a copy of `raw` under `(normalize_ticker(ticker), timeframe)`. `raw` is the frame "as published" in any shape `normalize_candles` accepts; replacing it simulates a newly published candle.
- `fail_next` appends `error` `times` times (`times >= 1`) to a first-in, first-out queue per `(normalized ticker, method)`.
- **`fetch_candles`:**
  1. `await asyncio.sleep(0)`;
  2. `request = CandleRequest(…)`;
  3. record a `FetchCall`;
  4. if the queue for `(request.ticker, "fetch_candles")` is non-empty, pop its first error and raise it;
  5. with no stored frame, raise `NoDataError`;
  6. return `prepare_candles(stored, request, calendar=calendar)`.
- **`validate_ticker`:**
  1. `await asyncio.sleep(0)`;
  2. `TypeError` for a non-`str`;
  3. record the text;
  4. `symbol = parse_ticker(text)`;
  5. if the queue for `(symbol, "validate_ticker")` is non-empty, pop and raise its first error;
  6. an unknown symbol raises `InvalidTickerError(NOT_FOUND, ticker=symbol)`;
  7. return `ensure_supported(info)`.
- Mutable state lives only on the instance. The fake never reads the clock and never touches the network.

#### 10.3 `tests/fixtures/provider_contract.py`

```python
def assert_closed_candles(
    frame: pd.DataFrame, request: CandleRequest, *, calendar: MarketCalendar
) -> None:
    """Raise AssertionError unless ``frame`` meets the fetch_candles return contract (§7.2)."""
```

Checks, each raising `AssertionError` with a message:

- `validate_candles` passes;
- the index dtype is `datetime64[us, UTC]`;
- the frame is not empty;
- `len(frame) <= request.lookback`;
- every label has a slot (`calendar.candle_slot` succeeds) whose `close_time <= request.now`;
- the last label equals `calendar.closed_candles(request.timeframe, request.now, 1)[-1].label`.

Self-tests build one violating frame per check. #10 runs this helper on every recorded-fixture result, and #14 on the fake's output.

### 11. Typing and purity guard

- **mypy:** no configuration change. `tests/fixtures/*.py` are already type-checked, and `src/trading_bot/data` is under `src`.
- **Extra-flag command** (spec 009 §8, extended with `src/trading_bot/data`):

  ```bash
  uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated \
    src/trading_bot/domain/*.py src/trading_bot/domain/indicators \
    src/trading_bot/domain/market_calendar src/trading_bot/data
  ```

  The tech-lead ran strict mypy with these three flags on a prototype of §3–§8 (the Protocol, `ClassVar` flags, dataclasses, logging, `pd.Timestamp | None` fields): green. The same prototype showed that mypy rejects a sync `fetch_candles` and one without `now` against the Protocol. pandas-stubs idioms that passed: `DatetimeIndex.nanosecond` to detect sub-microsecond labels, and `DataFrame.sort_index(kind="stable")`. `DatetimeIndex.asi8` and `np.argsort` on its result do not type-check.
- **Purity guard** (`tests/unit/test_domain_purity.py`): add `candle_normalization.py` and `closed_candles.py` to the expected scanned set. Neither gets a per-file allowance, and neither joins the lightweight fresh-interpreter list, because both import pandas.

### 12. Forward compatibility

| Issue | How it uses #8 |
|-------|----------------|
| #10 | Implements `MarketDataProvider`. It maps yfinance columns and metadata to the shapes of §3.1 and §6, and exchange codes to `Exchange` (unknown and OTC → `OTHER`). It plans requests with `candle_window` (clamped to Yahoo's history limits, logging when capped), builds `4h` bars from normalized `1h` bars, and finishes every fetch with `prepare_candles`. It maps library and network exceptions to `ProviderUnavailableError`/`ProviderDataError` `from None`, and runs `assert_closed_candles` on recorded-fixture results. See the hand-off list in Risks. |
| #12 | Stores `TickerInfo.symbol` (already normalized) and may store `name`, `asset_type`, `exchange` and `currency`; `/add` and the CLI call `validate_ticker` first. |
| #14 | Types the provider as `MarketDataProvider`; passes `lookback = max(rule.stable_warmup())` over the ticker's rules (at most 2 255 today, below `MAX_LOOKBACK`); isolates each ticker on `MarketDataError`; evaluates the returned frame as is (its last row is the last closed candle); keeps `candle_close_ts` nominal (spec 009 D22). Tests use `FakeMarketDataProvider`, `session_candles` and `fail_next`. |
| #15 | §9: the bounded retry window for `CandleNotPublishedError` with the same `now` and a deadline at the next close. |
| #16 | Injects the calendar built once (spec 009 D18) into the provider. |
| #17, #19, #22, #23 | Map `InvalidTickerError.reason` to user text; never show `str()` of a `ProviderUnavailableError` beyond its `failure`; escape `TickerInfo.name` for HTML/Markdown. |
| #25 | The chart endpoint fetches through the same Protocol with `now` read at the API edge. |
| #26, #27 | Classify errors by class and `retryable`; count `CandleNotPublishedError` skips and dropped-row warnings per ticker. |
| F8 | Backtests fetch with a past `now` and get only candles closed at that instant (§4.1). |

### 13. `docs/ARCHITECTURE.md`

- **Layers table:**
  - `domain/` row: add "candle normalization and open-candle removal (`normalize_candles`, `drop_open_candle`)".
  - `data/` row: "`MarketDataProvider` (async Protocol), `TickerInfo` and the D27 policy, typed errors, and `prepare_candles`, which every provider uses to normalize, drop bad rows and the open candle, and require the last closed candle; `YFinanceProvider` (#10) with retries and backoff". Keep the Yahoo limits text.
- **`## Domain models` → `### Candle frames`:** replace "the data provider (#8) converts zones, renames columns, casts volume to `float64` and decides how to repair or drop bad rows, then validates" with a sentence pointing to `normalize_candles` and the new section.
- **`### Nominal candle close` obligations:** mark "Label convention (#8, #10)" as fixed: canonical labels (spec 009 D21) are enforced by `normalize_candles` (spec 010), and changing them later requires migrating stored keys.
- **New `## Market data` section** right after `## Market calendar`, short, with:
  1. the Protocol and its contract (§7.2), with `now` explicit and `lookback` as a candle count (`MAX_LOOKBACK`, `candle_window`);
  2. the §2 flow diagram;
  3. normalization: canonical frame, structural errors, and the drop reasons in precedence order (§3.1–§3.3);
  4. `drop_open_candle`: half-open closes, every later row dropped, the last row checked, with three rows of §4.2 (exact close, holiday, DST);
  5. the last-candle rule and the logging levels (D43);
  6. the error table of §5 with `retryable`, and the chain-hygiene rule;
  7. the D33 split (§9);
  8. the fake provider and helpers for tests (§10), with a short `asyncio.run` example;
  9. a link to this spec.
- **`### Warmup and reproducibility`:** say that #14 passes `max(rule.stable_warmup())` as `lookback` (at most 2 255 today).
- **`## Decisions`:** a bullet "Async provider port with an explicit `now`" with the reasons of D36.
- **`src/trading_bot/domain/__init__.py` docstring:** the `candle_normalization` and `closed_candles` modules turn provider frames into closed canonical candles (spec 010).

### 14. Repository and image packaging (D66, AC23)

Found during implementation. The `.gitignore` rule `data/` (under "Local runtime data") has no leading slash, so git ignores a directory named `data` at any depth, including `src/trading_bot/data/`. This was measured on the implementation and on scratch copies:

- `git check-ignore -v src/trading_bot/data/pipeline.py` reports `.gitignore:241:data/`: the package cannot be staged, and `git add -f` is blocked by `git_guard.py`;
- ruff honors `.gitignore`, so `uv run ruff check --show-files .` lists none of the package files, and the gate's `ruff check .` and `ruff format --check .` steps skip it (mypy and pytest do not read `.gitignore` and already cover it);
- hatchling honors `.gitignore` too: a wheel built next to the repository's `.gitignore` contains no `trading_bot/data/` file; with the change below, or with no `.gitignore` at all (the Docker builder layout), it contains all five.

**`.gitignore` (developer)**, exactly one added line:

```gitignore
# Local runtime data (SQLite databases, backups, charts)
data/
!src/trading_bot/data/
```

- Git can re-include the package because none of its parent directories is excluded. `data/`, `deploy/data/` and `tests/fixtures/data/` stay ignored (AC23).
- Anchoring the original rule (`/data/`) was rejected: it would stop ignoring nested runtime data directories that the rule protects today.
- With the change, `scripts/check.py` lints and format-checks the package through its existing `ruff check .` and `ruff format --check .` steps; no script change is needed.

**`.dockerignore`: no change.** Docker matches `.dockerignore` patterns from the build context root only (`data/` excludes `./data`, not `src/trading_bot/data`). The builder stage copies only `pyproject.toml`, `uv.lock` and `src`, so no `.gitignore` reaches hatchling inside the image build.

**`Dockerfile` smoke check (developer)**, in the runtime stage right after the NYSE calendar check and before `USER app`:

```dockerfile
# Fail the build if the market data package is missing from the installed wheel (nothing imports it at startup yet).
RUN ["python", "-c", "import trading_bot.data.errors, trading_bot.data.pipeline, trading_bot.data.provider, trading_bot.data.tickers"]
```

- **Why it is warranted.** The image contains the package today only because the builder does not copy `.gitignore`. A later build change (copying the whole context, or building the wheel outside Docker) would silently drop it again. Nothing imports the package at startup until #16 wires the provider, so neither the arm64 build nor `/health` would notice. This is the same reasoning as spec 009 D25.
- **What it loads.** Importing `pipeline` also loads the two new domain modules and the calendar model.
- **Verified locally.** The command exits 0 against the implementation in the development environment. The authoritative run is the PR's `Docker build (arm64)` job.
- #16 may remove the check once the lifespan imports the package at startup.

## Test plan

All tests are unit tests without network and without the wall clock: every instant is a literal, and calendars are `nyse_test_calendar()` (built once per process) or `toy_calendar()`. Coroutines run with `asyncio.run` (D46). The mandatory template cases are:

- **Anti look-ahead (rule 4):**
  - `drop_open_candle` through `assert_no_lookahead` on every timeframe and three `now` values, with full sweeps and two controls that must fail with `result_appeared` (T7, AC9);
  - `normalize_candles` through the same harness on a raw frame with bad rows (T7, AC5);
  - a Hypothesis property that the result is monotonic in `now` and a prefix of the input (T15).

  The tech-lead ran all of these on the prototype: green, and both controls caught.
- **Idempotency (rule 5):** signal keys come from labels (`nominal_close`), so T3 pins that the same candles in different zones, units and row orders normalize to identical frames and identical `Timeframe.nominal_close(label)` for the last row, that normalization is idempotent, and that `prepare_candles` is deterministic. Deduplication itself is #13/#14.
- **Authorization:** N/A (no Telegram, API or dashboard code).
- **Secret redaction:**
  - T8 checks that error messages are single-line, bounded and built from safe fields, and that `ProviderDataError` is raised `from None`;
  - T11 checks that log records contain only the §8.4 fields;
  - T16 checks that a `TickerInfo.name` with a line break, a zero-width character or a bidirectional override is rejected, and that no message echoes it.

  No test uses a real URL, host, token or crumb. A crumb-like query string, if a test needs one, is built at runtime.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 normalization API | unit | `__all__`; dataclass shapes (`eq=False` on `NormalizedCandles`); argument `TypeError`s; empty input | AC1, AC3 | developer |
| T2 structural errors | unit | Every row of §3.1, in order (a frame with two defects reports the earlier check); `kind`, `column`, `ValueError`; single-line bounded messages; no raw label echoed | AC2 | developer |
| T3 canonical frame | unit | Dtypes and index; case-insensitive column matching, extras ignored; `int64`/`float32`/`Int64`/`Float64` casts; zones × units × shuffled order give identical frames and equal nominal closes; idempotence; input unchanged; `provider_shaped` round trip | AC3 | developer |
| T4 dropped rows | unit | §3.4 literally; the `ns` sub-microsecond row; precedence; identical versus conflicting duplicates; three identical copies; `CandleLabelErrorKind` ⊆ `DropReason` | AC4 | developer |
| T5 `drop_open_candle` contract | unit | Check order and error types; result is `iloc[:k]` with dtypes and unit kept; input unchanged; empty frames; toy-calendar `CalendarRangeError` cases | AC6, AC8 | developer |
| T6 `drop_open_candle` tables | unit | §4.2 literally (every row × timeframe); the kept-row counts; §4.3 literally | AC7, AC8 | developer |
| T7 anti look-ahead | unit | AC5 and AC9 harness calls with `max_cuts=len(frame)`; reports non-vacuous; the two controls raise `LookaheadError` `result_appeared` | AC5, AC9 | developer |
| T8 errors | unit | Hierarchy (`MarketDataError`, not `ValueError`); `retryable` per class; attributes and normalization (`ticker`, labels, `retry_after`, `kind` pattern); message elements; line-break and 301-character messages rejected; `from None` chains | AC10 | developer |
| T9 tickers | unit | §6.1 validation table; `parse_ticker`; §6.2 table literally, including the order case `RELIANCE.NS`; frozen, equal and hashable `TickerInfo` | AC11 | developer |
| T10 requests and windows | unit | `CandleRequest` order and errors (`bool` lookback, 0, 5 001, naive `now`, `"1h"`); `MAX_LOOKBACK` against the catalog maximum; §8.1 literally | AC12, AC14 | developer |
| T11 pipeline | unit | §8.2 order (a structural error wins over an out-of-coverage `now`); §8.3 literally; §8.4 records literally with `caplog` at `DEBUG`; no other record; injected `logger`; determinism | AC15 | developer |
| T12 fake provider | unit | Recorded calls; first-in, first-out `fail_next` with `times`; `NoDataError` without a frame; output equals `prepare_candles` and passes `assert_closed_candles`; `set_candles` replacement publishes a new candle; `validate_ticker` for `malformed`, `not_found`, unsupported and supported instruments; argument errors before recording | AC13, AC16 | developer |
| T13 shared fixtures | unit | `session_candles` grid alignment and validity (NYSE and toy calendars, all timeframes, empty range); `provider_shaped` shape and dtypes; `assert_closed_candles` raises for each violation of §10.3 and passes on a correct frame | AC17 | developer |
| T14 purity guard | unit | Both new modules in the expected set without allowances | AC18 | developer |
| T15 properties | unit (`@given`) | Over NYSE grid frames and `now` drawn in 2024 at microsecond resolution, for every timeframe: `drop_open_candle` returns a prefix; every kept row closes at or before `now`, and the first dropped row (if any) closes after `now`; monotonic in `now`; appending rows never changes the result. Over raw frames drawn from grid labels plus rows with random defects: normalization output passes `validate_candles`, is idempotent and independent of row order; each input row is either kept or reported exactly once; `prepare_candles` either returns a frame passing `assert_closed_candles` or raises one of `NoDataError`, `CandleNotPublishedError` or `ProviderDataError` | AC3–AC9, AC15 | tester |
| T16 adversarial | unit | Labels in both DST folds and in the spring gap given in ET; one microsecond around every boundary of the 2024-07-03 half day; a 100 000-row raw frame with one defect in the last row (reported once, no warning escapes); `volume` at `2**53 + 1`; subnormal prices; column labels with 10 000 characters, newlines or non-`str` objects (never echoed); `TickerInfo.name` abuse (control, zero-width and bidirectional-override characters, 121 characters, whitespace only); `lookback=True`; `now` as `pd.Timestamp` in another zone equal to the stdlib value; the fake with 1 000 scripted failures; warnings turned into errors during normalization | AC2–AC4, AC8, AC10–AC12, AC16 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC21 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot --cov-branch --cov-report=term-missing`: 100% for the six new `src/` modules, no regression elsewhere | AC21 |
| V3 budget | `uv run pytest --durations=40 -q`: new tests add at most 10 s, none above 2 s. Reported, never asserted | AC21 |
| V4 typing | `uv run mypy`; the §11 extra-flag command; the spec 004 `git grep` for `Any` over the new `src/` files and `tests/fixtures/{session_candles,fake_provider,provider_contract}.py` empty; `git grep -n "type: ignore"` on them empty | AC19 |
| V5 Protocol negative check | In a scratch file **outside the repository**, strict mypy rejects a class with a sync `fetch_candles` and one without `now` when passed as `MarketDataProvider` | AC13 |
| V6 dependencies | `git diff <base> -- pyproject.toml uv.lock` empty, where `<base>` is the `feature/market-calendar` commit this branch starts from | AC22 |
| V7 clock and network | The AC18 `git grep` over `src/trading_bot/data` is empty; `git grep -n -E "^\s*(import|from) (requests|httpx|urllib|socket|aiohttp|yfinance|curl_cffi)" src/trading_bot/data` is empty | AC18 |
| V8 docs | Section placement and content against §13; `uv run ruff format --check` on the Python blocks | AC20 |
| V9 scope | `git diff <base> --name-only` plus `git status --porcelain` list only the §1 files. After #9 merges and the branch is rebased, compare with `origin/main` | AC22 |
| V10 secrets and language | `python scripts/secret_scan.py --history`; English-only review of the diff, including test data (ticker names and messages) | — |
| V11 Docker | No dependency change and no startup import, but the `Dockerfile` gains the §14 smoke check, so this change **does** alter the image build. The local build is **BLOCKED** (no daemon); the local substitute is running the §14 import command with `uv run python -c …`. The authoritative checks are the PR's `Docker build (arm64)` job, which runs the smoke check, and the beta deploy with `/health`, verified by the lead. Report it as BLOCKED with this justification, not as PASS | AC23 |
| V12 ignore rules and lint coverage | `git check-ignore -v src/trading_bot/data/pipeline.py` exits 1; `git check-ignore data/x deploy/data/x tests/fixtures/data/x` prints the three paths; `uv run ruff check --show-files .` lists the five `src/trading_bot/data/*.py` files; `git diff <base> -- .gitignore` shows only the added `!src/trading_bot/data/` line; `git diff <base> -- Dockerfile` shows only the §14 comment and `RUN` line | AC23 |

**Testing rules for this feature** (spec 007 §14, spec 009):

- no wall-clock or elapsed-time assertions;
- no platform-dependent expectations (Windows locally, Linux on CI);
- literal expectations are written out, never recomputed with the code under test;
- exact comparisons in look-ahead checks (`rtol = atol = 0`);
- no real market data (D29): every frame is synthetic;
- `caplog` assertions filter on the `trading_bot.data.pipeline` logger.

**TDD order suggested to the developer:**

1. T14 (guard entries);
2. T1 → T2 → T3 → T4 (normalization, with `session_candles` and `provider_shaped` extracted as soon as T3 needs them);
3. T5 → T6 → T7 (`drop_open_candle` and look-ahead);
4. T8 → T9 → T10 (errors, tickers, requests);
5. T11 (pipeline);
6. `provider_contract.py` → T12 → T13 (fake and fixtures);
7. docs.

## Risks and security

- **Non-final candles right after the close.** A candle can be labelled and present while Yahoo is still consolidating its last trades. Nothing in the data shows it, so `CandleNotPublishedError` cannot catch it. Mitigation: `TB_CANDLE_CLOSE_DELAY_SECONDS` in #15; #10 fixtures may record a provisional-then-final pair to size the delay.
- **Inconsistent OHLC on real data.** D32 drops rows with `open` or `close` outside `[low, high]`, without repair. If Yahoo produces such rows often (for example rounding noise on the first `1h` bar), the newest candle would often be dropped, raising `CandleNotPublishedError` and skipping that run. Mitigation: #10 measures the frequency offline on real responses, never committed (D29). If it is material, a spec change proposes a repair with an epsilon to the user, because it would change D32.
- **Illiquid or halted tickers.** A missing `1h` bar for an hour without trades makes every run of that hour raise `CandleNotPublishedError`, wait for #15's window and skip. This costs log noise and time, never a wrong signal. #26 aggregates it.
- **Closures unknown to the calendar library** (spec 009 Risks). The provider publishes no candle for such a day, so each run now raises a logged retryable error instead of silently re-evaluating the previous candle. Idempotency still prevents resends.
- **Label convention.** Canonical labels are fixed here (spec 009 D21, enforced by normalization and by the last-row check in `drop_open_candle`) before #13 persists keys. Changing them later requires migrating stored signal keys (spec 004 §5).
- **Secret leakage through exceptions.** Yahoo request URLs carry a session crumb in the query string, and HTTP libraries put URLs in exception messages. Mitigations: D38 chain hygiene (`from None`), `failure` enums instead of exception text, messages built from safe fields (T8), and the rule handed to #10. The redaction filter in `logging_setup.py` does not recognize crumbs, so it cannot be relied on here.
- **Provider-controlled text.** Column labels are never echoed, dtype names are truncated, and `TickerInfo.name` is printable and bounded but still attacker-influenced display text: #17, #19 and #23 must escape it.
- **Log volume.** At most two records per reason per fetch, so a bounded number per run. Rows of the in-progress candle log at `DEBUG` only.
- **Cost on the Raspberry Pi.** Normalization is vectorized except the label classification, which the design limits to one grid query plus one lookup per rejected label (§3.2). `drop_open_candle` does one calendar query and one binary search. Nothing is asserted on time; budgets are evidence.
- **Ignored package.** The `data/` rule of `.gitignore` hid `src/trading_bot/data/` from git, ruff and hatchling (§14). Mitigations: the one-line negation, the AC23 checks, and the `Dockerfile` smoke import that fails the image build if the package is missing. Any future package or directory named `data` under `src/` or `tests/` needs the same treatment.
- **Protocol churn.** Adding `now` and `async` now, before any implementation or caller exists, costs nothing; changing them after #10 and #14 would touch every provider and the engine.
- **Sensitive data.** None: synthetic frames, invented ticker metadata, no tokens, hosts, IPs, users, `TB_*` variables, migrations or `secrets.env` changes.
- **Unbreakable rules.** All preserved:
  - signal-only: market data only, no order concepts, no broker credentials;
  - pure `domain/`: T14 and the purity guard;
  - closed candles only: this feature implements it, with look-ahead tests T7 and T15;
  - idempotency: canonical labels keep keys stable (T3);
  - UTC: all outputs are UTC, and `now` goes through `to_utc`;
  - single worker: untouched;
  - no `eval`;
  - English only.

### Hand-off list for spec 011 (#10)

1. Map yfinance output to the shapes of §3.1:
   - single-ticker `Ticker.history(auto_adjust=False, prepost=False)` (D28, D30); never the multi-ticker `download` shape (`multiindex_columns`);
   - intraday index in exchange time, daily index at 00:00 ET (spec 009 D21), converted as needed.
2. Map metadata to `TickerInfo`: `quoteType` → `AssetType`, exchange codes → `Exchange` (`NMS`/`NGM`/`NCM` → `NASDAQ`, `NYQ` → `NYSE`, `PCX` → `NYSE_ARCA`, `ASE` → `NYSE_AMERICAN`, `BTS` → `CBOE_BZX`, everything else including OTC → `OTHER`, to be confirmed on recorded structures), currency as given; then `ensure_supported`.
3. Plan requests with `candle_window`. Clamp to Yahoo's limits (`1h` ≈ 730 days) and log once when the history is capped, with ticker, timeframe, requested and available start.
4. **`4h` resampling:** build from `normalize_candles`'d `1h` bars, grouped by the `4h` slot that contains each label, labelled with the slot label. The **last** `4h` slot closed at `now` must include the `1h` bar whose close equals the `4h` close; otherwise the `4h` row must not be emitted, so `prepare_candles` raises `CandleNotPublishedError(MISSING)`. D34 (build from the existing bars and log the gap) applies to earlier slots only. Also decide the aggregated `volume` and the log message for gaps.
5. Transport retries with exponential backoff, jitter and a timeout, only for `ProviderUnavailableError`; honor `retry_after`; internal rate limiting as a loop-local primitive; blocking calls in `asyncio.to_thread`.
6. Map every library and network exception to `ProviderUnavailableError` (with a `ProviderFailure`) or `ProviderDataError` `from None`. Decide whether an empty Yahoo response is transient (`ProviderUnavailableError(INVALID_RESPONSE)`) or `NoDataError`, and whether Yahoo's "symbol may be delisted" message means `InvalidTickerError(NOT_FOUND)`.
7. Recorded fixtures keep Yahoo's structure and timestamps with synthetic values (D29). Run `assert_closed_candles` on every fetch result from them. Measure the inconsistent-OHLC frequency offline (Risks) without committing real data.
8. Never call `normalize_candles` or `drop_open_candle` directly on the final `4h`/`1h`/`1d` result: always use `prepare_candles`, so logging and the last-candle rule are shared.

## Implementation notes

Recorded on 2026-09-17, after `[impl]`. These clarify choices the spec left open and are part of the contract, so tests pin them and later changes need a spec update. Other implementation choices the developer reported are not contract: the Protocol method bodies are docstring-only; the fake provider re-raises scripted errors with a fresh traceback; and the §3.4 golden frame is duplicated in the pipeline tests.

- **Error strings (§5, AC10).** `str(error)` of every `MarketDataError` has the shape `"[<code>: ]<message>[ [name=value, ...]]"`:
  - a `<code>: ` prefix for the classes that carry one (for example the `reason` of `InvalidTickerError` or the `kind` of `ProviderDataError`);
  - the message argument;
  - a bracketed list of the fields that are set: `ticker` and `timeframe` first, then class-specific fields.

  The 300-character bound and the line-break ban apply to the message argument, and a test bounds the full `str()` under 400 characters.
- **Dtype echo (§3.1, AC2).** The dtype name in a `non_numeric_column` message is a `repr()` of at most 32 characters **including the quotes**.
- **`ensure_supported` arguments (§6.2, AC11).** A non-`TickerInfo` argument raises `TypeError`.
- **Fake provider arguments (§10.2, AC16).** Both methods validate their arguments before recording the call or popping a scripted failure: `fetch_candles` through `CandleRequest`, and `validate_ticker` with the `TypeError` check.
- **No summary log record (§8.4, AC15).** `prepare_candles` emits only the §8.4 records; the optional `DEBUG` summary was not implemented and is now excluded.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; M2 stacks #9 → #8 → #10.
- **D2 (2026-09-14), refined by D29:** synthetic data only in tests; no real Yahoo data is committed. This feature uses synthetic frames only.
- **D27–D34 (2026-09-17)** are recorded in spec 009 "User decisions". This spec implements:
  - D27 through `ensure_supported`, with OTC listings rejected as not among D27's exchanges (an interpretation recorded in D39, not a new question);
  - D28 through `outside_session` drops;
  - D32 through §3.3 and D43;
  - D33 through §9 and D44.

  It hands D29, D30, D31 and D34 to #10.
- No pending user decisions: every decision in this spec is technical (D35–D46 and D66).

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria, and T2–T11 fail without the implementation
- [x] Look-ahead harness on `drop_open_candle` and `normalize_candles` with both controls failing (T7)
- [x] No dependency change (V6); no clock or network imports in `data/` (V7)
- [x] `src/trading_bot/data/` is not ignored and is linted, runtime `data/` directories stay ignored, and the `Dockerfile` smoke check is present (V12, AC23)
- [x] No `Any` and no `type: ignore` in the new files; purity guard green (T14)
- [x] Scope limited to Design §1
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)

### Review record

Reviewed on 2026-09-17: **APPROVE**, with three non-blocking LOW notes. The review covered the branch `feature/market-data-provider` on `071e2e3` (#9), the uncommitted diff and the untracked files.

- **Gate.** `uv run python scripts/check.py` is green: ruff, format, mypy, and 4 125 passed, 10 skipped, 99.97% coverage. The new modules are at 100%; the only missing line is the existing `config.py:46`. No leaks were found.
- **Static checks.**
  - The §11 extra-flag mypy command is green.
  - The AC18 and AC19 greps are empty: no `Any`, no `type: ignore`, no clock reads, no network imports and no `nominal_close` in the new source.
  - Every new module defines `__all__`.
  - AC23 holds: `check-ignore` exits 1 for the package, the three runtime paths stay ignored, and `ruff check --show-files .` lists the five package files.
  - The only non-ASCII character in the delivered code, fixtures and tests is an existing em dash in `.gitignore`.
- **Planted bugs.** 48 bugs were planted, one at a time, in a scratch copy, and 47 were killed by the developer's tests alone. The killed ones include:
  - `drop_open_candle` using `nominal_close`, cutting with `side="left"`, always dropping the last row, dropping at most one row, skipping the last-label check or skipping validation;
  - wrong reason precedence, conflicting duplicates keeping a copy, the index unit left unchanged, a naive index localized, and `raw` mutated;
  - `invalid`/`missing` swapped, the chain kept (`from error`), no `lookback` trim, no last-candle requirement, and wrong log levels;
  - `retryable` flipped, the message bounds relaxed, and the D27 policy bypassed;
  - the fake skipping the pipeline, the fake popping failures last-in first-out or never yielding, and each `assert_closed_candles` check disabled.

  The survivor is note 1 below.
- **Docs.** The `## Market data` example in `docs/ARCHITECTURE.md` runs as written.
- **Non-blocking notes:**
  1. No test puts a raw label exactly at `calendar.coverage_end`. Turning `labels >= coverage_end` into `>` (`candle_normalization.py:234`) makes such a label escape as `CalendarRangeError` instead of an `outside_calendar` drop, and no test fails. A boundary test for both coverage edges would close it.
  2. `InvalidTickerError`, `ProviderDataError`, `ProviderUnavailableError` and `CandleNotPublishedError` cannot be pickled or copied with `copy.copy`, because `args` holds the rendered string rather than the constructor arguments (`errors.py:70`). The spec does not require it and no current caller copies errors, but the domain errors support it. Add `__reduce__` if #14, #26 or #27 need to copy error state.
  3. `prepare_candles` is synchronous and takes about 12 ms for a 5 000-row `1h` frame on the development machine (evidence only), more on the Pi. #10 should run it inside the same worker thread as the blocking fetch, not on the event loop.
