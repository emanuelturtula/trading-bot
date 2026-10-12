# 018 — Telegram commands and long polling

- **Status:** approved with the recommended defaults (Q1–Q3, see "Product questions"); the user
  may still change them before merge
- **Branch:** `feature/telegram-commands`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Issues:** #19 (Telegram commands with a chat allowlist) and #20 (long polling in the
  lifespan and beta/prod bot setup), delivered together as #16 and #17 were (spec 017, D1).

## Goal

Manage the bot from the phone: watch and unwatch symbols, see the rules, see what the bot is
doing, and pause or resume it, without SSH and the management CLI. Only the chats in
`TB_TELEGRAM_ALLOWED_CHAT_IDS` can do anything.

## Out of scope

- **Creating or editing rules** from Telegram (#19: rules belong to the dashboard, #24). The CLI
  remains the way to load them until then.
- **The chart** (#18), heartbeat and alerts (F7), the dashboard and the API (F6).
- **Webhooks**: the Pi exposes no public endpoint (ARCHITECTURE, "Long polling and not
  webhooks").
- Any order placement (CLAUDE.md rule 1): no command touches a broker.

## Acceptance criteria

### Commands (#19)

- [ ] **AC1:** `/start` and `/help` list the commands with one line each.
- [ ] **AC2:** `/add <ticker> [timeframe]` validates the symbol with the provider
  (`validate_ticker`), defaults the timeframe to `1d`, stores the ticker enabled, and assigns every
  stored rule of that timeframe (Q1). Re-adding a disabled ticker re-enables it. The reply names
  the symbol, the instrument name, the timeframe and how many rules were assigned, and warns when
  none is enabled. An unknown symbol, a malformed one or an unknown timeframe gets a one-line
  explanation; a provider outage says "try again later".
- [ ] **AC3:** `/remove <ticker> [timeframe]` disables the ticker and keeps its assignments and
  signal history (Q2). Without a timeframe it disables every timeframe of that symbol.
- [ ] **AC4:** `/list` shows every ticker: symbol, timeframe, enabled or disabled, and the number
  of enabled rules assigned.
- [ ] **AC5:** `/rules` shows every rule: name, timeframe, side, enabled or disabled, and the
  number of tickers it is assigned to.
- [ ] **AC6:** `/status` shows the environment and version, paused or running (with the pause
  instant), the number of enabled tickers, and per timeframe the last completed run and the next
  scheduled run, all in `TB_DISPLAY_TIMEZONE`.
- [ ] **AC7:** `/pause` and `/resume` set and clear the global pause (spec 015, U3) and say whether
  anything changed.
- [ ] **AC8:** an unknown command or plain text gets a short hint pointing to `/help`; commands
  addressed to another bot (`/status@other_bot`) are ignored.
- [ ] **AC9:** every message from a chat outside the allowlist is ignored without a reply and
  logged at `INFO` without the chat id, user id, username or text.
- [ ] **AC10:** every reply starts with `[BETA]` outside `prod` and user-provided text is
  HTML-escaped.

### Long polling (#20)

- [ ] **AC11:** when a token and at least one chat id are configured, the runtime starts one
  poller after the scheduler and stops it first at shutdown. Without them no poller starts.
- [ ] **AC12:** the poller never blocks or fails startup: Telegram being unreachable, a rejected
  token or a `409 Conflict` (another process polling the same token) is logged by class name and
  retried with capped backoff, and `/health` keeps answering.
- [ ] **AC13:** the poller advances its offset before handling a batch and, at shutdown, lets
  the command in flight finish and confirms it, so a clean restart (deploy, `docker compose
  restart`) never runs a command twice; only a crash in the middle of a command may. Commands
  sent while the bot was down are handled at startup (Q3).
- [ ] **AC14:** `docs/DEPLOYMENT.md` explains, with placeholders, how to create one bot per
  environment, find the chat id and load `secrets.env`.

### Guards

- [ ] **AC15:** `scripts/check.py` is green; no test reaches the network; the token never appears
  in logs, exceptions or replies (runtime-built fake token).
- [ ] **AC16:** python-telegram-bot stays confined to `notifications/telegram.py` and one module of
  `telegram_bot/`; command handlers are library-free and tested against a temporary database
  and a fake provider.

## Design

### 0. Decisions

| Id | Topic | Decision | Why |
|----|-------|----------|-----|
| D1 | Polling loop | Our own `getUpdates` loop over `telegram.Bot.get_updates`, not `telegram.ext.Application` | `Application.initialize` calls `getMe` and, on a rejected token, raises an error that embeds the token; it also fails startup when Telegram is unreachable. A 60-line loop keeps retries, the 409 handling and the offset explicit and testable with a fake |
| D2 | Long-poll timeout | 30 s, `allowed_updates=["message"]` | Few requests, near-instant replies; edits, callbacks and channel posts are not needed |
| D3 | Backoff | 1 s, 2 s, 4 s ... capped at 60 s; a 409 waits at least 30 s; reset after a successful call | A second process with the same token (rule 7) must not be hammered, and the log must say clearly what is happening once per streak, not once per second |
| D4 | Handlers | `telegram_bot/commands.py`: an async `CommandHandler.handle(text) -> str \| None`, library-free, over a command unit of work (tickers, rules, assignments, state), the provider and the scheduler's next fire times | Testable with fakes (#19 acceptance criteria); the only library code is the poller adapter |
| D5 | Authorization | Checked once in the poller, before parsing, against the allowlist from the settings | One place to get it right; handlers never see an unauthorized message |
| D6 | Replies | HTML parse mode through the same sender rules as spec 017 (no `initialize`, quiet `httpx` logger); a failed reply is logged by class name and dropped | A reply is not a signal: no retry queue |
| D7 | `/remove` | Disables (Q2); deleting stays a CLI action with `--force` | Keeps the signal history the dashboard and F7 need; `/add` brings it back |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/telegram_bot/__init__.py` | developer | New package docstring |
| `src/trading_bot/telegram_bot/commands.py` | developer | New: parsing, authorization-free handlers, replies |
| `src/trading_bot/telegram_bot/unit_of_work.py` | developer | New: `CommandRepositories` and the SQL unit of work |
| `src/trading_bot/telegram_bot/poller.py` | developer | New: `TelegramPoller` (D1–D3, D5), the only module of the package that imports python-telegram-bot |
| `src/trading_bot/runtime.py` | developer | Start the poller after the scheduler, stop it first (AC11) |
| `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md` | developer | AC14 |
| `tests/unit/test_telegram_commands.py`, `tests/unit/test_telegram_poller.py`, `tests/unit/test_runtime.py` | tester | Test plan |

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| Each command against a real temporary database and a fake provider | unit | AC1–AC8 |
| An unauthorized chat runs nothing and nothing identifying is logged | unit | AC9 |
| `[BETA]` prefix and HTML escaping | unit | AC10 |
| Runtime starts/stops the poller only when configured | unit | AC11 |
| Network error, rejected token and 409 are retried with backoff; no token in logs | unit (fake bot) | AC12, AC15 |
| Offsets are confirmed, including on stop; a handled update is not handled again | unit (fake bot) | AC13 |

## Risks and security

- The token is only in request URLs built by the library; the `httpx` logger stays at `WARNING`.
- Anyone who learns the bot's username can message it; the allowlist (AC9) is the protection,
  checked before anything else, and unauthorized attempts leave no personal data in the logs.
- **Beta and prod must use different tokens**: one token allows one poller (rule 7). With the same
  token, both log the 409 and back off instead of fighting.

## Product questions

- **Q1 — What `/add` assigns:** every stored rule of that timeframe, so a symbol added from the
  phone produces signals right away (recommended) **or** nothing, leaving assignments to the CLI.
- **Q2 — What `/remove` does:** disable the ticker and keep its history (recommended) **or** delete
  it with its assignments and signals.
- **Q3 — Commands sent while the bot was down:** handle them at startup (recommended) **or** drop
  them.

## User decisions

_Pending; the implementation follows the recommendations until the user answers._

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
