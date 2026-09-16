# 008 — Squash-only merge instructions

- **Status:** approved
- **Branch:** `feature/squash-merge-docs`
- **Spec author:** tech-lead
- **Expected commit type:** `docs:` → patch bump: beta `v0.5.1-beta.<sha7>`, prod `v0.5.1`. Suggested squash subject: `docs: document squash-only merges (#<n>)`.

## Goal

`CLAUDE.md` and the `/feature` skill tell the lead to run `gh pr merge --merge`, which GitHub rejects ("Merge commits are not allowed on this repository"): the repository only allows squash merges. Fix the instruction, state that the squash subject must be a Conventional Commit because it drives versioning, and document in `docs/DEPLOYMENT.md` the setting plus two behaviours hit while merging stacked PRs (branch update without force push, CodeQL on retargeted PRs).

Facts verified read-only on 2026-09-16 (`gh api repos/emanuelturtula/trading-bot` and the `main` ruleset):

| Item | Value |
|------|-------|
| Repository merge settings | `allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false` |
| Squash defaults | `squash_merge_commit_title: COMMIT_OR_PR_TITLE`, `squash_merge_commit_message: COMMIT_MESSAGES` |
| Branch cleanup | `delete_branch_on_merge: false` |
| `main` ruleset | `allowed_merge_methods: ["merge", "squash", "rebase"]` (the repository settings also apply, so only squash works) |
| Versioning | `scripts/next_version.py` reads the full messages (`git log --format=%B`) since the last tag: `feat` counts only at the start of a message (the subject); `type!:` and `BREAKING CHANGE:` count on any line |
| Wrong instructions | `CLAUDE.md:91` (`gh pr merge --merge`) and `.claude/skills/feature/SKILL.md:49` (`gh pr merge <n> --merge`). No other occurrence outside `docs/specs/**` (`git grep`); spec 002's "merge commit" refers to a local conflict resolution and stays as is |

## Out of scope

- Changing repository settings or the ruleset (for example `allowed_merge_methods`, the default squash title/message or `delete_branch_on_merge`). See "Risks" for an optional follow-up.
- `src/`, `tests/`, `scripts/` (including `next_version.py`), `deploy/`, `.github/` (including the PR template), agent definitions, `README.md`, `SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`.
- Past specs and git history.
- Automating stacked PR merges.
- A test in `tests/` for documentation text: there is no runtime behaviour, and a pytest asserting prose would be brittle. The greps in the test plan cover the regression.

## Acceptance criteria

### `CLAUDE.md`

- [ ] **AC1:** step 9 of "Mandatory feature workflow (Agent Team)" is the **only** change (`git diff origin/main -- CLAUDE.md`: exactly one line removed and one added). The new step 9:
  - keeps "Merge only with explicit user approval" and "Main → prod deploy (port 8081) + tag + GitHub Release";
  - says merges are squash merges, the only method the repository allows;
  - gives the command `gh pr merge <n> --squash --subject "<type>: <description> (#<n>)" --body "<body>"`;
  - says the subject must be a Conventional Commit that describes the whole PR because it drives the version bump;
  - links to `docs/DEPLOYMENT.md#merging-pull-requests`.

### `.claude/skills/feature/SKILL.md`

- [ ] **AC2 (§5 Merge):** the section:
  - says that squash is the only method allowed and that `--merge` is rejected;
  - gives `gh pr merge <n> --squash --subject "<PR title> (#<n>)" --body "<body>"`, with an example body (`Closes #<issue>. Spec: docs/specs/NNN-<slug>.md.`);
  - explains the subject rule: `feat` if the PR adds any feature, `!` if any change is breaking, because `scripts/next_version.py` only sees the squash commit;
  - points to the "Merging pull requests" section of `docs/DEPLOYMENT.md` for stacked PRs and PRs blocked on code scanning;
  - keeps following the `main` run and verifying prod on 8081 (`vX.Y.Z`) and the GitHub Release.
