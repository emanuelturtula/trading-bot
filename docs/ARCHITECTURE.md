# Architecture

## Goal

For a list of configurable tickers (stocks and ETFs via yfinance in v1), the bot:

1. waits for each candle to close according to the ticker's timeframe (1h, 4h, 1d; default 1d);
2. downloads the OHLCV candles and discards the in-progress candle;
3. computes technical indicators;
4. evaluates the rules assigned to the ticker;
5. if a rule fires, persists the signal and notifies it via Telegram with a chart.

The user decides whether to trade. **There is no order execution layer** and one must not be added.

## Layers

```
            ┌──────────────┐    ┌──────────────┐
 Telegram ─▶│ telegram_bot │    │ api/dashboard│◀─ browser (auth)
            └──────┬───────┘    └──────┬───────┘
                   │  commands / CRUD  │
                   ▼                   ▼
            ┌─────────────────────────────────┐
            │ persistence (SQLite + Alembic)  │  tickers, rules, signals
            └───────────────┬─────────────────┘
                            │
 scheduler ──tick──▶ ┌──────┴───────┐   fetch   ┌──────────────┐
 (candle close)      │    engine    │──────────▶│    data      │──▶ yfinance
                     │ SignalEngine │           └──────────────┘
                     └──────┬───────┘
                            │ DataFrame (closed candles)
                            ▼
                     ┌──────────────┐
                     │   domain     │  PURE: indicators + rule evaluator
                     └──────┬───────┘
                            │ Signal[]
                            ▼
                     ┌──────────────┐
                     │notifications │──▶ Telegram (text + chart BytesIO)
                     └──────────────┘
```

