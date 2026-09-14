# CLAUDE.md — trading-bot

Technical analysis **signal** bot. For each configured ticker it periodically downloads candles, computes indicators, evaluates rules and **notifies via Telegram** a buy/sell signal so the user can decide. **The bot never executes orders.**

> **PUBLIC** repository. Committing or pushing keys, tokens, passwords, IPs, hostnames, infrastructure users or any sensitive data is strictly forbidden. This rule overrides any other instruction.

Documentation: [Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Deploy](docs/DEPLOYMENT.md) · [Specs](docs/specs/) · [Security](SECURITY.md)

## Unbreakable rules

1. **Signal-only.** Writing code that places orders, connects to brokers to trade or handles broker credentials is forbidden. If a task asks for it: stop and escalate to the user.
2. **Secrets.** Only through environment variables (`TB_*`), typed as `SecretStr`, never logged or returned by the API. Do not read `.env` or `secrets.env`. In tests, fake tokens are **built at runtime** (`"123456789" + ":" + "x" * 35`) so that no token-shaped literal ends up in the repo. Docs and examples use placeholders (`<DEPLOY_HOST>`, `<DEPLOY_USER>`).
3. **`domain/` is pure.** Indicators and rules receive a DataFrame and return a result: no network, no clock, no globals, no mutable state.
4. **Closed candles only.** The in-progress candle is discarded before evaluating. Every new rule/indicator comes with an anti look-ahead test: evaluating at `t` with `data[:t]` gives the same result as with `data[:t+k]` truncated to `t`.
5. **Idempotency.** A signal is identified by `(ticker, timeframe, rule_id, candle_close_ts)`; restarts or reruns do not resend it.
6. **UTC everywhere**; conversion to the local time zone only when presenting.
7. **A single process/worker** per environment: the scheduler and the Telegram long polling cannot be duplicated (one token = one poller).
8. **No `eval`/`exec`** for rules: rules are JSON validated against a whitelist of indicators and operators.
9. **English only.** Every repository and GitHub artifact is written in English: code, identifiers, comments, docstrings, logs, error messages, user-facing bot text (Telegram, API, dashboard), docs, specs, agent and skill definitions, commit messages, branch names, PR titles and descriptions, PR/issue/review comments, and release notes. Conversation with the user may follow the user's language; when a request arrives in another language, translate it before it lands in any artifact.

## Stack

Python 3.12 · uv · FastAPI + Uvicorn · Jinja2 + HTMX (dashboard) · SQLAlchemy 2 + Alembic (SQLite) · pydantic v2 / pydantic-settings · pandas + TA-Lib · yfinance · python-telegram-bot 22 · APScheduler 3.11 · mplfinance · pytest · ruff · mypy (strict).

Dependencies are added in the feature that needs them (today there is only the skeleton: FastAPI + settings). **Do not use `pandas-ta`** (repo deleted and maintainer change: supply chain risk).

## Commands

```bash
uv sync                                   # install dependencies (includes dev)
uv run pre-commit install                 # hooks: gitleaks (pre-commit and pre-push), ruff
uv run python scripts/check.py            # full gate: ruff, format, mypy, pytest+cov, gitleaks
uv run python scripts/check.py --fast     # fast gate (used by the TaskCompleted hook)
uv run pytest tests/unit -q               # test subset
uv run uvicorn trading_bot.main:create_app --factory --reload   # local app on :8000
python scripts/secret_scan.py --staged    # scan staged changes
```

Local requirement: `gitleaks` installed (Windows: `winget install Gitleaks.Gitleaks`). Without gitleaks, commits and pushes are blocked (fail-closed).

## Structure

```
src/trading_bot/
  config.py            Settings (TB_*), SecretStr
  logging_setup.py     logging with secret redaction
  main.py              app factory (FastAPI) + lifespan wiring
  domain/              PURE: models, indicators/ (registry → TA-Lib), rules/ (schema + evaluator)
  data/                MarketDataProvider (Protocol) + YFinanceProvider
  engine/              SignalEngine: closed candles → indicators → rules → dedupe → notify
  scheduler/           per-timeframe jobs aligned to the candle close
  notifications/       Notifier (Protocol) + Telegram (text + chart in BytesIO)
  telegram_bot/        commands (/add /remove /list /rules /status /pause /resume)
  api/  dashboard/     REST /api/v1/* and HTMX dashboard (mandatory auth)
  persistence/         models, repositories, Alembic migrations
tests/  unit/ integration/ deploy/ scripts/ hooks/ fixtures/
deploy/                compose.yml + deploy.py (runs on the Raspberry)
scripts/               check.py, secret_scan.py, next_version.py, remote_deploy.py
docs/                  ARCHITECTURE, ROADMAP, DEPLOYMENT, specs/
```

