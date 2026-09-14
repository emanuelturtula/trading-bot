<!-- Write the PR title and every section in English (CLAUDE.md, rule 9). -->
## What changes

<!-- Short summary. Link to the spec: docs/specs/NNN-<slug>.md -->

## Type

- [ ] feat
- [ ] fix
- [ ] refactor / chore / docs / ci / build
- [ ] breaking change (`!`)

## Agent team workflow

- [ ] Spec approved by tech-lead
- [ ] Implementation (developer) with TDD
- [ ] Verification (tester): PASS with evidence
- [ ] Final review (tech-lead): approve

## Checklist

- [ ] `uv run python scripts/check.py` green
- [ ] No secrets, IPs, hostnames or infrastructure users in the diff (gitleaks OK)
- [ ] `domain/` is still pure; no look-ahead (closed candles only)
- [ ] No order execution code
- [ ] Beta verified on port 8082 (`/health` with the beta version)
- [ ] Everything in English: code, comments, docs, commits, and this PR

## Beta evidence

<!-- version, image digest, link to the run -->