- [ ] **AC3 (§4 step 5):** the PR title must be a Conventional Commit in English, because it becomes the squash subject. The rest of the skill (frontmatter included) does not change.

### `docs/DEPLOYMENT.md`

- [ ] **AC4 (§5 status line):** it keeps "verified on 2026-09-14" for the ruleset, adds that the merge settings were verified on 2026-09-16, and adds the re-verification command `gh api repos/emanuelturtula/trading-bot --jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge}'`.
- [ ] **AC5 (§5 merge method bullet):** one new bullet, after the `main` ruleset bullet, with:
  - the values of the table above (`allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false`, `COMMIT_OR_PR_TITLE`, `COMMIT_MESSAGES`, `delete_branch_on_merge: false`);
  - the note that the ruleset's `allowed_merge_methods` lists all three methods but the repository settings also apply;
  - the error message of `--merge`.
- [ ] **AC6 (§5 consequences):** (a)–(c) are unchanged and three are added:
  - **(d)** one commit per PR reaches `main`, and its subject drives the version;
  - **(e)** after a squash merge, the next stacked PR is behind `main` and is updated without force push;
  - **(f)** CodeQL does not analyze a PR retargeted to `main` after its last push, or closed and reopened, until a new push.

  (d) and (e) link to `#merging-pull-requests`.
- [ ] **AC7 (Operations → `### Merging pull requests`):** a new subsection, placed before `### Dependabot PRs`, containing:
  - the command, with `--repo emanuelturtula/trading-bot`, `--squash`, `--subject` and `--body`;
  - the **subject rule**: PR title + ` (#<n>)`, with the type chosen for the whole PR (`feat` → minor; `!` → major, minor while < 1.0; otherwise patch);
  - the **body rule**: always explicit, because the default `COMMIT_MESSAGES` concatenates every branch commit and can carry a `BREAKING CHANGE:` line into `main`; a `BREAKING CHANGE:` footer is used only when the PR is breaking;
  - the **stacked PR procedure**, in this order:
    1. squash-merge PR k, without deleting branch k before PR k+1 is retargeted;
    2. `git fetch origin`, switch to branch k+1 and bring it up to date (`git pull --ff-only`). **Only then** check the two preconditions: `git diff --quiet origin/main origin/<branch-k>` and `git merge-base --is-ancestor origin/<branch-k> HEAD`, both exit 0. If either fails, do **not** use `-s ours`;
    3. retarget PR k+1 to `main` **before pushing** (`gh pr edit`);
    4. note the tree hash and run `git merge -s ours origin/main` on the same `origin/main` checked in step 2: no `git fetch` or `git pull` in between (if one happened, go back to step 2);
    5. exact post-checks: the tree hash is unchanged **and** `git diff --quiet origin/main origin/<branch-k>` still exits 0. If either fails: do not push, undo the local merge (`git reset --keep HEAD~1`) and go back to step 2. Reading `diff --stat` alone does not count as the check;
    6. normal push, wait for CI + beta (+ `/health`), then squash-merge PR k+1;
  - the **remedy for a PR blocked on code scanning**: push a new commit (a real change or `--allow-empty`), wait, then merge.
- [ ] **AC8 (Dependabot step 6):** the step still requires explicit user approval and adds: squash merge with a `build(deps): …`/`ci(deps): …` subject, linking `#merging-pull-requests`.

### Cross-cutting

