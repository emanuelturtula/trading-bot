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

Load the values with `gh secret set <NAME>` (it asks for the value via stdin, so it does not stay in the shell history). While `DEPLOY_ENABLED` is not `true`, the pipeline builds the image and reports "deployment NOT performed".

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

- Secret scanning and push protection: **enabled** (verified).
- Ruleset for `main`: mandatory PR, required checks (`Secrets scan`, `Lint & types`, `Tests`), no force push or deletion.
- Actions: default `GITHUB_TOKEN` permissions set to read-only and approval required for workflows from forks.

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
