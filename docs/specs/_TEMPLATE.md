# NNN — <feature title>

- **Status:** draft | approved | implemented
- **Branch:** `feature/<slug>`
- **Spec author:** tech-lead
- **Expected commit type:** feat | fix | refactor | …

## Goal

What problem it solves and for whom. One or two sentences.

## Out of scope

What is explicitly not done in this feature.

## Acceptance criteria

- [ ] AC1: …
- [ ] AC2: …

## Design

- Affected modules and files (with owner: developer or tester).
- New or modified interfaces/Protocols (signatures).
- Data model or migrations.
- New configuration (`TB_*`): name, type, whether it is a secret.

## Test plan

| Case | Type (unit/integration) | What it verifies |
|------|-------------------------|------------------|
| … | … | … |

Mandatory depending on the case: anti look-ahead (indicators/rules), idempotency (signals), authorization (Telegram/API), secret redaction (config/logs).

## Risks and security

- Sensitive data involved and how it is protected.
- Deploy impact (migrations, new variables in `secrets.env`).

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