- [ ] **AC9 (no wrong instruction left):** no instruction to use `--merge` remains outside `docs/specs/**`. Mentions that it is rejected are allowed: the skill's "`--merge` is rejected" (AC2) and `docs/DEPLOYMENT.md`'s "`gh pr merge --merge` fails with" (AC5). Checked by V1.
- [ ] **AC10 (consistency):** `CLAUDE.md`, the skill and `docs/DEPLOYMENT.md` all contain `--squash`, `--subject`, `(#<n>)` and `--body`, and give the same subject rule. The heading `### Merging pull requests` exists exactly once, and the existing `#5-repository-protection` and `#dependabot-prs` anchors still resolve.
- [ ] **AC11 (scope):** relative to `origin/main`, only `CLAUDE.md`, `.claude/skills/feature/SKILL.md`, `docs/DEPLOYMENT.md` and this spec change.
- [ ] **AC12 (sensitive data):** only placeholders (`<n>`, `<branch-k>`, `<DEPLOY_HOST>`, …). The public slug `emanuelturtula/trading-bot` is already in the repo. No IPs, hostnames, users, tokens or real command output.
- [ ] **AC13 (language):** every added line is in English.
- [ ] **AC14 (gate):** `uv run python scripts/check.py` and `uv run pre-commit run --files <changed files>` are green.

## Design

### Files and owners

| File | Owner | Change |
|------|-------|--------|
| `CLAUDE.md` | developer | Step 9 (AC1) |
| `.claude/skills/feature/SKILL.md` | developer | §4 step 5 and §5 (AC2, AC3) |
| `docs/DEPLOYMENT.md` | developer | §5 status line, bullet and consequences; new Operations subsection; Dependabot step 6 (AC4–AC8) |
| — | tester | No file changes: runs V1–V8 and reports |

**Explicit authorization:** this spec assigns `CLAUDE.md` and `.claude/skills/feature/SKILL.md` to the developer. The design adds no Protocols, interfaces, migrations or `TB_*` variables.

**TDD applied to docs:** before editing, the developer runs V1 and sees it fail (it finds the two wrong lines). After editing, it must pass. The `origin/main` variant of V1 reproduces the failure after the edits.

The texts below are suggestions. The developer may adjust the wording as long as the ACs hold.

### `CLAUDE.md` step 9

