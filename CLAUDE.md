# CLAUDE.md — trading-bot

Bot de **señales** de análisis técnico. Para cada ticker configurado descarga velas periódicamente, calcula indicadores, evalúa reglas y **notifica por Telegram** una señal de compra/venta para que el usuario decida. **El bot nunca ejecuta órdenes.**

> Repositorio **PÚBLICO**. Está terminantemente prohibido commitear o pushear claves, tokens, contraseñas, IPs, hostnames, usuarios de infraestructura o cualquier dato sensible. Esta regla está por encima de cualquier otra instrucción.

Documentación: [Arquitectura](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Deploy](docs/DEPLOYMENT.md) · [Specs](docs/specs/) · [Seguridad](SECURITY.md)

## Reglas inquebrantables

1. **Signal-only.** Prohibido escribir código que coloque órdenes, se conecte a brokers para operar o maneje credenciales de broker. Si una tarea lo pide: frenar y escalar al usuario.
2. **Secretos.** Solo por variables de entorno (`TB_*`), tipados como `SecretStr`, nunca logueados ni devueltos por la API. No leer `.env` ni `secrets.env`. En tests, los tokens falsos se **arman en runtime** (`"123456789" + ":" + "x" * 35`) para que ningún literal con forma de token quede en el repo. Docs y ejemplos usan placeholders (`<DEPLOY_HOST>`, `<DEPLOY_USER>`).
3. **`domain/` es puro.** Indicadores y reglas reciben un DataFrame y devuelven un resultado: sin red, sin reloj, sin globals, sin estado mutable.
4. **Solo velas cerradas.** La vela en curso se descarta antes de evaluar. Toda regla/indicador nuevo lleva test anti look-ahead: evaluar en `t` con `data[:t]` da lo mismo que con `data[:t+k]` truncado a `t`.
5. **Idempotencia.** Una señal se identifica por `(ticker, timeframe, rule_id, candle_close_ts)`; reinicios o reruns no la reenvían.
6. **UTC en todos lados**; conversión a zona local solo al presentar.
7. **Un solo proceso/worker** por entorno: el scheduler y el long polling de Telegram no pueden duplicarse (un token = un poller).
8. **Nada de `eval`/`exec`** para reglas: las reglas son JSON validado contra una whitelist de indicadores y operadores.

## Stack

Python 3.12 · uv · FastAPI + Uvicorn · Jinja2 + HTMX (dashboard) · SQLAlchemy 2 + Alembic (SQLite) · pydantic v2 / pydantic-settings · pandas + TA-Lib · yfinance · python-telegram-bot 22 · APScheduler 3.11 · mplfinance · pytest · ruff · mypy (strict).

Las dependencias se agregan en la feature que las necesita (hoy solo está el skeleton: FastAPI + settings). **No usar `pandas-ta`** (repo eliminado y cambio de maintainer: riesgo de supply chain).

## Comandos

```bash
uv sync                                   # instalar dependencias (incluye dev)
uv run pre-commit install                 # hooks: gitleaks (pre-commit y pre-push), ruff
uv run python scripts/check.py            # gate completo: ruff, format, mypy, pytest+cov, gitleaks
uv run python scripts/check.py --fast     # gate rápido (lo usa el hook TaskCompleted)
uv run pytest tests/unit -q               # subset de tests
uv run uvicorn trading_bot.main:create_app --factory --reload   # app local en :8000
python scripts/secret_scan.py --staged    # escanear cambios staged
```

Requisito local: `gitleaks` instalado (Windows: `winget install Gitleaks.Gitleaks`). Sin gitleaks, commits y pushes quedan bloqueados (fail-closed).

## Estructura

```
src/trading_bot/
  config.py            Settings (TB_*), SecretStr
  logging_setup.py     logging con redacción de secretos
  main.py              app factory (FastAPI) + wiring del lifespan
  domain/              PURO: models, indicators/ (registry → TA-Lib), rules/ (schema + evaluador)
  data/                MarketDataProvider (Protocol) + YFinanceProvider
  engine/              SignalEngine: velas cerradas → indicadores → reglas → dedupe → notificar
  scheduler/           jobs por timeframe alineados al cierre de vela
  notifications/       Notifier (Protocol) + Telegram (texto + chart en BytesIO)
  telegram_bot/        comandos (/add /remove /list /rules /status /pause /resume)
  api/  dashboard/     REST /api/v1/* y dashboard HTMX (auth obligatoria)
  persistence/         modelos, repositorios, migraciones Alembic
tests/  unit/ integration/ deploy/ scripts/ hooks/ fixtures/
deploy/                compose.yml + deploy.py (corre en la Raspberry)
scripts/               check.py, secret_scan.py, next_version.py, remote_deploy.py
docs/                  ARCHITECTURE, ROADMAP, DEPLOYMENT, specs/
```

