# 001 — Dependabot without Python version jumps and repository protection docs

- **Status:** implemented
- **Branch:** `feature/dependabot-python-pin-docs`
- **Spec author:** tech-lead
- **Expected commit type:** `chore:` (`.github/dependabot.yml`) and `docs:` (documentation). Neither is `feat` → patch bump: beta `v0.1.1-beta.<sha7>`, prod `v0.1.1`.

## Goal

Stop Dependabot from proposing minor/major jumps of the `python` image again (PR #2, `3.12-slim` → `3.14-slim`, broke `Docker build (arm64)` because the project pins Python 3.12) and align the documentation with the real protection of `main`. That protection means Dependabot PRs cannot be merged directly, so a light path to process them is formalized (decision D1) as an explicit exception to the agent team workflow. In addition, the guide for loading secrets in PowerShell, which stored empty values without warning, is fixed.

Real configuration verified by the tech-lead with `gh api` (read-only) on 2026-09-14:

| Item | Real value |
|------|------------|
| Ruleset | name `main`, `enforcement: active`, target `~DEFAULT_BRANCH`, `bypass_actors: []` |
| Pull request | required, `required_approving_review_count: 0` |
| Status checks | `Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)`, `Deploy beta (8082) / Deploy beta` (GitHub Actions), `strict_required_status_checks_policy: true` |
| Code scanning | CodeQL, `alerts_threshold: errors`, `security_alerts_threshold: high_or_higher`; default setup `configured`, languages `actions` and `python` |
| Other rules | `deletion` and `non_fast_forward` (no deletion or force push); `copilot_code_review` with `review_on_push: true`, `review_draft_pull_requests: false` |
| Repo security | secret scanning `enabled`, push protection `enabled`; Dependabot alerts enabled (`vulnerability-alerts` → 204); Dependabot security updates `enabled` (`automated-security-fixes`: `enabled: true`, `paused: false`), enabled by the lead (D2) |
| Actions | `default_workflow_permissions: read`, `can_approve_pull_request_reviews: false`, approval of workflows from forks: `first_time_contributors` |

## Out of scope

- Any change in `src/`, `tests/`, `scripts/`, `deploy/`, `.github/workflows/`, `.claude/`, `.gitleaks.toml`.
- `Dockerfile`, `pyproject.toml`, `uv.lock`, `.python-version`: Python stays on 3.12.
- The `github-actions` and `uv` ecosystems of `dependabot.yml`, and the existing `schedule`/`commit-message`.
- Modifying the ruleset or any GitHub setting: only what already exists is documented, including the alerts and security updates that the lead enabled.
- Pinning the base image by digest or automating the creation of `feature/deps-*` branches for Dependabot PRs.
- Updating `.claude/skills/feature/SKILL.md` or the agent definitions: the light path is run by the lead and does not use that skill.
- `README.md`, `docs/ARCHITECTURE.md`.

## Acceptance criteria

### `.github/dependabot.yml`

- [ ] **AC1:** the `package-ecosystem: docker` entry has `ignore` with exactly one element: `dependency-name: python` and `update-types` = `version-update:semver-major` and `version-update:semver-minor`. It does not include `version-update:semver-patch` or `versions`.
- [ ] **AC2:** apart from that `ignore` key, the file is semantically identical to the one on `origin/main`. It is also valid YAML: it passes the V1 script and the `check-yaml` hook.
- [ ] **AC3:** a YAML comment in English, above `ignore`, explains that Python is pinned to 3.12 and that upgrading it is an explicit feature, not a Dependabot bump.

### `docs/DEPLOYMENT.md`

- [ ] **AC4 (section 5, configuration):** the section "5. Repository protection" replaces the old ruleset line (which listed 3 checks) and describes the real configuration of the table above:
  - `main` ruleset on the default branch and without bypass;
  - required PR with 0 approvals;
  - the 5 checks with their literal name in backticks and the requirement of a branch up to date with `main` (strict);
  - required CodeQL (default setup, `python` and `actions`, thresholds `errors` / `high_or_higher`);
  - deletion and force push blocked;
  - Copilot code review on every push;
  - secret scanning and push protection enabled;
  - Dependabot alerts and Dependabot security updates enabled (D2);
  - the Actions line with the real values.

  It states the verification date (2026-09-14).
- [ ] **AC5 (section 5, consequences):** the section explains:
  - (a) `Docker build (arm64)` only runs on `pull_request` events and `Deploy beta (8082) / Deploy beta` only on push to `feature/**`, so every mergeable PR comes from a `feature/**` branch with beta deployed;
  - (b) because of strict, if `main` moved ahead the branch must be updated, and that push triggers CI and the beta deploy again;
  - (c) if `DEPLOY_ENABLED` is not `true`, the beta deploy check is not reported and no PR can be merged.

  The section 3 sentence about `DEPLOY_ENABLED` refers to (c).
- [ ] **AC6 (Dependabot procedure, light path, D1):** there is a subsection `### Dependabot PRs` (anchor `#dependabot-prs`), linked from section 5, that contains:
  - **the reason:** `dependabot/**` branches do not trigger `delivery.yml` and `scripts/remote_deploy.py` only accepts beta from `refs/heads/feature/**`, so they never satisfy the required beta deploy check;
  - **the light path condition:** the change is **only** the Dependabot bump (`Dockerfile`, `pyproject.toml`/`uv.lock` or action SHAs), with no code or config changes. The lead runs it, without the agent team;
  - **the 6 steps, numbered and in this order:**
    1. the lead creates `feature/deps-<slug>` from `origin/main` (`git fetch origin` + `git switch -c feature/deps-<slug> origin/main`);
    2. `git cherry-pick <sha>` of the Dependabot commit, or the same bump by hand in a commit of its own `build(deps): …` or `ci(deps): …`. If the ruleset requires extra approval for unattributed commits, the manual option is used;
    3. `uv run python scripts/check.py`;
    4. push → CI + beta deploy (8082) → verify `/health` (with the placeholder `<DEPLOY_HOST>`) and the beta version;
    5. PR from `feature/deps-<slug>` that references the Dependabot one; the Dependabot one is closed with a comment pointing to the new PR;
    6. merge only with explicit user approval;
  - **the exit from the light path:** if the bump breaks tests or requires code or config changes, the full workflow with `/feature` is used;
  - **Dependabot security updates** follow the same path, with priority;
  - **the Python note:** pinned to 3.12 in `.python-version`, `requires-python`, `[tool.ruff] target-version`, `[tool.mypy] python_version` and the two `FROM` lines of the `Dockerfile`. Dependabot ignores its minor/major jumps; upgrading it is an explicit feature with `/feature` (never the light path) that updates those places, `uv.lock` and the `ignore` rule.
- [ ] **AC7 (section 3, secrets):** the recommendation of the interactive prompt (`gh secret set <NAME>` "asks for the value via stdin") is removed. In its place there are:
  - a warning: in PowerShell that prompt can store an empty value without an error;
  - a `powershell` block with the three verified forms: `--body "<value>"` only for non-sensitive values; `$v = Read-Host ...; gh secret set ... --body $v` and then `Remove-Variable v`; and for files, `` --body ((Get-Content "$HOME\.ssh\<key>" -Raw) -replace "`r", "") ``, noting that `<` does not exist in PowerShell;
  - the symptoms in CI: the Tailscale action message, `ValueError: Invalid host` from `remote_deploy.py` and the blank value instead of `***` in the log;
  - the remedy: reload the secret and re-run the job.

  Everything with placeholders (`<NAME>`, `<value>`, `<key>`, `<file>`).

### `SECURITY.md`

- [ ] **AC8:** the `GitHub` row of the "Enforcement" table, in English like the rest of the file, keeps secret scanning + push protection and adds: Dependabot alerts and security updates enabled (D2), `main` ruleset without bypass with required PR, required status checks (CI and beta deploy) on an up-to-date branch, required CodeQL, and no force push or deletion. The table still has 2 columns.

### `docs/ROADMAP.md`

- [ ] **AC9:** the table has a `Status` column after `Feature`: F0 = `Completed (v0.1.0)` and F1–F8 = `Pending`. All rows have the same number of columns.

### `CLAUDE.md`

- [ ] **AC10a (exception in the workflow, D1):** in "Mandatory feature workflow (Agent Team)", immediately after the paragraph "Every feature, fix or change… goes through the team…" and before the roles table, there is an **explicit exception** paragraph for Dependabot PRs that says:
  - it applies only if the change is solely the bump (`Dockerfile`, `pyproject.toml`/`uv.lock` or action SHAs), with no code or config changes;
  - the lead runs it without the agent team, on `feature/deps-<slug>`: cherry-pick or manual bump `build(deps)`/`ci(deps)` → `scripts/check.py` → beta 8082 with `/health` verified → PR that references the Dependabot one, which is closed with a comment → merge only with explicit user approval;
  - security updates go through the same path, with priority;
  - if it breaks tests or requires code/config, the full workflow with `/feature` is used;
  - it links to `docs/DEPLOYMENT.md#dependabot-prs`.
- [ ] **AC10b (Git, versioning and delivery):** there is **one** new bullet (≤ 3 lines) that says:
  - Dependabot PRs are not merged directly: they go through a `feature/deps-*` branch following the exception in "Mandatory feature workflow";
  - Python is pinned to 3.12: Dependabot ignores its minor/major jumps and upgrading the version is an explicit feature (never the light path).
- [ ] **AC10c (no other changes):** in `CLAUDE.md` only those two blocks are added. The unbreakable rules, the roles table, steps 1–9, "Teammates never…" and "Active protections" do not change.

### Cross-cutting

- [ ] **AC11 (scope):** the only files modified or added relative to `origin/main` are `.github/dependabot.yml`, `docs/DEPLOYMENT.md`, `SECURITY.md`, `docs/ROADMAP.md`, `CLAUDE.md` and this spec.
- [ ] **AC12 (sensitive data):** no changed file contains IPs, hostnames, infrastructure users, keys, tokens or real secret values; only placeholders. `gitleaks dir` passes on each changed file and the regex search (V8) finds nothing.
- [ ] **AC13 (gate):** `uv run python scripts/check.py` and `uv run pre-commit run --files <changed files>` green.

## Design

### Files and owners

| File | Owner | Change |
|------|-------|--------|
| `.github/dependabot.yml` | developer | `ignore` in the `docker` ecosystem (AC1–AC3) |
| `docs/DEPLOYMENT.md` | developer | Sections 3 and 5 and the `### Dependabot PRs` subsection in "Operations" (AC4–AC7) |
| `SECURITY.md` | developer | `GitHub` row (AC8) |
| `docs/ROADMAP.md` | developer | `Status` column (AC9) |
| `CLAUDE.md` | developer | Exception paragraph in "Mandatory feature workflow" and one bullet in "Git, versioning and delivery" (AC10a–AC10c) |
| — | tester | Does not add or modify files: runs the verification plan and reports |

**Explicit authorization:** this spec assigns `.github/dependabot.yml` (the only file under `.github/`) and `CLAUDE.md` to the developer. There are no new Protocols, interfaces, migrations or `TB_*` variables.

**TDD applied to configuration:** before editing, the developer runs V1 and confirms that it fails (the tech-lead already verified it: `AC1 ignore mismatch: None`). After editing it must pass.

### `.github/dependabot.yml` (expected text of the docker block)

```yaml
  - package-ecosystem: docker
    directory: /
    schedule:
      interval: weekly
    commit-message:
      prefix: build
    # Python is pinned to 3.12 (.python-version, pyproject.toml). Upgrading it is an
    # explicit feature, never a Dependabot bump; patch updates are still allowed.
    ignore:
      - dependency-name: python
        update-types:
          - version-update:semver-major
          - version-update:semver-minor
```

### `docs/DEPLOYMENT.md`

**Section 3.** Replace the paragraph "Load the values with `gh secret set <NAME>`…" with the warning, this block and the symptoms. The `DEPLOY_ENABLED` sentence is kept, with the reference to section 5 added to it.

```powershell
# Non-sensitive value (it is written on the command line)
gh secret set <NAME> --repo emanuelturtula/trading-bot --body "<value>"

# Value pasted visibly, without writing it on the command line
$v = Read-Host "<NAME>"
gh secret set <NAME> --repo emanuelturtula/trading-bot --body $v
Remove-Variable v

# File: PowerShell has no `<` input redirection; Windows CRs are removed
gh secret set DEPLOY_SSH_KEY --repo emanuelturtula/trading-bot --body ((Get-Content "$HOME\.ssh\<key>" -Raw) -replace "`r", "")
```

Optional: a line for bash/zsh with `gh secret set <NAME> --repo emanuelturtula/trading-bot < <file>`.

**Section 5.** Suggested structure (the developer may adjust the wording as long as it covers AC4 and AC5):

1. "Status verified on 2026-09-14."
2. "`main` ruleset" bullet with sub-bullets: required PR (0 approvals), checks + strict, CodeQL, deletion/force push, Copilot review.
3. Bullets for secret scanning/push protection, for Dependabot alerts + security updates and for Actions.
4. "Consequences" paragraph or list with (a), (b), (c) and the link to `#dependabot-prs`.

Optional: how to re-verify it with `gh ruleset list --repo emanuelturtula/trading-bot`.

**Operations → `### Dependabot PRs`.** It goes at the end of "Operations" because it is a recurring procedure, not a setup step. Content according to AC6. The suggested order is: reason → light path condition → 6 steps → exit to `/feature` → security updates with priority → Python note. It is the detailed version of the `CLAUDE.md` exception (AC10a): both must say the same thing.

### `SECURITY.md` (suggested text)

```markdown
| GitHub | Secret scanning and push protection enabled; Dependabot alerts and security updates enabled; `main` ruleset without bypass: pull request required, required status checks (`Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)`, beta deploy) on an up-to-date branch, required CodeQL code scanning, no force push or deletion |
```

