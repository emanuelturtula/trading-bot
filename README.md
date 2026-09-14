# trading-bot

Bot de análisis técnico que observa velas de tickers configurables, evalúa reglas definidas por el usuario y envía **señales de compra/venta por Telegram** para que el usuario decida si operar.

> **No es asesoramiento financiero.** El bot solo emite señales: nunca ejecuta órdenes ni se conecta a brokers para operar.

## Estado

En desarrollo. Ver el [roadmap](docs/ROADMAP.md).

## Documentación

- [Arquitectura](docs/ARCHITECTURE.md)
- [Deploy y CI/CD](docs/DEPLOYMENT.md)
- [Seguridad](SECURITY.md)
- [Guía para agentes / contribución](CLAUDE.md)

## Desarrollo rápido

```sh
uv sync
uv run pre-commit install
uv run python scripts/check.py
uv run uvicorn trading_bot.main:create_app --factory --reload
```

Requiere Python 3.12, [uv](https://docs.astral.sh/uv/) y [gitleaks](https://github.com/gitleaks/gitleaks).

## Licencia

[MIT](LICENSE)
