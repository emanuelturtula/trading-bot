# Deploy

> This document is public: it uses placeholders. **Never** write real IPs (LAN or tailnet), hostnames, users, keys or tokens here.

## Summary

| Event | Pipeline | Image | Target |
|-------|----------|-------|--------|
| Pull request | `ci.yml` | arm64 build without push | — |
| Push to `main` (merge) | `delivery.yml` | `ghcr.io/emanuelturtula/trading-bot:sha-<sha>`; after a healthy deploy: `:vX.Y.Z` and `:latest` | container `trading-bot-prod`, port **8081**, git tag + GitHub Release |

There is no beta environment (removed in spec [019](specs/019-remove-beta-rename-deploy-root.md)): a pull request is validated by `ci.yml` only, and production is the only deploy target.

`delivery.yml` flow: `ci.yml` (gitleaks → lint/mypy → tests) → `next_version.py` → native arm64 build (`ubuntu-24.04-arm`) → push to GHCR → `remote-deploy.yml` → release (main only).

`remote-deploy.yml` joins the runner to the tailnet with Tailscale OIDC (ephemeral node with `tag:trading-bot-ci`), and `scripts/remote_deploy.py`:

1. validates all inputs and that the run is a push to `main` of this repo (production is the only target);
2. copies `deploy/deploy.py` and `deploy/compose.yml` to a temporary directory on the Pi;
3. runs `docker login ghcr.io` with the job's ephemeral token **via stdin**;
4. runs `deploy.py` and deletes the temporary directory.

`deploy/deploy.py` (on the Pi):