### `CLAUDE.md` (suggested texts)

Exception, right after the introductory paragraph of "Mandatory feature workflow (Agent Team)" (AC10a):

```markdown
**Exception — Dependabot PRs (light path).** If the change is only the Dependabot bump (`Dockerfile`, `pyproject.toml`/`uv.lock` or action SHAs), with no code or config changes, the lead processes it without the agent team: branch `feature/deps-<slug>` from `origin/main` → cherry-pick of the Dependabot commit (or the same bump in a commit of its own, `build(deps)`/`ci(deps)`) → `scripts/check.py` → push, beta 8082 and `/health` verified → PR that references the Dependabot one, which is closed with a comment → merge only with explicit user approval. Security updates go through the same path, with priority. If it breaks tests or requires code/config: full workflow with `/feature`. Details in [Deploy](docs/DEPLOYMENT.md#dependabot-prs).
```

New bullet in "Git, versioning and delivery" (AC10b):

```markdown
- Dependabot: its PRs are not merged directly (they do not deploy to beta, which is a required check); they are brought into `feature/deps-<slug>` following the exception in "Mandatory feature workflow (Agent Team)". Python is pinned to 3.12: Dependabot ignores its minor/major jumps and upgrading the version is an explicit feature (never the light path).
```

## Test plan

