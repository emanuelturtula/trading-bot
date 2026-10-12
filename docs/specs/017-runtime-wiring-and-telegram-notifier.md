# 017 — Runtime wiring and the Telegram signal notifier

- **Status:** draft (product questions Q1–Q5 open, see "Product questions")
- **Branch:** `feature/runtime-wiring-telegram`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Issues:** #16 (App lifespan wiring and single-instance guard) and #17 (TelegramNotifier: signal
  message), delivered together in one PR at the user's request (2026-10-12).

## Goal

Today every analysis component exists and is tested, but the running app only opens the database
and answers `/health`: nothing fetches candles, evaluates rules or tells the user anything. This
feature starts the engine and the scheduler inside the FastAPI lifespan, guarantees a single
process per data directory, and delivers every new signal as a Telegram text message, so the
first real signal reaches the user's phone from beta.

## Out of scope

- **The chart** (#18): the message is text only. `SignalNotification.candles` and `.rule` are
  carried but not drawn; #18 adds the `sendPhoto` path behind the same `notify` call.
- **Commands and long polling** (#19, #20): the bot only sends. No `getUpdates`, no handler, no
  `Application`; the poller and its 409 handling belong to #20.
- **Heartbeat, error alerts and `/status`** (F7, #21, #26, #27). `/health` keeps its current
  shape (see D8).
- **Re-sending undelivered signals**: delivery stays at-most-once (spec 015, U4).
- Changes to `domain/`, `data/`, `persistence/`, `engine/` and `scheduler/` beyond what §Design
  names.

## Acceptance criteria

### Wiring (#16)

- [ ] **AC1:** the lifespan builds, inside the running loop and in this order: the instance lock,
  the `Database`, the `MarketCalendar`, the Yahoo provider, the notifier, the `SignalEngine`, the
  `TimeframeRunner` and one `SchedulerService`, then calls `service.start()`. Exactly one of each
  exists per process.
- [ ] **AC2:** shutdown runs in reverse order: `await service.aclose()`, `await
  notifier.aclose()`, `await provider.aclose()`, provider cache directory removed,
  `database.dispose()`, lock released. A failure in one step is logged (class name only) and does
  not skip the following steps.
- [ ] **AC3:** a failure while starting (lock busy, a `ValueError` from `SchedulerPolicy`, a
  calendar or database error) aborts startup: the lifespan never yields, the components already
  built are closed in reverse order, and uvicorn exits non-zero, so the deploy rolls back.
- [ ] **AC4:** a second process pointed at the same `TB_DATA_DIR` refuses to start and logs one
  `ERROR` line that names the lock file, not its contents.
- [ ] **AC5:** the scheduler policy comes from `TB_CANDLE_CLOSE_DELAY_SECONDS` and
  `TB_SCHEDULER_MISFIRE_GRACE_SECONDS`, and `should_run=skip_unpublished_hours(calendar)` is
  injected into both the runner and the service.
- [ ] **AC6:** `/health` is unchanged, public and configuration-free; the `deploy/compose.yml`
  healthcheck keeps passing on beta.

### Telegram notifier (#17)

- [ ] **AC7:** `TelegramNotifier.notify` sends one text message per recipient chat (Q1) through
  `sendMessage`, with HTML parse mode and every user-controlled string (ticker, rule
  name, indicator names) HTML-escaped.
- [ ] **AC8:** the message contains, in this order: the `[BETA]` prefix outside `prod`, the side
  with an emoji, the ticker, the timeframe, the rule name, the close price, the candle (session
  date for `1d`, real close time for `1h`/`4h`, in the display time zone of Q3, never the nominal
  close), each indicator value, and the line "Not financial advice."
- [ ] **AC9:** transient failures (network errors, timeouts, HTTP 5xx, HTTP 429 honouring
  `retry_after`) are retried with bounded backoff; permanent failures (400, 401, 403, 404) are
  not. When delivery to a chat ultimately fails, `notify` raises `NotificationError` whose
  message contains no token, chat id or Telegram response text. Delivery succeeds if at least one
  recipient received it (Q1).
- [ ] **AC10:** neither the token nor a chat id appears in any log record, exception message or
  `repr` produced by the notifier (redaction test with a runtime-built fake token).
- [ ] **AC11:** when no token is configured, the app follows Q2.

### Guards, typing and docs

- [ ] **AC12:** `scripts/check.py` is green (ruff, format, mypy strict, tests with coverage ≥ 85%,
  gitleaks); no test reaches the network.
- [ ] **AC13:** `docs/ARCHITECTURE.md` describes the runtime wiring and the notifier;
  `docs/ROADMAP.md` marks F4 completed; `docs/DEPLOYMENT.md` lists the new `secrets.env`
  variables with placeholders only.

## Design

### 0. Decisions

| Id | Topic | Decision | Why |
|----|-------|----------|-----|
| D1 | One PR for #16 and #17 | One branch and one PR, superseding spec 015 D1 ("one PR per issue") for these two issues | The user asked for both together: wiring without a notifier would record signals nobody sees |
| D2 | Telegram client | `python-telegram-bot>=22,<23` (the stack in CLAUDE.md), using only `telegram.Bot.send_message`; no `Application`, no polling | #20 needs the same library for long polling, so adding it now avoids a second HTTP client; its typed errors (`RetryAfter`, `TimedOut`, `NetworkError`, `Forbidden`, `BadRequest`, `InvalidToken`) map directly onto the retry policy of D3. Tests inject a fake `Bot`-shaped sender, so nothing reaches the network |
| D3 | Retries | Up to 4 attempts per chat, backoff 1 s, 2 s, 4 s (capped at 30 s); a 429 waits `parameters.retry_after` (capped at 60 s); per-request timeout 10 s | Signals fire a couple of minutes after the close; a bounded retry finishes well before the next `1h` candle, and the engine already tolerates a slow `notify` |
| D4 | Instance lock | `fcntl.flock(LOCK_EX \| LOCK_NB)` on `<TB_DATA_DIR>/trading-bot.lock`, held for the process lifetime, released by the OS if the process dies | No stale-lock cleanup is ever needed, unlike a PID file; Linux only, which is the container and the Pi. The file holds the PID for an operator, never secrets |
| D5 | Provider cache | A `tempfile.mkdtemp(prefix="tb-yf-")` directory per process, removed at shutdown | Spec 011 D50: never the data volume |
| D6 | Calendar | `build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)`, built in a worker thread | Building it takes noticeable time on a Pi; the loop stays responsive |
| D7 | Shutdown drain | If `service.aclose()` returns with a run still in flight (its `WARNING`), the lifespan waits up to 10 s more before `database.dispose()` | Spec 016 hand-off 1: a worker thread may still hold a unit of work |
| D8 | `/health` | Unchanged | The issue keeps it public and configuration-free; a failed component already prevents startup (AC3), which is what the healthcheck needs. Next fire times belong to `/status` (#21) |
| D9 | Price format | Two decimals at or above 1, otherwise four significant digits; thousands separated with a comma | Readable for both stocks and low-priced assets |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/runtime.py` | developer | New: `Runtime` (builds, starts and closes the components; AC1–AC5) |
| `src/trading_bot/instance_lock.py` | developer | New: `InstanceLock`, `InstanceLockError` (D4) |
| `src/trading_bot/main.py` | developer | Lifespan delegates to `Runtime` |
| `src/trading_bot/config.py` | developer | New settings (§4) |
| `src/trading_bot/logging_setup.py` | developer | Pin `httpx` and `telegram` loggers to `WARNING` (§2) |
| `pyproject.toml`, `uv.lock` | developer | Add `python-telegram-bot>=22,<23` (D2) |
| `src/trading_bot/notifications/telegram.py` | developer | New: `TelegramNotifier` |
| `src/trading_bot/notifications/message.py` | developer | New: pure `format_signal_message` |
| `src/trading_bot/notifications/log_notifier.py` | developer | New: `LogNotifier` (Q2) |
| `tests/unit/...`, `tests/integration/...` | tester | §Test plan |
| `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/DEPLOYMENT.md` | developer | AC13 |

### 2. Interfaces

```python
class TelegramNotifier:  # implements notifications.notifier.Notifier
    def __init__(self, *, token: SecretStr, chat_ids: Sequence[int], environment: Environment,
                 display_tz: ZoneInfo, calendar: MarketCalendar,
                 sender: MessageSender | None = None,  # tests inject a fake; default wraps telegram.Bot
                 sleep: Sleep = asyncio.sleep) -> None: ...
    async def notify(self, notification: SignalNotification) -> None: ...
    async def aclose(self) -> None: ...

def format_signal_message(notification: SignalNotification, *, environment: Environment,
                          display_tz: ZoneInfo, calendar: MarketCalendar) -> str: ...
```

`MessageSender` is a one-method `Protocol` (`send_message(chat_id, text) -> None`) so the retry
policy is tested without the library. The token only ever appears inside the request URL that
python-telegram-bot builds; **`httpx` logs every request URL at `INFO`**, token included, so
`configure_logging` pins the `httpx` and `telegram` loggers to `WARNING`, and a test asserts that
no record carrying the fake token reaches a handler. The `RedactingFilter` masking token-shaped
text stays as the second line of defence.

### 3. Message (example, beta, `1d`)

```
[BETA] 🟢 BUY · AAPL · 1d
Rule: RSI oversold bounce
Close: 182.41
Candle: 2026-10-09 (session)
RSI_14: 28.73
SMA_50: 190.12

Not financial advice.
```

### 4. Configuration

| Variable | Type | Secret | Default |
|----------|------|--------|---------|
| `TB_TELEGRAM_BOT_TOKEN` | `SecretStr \| None` | yes (already exists) | none |
| `TB_TELEGRAM_ALLOWED_CHAT_IDS` | comma-separated integers, parsed and validated | no, but never logged | empty (already exists) |
| `TB_DISPLAY_TIMEZONE` | IANA zone name, validated with `zoneinfo` | no | Q3 |

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| Startup builds every component once and starts the scheduler | integration (fake provider, fake notifier) | AC1, AC5 |
| Shutdown order and continue-on-error | unit | AC2 |
| Startup failure closes what was built and propagates | unit | AC3 |
| Second lock on the same directory fails; lock released on close | unit | AC4 |
| `/health` with the full lifespan | integration (`TestClient`) | AC6 |
| Message golden texts for `1h`, `4h`, `1d`, `prod` vs `beta`, HTML escaping | unit | AC7, AC8 |
| Retry on `TimedOut`/`NetworkError`, `RetryAfter` honoured, no retry on `BadRequest`/`Forbidden`/`InvalidToken` | unit (fake sender) | AC9 |
| Token and chat id absent from logs and exceptions | unit (runtime-built fake token) | AC10 |
| No token configured | unit | AC11 |

## Risks and security

- **The bot token** is a secret: `SecretStr`, only in the request URL, never logged, never in an
  exception. New `secrets.env` variables are documented with placeholders only.
- **Deploy impact:** no migration. Without `TB_TELEGRAM_BOT_TOKEN` in beta's `secrets.env` the
  app behaves as Q2 decides; the operator adds the token and chat id on the Pi by hand.
- **Yahoo traffic starts for real** on beta once tickers are configured with the CLI; the
  provider's pacing (spec 011) applies.
- **Beta and prod share a Telegram bot only if the operator configures the same token**; they must
  not until #20 adds polling (rule 7). Recommended: one bot per environment.

## Product questions

- **Q1 — Recipients:** send each signal to every chat in `TB_TELEGRAM_ALLOWED_CHAT_IDS`
  (recommended: one list for "who may talk to the bot" and "who gets signals") **or** a separate
  `TB_TELEGRAM_SIGNAL_CHAT_IDS`.
- **Q2 — No token configured:** start anyway with a `LogNotifier` that logs one `INFO` line per
  signal and one `WARNING` at startup (recommended: beta stays healthy before the bot exists)
  **or** refuse to start in `beta` and `prod`.
- **Q3 — Display time zone:** default `TB_DISPLAY_TIMEZONE` to the user's zone
  (`America/Argentina/Buenos_Aires` or `Europe/Madrid`), overridable per environment.
- **Q4 — Branch:** develop on `feature/runtime-wiring-telegram` so the push deploys to beta (port
  8082), per the CLAUDE.md workflow.
- **Q5 — One PR for #16 and #17** (D1).

## User decisions

_Pending._

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
