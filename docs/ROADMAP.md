# Roadmap

Cada ítem es una feature que pasa por el agent team (`/feature`) y termina en su propia release. El orden importa: cada fase se apoya en la anterior.

| Fase | Feature | Estado | Alcance | Criterios clave |
|------|---------|--------|---------|-----------------|
| F0 | Fundaciones | Completada (v0.1.0) | CLAUDE.md, agentes, hooks, protección de secretos, CI/CD beta/prod, skeleton `/health` | Pipeline verde; beta en 8082 y prod en 8081 |
| F1 | Dominio e indicadores | Pendiente | `domain/models.py`, registry de indicadores (TA-Lib), schema de reglas y evaluador puro | Tests contra valores de referencia; anti look-ahead; reglas inválidas rechazadas |
| F2 | Proveedor de datos | Pendiente | `MarketDataProvider` + `YFinanceProvider`: normalización UTC, descarte de vela abierta, retries | Tests con fixtures grabadas sin red; límites de intradía respetados |
| F3 | Persistencia | Pendiente | SQLAlchemy + Alembic: tickers, reglas, señales, estado | Migraciones reversibles; unique de idempotencia |
| F4 | Motor y scheduler | Pendiente | `SignalEngine` + APScheduler por timeframe y horario de mercado | Sin señales duplicadas tras reinicio; cooldown |
| F5 | Telegram | Pendiente | Notifier (texto + chart en BytesIO) y comandos con allowlist de chats | `[BETA]` fuera de prod; disclaimer; chats no autorizados ignorados |
| F6 | API y dashboard | Pendiente | REST `/api/v1/*`, login, dashboard HTMX: tickers, constructor de reglas, historial, charts | Auth obligatoria; validación de reglas en UI y backend |
| F7 | Operación | Pendiente | Heartbeat diario, alertas de error por Telegram, `/pause` global, métricas en `/status` | Silencio del heartbeat detectable |
| F8 | Backtesting | Pendiente | Backtest de reglas (vectorbt) con walk-forward y Deflated Sharpe Ratio en el dashboard | Separación in-sample/out-of-sample; costos incluidos |