There is no runtime behavior: no tests are added to `tests/`. Verification consists of reproducible commands; V1 plays the role of the "test that fails without the implementation". Mandatory cases of the template:

- anti look-ahead, idempotency and authorization: **N/A** (there are no indicators, rules, signals, Telegram or API);
- secret redaction: applies to the content of the documentation and is covered by V8.

**Important for tester and review:** teammates do not commit, so the changes are in the working tree. The local `main` of this worktree is stale, so compare against **`origin/main`** with `git diff origin/main` + `git status --porcelain` (not `git diff main...HEAD`). `scripts/secret_scan.py --history` only scans commits and does not see uncommitted changes; that is why V8 uses `gitleaks dir`.

| Case | Type | What it verifies | AC |
|------|------|------------------|----|
| V1 | config | Script below with the venv Python: it must print `dependabot.yml OK`. It fails on `origin/main` | AC1, AC2 |
| V2 | config | `uv run pre-commit run --files .github/dependabot.yml docs/DEPLOYMENT.md SECURITY.md docs/ROADMAP.md CLAUDE.md docs/specs/001-dependabot-python-pin-docs.md` green (`check-yaml`, EOF, trailing whitespace, `detect-private-key`) | AC2, AC13 |
| V3 | docs | In `docs/DEPLOYMENT.md` the 5 check names, `python`, `actions`, `errors`, `high_or_higher`, `Copilot`, `push protection`, `Dependabot alerts`, `security updates` and `2026-09-14` appear literally, and the old line with 3 checks is **not** left. Reading of (a), (b) and (c) | AC4, AC5 |
| V4 | docs | `### Dependabot PRs` exists and section 5 links to `#dependabot-prs`. The subsection includes: `refs/heads/feature/**`; the "only the bump" condition with `Dockerfile`, `uv.lock` and action SHAs; `feature/deps-`; `cherry-pick`; `build(deps)` and `ci(deps)`; the fallback for extra approval of unattributed commits; `scripts/check.py`; `8082` and `/health`; closing the Dependabot PR with a comment; explicit user approval; `/feature` as the exit; security updates with priority; and the places where Python is pinned. The 6 steps are in the order of AC6 | AC6 |
| V5 | docs | Section 3: without "asks for the value via stdin"; with `Read-Host`, `Remove-Variable`, `--body`, `Get-Content`, `-replace`, the Tailscale message, `Invalid host` and `***` | AC7 |
| V6 | docs | `GitHub` row of `SECURITY.md` with `Dependabot`, `ruleset` and `CodeQL` (AC8); `ROADMAP.md` columns with the one-liner below (AC9); `git diff origin/main -- CLAUDE.md` shows exactly two added blocks, with no deleted lines: the exception paragraph between the introductory paragraph and the roles table of "Mandatory feature workflow" (AC10a) and one bullet in "Git, versioning and delivery" (AC10b, AC10c). The exception matches the 6 steps of `docs/DEPLOYMENT.md` and the linked anchor exists | AC8–AC10c |
| V7 | scope | `git diff origin/main --name-only` + `git status --porcelain` list only the 6 files of AC11 | AC11 |
| V8 | security | `gitleaks dir --config .gitleaks.toml --redact --no-banner --ignore-gitleaks-allow --exit-code 1 <file>` **file by file** (with several paths it returns an error) with exit 0 for each; grep below without matches (exit 1); manual review that there are only placeholders | AC12 |
| V9 | gate | `uv run python scripts/check.py` and `python scripts/secret_scan.py --history` with exit 0 | AC13 |
| V10 | post-merge (lead, manual, does not block the PR) | Dependabot reads the config from `main`: in Insights → Dependency graph → Dependabot, the docker ecosystem shows no configuration error | AC1 |

