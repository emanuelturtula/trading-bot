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
| `domain/` | Models (`Candle`, `Timeframe`, `Signal`, `Rule`), indicator registry (whitelist → TA-Lib), rule evaluator | No I/O, no clock, no globals. Testable with fixed DataFrames. |
| `data/` | `MarketDataProvider` (Protocol) and `YFinanceProvider`: normalizes to UTC OHLCV, discards the open candle, retries with backoff | Never decides signals. Respects Yahoo's limits (intraday max. 60 days; 1h up to 730 days). |
| `engine/` | `SignalEngine`: orchestrates fetch → indicators → rules → dedupe/cooldown → persistence → notification | Idempotent by `(ticker, timeframe, rule_id, candle_close_ts)`. |
| `scheduler/` | APScheduler (AsyncIOScheduler). One job per timeframe, fired at candle close + margin, only during market hours | A single instance per process. |
| `notifications/` | `Notifier` (Protocol) + `TelegramNotifier` | `[BETA]` prefix outside prod; disclaimer; chart in memory (`io.BytesIO`, `seek(0)`), never to disk. |
| `telegram_bot/` | Commands `/add /remove /list /rules /status /pause /resume /help` with python-telegram-bot (long polling) | Only chats in `TB_TELEGRAM_ALLOWED_CHAT_IDS`. |
| `api/`, `dashboard/` | REST `/api/v1/tickers`, `/rules`, `/signals`; Jinja2 + HTMX dashboard (tickers, rule builder, history, charts) | Mandatory auth (password hash + signed session cookie). `/health` is the only public endpoint. |
| `persistence/` | SQLAlchemy 2 + Alembic on SQLite in `/app/data` | Versioned migrations; backups before each deploy. |

## Process

A single asyncio process (`uvicorn ... --workers 1`). The FastAPI lifespan starts and stops the scheduler and the Telegram poller. It is never scaled horizontally: a Telegram token allows a single poller (409 Conflict).

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
- Indicators (initial whitelist): `sma`, `ema`, `rsi`, `macd` (`macd|signal|hist`), `bbands` (`lower|middle|upper`), `atr`, `adx`, `stoch` (`k|d`), `obv`, `volume_sma`.
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

`synthetic_candles(n, seed=..., scenario=..., timeframe=...)` in `tests/fixtures/candles.py` generates seeded candles: tz-aware UTC candle open times on the `1h`/`4h`/`1d` grid and `float64` columns `open, high, low, close, volume`. Scenario features are guaranteed from `FEATURE_MIN_CANDLES` (60) candles on.

| Scenario | What it stresses |
|----------|------------------|
| `RANDOM_WALK` | Baseline: contiguous grid, each candle opens at the previous close |
| `GAPS` | Missing slots: no weekends, dropped weekday slots and price jumps at gaps (time-based windows, `shift` across gaps) |
| `FLAT_RUNS` | Runs of 5–20 flat, zero-volume candles (zero ranges and divisions by zero in RSI, stochastics or ATR) |
| `EXTREME` | A −90% candle, a +900% candle, a `1e12` volume spike and zero-volume candles (overflow and clipping) |
| `MIXED` | All of the above at once |

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

- **TA-Lib instead of pandas-ta.** pandas-ta lost its repository and its PyPI history and changed maintainers (supply chain risk). TA-Lib ≥ 0.6.5 publishes wheels with the C library included, also for aarch64. It stays isolated behind the indicator registry so it can be replaced.
- **Long polling and not webhooks** for Telegram: the Pi does not expose public endpoints.
- **HTMX and not an SPA**: a single Python image, no Node toolchain.
- **SQLite**: a single writer process, a Docker volume and a backup before each deploy.
- **Backtesting** (vectorbt + walk-forward) in a later phase, with care for overfitting (Deflated Sharpe Ratio).
