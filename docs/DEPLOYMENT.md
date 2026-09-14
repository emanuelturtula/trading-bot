# Deploy

> This document is public: it uses placeholders. **Never** write real IPs (LAN or tailnet), hostnames, users, keys or tokens here.

## Summary

| Event | Pipeline | Image | Target |
|-------|----------|-------|--------|
| Pull request | `ci.yml` | arm64 build without push | — |
| Push to `feature/**` | `delivery.yml` | `ghcr.io/emanuelturtula/trading-bot:vX.Y.Z-beta.<sha7>` | container `trading-bot-beta`, port **8082** |
| Push to `main` (merge) | `delivery.yml` | `...:sha-<sha>`; after a healthy deploy: `:vX.Y.Z` and `:latest` | container `trading-bot-prod`, port **8081**, git tag + GitHub Release |

`delivery.yml` flow: `ci.yml` (gitleaks → lint/mypy → tests) → `next_version.py` → native arm64 build (`ubuntu-24.04-arm`) → push to GHCR → `remote-deploy.yml` → release (main only).

`remote-deploy.yml` joins the runner to the tailnet with Tailscale OIDC (ephemeral node with `tag:trading-bot-ci`), and `scripts/remote_deploy.py`:

1. validates all inputs and that the run is a push from this repo (`main` → prod, `feature/**` → beta);
2. copies `deploy/deploy.py` and `deploy/compose.yml` to a temporary directory on the Pi;
3. runs `docker login ghcr.io` with the job's ephemeral token **via stdin**;
4. runs `deploy.py` and deletes the temporary directory.

`deploy/deploy.py` (on the Pi):

- takes a host lock (beta and prod never deploy in parallel);
- rejects runs older than the deployed one (an old rerun does not overwrite a new one);
- runs `docker pull` by digest and verifies the OCI labels `revision` and `version`;
- backs up the SQLite database of the current container;
- `docker compose up --wait` and verifies that the exact digest is running with a `healthy` healthcheck;
- if it fails, restores the previous deploy; if there was none, stops the container and keeps the volume;
- writes `current.json` only if the deploy ended up healthy and keeps the last 10 attempts.

## Layout on the Raspberry

```
~/trading-bot-deploy/                 (chmod 700)
  deploy.lock
  beta/  secrets.env (600)  current.json  attempts/<id>/{compose.yml,request.json,previous.json,database.sqlite3,result.json}
  prod/  secrets.env (600)  current.json  attempts/...
```

Compose projects: `trading-bot-beta` (8082 → 8000) and `trading-bot-prod` (8081 → 8000), each with its own `data` volume.

## Initial setup (only once)

Requirements on the Pi: Docker with Compose v2, `python3` and the Pi joined to the tailnet.

### 1. Tailscale

1. In the tailnet policy, **add** (without replacing the existing policy) the tag `tag:trading-bot-ci` with a suitable tag owner and access **only** to TCP port 22 of the Raspberry.
2. Create an **OpenID Connect trust** credential with the GitHub issuer and subject:
   `repo:emanuelturtula@<OWNER_ID>/trading-bot@<REPO_ID>:ref:refs/heads/*`
   The IDs are obtained with `gh api repos/emanuelturtula/trading-bot --jq '.owner.id, .id'`. Scope: only *Auth Keys write* with `tag:trading-bot-ci`. Do not widen the subject to other repos or to pull requests.
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

Symptoms of an empty secret in the deploy job (`Deploy beta (8082) / Deploy beta` or `Deploy production (8081) / Deploy prod`):