`docker build` is not mandatory because the `Dockerfile` does not change; `Docker build (arm64)` covers it on the PR anyway.

**V1** (save it in a temporary file outside the repo and run it from the root with `.venv/Scripts/python.exe`; PyYAML is already in the venv as a transitive dependency of pre-commit):

```python
import copy
import subprocess

import yaml

EXPECTED_IGNORE = [
    {
        "dependency-name": "python",
        "update-types": ["version-update:semver-major", "version-update:semver-minor"],
    }
]

new = yaml.safe_load(open(".github/dependabot.yml", encoding="utf-8"))
old = yaml.safe_load(
    subprocess.run(
        ["git", "show", "origin/main:.github/dependabot.yml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
)
docker = [u for u in new["updates"] if u["package-ecosystem"] == "docker"]
assert len(docker) == 1, "expected exactly one docker entry"
assert docker[0].get("ignore") == EXPECTED_IGNORE, f"AC1 ignore mismatch: {docker[0].get('ignore')}"
stripped = copy.deepcopy(new)
for u in stripped["updates"]:
    if u["package-ecosystem"] == "docker":
        u.pop("ignore")
assert stripped == old, "AC2: anything besides docker.ignore changed"
print("dependabot.yml OK")
```

**V6 (ROADMAP columns):**