````markdown
9. **Merge only with explicit user approval**, always as a squash merge (the only method the repository allows): `gh pr merge <n> --squash --subject "<type>: <description> (#<n>)" --body "<body>"`. The squash commit is the only commit of the PR that reaches `main`, so its subject must be a Conventional Commit describing the whole PR: it drives the version bump. Stacked PRs and CodeQL details in [Deploy](docs/DEPLOYMENT.md#merging-pull-requests). Main → **prod deploy (port 8081)** + tag + GitHub Release.
````

### `.claude/skills/feature/SKILL.md`

§4 step 5:

````markdown
5. Open the PR with `gh pr create` using `.github/PULL_REQUEST_TEMPLATE.md`. The PR title is a Conventional Commit in English (it becomes the squash commit subject); the description is in English: spec link, verdicts and beta evidence.
````

§5:

````markdown
## 5. Merge (only with explicit user approval)
Squash is the only merge method the repository allows (`--merge` is rejected):
`gh pr merge <n> --squash --subject "<PR title> (#<n>)" --body "<body>"`, with an explicit body such as `Closes #<issue>. Spec: docs/specs/NNN-<slug>.md.`
The squash commit is the only commit that reaches `main` and `scripts/next_version.py` computes the version from it: the subject type must describe the whole PR (`feat` if it adds any feature, `!` if any change is breaking). For stacked PRs or a PR blocked on code scanning, follow "Merging pull requests" in `docs/DEPLOYMENT.md`. Then follow the `main` run and verify the prod deploy on 8081 (`vX.Y.Z`) and the GitHub Release.
````

### `docs/DEPLOYMENT.md`

**§5 status line:**

````markdown
Ruleset status verified on 2026-09-14 and merge settings on 2026-09-16 (read-only). To re-verify: `gh ruleset list --repo emanuelturtula/trading-bot`, `gh ruleset view <id> --repo emanuelturtula/trading-bot` and `gh api repos/emanuelturtula/trading-bot --jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge}'`.
````

**§5 bullet (after the `main` ruleset bullet):**

````markdown
- **Merge method: squash only.** Repository settings: `allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false`; default squash title `COMMIT_OR_PR_TITLE` and message `COMMIT_MESSAGES`; `delete_branch_on_merge: false`. The `main` ruleset lists merge, squash and rebase in `allowed_merge_methods`, but the repository settings also apply: `gh pr merge --merge` fails with "Merge commits are not allowed on this repository".
````

**§5 consequences (appended after (c)):**

````markdown
- **(d) One commit per PR on `main`.** Only the squash commit reaches `main`, and `scripts/next_version.py` computes the version from the commits since the last tag: its subject must be a Conventional Commit that describes the whole PR (see [Merging pull requests](#merging-pull-requests)).
- **(e) Stacked PRs.** After a PR is squash-merged, the next PR of the stack still contains the original commits of the merged one and is behind `main`. Force push is blocked, so it is updated with the procedure in [Merging pull requests](#merging-pull-requests).
- **(f) CodeQL on retargeted or reopened PRs.** CodeQL default setup analyzes a PR when it is opened or receives a push with base `main`. A PR retargeted to `main` after its last push, or closed and reopened, stays blocked on the code scanning rule until a new push to its branch.
````

**Operations → new subsection before `### Dependabot PRs`:**

````markdown
### Merging pull requests

Only with explicit user approval, and only as a squash merge (see [section 5](#5-repository-protection)):

```sh
gh pr merge <n> --repo emanuelturtula/trading-bot --squash --subject "<PR title> (#<n>)" --body "<body>"
```

- **Subject.** The PR title, a Conventional Commit in English (`<type>[(<scope>)][!]: <description>`), followed by ` (#<n>)`. `scripts/next_version.py` only sees this commit, so the type describes the whole PR: `feat` if it adds any feature (minor), `!` if any change is breaking (major, minor while < 1.0), otherwise the type of the change (patch).
- **Body.** Always explicit, for example `Closes #<issue>. Spec: docs/specs/NNN-<slug>.md.` The default body (`COMMIT_MESSAGES`) concatenates every branch commit, review fixups included, and can carry a `BREAKING CHANGE:` line into `main`. Add a `BREAKING CHANGE:` footer only when the PR is breaking.
- After the merge, follow the `main` run of `delivery.yml` and verify the prod deploy on 8081 (`vX.Y.Z`) and the GitHub Release.

**Stacked PRs.** PR k+1 targets branch k. Merge the stack one PR at a time, from the bottom:

1. Squash-merge PR k. Do not delete branch k before PR k+1 is retargeted.
2. `git fetch origin`, `git switch <branch-k+1>` and `git pull --ff-only`. Only then check both preconditions (each must exit 0):
   - `git diff --quiet origin/main origin/<branch-k>`: the tree of `main` equals branch k;
   - `git merge-base --is-ancestor origin/<branch-k> HEAD`: branch k+1 contains the final branch k.

   If either fails, do not use `-s ours` (it would silently drop changes): still retarget first (step 3), then, instead of steps 4 and 5, run a normal `git merge origin/main`, resolve conflicts, review `git diff origin/main HEAD` and continue with step 6.
3. Retarget PR k+1 to `main` **before pushing** (see [consequence (f)](#5-repository-protection)): `gh pr edit <k+1> --repo emanuelturtula/trading-bot --base main`.
4. Without any `git fetch` or `git pull` since step 2 (if one happened, go back to step 2), note `git rev-parse "HEAD^{tree}"` and run `git merge -s ours origin/main`. The merge commit keeps the tree of branch k+1 and makes `main` an ancestor; the squash later discards it.
5. Check that `git rev-parse "HEAD^{tree}"` is unchanged and that `git diff --quiet origin/main origin/<branch-k>` still exits 0, so `git diff origin/main HEAD` shows only the changes of PR k+1. If either check fails, do not push: undo the merge with `git reset --keep HEAD~1` and go back to step 2.
6. `git push` (a normal push), wait for CI and the beta deploy of that push to finish green, verify `/health`, and squash-merge PR k+1 with its own subject.

**PR blocked on code scanning.** If a PR was retargeted to `main` after its last push, or closed and reopened, push a new commit to its branch (a real change or `git commit --allow-empty -m "chore: re-run checks"`), wait for CI, the beta deploy and CodeQL, then merge.
````

**Dependabot step 6:**

````markdown
6. Merge only with explicit user approval, as a squash merge with a `build(deps): …` or `ci(deps): …` subject (see [Merging pull requests](#merging-pull-requests)).
````

## Test plan

There is no runtime behaviour, so `tests/` does not change. Mandatory template cases: anti look-ahead, idempotency, authorization and secret redaction in config/logs are **N/A**. The sensitive-data check on the docs is V6.

**For tester and review:** teammates do not commit, and the local `main` of this worktree is stale. Compare against **`origin/main`** with `git diff origin/main` + `git status --porcelain`, not `git diff main...HEAD`. `git grep` searches the working tree of tracked files, so it sees uncommitted edits.

| Case | What it verifies | AC |
|------|------------------|----|
| V1 | Command below gives exit 1 (no matches) on the working tree. It skips only the two lines that say `--merge` is rejected (required by AC2 and AC5); `--merged` in `next_version.py` does not match. Its `origin/main` variant gives exit 0 and lists `CLAUDE.md:91` and `SKILL.md:49`, so V1 fails without the fix | AC9 |
| V2 | `git diff origin/main --numstat -- CLAUDE.md` is `1 1`. The new step 9 contains `--squash`, `--subject "<type>: <description> (#<n>)"`, `--body`, "Conventional Commit", "explicit user approval", `8081` and `docs/DEPLOYMENT.md#merging-pull-requests` | AC1, AC10 |
| V3 | Skill: §5 contains `--squash`, `--subject "<PR title> (#<n>)"`, `--body`, `--merge` described as rejected, `next_version.py`, `feat`, `!`, "Merging pull requests", `8081` and "GitHub Release"; §4 step 5 contains "Conventional Commit"; `git diff origin/main -- .claude/skills/feature/SKILL.md` touches only those two places (frontmatter intact) | AC2, AC3, AC10 |
| V4 | `docs/DEPLOYMENT.md` §5: `2026-09-14`, `2026-09-16`, the `gh api ... --jq` command, the six setting values, `allowed_merge_methods`, the error message, and (a)–(c) unchanged plus (d)–(f) as in AC6. `grep -c '^### Merging pull requests$'` = 1, and it sits before `### Dependabot PRs`. Read the subsection against AC7: command, subject and body rules, the 6 stacked steps in order and the code scanning remedy. In the stacked steps: `git pull --ff-only` comes **before** the preconditions; (ii) uses `HEAD`; step 4 forbids any fetch or pull between the check and `-s ours`; step 5 uses the exact `git diff --quiet origin/main origin/<branch-k>` re-check plus the tree hash, with no push and `git reset --keep HEAD~1` on failure; then push + CI + beta + `/health`. Scratch simulation (optional, no remote push): a commit lands on `main` after step 2 → the step 5 re-check must exit 1. Dependabot step 6 as in AC8 | AC4–AC8, AC10 |
| V5 | `git diff origin/main --name-only` + `git status --porcelain` list only the 4 files of AC11 | AC11 |
| V6 | `gitleaks dir --config .gitleaks.toml --redact --no-banner --ignore-gitleaks-allow --exit-code 1 <file>` **file by file**, exit 0 for each. The spec 001 V8 regex grep over the 4 files gives exit 1. Manual review: only placeholders | AC12 |
| V7 | Command below gives exit 1, plus a read-through for non-English text | AC13 |
| V8 | `uv run python scripts/check.py` and `uv run pre-commit run --files CLAUDE.md .claude/skills/feature/SKILL.md docs/DEPLOYMENT.md docs/specs/008-squash-merge-docs.md` green | AC14 |

Run from the repository root in Git Bash:

```bash
# V1: no instruction to use --merge outside past specs; the two "is rejected" mentions are allowed (expected: exit 1)
git grep -nE -e '--merge([^a-zA-Z-]|$)' -- . ':(exclude)docs/specs/**' \
  | grep -vE '`--merge` is rejected|`gh pr merge --merge` fails with'

# V1 without the fix: same filter on origin/main (expected: exit 0, lists CLAUDE.md:91 and SKILL.md:49)
git grep -nE -e '--merge([^a-zA-Z-]|$)' origin/main -- . ':(exclude)docs/specs/**' \
  | grep -vE '`--merge` is rejected|`gh pr merge --merge` fails with'

# V7: no Spanish characters in added lines (expected: exit 1; the UTF-8 locale is required for -P)
git diff origin/main -U0 -- CLAUDE.md .claude/skills/feature/SKILL.md docs/DEPLOYMENT.md \
  | grep '^+' | LC_ALL=C.UTF-8 grep -nP '[áéíóúñüÁÉÍÓÚÑÜ¿¡]'
LC_ALL=C.UTF-8 grep -nP '[áéíóúñüÁÉÍÓÚÑÜ¿¡]' docs/specs/008-squash-merge-docs.md | grep -v 'grep -nP'
```

The spec is untracked, so `git diff` does not see it. The second V7 line scans it directly and filters out the lines that hold the pattern itself.

`docker build` is not needed: no image input changes.

## Risks and security

- **`git merge -s ours` can silently drop content** if `main` does not equal branch k (another PR merged in between), or if branch k+1 lacks a late fixup of branch k. Mitigation: the two preconditions are mandatory and are checked on the same `origin/main` that is merged (after the last fetch or pull). The tree hash and precondition (i) are re-checked before pushing, and there is a fallback to a normal merge. Found in review: `git pull` refreshes `origin/main`, so a check made before the pull does not protect the merge. In a local simulation, another PR landed on `main` between the check and the pull; the `-s ours` merge then dropped its change while the tree hash check still passed.
- **A wrong squash subject produces a wrong release** (for example `docs:` on a PR that adds a feature → patch instead of minor), and published tags are not rewritten. Mitigation: the PR title is a Conventional Commit from creation (AC3) and the subject rule appears in all three files.
- **UI merges.** A merge from the GitHub UI uses the defaults (`COMMIT_OR_PR_TITLE`, `COMMIT_MESSAGES`), so the commit list lands in the body. Optional follow-up for the user, out of scope: set the default squash message to `PR_BODY` (and optionally the title to `PR_TITLE`), or restrict the ruleset's `allowed_merge_methods` to `squash` so the ruleset and the settings agree.
- **Consequence (f) is an observed behaviour** (2026-09-16), not a documented GitHub contract. If GitHub changes it, the remedy (a new push) stays harmless: it only re-runs CI and the beta deploy.
- **Sensitive data:** the new commands use only placeholders and the public repo slug. No real run output, hostnames or users are pasted.
- **Deploy:** no functional impact. Pushing the branch deploys beta `v0.5.1-beta.<sha7>` with the same functional image. There are no migrations, `TB_*` variables or `secrets.env` changes.
- **Unbreakable rules:** no runtime code is involved. Signal-only, pure `domain/`, closed candles, idempotency, UTC and a single worker are unaffected.

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users (V6 + manual review)
- [x] Each AC verified by the tester with evidence (V1–V8); V1 fails without the implementation
- [x] Scope limited to the 4 files of AC11; `CLAUDE.md` changes only step 9
- [x] The command, subject rule and anchor are consistent across `CLAUDE.md`, the skill and `docs/DEPLOYMENT.md`
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)
