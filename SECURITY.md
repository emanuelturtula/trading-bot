# Security Policy

## Sensitive data

This repository is **public**. The following must never be committed, pushed, logged, or included in issues, PRs, Actions logs, or container images:

- API keys, tokens (Telegram, GitHub, Tailscale, brokers), passwords, password hashes, session secrets
- SSH private keys, certificates, `known_hosts` entries
- IP addresses (LAN or tailnet), hostnames, and usernames of the infrastructure
- `.env` files and host-side `secrets.env` files

Runtime secrets live only in environment variables loaded from `secrets.env` on the deployment host (chmod 600). CI/CD credentials live only in GitHub Actions secrets.

## Enforcement

| Layer | Control |
|-------|---------|
| Local commit | pre-commit `gitleaks` on staged changes, `detect-private-key` |
| Local push | pre-commit `gitleaks` over the full history |
| Claude Code | `git_guard.py` hook (gitleaks on commit/push; blocks `--no-verify`, `add -f`, force push, push to `main`) and read-deny rules for secret files |
| CI | `Secrets scan` job (gitleaks, full history) gates every build and deploy |
| GitHub | Secret scanning and push protection enabled |
| Runtime | `SecretStr` settings, log redaction filter, `/health` exposes no configuration |

Custom gitleaks rules (`.gitleaks.toml`) add Telegram tokens, Tailscale keys, private IPv4 addresses and Tailscale CGNAT (tailnet) addresses. Inline `gitleaks:allow` comments and `.gitleaksignore` files are rejected.

## If a secret is exposed

1. **Revoke/rotate it immediately** (removing the commit is not enough once pushed).
2. Remove it from the working tree and history, then force-push only with the owner's explicit approval.
3. Review Actions logs, GHCR images and releases for copies.

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/emanuelturtula/trading-bot/security/advisories/new) instead of a public issue.
