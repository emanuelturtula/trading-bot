# 019 — Remove the beta environment and rename the deploy root

- **Status:** approved
- **Branch:** `feature/remove-beta-rename-deploy-dir-texy7x`
- **Spec author:** tech-lead
- **Expected commit type:** ci

## Goal

The user no longer wants a beta instance: pushes to `feature/**` stop deploying a container on
port 8082, and production is the only deploy target. The production deploy root on the Raspberry
Pi moves from `~/trading-bot-deploy` to `~/trading-bot`.

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
- [ ] AC4: the default deploy root is `~/trading-bot`.
- [ ] AC5: when `~/trading-bot-deploy` exists and `~/trading-bot` does not, the deploy moves it once
  and rebases the absolute paths in every `current.json` (`compose`, `secrets_env_file`,
  `attempt`, `backup`), so backup and rollback keep working.
- [ ] AC6: when both folders exist, the deploy stops before any docker command with a clear error.
- [ ] AC7: docs (DEPLOYMENT, CLAUDE.md, SECURITY, PR template, `.env.example`) no longer describe
  a beta deploy or the old folder, except as migration notes.

## Design

- `.github/workflows/delivery.yml`: trigger `push: [main]`; drop the channel step, the beta tag
  step and the `deploy-beta` job; `deploy-prod` and `release` no longer need a ref condition.
- `scripts/remote_deploy.py`: single `VERSION_PATTERN`, environment `prod` only, ref must be
  `refs/heads/main`.
- `deploy/deploy.py`: `PORTS`/`VERSIONS` only for `prod`; `DEFAULT_ROOT = ~/trading-bot`,
  `LEGACY_ROOT = ~/trading-bot-deploy`; `migrate_legacy_root(legacy, root) -> bool` runs from
  `main()` only when `--root` is the default. It renames the folder under the legacy lock (same
  filesystem, atomic) and rewrites `current.json` files.

## Test plan

| Case | Type (unit/integration) | What it verifies |
|------|-------------------------|------------------|
| Valid prod request; beta request rejected | unit | AC2 |
| `environment="beta"` fails before any command | unit | AC3 |
| Compose project is `trading-bot-prod`, only port 8081 | unit | AC3 |
| Default and legacy roots | unit | AC4 |
| Legacy root moved, secrets kept, paths rebased, other fields unchanged | unit | AC5 |
| No legacy root: no-op | unit | AC5 |
| Both roots exist: error, nothing moved | unit | AC6 |

## Risks and security

- The GitHub ruleset still requires `Deploy beta (8082) / Deploy beta`; it must be removed from the
  `main` ruleset before this PR can merge, since that check will never be reported again.
- The first prod deploy after the merge performs the folder migration. If `~/trading-bot` already
  exists on the Pi (for example a clone), the deploy fails safely and the user resolves it.
- The beta container, its volume and `beta/` files stay on the Pi until the user removes them by
  hand; nothing in the pipeline deletes data.
- The Tailscale OIDC subject can be narrowed from `refs/heads/*` to `refs/heads/main`.
- No secrets involved; `secrets.env` moves with the folder and is never read.
