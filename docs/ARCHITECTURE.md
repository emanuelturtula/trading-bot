# Arquitectura

## Objetivo

Para una lista de tickers configurables (acciones y ETFs vía yfinance en v1), el bot:

1. espera el cierre de cada vela según el timeframe del ticker (1h, 4h, 1d; default 1d);
2. descarga las velas OHLCV y descarta la vela en curso;
3. calcula indicadores técnicos;
4. evalúa las reglas asignadas al ticker;
5. si una regla dispara, persiste la señal y la notifica por Telegram con un chart.

El usuario decide si opera. **No existe capa de ejecución de órdenes** y no debe agregarse.

## Capas

```
            ┌──────────────┐    ┌──────────────┐
 Telegram ─▶│ telegram_bot │    │ api/dashboard│◀─ navegador (auth)
            └──────┬───────┘    └──────┬───────┘
                   │  comandos / CRUD  │
                   ▼                   ▼
            ┌─────────────────────────────────┐
            │ persistence (SQLite + Alembic)  │  tickers, reglas, señales
            └───────────────┬─────────────────┘
                            │
 scheduler ──tick──▶ ┌──────┴───────┐   fetch   ┌──────────────┐
 (cierre de vela)    │    engine    │──────────▶│    data      │──▶ yfinance
                     │ SignalEngine │           └──────────────┘
                     └──────┬───────┘
                            │ DataFrame (velas cerradas)
                            ▼
                     ┌──────────────┐
                     │   domain     │  PURO: indicadores + evaluador de reglas
                     └──────┬───────┘
                            │ Signal[]
                            ▼
                     ┌──────────────┐
                     │notifications │──▶ Telegram (texto + chart BytesIO)
                     └──────────────┘
```

| Capa | Responsabilidad | Reglas |
|------|-----------------|--------|
| `domain/` | Modelos (`Candle`, `Timeframe`, `Signal`, `Rule`), registry de indicadores (whitelist → TA-Lib), evaluador de reglas | Sin I/O, sin reloj, sin globals. Testeable con DataFrames fijos. |
| `data/` | `MarketDataProvider` (Protocol) y `YFinanceProvider`: normaliza a OHLCV UTC, descarta la vela abierta, retries con backoff | Nunca decide señales. Respeta los límites de Yahoo (intradía máx. 60 días; 1h hasta 730 días). |
| `engine/` | `SignalEngine`: orquesta fetch → indicadores → reglas → dedupe/cooldown → persistencia → notificación | Idempotente por `(ticker, timeframe, rule_id, candle_close_ts)`. |
| `scheduler/` | APScheduler (AsyncIOScheduler). Un job por timeframe, disparado al cierre de vela + margen, solo en horario de mercado | Una sola instancia por proceso. |
| `notifications/` | `Notifier` (Protocol) + `TelegramNotifier` | Prefijo `[BETA]` fuera de prod; disclaimer; chart en memoria (`io.BytesIO`, `seek(0)`), nunca a disco. |
| `telegram_bot/` | Comandos `/add /remove /list /rules /status /pause /resume /help` con python-telegram-bot (long polling) | Solo chats en `TB_TELEGRAM_ALLOWED_CHAT_IDS`. |
| `api/`, `dashboard/` | REST `/api/v1/tickers`, `/rules`, `/signals`; dashboard Jinja2 + HTMX (tickers, constructor de reglas, historial, charts) | Auth obligatoria (password hash + cookie de sesión firmada). `/health` es el único endpoint público. |
| `persistence/` | SQLAlchemy 2 + Alembic sobre SQLite en `/app/data` | Migraciones versionadas; backups antes de cada deploy. |

## Proceso

Un único proceso asyncio (`uvicorn ... --workers 1`). El lifespan de FastAPI arranca y detiene el scheduler y el poller de Telegram. Nunca se escala horizontalmente: un token de Telegram admite un solo poller (409 Conflict).

## Modelo de reglas

Las reglas se definen desde el dashboard y se guardan como JSON validado con pydantic. No hay `eval`.

```json
{
  "name": "RSI sobrevendido en tendencia alcista",
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

- Operandos: `{"indicator", "params", "output"?}` · `{"price": "open|high|low|close|volume"}` · `{"value": number}`.
- Operadores: `<`, `<=`, `>`, `>=`, `crosses_above`, `crosses_below`.
- Grupos: `all` / `any`, anidables hasta 2 niveles.
- Indicadores (whitelist inicial): `sma`, `ema`, `rsi`, `macd` (`macd|signal|hist`), `bbands` (`lower|middle|upper`), `atr`, `adx`, `stoch` (`k|d`), `obv`, `volume_sma`.
- Cada indicador declara sus parámetros permitidos y rangos; el evaluador rechaza cualquier cosa fuera de la whitelist.
- `cooldown_bars`: velas mínimas entre dos señales de la misma regla y ticker.
- Una regla se asigna a uno o más tickers.

## Datos persistidos (borrador)

- `tickers(id, symbol, timeframe, enabled, created_at)`
- `rules(id, name, signal, timeframe, definition_json, enabled, created_at, updated_at)`
- `ticker_rules(ticker_id, rule_id)`
- `signals(id, ticker_id, rule_id, timeframe, candle_close_ts, price, indicator_values_json, notified_at)` con unique `(ticker_id, rule_id, timeframe, candle_close_ts)`
- `bot_state(key, value)`: pausa global, último heartbeat

## Configuración (variables de entorno)

| Variable | Uso |
|----------|-----|
| `TB_ENVIRONMENT` | `dev` / `beta` / `prod` (lo fija compose en la Pi) |
| `TB_VERSION` | Versión inyectada en la imagen |
| `TB_LOG_LEVEL` | Nivel de log |
| `TB_TELEGRAM_BOT_TOKEN` | Secreto. Un bot distinto por entorno |
| `TB_TELEGRAM_ALLOWED_CHAT_IDS` | Chats autorizados (coma-separados) |
| `TB_DASHBOARD_PASSWORD_HASH` | Secreto. Hash del password del dashboard |
| `TB_SESSION_SECRET` | Secreto. Firma de cookies de sesión |

## Decisiones

- **TA-Lib en lugar de pandas-ta.** pandas-ta perdió su repositorio y su historial en PyPI y cambió de maintainer (riesgo de supply chain). TA-Lib ≥ 0.6.5 publica wheels con la librería C incluida, también para aarch64. Queda aislada detrás del registry de indicadores para poder reemplazarla.
- **Long polling y no webhooks** para Telegram: la Pi no expone endpoints públicos.
- **HTMX y no una SPA**: una sola imagen Python, sin toolchain de Node.
- **SQLite**: un solo proceso escritor, volumen Docker y backup previo a cada deploy.
- **Backtesting** (vectorbt + walk-forward) en una fase posterior, con cuidado por el overfitting (Deflated Sharpe Ratio).
