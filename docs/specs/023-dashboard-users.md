# 023 — Dashboard users

- **Status:** approved (design proposal accepted by the user; first of three multi-user specs)
- **Branch:** `feature/multi-user-bvxper`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Turn the single-password dashboard into a dashboard with accounts, so other people can use the
service. This is the first step of the multi-user redesign: every person will later get their own
tickers, rules and Telegram bot (specs 024 and 025). Here, only the accounts.

## Out of scope

- Data per user. Until spec 024 every account sees and edits the **same** tickers and rules, so
  no one should be invited before 024 ships.
- A Telegram bot per user and storing bot tokens in the database (spec 025).
- Open sign-up, e-mail, password recovery by e-mail, two-factor authentication.
- Deleting an account (disabling is enough until data is per user) and renaming one.

## Acceptance criteria

- [ ] **AC1:** migration `0004` adds `users`; existing rows are untouched, and the downgrade drops
  only `users`.
- [ ] **AC2:** while there is no user, the first login creates the administrator from
  `TB_DASHBOARD_PASSWORD_HASH` with the username `TB_ADMIN_USERNAME` (default `admin`). Once a user
  exists the variable is ignored.
- [ ] **AC3:** login takes a username and a password. An unknown user and a wrong password get the
  same `401` answer and cost the same scrypt verification.
- [ ] **AC4:** an administrator invites a person on the Users page with a username and an optional
  administrator role, and gets a one-time access link valid for 72 hours. Opening it lets the
  person choose a password (12 to 1024 characters, typed twice) and logs them in. A used, replaced
  or expired link answers `404`.
- [ ] **AC5:** only the SHA-256 digest of a link is stored, and the token is redacted from the
  server's access log.
- [ ] **AC6:** "New access link" on another user clears their password, logs them out everywhere
  and shows a new link: this is the password reset.
- [ ] **AC7:** an administrator can disable and re-enable another user; a disabled user is logged
  out at once and cannot log in or use a link. Administrators cannot disable themselves or reset
  their own password with a link.
- [ ] **AC8:** the Account page changes the password after checking the current one; every other
  session of the account is logged out and the current browser stays signed in.
- [ ] **AC9:** the Users page and its actions answer `403` to a non-administrator, and redirect to
  the login without a session, like every other page.
- [ ] **AC10:** 5 failed logins or link attempts per client address in 15 minutes, and 10 failed
  logins per username from any address, answer `429` until the window passes.
  `TB_CLIENT_IP_HEADER` (for example `CF-Connecting-IP`) names the header holding the browser's
  address when a proxy is in front; it is read only when the TCP peer is in `TB_TRUSTED_PROXIES`.
- [ ] **AC11:** an access link shown to the administrator uses the scheme and host the browser
  used (its `Origin`), so it is right behind a tunnel that terminates TLS.
- [ ] **AC12:** `scripts/check.py` is green.

## Design

- **D1. Model.** `users(id, username, password_hash NULL, is_admin, active, invite_digest NULL,
  invite_expires_at NULL, created_at, updated_at)`, `username` and `invite_digest` unique,
  `sqlite_autoincrement` so an id is never reused (it will own data in 024). A username is 3 to 32
  of `a-z 0-9 . _ -`, starting alphanumeric, stored lowercased. `SqlUserRepository` in
  `persistence/repositories/users.py`, exposed as `DashboardRepositories.users`.
- **D2. First administrator.** A migration cannot read settings, so the dashboard creates it in the
  login's unit of work while the table is empty. This keeps today's deploy working: the existing
  `TB_DASHBOARD_PASSWORD_HASH` becomes the administrator's password. Without users and without a
  valid hash the login page answers `503`.
- **D3. Access links.** 32 random bytes (`secrets.token_urlsafe`), stored as their SHA-256 hex
  digest with an expiry. The link is shown once, on the page that answers the invitation `POST`.
  `RedactingFilter` rewrites `/dashboard/invite/<token>` and is attached to `uvicorn.access`, whose
  records never reach the root handler.
- **D4. Reset.** A new link replaces the old one and sets the password to `NULL`: the account is
  locked until the link is used, and every session dies (D5).
- **D5. Sessions.** The cookie is `<expiry>.<user id>.<nonce>.<mac>`. The MAC key derives from
  `TB_SESSION_SECRET` only, and the MAC covers the user's current password hash, so a password
  change, a reset or a new secret ends sessions; the gate loads the user on each request and
  refuses a disabled one. CSRF tokens are unchanged (a MAC of the nonce).
- **D6. Throttling.** In memory, as before (one process, rule 7): per client address and per
  username. The address is the TCP peer, or the `TB_CLIENT_IP_HEADER` header when the peer is in
  `TB_TRUSTED_PROXIES` (default: loopback and the private ranges, where `cloudflared` on the same
  host or Docker network connects from). A browser reaching an open port directly arrives from a
  public address, so its forged header is ignored.

New configuration: `TB_ADMIN_USERNAME` (not a secret, default `admin`), `TB_CLIENT_IP_HEADER`
(not a secret, default empty) and `TB_TRUSTED_PROXIES` (not a secret, comma-separated networks or
the words `loopback` and `private`, default `loopback,private`). `TB_DASHBOARD_PASSWORD_HASH` is
only needed until the first login.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| `test_persistence_users.py` | unit | usernames, duplicates, links (expiry, replacement, single use), disabling |
| `test_persistence_migration_0004.py` | unit | upgrade keeps rows, DDL equals the model, downgrade drops only `users` |
| `test_dashboard_users.py` | unit (FastAPI) | AC2-AC11 end to end, including authorization and throttling |
| `test_dashboard_auth.py` | unit | per-user session cookie, tampering, stamp change, link tokens |
| `test_dashboard.py` | unit (FastAPI) | the spec 020 behaviour with a username in the login |

## Risks and security

- Passwords stay scrypt hashes; link tokens are stored as digests and redacted from logs; no log
  line carries a username typed at the login (only user ids).
- Deploy impact: migration `0004`; nothing to change in `secrets.env`. After the deploy the login
  asks for a username: `admin` (or `TB_ADMIN_USERNAME`) with the current password.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
