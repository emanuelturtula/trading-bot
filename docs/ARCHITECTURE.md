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
| `domain/` | Models (`Timeframe`, the candle frame contract with `validate_candles`, `Signal`/`SignalKey`/`Side`; `Rule` in #6), indicator registry (whitelist → TA-Lib), rule evaluator | No I/O, no clock, no globals. Testable with fixed DataFrames. |
| `data/` | `MarketDataProvider` (Protocol) and `YFinanceProvider`: normalizes to UTC OHLCV, discards the open candle, retries with backoff | Never decides signals. Respects Yahoo's limits (intraday max. 60 days; 1h up to 730 days). |
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

Every check is row-local or compares adjacent labels, so every prefix of a valid frame is valid. The validator never coerces: the data provider (#8) converts zones, renames columns, casts volume to `float64` and decides how to repair or drop bad rows, then validates.

### Nominal candle close

`timeframe.nominal_close(open_time)` is `open_time + duration` in UTC, without a calendar. It is the `candle_close_ts` of a signal: an **identifier** derived deterministically from the provider label, not a market fact. A real close from a calendar would change whenever calendar data is corrected, and so would the keys of signals already stored.

For US equities (regular session 09:30–16:00 ET):

| Timeframe | Bar label (yfinance) | Real close | Nominal close |
|-----------|----------------------|------------|---------------|
| `1h` | 09:30, 10:30, …, 15:30 ET | open + 1h, except the last bar: 16:00 ET (13:00 ET on half days) | always open + 1h: 16:30 ET for the last bar |
| `4h` (#10 resample, session-aligned) | 09:30 and 13:30 ET | 13:30 and 16:00 ET | 13:30 and 17:30 ET |
| `1d` | 00:00 ET of the session date (05:00 or 04:00 UTC) | 16:00 ET the same day | 00:00 ET the **next** day |

Obligations for later work:

- **Closedness and scheduling (#8, #9, #15):** never use `nominal_close` to decide whether a candle is closed or when to run. Use the real session closes of #9; with nominal closes the daily candle would count as open until about 05:00 UTC the next day and the last hourly bar would be missed.
- **Label convention (#8, #10):** keys depend on provider labels. Fix the convention (open times; the `1d` label policy) before #13 persists signals. Changing it later requires migrating the stored keys, otherwise signals already notified can be sent again.
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
    indicator_values={"rsi_14": 28.4},
)
assert str(signal.idempotency_key) == "AAPL|1d|42|2024-01-03T05:00:00+00:00"
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
- Values computed on a fetch window that starts later differ slightly from long-history values until `stable_warmup`. That is not look-ahead, but it affects reproducibility between runs and against backtests. The data layer and the engine (#8, #14) fetch at least the rules' `stable_warmup` candles, or everything available, and log when a provider limit caps the history. Requiring `stable_warmup` to fire would silence young tickers (`ema` `length=200` would need 904 daily candles).
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

Rules are defined from the dashboard and stored as JSON validated with pydantic. There is no `eval`.

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

- Operands: `{"indicator", "params", "output"?}` · `{"price": "open|high|low|close|volume"}` · `{"value": number}`.
- Operators: `<`, `<=`, `>`, `>=`, `crosses_above`, `crosses_below`.
- Groups: `all` / `any`, nestable up to 2 levels.
- Indicators (initial whitelist): `sma`, `ema`, `rsi`, `macd` (`macd|signal|hist`), `bbands` (`lower|middle|upper`), `atr`, `adx`, `stoch` (`k|d`), `obv` (`value|signal`), `volume_sma`. Parameters, ranges and outputs: [Indicators](#indicators).
- Each indicator declares its allowed parameters and ranges; the evaluator rejects anything outside the whitelist.
- `cooldown_bars`: minimum candles between two signals of the same rule and ticker.
- A rule is assigned to one or more tickers.

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
- **Long polling and not webhooks** for Telegram: the Pi does not expose public endpoints.
- **HTMX and not an SPA**: a single Python image, no Node toolchain.
- **SQLite**: a single writer process, a Docker volume and a backup before each deploy.
- **Backtesting** (vectorbt + walk-forward) in a later phase, with care for overfitting (Deflated Sharpe Ratio).
