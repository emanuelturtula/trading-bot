# 024 — Tickers, rules and signals per user

- **Status:** approved (design proposal accepted by the user; second of three multi-user specs)
- **Branch:** `feature/multi-user-bvxper`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Give every dashboard account its own tickers, rules and signal history, so people with different
strategies can share one bot without seeing or changing each other's configuration. One engine
run still serves everyone, and each symbol is downloaded once per run.

## Out of scope

- A Telegram bot per user (spec 025). Until then, the bot configured in `secrets.env` belongs to
  the first administrator; other users' signals are recorded and logged, not sent.
- A pause per user. The pause stays global and only an administrator can pause or resume.
- Telegram commands per user: `/add`, `/remove`, `/list` and `/rules` act on the first
  administrator's data, since the only bot is theirs.
- Sharing a rule between users, copying another user's rule, deleting an account.

## Acceptance criteria

- [ ] **AC1:** migration `0005` adds `user_id` to `tickers` and `rules`, a foreign key to `users`
  with `ON DELETE CASCADE`. The natural keys become `(user_id, symbol, timeframe)` and
  `(user_id, name)`. Every existing row, assignment and signal survives with its id, and the
  `sqlite_sequence` counters never go back.
- [ ] **AC2:** existing rows go to the administrator with the lowest id. If users exist but none is
  an administrator, the lowest id becomes one. If there is no user, an `admin` account without a
  password is created, and the first login sets its password from `TB_DASHBOARD_PASSWORD_HASH`.
- [ ] **AC3:** each user's Tickers, Rules and ticker pages list only their own rows.
- [ ] **AC4:** another user's ticker or rule is treated as unknown: its pages answer `404` and its
  actions change nothing.
- [ ] **AC5:** two users may watch the same symbol and timeframe and may give rules the same name.
- [ ] **AC6:** a rule can be assigned only to a ticker of the same user.
- [ ] **AC7:** an engine run evaluates the enabled tickers of every active user, downloads each
  symbol once per run with the largest lookback any of its tickers needs, and records each
  signal against its owner's ticker and rule. A disabled user's tickers are not evaluated.
- [ ] **AC8:** the Telegram bot of `secrets.env` sends only the first administrator's signals;
  every other user's signals are logged.
- [ ] **AC9:** only an administrator sees the Pause and Resume buttons; anyone else gets `403`.
- [ ] **AC10:** while nobody can log in (no user with a password and no pending link), the first
  login with `TB_DASHBOARD_PASSWORD_HASH` gives the first administrator that password and the
  username `TB_ADMIN_USERNAME`. This replaces spec 023 AC2, because the migration now creates the
  administrator before anyone logs in.

## Design

- **Owner scope** (`persistence/owner.py`): `Owner = int | ALL_OWNERS | None`. An id limits a
  repository to that user's rows, `ALL_OWNERS` sees every row (the engine), and `None` means the
  default owner, the administrator with the lowest id (Telegram commands, until spec 025).
- **Repositories:** `SqlTickerRepository`, `SqlRuleRepository` and `SqlAssignmentRepository` take
  `owner=`. Every read and write by id or by name is filtered by the owner, so a foreign id
  behaves as an unknown id. `list_enabled` joins `users` and keeps active users only.
  `StoredTicker` and `StoredRule` gain `user_id`.
- **Signals** keep their schema: a signal belongs to its ticker and its rule, which belong to the
  same user. Recording a signal looks the ticker up by symbol and timeframe within the rule's
  owner.
- **Dashboard:** the unit of work takes the signed-in user's id as owner.
- **Engine:** `TickerPlan` gains `user_id`, `SignalNotification` gains `owner_id`, and a run
  shares one download per symbol between the tickers that need it.
- **Notifications:** `OwnerRouter(notifier, owner_id)` sends the owner's signals through the
  configured notifier and logs the rest. It is replaced by one bot per user in spec 025.
- **Migration `0005`:** SQLite cannot change a constraint in place, so `tickers` and `rules` are
  rebuilt. `ticker_rules` and `signals` are copied aside first (dropping a referenced table
  would run their cascades), then recreated with their exact DDL. The downgrade drops `user_id`
  and fails on a duplicate if two users share a symbol or rule name; the pre-deploy backup is
  the recovery path.
- No new configuration.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| `test_persistence_migration_0005.py` | unit | AC1, AC2: rows, ids, counters, DDL, downgrade |
| `test_persistence_models.py` | unit | AC1: golden DDL, foreign keys, AC5 at the database |
| `test_persistence_tickers.py`, `_rules.py`, `_users.py` | unit | records carry the owner |
| `test_dashboard_owners.py` | unit | AC3-AC6, AC9 through the dashboard |
| `test_engine_owners.py` | unit | AC7: one download, signals per owner, disabled users |
| `test_notifications_routing.py` | unit | AC8 |
| existing engine idempotency tests | unit | a signal is still sent once per key |

## Risks and security

- A missed owner filter would leak one user's configuration to another. Every repository method
  that takes an id or a name filters by owner, and the dashboard tests try foreign ids on every
  page and action.
- Deploy: migration `0005` rebuilds two tables. The deploy takes a backup first, as it does for
  every migration. No new variable in `secrets.env`.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
