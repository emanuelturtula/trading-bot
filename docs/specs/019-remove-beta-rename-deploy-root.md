# 019 — Remove the beta environment and rename the deploy root

- **Status:** approved
- **Branch:** `feature/remove-beta-rename-deploy-dir-texy7x`
- **Spec author:** tech-lead
- **Expected commit type:** ci

## Goal

The user no longer wants a beta instance: pushes to `feature/**` stop deploying a container on
port 8082, and production is the only deploy target. The production deploy root on the Raspberry
Pi moves from `~/trading-bot-deploy` to `~/trading-bot`, with a flat layout: the deploy files at
the root and a `backups/` folder, without the per-deploy `attempts/` folders (user request,
2026-10-12).

## Out of scope

- The application's notion of environments (`TB_ENVIRONMENT` still accepts `dev`, `beta`, `prod`)
  and the `[BETA]` prefix outside `prod`, which now only shows on local `dev` runs.
- `scripts/next_version.py` keeps its `beta` channel; the pipeline no longer uses it.
- Removing the beta container, volume and files on the Pi, and the required status check in the
  GitHub ruleset: both are manual steps for the user (see Risks).

## Acceptance criteria

- [ ] AC1: `delivery.yml` runs only on pushes to `main`; it has no beta job and publishes only the
  `sha-<sha>` tag before the prod deploy (stable tags after it, as before).
- [ ] AC2: `scripts/remote_deploy.py` accepts only `prod` from a push to `main` with a `vX.Y.Z`
  version; a beta request is rejected before any command.
- [ ] AC3: `deploy/deploy.py` accepts only `prod` (port 8081, compose project `trading-bot-prod`,
  so the existing `data` volume is kept).
- [ ] AC4: the default deploy root is `~/trading-bot` and, after a healthy deploy, holds only
  `compose.yml`, `deploy.py`, `secrets.env`, `current.json`, `backups/` and `deploy.lock`. The new
  compose file is staged as `compose.next.yml` and replaces `compose.yml` only once healthy, so a
  rollback runs the previous one; a failed deploy leaves no staged file. The last 10 backups are
  kept as `backups/<timestamp>.sqlite3`.
- [ ] AC5: when `~/trading-bot-deploy/prod/current.json` exists and `~/trading-bot/current.json`
  does not, the deploy copies secrets, the running compose file, every attempt's database backup
  and the manifest (paths rewritten, `attempt` dropped) into the new root, once. The old folder
  is never deleted.
- [ ] AC6: when `~/trading-bot` holds anything else (a clone, for example), the deploy stops
  before any docker command with a clear error.
- [ ] AC7: docs (DEPLOYMENT, CLAUDE.md, SECURITY, PR template, `.env.example`) no longer describe
  a beta deploy or the old folder, except as migration notes.

## Design

- `.github/workflows/delivery.yml`: trigger `push: [main]`; drop the channel step, the beta tag
  step and the `deploy-beta` job; `deploy-prod` and `release` no longer need a ref condition.
- `scripts/remote_deploy.py`: single `VERSION_PATTERN`, environment `prod` only, ref must be
  `refs/heads/main`.
- `deploy/deploy.py`: `PORTS`/`VERSIONS` only for `prod`; `DEFAULT_ROOT = ~/trading-bot`,
  `LEGACY_ROOT = ~/trading-bot-deploy`; flat layout (`prepare_secrets_env_file(root)`,
  `prune_backups(backups)`, no attempt directories or per-attempt JSON evidence: failures are
  reported in the job log). `migrate_legacy_root(legacy, root) -> bool` runs from `main()` only
  when `--root` is the default; it copies under the new root's lock and writes `current.json`
  last, so an interrupted copy is redone.

## Test plan

| Case | Type (unit/integration) | What it verifies |
|------|-------------------------|------------------|
| Valid prod request; beta request rejected | unit | AC2 |
| `environment="beta"` fails before any command | unit | AC3 |
| Compose project is `trading-bot-prod`, only port 8081 | unit | AC3 |
| Default and legacy roots | unit | AC4 |
| Healthy deploy leaves only the flat files and a backup in `backups/` | unit | AC4 |
| Rollback keeps the previous `compose.yml`, no staged file left | unit | AC4 |
| Only the newest 10 backups are kept | unit | AC4 |
| Legacy state copied, paths rewritten, other fields unchanged, old folder kept | unit | AC5 |
| Migration runs once; no legacy prod state: no-op | unit | AC5 |
| Root holding something else: error, nothing written | unit | AC6 |

## Risks and security

- The GitHub ruleset still requires `Deploy beta (8082) / Deploy beta`; it must be removed from the
  `main` ruleset before this PR can merge, since that check will never be reported again.
- The first prod deploy after the merge performs the migration. If `~/trading-bot` already holds
  something else on the Pi (for example a clone), the deploy fails safely and the user resolves it.
- The beta container and volume, and the whole `~/trading-bot-deploy` folder, stay on the Pi until
  the user removes them by hand; nothing in the pipeline deletes them.
- Dropping the per-attempt JSON files loses the on-disk record of failed deploys; the job log
  keeps it.
- The Tailscale OIDC subject can be narrowed from `refs/heads/*` to `refs/heads/main`.
- No secrets involved; `secrets.env` is copied with its 0600 mode and never read.