- copies the state of older releases (`~/trading-bot-deploy/prod`) into `~/trading-bot` once (see [Layout](#layout-on-the-raspberry));
- takes a host lock (two deploys never run in parallel);
- rejects runs older than the deployed one (an old rerun does not overwrite a new one);
- runs `docker pull` by digest and verifies the OCI labels `revision` and `version`;
- backs up the SQLite database of the current container to `backups/`;
- `docker compose up --wait` with the new compose file staged as `compose.next.yml`, and verifies that the exact digest is running with a `healthy` healthcheck;
- if it fails, restores the previous deploy with the untouched `compose.yml`; if there was none, stops the container and keeps the volume; the error is in the job log;
- only if the deploy ended up healthy, replaces `compose.yml` and `deploy.py`, writes `current.json` and keeps the last 10 backups.

## Layout on the Raspberry

```
~/trading-bot/                        (chmod 700)
  compose.yml          compose file of the running deploy
  deploy.py            deploy script used by the running deploy
  secrets.env (600)    application secrets, written by you
  current.json         last healthy deploy (image, version, run, backup)
  backups/<timestamp>.sqlite3   database copy taken before each deploy (last 10)
  deploy.lock
```

Compose project: `trading-bot-prod` (8081 → 8000) with its `data` volume. The volume belongs to the compose project, not to the folder, so the database is not inside `~/trading-bot`.

**Migration from `~/trading-bot-deploy`.** Older releases used `~/trading-bot-deploy/{beta,prod}/` with an `attempts/` folder per deploy. The first deploy of this release copies `prod/secrets.env`, the running compose file, `current.json` (with its paths rewritten) and every `attempts/*/database.sqlite3` into `~/trading-bot`, so backup and rollback keep working; do not move anything by hand. The old folder is never deleted by the deploy: once the new deploy is healthy, remove it with `rm -rf ~/trading-bot-deploy`. If `~/trading-bot` already holds something else (a clone, for example), the deploy stops before touching anything with `... is not a deploy root`: move it away and re-run the failed job.

## Initial setup (only once)

Requirements on the Pi: Docker with Compose v2, `python3` and the Pi joined to the tailnet.

### 1. Tailscale

1. In the tailnet policy, **add** (without replacing the existing policy) the tag `tag:trading-bot-ci` with a suitable tag owner and access **only** to TCP port 22 of the Raspberry.
2. Create an **OpenID Connect trust** credential with the GitHub issuer and subject:
   `repo:emanuelturtula@<OWNER_ID>/trading-bot@<REPO_ID>:ref:refs/heads/main`
   The IDs are obtained with `gh api repos/emanuelturtula/trading-bot --jq '.owner.id, .id'`. Scope: only *Auth Keys write* with `tag:trading-bot-ci`. Only `main` deploys, so do not widen the subject to other branches, other repos or pull requests.
3. Store the client ID and the audience as repo secrets: `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE`.

### 2. Deploy SSH key

1. Generate a **dedicated** Ed25519 key (do not reuse your personal key or your GitHub key).
2. Add the public half to the `authorized_keys` of the deploy user on the Pi.
3. Store the private half as the `DEPLOY_SSH_KEY` secret.
4. Build the Pi's `known_hosts` entry **for its tailnet hostname or IP** from a trusted SSH session (not with an unverified `ssh-keyscan`) and store it as `DEPLOY_KNOWN_HOSTS`.

### 3. GitHub secrets and variables

| Name | Type | Content |
|------|------|---------|
| `TS_OAUTH_CLIENT_ID` | secret | Client ID of the Tailscale OIDC credential |
| `TS_AUDIENCE` | secret | Audience of the OIDC credential |
| `DEPLOY_HOST` | secret | Tailnet hostname or IP of the Pi (a secret so that it is masked in the public logs) |
| `DEPLOY_USER` | secret | Deploy user on the Pi |
| `DEPLOY_SSH_KEY` | secret | Deploy private key |
| `DEPLOY_KNOWN_HOSTS` | secret | known_hosts entry of the Pi |
| `DEPLOY_ENABLED` | **variable** | `true` when everything above is ready |

> **Warning (PowerShell):** do not load secrets with the interactive prompt of `gh secret set <NAME>` (without `--body`). In PowerShell that prompt can store an **empty** value without showing any error, and the problem only shows up when the deploy fails.

Verified forms in PowerShell:

```powershell
# Non-sensitive value (it is written on the command line and in the history)
gh secret set <NAME> --repo emanuelturtula/trading-bot --body "<value>"

# Value pasted visibly, without writing it on the command line
$v = Read-Host "<NAME>"
gh secret set <NAME> --repo emanuelturtula/trading-bot --body $v
Remove-Variable v

# File: PowerShell has no `<` input redirection; Windows CRs are removed
gh secret set DEPLOY_SSH_KEY --repo emanuelturtula/trading-bot --body ((Get-Content "$HOME\.ssh\<key>" -Raw) -replace "`r", "")
```

`DEPLOY_KNOWN_HOSTS` is loaded the same way as the key, with the path of its `<file>`. In bash/zsh, where `<` does exist, `gh secret set <NAME> --repo emanuelturtula/trading-bot < <file>` is enough.

Symptoms of an empty secret in the deploy job (`Deploy production (8081) / Deploy prod`):

- the `Join tailnet (OIDC, ephemeral)` step fails with `Please provide either an auth key, OAuth secret and tags, or federated identity client ID and audience with tags.` (`TS_OAUTH_CLIENT_ID` or `TS_AUDIENCE` empty);
- the `Deploy and verify health` step fails with `ValueError: Invalid host` (or `Invalid user`) from `scripts/remote_deploy.py` (`DEPLOY_HOST` or `DEPLOY_USER` empty);
- `DEPLOY_SSH_KEY` or `DEPLOY_KNOWN_HOSTS` empty or containing Windows CRs end in `ssh` errors (`Permission denied (publickey)`, `Host key verification failed`);
- in the log, the step's inputs and environment variables show the value **blank** instead of `***` (GitHub only masks secrets that have content).

Remedy: reload the secret with one of the forms above and re-run the failed job (*Re-run failed jobs* or `gh run rerun <run-id> --failed --repo emanuelturtula/trading-bot`). Secrets are read when the job runs, so a new push is not needed.

While `DEPLOY_ENABLED` is not `true`, the pipeline builds the image on `main` and reports "deployment NOT performed"; nothing is deployed or released.

### 4. Application secrets (on the Pi)

Create the bot with @BotFather (`/newbot`), for example one named `<your name> trading`. **A token must never be shared with another running process** (for example a local `dev` run): a token allows one poller, and two processes polling it both log `409 Conflict` and answer nothing. Use a separate bot for local development. On the Pi:

```sh
mkdir -p ~/trading-bot
chmod 700 ~/trading-bot
umask 077
cat > ~/trading-bot/secrets.env <<'EOF'
TB_TELEGRAM_BOT_TOKEN=<bot token>
TB_TELEGRAM_ALLOWED_CHAT_IDS=<chat id>
#TB_DISPLAY_TIMEZONE=<IANA zone, default Europe/Madrid>
TB_DASHBOARD_PASSWORD_HASH=<hash, see "Dashboard" below>
TB_SESSION_SECRET=<long random value, see "Dashboard" below>
EOF
chmod 600 ~/trading-bot/secrets.env
```

Every chat in `TB_TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated) receives each signal and is the only one whose commands (`/add`, `/status`, `/pause` ...) the bot answers; messages from any other chat are ignored. To find your chat id, **before** starting the container with the token, send any message to the bot and read `message.chat.id` from `https://api.telegram.org/bot<token>/getUpdates` in a browser (never paste the token anywhere else; once the bot runs it consumes the updates and that page stays empty). Without a token or chat ids the bot still starts, only logs its signals and answers no command.

`deploy.py` never reads this file; it only creates it empty if it is missing and refuses to deploy if its permissions are more open than 600.

Changes to `secrets.env` need the container to be **recreated**, not restarted: `docker compose restart` keeps the environment the container was created with, so a new token is ignored. The next deploy recreates it; to apply a change right away, on the Pi:

```sh
cd ~/trading-bot
TB_IMAGE=$(python3 -c 'import json; print(json.load(open("current.json"))["image"])') \
TB_PORT=8081 TB_ENVIRONMENT=prod TB_SECRETS_ENV_FILE="$PWD/secrets.env" \
  docker compose --project-name trading-bot-prod --file compose.yml \
  up --detach --force-recreate --wait app
```

Check it in `docker compose --project-name trading-bot-prod logs app`: with a token and chat ids the startup line reads `started: environment prod, telegram notifier`. Then, from the phone, `/status` answers with the version. If it does not, the same logs show one line explaining why (`409 Conflict`, rejected token, Telegram unreachable); the bot keeps retrying on its own.

#### Dashboard

The configuration dashboard (spec [020](specs/020-config-dashboard.md)) is served by the bot at `http://<DEPLOY_HOST>:8081/dashboard/`. It stays disabled (every page answers `503`) until both variables are set:

```sh
# The password hash: run it in the deployed container and type the password twice (no echo)
docker compose --project-name trading-bot-prod exec app python -m trading_bot.dashboard.password
# The session secret: any long random value
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put both values in `~/trading-bot/secrets.env` as `TB_DASHBOARD_PASSWORD_HASH=...` and `TB_SESSION_SECRET=...` and recreate the container as shown above. The hash contains no `$`, so it can be pasted as is. If the startup log says `dashboard disabled: ...`, it names the variable to fix and never shows its value. Changing either value logs every open session out; sessions otherwise last 7 days. After 5 wrong passwords from one device within 15 minutes, that device has to wait out the 15 minutes.

The dashboard travels over plain HTTP: use it from the LAN or the tailnet only, and never forward port 8081 from the router.

### 5. Repository protection

Ruleset status verified on 2026-09-14 and merge settings verified on 2026-09-16 (read-only). To re-verify: `gh ruleset list --repo emanuelturtula/trading-bot`, `gh ruleset view <id> --repo emanuelturtula/trading-bot` and `gh api repos/emanuelturtula/trading-bot --jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge}'`.