| Layer | Responsibility | Rules |
|-------|----------------|-------|
| `domain/` | Models (`Timeframe`, the candle frame contract with `validate_candles`, `Signal`/`SignalKey`/`Side`), the market calendar (`MarketCalendar`: NYSE sessions, candle grid and real closes), candle normalization and open-candle removal (`normalize_candles`, `drop_open_candle`), the JSON rule model (`parse_rule`, `dump_rule`, `rule_json_schema`), indicator registry (whitelist → TA-Lib), rule evaluator | No I/O, no clock, no globals. Testable with fixed DataFrames. |
| `data/` | `MarketDataProvider` (async Protocol), `TickerInfo` and the D27 policy, typed errors, and `prepare_candles`, which every provider uses to normalize, drop bad rows and the open candle, and require the last closed candle; `YFinanceProvider` (#10) with retries and backoff | Never decides signals. Respects Yahoo's limits (intraday max. 60 days; 1h up to 730 days). |
| `engine/` | `SignalEngine`: orchestrates fetch → indicators → rules → dedupe/cooldown → persistence → notification | Idempotent by `(ticker, timeframe, rule_id, candle_close_ts)`. |
| `scheduler/` | APScheduler (AsyncIOScheduler). One job per timeframe, fired at candle close + margin, only during market hours | A single instance per process. |
| `notifications/` | `Notifier` (Protocol) + `TelegramNotifier` | `[BETA]` prefix outside prod; disclaimer; chart in memory (`io.BytesIO`, `seek(0)`), never to disk. |
| `telegram_bot/` | Commands `/add /remove /list /rules /status /pause /resume /help` with python-telegram-bot (long polling) | Only chats in `TB_TELEGRAM_ALLOWED_CHAT_IDS`. |
| `api/`, `dashboard/` | REST `/api/v1/tickers`, `/rules`, `/signals`; Jinja2 + HTMX dashboard (tickers, rule builder, history, charts) | Mandatory auth (password hash + signed session cookie). `/health` is the only public endpoint. |
| `persistence/` | SQLAlchemy 2 + Alembic on SQLite in `/app/data` | Versioned migrations; backups before each deploy. |

## Process

A single asyncio process (`uvicorn ... --workers 1`). The FastAPI lifespan starts and stops the scheduler and the Telegram poller. It is never scaled horizontally: a Telegram token allows a single poller (409 Conflict).

## Domain models

`domain/` is pure (`CLAUDE.md` rule 3). Import from its submodules: the package re-exports nothing, so consumers that only need `Signal` or `Timeframe` (Telegram, persistence, API) do not load pandas. Design and decisions: spec [004](specs/004-domain-models.md).

### Timeframes

`Timeframe` (`domain/timeframe.py`) is a `StrEnum` with the codes `1h`, `4h` and `1d`, the single spelling used in rule JSON, the API, Telegram, the CLI and the database. `Timeframe.parse(text)` only ignores surrounding whitespace: other letter cases (`1H`), aliases (`60m`, `daily`) and unsupported codes raise `UnknownTimeframeError` (a `ValueError`) whose message lists the valid codes. `duration` is the fixed candle length (`timedelta`).

### Candle frames

The candle model is a validated `pd.DataFrame`; there is no per-row class. `validate_candles(frame)` (`domain/candles.py`) returns the same frame, unmodified, or raises a `CandleValidationError` (`CandleIndexError`, `CandleColumnsError` or `CandleValuesError`, all `ValueError`) whose `kind`, `column`, `timestamp`, `position` and `count` locate the first failing check:

- the index is a `DatetimeIndex` of candle **open** times in UTC (`"UTC"`, `datetime.UTC` or `ZoneInfo("UTC")`; not `Etc/UTC` or other zero-offset zones), with no `NaT`, unique and strictly increasing, in any unit and with any name;
- the columns are exactly `open, high, low, close, volume`, all numpy `float64`;
- every value is finite, prices are `> 0`, `volume >= 0`, `high >= low`, and `open` and `close` are within `[low, high]`;
- empty frames are valid. Grid alignment, gaps and whether the last candle is closed are not checked.

Every check is row-local or compares adjacent labels, so every prefix of a valid frame is valid. The validator never coerces: `normalize_candles` converts provider frames to this contract (UTC `us` index, the five `float64` columns) and drops bad rows before validating, as described in [Market data](#market-data).

### Nominal candle close

`timeframe.nominal_close(open_time)` is `open_time + duration` in UTC, without a calendar. It is the `candle_close_ts` of a signal: an **identifier** derived deterministically from the provider label, not a market fact. A real close from a calendar would change whenever calendar data is corrected, and so would the keys of signals already stored.

For US equities (regular session 09:30–16:00 ET):

| Timeframe | Bar label (yfinance) | Real close | Nominal close |
|-----------|----------------------|------------|---------------|
| `1h` | 09:30, 10:30, …, 15:30 ET | open + 1h, except the last bar: 16:00 ET (13:00 ET on half days) | always open + 1h: 16:30 ET for the last bar |
| `4h` (#10 resample, session-aligned) | 09:30 and 13:30 ET | 13:30 and 16:00 ET | 13:30 and 17:30 ET |
| `1d` | 00:00 ET of the session date (05:00 or 04:00 UTC) | 16:00 ET the same day | 00:00 ET the **next** day |

Obligations for later work:

- **Closedness and scheduling (#8, #9, #15):** never use `nominal_close` to decide whether a candle is closed or when to run. Use the real session closes of #9; with nominal closes the daily candle would count as open until about 05:00 UTC the next day and the last hourly bar would be missed. The real closes come from the [market calendar](#market-calendar): `candle_slot` for a candle's close, `next_candle_close` for when to run and `closed_candles` for the candles closed at `now`.
- **Label convention (#8, #10): fixed.** Keys depend on provider labels, so the canonical labels of the [market calendar](#candle-grid-and-labels) (spec 009 D21: open times for `1h` and `4h`, 00:00 New York of the session date for `1d`) are enforced by `normalize_candles` (spec 010). Changing them later requires migrating the stored keys, otherwise signals already notified can be sent again.
- **Counting candles (#7, #14):** count by row position (`cooldown_bars`, crossovers), never with `(t2 - t1) / duration`, which is wrong across nights, weekends and holidays.
- **Presentation (#17, #25):** do not show the nominal close as the market close; for `1d` it is midnight of the next day. Show the session date or the calendar close.

### Signal identity

`Signal` (`domain/signals.py`) is a frozen, keyword-only dataclass with `ticker`, `timeframe`, `rule_id`, `side` (`Side.BUY` or `Side.SELL`), `candle_close_ts`, `close_price` (a positive finite float) and `indicator_values` (a read-only mapping of finite floats). Its `idempotency_key` is a `SignalKey(ticker, timeframe, rule_id, candle_close_ts)` (rule 5), and `SignalKey` built directly validates the same way:

- `ticker` goes through `normalize_ticker`: stripped, upper-cased, 1–32 printable ASCII characters without whitespace or `|`;
- `rule_id` is an opaque, case-sensitive string of 1–64 such characters, never stripped;
- `candle_close_ts` accepts any aware `datetime` (including `pd.Timestamp`) and is stored as a stdlib `datetime` in UTC through `to_utc`; naive values are rejected (rule 6);
- `str(key)` is the canonical form `TICKER|timeframe|rule_id|close in ISO 8601`, stable across processes and used for logs;
- `hash(key)` changes between processes (`PYTHONHASHSEED`): persist the key fields or `str(key)`, never `hash()`.

```python
from datetime import UTC, datetime

from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe

timeframe = Timeframe.parse(" 1d ")
signal = Signal(
    ticker=" aapl ",
    timeframe=timeframe,
    rule_id="42",
    side=Side.BUY,
    candle_close_ts=timeframe.nominal_close(datetime(2024, 1, 2, 5, 0, tzinfo=UTC)),
    close_price=187.5,
    indicator_values={"rsi(length=14).value": 28.4},
)
assert str(signal.idempotency_key) == "AAPL|1d|42|2024-01-03T05:00:00+00:00"
```

## Market calendar

`domain/market_calendar/` answers the time questions the bot asks about the US equity market: whether the market is open at an instant, the regular session of a date (holidays and half days included), the real close of a candle, when the next candle closes and which candles are closed at `now`. The scheduler (#15) uses it to fire, the data layer (#8) to drop the in-progress candle and #10 to build `4h` bars and size lookbacks. Design and decisions: spec [009](specs/009-market-calendar.md).

- `sessions.py` holds the model (`MarketCalendar`, `Session`, `CandleSlot` and the errors) and every query, using the standard library only. `nyse.py` holds `build_nyse_calendar`, the only module that imports `exchange_calendars`, so the source can be replaced by rewriting one module.
- A `MarketCalendar` is an immutable snapshot of UTC sessions. Its queries take the instant as an argument, so they are pure and simulated clocks pass `now` explicitly. There is no module-level instance: the calendar is built once at startup and injected (#16).
- v1 uses the single NYSE calendar for every ticker (US-listed stocks and ETFs share NYSE hours and holidays) and regular hours only: 09:30–16:00 ET, 13:00 on half days.

```python
from datetime import UTC, date, datetime

from trading_bot.domain.market_calendar.nyse import build_nyse_calendar
from trading_bot.domain.timeframe import Timeframe

calendar = build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))  # built once, injected

session = calendar.session_bounds(date(2024, 7, 3))  # None on weekends and holidays
assert session is not None
assert session.close_time == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)  # 13:00 ET, a half day

now = datetime(2024, 7, 3, 17, 0, tzinfo=UTC)
assert not calendar.is_open(now)  # sessions are half-open
assert calendar.next_candle_close(Timeframe.H1, now) == datetime(2024, 7, 5, 14, 30, tzinfo=UTC)

daily = calendar.candle_slot(Timeframe.D1, datetime(2024, 7, 3, 4, 0, tzinfo=UTC))  # 00:00 ET
assert daily.close_time <= now  # the daily candle of 2024-07-03 is closed
assert calendar.closed_candles(Timeframe.D1, now, 1) == (daily,)
```

### Coverage

Every calendar covers an explicit span of exchange days, from `coverage_start` (00:00 of `first_day` in New York) to `coverage_end` (00:00 of the day after `last_day`). A day or instant outside it, or a question whose answer lies beyond it (no candle closes later, fewer closed candles than requested), raises `CalendarRangeError` (a `ValueError` with `calendar`, `first_day` and `last_day`) instead of guessing "open" or "closed".

`build_nyse_calendar` supports 2000-01-01 to 2099-12-31. The recommended wiring (#16) builds that whole range once at startup (about 0.5 s and 7 MB on the development machine), so no clock is read and there is no horizon to watch. Arguments follow the domain conventions: instants must be timezone-aware (`to_utc`), days are `date` values but never `datetime`, and timeframes are `Timeframe` members.

### Boundaries

Intervals are half-open. A session is `[open_time, close_time)`, a candle is closed when `close_time <= now`, and `next_candle_close(timeframe, now)` is strictly after `now`. At exactly 16:00:00 ET the market is closed and the daily candle is closed, and a scheduler that recomputes from the close it just used always moves forward.

### Candle grid and labels

Intraday candles are anchored at the session open and the last one is cut at the close; there is one `1d` candle per session. Arithmetic is done in UTC from the real open, so a DST change never splits a candle and an ad hoc late open still yields a consistent grid. `candle_slots(timeframe, start, end)` returns the candles whose label is in `[start, end)`.

| Session | `1h` (label → real close, UTC) | `4h` | `1d` |
|---------|--------------------------------|------|------|
| 2024-01-02 (regular, EST) | 14:30→15:30, 15:30→16:30, 16:30→17:30, 17:30→18:30, 18:30→19:30, 19:30→20:30, 20:30→21:00 | 14:30→18:30, 18:30→21:00 | 05:00 (open 14:30) → 21:00 |
| 2024-07-03 (half day, EDT) | 13:30→14:30, 14:30→15:30, 15:30→16:30, 16:30→17:00 | 13:30→17:00 | 04:00 (open 13:30) → 17:00 |

The canonical label is the candle open for `1h` and `4h`, and 00:00 of the session date in New York for `1d` (the yfinance convention). `candle_slot(timeframe, label)` matches labels exactly and raises `CandleLabelError` (a `ValueError`) for any other label inside the calendar, with the first `kind` that applies:

| Kind | Meaning | Example (ET) |
|------|---------|--------------|
| `not_a_session` | The label's New York date has no session | a `1h` label at 10:30 on 2024-07-04 |
| `outside_session` | `1h` and `4h` only: before the open, or at or after the close | a `1h` label at 08:00 (pre-market) or at 16:00 |
| `off_grid` | Any other label | a `1h` label at 10:00; a `1d` label at 00:00 UTC (20:00 the day before) |

A provider that labels daily bars at 00:00 UTC therefore fails loudly instead of mapping every bar to the previous session. #8 normalizes provider labels to this convention and decides what to do with rejected rows by `kind`.

### Identity stays nominal

The calendar decides closedness and scheduling only. `Evaluation.candle_close_ts` and signal keys keep `timeframe.nominal_close(label)` ([Nominal candle close](#nominal-candle-close)), so a library correction to a half day never changes stored keys. For the `1d` candle of 2024-03-08 the nominal close is `2024-03-09T05:00Z`, while the real close is `2024-03-08T21:00Z`.

### Data horizon

Regular holidays and early closes come from rules that `exchange_calendars` evaluates for any year; ad hoc closures (days of mourning, weather, emergencies) are hard-coded in each library release (4.13.2 includes 2025-01-09). A closure announced after the installed release is treated as a session: the scheduler runs at the expected closes, the provider publishes no new candle, the last candle is already closed and its signal key already exists, so nothing is sent twice (rule 5), although stale-data detection (#26) may report it. An unknown ad hoc early close delays that day's last candles until the regular close. Dependabot bumps of `exchange-calendars` bring new closures, and the per-year golden tables in the tests make any library correction visible.

## Market data

`data/` gives every candle source one async contract and one shared path from a provider response to the frame the engine evaluates. The pure frame operations live in `domain/` (`candle_normalization.py`, `closed_candles.py`), so they pass the purity guard and carry look-ahead tests. Design and decisions: spec [010](specs/010-market-data-provider.md).

### Provider contract

`MarketDataProvider` (`data/provider.py`) is a `typing.Protocol` with two async methods. Implementations (`YFinanceProvider`, #10) are injected in `main.py`, and the engine only sees the Protocol.

- `fetch_candles(ticker, timeframe, lookback, *, now)` validates its arguments with `CandleRequest` before any I/O and returns the last `lookback` candles closed at `now`: the frame passes `validate_candles` with a `datetime64[us, UTC]` index, every label is a canonical slot label whose slot closed at or before `now`, the last label is the last slot closed at `now`, and it has between 1 and `lookback` rows (fewer only when the history is shorter or a provider limit caps it, which the provider logs).
- `validate_ticker(ticker)` parses the text with `parse_ticker` and returns `ensure_supported(info)` for an instrument the provider knows.
- `now` is explicit: providers never read the wall clock, so the scheduled `now` of a run keeps retries and simulated clocks deterministic.
- `lookback` counts closed candles, `1 <= lookback <= MAX_LOOKBACK` (5 000). `candle_window(request, calendar=...)` turns it into the span to fetch: the `first` and `last` of the `lookback` closed slots, with `start = first.open_time` and `end = last.close_time`.
- Both methods raise only argument errors (`TypeError`/`ValueError`), the [errors](#errors) below and `CalendarRangeError`, a configuration error when the injected calendar does not cover the window. Every provider builds its result with `prepare_candles`.

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

### Normalization

`normalize_candles(raw, timeframe, calendar=...)` returns `NormalizedCandles(candles, dropped)`.

- **Canonical frame.** The index is converted to UTC with unit `us`, named `None` and sorted. The five columns are matched case-insensitively after stripping whitespace (`Adj Close`, `Dividends` and other columns are ignored), and integer, float and nullable numeric dtypes are cast to `float64` (`pd.NA` becomes NaN). The same candles in any zone, unit or row order give identical frames, so signal keys do not depend on the provider's shape, and normalizing a canonical frame changes nothing. `raw` is never modified.
- **Structural errors.** `CandleNormalizationError` (a `ValueError` with `kind` and `column`) reports the first failing check: `index_type`, `naive_index` (a naive index is never localized), `multiindex_columns`, `ambiguous_column`, `missing_column` and `non_numeric_column` (object, string, bool, datetime and category columns). Messages never echo raw column labels.
- **Dropped rows (decision D32).** Bad rows are dropped, never repaired, and each one is reported once as `DroppedRow(position, label, reason)` with the first matching reason:

| Order | Reason | The row |
|-------|--------|---------|
| 1 | `missing_timestamp` | has a `NaT` label |
| 2 | `outside_calendar` | has a label outside the calendar coverage |
| 3 | `off_grid` | has a label with sub-microsecond precision |
| 4 | `not_a_session`, `outside_session`, `off_grid` | has a label the calendar rejects (`candle_slot`), with its kind |
| 5 | `missing_value` | has NaN in any value |
| 6 | `infinite_value` | has ±inf in any value |
| 7 | `non_positive_price` | has a price `<= 0` |
| 8 | `negative_volume` | has `volume < 0` |
| 9 | `inconsistent_range` | has `high < low`, or `open` or `close` outside `[low, high]` |
| 10 | `duplicate` | repeats the label of an earlier surviving row with equal values (the first copy is kept) |
| 11 | `conflicting_duplicate` | shares its label with surviving copies that differ (every copy is dropped) |

Label reasons come first, so an after-hours bar is `outside_session` even with broken values, and values come before duplicates, so a valid bar survives a provisional NaN copy. Every rule is row-local or compares rows with the same label, so a prefix of the input gives a prefix of the output.

### Open-candle removal

`drop_open_candle(candles, timeframe, now, calendar=...)` keeps the rows labelled at or before the label of the last slot closed at `now` (`close_time <= now`, half-open) with one calendar query and one binary search, and drops **every** later row: with a `now` in the past it returns exactly what was closed then (simulations, backtests). The last returned label must be a slot label, and a rejected label raises the calendar's `CandleLabelError`. On the NYSE grid:

| `now` | `1h`: last kept label → real close | `4h` | `1d` |
|-------|------------------------------------|------|------|
| 2024-07-02T20:00Z (exactly the close: kept) | 19:30Z → 20:00Z | 17:30Z → 20:00Z | 07-02T04:00Z → 20:00Z |
| 2024-07-04T15:00Z (holiday) | 07-03T16:30Z → 17:00Z | 07-03T13:30Z → 17:00Z | 07-03T04:00Z → 17:00Z |
| 2024-03-11T14:30Z (first `1h` close in EDT) | 13:30Z → 14:30Z | 03-08T18:30Z → 21:00Z | 03-08T05:00Z → 21:00Z |

### Last closed candle and logging

`prepare_candles(raw, request, calendar=...)` requires the last returned row to be the last slot closed at `now` (decision D43). Otherwise it raises `CandleNotPublishedError` with reason `invalid` when a dropped row had that label or `missing` when none did, or `NoDataError` when no closed candle remains and nothing was dropped at that label. The engine therefore never re-evaluates a stale frame by accident. A `CandleNormalizationError` becomes `ProviderDataError` with the same `kind`, raised `from None`.

Dropped rows give one record per reason, in `DropReason` order, on the `trading_bot.data.pipeline` logger: `WARNING` for rows labelled at or before the last closed slot (or `NaT`), which were closed candles sent broken, and `DEBUG` for later rows, which belong to the in-progress candle, and for exact duplicates. A record holds the count, the reason, the ticker, the timeframe and the first and last ISO labels, never values or provider text:

```text
dropped 2 outside_session candle rows for AAPL 1h (first 2024-07-02T20:00:00+00:00, last 2024-07-03T12:00:00+00:00)
```

### Errors

Every error is a `MarketDataError(Exception)`, and none is a `ValueError`, so an `except ValueError` meant for programming errors never swallows a provider failure. Callers branch on the class and on the class-level `retryable` flag.

| Class | `retryable` | Raised when |
|-------|-------------|-------------|
| `InvalidTickerError` (`reason`) | no | the text is malformed, the provider does not know the ticker, or `ensure_supported` rejects it |
| `NoDataError` | no | no candle closed by `now` survives normalization and no dropped row has the expected label |
| `ProviderDataError` (`kind`) | no | the response cannot be normalized, or its metadata is unusable |
| `CandleNotPublishedError` (`reason`, `expected_label`, `last_label`) | yes | the last slot closed at `now` has no valid row |
| `ProviderUnavailableError` (`failure`, `retry_after`) | yes | a transport failure, timeout, rate limit, server error or transient malformed response |

- Messages are one line built only from normalized tickers, timeframe codes, enum values and ISO timestamps, for example `missing: the provider has no row for the last closed candle [ticker=AAPL, timeframe=1d, expected_label=2024-07-05T04:00:00+00:00, last_label=2024-07-03T04:00:00+00:00]`.
- **Chain hygiene.** Errors raised while handling a library or network exception use `raise ... from None`. HTTP exceptions can carry URLs whose query strings hold Yahoo's session crumb, and the log redaction filter does not recognize crumbs.
- `TickerInfo(symbol, name, asset_type, exchange, currency)` can describe any instrument. `ensure_supported(info)` is the single implementation of decision D27, checked in order: asset type `equity` or `etf`, an exchange other than `Exchange.OTHER` (OTC markets map to `OTHER`), then currency `USD`. `name` is provider text: presentation layers must escape it.

### Unpublished candles

| Owner | Responsibility |
|-------|----------------|
| #8 | Detects: `prepare_candles` raises `CandleNotPublishedError` (retryable, with `expected_label`) instead of returning a frame that ends before the last closed slot |
| #10 | Never retries it internally: transport retries (backoff, jitter, timeout) apply to `ProviderUnavailableError` only |
| #15 (with #14) | Owns the bounded window: retries those tickers with the **same** scheduled `now`, until a deadline no later than `calendar.next_candle_close(timeframe, now)`, then skips them and logs |

### Testing with the fake provider

`FakeMarketDataProvider` (`tests/fixtures/fake_provider.py`) runs the real `prepare_candles` on stored raw frames, records calls and raises scripted `MarketDataError`s first-in, first-out, so engine tests only see frames production can return. `session_candles` (`tests/fixtures/session_candles.py`) builds canonical frames on the calendar grid and `provider_shaped` gives them the yfinance shape, with synthetic values only. `assert_closed_candles` (`tests/fixtures/provider_contract.py`) checks the `fetch_candles` return contract. Tests run coroutines with `asyncio.run`.

```python
import asyncio

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.fake_provider import FakeMarketDataProvider
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.data.errors import ProviderFailure, ProviderUnavailableError
from trading_bot.domain.timeframe import Timeframe

calendar = nyse_test_calendar()  # built once and injected in production (#16)
candles = session_candles(calendar, Timeframe.D1, utc("2024-06-03T00:00"), utc("2024-07-10T00:00"))
provider = FakeMarketDataProvider(calendar=calendar)
provider.set_candles("SPY", Timeframe.D1, provider_shaped(candles))  # synthetic values
provider.fail_next(ProviderUnavailableError(ProviderFailure.TIMEOUT), ticker="SPY")

now = utc("2024-07-05T20:00:30")  # just after the close of 2024-07-05
try:
    asyncio.run(provider.fetch_candles("SPY", Timeframe.D1, 2, now=now))
except ProviderUnavailableError as error:
    assert error.retryable  # the scripted failure comes first
frame = asyncio.run(provider.fetch_candles("SPY", Timeframe.D1, 2, now=now))
assert [label.isoformat() for label in frame.index] == [
    "2024-07-03T04:00:00+00:00",  # 2024-07-04 is a holiday
    "2024-07-05T04:00:00+00:00",  # the last candle closed at now
]
```

## Indicators

Rules can only use the closed catalog of `domain/indicators/` (`CLAUDE.md` rules 3, 4 and 8). Design, exact definitions and decisions: spec [005](specs/005-indicator-registry.md).

```python
from tests.fixtures.candles import synthetic_candles
from trading_bot.domain.indicators.catalog import REGISTRY

candles = synthetic_candles(300)  # validated closed candles from the data provider in production
params = REGISTRY.validate_params("macd", {"fast": 8})  # fast=8, slow=26, signal=9
output = REGISTRY.resolve_output("macd", "hist")  # required: macd has several outputs

hist = REGISTRY.compute("macd", params, candles)[output]  # float64 series on candles.index
assert hist.iloc[: REGISTRY.lookback("macd", params)].isna().all()
assert REGISTRY.warmup("macd", params) == 34  # candles for a value at the last candle
assert REGISTRY.stable_warmup("macd", params) == 164  # candles to fetch for reproducible values
```

`REGISTRY` (`catalog.py`) is an immutable `IndicatorRegistry`. Names are exact and case-sensitive. `validate_params` rejects undeclared names, booleans, non-integral values for integer parameters, values out of range and violated constraints with an `IndicatorError` (a `ValueError` with `kind`, `indicator`, `parameter` and `output`), and fills defaults. `describe()` returns the catalog as JSON-ready metadata for the dashboard.

### Catalog

| Name | Inputs | Parameters: default `[min, max]` | Outputs | `lookback` | `settle` |
|------|--------|----------------------------------|---------|------------|----------|
| `sma` | close | `length` 20 `[2, 500]` | `value` | `length - 1` | `0` |
| `ema` | close | `length` 20 `[2, 500]` | `value` | `length - 1` | `ema_settle(length)` |
| `rsi` | close | `length` 14 `[2, 100]` | `value` | `length` | `7 * length` |
| `macd` | close | `fast` 12 `[2, 100]`, `slow` 26 `[3, 200]`, `signal` 9 `[1, 100]`; `fast < slow` | `macd`, `signal`, `hist` | `slow + signal - 2` | `ema_settle(slow) + ema_settle(signal)` |
| `bbands` | close | `length` 20 `[2, 500]`, `std` 2.0 `[0.1, 5.0]` (float) | `lower`, `middle`, `upper` | `length - 1` | `0` |
| `atr` | high, low, close | `length` 14 `[1, 100]` | `value` | `length` | `7 * length` |
| `adx` | high, low, close | `length` 14 `[2, 100]` | `value` | `2 * length - 1` | `10 * length` |
| `stoch` | high, low, close | `length` 14 `[1, 100]`, `smooth_k` 3 `[1, 100]`, `smooth_d` 3 `[1, 100]` | `k`, `d` | `length + smooth_k + smooth_d - 3` | `0` |
| `obv` | close, volume | `signal` 20 `[2, 500]` | `value`, `signal` | `signal - 1` | `0` |
| `volume_sma` | volume | `length` 20 `[2, 500]` | `value` | `length - 1` | `0` |

Parameters are integers except `bbands` `std`. `ema_settle(p) = (7 * (p + 1) + 1) // 2`, that is `ceil(3.5 * (p + 1))`. Renaming a parameter or changing a range requires migrating stored rules.

### Outputs

- `compute(name, params, candles)` validates the name, the parameters and then the frame (`validate_candles`), and returns a new `dict` with one series per output in declared order: `float64`, named after the output, as long as the frame and indexed by `candles.index`. It never modifies `candles`.
- Every output is NaN before row position `lookback` (never back-filled), and never `±inf`. `macd`, `stoch` and `obv` start all their outputs at the same position, although their first line is defined earlier.
- Values are NaN, not TA-Lib's substituted `0`, where they are undefined: `rsi` while every close so far equals the first one; `adx` until the first candle with directional movement; `stoch` `k` when the high-low range of a window it averages is zero, and `d` when one of the `k` values it averages is undefined. Flat candles of a halted or illiquid ticker therefore do not fire `stoch k < 20` on substituted zeros. `macd` and `atr` give `0` and `bbands` three equal bands on flat data, which are correct values.
- `macd`, `bbands`, `stoch` and `obv` have several outputs and no default: rules must name one (`resolve_output`). The others default to `value`.

### Warmup and reproducibility

- `warmup = lookback + 1` is the minimum frame length for a value at the last candle; rules can fire from it (crossovers need one more candle).
- `stable_warmup = warmup + settle` is the frame length after which a recursive indicator no longer depends on where the history starts, within about 0.1% (the seed weighs about `e^-7`). The window indicators (`sma`, `bbands`, `stoch`, `volume_sma`) have `settle = 0`.
- Values computed on a fetch window that starts later differ slightly from long-history values until `stable_warmup`. That is not look-ahead, but it affects reproducibility between runs and against backtests. The data layer and the engine (#8, #14) fetch at least the rules' `stable_warmup` candles, or everything available, and log when a provider limit caps the history: #14 passes `max(rule.stable_warmup())` over the ticker's rules as the provider `lookback` (at most 2 255 today, below `MAX_LOOKBACK`). Requiring `stable_warmup` to fire would silence young tickers (`ema` `length=200` would need 904 daily candles).
- History limits: 730 days of `1h` US equity data is about 3 500 bars and the `4h` resample about 1 000. `stable_warmup` exceeds 1 000 from `ema` `length=222`, `rsi` and `atr` `length=125`, `adx` `length=84`, and `macd` `slow=200` with `signal >= 21`; such rules still fire from `warmup`, but runs may differ by up to about 0.1% as the window moves.
- **`obv`:** the level is a cumulative sum from the first candle of the frame, so moving the start shifts `value` by a constant, and `signal` (its simple moving average) by the same constant. Comparing `value` with `signal`, including crossovers, does not depend on the history start; comparing `value` or `signal` with a fixed value, a price or another indicator never becomes reproducible.

### TA-Lib isolation

- Only `talib_kernels.py` imports `talib`; `registry.py`, `spec.py`, `params.py` and `errors.py` never load it, so TA-Lib can be replaced by rewriting one module. The purity guard enforces it.
- TA-Lib unstable-period setters change results process-wide: they are never called in `src/` and must not be called by any code in the process. The `ema`, `macd`, `rsi`, `atr` and `adx` kernels check that the relevant unstable period is `0` on every call and raise `IndicatorComputationError` (a `RuntimeError`) otherwise, instead of returning shifted values. The compatibility setter has been a no-op since TA-Lib C 0.8.1, which removed the MetaStock compatibility mode; it is not called either. With no setter calls, `compute` holds no state and is safe to call from several threads.

### How to add an indicator

1. A spec with the definition, parameters (names, defaults, ranges), outputs, `lookback`, `settle` and undefined values.
2. A kernel in `talib_kernels.py`: every TA-Lib parameter by keyword, explicit moving-average types, masks for undefined values computed from candles at or before each position, and the unstable-period guard when the TA-Lib function has one.
3. A `catalog.py` entry with `lookback` and `settle`, and its row in the catalog table above.
4. Golden cases computed by hand, an independent plain-Python reference compared on the fixture matrix, look-ahead tests on every `Scenario` plus the property test, warmup and stable-warmup tests, and the updated `describe()` expectations.

## Rule model

Rules are defined from the dashboard, validated with pydantic against the indicator whitelist and stored as JSON. There is no `eval` (`CLAUDE.md` rule 8). Design, bounds and decisions: spec [006](specs/006-rule-schema.md).

```json
{
  "name": "RSI oversold in uptrend",
  "signal": "BUY",
  "timeframe": "1d",
  "conditions": {
    "all": [
      {
        "left": {"indicator": "rsi", "params": {"length": 14}},
        "op": "crosses_below",
        "right": {"value": 30}
      },
      {
        "left": {"price": "close"},
        "op": ">",
        "right": {"indicator": "sma", "params": {"length": 200}}
      }
    ]
  },
  "cooldown_bars": 5
}
```

```python
import json

from trading_bot.domain.rules.schema import dump_rule, parse_rule

rule = parse_rule(document)  # the JSON above as text, UTF-8 bytes or a mapping
canonical = dump_rule(rule)  # every default filled, in the canonical key order
assert canonical["conditions"]["all"][0]["left"] == {
    "indicator": "rsi",
    "params": {"length": 14},
    "output": "value",
}
assert parse_rule(json.dumps(canonical)) == rule  # the canonical form is a fixed point
assert rule.warmup() == 200  # candles before the rule can fire
assert rule.stable_warmup() == 200  # candles for values independent of the history start
```

### Grammar

```text
rule        := {"name": string, "signal": "BUY"|"SELL", "timeframe": "1h"|"4h"|"1d",
                "conditions": group, "cooldown_bars"?: integer}
group       := {"all": [item, ...]} | {"any": [item, ...]}          # exactly one key, 1..10 items
item        := condition | nested                                    # only inside the root group
nested      := {"all": [condition, ...]} | {"any": [condition, ...]} # exactly one key, 1..10 items
condition   := {"left": operand, "op": operator, "right": operand}
operand     := {"indicator": name, "params"?: object, "output"?: string}
             | {"price": "open"|"high"|"low"|"close"|"volume"}
             | {"value": number}
operator    := "<" | "<=" | ">" | ">=" | "crosses_above" | "crosses_below"
```

- **Operands.** An operand has **exactly one** of `indicator`, `price` or `value`. `indicator` is an exact, case-sensitive name of the [whitelist](#indicators) (`sma`, `ema`, `rsi`, `macd`, `bbands`, `atr`, `adx`, `stoch`, `obv`, `volume_sma`); its `params` are validated by the registry and its `output` must be named for `macd`, `bbands`, `stoch` and `obv`, which have no default output. `price` is a candle column and `value` a constant.
- **Levels.** The root group is level 1 and a group inside it is level 2; a group at level 3 is rejected. `conditions` is always a group: a single condition is written `{"all": [condition]}`.
- **Optional keys are absent, not null.** `params`, `output` and `cooldown_bars` are omitted when unset; an explicit `null` is rejected.
- **Nothing is coerced and unknown keys are rejected everywhere:** `"14"`, `14.0` and `true` are not integers, `"30"` is not a number, and `"1D"`, `" 1d "` or `"buy"` are not valid codes.
- **Semantics.** A condition whose two sides are constants, or whose two operands are equal once normalized, is rejected: it cannot depend on the market. Duplicated, contradictory or unit-mismatched conditions stay valid; the domain does not judge a strategy.
- `cooldown_bars` is the minimum number of closed candles between two notified signals of the same rule and ticker; `0` means no cooldown (idempotency by `(ticker, timeframe, rule_id, candle_close_ts)` still prevents resending the same candle). It is implemented in #14.
- `timeframe` is the candle timeframe the rule is evaluated on, a `Timeframe` code with the exact [#4 semantics](#timeframes). It must equal the timeframe of every ticker the rule is assigned to: #12 rejects an assignment whose timeframes differ.
- A rule is assigned to one or more tickers (#12). A rule document carries no identifier and no ticker list: `rule_id` and `enabled` belong to the database.

| Bound | Value |
|-------|-------|
| Members per group | 10 |
| Conditions per rule | 20 |
| Nesting levels | 2 |
| Rule name | 1 to 80 printable characters, surrounding whitespace stripped |
| `cooldown_bars` | integer in `[0, 500]`, default `0` |
| Constant operand | finite number with `abs(value) <= 1e15` |
| Document | 65 536 UTF-8 bytes |
| Problems per error | 20 |

### Canonical form

`dump_rule(rule)` writes the accepted rule back with every default filled: `params` holds every declared parameter in declaration order, `output` is always explicit and `value` is always a float. The keys are in the order of the grammar above (`name`, `signal`, `timeframe`, `conditions`, `cooldown_bars`; `left`, `op`, `right`; `indicator`, `params`, `output`). Two documents that differ only in omitted defaults, key order or `30` against `30.0` give equal, hashable and frozen rules.

`Rule.model_dump(mode="json")` and `Rule.model_dump_json()` produce the same document, so a rule encoded through pydantic (a stored row, an API response) is the canonical one too.

This is what keeps stored rules stable (#12 stores `json.dumps(dump_rule(rule))`): changing a catalog default later cannot silently change the meaning of a rule already saved, and the same document always produces the same signals.

### Validation and errors

`parse_rule` is the only entry point for untrusted input and raises `RuleValidationError` (a `ValueError`) for every rejection, including a payload above the byte cap, invalid UTF-8, malformed JSON, `NaN`/`Infinity` literals, duplicate JSON keys and documents nested thousands of levels deep. `Rule.model_validate` stays available for in-process construction from trusted values and raises pydantic's `ValidationError` instead.

The error carries `kind`, `path` and `problems`: one `RuleProblem(kind, path, message)` per rejected field, in document order, at most 20. `kind` is a `RuleErrorKind` (`unknown_indicator`, `invalid_parameter`, `invalid_operand`, `nested_too_deep`, `degenerate_condition`, `too_large`, ... with `invalid_rule` as the catch-all), and `path` locates the field (`conditions.all[1].left.params.length`). Every message is one English line, every path segment is at most 32 characters and every path at most 160: rule documents are attacker-controlled and these strings reach logs, Telegram and HTTP responses, so user input only appears as a `repr()`-escaped, truncated echo and the raw input is never copied.

### Warmup

`rule.warmup()` is the number of candles the rule needs before it can fire: the maximum over its conditions of the maximum over the two operands of `REGISTRY.warmup(indicator, params)` for an indicator, `1` for a price and `0` for a constant, plus one candle for both sides when the operator is a crossover, and never below `1`. `rule.stable_warmup()` is the same with `REGISTRY.stable_warmup` and is always at least `warmup()`. The rule above gives `200`/`200`; `macd` crossing its signal line gives `35`/`165`, `rsi` crossing `30` gives `16`/`114` and a rule of prices and constants gives `1`/`1`. The data layer and the engine (#8, #14) fetch at least `stable_warmup()` candles over the rules of a ticker.

### JSON Schema

`rule_json_schema()` exports the document as a JSON Schema draft 2020-12, with the indicator operand inlined as a `oneOf` with one branch per registry indicator: a `const` name, one typed property per declared parameter (`integer`/`number` with `minimum`, `maximum`, `default`, `title` and `description` from `describe()`), the allowed outputs as an `enum` and the parameter constraints as advisory `x-constraints`. It is generated from the models and the catalog, so it cannot drift from them, and the dashboard rule builder (#24) uses it to render forms and pre-validate.

The server is authoritative: in four documented cases the schema accepts what the model rejects, because draft 2020-12 cannot express them — non-integral numbers for integer fields (`14.0`), the `less_than` constraints between parameters (`macd` `fast < slow`), the degenerate-condition checks, and the rule-name stripping and printability rules together with the total number of conditions.

## Rule evaluation

`domain/rules/evaluator.py` decides whether a parsed rule fires on a candle frame and returns the data the rest of the bot needs to act on that decision. It is pure (`CLAUDE.md` rules 3 and 4) and signal-only: it returns a boolean and the values behind it, nothing else. Design and decisions: spec [007](specs/007-rule-evaluator.md).

```python
from tests.fixtures.candles import synthetic_candles
from trading_bot.domain.rules.evaluator import evaluate, evaluate_each
from trading_bot.domain.rules.schema import parse_rule

rule = parse_rule(document)  # the rule of the Rule model section
candles = synthetic_candles(300)  # validated closed candles from the data provider in production

evaluation = evaluate(rule, candles)  # about candles.index[-1], the last closed candle
assert evaluation.candle_close_ts == rule.timeframe.nominal_close(candles.index[-1])
assert evaluation.close_price == candles["close"].iloc[-1]
assert list(evaluation.indicator_values) == ["rsi(length=14).value", "sma(length=200).value"]

recent = evaluate_each(rule, candles, last=20)  # one Evaluation per candle, oldest first
assert recent[-1] == evaluation
```

`Evaluation` is a frozen, slotted, keyword-only dataclass (immutable, hashable, equal by value, picklable) with:

- `triggered`: the conditions hold **and** the candle has at least `rule.warmup()` candles of history in the frame;
- `candle_close_ts`: `rule.timeframe.nominal_close(open time)`, a stdlib `datetime` in UTC. It is the identity field of a signal, not the market close ([Nominal candle close](#nominal-candle-close));
- `close_price`: the close of the candle;
- `indicator_values`: the finite values of the indicator operands the rule uses, with the keys below;
- `condition_results`: the raw outcome of each entry of `rule.all_conditions`, in that order and before the warmup gate, so the dashboard can explain why a rule did or did not fire.

### Frames and entry points

- The last row of the frame is treated as the **last closed candle**. Dropping the in-progress candle is the data layer's job (#8); the evaluator reads no clock and cannot tell.
- The frame is checked with `validate_candles` (`CandleValidationError`, also for rules without indicators) and never modified. Arguments of the wrong type raise `TypeError` and registry errors propagate unchanged. A returned candle whose open time has sub-microsecond precision raises `ValueError` (from `to_utc`, it cannot name a `candle_close_ts`); normalizing labels is the data layer's job (#8). NaN values, flat prices, short histories and extreme values never raise.
- `evaluate` on an empty frame raises `ValueError` (there is no candle to describe); `evaluate_each` returns `()`.
- `evaluate_each(rule, candles, last=N)` covers the last `N` candles: `None` means the whole frame, a value above the frame length is clamped, `0` gives `()`, a negative value raises `ValueError` and a non-`int` (including `bool`) raises `TypeError`. `evaluate(rule, candles) == evaluate_each(rule, candles)[-1]` and `evaluate_each(rule, candles, last=k) == evaluate_each(rule, candles)[-k:]`, bitwise. The dashboard "test rule" (#24) and backtesting (F8) use this form.
- `registry=` injects an `IndicatorRegistry`, meant for instrumented doubles in tests. It must be compatible with the catalog that validated the rule, because the rule's parameters and `rule.warmup()` come from that catalog.

### Operators

Comparisons read the evaluated candle only, with the left value `l` and the right value `r`:

| Case | `<` | `<=` | `>` | `>=` |
|------|-----|------|-----|------|
| `l < r` | True | True | False | False |
| `l == r` | False | True | False | True |
| `l > r` | False | False | True | True |
| `l` or `r` is NaN (or both) | False | False | False | False |

Crossovers also read the **previous row** of the same frame (`pl`, `pr`); a touch counts as a cross (decision D13):

- `crosses_above` is `pl <= pr and l > r`;
- `crosses_below` is `pl >= pr and l < r`.

| Previous close | Close | `close crosses_above 100` |
|----------------|-------|---------------------------|
| — (first row) | 99 | False: no previous candle |
| 99 | 100 | False: equal, not above |
| 100 | 101 | **True**: touched, then broke above |
| 101 | 101 | False: already above |
| 100 | 100 | False: equal, not above |

The first row of a frame never crosses, a crossover implies the strict comparison at the candle, the two crossovers are never true at the same candle and the same crossover is never true at two consecutive candles.

### NaN, warmup and groups

- **NaN never fires and never raises** (decision D4). A condition whose operands are NaN at any candle it reads is `False`, so a crossover needs two finite candles, and `rsi`, `adx` and `stoch` rules never fire on flat data, where those indicators are undefined.
- **Warmup gate** (decision D12). At row position `i` the candle has `i + 1` candles of history; while `i + 1 < rule.warmup()`, `triggered` is `False` whatever the conditions say. It only changes an outcome for `any` groups, where a cheap leg could otherwise fire before an expensive leg is computable: `any` of [`close > 50`, `close > sma(200)`] on 30 candles gives `condition_results == (True, False)` and `triggered == False`. The gate depends on the row position only, so a prefix and the full frame agree.
- `stable_warmup()` is **not** required to fire: requiring it would silence young tickers. Fetching enough history for reproducible values is the job of #8 and #14.
- **Groups.** `all` is the conjunction and `any` the disjunction of its members, nested groups first. Evaluation is complete, never short-circuited, so `indicator_values` and `condition_results` are complete even when an early `all` member is already `False`.

### Indicator value keys

Keys are `indicator(param=value, ...).output` (decision D14): parameters in declared order with the canonical values of the rule model (integers as integers, `bbands` `std` as a float) and the output always present, for example `rsi(length=14).value` and `macd(fast=12, slow=26, signal=9).hist`.

- One entry per distinct indicator operand, in first-appearance order (`left` before `right`). Operands that are equal once normalized (`{"indicator": "rsi"}` and `{"indicator": "rsi", "params": {"length": 14}, "output": "value"}`) share one entry.
- Only finite values appear: an operand that is NaN at the candle is absent, never NaN, which is what `Signal` requires.
- Prices and constants are never included; `close_price` is its own field.
- These are the keys #13 stores with each signal: changing the scheme later requires migrating stored signals.

### Purity and cost

- No clock, no I/O, no globals and no cache across calls. The module passes the purity guard with the default allowlist: it consumes the rule models without importing pydantic.
- Everything is vectorized over the frame. Each distinct `(indicator, params)` pair is computed exactly once per call, through a dictionary local to the call (`macd.macd` and `macd.signal` share one computation), whatever the frame length and `last` are. Comparisons read one row and crossovers the previous row, so every value at a candle depends only on that candle and earlier ones; the [look-ahead tests](#look-ahead-testing) verify it on every scenario instead of assuming it.

### What the engine adds

The evaluator answers "do the conditions hold at this candle?" and nothing else. The engine (#14):

- evaluates only rules whose `timeframe` equals the ticker's, on frames whose open candle was dropped (#8) and that hold at least `rule.stable_warmup()` candles when available, never on an empty frame;
- builds `Signal(ticker=..., timeframe=rule.timeframe, rule_id=..., side=rule.signal, candle_close_ts=evaluation.candle_close_ts, close_price=evaluation.close_price, indicator_values=evaluation.indicator_values)` when `triggered` is true;
- deduplicates by `Signal.idempotency_key` (`CLAUDE.md` rule 5) before notifying;
- applies `cooldown_bars` by row position over the evaluated frame, against the last notified signal of the same ticker and rule, never with `(t2 - t1) / duration`.

## Look-ahead testing

`CLAUDE.md` rule 4 requires that the result at candle `t` computed with `data[:t]` is identical to the one computed with `data[:t+k]` truncated to `t` (`data[:t]` includes `t`). The reusable harness lives in `tests/lookahead.py` and the fixtures in `tests/fixtures/` (spec [003](specs/003-lookahead-harness.md)). Always import them with the `tests.` prefix (`from tests.lookahead import ...`): a second import path creates a second `LookaheadError` class that `pytest.raises` does not match.

### When it is mandatory

- Every new indicator in the registry.
- Every rule operator and every change to the rule evaluator.
- Any other function in `domain/` that returns values per candle.

### Entry points

| Function | Use it for |
|----------|------------|
| `assert_no_lookahead(func, candles)` | Series-valued functions: `func(frame)` returns a `Series`, a `DataFrame` or a `Mapping[str, Series]` indexed by labels of `frame`. Warmup rows may be dropped and windowed outputs may return only their last rows. |
| `assert_no_lookahead_point_in_time(func, reference, candles)` | Last-candle functions: `func(frame)` returns one result about `frame.index[-1]`, like `evaluate(rule, candles)`. |

A last-candle function cannot be checked on its own. Each prefix yields a single result about its own last candle, and a longer prefix yields a result about a different candle, so there is never a second computation of the same `t` to compare: a truncation-only check would always pass. The harness therefore needs a **reference**, a series-valued function that gives the same result for every candle of a frame (for the rule evaluator, the evaluation over the last `N` candles with `N = len(frame)`, indexed by candle open time). It first checks the reference with `assert_no_lookahead`, then checks that `func(data[:t])` equals the reference at `t` computed on `data[:t]` (`k = 0`), on `data[:t+k]` and on the full frame.

### Examples

```python
import pandas as pd
import pytest
from hypothesis import given

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.strategies import candle_frames
from tests.lookahead import assert_no_lookahead, assert_no_lookahead_point_in_time


def sma_20(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean()


@pytest.mark.parametrize("scenario", list(Scenario))
def test_sma_20_has_no_lookahead(scenario: Scenario) -> None:
    assert_no_lookahead(sma_20, synthetic_candles(250, seed=7, scenario=scenario))


@given(candles=candle_frames(min_size=40))
def test_sma_20_has_no_lookahead_on_random_frames(candles: pd.DataFrame) -> None:
    assert_no_lookahead(sma_20, candles, max_cuts=10)


def closes_higher(candles: pd.DataFrame) -> bool:
    close = candles["close"]
    return bool(close.iloc[-1] > close.iloc[-2])


def closes_higher_by_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close > close.shift(1)


def test_closes_higher_has_no_lookahead() -> None:
    candles = synthetic_candles(250, seed=3, scenario=Scenario.MIXED)
    assert_no_lookahead_point_in_time(closes_higher, closes_higher_by_candle, candles, min_prefix=2)
```

### Defaults

- **`ks=(1, 2, 5)` plus the full frame.** Each cut `n` is compared with the frame extended by 1, 2 and 5 candles and with the whole frame. The full-frame comparison catches any forward peek regardless of `ks`; the small `ks` catch behavior that depends on the frame length.
- **At most `max_cuts=25` cuts** (`select_cuts`): the first 5 and last 5 prefix lengths plus evenly spaced ones, deterministic. Use `max_cuts=len(candles)` for a full sweep or `cuts=[...]` to target specific prefix lengths. Raise `min_prefix` when the function needs a minimum frame (for example `iloc[-2]`).
- **Exact comparison** (`rtol=0.0`, `atol=0.0`). NaN equals NaN at the same candle and `-0.0 == 0.0`. Both sides run in the same process with the same sequential operations, so rolling windows, EWM, cumulative operations and TA-Lib recurrences are bitwise identical. `rtol`/`atol` are only for computations that genuinely cannot be bitwise stable, and the call site must carry a comment justifying the tolerance. TA-Lib indicators must not need it.

### Reading a failure

A violation raises `LookaheadError` (an `AssertionError`) whose `violation` attribute holds the structured fields. The first violation in check order is reported, at its earliest offending candle:

```text
look-ahead check failed in cheat_shift_forward: value_mismatch
  output: close
  cut: t=2024-01-01T00:00:00+00:00 (prefix length n=1)
  compared with: data[:t+k] with k=1 (prefix length 2)
  first offending candle: 2024-01-01T00:00:00+00:00
  with data[:t]:   nan
  with data[:t+k]: 100.60175469456382
  offending candles at or before t: 1
  hint: a value at or before t changed when later candles were appended; look for shift(-n), centered windows, bfill/interpolate or statistics over the whole frame
```

- `n` is the prefix length and `t = candles.index[n - 1]` its last candle.
- `k` is the number of candles appended after `t`; `(full frame)` marks the comparison with the whole frame.
- The first offending candle is at or before `t`; the two values are its result computed with `data[:t]` and with `data[:t+k]`.

| Kind | Meaning | Typical cause |
|------|---------|---------------|
| `value_mismatch` | A result at or before `t` changed when candles were appended | `shift(-n)`, centered windows, `bfill`/`interpolate`, statistics over the whole frame (`max()`, `mean()`, `rank()`) |
| `dtype_mismatch` | The output dtype depends on the frame length | Implicit upcasting or object columns that appear with more data |
| `result_appeared` | A result for a past candle exists only once later candles arrive | Filters or `dropna()` on conditions that read later rows |
| `result_disappeared` | A past result, not at the start of the output, is removed when candles are appended | Filters or de-duplication that depend on later rows |
| `outputs_changed` | Output names or their order depend on the frame length | Outputs added conditionally |
| `point_in_time_mismatch` | The last-candle result differs from the reference at `t` | Deciding about an earlier candle "confirmed" by a later one, reading candles after `t`, or a reference that describes a different result |
| `missing_reference` | The reference has no result at `t` | A reference restricted to a window or with dropped rows; evaluate it over the whole frame |
| `invalid_output` | The return value breaks the output contract | A scalar result (use the point-in-time entry point), a `RangeIndex` or tz-naive index, duplicated or unsorted labels |
| `input_mutated` | The function modified the frame it received | In-place assignment or `inplace=True` |
| `nondeterministic` | The same frame gave different results | Caches, counters, randomness or the clock |
| `vacuous` | Every compared value was missing, so nothing was checked | A frame shorter than the warmup |

Misuse of the harness (for example fewer than 2 candles or `ks` containing 0) raises `TypeError` or `ValueError`. An exception raised by the function under test propagates with a note naming `n` and `t`.

### Fixtures

`synthetic_candles(n, seed=..., scenario=..., timeframe=...)` in `tests/fixtures/candles.py` generates seeded candles: tz-aware UTC candle open times on the `1h`/`4h`/`1d` grid and `float64` columns `open, high, low, close, volume`. From `FEATURE_MIN_CANDLES` (60) candles on, the gap timestamps, flat runs, volume spike and zero-volume candles are guaranteed for any parameters. The price features (the −90% and +900% candles and the price jump after a gap) also need prices away from the `[1e-6, 1e9]` clipping bounds and, for the jump, `volatility > 0`. Fixed-seed tests check them at the default `start_price` and `volatility`, and property tests over drawn parameters assert only the parameter-independent features.

| Scenario | What it stresses |
|----------|------------------|
| `RANDOM_WALK` | Baseline: contiguous grid, each candle opens at the previous close |
| `GAPS` | Missing slots: no weekends, dropped weekday slots and price jumps at gaps (time-based windows, `shift` across gaps) |
| `FLAT_RUNS` | Runs of 5–20 flat, zero-volume candles (zero ranges and divisions by zero in RSI, stochastics or ATR) |
| `EXTREME` | A −90% candle, a +900% candle, a `1e12` volume spike and zero-volume candles (overflow and clipping) |
| `MIXED` | All of the above at once |

- `timeframe` (and `timeframes` in `candle_frames`) accepts `Timeframe` members as well as their codes. Shared assertions live in `tests/fixtures/candle_assertions.py`: `assert_valid_candles(candles, timeframe)` runs `validate_candles` plus the generator invariants (grid alignment and price bounds), and the scenario feature helpers raise `AssertionError` with a message.
- `candle_frames(...)` in `tests/fixtures/strategies.py` is a Hypothesis strategy that draws generator parameters (size, scenario, timeframe, seed, start, start price, volatility). The `trading-bot` profile in `tests/conftest.py` runs 50 examples without a deadline and keeps Hypothesis' CI profile (derandomized, no example database) on CI.
- Use frames longer than the warmup (and `min_size` above it for `candle_frames`); otherwise the check fails as `vacuous`. Property tests must only assert detection of cheats that do not depend on the data: `shift(-1)` is always detectable, but `close / close.max()` is not on a flat series.
- Never assert golden values from the generator: numpy does not guarantee random streams across versions. Assert invariants and scenario features instead.
- Recorded real-data fixtures arrive with #10 (`YFinanceProvider`): the repository is public and Yahoo's terms restrict redistribution of its data. They must not live under a directory named `data/`, which `.gitignore` ignores.

## Persisted data (draft)

- `tickers(id, symbol, timeframe, enabled, created_at)`
- `rules(id, name, signal, timeframe, definition_json, enabled, created_at, updated_at)`
- `ticker_rules(ticker_id, rule_id)`
- `signals(id, ticker_id, rule_id, timeframe, candle_close_ts, price, indicator_values_json, notified_at)` with unique `(ticker_id, rule_id, timeframe, candle_close_ts)`
- `bot_state(key, value)`: global pause, last heartbeat

## Configuration (environment variables)

| Variable | Purpose |
|----------|---------|
| `TB_ENVIRONMENT` | `dev` / `beta` / `prod` (set by compose on the Pi) |
| `TB_VERSION` | Version injected into the image |
| `TB_LOG_LEVEL` | Log level |
| `TB_TELEGRAM_BOT_TOKEN` | Secret. A different bot per environment |
| `TB_TELEGRAM_ALLOWED_CHAT_IDS` | Authorized chats (comma-separated) |
| `TB_DASHBOARD_PASSWORD_HASH` | Secret. Hash of the dashboard password |
| `TB_SESSION_SECRET` | Secret. Session cookie signing |

## Decisions

- **TA-Lib instead of pandas-ta.** pandas-ta lost its repository and its PyPI history and changed maintainers (supply chain risk). The `ta-lib` 0.8.0 wheels bundle the TA-Lib C library, including `manylinux` aarch64 for Python 3.12, so the image needs no compiler or system package (the `Dockerfile` smoke check fails the build otherwise). It is imported only by `domain/indicators/talib_kernels.py`, behind the indicator registry, so it can be replaced. TA-Lib global setters must never be called.
- **`exchange_calendars` for NYSE sessions.** It is correct for every golden case of spec 009, including the 2025-01-09 closure and the holiday observance rules, maintained, Apache-2.0, ships pure-Python wheels and works with the locked pandas and numpy. `pandas_market_calendars` depends on it (a larger supply chain for no gain), and an in-house rule table would move holiday rules and ad hoc closures into our maintenance. It is imported only by `domain/market_calendar/nyse.py`, which converts the schedule once into an immutable `MarketCalendar`, so it can be replaced by rewriting one module. The library's calendar registry is never used: it caches instances and defaults its bounds from the wall clock (the `Dockerfile` smoke check fails the build if the calendar cannot be built).
- **Async provider port with an explicit `now`.** The app is one asyncio process (FastAPI, `AsyncIOScheduler`, python-telegram-bot), so `MarketDataProvider` methods are coroutines: yfinance blocks, so #10 runs its calls in `asyncio.to_thread` and waits with `asyncio.sleep` between retries, which lets a run be cancelled at shutdown instead of waiting on a sleeping thread and keeps rate limiting a loop-local primitive without locks. `now` is the injected clock: the provider needs it to plan the window, drop the open candle and know which candle must be last, and one `now` per run keeps retries and simulated-clock tests deterministic. Providers never read the wall clock.
- **Long polling and not webhooks** for Telegram: the Pi does not expose public endpoints.
- **HTMX and not an SPA**: a single Python image, no Node toolchain.
- **SQLite**: a single writer process, a Docker volume and a backup before each deploy.
- **Backtesting** (vectorbt + walk-forward) in a later phase, with care for overfitting (Deflated Sharpe Ratio).
