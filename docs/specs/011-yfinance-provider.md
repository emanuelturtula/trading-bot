# 011 — YFinanceProvider: Yahoo candles, 4h resampling, retries and ticker validation

- **Status:** approved
- **Branch:** `feature/yfinance-provider`, stacked on `feature/market-data-provider` (#8), which is stacked on `feature/market-calendar` (#9, commit `071e2e3`, PR #42). Last of the M2 series #9 → #8 → #10.
- **Spec author:** tech-lead
- **Issue:** #10 (milestone M2 · Market data)
- **Expected commit type:** `feat:`. The change adds the first real `MarketDataProvider`, a pure domain module and a runtime dependency (`yfinance` plus 16 transitive packages) that changes the published image. `feat` → minor bump. Suggested squash subject: `feat: add the Yahoo Finance market data provider (#<n>)`.

## Goal

Implement the `MarketDataProvider` of spec 010 on yfinance, so the engine (#14) and the scheduler (#15) can fetch closed `1h`, `4h` and `1d` candles of US-listed stocks and ETFs, and `/add` (#19) can validate tickers:

- `1h` and `1d` are fetched natively, and `4h` is built from `1h` bars on the session grid (D31);
- Yahoo's history limits are respected, and capped requests are logged;
- every failure is a typed error, and transient ones get retries with exponential backoff and jitter, a per-attempt timeout and internal pacing;
- tests replay recorded Yahoo structures with synthetic values (D29), and no test can reach the network.

## Out of scope

- **Wiring** the provider into `main.py` or the lifespan (building the calendar and the cache directory, creating the provider, closing it at shutdown): #16. This feature ships `build_yfinance_provider`, and nothing in the running app imports it (D63).
- **The bounded retry window** for `CandleNotPublishedError` (D33, D44): #15. **Choosing `lookback`** from the rules: #14.
- **Persisting** `TickerInfo` (#12), **rendering** errors for users (#19, #22, #23) and **stale-data alerts** (#26).
- Pre-market and after-hours data, non-USD or non-US instruments and per-exchange calendars (D27, D28); dividend-adjusted prices (D30); Yahoo intervals other than `1h` and `1d`; yfinance's `download`, `Tickers`, `info`, `fast_info`, websockets, screeners and price repair.
- New `TB_*` variables, migrations, `config.py`, `logging_setup.py`, `main.py`, `deploy/`, `.github/`, `CLAUDE.md`, `docs/ROADMAP.md`, `docs/DEPLOYMENT.md`.

## Acceptance criteria

Conventions:

- Timestamps written `…Z` are UTC, and "ET" is `America/New_York`.
- "NYSE calendar" is `nyse_test_calendar()` from `tests/fixtures/calendars.py` (2021-01-01 to 2027-12-31).
- "Recording `name`" is the file `tests/fixtures/yahoo/<name>.json` (§13).
- "Hand frame" is the hourly input of §5.3.

### Dependency, image and typing

- [ ] **AC1 (dependency):**
  - `uv add yfinance` writes `"yfinance>=1.7.0"` to `[project.dependencies]`;
  - `uv.lock` gains exactly the 16 packages of §2.1 marked "new", and no locked version changes;
  - `uv export --no-dev --no-hashes --locked --no-emit-project` differs from the same command on the base commit exactly as §2.2 states.
- [ ] **AC2 (arm64 wheels):** that export piped to `uv pip compile - --python-version 3.12 --python-platform aarch64-manylinux_2_28 --only-binary :all:` resolves all 42 runtime packages. No added package is sdist-only (§2.1).
- [ ] **AC3 (image):**
  - the `Dockerfile` runtime stage gains the smoke check of §15, after spec 010's data-layer smoke check and before `USER app`;
  - the check uses no network;
  - the PR's `Docker build (arm64)` job is green.
- [ ] **AC4 (typing):**
  - `[tool.mypy]` gains `untyped_calls_exclude = ["yfinance"]`, and one override `module = ["yfinance", "yfinance.*"]` with `follow_untyped_imports = true`, and nothing else changes in the mypy configuration;
  - `uv run mypy` passes, and so does the extra-flag command of §14;
  - `typing.Any` (the spec 004 `git grep`) and `# type: ignore` are absent from the new `src/` modules, the new `tests/fixtures/` modules and `scripts/record_yahoo_fixture.py`.

### Resampling (`domain/candle_resampling.py`)

- [ ] **AC5 (contract):** `resample_hourly_to_4h` follows §5.1 and §5.2:
  - the argument checks run in the stated order;
  - the result is canonical and passes `validate_candles`;
  - every label is a `4h` slot label closed at `now`, and `slots` is aligned with the rows;
  - the input is never modified;
  - the module is pure.
- [ ] **AC6 (hand-computed case):** the hand frame gives literally the table of §5.3 for every `now` listed, including the empty result and the exact-close boundary.
- [ ] **AC7 (anti look-ahead, rule 4):** the three checks of §5.4 pass:
  - the harness with completion stamps, as a full sweep, on the gap-free frame and on the gapped frame;
  - the two controls, which fail with the stated kinds;
  - the `now` sweep.
- [ ] **AC8 (purity):** the purity guard scans `candle_resampling.py` and passes with no per-file allowance.

### Yahoo types, symbols and metadata (`data/yahoo/history.py`, `data/yahoo/instruments.py`)

- [ ] **AC9 (types):**
  - `HistoryQuery`, `ChartMetadata`, `YahooHistory` and `YahooClient` are exactly as §6 states, including validation order and errors;
  - `ChartMetadata.from_mapping` reads only its six keys through `get`. A mapping whose `__iter__`, `keys`, `items` and `tradingPeriods` lookups raise still works.
- [ ] **AC10 (symbols):** `check_yahoo_symbol` gives the table of §7.1 literally.
- [ ] **AC11 (metadata):**
  - `ticker_info` gives the table of §7.3 literally, on the recording `metadata` and on the synthetic rows;
  - `ASSET_TYPES` and `EXCHANGES` are exactly the read-only mappings of §7.2;
  - no `ValueError` escapes.

### Client (`data/yahoo/client.py`)

- [ ] **AC12 (configuration):**
  - `configure_yfinance` performs the steps of §8.1, and calling it twice with the same directory gives the same state;
  - tests leave no global change behind other than yfinance's cache location, which points to a pytest temporary directory.
- [ ] **AC13 (call):** `YFinanceClient.history`:
  - calls `yfinance.Ticker(symbol)`, `Ticker.history(...)` and `Ticker.get_history_metadata()` once each, with exactly the keyword arguments of §8.2;
  - never passes `end`, and never reads `info`, `fast_info` or `tradingPeriods`.
- [ ] **AC14 (error mapping):** every exception listed in §8.3, built at runtime, maps literally to the stated result, with these properties:
  - the mapped error is raised `from None` (`__cause__ is None` and `__suppress_context__` is true);
  - `str()`, `repr()` and every log record contain no URL, host, query string or crumb-like text from the original exception;
  - the catch-all row emits exactly one `ERROR` record, naming only the exception class and the symbol;
  - `BaseException` subclasses that are not `Exception` propagate unchanged.
- [ ] **AC15 (library canary, no network):** the installed yfinance exposes every surface listed in §8.4.

### Transport (`data/transport.py`)

- [ ] **AC16 (policies):** `RetryPolicy` and `RateLimit` validate as §9.1 states, and `RetryPolicy.delay` gives the table of §9.2 literally.
- [ ] **AC17 (retries):** `ProviderTransport.call` behaves as §9.3 states:
  - it retries `ProviderUnavailableError` only;
  - it makes at most `max_attempts` attempts, and its sleeps equal the policy delays;
  - when attempts run out or the delay is `None`, it re-raises the last error instance;
  - the `INFO` record is literal;
  - any other exception propagates immediately without sleeping.
- [ ] **AC18 (timeouts and concurrency):** as §9.4 states:
  - a timed-out attempt raises `ProviderUnavailableError(TIMEOUT)` `from None`;
  - its worker keeps the in-flight slot until it ends, so another call's operation cannot start meanwhile;
  - late results and exceptions are discarded without logging;
  - `aclose()` waits for running workers, and `call` after `aclose()` raises `RuntimeError`;
  - no test sleeps for real or asserts elapsed time.
- [ ] **AC19 (token bucket):** the sequences of §9.5 hold literally with a fake clock.

### Provider (`data/yahoo/provider.py`, `data/yahoo/factory.py`)

- [ ] **AC20 (planning):** `plan_history` gives the table of §10.1 literally.
- [ ] **AC21 (fetch flow):** `fetch_candles` runs the steps of §10.2 in order:
  - argument and symbol errors are raised before any client call;
  - `CalendarRangeError` propagates;
  - there is one client call per attempt;
  - a `1h` or `1d` result equals `prepare_candles` applied to the client frame;
  - a `4h` result equals `prepare_candles` applied to `publishable_4h` of the resampled, normalized client frame;
  - the post-fetch processing runs in a worker thread, not on the event loop (D67): a spy records that `prepare_candles` is called off the main thread, and the errors it raises reach the caller without consuming a transport attempt.
- [ ] **AC22 (recorded outcomes):**
  - the table of §10.3 holds literally, and every returned frame passes `assert_closed_candles`;
  - for recording `nvda_1d_2024-06-03`, `close` equals the recorded `Close` on every row and differs from `Adj Close`;
  - the structural preconditions of §13.2 are asserted before the outcome checks.
- [ ] **AC23 (4h publication and logs):**
  - `is_unpublished_hour` and `publishable_4h` give the tables of §10.4 literally;
  - through the provider, the hand frame produces literally the records of §10.5;
  - dropped hourly rows are logged by `log_dropped_rows` with the timeframe `1h`, from the worker thread of D67.
- [ ] **AC24 (capped history):** the records of §10.6 hold literally.
- [ ] **AC25 (`validate_ticker`):**
  - the table of §10.7 holds literally;
  - only the errors allowed by spec 010 §7.2 are raised;
  - malformed and ISIN-shaped symbols make no client call.
- [ ] **AC26 (error surface):** each exception of §8.3 is raised inside a fake `yfinance.Ticker` and runs through the real client and transport, with fake sleep and the default policy. `fetch_candles` and `validate_ticker` then raise only the error types of spec 010 §7.2:
  - after exactly `max_attempts` client calls when the mapped error is a `ProviderUnavailableError` whose delays are not `None`;
  - after one call otherwise (non-retryable errors, and `retry_after` above `max_delay`).
- [ ] **AC27 (Protocol and factory):**
  - a typed function returning `YFinanceProvider` as a `MarketDataProvider` passes strict mypy;
  - `build_yfinance_provider` wires the objects of §11, verified without network by the behavioral check stated there;
  - `YFinanceProvider.aclose()` closes the transport.

### Pipeline amendment (`data/pipeline.py`)

- [ ] **AC28:**
  - `log_dropped_rows` is public with the signature of §12;
  - `prepare_candles` uses it, and the spec 010 §8.4 records are unchanged;
  - `__all__` lists it.

### Fixtures, recording script and network guard

- [ ] **AC29 (recordings):**
  - the six files of §13.2 exist in the format of §13.3, and their metadata keys are a subset of the allowlist;
  - a test regenerates every price, volume, dividend and capital-gain value from the file's seed and finds them equal;
  - the structural facts of §13.2 hold.
- [ ] **AC30 (recording script):** `scripts/record_yahoo_fixture.py` behaves as §13.4 states:
  - its pure functions are tested without network;
  - it refuses to write outside `tests/fixtures/yahoo/`;
  - no module under `src/` and no test imports its network path, and no CI step runs it.
- [ ] **AC31 (replay fakes):** `load_recording`, `load_metadata`, `hand_frame`, `RecordedYahooClient` and `as_yahoo_client` behave as §13.5 states.
- [ ] **AC32 (network guard):**
  - the guard of §13.6 is active for every test in the suite, and its self-tests pass;
  - a test that calls the real `YFinanceClient` without a fake fails with `NetworkAccessError`;
  - `asyncio.run` and `socket.socketpair()` work with the guard active, and the whole suite passes with it, including #8's `asyncio.run` tests, `tests/unit/test_health.py` (in-process `TestClient`) and `tests/deploy/`.

### Isolation, docs, gate and scope

- [ ] **AC33 (isolation):** the AST scan and the fresh-interpreter checks of §14 pass.
- [ ] **AC34 (docs):** `docs/ARCHITECTURE.md` and the package docstrings change as §17 states, and the Python blocks of `docs/ARCHITECTURE.md` and of this spec pass `ruff format --check` (the gate formats Markdown code blocks).
- [ ] **AC35 (gate, coverage and budget):**
  - `uv run python scripts/check.py` is green;
  - every new `src/` module, and the changed lines of `data/pipeline.py`, have 100% line and branch coverage;
  - the new tests add at most **15 s** to `pytest`, and no single new test takes more than **2 s** (evidence from `--durations`, reported, never asserted).
- [ ] **AC36 (scope):** compared with the `feature/market-data-provider` commit this branch starts from, only the files of §1 change, and `main.py`, `config.py`, `logging_setup.py` and `deploy/` are untouched.

## Design

### 0. Decisions

Decisions D1–D46 are recorded in specs 003–010. This spec implements D27–D34 and the spec 010 hand-off list (§16.1 maps every item to a section). D47–D64 are technical; D65 is the user's answer about the half-day `4h` candle (see "User decisions").

| ID | Topic | Decision | Why |
|----|-------|----------|-----|
| D47 | Dependency | **`yfinance` 1.7.0**, added with `uv add yfinance` (`yfinance>=1.7.0`) | Issue #10 and `docs/ARCHITECTURE.md` choose yfinance. 1.7.0 is the latest release (2026-08-26) of an actively released package (17 releases since 2025-06, 1.0 in 2025-12), Apache-2.0, and every runtime package it adds has a linux/aarch64 wheel for Python 3.12 (§2). |
| D48 | Fetch boundary | **One public call per fetch:** `Ticker(symbol).history(start=…, interval="1h" or "1d", prepost=False, actions=True, auto_adjust=False, back_adjust=False, repair=False, keepna=False, rounding=False, timeout=15.0)`, then `get_history_metadata()`. **No `end`.** `validate_ticker` uses `period="1mo"`, `interval="1d"` | Spec 010 hand-off 1 fixes `Ticker.history`. When `end` is at least 30 minutes in the past, yfinance answers from an in-memory cache of up to 64 responses, so a #15 retry could re-read the same stale answer until the next close; without `end` it fetches up to the current time, and the pipeline drops the later rows at `DEBUG`. `repair=False` avoids extra requests and rewritten values. `keepna=False` drops Yahoo's all-null rows (the half-day 12:30 ET row, §3.4) instead of logging a `missing_value` warning on every fetch. **Rejected:** `yf.download` (multi-ticker shape, threads); Yahoo's native `4h` interval (not documented by yfinance, same half-day hole, and the issue asks for resampling); requesting the chart endpoint through yfinance's internal `YfData` (not public API); `curl_cffi` without yfinance (the issue requires yfinance). |
| D49 | Layout | Pure **`domain/candle_resampling.py`**. Provider-agnostic **`data/transport.py`**. Subpackage **`data/yahoo/`**: `history.py` (types and the `YahooClient` port, no yfinance), `instruments.py` (symbols and metadata), `client.py` (**the only module that imports yfinance**), `provider.py` and `factory.py` (wiring) | Resampling is a pure frame operation about closedness (spec 010 D35), so it gets the purity guard and the look-ahead obligation. yfinance isolated in one module, like TA-Lib in `talib_kernels.py` and `exchange_calendars` in `nyse.py`, stays replaceable, keeps every exception mapping in one place, and lets the provider and its fakes load without yfinance or `curl_cffi` (fresh-interpreter check, §14). The transport knows nothing about Yahoo. |
| D50 | yfinance global and disk state | **`configure_yfinance(cache_dir)`** is the only code that touches yfinance globals: it creates `cache_dir` (mode `0o700`), calls `set_tz_cache_location`, sets `config.debug.hide_exceptions = False`, `config.debug.logging = False`, `config.network.retries = 0`, and sets the `yfinance` logger to `WARNING`. `cache_dir` is required and has no default. **Recommended for #16:** a per-process temporary directory, removed at shutdown, never the data volume | Measured (§3.7): importing yfinance writes nothing, but the first call creates three SQLite files under the user cache directory, one of them a **pickled** cookie jar. An explicit directory keeps them out of the repository and the home directory and makes tests deterministic; keeping them off `/app/data` keeps pickles out of backups and off a persistent volume, for the price of one cookie, one crumb and one time-zone request per ticker per process. `hide_exceptions=False` turns silently empty frames into exceptions we can map. The logger level stops a `DEBUG` root logger from printing the session crumb and request URLs, which the redaction filter cannot recognize, while yfinance's `WARNING` records (such as the `curl_cffi` fallback) still appear. `retries=0` leaves retries to D54, because yfinance's retry sleeps would block the worker thread. |
| D51 | Metadata source | `validate_ticker` reads the **chart metadata** of the `history` call it just made: `symbol`, `instrumentType`, `exchangeName`, `currency`, `longName`, `shortName`, mapped with the tables of §7.2 (OTC and unknown codes → `OTHER`) | `Ticker.info` calls the quoteSummary endpoint, needs a crumb, is slower and fails with 401, 404 and 429 more often. `fast_info` takes currency, quote type and exchange from the same chart metadata, but its other fields trigger extra history requests. The chart metadata arrives with the price request (no extra request) and has every field `TickerInfo` needs (§3.2). `tradingPeriods` is never read: yfinance loads it lazily with a second request. |
| D52 | Symbols | **`check_yahoo_symbol`** accepts `[A-Z0-9^][A-Z0-9.^=-]{0,23}` (a full match, after `normalize_ticker`), otherwise `malformed`; ISIN-shaped symbols (`[A-Z]{2}[A-Z0-9]{9}[0-9]`) are `not_found` without a request. **`ticker_info`** requires the metadata `symbol` to equal the requested one (`symbol_mismatch` otherwise) | yfinance interpolates the symbol into the request path without escaping, so `/`, `?`, `#` or `%` would reach other Yahoo endpoints. The alphabet covers every Yahoo symbol shape (`BRK-B`, `^GSPC`, `EURUSD=X`, `RELIANCE.NS`, option symbols). `Ticker()` resolves ISIN-shaped text with a search request inside its constructor, outside our timeout, and silently switches to another symbol (§3.3). The symbol echo prevents storing a symbol whose data belongs to another instrument. |
| D53 | Error mapping | **One ordered table** (§8.3) in `client.py`, applied to every exception from yfinance and raised `from None`. Yahoo's "symbol may be delisted" (`yahoo_reason`) → `not_found`; an empty response, or any other Yahoo reason → `NoDataError`; HTTP 404 → `not_found`; 429 → `rate_limited`; 5xx → `server_error`; timeouts → `timeout`; other `OSError` → `connection`; `YFDataException` and malformed JSON → `invalid_response`; anything else → `ProviderDataError("unexpected_provider_error")` with one `ERROR` record naming the class | Implements spec 010 D38 and hand-off 6. Evidence (§3.3): "No data found, symbol may be delisted" is Yahoo's `Not Found` answer (HTTP 404), while yfinance's own "possibly delisted" text also appears on empty answers for valid symbols (a weekend range), so an empty answer is `NoDataError`, not an invalid ticker. It is not treated as transient either: the requested window always contains sessions, and retrying an illiquid ticker only adds load. Unknown exceptions are not retryable: a change in Yahoo's answer does not heal in seconds. The detection works for both HTTP backends (`curl_cffi` and the `requests` fallback) without importing them: both derive their errors from `OSError`, name their timeout class `Timeout`, and attach `response.status_code` to HTTP errors. |
| D54 | Transport | **`ProviderTransport.call(operation)`**: for each attempt, take a token, wait for the single in-flight slot, run the blocking operation with `asyncio.to_thread` under `asyncio.shield`, and wait at most `attempt_timeout`; retry only `ProviderUnavailableError`, with exponential backoff and equal jitter. Defaults: 3 attempts, base 2 s, cap 30 s, 15 s floor after `rate_limited`, `retry_after` honored and waits above the cap not retried, 20 s per attempt, 15 s yfinance request timeout. Clock, sleep and jitter are injected | Spec 010 D36, D44 and hand-off 5. A thread cannot be cancelled, so a timeout abandons its worker, which ends when the library's socket timeouts fire; `shield` keeps the in-flight slot held until then, so yfinance's process-wide session is never used by two threads at once. Equal jitter (a wait in `[cap/2, cap)`) spreads retries without the near-zero waits of full jitter. The floor gives Yahoo room after a 429, which yfinance reports without `Retry-After`. The worst case of one call is 3 attempts × 20 s plus less than 6 s of backoff (30 s after rate limiting) and the pacing wait, well inside one `1h` candle. Pacing and timeouts read the event loop's monotonic clock; D36 still holds, because no decision about candles reads a clock. |
| D55 | Rate limiting | **Token bucket** `RateLimit(rate=1.0, burst=5)`: at most 5 immediate attempts, then one per second, shared by `fetch_candles` and `validate_ticker` of one provider; one token per attempt | Scheduled runs fetch every ticker of a timeframe at the same close. The bucket turns that burst into a steady stream: a run of 50 tickers takes about 45 s of pacing, and the bot never sends more than about 3 600 attempts per hour, far below a bulk scrape, while retries are paced too. The limit is per instance; #16 creates exactly one provider. |
| D56 | History limits | **`plan_history`** starts at the label of the first slot of `candle_window`. For `1h` and `4h` the start is never before `now - 720 days`: otherwise it becomes the label of the first slot of that timeframe at or after that instant (`capped`). `1d` is not capped. The first capped fetch of a `(symbol, timeframe)` per provider logs `INFO`, later ones `DEBUG` | Measured (§3.6): Yahoo rejects `1h` requests that start 730 days ago or earlier (HTTP 422) and accepts 729. The 10-day margin covers #15 retrying a run with the same `now` across a long weekend (up to about 4 days) and clock differences, for the cost of about 60 of 3 500 hourly bars. Yahoo returns the bar that contains the requested start, so a slot label is a safe start. Hand-off 3's "log once" becomes once per provider: hourly runs of rules with long warmups are always capped, and repeating it at `INFO` would be noise. |
| D57 | `4h` resampling | **`resample_hourly_to_4h(hourly, now, *, calendar)`** emits, for every `4h` slot closed at `now` with at least one hourly row, the open of its first row, the highest high, the lowest low, the close of its last row and the sum of volumes, with a per-slot report (`bars`, `missing`, `closing_bar_missing`). It applies no publication policy and does not log | The session grid comes from the calendar (D31, spec 009 D20), so DST changes, half days and late opens need no special cases. D34 (build from the bars that exist) is the pure behavior; the publication rule for the last slot (D58) and the logging depend on Yahoo's behavior, so they live in the data layer. Emitting only slots closed at `now` keeps the function free of look-ahead (§5.4). |
| D58 | Last `4h` slot | **`publishable_4h`** removes the row of the last `4h` slot closed at `now` when its closing hourly bar is missing (hand-off 4), so `prepare_candles` raises `CandleNotPublishedError(missing)`. **Exception (D65):** the row is kept when the missing bar is the truncated final hour of an early-close session (`is_unpublished_hour`) and the hour before it is present | Hand-off 4 prevents evaluating a `4h` candle before Yahoo publishes its last hour. Evidence (§3.4): Yahoo never publishes the 12:30–13:00 ET hour of a half day, for any symbol. Under the strict rule the half-day `4h` candle could never be published live: #15 would retry until the next `4h` close (the next Monday after a Friday half day), and the candle would later appear built from the same three bars without ever being evaluated as the last candle. |
| D59 | Fixtures | **Recordings** in `tests/fixtures/yahoo/*.json` keep yfinance's frame structure (index epoch seconds, zone, unit and name; column order and dtypes) and an allowlist of chart metadata. Every price, volume, dividend and capital-gain value comes from a generator seeded per file and built only on `random.Random(seed).random()`. They are recorded by `scripts/record_yahoo_fixture.py` (manual, needs network, never in CI) and replayed by **`RecordedYahooClient` at the `YahooClient` port**; the client itself is tested with a fake `yfinance.Ticker` | D29. Mocking at the `Ticker` boundary (hand-off 1) tests our flow and mapping. HTTP-level replay would test yfinance's internals (a process-wide session, cookie and crumb requests, three disk caches, a global counter, the wall clock) and cannot intercept `curl_cffi`, whose sockets live in C. `random()` is the only stream Python guarantees across versions, so a test can regenerate the values and prove that no recorded price or volume was kept. The recordings keep structure, timestamps, split ratios and public reference codes and names only. The cost of this boundary is that CI does not run yfinance's own parsing; the canary (§8.4) and the beta deploy cover it. |
| D60 | Network guard | **A suite-wide autouse fixture** makes `socket.socket.connect`, `socket.socket.connect_ex`, `socket.getaddrinfo` and `curl_cffi`'s `Session.request` and `AsyncSession.request` raise `NetworkAccessError`, a `BaseException`, for anything but loopback | "Tests without network" (CLAUDE.md) becomes enforced. A `BaseException` escapes the `except Exception` blocks of yfinance and of our client. Socket creation, `bind`, `listen` and `socket.socketpair()` are never patched, and loopback connections stay allowed: every `asyncio.run` creates a self-pipe with `socketpair()`, a native call on Linux (selector loop) and a loopback listener plus `connect` on Windows (proactor loop). `curl_cffi` needs its own patch because libcurl opens sockets in C. |
| D61 | Typing | `follow_untyped_imports = true` for yfinance (as spec 009 D24) plus `untyped_calls_exclude = ["yfinance"]` | yfinance ships no `py.typed`. Following it keeps real types for `Ticker`, the exception classes and `config`. `Ticker(...)` has an unannotated constructor, which strict mypy reports as an untyped call, and the exclusion silences only that, for yfinance only. `ignore_missing_imports` would make every yfinance name `Any`. Measured on the prototype: strict mypy and the three extra flags pass. |
| D62 | Image check | A build-time `RUN` imports `curl_cffi`, creates and closes an impersonating session, and imports yfinance and the provider factory | Nothing in the running app imports the provider yet, so neither the arm64 build nor `/health` would notice a broken install. If `curl_cffi` cannot load, yfinance quietly falls back to `requests` without browser impersonation, which Yahoo throttles; the check fails the build instead. |
| D63 | Wiring | **Deferred to #16.** `build_yfinance_provider(calendar=…, cache_dir=…)` exists and is tested; `main.py` does not call it | The lifespan also builds the calendar (spec 009 D18), closes the transport and removes the cache directory; those belong together in #16. Wiring now would start network activity with no consumer. |
| D64 | Pipeline helper | **`log_dropped_rows`** replaces #8's private `_log_dropped(dropped, expected, request, logger)` in `data/pipeline.py` as a public function, with identical records | The `4h` path must log dropped hourly rows in exactly the spec 010 §8.4 format before resampling, and a second copy would drift. Spec 010 AC15 stays unchanged. |
| D67 | Post-fetch processing | **The work after the client call runs off the event loop,** in its own `asyncio.to_thread` worker: `1h` and `1d` run `prepare_candles`, and `4h` runs normalization, the hourly drop logs, resampling, the gap logs, `publishable_4h` and `prepare_candles` (§10.2 step 6). It is not part of the transport operation, so the attempt timeout, the retries, the pacing token and the in-flight slot cover the client call only | Measured with the implementation on the development machine: 2.0 ms for 1 301 daily rows, 8.6 ms for 3 436 hourly rows and 30 ms for the whole `4h` path (3 436 hourly rows into 981 candles). The Raspberry Pi is several times slower, so a scheduled run of a handful of tickers would block the loop that also runs the Telegram poller and the scheduler for hundreds of milliseconds per ticker. **Rejected: processing inside the transport operation.** Its cost would count against `attempt_timeout`, so slow processing of a successful download would be reported as `timeout` and retried; and the abandoned worker of a timed-out attempt would keep processing and emit gap and dropped-row records for a call that already failed, duplicating them when the retry succeeds and breaking the literal records of §8.4 and §10.5. **Rejected: running it on the loop**, which is what blocks. The in-flight slot is not held, because it exists to serialize yfinance's process-wide session and this work touches no yfinance state; processing one ticker may overlap the next ticker's download. Threads do not free the loop completely (label classification and the per-slot calendar work are Python-level and hold the GIL), but the loop interleaves instead of stalling. |
| D68 | One event loop per provider | **A provider and its transport belong to the event loop that first uses them.** #16 builds the provider inside the running loop and never shares it with another loop; tests build a new transport (and provider) for each `asyncio.run` | `asyncio.Semaphore` and `asyncio.Lock` bind to the running loop the first time a waiter blocks on them, and raise `RuntimeError` afterwards from another loop. The app has a single loop (`CLAUDE.md` rule 7), so this only constrains tests and any future tool that calls `asyncio.run` twice. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `pyproject.toml`, `uv.lock` | developer | `uv add yfinance`; the mypy settings of AC4 |
| `Dockerfile` | developer | Smoke check (§15); explicitly authorized by this spec |
| `src/trading_bot/domain/candle_resampling.py` | developer | §5 |
| `src/trading_bot/domain/__init__.py` | developer | Docstring package map mentions `candle_resampling` (§17) |
| `src/trading_bot/data/__init__.py` | developer | Docstring mentions `transport` and the `yahoo` subpackage |
| `src/trading_bot/data/pipeline.py` | developer | §12 |
| `src/trading_bot/data/transport.py` | developer | §9 |
| `src/trading_bot/data/yahoo/__init__.py` | developer | Package docstring; no re-exports |
| `src/trading_bot/data/yahoo/history.py` | developer | §6 |
| `src/trading_bot/data/yahoo/instruments.py` | developer | §7 |
| `src/trading_bot/data/yahoo/client.py` | developer | §8 |
| `src/trading_bot/data/yahoo/provider.py` | developer | §10 |
| `src/trading_bot/data/yahoo/factory.py` | developer | §11 |
| `scripts/record_yahoo_fixture.py` | developer | §13.4 |
| `tests/fixtures/yahoo/*.json` (6 files) | developer | Recorded with the script (§13.2) |
| `tests/fixtures/yahoo_recordings.py` | developer | Loaders, `RecordedYahooClient`, `hand_frame` (§13.5) |
| `tests/fixtures/network_guard.py` | developer | §13.6 |
| `tests/conftest.py` | developer | Autouse network guard (§13.6) |
| `tests/unit/test_candle_resampling.py` | developer | TDD: T1–T2 |
| `tests/unit/test_candle_resampling_lookahead.py` | developer | TDD: T3 |
| `tests/unit/test_yahoo_instruments.py` | developer | TDD: T4 |
| `tests/unit/test_yahoo_client.py` | developer | TDD: T5–T6 |
| `tests/unit/test_provider_transport.py` | developer | TDD: T7–T8 |
| `tests/unit/test_yfinance_provider.py` | developer | TDD: T9–T11 |
| `tests/unit/test_yahoo_recordings.py` | developer | TDD: T12 |
| `tests/scripts/test_record_yahoo_fixture.py` | developer | TDD: T13 |
| `tests/unit/test_network_guard.py` | developer | TDD: T14 |
| `tests/unit/test_yahoo_isolation.py` | developer | TDD: T15 |
| `tests/unit/test_domain_purity.py` | developer | T16 |
| `tests/unit/test_market_data_pipeline.py` | developer | T17 (`log_dropped_rows`) |
| `tests/unit/test_yfinance_provider_properties.py` | tester | T18 |
| `tests/unit/test_yfinance_provider_adversarial.py` | tester | T19 |
| `docs/ARCHITECTURE.md` | developer | §17; explicitly authorized by this spec |
| `docs/specs/011-yfinance-provider.md` | tech-lead | This spec |

No migrations and no `TB_*` variables. `tests/fixtures/calendars.py` (#9) and `tests/fixtures/{session_candles,fake_provider,provider_contract}.py` (#8) are reused unchanged.

**`.gitignore`:** this feature relies on the `!src/trading_bot/data/` negation that #8 adds (spec 010 D66). The `data/` line otherwise ignores `src/trading_bot/data/` at any depth, including `data/transport.py` and `data/yahoo/`. Checked with `git check-ignore -v` on a copy of `.gitignore`:
- with the negation, `src/trading_bot/data/transport.py` and `src/trading_bot/data/yahoo/client.py` are not ignored, and a root `data/*.db` still is;
- `tests/fixtures/yahoo/*.json` and `scripts/record_yahoo_fixture.py` are not ignored, with or without it.

#10 changes no ignore rule.

### 2. Dependency and supply chain

Evidence gathered by the tech-lead on 2026-09-17 in a scratch copy of `pyproject.toml` and `uv.lock` taken from `071e2e3` (the project files were never edited): PyPI metadata, `uv add --no-sync yfinance`, `uv export`, `uv pip compile` for linux/aarch64, a scratch environment with the resulting lock, and a prototype of §5–§12.

#### 2.1 Packages

`uv.lock` gains 16 packages and no locked version changes. `platformdirs` was already locked for the development tools and becomes a runtime dependency. `frozendict` is not a dependency of 1.7.0 (yfinance vendors its own).

| Package | Version | Lock | License | Why it is installed | linux/aarch64 wheel for Python 3.12 |
|---------|---------|------|---------|---------------------|-------------------------------------|
| `yfinance` | 1.7.0 | new | Apache-2.0 | The provider | `py3-none-any` |
| `curl-cffi` | 0.16.3 | new | MIT | yfinance's HTTP backend (browser TLS impersonation); bundles libcurl-impersonate | `cp310-abi3-manylinux2014_aarch64` |
| `cffi` | 2.1.1 | new | MIT-0 | `curl-cffi` | `cp312-manylinux2014_aarch64` |
| `pycparser` | 3.0 | new | BSD-3-Clause | `cffi` (marker `implementation_name != 'PyPy'`) | `py3-none-any` |
| `certifi` | 2026.7.22 | new | MPL-2.0 | CA bundle for `curl-cffi` and `requests` | `py3-none-any` |
| `requests` | 2.34.2 | new | Apache-2.0 | yfinance's fallback backend | `py3-none-any` |
| `urllib3` | 2.8.0 | new | MIT | `requests` | `py3-none-any` |
| `charset-normalizer` | 3.5.1 | new | MIT | `requests` | `manylinux_2_28_aarch64` (abi3 and cp312) |
| `peewee` | 4.5.1 | new | MIT (license file; no classifier) | yfinance's SQLite caches | `py3-none-any` |
| `platformdirs` | 4.11.8 | dev → runtime | MIT | yfinance's default cache path | `py3-none-any` |
| `protobuf` | 7.36.1 | new | BSD-3-Clause | yfinance's live-price messages (never used) | `cp310-abi3-manylinux2014_aarch64` |
| `websockets` | 17.1 | new | BSD-3-Clause | yfinance's live prices (never used) | `cp312-manylinux_2_28_aarch64` |
| `beautifulsoup4` | 4.15.0 | new | MIT | yfinance's consent-page parsing | `py3-none-any` |
| `soupsieve` | 2.9.2 | new | MIT | `beautifulsoup4` | `py3-none-any` |
| `lxml` | 6.1.3 | new | BSD-3-Clause | yfinance's HTML parsing | `cp312-manylinux_2_28_aarch64` |
| `multitasking` | 0.0.13 | new | Apache-2.0 | yfinance's threaded downloads (never used) | `py3-none-any` |
| `pytz` | 2026.3.post1 | new | MIT | yfinance's time zones | `py2.py3-none-any` |

- **No sdist-only package:** every row has a wheel, so the `python:3.12-slim` builder, which has no compiler, never builds from source.
- **Size:** the aarch64 wheels add about 20 MB compressed, 12.8 MB of it `curl-cffi` and 5.0 MB `lxml`.
- **Maintenance:**
  - `yfinance` has 150 releases since 2019, and 17 since 2025-06. It is maintained by its original author and community contributors, as an unofficial client of undocumented Yahoo endpoints that break when Yahoo changes them (Risks).
  - `curl-cffi` has 101 releases, the last on 2026-09-02; `peewee` has 221, the last on 2026-09-08; `multitasking` (by yfinance's author) has 13, the last on 2026-04-23. The rest are mainstream packages released within the last months.
- **Supply-chain notes** (the `pandas-ta` precedent):
  - hashes are pinned in `uv.lock`, and Dependabot bumps are reviewed;
  - `curl-cffi` carries a native libcurl build with its own TLS stack, so TLS fixes arrive only through `curl-cffi` releases;
  - `peewee`'s cookie cache unpickles bytes from disk, which D50 confines to a private temporary directory;
  - `import yfinance` loads `protobuf`, `websockets`, `multitasking`, `beautifulsoup4` and `lxml` eagerly, although the provider never uses their features.

#### 2.2 Runtime export difference (AC1)

`uv export --no-dev --no-hashes --locked --no-emit-project` on the branch, compared with the same command on the base commit:

- **17 added requirements, each with its `# via` lines:**
  - `beautifulsoup4==4.15.0` (via `yfinance`);
  - `certifi==2026.7.22` (via `curl-cffi`, `requests`);
  - `cffi==2.1.1` (via `curl-cffi`);
  - `charset-normalizer==3.5.1` (via `requests`);
  - `curl-cffi==0.16.3` (via `yfinance`);
  - `lxml==6.1.3` (via `yfinance`);
  - `multitasking==0.0.13` (via `yfinance`);
  - `peewee==4.5.1` (via `yfinance`);
  - `platformdirs==4.11.8` (via `yfinance`);
  - `protobuf==7.36.1` (via `yfinance`);
  - `pycparser==3.0 ; implementation_name != 'PyPy'` (via `cffi`);
  - `pytz==2026.3.post1` (via `yfinance`);
  - `requests==2.34.2` (via `yfinance`);
  - `soupsieve==2.9.2` (via `beautifulsoup4`);
  - `urllib3==2.8.0` (via `requests`);
  - `websockets==17.1` (via `yfinance`);
  - `yfinance==1.7.0` (via `trading-bot`).
- **Changed `# via` annotations only:** `idna` gains `requests`; `numpy` and `pandas` gain `yfinance`; `typing-extensions` gains `beautifulsoup4`.
- No other line changes. If a Dependabot bump reaches the base first, compare against the rebased base and report it.

#### 2.3 linux/arm64 resolution (AC2)

`uv export --no-dev --no-hashes --locked --no-emit-project | uv pip compile - --python-version 3.12 --python-platform aarch64-manylinux_2_28 --only-binary :all:` resolved all 42 runtime packages on the scratch lock. The same command for `manylinux_2_17` fails on `numpy`, before and after this change; `python:3.12-slim` is a Debian image with a newer glibc, which the TA-Lib and calendar smoke checks already prove on the arm64 job.

### 3. Yahoo and yfinance behavior (evidence)

Probed on 2026-09-17 with yfinance 1.7.0 from a scratch environment, with read-only requests. Only structure, reference codes and counts are written here; no price or volume.

#### 3.1 `Ticker.history(auto_adjust=False, prepost=False, actions=True)`

| Item | `1h` | `1d` |
|------|------|------|
| Index | `DatetimeIndex`, `datetime64[s, America/New_York]`, named `Datetime` | Same dtype, named `Date` |
| Labels | Bar open: 09:30, 10:30, …, 15:30 ET | 00:00 ET of the session date |
| Columns | `Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`, plus `Capital Gains` for `ETF` and `MUTUALFUND` | Same |
| Dtypes | `float64`, except `Volume` `int64` | Same |
| Prices | `Close` is split-adjusted; `Adj Close` is split- and dividend-adjusted. D30 uses `Close`, which `normalize_candles` selects | Same |
| During the session | The in-progress `1h` bar, with the latest trade merged in | Today's in-progress row |

- **Requested start:** Yahoo returns the bar that contains `period1` (a start at 11:30:01 ET returns the 11:30 bar; a daily start at 09:30:01 ET returns that day).
- **Live row:** Yahoo appends a row stamped with the last trade time. yfinance drops it when it falls outside the regular period and otherwise merges it into the preceding bar when both fall within one hour (Risks).

#### 3.2 Chart metadata

`instrumentType` / `exchangeName` / `fullExchangeName` / `currency` / `exchangeTimezoneName`:

| Symbol | Metadata | D27 outcome |
|--------|----------|-------------|
| `AAPL`, `MSTR` | `EQUITY` / `NMS` / `NasdaqGS` / `USD` / New York | supported (`XNAS`) |
| `QQQ` | `ETF` / `NGM` / `NasdaqGM` / `USD` / New York | supported (`XNAS`) |
| `BRK-B` (also requested as `brk-b`) | `EQUITY` / `NYQ` / `NYSE` / `USD` / New York | supported (`XNYS`) |
| `SPY`, `IWM` | `ETF` / `PCX` / `NYSEArca` / `USD` / New York | supported (`ARCX`) |
| `UEC`, `IMO` | `EQUITY` / `ASE` / `NYSE American` / `USD` / New York | supported (`XASE`) |
| `ARKB`, `HODL` | `ETF` / `BTS` / `Cboe US` / `USD` / New York | supported (`BATS`) |
| `^GSPC` | `INDEX` / `SNP` / `SNP` / `USD` / New York | `unsupported_asset_type` |
| `VFIAX` | `MUTUALFUND` / `NAS` / `Nasdaq` / `USD` / New York | `unsupported_asset_type` |
| `BTC-USD` | `CRYPTOCURRENCY` / `CCC` / `CCC` / `USD` / UTC | `unsupported_asset_type` |
| `EURUSD=X` | `CURRENCY` / `CCY` / `CCY` / `USD` / London | `unsupported_asset_type` |
| `ES=F` | `FUTURE` / `CME` / `CME` / `USD` / New York (`longName` null) | `unsupported_asset_type` |
| `RELIANCE.NS` | `EQUITY` / `NSI` / `NSE` / `INR` / Kolkata | `unsupported_exchange` |
| `TCEHY` | `EQUITY` / `PNK` / `OTC Markets OTCPK` / `USD` / New York | `unsupported_exchange` |
| `NSRGY` | `EQUITY` / `OID` / `OTC Markets OTCID` / `USD` / New York (non-ASCII `longName`) | `unsupported_exchange` |
| `SHOP.TO` | `EQUITY` / `TOR` / `Toronto` / `CAD` / Toronto | `unsupported_exchange` |

The metadata `symbol` echoes Yahoo's canonical symbol (`brk-b` → `BRK-B`). Symbols are reused over time: `FB` now answers as an `ETF` on `BTS`.

#### 3.3 Errors

| Request | Yahoo answer | What yfinance raises (`hide_exceptions=False`) |
|---------|--------------|-----------------------------------------------|
| Unknown symbol | HTTP 404, `{"code": "Not Found", "description": "No data found, symbol may be delisted"}` | The first two per process: `HTTPError` 404 from the quoteSummary fallback of its time-zone lookup, whose message contains the request URL. Later: `YFPricesMissingError` with that `yahoo_reason` |
| `BRK.B` (wrong separator), delisted `TWTR` | Same 404 | `YFPricesMissingError`, `yahoo_reason` "No data found, symbol may be delisted" |
| Valid symbol, range without sessions (a weekend) | HTTP 200, metadata, no timestamps | `YFPricesMissingError` without `yahoo_reason`, message "possibly delisted; no price data found …" |
| `1h` starting 730 days ago (729 is accepted) | HTTP 422, "… The requested range must be within the last 730 days." | `YFPricesMissingError` with that `yahoo_reason` |
| Range before the listing date | HTTP 400, "Data doesn't exist for startDate = …, endDate = …" | `YFPricesMissingError` with that `yahoo_reason` |
| A 12-character symbol shaped like an ISIN | — | `Ticker()` itself sends a search request and raises `ValueError("Invalid ISIN number: …")` |

#### 3.4 Half days

On all five early-close sessions inside the hourly window (2024-11-29, 2024-12-24, 2025-07-03, 2025-11-28, 2025-12-24), for `SPY`, `AAPL` and `UEC`:

- raw Yahoo `1h` returns the 12:30 ET row with every value null, a post-market 13:00 ET row despite `includePrePost=false`, and a live 16:00 row;
- yfinance returns only the 09:30, 10:30 and 11:30 ET bars;
- Yahoo's native `4h` interval has the same hole (labels 09:30, 13:00 and 16:00 on that day).

The 12:30–13:00 ET half hour of a half day is never published as hourly data, not even months later.

#### 3.5 Data quality (spec 010 Risks, hand-off 7)

Offline counts on real responses, never stored:

- **Sample:** 10 symbols (`SPY`, `AAPL`, `BRK-B`, `QQQ`, `IWM`, `UEC`, `ARKB`, `IMO`, `HODL`, `SCHD`), 729 days of `1h` (about 3 470 rows each) and 10 years of `1d` (about 2 512 rows each; 672 for the 2024 listings).
- **Found none of:** NaN rows, rows with `open` or `close` outside `[low, high]` or `high < low`, non-positive prices, duplicate labels, or labels the calendar rejects.
- **Zero-volume hourly rows:** 1 to 20 per symbol, which are valid.
- **Missing hourly slots:** 14 or 15 of 3 485 per symbol, namely the five half-day 12:30 slots plus a Yahoo outage on 2026-01-30 (from 10:30 or 11:30 ET to the close) and 2026-02-02 (09:30–12:30 ET).
- **Missing daily sessions:** none.

**Conclusion:** D32's drop-without-repair policy is adequate, and no repair with an epsilon needs to be proposed.

#### 3.6 History limits

- **`1h`:** accepted when the start is less than 730 days before Yahoo's clock.
- **`1d`:** unlimited (`AAPL` returned 10 068 daily rows in one call).
- **Sub-hour intervals:** 60 days, and never used.

At `2026-09-17T15:00:00Z`, the 720-day cap of D56 leaves 3 435 `1h` slots and 980 `4h` slots.

#### 3.7 Global and disk state

- **Import:** `import yfinance` creates no file. It computes the cache path `platformdirs.user_cache_dir()/py-yfinance`, adds a `warnings` filter, and leaves the `yfinance` logger at `NOTSET`, so a `DEBUG` root logger prints `crumb = '…'` and every request URL with its parameters.
- **Disk:** the first request creates three peewee SQLite files: `tkr-tz.db` (symbol → time zone), `cookies.db` (a **pickled** cookie jar) and `isin-tkr.db`.
- **Session:** `YfData` is a process-wide singleton session (`curl_cffi` impersonating Chrome, or `requests` with one `WARNING` when `curl_cffi` cannot load). The first request of a process fetches a cookie and a crumb.
- **Defaults:** `config.debug.hide_exceptions` is `True` (errors are logged and an empty frame is returned), and `config.network.retries` is `0`.
- **Before the first history call of a `Ticker`,** yfinance resolves the symbol's time zone: the SQLite cache, then a chart request with a fixed 10 s timeout, then, at most twice per process, `info`. Responses whose `end` lies at least 30 minutes in the past are served from an in-memory cache of 64 entries, and `pd.Timestamp.now()` is read when `end` is omitted.

### 4. Data flow

```text
fetch_candles(ticker, timeframe, lookback, now)
  1 CandleRequest + check_yahoo_symbol                                  (no I/O)
  2 plan_history ── candle_window ── 720-day cap for 1h/4h ── capped log
  3 ProviderTransport.call ── token ── in-flight slot ── to_thread + timeout ── retries
        └─ YFinanceClient.history ── yfinance.Ticker.history + get_history_metadata ── §8.3 mapping
  4 asyncio.to_thread (a second worker, no timeout, no slot: D67)
    1h, 1d: prepare_candles(frame)                                      (spec 010 §8)
    4h:     normalize_candles(frame, 1h) ── log_dropped_rows(1h)
            ── resample_hourly_to_4h(now) ── gap logs ── publishable_4h
            ── prepare_candles(4h frame)
validate_ticker(ticker)
  parse_ticker + check_yahoo_symbol ── transport.call(history(1d, period 1mo))
  ── ticker_info(metadata) ── ensure_supported
```

### 5. `domain/candle_resampling.py`

#### 5.1 API

```python
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar

__all__ = ["ResampledCandles", "ResampledSlot", "resample_hourly_to_4h"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResampledSlot:
    slot: CandleSlot  # the 4h slot of the row
    bars: int  # hourly rows aggregated, >= 1
    # labels of the slot's 1h grid slots without a row, stdlib UTC, increasing
    missing: tuple[datetime, ...]
    closing_bar_missing: bool  # the 1h slot whose close_time equals slot.close_time has no row


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class ResampledCandles:
    candles: pd.DataFrame  # canonical 4h frame, one row per entry of `slots`, in the same order
    slots: tuple[ResampledSlot, ...]


def resample_hourly_to_4h(
    hourly: pd.DataFrame, now: datetime, *, calendar: MarketCalendar
) -> ResampledCandles: ...
```

`ResampledSlot` is equal by value and hashable; `ResampledCandles` holds a frame, so it compares by identity (as spec 010's `NormalizedCandles`).

#### 5.2 Semantics

Checks, in this order:

1. `TypeError` when `calendar` is not a `MarketCalendar`.
2. `now` through `to_utc` (`TypeError`/`ValueError`).
3. `validate_candles(hourly)` (`TypeError`/`CandleValidationError`).
4. `last = calendar.closed_candles(Timeframe.H4, now, 1)[-1]` (`CalendarRangeError`).
5. The **considered rows** are those labelled before `last.close_time`. Each considered label must be a `1h` slot label: the error of `calendar.candle_slot(Timeframe.H1, label)` propagates (`CandleLabelError`, `CalendarRangeError`, or `ValueError` from `to_utc` for a sub-microsecond label). Later rows are ignored without checks.

Result:

- **Rows.** For every `4h` slot `S` with `S.label <= last.label` that contains at least one considered row (`S.open_time <= label < S.close_time`), in label order, one row labelled `S.label`:
  - `open`: the `open` of its earliest row;
  - `high`: the highest `high`;
  - `low`: the lowest `low`;
  - `close`: the `close` of its latest row;
  - `volume`: the sum of `volume`.
- **Report.** For each row, `ResampledSlot(slot=S, bars=…, missing=…, closing_bar_missing=…)`, where `missing` lists the labels of `calendar.candle_slots(Timeframe.H1, S.open_time, S.close_time)` that have no row, and `closing_bar_missing` tells whether the last of those `1h` slots has no row.
- **Frame.**
  - index `datetime64[us, UTC]` named `None`, columns `OHLCV_COLUMNS`, all `float64`;
  - `validate_candles` runs as a postcondition, and its error propagates (only reachable when a volume sum overflows to `inf`);
  - the result is a new object, and `hourly` is never modified.
- **Empty.** With no considered rows, the result is an empty canonical frame and `slots == ()`.
- **Purity.** No clock, no logging, no module state. Guidance, not an AC: one grid query per call and binary searches, with no calendar call per valid row.

#### 5.3 Hand-computed case (AC6)

The **hand frame** holds the hourly rows below. It is given to the provider shaped like yfinance's output (§13.5: `datetime64[s, America/New_York]` index named `Datetime`, `Adj Close` equal to `Close`, `Volume` `int64`, `Dividends` and `Stock Splits` `0.0`), and to `resample_hourly_to_4h` after `normalize_candles`. It covers a regular session with an interior gap, Thanksgiving, a half day without its truncated last hour (as Yahoo publishes it, §3.4) and a session whose closing hour is missing.

| Label (UTC) | ET | open | high | low | close | volume |
|-------------|----|------|------|-----|-------|--------|
| 2024-11-27T14:30Z | Wed 09:30 | 100 | 102 | 99 | 101 | 1000 |
| 2024-11-27T15:30Z | 10:30 | 101 | 104 | 100 | 103 | 2000 |
| *(2024-11-27T16:30Z, 11:30: no row)* | | | | | | |
| 2024-11-27T17:30Z | 12:30 | 103 | 105 | 98 | 99 | 1500 |
| 2024-11-27T18:30Z | 13:30 | 99 | 100 | 97 | 98 | 800 |
| 2024-11-27T19:30Z | 14:30 | 98 | 99 | 96 | 97 | 900 |
| 2024-11-27T20:30Z | 15:30 (30-minute slot) | 97 | 101 | 97 | 100 | 3000 |
| *(2024-11-28: Thanksgiving, no session)* | | | | | | |
| 2024-11-29T14:30Z | Fri half day 09:30 | 100 | 103 | 100 | 102 | 500 |
| 2024-11-29T15:30Z | 10:30 | 102 | 106 | 101 | 105 | 700 |
| 2024-11-29T16:30Z | 11:30 | 105 | 105 | 102 | 104 | 600 |
| *(2024-11-29T17:30Z, 12:30–13:00: no row)* | | | | | | |
| 2024-12-02T14:30Z | Mon 09:30 | 104 | 108 | 103 | 107 | 1200 |
| 2024-12-02T15:30Z | 10:30 | 107 | 109 | 106 | 108 | 1100 |
| 2024-12-02T16:30Z | 11:30 | 108 | 110 | 107 | 109 | 1000 |
| 2024-12-02T17:30Z | 12:30 | 109 | 111 | 105 | 106 | 900 |
| 2024-12-02T18:30Z | 13:30 | 106 | 107 | 104 | 105 | 700 |
| 2024-12-02T19:30Z | 14:30 | 105 | 106 | 102 | 103 | 650 |
| *(2024-12-02T20:30Z, 15:30: no row)* | | | | | | |

The `4h` rows, computed by hand (verified on the prototype):

| `4h` label | Hourly rows | open | high | low | close | volume | `bars` | `missing` | `closing_bar_missing` |
|------------|-------------|------|------|-----|-------|--------|--------|-----------|-----------------------|
| 2024-11-27T14:30Z | 14:30Z, 15:30Z, 17:30Z | 100 | 105 | 98 | 99 | 4500 | 3 | 2024-11-27T16:30Z | false |
| 2024-11-27T18:30Z | 18:30Z, 19:30Z, 20:30Z | 99 | 101 | 96 | 100 | 4700 | 3 | — | false |
| 2024-11-29T14:30Z | 14:30Z, 15:30Z, 16:30Z | 100 | 106 | 100 | 104 | 1800 | 3 | 2024-11-29T17:30Z | true |
| 2024-12-02T14:30Z | 14:30Z–17:30Z | 104 | 111 | 103 | 106 | 4200 | 4 | — | false |
| 2024-12-02T18:30Z | 18:30Z, 19:30Z | 106 | 107 | 102 | 103 | 1350 | 2 | 2024-12-02T20:30Z | true |

| `now` | Rows returned |
|-------|---------------|
| 2024-11-27T18:29:59.999999Z | none: the last closed `4h` slot is 2024-11-26T18:30Z; empty frame and `slots == ()` |
| 2024-11-27T18:30:00Z | 2024-11-27T14:30Z (exactly its close, D19) |
| 2024-11-27T21:00:30Z | 2024-11-27T14:30Z, 2024-11-27T18:30Z |
| 2024-11-29T18:00:30Z | the previous two and 2024-11-29T14:30Z |
| 2024-12-02T18:30:30Z | the previous three and 2024-12-02T14:30Z |
| 2024-12-02T21:00:30Z | all five |
| 2024-12-03T18:30:30Z | all five (no hourly row after 2024-12-02) |

#### 5.4 Anti look-ahead (AC7, rule 4)

A `4h` row labelled at its slot open is only complete when the slot closes, so the harness compares rows at the moment they become available. **Completion stamp:** for a row labelled `L` computed on a prefix `P`, the first label of `P` whose `1h` slot closes at or after the close of the `4h` slot `L`.

1. **Harness.** `resample_at(P)` is `resample_hourly_to_4h(P, calendar.candle_slot(Timeframe.H1, P.index[-1]).close_time, calendar=cal).candles`, with its index replaced by the completion stamps. `assert_no_lookahead(resample_at, frame, max_cuts=len(frame))` passes on:
   - the **gap-free frame** `session_candles(nyse_test_calendar(), Timeframe.H1, utc("2024-11-20T00:00"), utc("2024-12-10T00:00"))` (88 rows);
   - the **gapped frame**: the same without the rows labelled 2024-11-27T16:30Z, 2024-11-29T17:30Z and 2024-12-02T20:30Z and every row of the 2024-12-04 session (78 rows).

   Stamps are unique on both frames. A gap pattern in which two slots share a stamp is invalid input for this wrapper, not a failure of the function.
2. **Controls,** each raising `LookaheadError`:
   - `cheat_in_progress` resamples with `now` three days after the prefix's last close and stamps each row with the last prefix label before its slot's close → `result_disappeared`;
   - `cheat_next_open` is `resample_at` with each `close` replaced by the `open` of the prefix row after the row's stamp (NaN when there is none) → `value_mismatch`.
3. **`now` sweep** on the gapped frame. For every `1h` slot `h` labelled in `[2024-11-20T00:00Z, 2024-12-10T00:00Z)` and every `now` in `{h.close_time − 1 µs, h.close_time}`, `resample_hourly_to_4h(frame, now).candles`:
   - equals, exactly, the result on only the rows whose `1h` slot closes at or before `now` (no row published after `now` is read);
   - equals the rows with the same labels of `resample_hourly_to_4h(frame, utc("2024-12-10T00:00")).candles` (a returned row is final).

The tech-lead ran all three on the prototype: green, and both controls caught.

### 6. `data/yahoo/history.py`

```python
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

import pandas as pd

__all__ = [
    "ISIN_PATTERN",
    "YAHOO_SYMBOL_PATTERN",
    "ChartMetadata",
    "HistoryQuery",
    "YahooClient",
    "YahooHistory",
    "YahooInterval",
]

YAHOO_SYMBOL_PATTERN: Final = re.compile(r"[A-Z0-9^][A-Z0-9.^=-]{0,23}")  # used with fullmatch
ISIN_PATTERN: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")  # used with fullmatch

type YahooInterval = Literal["1h", "1d"]


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryQuery:
    symbol: str
    interval: YahooInterval
    start: datetime | None = None  # stdlib UTC; exactly one of start and period
    period: Literal["1mo"] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChartMetadata:
    symbol: str | None
    instrument_type: str | None
    exchange_name: str | None
    currency: str | None
    long_name: str | None
    short_name: str | None

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, object]) -> "ChartMetadata": ...


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class YahooHistory:
    frame: pd.DataFrame  # exactly as yfinance returned it
    metadata: ChartMetadata


class YahooClient(Protocol):
    def history(self, query: HistoryQuery) -> YahooHistory: ...
```

- **`HistoryQuery`** validates in `__post_init__`, in this order:
  1. `symbol` is a `str` (`TypeError`) that fully matches `YAHOO_SYMBOL_PATTERN` and does not fully match `ISIN_PATTERN` (`ValueError`);
  2. `interval` is `"1h"` or `"1d"` (`ValueError`);
  3. exactly one of `start` and `period` is set (`ValueError`);
  4. `start` goes through `to_utc` and is stored as a stdlib UTC `datetime`;
  5. `period`, when set, is `"1mo"` (`ValueError`).

  It is frozen, equal by value and hashable. These are programming errors: the provider validates user text first (§7.1).
- **`ChartMetadata.from_mapping`** raises `TypeError` for a non-`Mapping`. It calls `metadata.get(key)` once for each of `symbol`, `instrumentType`, `exchangeName`, `currency`, `longName` and `shortName`, and keeps a value only when it is a `str` (otherwise `None`). It never iterates the mapping and never reads `tradingPeriods`, which yfinance loads lazily with an extra request.
- **`YahooClient.history`** (docstring of the Protocol): a blocking call, run in a worker thread by the transport. It raises only `MarketDataError` subclasses (the results of §8.3) and `BaseException`s that are not `Exception`.
- The module imports neither yfinance nor any HTTP library.

### 7. `data/yahoo/instruments.py`

```python
from collections.abc import Mapping
from typing import Final

from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.data.yahoo.history import ChartMetadata

__all__ = ["ASSET_TYPES", "EXCHANGES", "check_yahoo_symbol", "ticker_info"]

ASSET_TYPES: Final[Mapping[str, AssetType]]  # types.MappingProxyType, §7.2
EXCHANGES: Final[Mapping[str, Exchange]]  # types.MappingProxyType, §7.2


def check_yahoo_symbol(symbol: str) -> str: ...
def ticker_info(symbol: str, metadata: ChartMetadata) -> TickerInfo: ...
```

#### 7.1 `check_yahoo_symbol` (AC10)

It receives the output of `parse_ticker`, returns it unchanged, or raises `InvalidTickerError` without any I/O. Checks:

1. `TypeError` for a non-`str`.
2. No full match of `YAHOO_SYMBOL_PATTERN` → `malformed`.
3. A full match of `ISIN_PATTERN` → `not_found`.

The error's `ticker` is the symbol when `normalize_ticker(symbol) == symbol`, otherwise `None`. Messages name the reason and, when set, the ticker.

| Input | Result |
|-------|--------|
| `SPY`, `BRK-B`, `^GSPC`, `EURUSD=X`, `ES=F`, `RELIANCE.NS`, `BTC-USD`, `A` | returned |
| `ABCDEFGHIJKLMNOPQRSTUVWX` (24 characters) | returned |
| `ABCDEFGHIJKLMNOPQRSTUVWXY` (25 characters) | `malformed` |
| `.SPY`, `-SPY`, `=X` | `malformed` (first character) |
| `M&M.NS`, `SPY/1`, `SPY?X`, `SPY#1`, `SPY%20` | `malformed` |
| `spy` (not normalized) | `malformed` |
| `US0378331005` | `not_found` |
| `US037833100A` (the last character is not a digit) | returned |
| `42` (an `int`) | `TypeError` |

#### 7.2 Mapping tables (AC11)

| `instrumentType` | `AssetType` |
|------------------|-------------|
| `EQUITY` | `EQUITY` |
| `ETF` | `ETF` |
| `INDEX` | `INDEX` |
| `MUTUALFUND` | `MUTUAL_FUND` |
| `CRYPTOCURRENCY` | `CRYPTOCURRENCY` |
| `CURRENCY` | `CURRENCY` |
| `FUTURE` | `FUTURE` |
| `OPTION` | `OPTION` |
| anything else, or missing | `OTHER` |

| `exchangeName` | `Exchange` | Seen as `fullExchangeName` |
|----------------|------------|----------------------------|
| `NYQ` | `NYSE` | `NYSE` |
| `NMS`, `NGM`, `NCM` | `NASDAQ` | `NasdaqGS`, `NasdaqGM`, `NasdaqCM` |
| `PCX` | `NYSE_ARCA` | `NYSEArca` |
| `ASE` | `NYSE_AMERICAN` | `NYSE American` |
| `BTS` | `CBOE_BZX` | `Cboe US` |
| anything else (`PNK`, `OID`, `OQB`, `OQX`, `NAS`, `SNP`, `CCC`, `CCY`, `CME`, `NSI`, `TOR`, …), or missing | `OTHER` | |

Codes match exactly and case-sensitively. `NAS` appears for Nasdaq mutual funds, which the asset type rejects first.

#### 7.3 `ticker_info` (AC11)

Checks, in this order; every `ProviderDataError` has `ticker=symbol`:

1. `metadata.symbol` is `None` → `ProviderDataError("invalid_metadata")`.
2. `normalize_ticker(metadata.symbol)` raises → `ProviderDataError("invalid_metadata")`.
3. The normalized metadata symbol differs from `symbol` → `ProviderDataError("symbol_mismatch")`.
4. `metadata.currency` is `None` or not 1–8 ASCII letters → `ProviderDataError("invalid_metadata")`.
5. `asset_type` and `exchange` come from §7.2.
6. `name` is the first of `long_name` and `short_name` that `TickerInfo` accepts (spec 010 §6.1), otherwise `symbol`.

It returns the `TickerInfo`, and no `ValueError` escapes. The D27 policy (`ensure_supported`) is applied by the provider (§10.7).

On the recording `metadata` (§13.2):

| Symbol | `asset_type` | `exchange` | `currency` | `name` taken from |
|--------|--------------|------------|------------|-------------------|
| `AAPL` | `equity` | `XNAS` | `USD` | `longName` |
| `QQQ` | `etf` | `XNAS` | `USD` | `longName` |
| `BRK-B` | `equity` | `XNYS` | `USD` | `longName` |
| `SPY` | `etf` | `ARCX` | `USD` | `longName` |
| `UEC` | `equity` | `XASE` | `USD` | `longName` |
| `ARKB` | `etf` | `BATS` | `USD` | `longName` |
| `^GSPC` | `index` | `OTHER` | `USD` | `longName` |
| `VFIAX` | `mutual_fund` | `OTHER` | `USD` | `longName` |
| `BTC-USD` | `cryptocurrency` | `OTHER` | `USD` | `longName` |
| `EURUSD=X` | `currency` | `OTHER` | `USD` | `longName` |
| `ES=F` | `future` | `OTHER` | `USD` | `shortName` (`longName` is null) |
| `RELIANCE.NS` | `equity` | `OTHER` | `INR` | `longName` |
| `TCEHY` | `equity` | `OTHER` | `USD` | `longName` |
| `NSRGY` | `equity` | `OTHER` | `USD` | `longName` (non-ASCII, kept) |
| `SHOP.TO` | `equity` | `OTHER` | `CAD` | `longName` |

Synthetic rows (the `AAPL` metadata with one change, built in the test):

| Change | Result |
|--------|--------|
| `exchangeName` `NCM` | `exchange` `XNAS` |
| `exchangeName` `" NMS"`, missing, or `42` | `exchange` `OTHER` |
| `instrumentType` `ECNQUOTE`, `equity`, or missing | `asset_type` `OTHER` |
| `symbol` missing, or `AAPL\|X` | `ProviderDataError("invalid_metadata")` |
| `symbol` `MSFT` | `ProviderDataError("symbol_mismatch")` |
| `symbol` `aapl` | accepted |
| `currency` missing, `""`, `US$`, or `USDOLLARS` | `ProviderDataError("invalid_metadata")` |
| `currency` `GBp` | accepted, stored as `GBp` |
| `longName` with a line break, 121 characters, or only whitespace, with a valid `shortName` | `name` from `shortName` |
| both names missing or invalid | `name` `AAPL` |

### 8. `data/yahoo/client.py`

```python
from pathlib import Path
from typing import Final

from trading_bot.data.errors import MarketDataError
from trading_bot.data.yahoo.history import HistoryQuery, YahooHistory

__all__ = ["YAHOO_REQUEST_TIMEOUT", "YFinanceClient", "configure_yfinance", "map_yahoo_error"]

YAHOO_REQUEST_TIMEOUT: Final = 15.0  # seconds, passed to Ticker.history(timeout=...)


def configure_yfinance(cache_dir: Path) -> None: ...


class YFinanceClient:
    """The YahooClient backed by yfinance. The only class in src/ that calls yfinance."""

    def __init__(
        self, *, cache_dir: Path, request_timeout: float = YAHOO_REQUEST_TIMEOUT
    ) -> None: ...

    def history(self, query: HistoryQuery) -> YahooHistory: ...


def map_yahoo_error(error: Exception, *, symbol: str) -> MarketDataError: ...
```

This module is the only one in `src/` that imports `yfinance` (`import yfinance` and `from yfinance.exceptions import …`). It imports neither `curl_cffi` nor `requests`.

#### 8.1 `configure_yfinance` (AC12)

1. `TypeError` unless `cache_dir` is a `pathlib.Path`; `ValueError` unless it is absolute.
2. `cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)`; an `OSError` propagates (a startup configuration error).
3. `yfinance.set_tz_cache_location(str(cache_dir))`, which moves the time-zone, cookie and ISIN caches.
4. `yfinance.config.debug.hide_exceptions = False`.
5. `yfinance.config.debug.logging = False`.
6. `yfinance.config.network.retries = 0`.
7. `logging.getLogger("yfinance").setLevel(logging.WARNING)`.

It does not create the SQLite files, set a proxy, or read environment variables. `YFinanceClient.__init__` validates `request_timeout` (an `int` or `float`, not a `bool`, finite and `> 0`: `TypeError`/`ValueError`) and then calls `configure_yfinance(cache_dir)`.

Tests replace `yfinance.set_tz_cache_location` with a recorder, or point it at a pytest temporary directory, and restore `yfinance.config` and the `yfinance` logger level afterwards.

#### 8.2 The call (AC13)

`history(query)` runs, inside one `try` block:

```python
ticker = yfinance.Ticker(query.symbol)
frame = ticker.history(
    start=query.start,  # a start query; a period query passes period="1mo" instead
    interval=query.interval,
    prepost=False,
    actions=True,
    auto_adjust=False,
    back_adjust=False,
    repair=False,
    keepna=False,
    rounding=False,
    timeout=self._request_timeout,
)
metadata = ChartMetadata.from_mapping(ticker.get_history_metadata())
```

- A start query passes `start` and omits `period`; a period query passes `period="1mo"` and omits `start`. yfinance's default `period` is a sentinel string, so `None` is never passed. Neither shape passes `end` (D48) or `raise_errors` (deprecated).
- `except Exception as error: raise map_yahoo_error(error, symbol=query.symbol) from None`. `BaseException`s that are not `Exception` are not caught.
- After the `try`: a `frame` that is not a `pd.DataFrame`, or metadata that is not a `Mapping`, raises `ProviderDataError("invalid_response", ticker=symbol)`.
- It returns `YahooHistory(frame=frame, metadata=metadata)`, with the frame unmodified.

#### 8.3 Error mapping (AC14, AC26)

`map_yahoo_error` applies the first matching row. The **failure** column is the `ProviderFailure` of a `ProviderUnavailableError`. Every result has `ticker=symbol`.

| # | Exception | Result |
|---|-----------|--------|
| 1 | a `MarketDataError` | the same object |
| 2 | `yfinance.exceptions.YFRateLimitError` | `ProviderUnavailableError`, failure `rate_limited`, `retry_after=None` |
| 3 | `YFTzMissingError` | `InvalidTickerError(not_found)` |
| 4 | `YFPricesMissingError` whose `yahoo_reason` is a `str` containing `symbol may be delisted` (case-insensitive) | `InvalidTickerError(not_found)` |
| 5 | any other `YFPricesMissingError` | `NoDataError` |
| 6 | `YFDataException` | failure `invalid_response` |
| 7 | `json.JSONDecodeError`, including the JSON errors of `requests` and `curl_cffi`, which subclass it | failure `invalid_response` |
| 8 | builtin `TimeoutError`, or any exception with a class named `Timeout` in its MRO (`Timeout`, `ConnectTimeout`, `ReadTimeout` of both backends) | failure `timeout` |
| 9 | any exception whose `response.status_code` is an `int` (not `bool`) | 404 → `InvalidTickerError(not_found)`; 429 → failure `rate_limited`, with `retry_after` from a `Retry-After` header made only of digits (seconds), capped at 1 hour, otherwise `None`; 500 to 599 → failure `server_error`; any other status → failure `invalid_response` |
| 10 | any other `OSError` (connection, DNS, TLS, reset; both backends' `RequestException` derive from `OSError`) | failure `connection` |
| 11 | any other `Exception` | `ProviderDataError("unexpected_provider_error")`, plus one `ERROR` record on `trading_bot.data.yahoo.client`: `unexpected %s from yfinance for %s`, with `module.qualname` of the exception class and the symbol |

Rows 1 to 10 log nothing. Messages are built only from the symbol, enum values and the stated texts; the original message, its URL, query string, headers and body never appear.

The tests build these exceptions at runtime, each with a message that contains a crumb-like URL assembled at runtime (for example `"https://" + "query.example.invalid" + "/v8/finance/chart/SPY?crumb=" + "c" * 11`). They assert the result and that neither `example.invalid` nor `crumb` appears in `str()`, `repr()` or the captured log text:

| Exception built at runtime | Row | Result |
|----------------------------|-----|--------|
| `NoDataError("…", ticker="SPY")` | 1 | same object |
| `YFRateLimitError()` | 2 | `rate_limited` |
| `YFTzMissingError("SPY")` | 3 | `not_found` |
| `YFPricesMissingError("SPY", "", yahoo_reason="No data found, symbol may be delisted")` | 4 | `not_found` |
| `YFPricesMissingError("SPY", " (1h 2026-09-12 12:00:00+00:00 -> 2026-09-13 08:00:00+00:00)")` | 5 | `NoDataError` |
| `YFPricesMissingError("SPY", "", yahoo_reason="1h data not available for startTime=1 and endTime=2. The requested range must be within the last 730 days.")` | 5 | `NoDataError` |
| `YFDataException("*** YAHOO! FINANCE IS CURRENTLY DOWN! ***")` | 6 | `invalid_response` |
| `json.JSONDecodeError("Expecting value", "<html>", 0)`; the `requests` JSON decode error | 7 | `invalid_response` |
| `TimeoutError()`; `curl_cffi.requests.exceptions.Timeout`; `curl_cffi…ConnectTimeout`; `requests.exceptions.ReadTimeout` | 8 | `timeout` |
| an HTTP error of each backend with `response.status_code` 404 | 9 | `not_found` |
| status 429 with `Retry-After: 7` / `7200` / `Wed, 21 Oct 2026 07:28:00 GMT` / no header | 9 | `rate_limited`, `retry_after` 7 s / 3 600 s / `None` / `None` |
| status 500 and 503 | 9 | `server_error` |
| status 401 | 9 | `invalid_response` |
| `curl_cffi…DNSError`; `requests.exceptions.ConnectionError`; `ssl.SSLError`; `ConnectionResetError`; `socket.gaierror` | 10 | `connection` |
| `KeyError("chart")`; `ValueError("Invalid ISIN number: X")`; `YFInvalidPeriodError("SPY", "2mo", "1d, 5d")` | 11 | `unexpected_provider_error`, one `ERROR` record, for example `unexpected builtins.KeyError from yfinance for SPY` |
| `KeyboardInterrupt()`; `NetworkAccessError` (§13.6) | — | propagates unchanged (not an `Exception`) |

#### 8.4 Library canary (AC15)

Without network, a test checks that the installed yfinance still has what §8 relies on:

- the parameters of `yfinance.scrapers.history.PriceHistory.history` include `period`, `interval`, `start`, `end`, `prepost`, `actions`, `auto_adjust`, `back_adjust`, `repair`, `keepna`, `rounding` and `timeout`;
- `yfinance.Ticker` has `history` and `get_history_metadata`;
- `yfinance.exceptions` defines `YFException`, `YFDataException`, `YFRateLimitError`, `YFTzMissingError` and `YFPricesMissingError`, and `YFPricesMissingError("SPY", "", yahoo_reason="x").yahoo_reason == "x"`;
- `yfinance.set_tz_cache_location` is callable, and `yfinance.config.debug.hide_exceptions`, `yfinance.config.debug.logging` and `yfinance.config.network.retries` can be read and assigned (restored afterwards);
- `yfinance.utils.is_isin` agrees with `ISIN_PATTERN` on `US0378331005`, `US037833100A` and `SPY`.

A failure after a Dependabot bump means §8 must be revisited, not that the test should be loosened.

### 9. `data/transport.py`

#### 9.1 API

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from trading_bot.data.errors import ProviderUnavailableError
from trading_bot.domain.timeframe import Timeframe

__all__ = ["ProviderTransport", "RateLimit", "RetryPolicy", "TokenBucket"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 2.0  # seconds
    max_delay: float = 30.0
    rate_limited_delay: float = 15.0
    attempt_timeout: float = 20.0

    def delay(
        self, attempt: int, error: ProviderUnavailableError, jitter: float
    ) -> float | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    rate: float = 1.0  # tokens per second
    burst: int = 5


class TokenBucket:
    def __init__(
        self,
        limit: RateLimit,
        *,
        clock: Callable[[], float] | None = None,  # default: the running loop's time()
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None: ...

    async def acquire(self) -> None: ...


class ProviderTransport:
    def __init__(
        self,
        *,
        policy: RetryPolicy,
        limiter: TokenBucket,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] | None = None,  # default: random() of a private random.Random()
        # default: logging.getLogger("trading_bot.data.transport")
        logger: logging.Logger | None = None,
    ) -> None: ...

    async def call[T](
        self, operation: Callable[[], T], *, ticker: str, timeframe: Timeframe | None
    ) -> T: ...

    async def aclose(self) -> None: ...
```

Validation, raising `TypeError` for wrong types and `ValueError` for bad values:

- **`RetryPolicy`:**
  - `max_attempts` is an `int` (not `bool`) in `[1, 10]`;
  - every delay and the timeout are an `int` or `float` (not `bool`) and finite;
  - `base_delay > 0`, `max_delay >= base_delay`, `0 <= rate_limited_delay <= max_delay` and `attempt_timeout > 0`.
- **`RateLimit`:** `rate` is an `int` or `float` (not `bool`), finite and `> 0`; `burst` is an `int` (not `bool`) `>= 1`.
- **`delay`:** `attempt` is an `int` (not `bool`) `>= 1`, and `jitter` is in `[0.0, 1.0)`.

The module reads no wall clock: the default `clock` is `asyncio.get_running_loop().time`, called inside `acquire`, and the spec 010 AC18 `git grep` over `src/trading_bot/data` stays empty.

**One event loop (D68).** `TokenBucket` and `ProviderTransport` own `asyncio.Lock` and `asyncio.Semaphore` objects, which bind to the running loop the first time a waiter blocks on them. A transport, and the provider that holds it, therefore belong to one event loop: the docstrings say so, #16 builds them inside the running loop, and each test that calls `asyncio.run` builds its own.

#### 9.2 `RetryPolicy.delay` (AC16)

1. `cap = min(max_delay, base_delay * 2 ** (attempt - 1))`.
2. `wait = cap / 2 + jitter * cap / 2` (equal jitter).
3. If `error.failure` is `rate_limited`: `wait = max(wait, rate_limited_delay)`.
4. If `error.retry_after` is set: `wait = max(wait, retry_after.total_seconds())`.
5. Return `None` when `wait > max_delay` (do not retry), otherwise `wait`.

With the defaults (verified on the prototype):

| `attempt` | `failure` | `retry_after` | `jitter` | Result |
|-----------|-----------|---------------|----------|--------|
| 1 | `connection` | — | 0.0 | 1.0 |
| 1 | `connection` | — | 0.5 | 1.5 |
| 2 | `timeout` | — | 0.75 | 3.5 |
| 3 | `server_error` | — | 0.0 | 4.0 |
| 5 | `connection` | — | 0.5 | 22.5 |
| 1 | `rate_limited` | — | 0.5 | 15.0 |
| 2 | `rate_limited` | — | 0.75 | 15.0 |
| 1 | `invalid_response` | 20 s | 0.0 | 20.0 |
| 4 | `connection` | 30 s | 0.0 | 30.0 |
| 1 | `rate_limited` | 31 s | 0.0 | `None` |

#### 9.3 `call` (AC17)

1. `RuntimeError("provider transport is closed")` after `aclose()`, before anything else.
2. For `attempt` from 1:
   1. `await limiter.acquire()`;
   2. run one attempt (§9.4);
   3. return its result;
   4. on `ProviderUnavailableError` (from the operation or from the timeout): if `attempt < max_attempts` and `policy.delay(attempt, error, jitter())` is not `None`, log one `INFO` record, `await sleep(delay)` and continue; otherwise re-raise that same error instance;
   5. any other exception (other `MarketDataError`s, `TypeError`, `BaseException`s) propagates immediately, without sleeping.
3. The `INFO` record, on the transport logger: `retrying provider call for %s %s after %s (attempt %d of %d, waiting %.3f s)`, with the ticker, the timeframe code or `-`, the failure value, the attempt, `max_attempts` and the delay. For example: `retrying provider call for SPY 1h after connection (attempt 1 of 3, waiting 1.500 s)`. Nothing is logged when the error is finally raised; the caller logs it.

Concurrent `call`s are allowed: they queue for tokens (in arrival order) and for the in-flight slot.

#### 9.4 One attempt: thread, timeout and in-flight slot (AC18)

1. Wait for the in-flight slot, an `asyncio.Semaphore(1)` owned by the transport.
2. Start `asyncio.to_thread(operation)` as a task, and release the slot in the task's done callback, which also retrieves its result or exception and discards it without logging.
3. `await asyncio.wait_for(asyncio.shield(task), policy.attempt_timeout)`.
4. On `TimeoutError`: raise `ProviderUnavailableError(TIMEOUT, ticker=…, timeframe=…)` `from None`. The worker keeps running, and keeps the slot, until the operation returns or raises (yfinance's own socket timeouts bound it).
5. If `call` is cancelled while waiting, the cancellation propagates, and the worker likewise keeps the slot until it ends.
6. `aclose()` marks the transport closed and awaits every running worker (their outcomes discarded); it is idempotent.

Deterministic test recipe: the operation blocks on a `threading.Event`, the policy has `attempt_timeout=0.001` and `max_attempts=1`, and the test sets the event only after asserting the timeout. A second `call` started meanwhile records that its operation begins only after the first worker's operation has ended (an ordered list, not a clock).

#### 9.5 `TokenBucket` (AC19)

The bucket starts full (`burst` tokens). Each `acquire`, serialized by an `asyncio.Lock`, loops:

1. `now = clock()`; if a previous reading exists, add `max(0, now - previous) * rate` tokens, capped at `burst`; remember `now`.
2. With at least one token, take it and return.
3. Otherwise `await sleep((1 - tokens) / rate)`.

A clock that goes backwards adds nothing. With a fake clock that starts at `1000.0` and a fake `sleep` that records the delay and advances the clock by it (verified on the prototype):

| `RateLimit` | Script | Recorded sleeps |
|-------------|--------|-----------------|
| `rate=0.5, burst=2` | 4 acquires | `[2.0, 2.0]` |
| `rate=1.0, burst=5` | 7 acquires | `[1.0, 1.0]` |
| `rate=1.0, burst=2` | 2 acquires; clock set to `1001.5`; 2 acquires | `[0.5]` |
| `rate=1.0, burst=2` | 2 acquires; clock set to `990.0`; 1 acquire | `[1.0]` |

**Coarse host clocks (reviewed for #10).** When the shortfall to the next token is smaller than the host clock's resolution, `acquire` can loop more than once before the clock reports progress: `time.monotonic()` advances in steps of about 15.6 ms on Windows, against nanoseconds on Linux. Each pass still awaits `sleep`, which on a real host advances the clock, so the effect is bounded by one clock step and measured in a couple of extra wakeups (12 paced acquisitions at `rate=1.9, burst=7` took 26 sleeps instead of 24 in a simulation with a 15.6 ms clock). It costs nothing on the Raspberry Pi, changes no pacing guarantee, and needs no floor on the computed wait. Property tests that draw arbitrary rates discard that degenerate case instead of asserting one sleep per acquisition.

### 10. `data/yahoo/provider.py`

```python
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import pandas as pd

from trading_bot.data.provider import CandleRequest
from trading_bot.data.tickers import TickerInfo
from trading_bot.data.transport import ProviderTransport
from trading_bot.data.yahoo.history import YahooClient, YahooInterval
from trading_bot.domain.candle_resampling import ResampledCandles
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "INTRADAY_HISTORY",
    "HistoryPlan",
    "YFinanceProvider",
    "is_unpublished_hour",
    "plan_history",
    "publishable_4h",
]

INTRADAY_HISTORY: Final = timedelta(days=720)


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryPlan:
    interval: YahooInterval
    start: datetime  # stdlib UTC; sent to Yahoo
    requested_start: datetime  # the label of candle_window(request).first
    capped: bool


def plan_history(request: CandleRequest, *, calendar: MarketCalendar) -> HistoryPlan: ...
def is_unpublished_hour(slot: CandleSlot, *, calendar: MarketCalendar) -> bool: ...
def publishable_4h(
    resampled: ResampledCandles, *, last: CandleSlot, calendar: MarketCalendar
) -> pd.DataFrame: ...


class YFinanceProvider:
    def __init__(
        self,
        *,
        client: YahooClient,
        transport: ProviderTransport,
        calendar: MarketCalendar,
        # default: logging.getLogger("trading_bot.data.yahoo.provider")
        logger: logging.Logger | None = None,
    ) -> None: ...

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame: ...

    async def validate_ticker(self, ticker: str) -> TickerInfo: ...

    async def aclose(self) -> None: ...  # closes the transport
```

The constructor raises `TypeError` for a `transport` that is not a `ProviderTransport` or a `calendar` that is not a `MarketCalendar`. The only mutable state is the set of `(symbol, timeframe)` pairs already logged as capped (§10.6). `plan_history`, `is_unpublished_hour` and `publishable_4h` raise `TypeError` for arguments of the wrong type.

#### 10.1 `plan_history` (AC20)

1. `window = candle_window(request, calendar=calendar)`; `CalendarRangeError` propagates. `requested = window.first.label`.
2. `1d`: interval `"1d"`, `start = requested`, not capped.
3. `1h` and `4h`: interval `"1h"`, and `earliest = request.now - INTRADAY_HISTORY`.
   - If `requested >= earliest`: `start = requested`, not capped.
   - Otherwise: `start` is the label of `calendar.candle_slots(request.timeframe, earliest, window.last.label + 1 µs)[0]`, and `capped=True`. That tuple is never empty, because the last closed slot is always after `earliest`.

NYSE calendar, verified on the prototype:

| `timeframe`, `now`, `lookback` | `interval` | `requested_start` | `start` | `capped` |
|--------------------------------|------------|-------------------|---------|----------|
| `1h`, `2025-11-28T18:00:30Z`, 5 | `1h` | `2025-11-26T20:30Z` | `2025-11-26T20:30Z` | false |
| `4h`, `2025-11-28T18:00:30Z`, 5 | `1h` | `2025-11-25T14:30Z` | `2025-11-25T14:30Z` | false |
| `1d`, `2025-12-02T21:00:30Z`, 5 | `1d` | `2025-11-25T05:00Z` | `2025-11-25T05:00Z` | false |
| `1d`, `2026-09-17T15:00:00Z`, 1 000 | `1d` | `2022-09-21T04:00Z` | `2022-09-21T04:00Z` | false |
| `1h`, `2026-09-17T15:00:00Z`, 3 435 | `1h` | `2024-09-27T15:30Z` | `2024-09-27T15:30Z` | false |
| `1h`, `2026-09-17T15:00:00Z`, 3 436 | `1h` | `2024-09-27T14:30Z` | `2024-09-27T15:30Z` | true |
| `1h`, `2026-09-17T15:00:00Z`, 5 000 | `1h` | `2023-11-03T19:30Z` | `2024-09-27T15:30Z` | true |
| `4h`, `2026-09-17T15:00:00Z`, 980 | `1h` | `2024-09-27T17:30Z` | `2024-09-27T17:30Z` | false |
| `4h`, `2026-09-17T15:00:00Z`, 981 | `1h` | `2024-09-27T13:30Z` | `2024-09-27T17:30Z` | true |
| `4h`, `2026-09-17T15:00:00Z`, 5 000 | — | `CalendarRangeError` (the window starts in 2016, before the test calendar) | | |

At that `now`, `earliest` is `2024-09-27T15:00:00Z`.

#### 10.2 `fetch_candles` (AC21)

1. `request = CandleRequest(ticker=ticker, timeframe=timeframe, lookback=lookback, now=now)`.
2. `symbol = check_yahoo_symbol(request.ticker)`.
3. `plan = plan_history(request, calendar=calendar)`.
4. When `plan.capped`, log it (§10.6).
5. `history = await transport.call(lambda: client.history(HistoryQuery(symbol=symbol, interval=plan.interval, start=plan.start)), ticker=symbol, timeframe=timeframe)`.
6. `return await asyncio.to_thread(self._prepare, history.frame, request, symbol)`: the whole post-fetch processing runs in a worker thread (D67), outside the transport, so it has no attempt timeout, takes no pacing token and holds no in-flight slot.
7. `_prepare(frame, request, symbol)`, the synchronous body of that worker:
   1. `1h` and `1d`: return `prepare_candles(frame, request, calendar=calendar)`.
   2. `4h`: `hourly = normalize_candles(frame, Timeframe.H1, calendar=calendar)`. A `CandleNormalizationError` becomes `ProviderDataError(error.kind.value, …, ticker=symbol, timeframe=Timeframe.H4)` raised `from None`.
   3. `log_dropped_rows(hourly.dropped, ticker=symbol, timeframe=Timeframe.H1, last_label=calendar.closed_candles(Timeframe.H1, request.now, 1)[-1].label)`, on the pipeline's default logger.
   4. `resampled = resample_hourly_to_4h(hourly.candles, request.now, calendar=calendar)`.
   5. `last = calendar.closed_candles(Timeframe.H4, request.now, 1)[-1]`.
   6. `frame = publishable_4h(resampled, last=last, calendar=calendar)`, logging as §10.5 states.
   7. Return `prepare_candles(frame, request, calendar=calendar)`.

Steps 1 to 4 make no client call, and they are the only work on the event loop besides awaiting: they are argument checks and one calendar query. Errors raised inside the worker propagate unchanged to the caller: the transport never sees them, so they consume no attempt and trigger no retry. The worker has no timeout, because a thread cannot be cancelled and the work is local; a cancellation of `fetch_candles` during it (shutdown) lets the thread finish. Its log records are emitted before `fetch_candles` returns, so the order and content of §10.5 and §10.6 do not change.

Every returned frame meets the spec 010 §7.2 contract, and every error is one that §7.2 allows. For `4h`, `CandleNotPublishedError.reason` is always `missing`; a dropped closing hourly row is visible in the `1h` warning of step 7.3.

#### 10.3 Recorded outcomes (AC22)

Each row fetches through `RecordedYahooClient`, serving the named recording for its symbol and interval, on the NYSE calendar. The lists give labels in UTC, oldest first. Verified on the prototype, with scratch recordings of the same calls (structure and timestamps from Yahoo, synthetic values, never stored in the repository).

| Recording | `timeframe` | `now` | `lookback` | Result |
|-----------|-------------|-------|------------|--------|
| `spy_1h_2025-11-24` | `1h` | `2025-11-26T21:00:30Z` | 7 | 7 rows, `2025-11-26T14:30Z` … `2025-11-26T20:30Z` |
| `spy_1h_2025-11-24` | `1h` | `2025-11-28T18:00:30Z` | 5 | `CandleNotPublishedError`: `missing`, expected `2025-11-28T17:30Z`, last `2025-11-28T16:30Z` |
| `spy_1h_2025-11-24` | `1h` | `2025-12-01T15:30:30Z` | 3 | `2025-11-28T15:30Z`, `2025-11-28T16:30Z`, `2025-12-01T14:30Z` |
| `spy_1h_2025-11-24` | `4h` | `2025-11-28T18:00:30Z` | 5 | `2025-11-25T14:30Z`, `2025-11-25T18:30Z`, `2025-11-26T14:30Z`, `2025-11-26T18:30Z`, `2025-11-28T14:30Z` |
| `spy_1h_2025-11-24` | `4h` | `2025-12-01T14:30:00Z` | 2 | `2025-11-26T18:30Z`, `2025-11-28T14:30Z` |
| `spy_1h_2025-11-24` | `4h` | `2025-12-02T21:00:30Z` | 6 | `2025-11-26T18:30Z`, `2025-11-28T14:30Z`, `2025-12-01T14:30Z`, `2025-12-01T18:30Z`, `2025-12-02T14:30Z`, `2025-12-02T18:30Z` |
| `spy_1d_2025-11-24` | `1d` | `2025-11-28T17:59:59Z` | 2 | `2025-11-25T05:00Z`, `2025-11-26T05:00Z` |
| `spy_1d_2025-11-24` | `1d` | `2025-11-28T18:00:30Z` | 3 | `2025-11-25T05:00Z`, `2025-11-26T05:00Z`, `2025-11-28T05:00Z` |
| `aapl_1h_2026-01-28` | `1h` | `2026-01-30T21:00:30Z` | 3 | `CandleNotPublishedError`: `missing`, expected `2026-01-30T20:30Z`, last `2026-01-30T15:30Z` |
| `aapl_1h_2026-01-28` | `4h` | `2026-01-30T21:00:30Z` | 3 | `CandleNotPublishedError`: `missing`, expected `2026-01-30T18:30Z`, last `2026-01-30T14:30Z` |
| `aapl_1h_2026-01-28` | `4h` | `2026-02-02T18:30:30Z` | 3 | `CandleNotPublishedError`: `missing`, expected `2026-02-02T14:30Z`, last `2026-01-30T14:30Z` |
| `aapl_1h_2026-01-28` | `4h` | `2026-02-03T21:00:30Z` | 6 | `2026-01-29T14:30Z`, `2026-01-29T18:30Z`, `2026-01-30T14:30Z`, `2026-02-02T18:30Z`, `2026-02-03T14:30Z`, `2026-02-03T18:30Z` |
| `spy_1h_2025-03-06` | `1h` | `2025-03-10T14:30:00Z` | 2 | `2025-03-07T20:30Z`, `2025-03-10T13:30Z` |
| `spy_1h_2025-03-06` | `4h` | `2025-03-10T20:00:30Z` | 4 | `2025-03-07T14:30Z`, `2025-03-07T18:30Z`, `2025-03-10T13:30Z`, `2025-03-10T17:30Z` |
| `nvda_1d_2024-06-03` | `1d` | `2024-06-14T20:00:30Z` | 10 | the 10 sessions `2024-06-03T04:00Z` … `2024-06-14T04:00Z`; `close` equals the recorded `Close`, not `Adj Close` |

These outcomes depend only on labels, so they hold for any synthetic values.

#### 10.4 `is_unpublished_hour` and `publishable_4h` (AC23)

**`is_unpublished_hour(slot, *, calendar)`** raises `ValueError` when `slot.timeframe` is not `Timeframe.H1`. It is true exactly when the slot lasts less than one hour and its `close_time`, in `calendar.timezone`, is before 16:00: the truncated final hour of an early-close session, which Yahoo never publishes (§3.4).

| Slot | Result |
|------|--------|
| `1h` 2024-11-29T17:30Z (12:30–13:00 ET, half day) | true |
| `1h` 2024-07-03T16:30Z (12:30–13:00 EDT, half day) | true |
| `1h` 2024-11-27T20:30Z (15:30–16:00 ET, regular close) | false |
| `1h` 2024-11-29T16:30Z (11:30–12:30 ET, a full hour of a half day) | false |
| any `4h` or `1d` slot | `ValueError` |

**`publishable_4h(resampled, *, last, calendar)`:**

1. If `resampled.slots` is empty, or its last entry's `slot` is not `last`, or that entry's `closing_bar_missing` is false: return `resampled.candles`.
2. **Early-close exception (D65):** let `hours = calendar.candle_slots(Timeframe.H1, last.open_time, last.close_time)`. If `is_unpublished_hour(hours[-1])` and `hours[-2].label` is not in the entry's `missing`, return `resampled.candles`.
3. Otherwise return `resampled.candles.iloc[:-1]` (the last slot is withheld).

`hours[-2]` always exists at step 2: reaching it means `last` has a row, so at least one of its hourly slots has a bar, while `closing_bar_missing` says the last one has none. A guard on `len(hours)` would be an unreachable branch, so there is none. A `4h` slot whose only hourly slot is the unpublished hour therefore never reaches step 2: with no bar it has no row, it is absent from `slots`, and step 1 returns the frame unchanged. A test documents that case.

On the hand frame:

| `now` | Last closed `4h` slot | Its closing hour | Result |
|-------|-----------------------|------------------|--------|
| `2024-11-27T18:30:30Z` | `2024-11-27T14:30Z` | `17:30Z` present | unchanged (1 row) |
| `2024-11-29T18:00:30Z` | `2024-11-29T14:30Z` | `17:30Z` missing, an unpublished hour; `16:30Z` present | unchanged (3 rows) |
| the same, on the hand frame without `2024-11-29T16:30Z` | `2024-11-29T14:30Z` | `17:30Z` and `16:30Z` missing | withheld (2 rows) |
| `2024-12-02T21:00:30Z` | `2024-12-02T18:30Z` | `20:30Z` missing, regular close | withheld (4 rows) |
| `2024-12-03T18:30:30Z` | `2024-12-03T14:30Z` | no hourly row in the slot, so not in `slots` | unchanged (5 rows) |

#### 10.5 `4h` logs (AC23)

On the provider logger, for each `4h` fetch after step 7.5, in this order:

1. If the last slot was withheld: `DEBUG` `withheld %s %s candle %s: hourly bar %s is missing`, with the symbol, `4h`, the ISO label of `last` and the ISO label of its closing hour.
2. For each returned row, the **relevant missing hours** are its `missing` labels minus unpublished hours (`is_unpublished_hour`). The **final row** is the last returned row when its slot is `last`; all other returned rows are **earlier rows**.
3. If some earlier rows have relevant missing hours: one `DEBUG` record, `built %d earlier %s %s candles from incomplete hourly bars (first %s, last %s)`, with their count, the symbol, `4h`, and the first and last such labels.
4. If the final row has relevant missing hours: one `WARNING` record (D34), `built %s %s candle %s from %d of %d hourly bars (missing %s)`, with the symbol, `4h`, its label, `bars`, the number of hourly slots of the slot, and the relevant missing labels in ISO 8601 joined by `, `.

The provider emits no other record on its logger except the capped-history records of §10.6. Records on the hand frame, symbol `SPY`, `lookback=10` (verified on the prototype):

| `now` | Records, in order | `fetch_candles` result |
|-------|-------------------|------------------------|
| `2024-11-27T18:30:30Z` | `WARNING built SPY 4h candle 2024-11-27T14:30:00+00:00 from 3 of 4 hourly bars (missing 2024-11-27T16:30:00+00:00)` | ends at `2024-11-27T14:30Z` |
| `2024-11-29T18:00:30Z` | `DEBUG built 1 earlier SPY 4h candles from incomplete hourly bars (first 2024-11-27T14:30:00+00:00, last 2024-11-27T14:30:00+00:00)` | ends at `2024-11-29T14:30Z` |
| `2024-12-02T21:00:30Z` | `DEBUG withheld SPY 4h candle 2024-12-02T18:30:00+00:00: hourly bar 2024-12-02T20:30:00+00:00 is missing`; `DEBUG built 1 earlier SPY 4h candles from incomplete hourly bars (first 2024-11-27T14:30:00+00:00, last 2024-11-27T14:30:00+00:00)` | `CandleNotPublishedError`: `missing`, expected `2024-12-02T18:30Z`, last `2024-12-02T14:30Z` |
| `2024-12-03T18:30:30Z` | `DEBUG built 2 earlier SPY 4h candles from incomplete hourly bars (first 2024-11-27T14:30:00+00:00, last 2024-12-02T18:30:00+00:00)` | `CandleNotPublishedError`: `missing`, expected `2024-12-03T14:30Z`, last `2024-12-02T18:30Z` |

The half-day row never appears among the incomplete candles, because its only missing hour is unpublished. The hand frame has no bad rows, so the pipeline logger emits no `WARNING`.

#### 10.6 Capped history (AC24)

Records on the provider logger, for fetches on one provider with the NYSE calendar and a client serving any valid frame:

| Fetch | Record |
|-------|--------|
| `SPY`, `1h`, `lookback=5000`, `now=2026-09-17T15:00:00Z` | `INFO Yahoo history for SPY 1h capped: requested from 2023-11-03T19:30:00+00:00, available from 2024-09-27T15:30:00+00:00` |
| the same fetch again | the same message at `DEBUG` |
| `SPY`, `4h`, `lookback=981`, same `now` | `INFO Yahoo history for SPY 4h capped: requested from 2024-09-27T13:30:00+00:00, available from 2024-09-27T17:30:00+00:00` |
| `QQQ`, `1h`, `lookback=5000`, same `now` | `INFO` (the first capped fetch for `QQQ 1h`) |
| `SPY`, `1h`, `lookback=3435`, same `now` | none |
| a new provider, `SPY`, `1h`, `lookback=5000` | `INFO` again |

The record is emitted before the client call, and also when the fetch then fails.

#### 10.7 `validate_ticker` (AC25)

1. `symbol = check_yahoo_symbol(parse_ticker(ticker))`; `parse_ticker` raises `TypeError` for a non-`str`.
2. `history = await transport.call(lambda: client.history(HistoryQuery(symbol=symbol, interval="1d", period="1mo")), ticker=symbol, timeframe=None)`. A `NoDataError` becomes `InvalidTickerError(not_found, ticker=symbol)` raised `from None`.
3. Return `ensure_supported(ticker_info(symbol, history.metadata))`. The frame is not used.

With `RecordedYahooClient` serving the recording `metadata`, and a transport with a fake sleep and `jitter=0.5`:

| Input | Client | Result |
|-------|--------|--------|
| `" spy "` | recording | `TickerInfo(symbol="SPY", asset_type=etf, exchange=ARCX, currency="USD", name=<the recorded longName>)`; one query, `HistoryQuery(symbol="SPY", interval="1d", period="1mo")` |
| `AAPL`, `QQQ`, `BRK-B`, `UEC`, `ARKB` | recording | supported, with the fields of §7.3 |
| `^GSPC`, `VFIAX`, `BTC-USD`, `EURUSD=X`, `ES=F` | recording | `InvalidTickerError(unsupported_asset_type)` |
| `RELIANCE.NS`, `TCEHY`, `NSRGY`, `SHOP.TO` | recording | `InvalidTickerError(unsupported_exchange)` |
| `AAPL` with `exchangeName` `NYQ` and `currency` `GBp` | synthetic | `InvalidTickerError(unsupported_currency)` |
| `US0378331005` | not called | `InvalidTickerError(not_found)` |
| `M&M.NS` | not called | `InvalidTickerError(malformed)` |
| `AAPL\|X`, `""` | not called | `InvalidTickerError(malformed)` (from `parse_ticker`) |
| `42` | not called | `TypeError` |
| `ZZZZNOTREAL` | raises `InvalidTickerError(not_found)` | that error |
| `SPY` | raises `NoDataError` | `InvalidTickerError(not_found)`, `from None` |
| `SPY` | raises `ProviderUnavailableError(connection)` on every call | that error, after 3 calls and sleeps `[1.5, 3.0]` |
| `SPY` | metadata `symbol` `SPYX` | `ProviderDataError("symbol_mismatch")` |

### 11. `data/yahoo/factory.py`

```python
from pathlib import Path

from trading_bot.data.transport import RateLimit, RetryPolicy
from trading_bot.data.yahoo.provider import YFinanceProvider
from trading_bot.domain.market_calendar.sessions import MarketCalendar

__all__ = ["build_yfinance_provider"]


def build_yfinance_provider(
    *,
    calendar: MarketCalendar,
    cache_dir: Path,
    policy: RetryPolicy | None = None,  # default RetryPolicy()
    rate_limit: RateLimit | None = None,  # default RateLimit()
) -> YFinanceProvider: ...
```

It builds, with no I/O other than creating `cache_dir`:

- `YFinanceClient(cache_dir=cache_dir)`;
- `TokenBucket(rate_limit)` with the default clock and `asyncio.sleep`;
- `ProviderTransport(policy=…, limiter=…)` with the default sleep and jitter;
- `YFinanceProvider(client=…, transport=…, calendar=calendar)`.

AC27 checks it without network: with `configure_yfinance` monkeypatched to a recorder and `yfinance.Ticker` replaced by the fake of T5 serving the `SPY` metadata, `validate_ticker("SPY")` on the built provider returns `SPY`'s info, `configure_yfinance` was called once with `cache_dir`, and nothing slept.

**Wiring waits for #16 (D63).** Recommended lifespan code, not part of this feature:

```python
calendar = build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)
cache_dir = Path(tempfile.mkdtemp(prefix="trading-bot-yfinance-"))
provider = build_yfinance_provider(calendar=calendar, cache_dir=cache_dir)
...  # on shutdown:
await provider.aclose()
shutil.rmtree(cache_dir, ignore_errors=True)
```

The container's `/tmp` is on its writable layer. If the service ever runs with a read-only root file system, #16 must mount a `tmpfs` at `/tmp`.

The provider is built inside the running event loop and used only from it (D68), and one instance per process keeps the pacing and the in-flight slot meaningful (D55).

### 12. `data/pipeline.py` amendment (AC28, D64)

```python
def log_dropped_rows(
    dropped: Sequence[DroppedRow],
    *,
    ticker: str,
    timeframe: Timeframe,
    last_label: datetime,  # the label of the last slot closed at now
    logger: logging.Logger | None = None,  # default: logging.getLogger("trading_bot.data.pipeline")
) -> None: ...
```

- It emits exactly the records of spec 010 §8.4 for `dropped`, with `last_label` in the role of the last closed slot's label. It raises `TypeError` for a non-`Timeframe`, and `last_label` goes through `to_utc`.
- It replaces #8's private `_log_dropped(dropped, expected, request, logger)`, taking `ticker` and `timeframe` instead of the request. `prepare_candles` calls it with `request.ticker`, `request.timeframe`, the label of the last closed slot and its own logger, so the spec 010 AC15 records do not change.
- `__all__` becomes `["CandleWindow", "candle_window", "log_dropped_rows", "prepare_candles"]`.

### 13. Fixtures, recording and the network guard

#### 13.1 Where and why

- Recordings live in `tests/fixtures/yahoo/`: never under a directory named `data/`, which `.gitignore` ignores (§1), and never in the image (`.dockerignore` excludes `tests`).
- They replay at the `YahooClient` port (D59). The client itself is tested with a fake `yfinance.Ticker` (T5, T6), so every test layer runs without network, and the guard of §13.6 proves it.

#### 13.2 Recordings (AC29)

Recorded by the developer with the script of §13.4. Before any outcome check, a test asserts the structural facts listed here. If Yahoo's answer has changed since 2026-09-17 (for example, the 2026-01-30 outage was backfilled), the developer reports it to the tech-lead instead of editing a file.

| File | Call (`seed`) | Structural facts |
|------|---------------|------------------|
| `spy_1h_2025-11-24.json` | `SPY`, `1h`, `2025-11-24T14:00:00Z` → `2025-12-03T00:00:00Z` (11) | 38 rows; index `datetime64[s, America/New_York]` named `Datetime`; 9 columns, `Capital Gains` last; no row on 2025-11-27; on 2025-11-28 only the 09:30, 10:30 and 11:30 ET rows |
| `spy_1d_2025-11-24.json` | `SPY`, `1d`, `2025-11-24T05:00:00Z` → `2025-12-03T00:00:00Z` (12) | 6 rows; index named `Date`, labels at 00:00 ET; no 2025-11-27 row |
| `aapl_1h_2026-01-28.json` | `AAPL`, `1h`, `2026-01-28T14:00:00Z` → `2026-02-05T00:00:00Z` (13) | 33 rows; 8 columns, no `Capital Gains`; on 2026-01-30 only the 09:30 and 10:30 ET rows; on 2026-02-02 no row before 13:30 ET |
| `spy_1h_2025-03-06.json` | `SPY`, `1h`, `2025-03-06T14:00:00Z` → `2025-03-12T00:00:00Z` (14) | 28 rows; 2025-03-07 labels `14:30Z` … `20:30Z` (EST) and 2025-03-10 labels `13:30Z` … `19:30Z` (EDT) |
| `nvda_1d_2024-06-03.json` | `NVDA`, `1d`, `2024-06-03T04:00:00Z` → `2024-06-15T00:00:00Z` (15) | 10 rows; `Stock Splits` 10.0 on 2024-06-10 only; `Dividends` non-zero on 2024-06-11 only |
| `metadata.json` | the chart metadata of the 15 symbols of §7.3, from `history(period="1mo", interval="1d")` | exactly the allowlisted keys per symbol |

#### 13.3 Format

```json
{
  "format": "yfinance-history/1",
  "recorded_with": {"yfinance": "1.7.0"},
  "call": {
    "symbol": "SPY",
    "interval": "1h",
    "start": "2025-11-24T14:00:00+00:00",
    "end": "2025-12-03T00:00:00+00:00"
  },
  "frame": {
    "index": {"name": "Datetime", "timezone": "America/New_York", "unit": "s", "epoch_seconds": [...]},
    "columns": ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits", "Capital Gains"],
    "dtypes": ["float64", "float64", "float64", "float64", "float64", "int64", "float64", "float64", "float64"],
    "rows": [[...], ...]
  },
  "synthetic": {"generator": "random-walk/1", "seed": 11}
}
```

- **Metadata file:** `{"format": "yfinance-metadata/1", "recorded_with": {...}, "symbols": {"SPY": {...}, ...}}`, whose per-symbol allowlist is `symbol`, `instrumentType`, `exchangeName`, `fullExchangeName`, `currency`, `exchangeTimezoneName`, `longName` and `shortName` (a `null` is kept). Every other key is dropped, in particular every price, volume, change, 52-week and trading-period field.
- **Rebuilding a frame:** the index is `pd.to_datetime(epoch_seconds, unit="s", utc=True)`, converted to `timezone`, cast to `unit` and named `name`; each column gets its recorded dtype, in the recorded order. A NaN is stored as `null`. Verified on the prototype: the rebuilt index dtype and name, the column order and the dtypes equal yfinance's frame.

#### 13.4 Synthetic values and `scripts/record_yahoo_fixture.py` (AC29, AC30)

**Generator `random-walk/1`.** `rng = random.Random(seed)` and `previous = 100.0`. For each row, in order:

```text
open   = previous
close  = round(open * (1 + (rng.random() - 0.5) * 0.004), 4)
high   = round(max(open, close) * (1 + rng.random() * 0.002), 4)
low    = round(min(open, close) * (1 - rng.random() * 0.002), 4)
volume = 1000 + int(rng.random() * 99000)
previous = close
```

| Column | Value |
|--------|-------|
| `Open`, `High`, `Low`, `Close` | `open`, `high`, `low`, `close` |
| `Adj Close` | `round(close * 0.97, 4)`, always different from `Close`, so a test catches the wrong column (D30) |
| `Volume` | `volume` |
| `Dividends`, `Capital Gains` | `0.25` where the recorded value is non-zero, otherwise `0.0` |
| `Stock Splits` | the recorded ratio (a public corporate action, not a price) |
| a cell that was NaN | NaN |
| any other column | the script fails |

Only `Random.random()` is used, the one stream Python guarantees across versions, so T12 can regenerate every value. Rounding is monotonic, so `low <= min(open, close) <= max(open, close) <= high` holds.

**The script** (manual, needs network, never run by tests or CI):

```bash
uv run python scripts/record_yahoo_fixture.py history --symbol SPY --interval 1h \
  --start 2025-11-24T14:00:00Z --end 2025-12-03T00:00:00Z --seed 11 \
  --output tests/fixtures/yahoo/spy_1h_2025-11-24.json
uv run python scripts/record_yahoo_fixture.py metadata --symbols AAPL QQQ BRK-B SPY UEC ARKB \
  ^GSPC VFIAX BTC-USD EURUSD=X ES=F RELIANCE.NS TCEHY NSRGY SHOP.TO \
  --output tests/fixtures/yahoo/metadata.json
```

- It refuses, with exit status 2 and before any request, an `--output` that does not resolve inside `tests/fixtures/yahoo/` or does not end in `.json`.
- It uses a `tempfile.TemporaryDirectory()` as the cache directory through `configure_yfinance`.
- It calls yfinance directly with `end`: the response cache of D48 does not matter in a one-shot process.
- It writes UTF-8 JSON with `indent=1`, `ensure_ascii=False` and a trailing newline, and prints only the output path and the row count, never values.
- **Pure functions, tested by T13 without network:**
  - `synthetic_rows(columns, dtypes, recorded_rows, seed)`;
  - `history_document(frame, *, symbol, interval, start, end, seed, yfinance_version) -> dict`;
  - `metadata_document(metadata_by_symbol, *, yfinance_version) -> dict`;
  - `output_path(text) -> Path`.
- Network access lives only in `main()`.

#### 13.5 `tests/fixtures/yahoo_recordings.py` (AC31)

```python
RECORDINGS_DIR: Final = Path(__file__).parent / "yahoo"


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Recording:
    name: str
    symbol: str
    interval: YahooInterval
    frame: pd.DataFrame  # rebuilt exactly as §13.3
    seed: int


def load_recording(name: str) -> Recording: ...
def load_metadata() -> Mapping[str, Mapping[str, object]]: ...
# the canonical rows of §5.3; provider_shaped(hand_frame()) is the client frame
def hand_frame() -> pd.DataFrame: ...


class RecordedYahooClient:
    """YahooClient replaying recordings; records queries; scripted failures first-in, first-out."""

    def __init__(
        self,
        *,
        histories: Mapping[tuple[str, YahooInterval], pd.DataFrame] | None = None,
        metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None: ...

    @property
    def queries(self) -> tuple[HistoryQuery, ...]: ...

    def fail_next(self, error: BaseException, *, times: int = 1) -> None: ...

    def history(self, query: HistoryQuery) -> YahooHistory: ...


def as_yahoo_client(client: RecordedYahooClient) -> YahooClient:
    """Returns its argument; strict mypy checks conformance to the port."""
```

`history(query)`:

1. Record the query.
2. Pop and raise the first scripted failure, if any.
3. When neither a frame for `(symbol, interval)` nor metadata for `symbol` exists, raise `InvalidTickerError(not_found)`, as the real client does.
4. Otherwise return a copy of the frame (for metadata-only symbols, an empty frame shaped like `spy_1d_2025-11-24`), with `ChartMetadata.from_mapping(metadata.get(symbol, {"symbol": symbol, "currency": "USD"}))`.

The module imports no yfinance and reads no clock. `provider_shaped` (spec 010 §10.1) builds the yfinance-like frame of the hand frame.

#### 13.6 Network guard (AC32, D60)

`tests/fixtures/network_guard.py`:

```python
class NetworkAccessError(BaseException):
    """A test tried to reach the network. Not an Exception, so no `except Exception` can hide it."""


LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> None: ...
```

It blocks outbound connections only. It never patches socket creation, `bind`, `listen`, `accept` or `socket.socketpair()`, which `asyncio.run` uses for its self-pipe on every platform. It patches:

- `socket.socket.connect` and `socket.socket.connect_ex`: allowed for `AF_UNIX` sockets (where that family exists) and addresses whose host is in `LOOPBACK_HOSTS`; otherwise `NetworkAccessError`.
- `socket.getaddrinfo`: allowed for a `None` host and `LOOPBACK_HOSTS`; otherwise `NetworkAccessError`. This also covers `socket.create_connection`, `urllib`, `requests` and `websockets`.
- `curl_cffi.requests.Session.request` and `curl_cffi.requests.AsyncSession.request`: always `NetworkAccessError`. Every verb of both classes goes through `request`. The patch is skipped only when `curl_cffi` cannot be imported, in which case yfinance cannot use it either.

`tests/conftest.py` gains an autouse fixture that calls `install_network_guard(monkeypatch)` for every test of the suite.

Self-tests (T14):

- `socket.getaddrinfo("example.invalid", 443)` raises;
- `socket.create_connection(("192.0.2.1", 443), timeout=1)` raises before any packet is sent;
- `urllib.request.urlopen("http://example.invalid/", timeout=1)` raises `NetworkAccessError`, not a `URLError`;
- a loopback server (`socket.create_server(("127.0.0.1", 0))`) accepts a loopback connection;
- `asyncio.run(asyncio.sleep(0))` works, and a `socket.socketpair()` exchanges a byte (native on Linux; a loopback listener and `connect` on Windows);
- `curl_cffi.requests.Session().get("https://example.invalid/")` raises through a function that catches `Exception`;
- `YFinanceClient(cache_dir=tmp_path).history(HistoryQuery(symbol="SPY", interval="1d", period="1mo"))` raises `NetworkAccessError`: a test that forgets the fake fails loudly. The test restores yfinance's configuration afterwards.

Evidence: the tech-lead ran the whole current suite (#9 committed plus #8 as implemented: 4 103 passed, 10 skipped) on Windows with this guard loaded as a plugin, from a scratch copy. Everything passed, including #8's `asyncio.run` tests, `test_health` and `tests/deploy/`, and nothing was blocked. A probe in the same run confirmed that `getaddrinfo`, `create_connection` to `192.0.2.1` and `urlopen` raise `NetworkAccessError`, while `asyncio.run`, `socketpair()` and a loopback server work. CI repeats this on Linux.

### 14. Typing, purity and isolation

- **mypy** (AC4, D61), in `pyproject.toml`:

  ```toml
  [tool.mypy]
  # existing settings unchanged, plus:
  untyped_calls_exclude = ["yfinance"]

  [[tool.mypy.overrides]]
  module = ["yfinance", "yfinance.*"]
  follow_untyped_imports = true
  ```

- **Extra-flag command** (spec 010 §11, unchanged paths; `src/trading_bot/data` now includes `transport.py` and `yahoo/`):

  ```bash
  uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated \
    src/trading_bot/domain/*.py src/trading_bot/domain/indicators \
    src/trading_bot/domain/market_calendar src/trading_bot/data
  ```

  The tech-lead ran it with strict mypy on a prototype of §5–§12 (the dataclasses, the `YahooClient` Protocol, the generic `call[T]`, the shielded worker task, `yfinance.Ticker` and its exception classes): green. With `ignore_missing_imports` instead, it also passes but every yfinance name is `Any`; with neither setting it fails with `import-untyped`; with `follow_untyped_imports` alone it fails with `no-untyped-call` on `Ticker(...)`.
- **Purity guard** (T16): add `candle_resampling.py` to the expected scanned set, with no per-file allowance. It does not join the lightweight fresh-interpreter list, because it imports pandas.
- **Isolation** (T15, AC33):
  1. An AST scan of `src/trading_bot/**/*.py`: `yfinance`, or any of its submodules, is imported only by `data/yahoo/client.py`, and no module imports `curl_cffi`, `requests`, `urllib3`, `peewee`, `httpx`, `aiohttp`, `websockets`, `socket`, `http.client` or `urllib.request`. Scanner self-tests cover `import yfinance`, `from yfinance.exceptions import X` and `import curl_cffi.requests` in another file.
  2. In a fresh interpreter (the spec 009 AC14 pattern), importing each of `trading_bot.data.yahoo.provider`, `trading_bot.data.yahoo.instruments`, `trading_bot.data.yahoo.history`, `trading_bot.data.transport` and `trading_bot.domain.candle_resampling` leaves `yfinance`, `curl_cffi`, `peewee` and `requests` out of `sys.modules`. Importing `trading_bot.data.yahoo.factory` loads `yfinance` (control).
  3. `tests/fixtures/yahoo_recordings.py` does not import yfinance.

### 15. `Dockerfile` (AC3, D62)

In the runtime stage, right after the data-layer smoke check that spec 010 adds after the NYSE calendar check (`import trading_bot.data.errors, trading_bot.data.pipeline, trading_bot.data.provider, trading_bot.data.tickers`), and before `USER app`:

```dockerfile
# Fail the build if yfinance or its browser-impersonating HTTP backend cannot load on this platform (no network is used).
RUN ["python", "-c", "from curl_cffi import requests; requests.Session(impersonate='chrome').close(); import yfinance.exceptions, trading_bot.data.yahoo.factory"]
```

- Importing creates no file (§3.7), and the factory is not called.
- It cannot run locally (the Docker daemon is off). V7 runs the same command with a runtime-only environment.
- The PR's `Docker build (arm64)` job is the authoritative check.

### 16. Forward compatibility

#### 16.1 Spec 010 hand-off list

| Item | Addressed in |
|------|--------------|
| 1. Single-ticker `Ticker.history(auto_adjust=False, prepost=False)` | D48, §8.2 |
| 2. Metadata → `TickerInfo` (OTC and unknown → `OTHER`), then `ensure_supported` | D51, §7.2, §7.3, §10.7 |
| 3. Plan with `candle_window`, clamp to Yahoo's limits, log when capped | D56, §10.1, §10.6 |
| 4. `4h` from normalized `1h`, labelled by slot; last slot needs its closing bar; D34 for earlier slots | D57, D58, D65, §5, §10.2, §10.4, §10.5 |
| 5. Retries only for `ProviderUnavailableError`; backoff, jitter, timeout, `retry_after`, pacing; `asyncio.to_thread` | D54, D55, §9 |
| 6. Typed errors `from None`; empty responses and "symbol may be delisted" | D53, §8.3 |
| 7. Synthetic-value recordings; `assert_closed_candles` on every result; inconsistent-OHLC frequency measured offline | D59, §13, §10.3, §3.5 |
| 8. Always finish with `prepare_candles` | §10.2 |

#### 16.2 Later issues

| Issue | How it uses #10 |
|-------|-----------------|
| #14 | Types the provider as `MarketDataProvider` and passes `lookback = max(rule.stable_warmup())`. A `4h` frame may lack whole candles for which Yahoo published no hourly bar (an outage); cooldown and crossovers count by row position, as for any frame. |
| #15 | Expects `CandleNotPublishedError(missing)` for the truncated final `1h` slot of every early-close session, which Yahoo never publishes (§3.4). `is_unpublished_hour(slot, calendar=…)` identifies it, so #15 can skip it instead of retrying until the next close. Retries reuse the scheduled `now`, which the 10-day margin of D56 covers. `TB_CANDLE_CLOSE_DELAY_SECONDS` also limits yfinance's merge of a live trade into the bar that just closed (Risks). |
| #16 | Calls `build_yfinance_provider` once, inside the running event loop and for that loop only (D68), with a per-process temporary cache directory (§11), and at shutdown awaits `provider.aclose()` and removes the directory. `fetch_candles` keeps the loop free while it downloads and while it processes (D67), so the Telegram poller and the scheduler keep running. |
| #17 | May copy the `ProviderTransport` pattern (retries, jitter, pacing, worker threads) for Telegram, with its own error types. |
| #19, #22, #23 | Map `InvalidTickerError.reason`: `malformed` also covers symbols outside Yahoo's alphabet; `not_found` covers ISIN-shaped input ("use the ticker symbol"). `ProviderDataError.kind` `symbol_mismatch` and `invalid_metadata` mean inconsistent provider data. `TickerInfo.name` may contain non-ASCII letters and must be escaped. |
| #26 | Counts `ProviderUnavailableError` by `failure`, capped-history records, gap warnings and withheld `4h` candles per ticker. |
| Dependabot | A `yfinance` bump runs the canary (§8.4) in CI. Behavior changes are visible only on beta: after such a bump, check a live fetch there, and re-record the fixtures when the frame shape changes. |

### 17. `docs/ARCHITECTURE.md` and docstrings (AC34)

- **Layers table:**
  - `domain/` row: add "`4h` candles resampled from hourly candles (`resample_hourly_to_4h`)".
  - `data/` row: mention `YFinanceProvider` in `data/yahoo/`, `4h` built from `1h` bars, and `ProviderTransport` (retries with backoff and jitter, timeouts, pacing). Replace "Respects Yahoo's limits (intraday max. 60 days; 1h up to 730 days)" with "Respects Yahoo's limits: `1h` requests start at most 720 days back; `1d` is unlimited".
- **`## Market data`** (added by #8) gains a `### Yahoo provider` subsection, as concise as the points allow:
  1. the layout and yfinance's isolation (D49), with a short example of `build_yfinance_provider` and `fetch_candles`, noting that wiring waits for #16;
  2. what is requested per timeframe, the start planning and the 720-day cap (D48, D56);
  3. `4h` resampling: aggregation, gaps (D34), the last-slot rule and the half-day hole (D57, D58, D65), with the §5.3 rows of 2024-11-27 and 2024-11-29, and that the processing runs off the event loop (D67);
  4. the transport defaults (D54, D55);
  5. a summary of the error mapping (D53);
  6. yfinance's global state, caches and logging (D50);
  7. recordings, the recording script and the network guard (D59, D60);
  8. a link to this spec.
- **`## Look-ahead testing` → `### Fixtures`:** replace the bullet "Recorded real-data fixtures arrive with #10 …" with a pointer to `tests/fixtures/yahoo/` and the synthetic-values rule, and add that every test runs under the network guard.
- **`### Warmup and reproducibility`:** update the history-limits bullet to the 720-day cap: about 3 435 `1h` bars and 980 `4h` bars. `stable_warmup` exceeds 980 from `ema` `length=218`, `adx` `length=82`, and `macd` `slow=200` with `signal >= 17`; `rsi` and `atr` never exceed it within their parameter ranges. The current "`rsi` and `atr` `length=125`" lies outside those ranges. These values were computed with `REGISTRY`, and the developer recomputes them the same way.
- **`## Decisions`:** a bullet "yfinance behind one adapter module", with the reasons of D47, D49 and D50 and the half-day evidence.
- **Docstrings:** `src/trading_bot/domain/__init__.py` says that `candle_resampling` builds session-anchored `4h` candles from hourly candles (spec 011). `src/trading_bot/data/__init__.py` mentions `transport` and the `yahoo` subpackage. `src/trading_bot/data/yahoo/__init__.py` describes the modules and that only `client.py` imports yfinance.
- The configuration table does not change (no `TB_*`).

## Test plan

All tests are unit tests. They run under the network guard, never read the wall clock and never sleep for real. The transport gets a fake `sleep`, a fixed `jitter` and a fake clock; the only real waits are thread hand-offs gated by a `threading.Event`. Calendars come from `nyse_test_calendar()`, and coroutines run with `asyncio.run` (spec 010 D46). The mandatory cases of the template:

- **Anti look-ahead (rule 4):** `resample_hourly_to_4h` through the harness with completion stamps on two frames, full sweeps, two controls that must fail, and the `now` sweep (T3, AC7); a Hypothesis property over drawn gapped grids and instants (T18). The tech-lead ran T3's checks on the prototype: green, and both controls caught.
- **Idempotency (rule 5):** signal keys come from labels. T10 pins that every returned label is a canonical slot label (`assert_closed_candles`), that `4h` labels are slot labels whatever hourly bars are missing, and that the same recording always gives the same frame. Deduplication itself belongs to #13 and #14.
- **Authorization:** not applicable (no Telegram, API or dashboard code).
- **Secret redaction:**
  - T6 builds every exception with a crumb-like URL assembled at runtime and asserts that no URL, host or crumb reaches `str()`, `repr()` or log records, and that errors are raised `from None`;
  - T5 checks that `configure_yfinance` sets the `yfinance` logger to `WARNING`, which keeps the crumb and request URLs out of `DEBUG` logs;
  - T12 and T13 check that recordings keep no price or volume, and no metadata key outside the allowlist.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 resampling contract | unit | §5.1–§5.2: argument order and errors; canonical output and dtypes; the slot report; input unchanged; empty result; an off-grid considered hourly label raises `CandleLabelError`, while an off-grid label at or after `last.close_time` is ignored | AC5 | developer |
| T2 hand case | unit | §5.3 literally, every `now` | AC6 | developer |
| T3 anti look-ahead | unit | §5.4: harness on both frames with `max_cuts=len(frame)` and non-vacuous reports; both controls raise `LookaheadError` with the stated kinds; the `now` sweep | AC7 | developer |
| T4 Yahoo types and instruments | unit | §6 validation order; `from_mapping` with a mapping whose iteration and `tradingPeriods` raise; §7.1, §7.2 and §7.3 literally | AC9–AC11 | developer |
| T5 client configuration and call | unit | A fake `yfinance.Ticker` that records calls. §8.1 steps and idempotence; both §8.2 call shapes, exact keyword arguments, no `end`; `invalid_response` for wrong return types; yfinance globals restored | AC12, AC13 | developer |
| T6 error mapping and canary | unit | Both §8.3 tables; `from None`; no leak in `str`, `repr` or logs; the `ERROR` record literally; `BaseException` propagation; §8.4 | AC14, AC15 | developer |
| T7 policies and bucket | unit | §9.1 validation; §9.2 table; §9.5 sequences | AC16, AC19 | developer |
| T8 transport | unit | §9.3 and §9.4: retry decisions, sleeps and the `INFO` record literally; other errors propagate without sleep; the timeout with an `Event`-blocked operation; slot ordering between two calls; late outcomes discarded without logging; cancellation; `aclose` and the closed transport | AC17, AC18 | developer |
| T9 planning | unit | §10.1 literally, including `CalendarRangeError` | AC20 | developer |
| T10 provider flow and recordings | unit | §10.2 order (errors before any client call, one call per attempt, the exact query), the post-fetch processing off the main thread with a spy, a processing error reaching the caller after a single client call, §10.3 literally after the §13.2 preconditions, `assert_closed_candles` on every frame, `NVDA` `close` against `Close` and `Adj Close`, §10.6 records | AC21, AC22, AC24 | developer |
| T11 publication, validation, error surface, factory | unit | §10.4 tables, including a `4h` slot whose only hourly slot is the unpublished hour (absent from `slots`, frame unchanged); §10.5 records through the provider on `provider_shaped(hand_frame())`; dropped hourly rows logged with `1h`; §10.7 literally; the AC26 matrix (each §8.3 exception, through the fake `Ticker`, the real client and transport); the Protocol conformance function; the factory without network; `aclose` | AC23, AC25–AC27 | developer |
| T12 recordings | unit | The six files; §13.3 format; the metadata allowlist; every generated value equals `random-walk/1` for the file's seed; §13.2 structural facts | AC29 | developer |
| T13 recording script | unit (`tests/scripts`) | Pure functions only: sentinel prices and volumes never survive; index, names, column order and dtypes kept; `Adj Close` differs from `Close`; an unknown column is refused; outputs outside `tests/fixtures/yahoo/` are refused; the metadata allowlist | AC30 | developer |
| T14 network guard | unit | §13.6 self-tests, including `asyncio.run` and `socketpair()` under the guard; the whole suite passes with the guard active (V1) | AC32 | developer |
| T15 isolation | unit | §14 AST scan with scanner self-tests; fresh-interpreter checks and control | AC33 | developer |
| T16 purity guard | unit | `candle_resampling.py` in the expected set, with no allowance | AC8 | developer |
| T17 pipeline helper | unit | `log_dropped_rows` called directly reproduces the spec 010 §8.4 records; `prepare_candles`' records unchanged; `__all__` | AC28 | developer |
| T18 properties | unit (`@given`) | **Resampler**, over drawn gapped NYSE hourly grids in 2024 and `now` at microsecond resolution: each row equals the aggregation of its slot's rows; only closed slots with rows appear; rows are final as `now` grows; no row depends on data published after `now`. **Transport**, over drawn failure scripts, jitters and policies: sleeps equal `delay`, calls never exceed `max_attempts`, and the same error instance is raised. **Bucket**, over drawn clock scripts: acquisitions within any window of `w` seconds never exceed `burst + rate · w`. **Planning**, over drawn `now` in 2024–2026 and lookbacks: an intraday `start` is never before `now − 720 days`, `start` is a slot label of the timeframe, and `capped` holds exactly when `requested_start < now − 720 days`. **Symbols**, over drawn printable ASCII: every accepted symbol builds a `HistoryQuery` | AC5, AC7, AC10, AC16–AC20 | tester |
| T19 adversarial | unit | Frames shaped like `yf.download` (MultiIndex columns → `ProviderDataError("multiindex_columns")`), a naive index, hourly rows at 10:00 ET and after hours on the `4h` path (dropped and logged as `1h`), duplicated hourly rows, an empty frame (`NoDataError`), a frame with only rows after `now`; metadata values of wrong types, 10 000-character names and bidirectional-override characters; `Retry-After` with spaces, a sign or huge values; symbols of 24 and 25 characters and ISIN look-alikes (11 and 13 characters, lowercase); an operation raising a `BaseException`, or cancelled mid-flight (the slot is held until the worker ends); `aclose` twice; 1 000 scripted failures with `max_attempts=10`; the guard with IPv6 loopback and `urllib` | AC9–AC14, AC17, AC18, AC21, AC25, AC32 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC35 |
| V2 coverage | `uv run pytest --cov=trading_bot --cov-branch --cov-report=term-missing`: 100% for every new `src/` module and the changed lines of `data/pipeline.py`; no regression elsewhere | AC35 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 15 s, and none takes more than 2 s. Reported, never asserted | AC35 |
| V4 typing | `uv run mypy`; the §14 extra-flag command; the spec 004 `git grep` for `Any` over the new `src/` modules, `tests/fixtures/{yahoo_recordings,network_guard}.py` and the new script, empty; `git grep -n "type: ignore"` on them, empty; `git diff <base> -- pyproject.toml` shows only the dependency line and the mypy settings | AC4 |
| V5 runtime dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch, compared with the same command in a temporary `git worktree` of `<base>` (in the scratch directory, removed afterwards): exactly §2.2 | AC1 |
| V6 arm64 wheels | The §2.3 command resolves 42 packages | AC2 |
| V7 runtime-only install | `UV_PROJECT_ENVIRONMENT=<scratch>/venv uv sync --locked --no-dev`, then that interpreter runs the §15 command and exits 0. Keep the scratch path short on Windows: `curl_cffi`'s native library fails to load from paths near the 260-character limit | AC1, AC3 |
| V8 clock and network greps | The spec 010 AC18 `git grep` over `src/trading_bot/data` is empty; `git grep -n -E "^\s*(import\|from) (yfinance\|curl_cffi\|requests\|urllib3\|peewee\|httpx\|aiohttp\|websockets\|socket)\b" src` lists only `data/yahoo/client.py`, for `yfinance` | AC33 |
| V9 docs | Placement and content against §17; `uv run ruff format --check` on the Python blocks | AC34 |
| V10 scope | `git diff <base> --name-only` plus `git status --porcelain` list only the §1 files, and `git check-ignore -v` on them prints nothing. `<base>` is the `feature/market-data-provider` commit this branch starts from; after #8 merges and the branch is rebased, compare with `origin/main` | AC36 |
| V11 secrets and language | `python scripts/secret_scan.py --history`; English-only review of the diff, including recordings (no URL, host or crumb; names are Yahoo's public instrument names) | — |
| V12 Docker | **BLOCKED locally** (no daemon), and this change **does** alter the image. V7 is the local substitute; the PR's `Docker build (arm64)` job runs §15, and the lead verifies the beta deploy and `/health`. Report it as BLOCKED with this justification, not as PASS | AC3 |

**Testing rules for this feature** (spec 007 §14, specs 009 and 010):

- no wall-clock or elapsed-time assertions, and no real sleeps;
- no platform-dependent expectations (Windows locally, Linux on CI);
- literal expectations are written out, never recomputed with the code under test;
- look-ahead checks compare exactly (`rtol = atol = 0`);
- no real market data (D29), and no recording is edited by hand;
- log assertions filter by `record.name` and compare `record.getMessage()` (or `caplog.messages`), never `record.msg` or `record.args`: once `create_app` has run in the process (as `tests/unit/test_health.py` does), the root handler's `RedactingFilter` rewrites `msg` and clears `args` of every later record in place. A non-propagating injected logger with its own handler is the alternative;
- tests that change yfinance globals restore them;
- each test that calls `asyncio.run` builds its own transport and provider (D68);
- nothing imports the network path of `scripts/record_yahoo_fixture.py`, and the network guard is never disabled.

**TDD order suggested to the developer:**

1. The dependency and the mypy settings (check the lock diff first), then T14 (the network guard) and T15 (the scanner and its self-tests);
2. T16 → T1 → T2 → T3 (resampling);
3. T17 (pipeline helper);
4. T4 (types and instruments);
5. T7 → T8 (transport);
6. T5 → T6 (client);
7. T13 (script functions) → record the six files → T12 → T9 → T10 → T11 (provider);
8. the `Dockerfile`, then the docs.

## Risks and security

- **Unofficial data source.** yfinance scrapes undocumented Yahoo endpoints, and Yahoo can change or block them at any time, and restricts redistribution of its data (D29). The user chose yfinance for v1. Mitigations: typed errors, yfinance isolated in one module so the source can be replaced, the canary test, and live checks on beta.
- **Secret leakage.** Yahoo's session crumb travels in query strings. yfinance puts request URLs into exception messages and logs the crumb at `DEBUG`, and the redaction filter only knows Telegram-shaped tokens and configured secrets. Mitigations: the `yfinance` logger at `WARNING` (D50), every exception mapped `from None` with messages built from safe fields (D53), and T6's leak assertions.
- **Pickled cookie cache.** yfinance unpickles `cookies.db` from its cache directory, and anyone who can write there can run code in the bot. Mitigation: a private directory created with mode `0o700`, recommended to be a per-process temporary directory, never the data volume or its backups (D50, §11).
- **Throttling by Yahoo.** Mitigations: pacing (D55), a single in-flight call, backoff with a floor after `rate_limited`, and browser impersonation through `curl_cffi`, guaranteed in the image by the smoke check (D62). A 429 during a scheduled run delays that ticker by at least 15 s.
- **Abandoned worker threads.** A timed-out attempt keeps its thread until yfinance's socket timeouts end it. Before the first chart request, a call may make up to three other requests, the cookie and the crumb with yfinance's fixed 30 s timeouts and the time zone with 10 s, so a worst-case worker lives about 85 s. The single in-flight slot keeps workers serialized, and #16 awaits `aclose()` at shutdown.
- **Live-row merge right after a close.** yfinance merges Yahoo's live row into the preceding bar when both fall within one hour. When no trade happened after the close, that can add the live row's volume to the bar that just closed. Mitigation: #15's close delay; this adds to spec 010's "non-final candles" risk.
- **Half days** (§3.4). The `1h` candle of 12:30 ET never exists, and the half-day `4h` candle closes without its last 30 minutes (D65). If Yahoo ever published that half hour late, the candle would change after being evaluated; signal keys would not, so nothing would be sent twice. Mitigation: `is_unpublished_hour` for #15, and the §3.4 evidence (the hour was still missing months later).
- **Revised history.** Yahoo can backfill or correct past bars (for example after an outage). Every run re-reads the history, so indicator values can shift; signal keys do not change, so nothing is sent twice (rule 5).
- **Symbol reuse.** Tickers are reassigned (`FB` now answers as an ETF). `validate_ticker` describes the current instrument; re-validation on reassignment belongs to #12 and #19.
- **Dependabot bumps of yfinance.** The replay tests cannot see behavior changes inside yfinance. Mitigations: the canary, the beta check and re-recording when the frame shape changes (§16.2).
- **Image size and import cost.** About 20 MB of compressed wheels (12.8 MB `curl-cffi`, 5.0 MB `lxml`). `import yfinance` loads `curl_cffi`, `lxml`, `bs4`, `protobuf` and `websockets`; nothing imports it until #16.
- **Local Windows path length.** `curl_cffi`'s native library fails to load from very long paths, and yfinance then silently falls back to `requests`. The project `.venv` paths are short; V7 notes it.
- **Sensitive data.** None: recordings hold synthetic values, public reference codes and names. There are no tokens, infrastructure hosts, IPs, users, `TB_*` variables, migrations or `secrets.env` changes. Test code uses `example.invalid` and the documentation address `192.0.2.1` only.
- **Unbreakable rules.** All preserved:
  - signal-only: market data only, no order concepts and no broker credentials;
  - pure `domain/`: T16 and the purity guard;
  - closed candles only: `4h` rows exist only for closed slots, and every fetch ends with `prepare_candles`, with anti look-ahead tests T3 and T18;
  - idempotency: canonical labels (T10);
  - UTC: every output is UTC, and `now` goes through `to_utc`;
  - single worker: untouched, and the transport serializes yfinance within the process;
  - no `eval`;
  - English only.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches; M2 stacks #9 → #8 → #10.
- **D2 (2026-09-14), refined by D29:** synthetic data only; no real Yahoo data is committed.
- **D27–D34 (2026-09-17)** are recorded in spec 009 "User decisions". This spec implements:
  - D27 through `ticker_info`'s mapping tables and `ensure_supported`;
  - D28 through `prepost=False` and spec 010's normalization;
  - D29 through the recordings, the generator and the script (§13);
  - D30 through `auto_adjust=False`, with `close` taken from `Close`;
  - D31 through `resample_hourly_to_4h` on the calendar's grid;
  - D32 through `prepare_candles` and `log_dropped_rows`, with the offline measurement of §3.5 showing that no repair is needed;
  - D33 and D44 by never retrying `CandleNotPublishedError`;
  - D34 through the resampler for earlier slots, with the warning of §10.5.
- **D65 (2026-09-17, user): the half-day `4h` candle.** Yahoo never publishes the last 30 minutes of a half day (12:30–13:00 ET) as hourly data (§3.4), so the `1h` candle of 12:30 ET never exists. The half-day `4h` candle (09:30–13:00 ET, D31) closes at 13:00 with the 09:30–12:30 data, as long as the 11:30 ET bar is present; any other missing closing bar is still withheld (hand-off 4). Rejected: withholding it like any other incomplete last candle, which would make #15 retry until the next `4h` close and would never send a signal on that candle. Implemented by `publishable_4h` step 2 (§10.4), with the outcomes of §10.3 and §10.5.

## Review checklist (tech-lead)

- [ ] D65: the half-day `4h` candle is published without its unpublished last hour, and every other missing closing bar is withheld (§10.4)
- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users; recordings hold no real price or volume and no URL or crumb
- [ ] Tests cover the acceptance criteria, and T1–T17 fail without the implementation
- [ ] Look-ahead harness on the resampler with both controls failing (T3)
- [ ] Runtime dependency diff exactly §2.2 (V5), arm64 resolution (V6), runtime-only smoke command (V7) and the image smoke check (AC3)
- [ ] yfinance imported only by `data/yahoo/client.py`; network guard active for the whole suite (T14, T15)
- [ ] No `Any` and no `type: ignore` in the new files; purity guard green (T16)
- [ ] `main.py` untouched; scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
