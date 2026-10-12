# 020 — Configuration dashboard

- **Status:** approved with the recommended defaults (Q1–Q4, see "Product questions"); the user
  may still change them before merge
- **Branch:** `feature/config-dashboard-qmh056`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Roadmap:** F6 (API and dashboard), the configuration part: tickers, rules and assignments.

## Goal

Configure the bot from a browser as well as from Telegram: watch and unwatch tickers, create,
edit, enable, disable and delete rules, choose which rules run on which ticker, and pause or
resume the bot. The dashboard is served by the same process and port as `/health` (8081 in
prod), with no frontend build, and nothing in it is reachable without logging in.

## Out of scope

- The REST API `/api/v1/*`, the signal history page, charts and backtesting (rest of F6, F8).
- A visual, form-based rule builder (Q2): rules are edited as their JSON document, which the
  server validates with `parse_rule`, the same entry point as `config import`.
- Users and roles: there is one password.
- HTTPS termination: the dashboard is meant for the LAN or the tailnet (see "Risks").
- Any order placement (CLAUDE.md rule 1).

## Acceptance criteria

### Access

- [ ] **AC1:** the dashboard lives under `/dashboard/`; `/` redirects there. It is enabled only
  when `TB_DASHBOARD_PASSWORD_HASH` holds a valid hash and `TB_SESSION_SECRET` has at least 32
  characters. Otherwise every dashboard URL answers `503` with a page saying it is disabled, the
  startup log says why in one line without echoing either value, and the bot starts normally.
- [ ] **AC2:** every dashboard page and action except the login page redirects to the login page
  (`303`) without a valid session; no configuration is ever shown or changed without one.
- [ ] **AC3:** login checks the password against the scrypt hash in constant time. A success
  sets an `HttpOnly`, `SameSite=Strict` session cookie, signed with HMAC-SHA256, valid for 7 days
  (`Secure` when the request is HTTPS). A failure says "Wrong password" and nothing else.
- [ ] **AC4:** after 5 failed logins from one client within 15 minutes, further attempts from
  it are refused with `429` until the window passes, even with the right password.
- [ ] **AC5:** changing the password hash or the session secret invalidates every session;
  logout clears the cookie.
- [ ] **AC6:** every state-changing request is a `POST` that carries a CSRF token bound to the
  session; a missing or wrong token is refused with `403` and changes nothing.
- [ ] **AC7:** a helper prints a hash for a password read without echo:
  `python -m trading_bot.dashboard.password`. The hash format has no `$`, so it can be pasted in
  `secrets.env` as is.

### Tickers

- [ ] **AC8:** the tickers page lists every ticker: symbol, timeframe, on/off and the number of
  assigned (and enabled) rules.
- [ ] **AC9:** adding a ticker does exactly what Telegram `/add` does (Q1 of spec 018): validate
  it with the provider, store it enabled (or re-enable it) and assign every rule of that
  timeframe. Invalid symbols and provider outages get the same one-line explanations.