```bash
.venv/Scripts/python.exe -c "import sys; rows=[l for l in open('docs/ROADMAP.md',encoding='utf-8') if l.startswith('|')]; n={l.count('|') for l in rows}; print(n); sys.exit(len(n)!=1)"
```

**V8 (sensitive data grep; the expected result is exit 1, no matches):**

```bash
grep -nEi '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b|\.ts\.net\b|ssh-(ed25519|rsa)|BEGIN [A-Z ]*PRIVATE KEY|tske[y]-|gh[p]_|github_pa[t]_|\b[a-z_][a-z0-9_-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,}\b' CLAUDE.md SECURITY.md docs/DEPLOYMENT.md docs/ROADMAP.md .github/dependabot.yml docs/specs/001-dependabot-python-pin-docs.md
```

The brackets (`tske[y]-`, `gh[p]_`) keep the pattern from matching itself in this spec. On the current files and this spec, the grep already gives exit 1 (no noise), so any new match is a finding to review.

## Risks and security

- **Infrastructure data in public docs.** The PowerShell and procedure examples use only placeholders. Real output of `gh secret`, Actions logs, tailnet hostnames or users are never pasted. The slug `emanuelturtula/trading-bot` is public and already in the repo.
- **`--body "<value>"` leaves the value on the command line** (shell history, process list). That is why the doc limits it to non-sensitive values and uses `Read-Host` for the rest.
- **The effect of the `ignore` rule only becomes visible after the merge to `main`** (Dependabot reads the config from the default branch). Since PR #2 was closed, Dependabot would not reopen 3.14 either. The real test is the next Python minor.
- **Python patches.** With the current tag `3.12-slim` (without a patch component), Dependabot is expected not to propose PRs for `python`: 3.12.x patches arrive through the floating tag on every rebuild. The rule stays correct if `3.12.x-slim` or a digest is pinned in the future, but that is out of scope.
- **`require_extra_approval_for_unattributed_changes: true` in the ruleset.** How it interacts with commits authored by `dependabot[bot]` brought in by cherry-pick and 0 required approvals is not verified. Step 2 of the procedure already provides the manual option with a commit of the lead's own.
- **The light path has no tech-lead or tester review.** The risk is supply chain: a compromised dependency or action gets in without a second review by the team. Mitigations:
  - the condition is strict: any change beyond the bump goes through `/feature`;
  - gitleaks, lint/mypy, tests, `Docker build (arm64)`, CodeQL, Copilot review and the beta with `/health` are still mandatory;
  - actions are pinned by SHA;
  - the merge requires explicit user approval.

  Recommended, although not part of the ACs: the lead reads the release notes of the Dependabot PR before the cherry-pick.