- the `Join tailnet (OIDC, ephemeral)` step fails with `Please provide either an auth key, OAuth secret and tags, or federated identity client ID and audience with tags.` (`TS_OAUTH_CLIENT_ID` or `TS_AUDIENCE` empty);
- the `Deploy and verify health` step fails with `ValueError: Invalid host` (or `Invalid user`) from `scripts/remote_deploy.py` (`DEPLOY_HOST` or `DEPLOY_USER` empty);
- `DEPLOY_SSH_KEY` or `DEPLOY_KNOWN_HOSTS` empty or containing Windows CRs end in `ssh` errors (`Permission denied (publickey)`, `Host key verification failed`);
- in the log, the step's inputs and environment variables show the value **blank** instead of `***` (GitHub only masks secrets that have content).

Remedy: reload the secret with one of the forms above and re-run the failed job (*Re-run failed jobs* or `gh run rerun <run-id> --failed --repo emanuelturtula/trading-bot`). Secrets are read when the job runs, so a new push is not needed.

While `DEPLOY_ENABLED` is not `true`, the pipeline builds the image and reports "deployment NOT performed", and no PR can be merged because the required beta deploy check is never reported (see [section 5, consequence (c)](#5-repository-protection)).

### 4. Application secrets (on the Pi)

Create a different bot per environment with @BotFather. On the Pi:

```sh
mkdir -p ~/trading-bot-deploy/beta ~/trading-bot-deploy/prod
chmod 700 ~/trading-bot-deploy
umask 077
cat > ~/trading-bot-deploy/beta/secrets.env <<'EOF'
TB_TELEGRAM_BOT_TOKEN=<beta bot token>
TB_TELEGRAM_ALLOWED_CHAT_IDS=<chat id>
TB_DASHBOARD_PASSWORD_HASH=<hash>
TB_SESSION_SECRET=<long random value>
EOF
chmod 600 ~/trading-bot-deploy/beta/secrets.env
```

Repeat for `prod/secrets.env` with the production bot token. `deploy.py` never reads this file; it only creates it empty if it is missing and refuses to deploy if its permissions are more open than 600. Changes apply on restart: `docker compose --project-name trading-bot-beta restart app` (or `trading-bot-prod`).

### 5. Repository protection

Status verified on 2026-09-14 (read-only). To re-verify it: `gh ruleset list --repo emanuelturtula/trading-bot` and `gh ruleset view <id> --repo emanuelturtula/trading-bot`.

- **`main` ruleset**: active on the default branch, **without bypass** (nobody can skip it, not even administrators).
  - Pull request required, with 0 required approvals.
  - Required status checks (GitHub Actions): `Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)` and `Deploy beta (8082) / Deploy beta`. Strict mode: the PR branch must be up to date with `main`.
  - Required code scanning with CodeQL (default setup, languages `python` and `actions`): it blocks the merge on alerts of severity `errors` or security alerts `high_or_higher`.
  - Branch deletion and force push blocked.
  - Automatic Copilot code review on every push (not on draft PRs).
- Secret scanning and push protection: **enabled**.
- Dependabot alerts and security updates: **enabled**. Their PRs, like those of version updates, are processed according to [Dependabot PRs](#dependabot-prs).
- Actions: default `GITHUB_TOKEN` permissions set to read-only (`read`), GitHub Actions cannot approve pull requests and workflows from forks require approval for first-time contributors (`first_time_contributors`).

Consequences:

- **(a) Every mergeable PR comes from a `feature/**` branch with beta deployed.** `Docker build (arm64)` only runs on `pull_request` events (`ci.yml`) and `Deploy beta (8082) / Deploy beta` only on push to `feature/**` (`delivery.yml`). A PR from any other branch never reports the beta deploy check; Dependabot PRs are processed according to [Dependabot PRs](#dependabot-prs).
- **(b) Up-to-date branch (strict).** If `main` moved ahead, the branch must be updated (*Update branch* button or `git merge origin/main` + push). That push triggers CI and the beta deploy again, and you must wait for them to finish green before merging.
- **(c) `DEPLOY_ENABLED`.** If the variable is not `true`, the `Deploy beta (8082)` job is skipped, the required check `Deploy beta (8082) / Deploy beta` is not reported (it keeps waiting for the status) and **no PR can be merged**.

### 6. Local setup (every clone)

```sh
winget install Gitleaks.Gitleaks      # or: brew install gitleaks
pip install uv                        # or the official uv installer
uv sync
uv run pre-commit install
```

## Operations

- Status: `curl http://<DEPLOY_HOST>:8082/health` (beta) and `:8081/health` (prod) from the LAN or the tailnet.
- Logs: `docker compose --project-name trading-bot-prod logs -f app`.
- Last deploy: `~/trading-bot-deploy/prod/current.json`; history in `attempts/`.
- Manual rollback: re-running the workflow of the previous commit is not allowed (run ordering protection). Revert with a PR (`git revert`) and merge.
- Restoring the database: the backup taken before each deploy is kept in `attempts/<id>/database.sqlite3`.

### Dependabot PRs

Dependabot PRs (version updates and security updates) **are not merged directly**. Their `dependabot/**` branches do not trigger `delivery.yml` (it only runs on push to `feature/**` and `main`) and `scripts/remote_deploy.py` only accepts beta deploys from `refs/heads/feature/**`, so the required check `Deploy beta (8082) / Deploy beta` is never reported and the PR stays blocked (see [section 5](#5-repository-protection)). The change is brought into a `feature/deps-<slug>` branch.

**Light path (without the agent team).** It applies only if the change is exclusively the bump generated by Dependabot: tag or digest in the `Dockerfile`, versions in `pyproject.toml`/`uv.lock` or action SHAs in `.github/workflows/`, with no code or configuration changes. Security updates follow this same path, with priority. The lead runs it:

1. Review the Dependabot PR (the dependency's changelog, diff and CI) and create the branch from an up-to-date `main`:
   ```sh
   git fetch origin
   git switch -c feature/deps-<slug> origin/main
   ```
2. Bring in the change with `git cherry-pick <sha>` of the Dependabot commit (`gh pr view <number> --repo emanuelturtula/trading-bot --json commits --jq '.commits[].oid'` lists the SHAs), or apply the same bump by hand in a commit of your own (`build(deps): ...` or `ci(deps): ...`). Use the manual option if the cherry-pick does not apply cleanly (`git cherry-pick --abort`) or if the ruleset asks for extra approval because of unattributed commits (authored by `dependabot[bot]`).
3. Run `uv run python scripts/check.py`.
4. Push the branch: CI and the beta deploy run (port 8082). Verify with `curl http://<DEPLOY_HOST>:8082/health` that the response contains `status` `ok`, `environment` `beta` and the `version` of that run (`vX.Y.Z-beta.<sha7>`, the one in the run summary). A healthy `/health` with the previous version does not prove that the bump is deployed.
5. Open the PR from `feature/deps-<slug>` referencing the Dependabot one (for example, "Replaces #<number>") and close the Dependabot one with a comment pointing to the replacement: `gh pr close <number> --repo emanuelturtula/trading-bot --comment "Replaced by #<new PR number>"`. Once it is closed, Dependabot does not propose that version again: if the replacement does not get merged, resume the bump from `feature/deps-<slug>` or apply it by hand (Dependabot deletes its branch when the PR is closed, so reopening it is not a reliable route).
6. Merge only with explicit user approval.

If the bump breaks tests or requires code or configuration changes, it is no longer light and goes through the full agent team workflow (`/feature`, see [CLAUDE.md](../CLAUDE.md)).

**Python is pinned to 3.12** in `.python-version`, `requires-python` (`pyproject.toml`), `[tool.ruff] target-version`, `[tool.mypy] python_version` and the two `FROM` lines of the `Dockerfile`. Dependabot ignores its minor/major jumps (`ignore` rule of the `docker` ecosystem in `.github/dependabot.yml`). Upgrading the version is an explicit feature that updates all those places, `uv.lock` and the `ignore` rule.
