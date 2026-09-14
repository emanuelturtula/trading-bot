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
