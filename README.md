# trading-bot

Technical analysis bot that watches candles of configurable tickers, evaluates user-defined rules and sends **buy/sell signals via Telegram** so the user can decide whether to trade.

> **Not financial advice.** The bot only emits signals: it never executes orders or connects to brokers to trade.

## Status

In development. See the [roadmap](docs/ROADMAP.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deploy and CI/CD](docs/DEPLOYMENT.md)
- [Security](SECURITY.md)
- [Guide for agents / contributing](CLAUDE.md)

## Quick development

```sh
uv sync
uv run pre-commit install
uv run python scripts/check.py
uv run uvicorn trading_bot.main:create_app --factory --reload
```

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and [gitleaks](https://github.com/gitleaks/gitleaks).

## License

[MIT](LICENSE)
