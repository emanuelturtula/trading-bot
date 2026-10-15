# 025 — A Telegram bot per user

- **Status:** approved (design proposal accepted by the user; third of three multi-user specs)
- **Branch:** `feature/multi-user-bvxper`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Let every dashboard account receive its signals through a Telegram bot of its own, configured in
the dashboard instead of `secrets.env`: each user pastes the token @BotFather gave them, links the
chats that should get their signals, and can pause their own signals. The bot of each user
answers commands on that user's tickers and rules only.

## Out of scope

- Sending one user's signals to another user's chats, or sharing a bot between accounts.
- Webhooks: every bot keeps long polling, all in the one process (rule 7).
- Rotating `TB_MASTER_KEY` with re-encryption. Changing the key makes the stored tokens
  unreadable; each user pastes their token again.
- Deleting an account (a disabled user's bot simply stops).

## Acceptance criteria

- [ ] **AC1:** migration `0007` adds `telegram_bots` (one per user: encrypted token, keyed token
  fingerprint, bot username, pending link code digest and expiry), `telegram_chats`
  (`(user_id, chat_id)`, a 64-bit chat id and an optional title) and `user_pauses`, each with a
  foreign key to `users` and `ON DELETE CASCADE`. Existing rows are untouched; the downgrade
  drops only these tables.
- [ ] **AC2:** the Telegram page lets a user paste a token. It is checked with Telegram
  (`getMe`) before it is stored, stored only encrypted with `TB_MASTER_KEY`, and never shown,
  logged or returned again. A malformed token is refused without calling Telegram; a token
  Telegram rejects is refused, and five rejections within 15 minutes block that user's attempts.
- [ ] **AC3:** the same token cannot be stored by two accounts (`409`); saving a different token
  forgets the old bot's chats and pending code.
- [ ] **AC4:** "Link a chat" shows a one-time code valid for 30 minutes. Sending `/link CODE` (or
  `/link@bot CODE`) to that user's bot, from a private chat or a group, links that chat. A
  wrong, expired or spent code links nothing. Linked chats are listed with their title and the
  last digits of their id, and each can be unlinked.
- [ ] **AC5:** each user's signals go to the chats linked to their own bot only; a user without a
  bot or without a linked chat has their signals logged.
- [ ] **AC6:** a bot answers only its owner's linked chats, and its commands (`/add`, `/remove`,
  `/list`, `/rules`, `/status`, `/pause`, `/resume`) act on its owner's data.
- [ ] **AC7:** every user can pause and resume their own signals, from the overview or with
  `/pause` and `/resume`: their tickers are not evaluated meanwhile. The global pause stays an
  administrator's switch, labelled "for everyone".
- [ ] **AC8:** saving or removing a bot, linking or unlinking a chat, and enabling or disabling a
  user take effect without a restart; a disabled user's bot stops polling.
- [ ] **AC9:** with `TB_MASTER_KEY` set, the bot of `TB_TELEGRAM_BOT_TOKEN` and the chats of
  `TB_TELEGRAM_ALLOWED_CHAT_IDS` are imported once as the first administrator's, and the log
  says those variables can be removed. Without `TB_MASTER_KEY` (or with an invalid one, which is
  logged without its value), nobody can store a bot and that environment bot keeps serving the
  first administrator exactly as before.

## Design

- **D1, tables:** `persistence/models.py` gains `TelegramBotRow`, `TelegramChatRow` and
  `UserPauseRow`; migration `0007_telegram_per_user.py` creates them with the same DDL.
  `SqlTelegramRepository` (`persistence/repositories/telegram.py`) only moves ciphertext and
  digests. `SqlUserRepository` gains `paused_since`, `pause` and `resume`.
  `SqlTickerRepository.list_enabled` skips the tickers of paused users.
- **D2, encryption:** `trading_bot/secretbox.py`. `SecretBox(TB_MASTER_KEY)` encrypts with Fernet
  (`cryptography`, AES-128-CBC with HMAC-SHA256) and fingerprints a token with HMAC-SHA256 under
  a key derived from the master key, so uniqueness is checked without storing or exposing the
  token. `python -m trading_bot.secretbox` prints a new key.
- **D3, saving a token:** the dashboard checks the shape (`digits:secret`), asks Telegram for the
  bot's username through a throwaway `UpdateSource`, encrypts, stores and reloads the bots.