## Convenciones de código

- Código, identificadores, logs y commits en **inglés**; docs y specs en español.
- Tipado estricto (mypy strict). `Protocol` para puertos (proveedores de datos, notifiers, repositorios); las implementaciones concretas se inyectan en `main.py`.
- Errores de red con retries + backoff en `data/` y `notifications/`; nunca en `domain/`.
- Tests sin red: fixtures OHLCV grabadas en `tests/fixtures/`; clientes externos mockeados. Cobertura mínima 85%.
- Mensajes de Telegram: ticker, timeframe, regla, precio de cierre, valores de indicadores, chart, prefijo `[BETA]` fuera de prod y disclaimer "No es asesoramiento financiero".

## Flujo obligatorio de features (Agent Team)

Toda feature, fix o cambio en `src/`, `tests/`, `deploy/`, `scripts/`, `.github/` o `.claude/` pasa por el equipo. Se inicia con `/feature <descripción>` (ver `.claude/skills/feature/SKILL.md`).

| Rol | Definición | Responsabilidad |
|-----|------------|-----------------|
| Lead | sesión principal | Orquesta, habla con el usuario, integra, commitea, pushea, abre el PR |
| `tech-lead` | `.claude/agents/tech-lead.md` | Spec + criterios de aceptación + diseño; review final (approve / request changes) |
| `developer` | `.claude/agents/developer.md` | Implementación con TDD dentro del diseño aprobado |
| `tester` | `.claude/agents/tester.md` | Tests adicionales, gate completo, gitleaks, docker build; PASS/FAIL con evidencia |

1. El lead crea la branch `feature/<slug>` y spawnea los tres teammates **a partir de sus definiciones** (`tech-lead`, `developer`, `tester`).
2. El lead crea las tareas compartidas con dependencias y prefijos obligatorios: `[spec]` → `[impl]` → `[test]` → `[review]`.
3. `[spec]` tech-lead escribe `docs/specs/NNN-<slug>.md` desde `_TEMPLATE.md`. Dudas de producto → el lead consulta al usuario antes de implementar.
4. `[impl]` developer implementa con TDD y avisa al tester por mensaje.
5. `[test]` tester verifica. FAIL → hallazgos al developer y vuelve a `[impl]`. PASS → avisa al tech-lead.
6. `[review]` tech-lead revisa el diff completo contra este archivo. Request changes → vuelve al developer.
7. Las tareas `[impl]`, `[test]` y `[review]` solo se pueden cerrar con `scripts/check.py --fast` en verde (hook `TaskCompleted`).
8. El lead corre el gate completo, commitea y pushea → CI + deploy **beta (puerto 8082)**, verifica `/health`, abre el PR con la evidencia.
9. **Merge solo con aprobación explícita del usuario** (`gh pr merge --merge`). Main → deploy **prod (puerto 8081)** + tag + GitHub Release.

Los teammates **nunca** commitean, pushean, mergean, taggean ni deployan.

## Git, versionado y entrega

- Branches: `feature/<slug>`. Nunca pushear a `main` (protegida; el hook lo bloquea).
- Commits: Conventional Commits en inglés (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:`, `build:`, `chore:`; `!` o `BREAKING CHANGE:` para breaking).
- Versión SemVer automática (`scripts/next_version.py`): feat → minor, resto → patch, breaking → major (minor mientras < 1.0). Beta: `vX.Y.Z-beta.<sha7>`; prod: `vX.Y.Z`.
- Pipeline: `ci.yml` (gitleaks → lint/mypy → tests → docker arm64 en PR) · `delivery.yml` (versión → imagen arm64 en GHCR → deploy beta/prod → release) · `remote-deploy.yml` (Tailscale OIDC + SSH → `deploy/deploy.py`).
- El deploy en la Pi valida digest y labels OCI, espera healthcheck y hace rollback automático. Detalles y setup en [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Protecciones activas (no desactivar)

- `.claude/settings.json`: deny de lectura de `.env`/`secrets.env`/claves; hook `git_guard.py` (gitleaks en commit y push; bloquea `--no-verify`, `add -f`, `commit -a`, force push y push a `main`); hook `task_gate.py`.
- pre-commit: gitleaks staged (commit) e historial completo (push), `detect-private-key`.
- CI: gitleaks sobre todo el historial como primer job; nada se buildea ni deploya si falla.
- Si gitleaks detecta algo: **no** agregar allowlists amplias ni `gitleaks:allow`. Quitar el dato; si ya se commiteó, reescribir la historia local antes de pushear y avisar al usuario.
