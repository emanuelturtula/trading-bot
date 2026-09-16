---
name: feature
description: Develops a trading-bot feature end to end with the agent team (tech-lead, developer, tester), from the spec to the PR verified on beta.
disable-model-invocation: true
argument-hint: "<feature description>"
---

# /feature — mandatory Agent Team workflow

Request: $ARGUMENTS

Follow `CLAUDE.md` (section "Mandatory feature workflow (Agent Team)"). You are the **lead**.

## 0. Preparation
1. Read `CLAUDE.md`, `docs/ARCHITECTURE.md` and `docs/ROADMAP.md`. If the request contradicts an unbreakable rule (for example, executing orders), stop and explain it to the user.
2. Verify that `git status` is clean and start from an up-to-date `main`: `git fetch origin` and `git switch -c feature/<slug> origin/main` (the `<slug>` is in English).
3. If the request is functionally ambiguous, ask the user before spawning the team.

## 1. Team
Spawn three teammates **using the agent definitions**, with fixed names:
- `tech-lead` (agent type `tech-lead`)
- `developer` (agent type `developer`)
- `tester` (agent type `tester`)

Teammates do not inherit this conversation: in each teammate's prompt include the full request (with an English translation if the user wrote in another language), the branch, the user's answers, which task they own, and a reminder that every artifact must be in English (`CLAUDE.md`, rule 9).

## 2. Shared tasks
Create the tasks with these exact prefixes (the `TaskCompleted` hook depends on them) and chained dependencies:
1. `[spec] <slug>` → tech-lead
2. `[impl] <slug>` → developer (blocked by spec)
3. `[test] <slug>` → tester (blocked by impl)
4. `[review] <slug>` → tech-lead (blocked by test)

## 3. Coordination
- When the spec is ready, read it. If product decisions come up, consult the user and forward the answers.
- Let developer, tester and tech-lead iterate through messages (FAIL → developer; REQUEST CHANGES → developer).
- Do not implement yourself: if a teammate gets stuck, redirect them or reassign the task.

## 4. Integration and delivery (lead only)
1. With `[review]` at APPROVE, run `uv run python scripts/check.py` and review the full diff looking for secrets, IPs, hostnames or users.
2. Commits in Conventional Commits in English, staging explicit paths (never a blind `git add -A` or `commit -a`).
3. `git push -u origin feature/<slug>`. The hooks run gitleaks; if they block, fix the cause and never use `--no-verify`.
4. Follow `delivery.yml` with `gh run watch`. A push or a built image is not a verified deploy: confirm the beta job is green and, if you have access, `/health` on port 8082 with version `vX.Y.Z-beta.<sha7>`.
5. Open the PR with `gh pr create` using `.github/PULL_REQUEST_TEMPLATE.md`. The PR title is a Conventional Commit in English (it becomes the squash commit subject); the description is in English: spec link, verdicts and beta evidence.
6. Shut down the team (ask each teammate to finish).
7. Report to the user (in the user's language): what was done, beta version, PR link and what to test. **Do not merge without explicit approval.**

## 5. Merge (only with explicit user approval)
Squash is the only merge method the repository allows (`--merge` is rejected):
`gh pr merge <n> --squash --subject "<PR title> (#<n>)" --body "<body>"`, with an explicit body such as `Closes #<issue>. Spec: docs/specs/NNN-<slug>.md.`
The squash commit is the only commit that reaches `main` and `scripts/next_version.py` computes the version from it: the subject type must describe the whole PR (`feat` if it adds any feature, `!` if any change is breaking). For stacked PRs or a PR blocked on code scanning, follow "Merging pull requests" in `docs/DEPLOYMENT.md`. Then follow the `main` run and verify the prod deploy on 8081 (`vX.Y.Z`) and the GitHub Release.
