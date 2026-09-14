# Roadmap

Each item is a feature that goes through the agent team (`/feature`) and ends in its own release. Order matters: each phase builds on the previous one.

| Phase | Feature | Scope | Key criteria |
|-------|---------|-------|--------------|
| F0 | Foundations | CLAUDE.md, agents, hooks, secret protection, beta/prod CI/CD, `/health` skeleton | Green pipeline; beta on 8082 and prod on 8081 |
| F1 | Domain and indicators | `domain/models.py`, indicator registry (TA-Lib), rule schema and pure evaluator | Tests against reference values; anti look-ahead; invalid rules rejected |
| F2 | Data provider | `MarketDataProvider` + `YFinanceProvider`: UTC normalization, open candle discarded, retries | Tests with recorded fixtures and no network; intraday limits respected |
| F3 | Persistence | SQLAlchemy + Alembic: tickers, rules, signals, state | Reversible migrations; idempotency unique constraint |
| F4 | Engine and scheduler | `SignalEngine` + APScheduler per timeframe and market hours | No duplicate signals after restart; cooldown |
| F5 | Telegram | Notifier (text + chart in BytesIO) and commands with a chat allowlist | `[BETA]` outside prod; disclaimer; unauthorized chats ignored |
| F6 | API and dashboard | REST `/api/v1/*`, login, HTMX dashboard: tickers, rule builder, history, charts | Mandatory auth; rule validation in UI and backend |
| F7 | Operations | Daily heartbeat, error alerts via Telegram, global `/pause`, metrics in `/status` | Heartbeat silence detectable |
| F8 | Backtesting | Rule backtesting (vectorbt) with walk-forward and Deflated Sharpe Ratio in the dashboard | In-sample/out-of-sample separation; costs included |