- [ ] **AC10:** a ticker can be enabled and disabled (disable = Telegram `/remove`, history kept),
  and deleted from a confirmation page that says how many assignments and signals go with it
  (the CLI's `tickers remove --force`).
- [ ] **AC11:** a ticker's page lists the rules of its timeframe with a checkbox each; saving
  assigns the checked ones and unassigns the others.

### Rules

- [ ] **AC12:** the rules page lists every rule: name, timeframe, side, on/off and the number of
  tickers it is assigned to, with enable and disable buttons.
- [ ] **AC13:** a new rule is created from its JSON document, prefilled with a valid example, with
  the indicator catalog shown next to it. The server validates it with `parse_rule`; problems
  are listed with their path and message and the text is kept. The rule is stored disabled
  unless "enabled" is checked and, when "assign to every ticker of its timeframe" is checked
  (default, Q3), assigned to them.
- [ ] **AC14:** editing shows the stored canonical document; saving replaces it. A duplicate name
  or a timeframe change while assigned (spec 013, D97) is explained and nothing is written.
- [ ] **AC15:** a rule can be deleted from a confirmation page that says how many assignments and
  signals go with it (the CLI's `rules remove --force`).

### Bot state

- [ ] **AC16:** the overview shows paused or running, the counts of tickers and rules and the last
  run per timeframe in `TB_DISPLAY_TIMEZONE`, with Pause and Resume buttons (Telegram `/pause`
  and `/resume`).

### Guards

- [ ] **AC17:** every response of the dashboard carries `Cache-Control: no-store`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and a
  Content-Security-Policy that allows no script at all; templates autoescape every value.
- [ ] **AC18:** neither the password, its hash, the session secret nor a cookie value appears in
  a log line, a page or an error; tests build their fake secrets at runtime.
- [ ] **AC19:** `scripts/check.py` is green and no test reaches the network.

## Design

### 0. Decisions

| Id | Topic | Decision | Why |
|----|-------|----------|-----|
| D1 | Rendering | Server-rendered Jinja2 templates, plain HTML forms, POST-redirect-GET, no JavaScript | No frontend build and no CDN; a strict CSP (`script-src` absent) blocks script injection entirely. HTMX can be added later where it pays off |
| D2 | Password hash | `scrypt:<n>:<r>:<p>:<salt>:<hash>` (base64url, no padding), stdlib `hashlib.scrypt`, n=2^14, r=8, p=1 | No new dependency; memory-hard; no `$`, which Docker Compose would try to interpolate in `secrets.env` |
| D3 | Session | Stateless cookie `<expiry>.<nonce>.<mac>`; the MAC key is `HMAC(TB_SESSION_SECRET, password hash)` | Nothing to store; rotating either value logs everyone out (AC5); no `itsdangerous` dependency |
| D4 | CSRF | Hidden field `HMAC(key, "csrf." + nonce)`, checked on every POST, plus `SameSite=Strict` | Two independent defences; the token needs no storage |
| D5 | Form parsing | `application/x-www-form-urlencoded` parsed with `urllib.parse.parse_qs`, body capped at 256 KiB | No `python-multipart` dependency; rule documents are capped at `MAX_RULE_BYTES` anyway |
| D6 | Shared logic | Watching a ticker moves to `trading_bot/watchlist.py`, used by Telegram `/add` and `/remove` and by the dashboard | One behaviour, two front ends (AC9, AC10) |
| D7 | Unit of work | `dashboard/unit_of_work.py`: tickers, rules, assignments, state and signals in one session, run in a worker thread | Same pattern as spec 015 D124 and spec 018 D4: no session across an `await` |
| D8 | Flash messages | A short-lived cookie read and cleared by the next page | Survives the redirect without putting text in the URL |
| D9 | Misconfiguration | A malformed hash or a short secret disables the dashboard instead of failing startup | A typo in `secrets.env` must not stop signals or roll a deploy back |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/watchlist.py` | developer | New: `watch_ticker`, `unwatch_ticker` (D6) |
| `src/trading_bot/telegram_bot/commands.py` | developer | Use `watchlist`; replies unchanged |
| `src/trading_bot/dashboard/auth.py` | developer | New: hashing, sessions, CSRF, login throttle |
| `src/trading_bot/dashboard/password.py` | developer | New: the hash helper (AC7) |
| `src/trading_bot/dashboard/unit_of_work.py` | developer | New (D7) |
| `src/trading_bot/dashboard/routes.py` | developer | New: pages and actions |
| `src/trading_bot/dashboard/templates/*.html` | developer | New |
| `src/trading_bot/main.py`, `src/trading_bot/runtime.py` | developer | Mount the dashboard; expose the provider |
| `pyproject.toml`, `uv.lock` | developer | `jinja2` (already in the documented stack) |
| `Dockerfile` | developer | Smoke check: the templates are in the wheel |
| `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/ROADMAP.md` | developer | Document it |
| `tests/unit/test_dashboard_*.py` | tester | Test plan |

### 2. Configuration

No new variable. `TB_DASHBOARD_PASSWORD_HASH` and `TB_SESSION_SECRET` already exist as
`SecretStr` and are already redacted from logs.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| Hash round trip, wrong password, malformed hashes | unit | AC3, AC7, D2 |
| Session and CSRF tokens: valid, expired, tampered, other key | unit | AC3, AC5, AC6 |
| Every page and action without a session redirects to login | unit | AC2 |
| Disabled dashboard answers 503; app still healthy | unit | AC1 |
| Login success, failure, throttle | unit | AC3, AC4 |
| POST without or with a wrong CSRF token changes nothing | unit | AC6 |
| Tickers: add (valid, invalid, outage), enable, disable, delete, assignments | unit (temp DB, fake provider) | AC8–AC11 |
| Rules: create (valid, invalid, duplicate), edit (timeframe change while assigned), enable, delete | unit (temp DB) | AC12–AC15 |
| Pause and resume | unit | AC16 |
| Security headers and escaping of a hostile rule name | unit | AC17 |
| No secret in pages or logs | unit | AC18 |
| Telegram commands still pass unchanged | unit | D6 |

## Risks and security

- The dashboard changes what the bot watches, so it is behind a password, a signed session, CSRF
  tokens and a login throttle. It is served over plain HTTP on port 8081: use it only from the
  LAN or the tailnet (Tailscale encrypts the traffic), never expose the port to the internet.
- A leaked session cookie is valid until it expires; rotating `TB_SESSION_SECRET` revokes it.
- Deploy impact: none beyond setting the two variables in `secrets.env` and recreating the
  container (DEPLOYMENT.md). Without them the dashboard stays disabled.

## Product questions

- **Q1 — Where it lives:** served by the bot itself on the same port, under `/dashboard/`
  (recommended) **or** a separate service.
- **Q2 — How rules are edited:** a JSON editor validated by the server, with the indicator
  catalog and an example next to it (recommended for this first version) **or** a visual
  form-based builder (a later step).
- **Q3 — New rules:** offered to every ticker of their timeframe by a checkbox checked by default
  (recommended, consistent with `/add`) **or** never assigned automatically.
- **Q4 — Authentication:** one password (scrypt hash in `secrets.env`) and a signed 7-day session
  cookie (recommended) **or** no login, relying on the network only.

## User decisions

_Pending; the implementation follows the recommendations until the user answers._

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