- **Closing the Dependabot PR in step 5, before the replacement is merged.** If the replacement PR is abandoned, Dependabot does not propose that same version again (it does propose later ones). This is acceptable per decision D1, but worth keeping in mind.
- **`DEPLOY_ENABLED` other than `true` blocks all merges** (the beta deploy check is never reported). It is documented in AC5 (c).
- **Deploy:** no functional impact. There are no migrations, `TB_*` variables or changes to `secrets.env`. Pushing the branch generates a beta `v0.1.1-beta.<sha7>` with the same functional image.
- **Unbreakable rules:** no runtime code is involved; signal-only, pure `domain/`, UTC and a single worker remain intact.

## User decisions (2026-09-14)

- **D1 (formerly P1): light path for Dependabot PRs.** They do not go through the agent team when the change is only the Dependabot bump (`Dockerfile`, `pyproject.toml`/`uv.lock` or action SHAs), with no code or config changes. Procedure, owned by the lead:
  1. `feature/deps-<slug>` from `origin/main`;
  2. cherry-pick, or the same bump by hand in a commit of the lead's own `build(deps)`/`ci(deps)`, which is the option to use if the ruleset requires extra approval for unattributed commits;
  3. `uv run python scripts/check.py`;
  4. push → CI + beta 8082 → `/health`;
  5. PR that references the Dependabot one, which is closed with a comment;
  6. merge only with explicit user approval.

  If it breaks tests or requires code/config, the full workflow with `/feature` is used. It is documented as an explicit exception in `CLAUDE.md` (AC10a) and in detail in `docs/DEPLOYMENT.md` (AC6).
- **D2 (formerly P2): Dependabot alerts and security updates enabled** by the lead. The tech-lead also verified it: `automated-security-fixes` `enabled: true`, `vulnerability-alerts` → 204 and `dependabot_security_updates: enabled`. Security updates follow the light path with priority. They are documented in `docs/DEPLOYMENT.md` section 5 (AC4), in `SECURITY.md` (AC8) and in the `CLAUDE.md` exception (AC10a).

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users (V8 + manual review)
- [ ] Each AC verified by the tester with evidence (V1–V9); V1 fails without the implementation
- [ ] Scope limited to the 6 files of AC11
- [ ] The `CLAUDE.md` exception is limited to pure Dependabot bumps and matches `docs/DEPLOYMENT.md` (D1)
- [ ] `scripts/check.py` green