- **D4, linking:** a code is 8 characters from an alphabet without look-alikes, stored as the
  SHA-256 of its upper-case form, valid 30 minutes and spent on use. `TelegramPoller` hands
  `/link CODE` to a `Linker` before its allowlist check, may start with an empty allowlist when
  it has a linker, and its allowlist can be replaced while it runs (`set_allowed`).
- **D5, supervisor and routing:** `telegram_bot/supervisor.py`. `BotSupervisor` holds one poller,
  one sender and one `TelegramNotifier` per user; `reload()` decrypts the bots of active users
  and starts, restarts (new fingerprint) or stops pollers to match the database; chat changes
  only update the allowlist and recipients. `UserRouter` is the engine's notifier: it sends each
  signal through its owner's notifier, or logs it. The command handler and its unit of work are
  built per owner. Logs name a bot by its owner's user id, never by token, username or chat.
- **D6, pauses and disabled users:** a row in `user_pauses` is a user's pause. The overview shows
  "Your signals" with Pause/Resume for everyone, and the global pause as "Pause for everyone"
  for administrators. Enabling or disabling a user reloads the bots.
- **D7, environment bot:** with a valid `TB_MASTER_KEY`, `import_env_bot` stores the environment
  bot once for the first administrator (skipped when they already have a bot or another account
  uses that token) and the database is the only source from then on. Without it,
  `BotSupervisor.run_static` runs the environment bot for the first administrator.
- **Runtime order:** the supervisor is built before the engine; bots start after the scheduler;
  at shutdown pollers stop first, then the scheduler, then the senders, so a run in flight can
  still deliver.
- **Dashboard:** `GET /dashboard/telegram`; `POST /dashboard/telegram/token`, `/telegram/delete`,
  `/telegram/code`, `/telegram/unlink`, `/my/pause`, `/my/resume`. Every action needs a
  session and a CSRF token, and acts on the signed-in user only.
- **New configuration:** `TB_MASTER_KEY` (`SecretStr`, secret, optional). New dependency:
  `cryptography`.

## Test plan

| Case | Type (unit/integration) | What it verifies |
|------|-------------------------|------------------|
| `test_secretbox.py` | unit | round trip, wrong key, damaged ciphertext, keyed fingerprint, invalid key never echoed, key command |
| `test_persistence_migration_0007.py` | unit | head chain, DDL equals the models, upgrade keeps data, downgrade drops only the new tables |
| `test_persistence_telegram.py` | unit | save/replace, duplicate token, new token forgets chats, code once and before expiry, per-user codes, title clipping, user pause, paused tickers skipped by the engine |
| `test_telegram_poller.py` | unit | `/link` reaches the linker from any chat, `/link@other_bot` ignored, empty allowlist with a linker, `set_allowed`, username sink |
| `test_telegram_supervisor.py` | unit | reload follows the database, per-owner answers, link flow end to end, undecryptable bot skipped, static env bot, validate, failing close isolated, router per owner, import once |
| `test_telegram_commands.py` | unit | `/pause` and `/resume` per owner, `/status` shows the global pause, bare `/link` |
| `test_dashboard_telegram.py` | integration | save, malformed and rejected tokens, throttle, Telegram down, duplicate across accounts, code and unlink, removal, isolation between users, disable/enable, no master key, own pause, overview status |
| `test_runtime.py` | integration | shutdown order, no master key, invalid key not logged, import once on restart |

Authorization: every dashboard action is per signed-in user and needs CSRF; a bot answers only
its owner's linked chats. Secret redaction: `TB_MASTER_KEY` joins `secret_values()`; tests assert
no token, key, code or chat id reaches the logs.

## Risks and security

- **Tokens at rest** are Fernet ciphertext; the key lives only in `secrets.env`. Losing or
  changing `TB_MASTER_KEY` makes the stored bots unreadable (logged per user); users paste their
  tokens again. A database leak without the key exposes no token.
- **Link codes:** 31^8 combinations, valid 30 minutes, single use, per bot; a stranger who finds
  a bot cannot realistically guess one through Telegram's rate limits.
- **Telegram calls from the dashboard** are throttled per user on rejection.
- **Deploy impact:** migration `0007` runs at startup. `TB_MASTER_KEY` must be added to
  `secrets.env` and the container recreated for users to store bots; without it everything keeps
  working as before (AC9).

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)