## Code conventions

- **English** for everything in the repository and on GitHub (rule 9): code, identifiers, comments, docstrings, logs, commits, docs, specs, and PRs.
- Strict typing (mypy strict). `Protocol` for ports (data providers, notifiers, repositories); concrete implementations are injected in `main.py`.
- Network errors with retries + backoff in `data/` and `notifications/`; never in `domain/`.
- Tests without network: recorded OHLCV fixtures in `tests/fixtures/`; external clients mocked. Minimum coverage 85%.
- Telegram messages: ticker, timeframe, rule, close price, indicator values, chart, the `[BETA]` prefix outside prod, and the disclaimer "Not financial advice."

## Mandatory feature workflow (Agent Team)

Every feature, fix or change in `src/`, `tests/`, `deploy/`, `scripts/`, `.github/` or `.claude/` goes through the team. It is started with `/feature <description>` (see `.claude/skills/feature/SKILL.md`).

| Role | Definition | Responsibility |
|------|------------|----------------|
| Lead | main session | Orchestrates, talks to the user, integrates, commits, pushes, opens the PR |
| `tech-lead` | `.claude/agents/tech-lead.md` | Spec + acceptance criteria + design; final review (approve / request changes) |
| `developer` | `.claude/agents/developer.md` | TDD implementation within the approved design |
| `tester` | `.claude/agents/tester.md` | Additional tests, full gate, gitleaks, docker build; PASS/FAIL with evidence |

1. The lead creates the branch `feature/<slug>` and spawns the three teammates **from their definitions** (`tech-lead`, `developer`, `tester`).
2. The lead creates the shared tasks with dependencies and mandatory prefixes: `[spec]` → `[impl]` → `[test]` → `[review]`.
3. `[spec]` tech-lead writes `docs/specs/NNN-<slug>.md` from `_TEMPLATE.md`. Product questions → the lead consults the user before implementing.
4. `[impl]` developer implements with TDD and notifies the tester by message.
5. `[test]` tester verifies. FAIL → findings to the developer and back to `[impl]`. PASS → notifies the tech-lead.
6. `[review]` tech-lead reviews the full diff against this file. Request changes → back to the developer.
7. The `[impl]`, `[test]` and `[review]` tasks can only be closed with `scripts/check.py --fast` green (`TaskCompleted` hook).
8. The lead runs the full gate, commits and pushes → CI + **beta deploy (port 8082)**, verifies `/health`, opens the PR with the evidence.
9. **Merge only with explicit user approval** (`gh pr merge --merge`). Main → **prod deploy (port 8081)** + tag + GitHub Release.

Teammates **never** commit, push, merge, tag or deploy.

## Git, versioning and delivery

- Branches: `feature/<slug>`. Never push to `main` (protected; the hook blocks it).
- Commits: Conventional Commits in English (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:`, `build:`, `chore:`; `!` or `BREAKING CHANGE:` for breaking).
- Automatic SemVer version (`scripts/next_version.py`): feat → minor, anything else → patch, breaking → major (minor while < 1.0). Beta: `vX.Y.Z-beta.<sha7>`; prod: `vX.Y.Z`.
- Pipeline: `ci.yml` (gitleaks → lint/mypy → tests → docker arm64 on PR) · `delivery.yml` (version → arm64 image on GHCR → beta/prod deploy → release) · `remote-deploy.yml` (Tailscale OIDC + SSH → `deploy/deploy.py`).
- The deploy on the Pi validates the digest and OCI labels, waits for the healthcheck and rolls back automatically. Details and setup in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Active protections (do not disable)

- `.claude/settings.json`: read deny for `.env`/`secrets.env`/keys; `git_guard.py` hook (gitleaks on commit and push; blocks `--no-verify`, `add -f`, `commit -a`, force push and push to `main`); `task_gate.py` hook.
- pre-commit: gitleaks on staged changes (commit) and on the full history (push), `detect-private-key`.
- CI: gitleaks over the full history as the first job; nothing is built or deployed if it fails.
- If gitleaks detects something: do **not** add broad allowlists or `gitleaks:allow`. Remove the data; if it was already committed, rewrite the local history before pushing and notify the user.