- **`main` ruleset**: active on the default branch, **without bypass** (nobody can skip it, not even administrators).
  - Pull request required, with 0 required approvals.
  - Required status checks (GitHub Actions): `Secrets scan`, `Lint & types`, `Tests` and `Docker build (arm64)`. Strict mode: the PR branch must be up to date with `main`. (`Deploy beta (8082) / Deploy beta` was removed with the beta environment, spec [019](specs/019-remove-beta-rename-deploy-root.md).)
  - Required code scanning with CodeQL (default setup, languages `python` and `actions`): it blocks the merge on alerts of severity `errors` or security alerts `high_or_higher`.
  - Branch deletion and force push blocked.
  - Automatic Copilot code review on every push (not on draft PRs).
- **Merge method: squash only.** Repository settings: `allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false`; default squash title `COMMIT_OR_PR_TITLE` and message `COMMIT_MESSAGES`; `delete_branch_on_merge: false`. The `main` ruleset lists merge, squash and rebase in `allowed_merge_methods`, but the repository settings also apply: `gh pr merge --merge` fails with "Merge commits are not allowed on this repository". How to merge: [Merging pull requests](#merging-pull-requests).
- Secret scanning and push protection: **enabled**.
- Dependabot alerts and security updates: **enabled**. Their PRs, like those of version updates, are processed according to [Dependabot PRs](#dependabot-prs).
- Actions: default `GITHUB_TOKEN` permissions set to read-only (`read`), GitHub Actions cannot approve pull requests and workflows from forks require approval for first-time contributors (`first_time_contributors`).

Consequences:

- **(a) Checks run on the pull request.** The required checks come from `ci.yml`, which runs on `pull_request` events; a push to a branch without a PR runs nothing. Branches are still named `feature/<slug>` by convention (CLAUDE.md).
- **(b) Up-to-date branch (strict).** If `main` moved ahead, the branch must be updated (*Update branch* button or `git merge origin/main` + push). That push triggers CI again, and you must wait for it to finish green before merging.
- **(c) `DEPLOY_ENABLED`.** It only gates the production deploy and release on `main`; it does not block merging.
- **(d) One commit per PR on `main`.** Only the squash commit reaches `main`, and `scripts/next_version.py` computes the version from the commits since the last tag: its subject must be a Conventional Commit that describes the whole PR (see [Merging pull requests](#merging-pull-requests)).
- **(e) Stacked PRs.** After a PR is squash-merged, the next PR of the stack still contains the original commits of the merged one and is behind `main`. Force push is blocked, so it is updated with a merge and a normal push (no force push), following the procedure in [Merging pull requests](#merging-pull-requests).
- **(f) CodeQL on retargeted or reopened PRs.** CodeQL default setup analyzes a PR when it is opened or receives a push with base `main`. A PR retargeted to `main` after its last push, or closed and reopened, is not analyzed and stays blocked on the code scanning rule until a new push to its branch.

### 6. Local setup (every clone)

```sh
winget install Gitleaks.Gitleaks      # or: brew install gitleaks
pip install uv                        # or the official uv installer
uv sync
uv run pre-commit install
```

## Operations

- Status: `curl http://<DEPLOY_HOST>:8081/health` from the LAN or the tailnet.
- Logs: `docker compose --project-name trading-bot-prod logs -f app`.
- Last deploy: `~/trading-bot/current.json`; the job log of each `delivery.yml` run explains a failed deploy.
- Manual rollback: re-running the workflow of the previous commit is not allowed (run ordering protection). Revert with a PR (`git revert`) and merge.
- Database: the SQLite file lives in the `data` volume at `/app/data/trading_bot.db`, exactly the path the deploy backs up; the schema is migrated at startup, so a container that serves `/health` has already been migrated.
- Restoring the database: the backup taken before each deploy is kept in `~/trading-bot/backups/<timestamp>.sqlite3` (the last 10), and from the second deploy onwards `current.json` records the `backup` it took (the first deploy reports none, because there was no database yet).

### Managing the configuration

The tickers, the rules and their assignments live in that database. Day to day they are managed
from the [dashboard](#dashboard) or Telegram; for bulk changes and backups, use
`python -m trading_bot.cli` inside the running container (spec
[013](specs/013-config-repositories.md)). The examples use `-` for standard input and output, so
no file has to be copied into the container.

```sh
# Load a configuration file from the workstation, checking it first
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import --dry-run - < config.json
docker compose --project-name trading-bot-prod exec -T app \
  python -m trading_bot.cli config import - < config.json

# Day-to-day management
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli tickers add AAPL --timeframe 1d
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli assignments add AAPL "RSI oversold in uptrend"
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli rules enable "RSI oversold in uptrend"

# Readable backup of the whole configuration
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli config export -

# What the engine recorded, newest first, and the bot state
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli signals list --ticker AAPL --limit 10
docker compose --project-name trading-bot-prod exec app \
  python -m trading_bot.cli state show
```

- The export is a **readable companion** to the binary backup taken before each deploy, not a
  replacement for it: restoring an exact state is the backup's job.
- `config import` **merges and never deletes**. An item the file does not mention is left alone,
  so removing a rule from an exported file does not remove it from the database; `rules remove`
  does. There is no `--prune`.
- `signals list` and `state show` are read-only: they print the recorded signals (newest
  first, at most `--limit`, default 20) and the pause, the last heartbeat and the last run of
  each timeframe. They write nothing, and the signal history is not part of `config export`; the
  binary pre-deploy backup is what covers it.
- `tickers remove --force` and `rules remove --force` also delete the **signal history** of that
  row. Without `--force` the command refuses and names how many assignments and signals would
  go, and `--dry-run` shows the same report without writing.
- Every mutating command accepts `--dry-run`, which does the whole unit of work and rolls it back.
  Exit codes: `0` success, `1` the request was rejected and nothing was written, `2` usage, `3`
  the environment is unusable, including a database that is not at the expected schema revision.
- The command line never migrates the database: the application does that at startup, so run a
  deploy first if it refuses with exit code `3`.
- A configuration change takes effect on the **next** run of the engine, not immediately, and the
  application prints no confirmation of its own.
- Ready-to-import example rules, all disabled, live in `docs/examples/rules/`.

### Merging pull requests

Only with explicit user approval, and only as a squash merge (see [section 5](#5-repository-protection)):

```sh
gh pr merge <n> --repo emanuelturtula/trading-bot --squash --subject "<PR title> (#<n>)" --body "<body>"
```

- **Subject.** The PR title, a Conventional Commit in English (`<type>[(<scope>)][!]: <description>`), followed by ` (#<n>)`. `scripts/next_version.py` only sees this commit, so the type describes the whole PR: `feat` if it adds any feature (minor), `!` if any change is breaking (major, minor while < 1.0), otherwise the type of the change (patch).
- **Body.** Always explicit, for example `Closes #<issue>. Spec: docs/specs/NNN-<slug>.md.` The default body (`COMMIT_MESSAGES`) concatenates every branch commit, review fixups included, and can carry a `BREAKING CHANGE:` line into `main`, which `scripts/next_version.py` reads as a breaking change. Add a `BREAKING CHANGE:` footer only when the PR is breaking.
- After the merge, follow the `main` run of `delivery.yml` and verify the prod deploy on 8081 (`vX.Y.Z`) and the GitHub Release.

**Stacked PRs.** PR k+1 targets branch k. Merge the stack one PR at a time, from the bottom:

1. Squash-merge PR k. Do not delete branch k before PR k+1 is retargeted.
2. `git fetch origin`, `git switch <branch-k+1>` and `git pull --ff-only`. **Only then** check both preconditions (each must exit 0):
   - `git diff --quiet origin/main origin/<branch-k>`: the tree of `main` equals branch k;
   - `git merge-base --is-ancestor origin/<branch-k> HEAD`: branch k+1 contains the final branch k.

   If either fails, do **not** use `-s ours` (it would silently drop changes): still retarget first (step 3), then, instead of the `-s ours` merge and checks of steps 4 and 5, run a normal `git merge origin/main`, resolve conflicts, review `git diff origin/main HEAD` and continue with step 6.
3. Retarget PR k+1 to `main` **before pushing** (see [consequence (f)](#5-repository-protection)): `gh pr edit <k+1> --repo emanuelturtula/trading-bot --base main`.
4. Without any `git fetch` or `git pull` since step 2 (if one happened, go back to step 2), so that `origin/main` is the one checked there, note `git rev-parse "HEAD^{tree}"` and run `git merge -s ours origin/main`. The merge commit keeps the tree of branch k+1 and makes `main` an ancestor; the squash later discards it.
5. Check that `git rev-parse "HEAD^{tree}"` is unchanged **and** that `git diff --quiet origin/main origin/<branch-k>` still exits 0, so `git diff origin/main HEAD` shows only the changes of PR k+1. Reading `git diff --stat` alone is not the check. If either check fails, do not push: undo the merge with `git reset --keep HEAD~1` and go back to step 2.
6. `git push` (a normal push, never a force push), wait for CI of that push to finish green and squash-merge PR k+1 with its own subject.

**PR blocked on code scanning.** If a PR was retargeted to `main` after its last push, or closed and reopened, CodeQL does not analyze it (see [consequence (f)](#5-repository-protection)). Push a new commit to its branch (a real change or `git commit --allow-empty -m "chore: re-run checks"`), wait for CI and CodeQL to finish green, then merge.

### Dependabot PRs

Dependabot PRs (version updates and security updates) **are not merged directly**: the change is brought into a `feature/deps-<slug>` branch, so it follows the same branch naming, gate and squash subject rules as any other change.

**Light path (without the agent team).** It applies only if the change is exclusively the bump generated by Dependabot: tag or digest in the `Dockerfile`, versions in `pyproject.toml`/`uv.lock` or action SHAs in `.github/workflows/`, with no code or configuration changes. Security updates follow this same path, with priority. The lead runs it:

1. Review the Dependabot PR (the dependency's changelog, diff and CI) and create the branch from an up-to-date `main`:
   ```sh
   git fetch origin
   git switch -c feature/deps-<slug> origin/main
   ```
2. Bring in the change with `git cherry-pick <sha>` of the Dependabot commit (`gh pr view <number> --repo emanuelturtula/trading-bot --json commits --jq '.commits[].oid'` lists the SHAs), or apply the same bump by hand in a commit of your own (`build(deps): ...` or `ci(deps): ...`). Use the manual option if the cherry-pick does not apply cleanly (`git cherry-pick --abort`) or if the ruleset asks for extra approval because of unattributed commits (authored by `dependabot[bot]`).
3. Run `uv run python scripts/check.py`.
4. Push the branch.
5. Open the PR from `feature/deps-<slug>` referencing the Dependabot one (for example, "Replaces #<number>") and close the Dependabot one with a comment pointing to the replacement: `gh pr close <number> --repo emanuelturtula/trading-bot --comment "Replaced by #<new PR number>"`. Once it is closed, Dependabot does not propose that version again: if the replacement does not get merged, resume the bump from `feature/deps-<slug>` or apply it by hand (Dependabot deletes its branch when the PR is closed, so reopening it is not a reliable route).
6. Wait for CI on the PR to finish green, then merge only with explicit user approval, as a squash merge with a `build(deps): …` or `ci(deps): …` subject (see [Merging pull requests](#merging-pull-requests)).

If the bump breaks tests or requires code or configuration changes, it is no longer light and goes through the full agent team workflow (`/feature`, see [CLAUDE.md](../CLAUDE.md)).

**Python is pinned to 3.12** in `.python-version`, `requires-python` (`pyproject.toml`), `[tool.ruff] target-version`, `[tool.mypy] python_version` and the two `FROM` lines of the `Dockerfile`. Dependabot ignores its minor/major jumps (`ignore` rule of the `docker` ecosystem in `.github/dependabot.yml`). Upgrading the version is an explicit feature that updates all those places, `uv.lock` and the `ignore` rule.
