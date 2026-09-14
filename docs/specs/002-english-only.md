# 002 — English-only repository

- **Status:** approved
- **Revision:** 2 — merges PR #35 (`origin/main` at 7df578f) into this branch and translates its content
- **Branch:** `feature/english-language-rules-agents-5a3121`
- **Spec author:** tech-lead
- **Expected commit type:** docs

## Goal

Make English the single language of every repository and GitHub artifact, so that a public repository is readable by anyone and the agent team produces consistent output. Two parts: (1) add an explicit, enforceable "English only" rule to `CLAUDE.md`, the three agent definitions, the `/feature` skill and the PR template; (2) translate every existing Spanish artifact to English without changing its meaning.

Background: code (`src/`, `tests/`, `scripts/`, `deploy/`, `.claude/hooks/`, workflows, configs) and `SECURITY.md` are already in English. Spanish text lived in 11 Markdown files (listed under Design). `CLAUDE.md` stated that docs and specs are written in Spanish; that convention is replaced.

Revision 2: PR #35 (Dependabot Python pin + repository protection docs) merged to `main` first, by user decision, and added new Spanish content: two blocks in `CLAUDE.md`, new material in sections 3, 5 and "Operations" of `docs/DEPLOYMENT.md`, a status column in `docs/ROADMAP.md`, and the whole spec `docs/specs/001-dependabot-python-pin-docs.md`. Its changes to `.github/dependabot.yml` and `SECURITY.md` are already in English. The lead has started `git merge --no-ff --no-commit origin/main`; `CLAUDE.md`, `docs/DEPLOYMENT.md` and `docs/ROADMAP.md` are in conflict. This spec was renumbered from 001 to 002 because number 001 belongs to the PR #35 spec. **The comparison base for every check is now `origin/main` (7df578f), not the local `main`.**

## Out of scope

- Any change to Python code, hooks, workflows, configs, `Dockerfile`, `.gitleaks.toml`, `pyproject.toml` or `uv.lock`.
- A new automated Spanish-detection check in `scripts/check.py`, pre-commit or CI (false-positive risk). The scripts in the Test plan are run ad hoc from a scratch directory and are **not** committed.
- Rewriting git history, or editing already published PR/issue/release text on GitHub (for example PR #1 or PR #35).
- Content changes beyond faithful translation, the new language rule and the merge of PR #35: no new sections, no rewording of security rules, no updates to the roadmap, architecture or deploy procedure.
- Revisiting any decision or fact of PR #35 (D1 light path, D2, the documented ruleset, dates, verified values): translate only.
- `SECURITY.md` and `.github/dependabot.yml`: already English; must stay byte-identical to `origin/main`.
- The language of conversation with the user (not a repository artifact; it may follow the user's language).

## Acceptance criteria

- [ ] AC1: The language scan (Test plan, script `language_scan.py`) exits 0 over every tracked and untracked, non-ignored file: no accented Latin-1 letters and no inverted punctuation marks anywhere, and none of the listed Spanish marker words anywhere except inside the Python blocks of this spec.
- [ ] AC2: `CLAUDE.md` contains the new unbreakable rule 9 with the exact text given in Design §2.1, the replaced language convention (§2.2) and the English Telegram disclaimer "Not financial advice." (§2.3). `git grep -n -i --untracked "spanish"` returns matches only in `docs/specs/002-english-only.md`.
- [ ] AC3: Each of `.claude/agents/tech-lead.md`, `.claude/agents/developer.md` and `.claude/agents/tester.md` contains the shared language block of Design §2.4 verbatim, plus its role-specific additions (§2.5–§2.7).
- [ ] AC4: `.claude/skills/feature/SKILL.md` contains the lead language instructions of Design §2.8; `.github/PULL_REQUEST_TEMPLATE.md` contains the additions of §2.9; `docs/specs/_TEMPLATE.md` contains the addition of §2.10.
- [ ] AC5: Frontmatter of the three agents and the skill parses with `yaml.safe_load` into a mapping with the same keys as on `origin/main`; every value except `description` (and `argument-hint` in the skill) is unchanged; `description` and `argument-hint` are non-empty English strings. (Note: `tester.md` on `origin/main` is not valid strict YAML because its `description` contains an unquoted `": "`; the translated version must be valid.)
- [ ] AC6: The structural fidelity check (Test plan, script `fidelity_check.py`) exits 0. Against `origin/main`, after applying the `RENAMES` table and the `CA<n>` → `AC<n>` rename: only the 13 in-scope files differ; per file the number of headings, table rows and code fences is unchanged; link targets are identical; no placeholder, `TB_*` or `$ARGUMENTS`-style token is lost; every inline code span without Spanish is preserved; the literals in `VERBATIM` (ports, repo slug, verification date, GitHub UI labels, pipeline output) keep their occurrence count; task prefixes are not lost; no `CA<n>` IDs remain; `CLAUDE.md` has exactly one more numbered bold item (rule 9); the architecture diagram keeps every box-drawing character at the same column; the rule JSON example is still valid JSON.
- [ ] AC7: Faithful translation, verified by the tech-lead reading `git diff origin/main` side by side (and `git diff ea57c27` for the three conflicted files): same meaning, same structure and ordering, all security rules and prohibitions keep their strength ("never", "must", "forbidden", "only with explicit user approval" stay absolute), agent names, commands, paths, ports, versions and dates unchanged.
- [ ] AC8: `uv run python scripts/check.py` is green (includes `ruff format --check`, which also formats the Python blocks inside these specs), `python scripts/secret_scan.py --history` is clean, and the added lines of `git diff origin/main` contain no IP address, hostname, username, token or key (placeholders only).
- [ ] AC9 (merge resolved, both sides kept): `git ls-files -u` is empty and no file contains conflict markers. Every line of the branch translation (`ea57c27`) of `CLAUDE.md`, `docs/DEPLOYMENT.md` and `docs/ROADMAP.md` is still present, except the lines PR #35 rewrote (the secret-loading paragraph of section 3, the three bullets of section 5, and the roadmap table rows). Each roadmap row equals the branch row with a `Status` column inserted after `Feature` (`Completed (v0.1.0)` for F0, `Pending` for F1–F8). The PR #35 additions exist in English at the positions defined in Design §4.
- [ ] AC10 (anchors): `docs/DEPLOYMENT.md` has the headings `### 5. Repository protection` and `### Dependabot PRs`; every link to them uses `#5-repository-protection` and `#dependabot-prs`. In every Markdown file, outside code, every relative link target exists and every `#anchor` resolves to a heading of the target file (GitHub slug rules).
- [ ] AC11 (PR #35 spec): `docs/specs/001-dependabot-python-pin-docs.md` is translated in full, following Design §4.4: acceptance-criterion IDs `CA<n>` become `AC<n>` everywhere (prose, inline code and the assertion messages of its V1 script); verification IDs `V1`–`V10` and decision IDs `D1`/`D2` stay; its code blocks that copy real files (the `dependabot.yml` excerpt, the `SECURITY.md` row, the `CLAUDE.md` exception paragraph and bullet) are identical to the final English text of those files.

## Design

### 1. Affected files and owners

All edits are Markdown. **The developer is explicitly authorized by this spec to edit `.claude/` and `.github/PULL_REQUEST_TEMPLATE.md`** (normally outside the developer's scope) and to resolve the merge conflicts. The tester does not modify any file: verification uses the scripts below from the scratch directory. No tests are added under `tests/` (no code changes; no automated language check, see Out of scope).

| File | Owner | Change |
|------|-------|--------|
| `CLAUDE.md` | developer | Translate; add rule 9 (§2.1); replace language convention (§2.2); English disclaimer (§2.3); resolve conflict (§4.1) |
| `README.md` | developer | Translate; disclaimer blockquote starts with `**Not financial advice.**` |
| `docs/ARCHITECTURE.md` | developer | Translate (including diagram labels and the rule `name` in the JSON example) |
| `docs/DEPLOYMENT.md` | developer | Translate (including shell comments and descriptive placeholders); resolve conflict (§4.2) |
| `docs/ROADMAP.md` | developer | Translate; resolve conflict (§4.3) |
| `docs/specs/_TEMPLATE.md` | developer | Translate using the headings of this spec; add §2.10 |
| `docs/specs/001-dependabot-python-pin-docs.md` | developer | Translate in full (§4.4) |
| `.github/PULL_REQUEST_TEMPLATE.md` | developer | Translate; add §2.9 |
| `.claude/agents/tech-lead.md` | developer | Translate body and `description`; add §2.4 and §2.5 |
| `.claude/agents/developer.md` | developer | Translate body and `description`; add §2.4 and §2.6 |
| `.claude/agents/tester.md` | developer | Translate body and `description` (valid YAML); add §2.4 and §2.7 |
| `.claude/skills/feature/SKILL.md` | developer | Translate body, `description` and `argument-hint`; add §2.8 |
| `docs/specs/002-english-only.md` | tech-lead | This spec |

`SECURITY.md` and `.github/dependabot.yml` merged cleanly and must not be edited. No interfaces, Protocols, migrations or `TB_*` variables are added or changed.

### 2. Exact rule text

Insert the texts below verbatim (Markdown included). Placement is prescriptive so the fidelity check stays deterministic: **do not add new headings, table rows or code fences** anywhere.

#### 2.1 `CLAUDE.md` — new unbreakable rule (append after rule 8)

```markdown
9. **English only.** Every repository and GitHub artifact is written in English: code, identifiers, comments, docstrings, logs, error messages, user-facing bot text (Telegram, API, dashboard), docs, specs, agent and skill definitions, commit messages, branch names, PR titles and descriptions, PR/issue/review comments, and release notes. Conversation with the user may follow the user's language; when a request arrives in another language, translate it before it lands in any artifact.
```

#### 2.2 `CLAUDE.md` — code conventions

Replace the current first bullet of the code conventions section (the one assigning languages to code vs. docs) with:

```markdown
- **English** for everything in the repository and on GitHub (rule 9): code, identifiers, comments, docstrings, logs, commits, docs, specs, and PRs.
```

#### 2.3 `CLAUDE.md` — Telegram messages convention

The Telegram messages bullet keeps its content, translated, and ends with: ``the `[BETA]` prefix outside prod, and the disclaimer "Not financial advice."``

#### 2.4 Shared block for the three agent definitions

Insert as a separate paragraph immediately after the "read these files before starting" paragraph (the second body paragraph) of each agent:

```markdown
**Language: English only** (`CLAUDE.md`, rule 9). Everything you write to the repository or to GitHub is in English: code, identifiers, comments, docstrings, logs, error messages, user-facing strings, tests, docs, specs, and the reports and messages you send to teammates (they end up in PRs). The lead may relay user requests in another language: translate them, and never copy non-English text into an artifact.
```

#### 2.5 `tech-lead.md` — role-specific

- In the `[spec]` step that tells the tech-lead to write `docs/specs/NNN-<slug>.md`, state that the spec is written in English.
- In the review checklist, the list of unbreakable rules becomes: `signal-only, pure domain/, closed candles only, idempotency, UTC, single worker, no eval, English only`.
- Add this bullet to the review checklist, right after the secrets bullet:

```markdown
   - **language**: every artifact in the diff is in English (code, identifiers, comments, docstrings, logs, error messages, user-facing strings, docs, specs); any non-English text is a REQUEST CHANGES finding;
```

- The verdict format block becomes exactly:

```text
VERDICT: APPROVE | REQUEST CHANGES
Findings:
- [CRITICAL|HIGH|MEDIUM|LOW] file:line — problem — expected behavior
```

#### 2.6 `developer.md` — role-specific

Add this bullet at the end of the code rules list (step 3):

```markdown
   - English only: identifiers, comments, docstrings, log and error messages, user-facing strings (Telegram, API, dashboard) and any docs you touch.
```

#### 2.7 `tester.md` — role-specific

- Add this bullet at the end of the test-writing list (step 1):

```markdown
   - **language**: test names, docstrings, assertion messages and fixture text in English;
```

- Add this bullet at the end of the evidence list (step 3):

```markdown
   - a language review of the diff: any non-English text in a repository artifact is a finding.
```

- The report format block becomes exactly:

```text
RESULT: PASS | FAIL | BLOCKED
Acceptance criteria:
- AC1: PASS|FAIL — covering test(s)
Evidence:
- command → summarized result
Findings (if FAIL):
- [CRITICAL|HIGH|MEDIUM|LOW] file:line — how to reproduce — expected vs actual
```

#### 2.8 `.claude/skills/feature/SKILL.md` — lead instructions

- Frontmatter: `argument-hint: "<feature description>"`; `description` translated.
- Preparation step that creates the branch: the `<slug>` is in English.
- Team section, the sentence about teammates not inheriting the conversation becomes: "Teammates do not inherit this conversation: in each teammate's prompt include the full request (with an English translation if the user wrote in another language), the branch, the user's answers, which task they own, and a reminder that every artifact must be in English (`CLAUDE.md`, rule 9)."
- Integration step that opens the PR: "Open the PR with `gh pr create` using `.github/PULL_REQUEST_TEMPLATE.md`; the PR title and description are in English: spec link, verdicts and beta evidence."
- Integration step that reports to the user: begins "Report to the user (in the user's language): ..." and keeps the rest.
- The cross-reference to `CLAUDE.md` must name the translated heading exactly: `Mandatory feature workflow (Agent Team)`.

#### 2.9 `.github/PULL_REQUEST_TEMPLATE.md`

- First line of the file (before the first heading):

```markdown
<!-- Write the PR title and every section in English (CLAUDE.md, rule 9). -->
```

- Last item of the checklist section:

```markdown
- [ ] Everything in English: code, comments, docs, commits, and this PR
```

#### 2.10 `docs/specs/_TEMPLATE.md`

Last item of the review checklist:

```markdown
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
```

### 3. Translation guidelines

- **Faithful, not creative.** Same meaning, order, list numbering, bold/emphasis and tone of obligation. Imperative second person for agent/skill instructions ("Read", "Never", "You are the...").
- **Never change:** commands, paths, file names, identifiers, `TB_*` names, GitHub secret names, ports, versions (`vX.Y.Z-beta.<sha7>`), dates, PR numbers, URLs, link targets (except the anchor renames of §4.2), agent names (`tech-lead`, `developer`, `tester`, `lead`), task prefixes, `$ARGUMENTS`, Conventional Commit types, emphasized "public"/"never" warnings, and quoted pipeline output (for example "deployment NOT performed").
- **Quoted tool output and UI labels stay verbatim**: error messages from tools or GitHub (for example `ValueError: Invalid host`, `Please provide either an auth key, OAuth secret and tags, or federated identity client ID and audience with tags.`, `Permission denied (publickey)`) and GitHub UI labels (*Re-run failed jobs*, *Update branch*, *Auth Keys write*). **Strings meant to be typed into GitHub** (PR body and close comment) are translated: "Replaces #<number>" and "Replaced by #<new PR number>".
- **Placeholders:** identifier-style placeholders stay as they are (`<DEPLOY_HOST>`, `<DEPLOY_USER>`, `<OWNER_ID>`, `<REPO_ID>`, `<slug>`, `<sha>`, `<sha7>`, `<run-id>`, `<id>`, `<n>`, `<hash>`, `<chat id>`). Descriptive Spanish placeholders are translated. The **authoritative mapping is the `RENAMES` table in `fidelity_check.py`** (Test plan); its English targets are `<NAME>`, `<value>`, `<key>`, `<file>`, `<changed files>`, `<number>`, `<new PR number>`, `<package>`, `<beta bot token>`, `<long random value>`, `<description>` (in `CLAUDE.md`), `<feature description>` (in the skill) and `<feature title>` (in the template).
- **Code blocks:** translate only human-language comments/labels inside them (shell and PowerShell `#` comments, the `CLAUDE.md` structure tree descriptions, diagram labels, the JSON rule `name`, report formats, `Read-Host` prompts are placeholders). In the `CLAUDE.md` structure tree keep the description column aligned. In the `docs/ARCHITECTURE.md` diagram, every box-drawing and arrow character must stay at the same column: pad or shorten labels with spaces (text to the right of the last box character on a line may change length freely). JSON example `name`: `"RSI oversold in uptrend"`.
- **Headings used by cross-references and templates** (use exactly):
  - `CLAUDE.md`: `Unbreakable rules`, `Commands`, `Structure`, `Code conventions`, `Mandatory feature workflow (Agent Team)`, `Git, versioning and delivery`, `Active protections (do not disable)`; top doc links labelled `Architecture · Roadmap · Deploy · Specs · Security`.
  - `docs/DEPLOYMENT.md`: `5. Repository protection`, `Dependabot PRs`.
  - `docs/specs/_TEMPLATE.md`: title `NNN — <feature title>`; metadata `Status: draft | approved | implemented`, `Branch`, `Spec author`, `Expected commit type`; sections `Goal`, `Out of scope`, `Acceptance criteria` (items `AC1`, `AC2`), `Design`, `Test plan` (columns `Case | Type (unit/integration) | What it verifies`), `Risks and security`, `Review checklist (tech-lead)`.
  - Agents: `Your tasks`, `` `[spec]` — specification ``, `` `[review]` — final review ``, ``How you work (`[impl]`)``, ``How you work (`[test]`)``, `Limits`.
  - `SKILL.md`: `/feature — mandatory Agent Team workflow`, `0. Preparation`, `1. Team`, `2. Shared tasks`, `3. Coordination`, `4. Integration and delivery (lead only)`, `5. Merge (only with explicit user approval)`.
- **Glossary:** signal, candle, closed candle, open (in-progress) candle, rule, indicator, acceptance criterion (AC), finding, verdict, unbreakable rule, secret, placeholder, beta/prod, lead, teammate, light path (the Dependabot exception), version updates / security updates (GitHub terms), ruleset, bypass, required status checks, strict mode, consequences, symptoms, remedy, unattributed commits.
- **Frontmatter:** keep `description` on one line. Do not use `": "` inside a plain scalar; if unavoidable, quote the whole value with double quotes.
- **Anchors:** GitHub derives an anchor from the heading text (lowercase, punctuation removed, spaces become hyphens). Renaming a heading renames its anchor, so every link to it must be updated in the same change (AC10). Do not add anchors beyond those of §4.2.

### 4. Merge of PR #35

The developer resolves the three conflicts by taking **this branch's English version as the base** and adding **PR #35's content, translated**, at the positions below. Resolve content only; then mark each resolved file with `git add <path>` using explicit paths (never `git add -A`, never commit). The lead concludes the merge commit. If `origin/main` advances again, stop and tell the lead: `BASE` and `BRANCH` in the scripts must be updated.

#### 4.1 `CLAUDE.md`

- Keep everything from the branch translation (rule 9, conventions, all other sections).
- Insert PR #35's exception paragraph, translated, immediately after the introductory paragraph of `Mandatory feature workflow (Agent Team)` and before the roles table. It starts with `**Exception — Dependabot PRs (light path).**` and ends with a link whose target is `docs/DEPLOYMENT.md#dependabot-prs`.
- Append PR #35's Dependabot bullet, translated, as the last bullet of `Git, versioning and delivery`; it refers to the exception in "Mandatory feature workflow (Agent Team)".

#### 4.2 `docs/DEPLOYMENT.md`

- Keep everything from the branch translation except the lines PR #35 rewrote.
- Section 3: replace the branch paragraph that starts "Load the values with" by PR #35's material, translated and in the same order: the PowerShell warning blockquote, the "verified forms" line, the `powershell` block (comments translated), the `DEPLOY_KNOWN_HOSTS` and bash/zsh paragraph, the empty-secret symptoms list, the remedy paragraph, and the `DEPLOY_ENABLED` sentence whose link targets `#5-repository-protection`.
- Section 5 (heading `### 5. Repository protection`): replace the three branch bullets with PR #35's full section, translated (verification date and re-verify commands, the ruleset bullet with its sub-bullets, the scanning/Dependabot/Actions bullets, and consequences (a), (b), (c)); links target `#dependabot-prs`.
- Append PR #35's subsection at the end of `Operations` with the heading `### Dependabot PRs` (anchor `#dependabot-prs`): reason, light-path condition, the six numbered steps with their indented `sh` block, the exit to `/feature`, and the Python pin note. Its links target `#5-repository-protection` and `../CLAUDE.md`.

#### 4.3 `docs/ROADMAP.md`

- Keep the branch translation of the intro paragraph and every table row; insert a `Status` column after `Feature`: header `Status`, F0 `Completed (v0.1.0)`, F1–F8 `Pending`.

#### 4.4 `docs/specs/001-dependabot-python-pin-docs.md`

- Translate everything: title (`001 — Dependabot without Python version jumps and repository protection docs`), metadata (`Status: implemented`, `Branch`, `Spec author`, `Expected commit type`), sections with the English template headings plus `User decisions (2026-09-14)`, subsection headings (`Files and owners`, `Cross-cutting`, and the ones that are file paths stay as paths), tables, prose, and comments inside code blocks.
- `CA<n>` → `AC<n>` everywhere, including the inline code span with the V1 failure message and the assertion messages inside the V1 script, so they stay consistent.
- References to the renamed anchor and heading use `#dependabot-prs` and `### Dependabot PRs`; the `ROADMAP` values it cites use `Status`, `Completed (v0.1.0)` and `Pending`.
- The `markdown` blocks that copy the `CLAUDE.md` exception paragraph and bullet must be byte-identical to the final English text in `CLAUDE.md`; the `yaml` and `SECURITY.md` blocks are already English and stay identical to those files.
- Keep historical facts unchanged: dates, PR numbers, API field names and values, commands, versions, the V8 grep pattern.
- Python blocks must remain `ruff format` clean (run `uv run ruff format <file>` after editing; it formats Python code blocks in Markdown).

## Test plan

No pytest tests: there is no code change, and a committed language check is out of scope. The mandatory cases of the template (anti look-ahead, idempotency, authorization, secret redaction) **do not apply**, since no indicator, rule, signal, endpoint, config or log code changes. `docker build` does not apply either: `.dockerignore` excludes `*.md`, `docs`, `.claude` and `.github`, so the image is unaffected (report it as N/A with this justification, not as BLOCKED).

**Comparison base:** `origin/main` (7df578f). The local `main` is stale; do not use `git diff main`. Changes are uncommitted during the merge, so use `git diff origin/main` plus `git status --porcelain`.

The tester copies both scripts below into the session scratch directory (never into the repository) and runs them from the repository root with `PYTHONIOENCODING=utf-8` (on Windows). `fidelity_check.py` needs PyYAML (`uv run python ...` provides it through the dev environment). `uv` is installed under `$APPDATA/Python/Python312/Scripts` on the Windows host if it is not on `PATH`.

| Case | Type | What it verifies |
|------|------|------------------|
| T1 language scan | script | AC1: `python <scratch>/language_scan.py` exits 0 and prints `TOTAL 0` |
| T2 fidelity check | script | AC5, AC6, AC9, AC10, AC11 (structure part): `uv run python <scratch>/fidelity_check.py` exits 0 and prints `FIDELITY OK` |
| T3 rule text present | grep | AC2–AC4, AC10: `git grep -n -F` for a distinctive fragment of each block in §2.1–§2.10 and §4 (for example `"9. **English only.**"`, `"**Language: English only**"`, `"VERDICT: APPROVE"`, `"RESULT: PASS"`, `"<feature description>"`, `"Everything in English"`, `"Every artifact is in English"`, `"Not financial advice."`, `"**Exception — Dependabot PRs (light path).**"`, `"### Dependabot PRs"`, `"### 5. Repository protection"`) returns a match in each expected file |
| T4 "spanish" mentions | grep | AC2: `git grep -n -i --untracked spanish` matches only `docs/specs/002-english-only.md` |
| T5 hook prefixes intact | grep | `.claude/hooks/task_gate.py` is unchanged and `git grep -n -F "[review] <slug>"` still matches the skill |
| T6 quality gate | command | AC8: `uv run python scripts/check.py` green |
| T7 secret scan | command | AC8: `python scripts/secret_scan.py --history` clean (it only sees commits; T8 covers the uncommitted diff) |
| T8 no infrastructure data | grep | AC8: `git diff origin/main -U0 \| grep -E '^\+' \| grep -E '([0-9]{1,3}\.){3}[0-9]{1,3}'` returns nothing, and a manual read confirms only placeholders |
| T9 faithful translation | manual (tech-lead review) | AC7, AC9, AC11: side-by-side read of `git diff origin/main` and `git diff ea57c27 -- CLAUDE.md docs/DEPLOYMENT.md docs/ROADMAP.md` |
| T10 negative control | script | Proves both scripts can fail: `git worktree add <scratch>/base-check origin/main`, run T1 and T2 from inside it, confirm T1 reports Spanish hits and T2 reports at least the invalid `tester.md` frontmatter, the remaining `CA<n>` IDs and the missing rule 9, then `git worktree remove <scratch>/base-check` |

### Script `language_scan.py`

```python
"""Ad hoc language scan for spec 002 (run from the repository root; never commit)."""

import re
import subprocess
import sys

SPEC = "docs/specs/002-english-only.md"
SKIPPED = {"uv.lock"}
ACCENTS = re.compile("[\u00c0-\u00ff\u00a1\u00bf]")
WORDS = re.compile(
    r"\b(que|para|los|las|del|una|por|sin|cuando|nunca|siempre|tambien|debe|usar"
    r"|archivo|archivos|usuario|clave|claves|regla|reglas|vela|velas|senal|senales"
    r"|leer|antes|despues|segun|hacer|donde|esta|estan|puerto|rama|cada|pero)\b",
    re.IGNORECASE,
)

listed = subprocess.run(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=True,
).stdout.split("\n")
hits = 0
for path in sorted(set(filter(None, listed))):
    if path in SKIPPED:
        continue
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (UnicodeDecodeError, OSError):
        continue
    in_scanner_block = False
    for number, line in enumerate(lines, 1):
        if path == SPEC and line.startswith("```"):
            in_scanner_block = line.startswith("```python") and not in_scanner_block
        word_hit = WORDS.search(line) and not (path == SPEC and in_scanner_block)
        if ACCENTS.search(line) or word_hit:
            hits += 1
            print(f"{path}:{number}: {line.strip()[:120]}")
print(f"TOTAL {hits}")
sys.exit(1 if hits else 0)
```

Note: the accent class covers U+00C0–U+00FF, which also contains the multiplication and division signs; none are used today. If a legitimate English word from the list appears (a false positive), report it to the tech-lead instead of rewording the docs.

### Script `fidelity_check.py`

```python
"""Ad hoc structural fidelity check for spec 002: origin/main vs working tree (never commit)."""

import json
import os
import re
import subprocess
import sys

import yaml

BASE = "origin/main"  # 7df578f, PR #35 merged
BRANCH = "ea57c27"  # English translation on this branch before merging PR #35
SPEC = "docs/specs/002-english-only.md"
DEPENDABOT_SPEC = "docs/specs/001-dependabot-python-pin-docs.md"
TRANSLATED = [
    "CLAUDE.md",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOYMENT.md",
    "docs/ROADMAP.md",
    "docs/specs/_TEMPLATE.md",
    DEPENDABOT_SPEC,
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".claude/agents/tech-lead.md",
    ".claude/agents/developer.md",
    ".claude/agents/tester.md",
    ".claude/skills/feature/SKILL.md",
]
ALLOWED_CHANGES = set(TRANSLATED) | {SPEC}
CONFLICTED = ["CLAUDE.md", "docs/DEPLOYMENT.md", "docs/ROADMAP.md"]
# Lines of the branch translation that PR #35 rewrote (prefix match); every other line must survive.
REPLACED_BY_PR35 = {
    "docs/DEPLOYMENT.md": (
        "Load the values with",
        "- Secret scanning and push protection:",
        "- Ruleset for `main`:",
        "- Actions: default",
    ),
    "docs/ROADMAP.md": ("|",),
}
# Authoritative Spanish -> English renames, applied to BASE text before comparing
# links, placeholders and code spans. Longest strings first.
RENAMES = [
    ("Reemplazado por #<n\u00famero del PR nuevo>", "Replaced by #<new PR number>"),
    ("Reemplaza #<n\u00famero>", "Replaces #<number>"),
    ("<n\u00famero del PR nuevo>", "<new PR number>"),
    ("<n\u00famero>", "<number>"),
    ("<archivos cambiados>", "<changed files>"),
    ("<archivo>", "<file>"),
    ("<clave>", "<key>"),
    ("<valor aleatorio largo>", "<long random value>"),
    ("<valor>", "<value>"),
    ("<token del bot beta>", "<beta bot token>"),
    ("<NOMBRE>", "<NAME>"),
    ("<paquete>", "<package>"),
    ("<descripci\u00f3n de la feature>", "<feature description>"),
    ("<descripci\u00f3n>", "<description>"),
    ("<t\u00edtulo de la feature>", "<feature title>"),
    ("#5-protecci\u00f3n-del-repositorio", "#5-repository-protection"),
    ("#prs-de-dependabot", "#dependabot-prs"),
    ("### PRs de Dependabot", "### Dependabot PRs"),
    ("Completada (v0.1.0)", "Completed (v0.1.0)"),
    ("Pendiente", "Pending"),
    ("Estado", "Status"),
]
CRITERION_ID = re.compile(r"\bCA(\d+[a-c]?)\b")
SPANISH = re.compile(
    "[\u00c0-\u00ff\u00a1\u00bf]"
    r"|\b(que|para|los|las|del|una|por|sin|cuando|nunca|siempre|tambien|debe|usar"
    r"|archivo|archivos|usuario|clave|claves|regla|reglas|vela|velas|senal|senales"
    r"|leer|antes|despues|segun|hacer|donde|esta|estan|puerto|rama|cada|pero|aclarando)\b",
    re.IGNORECASE,
)
PLACEHOLDER = re.compile(r"<\w[\w\- ]{0,30}>")
VERBATIM = (
    "8081",
    "8082",
    "emanuelturtula/trading-bot",
    "2026-09-14",
    "*Re-run failed jobs*",
    "*Update branch*",
    "*Auth Keys write*",
    "deployment NOT performed",
)
SYNC_TARGETS = ["CLAUDE.md", "SECURITY.md", "docs/DEPLOYMENT.md", ".github/dependabot.yml"]
BOX = re.compile("[\u2500-\u257f\u25b6\u25c0]")
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
errors: list[str] = []


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.replace("\r\n", "\n")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


def check(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def normalize(text: str) -> str:
    for spanish, english in RENAMES:
        text = text.replace(spanish, english)
    return CRITERION_ID.sub(r"AC\1", text)


def fences(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.split("\n"):
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def prose_lines(text: str) -> list[str]:
    lines, inside = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            inside = not inside
        elif not inside:
            lines.append(line)
    return lines


def github_slugs(path: str) -> set[str]:
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for line in prose_lines(read(path)):
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1)).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def frontmatter_block(text: str) -> str | None:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else None


def lenient_keys(block: str) -> dict[str, str]:
    # BASE contains a plain scalar with ": " (invalid strict YAML); parse line by line.
    pairs = (re.match(r"^([\w-]+):\s?(.*)$", line) for line in block.split("\n"))
    return {m.group(1): m.group(2).strip().strip('"') for m in pairs if m}


def table_cells(line: str) -> list[str]:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return ["---" if cell and set(cell) <= {"-", ":"} else cell for cell in cells]


# 1. Merge state and scope
check(not git("ls-files", "-u").strip(), "unmerged paths remain (resolve and mark with git add)")
listed = git("ls-files", "--cached", "--others", "--exclude-standard").split("\n")
text_files = sorted({p for p in listed if p and os.path.isfile(p) and p != "uv.lock"})
for path in text_files:
    try:
        if CONFLICT_MARKER.search(read(path)):
            errors.append(f"{path}: conflict markers present")
    except UnicodeDecodeError:
        pass
changed = set(git("diff", "--name-only", BASE).split())
changed |= set(git("ls-files", "--others", "--exclude-standard").split())
check(
    changed <= ALLOWED_CHANGES, f"files changed outside scope: {sorted(changed - ALLOWED_CHANGES)}"
)

# 2. Per-file structure against BASE
for path in TRANSLATED:
    raw_old, new = git("show", f"{BASE}:{path}"), read(path)
    old = normalize(raw_old)
    for label, pattern in {
        "headings": r"^#{1,6} ",
        "table rows": r"^\|",
        "fences": r"^```",
    }.items():
        same = len(re.findall(pattern, old, re.M)) == len(re.findall(pattern, new, re.M))
        check(same, f"{path}: {label} count changed")
    link = r"\]\(([^)]+)\)"
    check(
        sorted(re.findall(link, old)) == sorted(re.findall(link, new)),
        f"{path}: link targets changed",
    )
    for token in (PLACEHOLDER, re.compile(r"\bTB_[A-Z_]+\b"), re.compile(r"\$[A-Z_]+\b")):
        lost = set(token.findall(old)) - set(token.findall(new))
        check(not lost, f"{path}: tokens lost {sorted(lost)}")
    span = r"`([^`\n]+)`"
    kept = {s for s in re.findall(span, old) if not SPANISH.search(s)}
    missing = kept - set(re.findall(span, new))
    check(not missing, f"{path}: code spans lost {sorted(missing)}")
    for literal in VERBATIM:
        check(
            old.count(literal) == new.count(literal), f"{path}: occurrences of {literal!r} changed"
        )
    for prefix in ("[spec]", "[impl]", "[test]", "[review]"):
        check(new.count(prefix) >= old.count(prefix), f"{path}: task prefix {prefix} lost")
    check(not CRITERION_ID.search(new), f"{path}: Spanish acceptance-criterion IDs (CA<n>) remain")
    old_block = frontmatter_block(raw_old)
    if old_block is None:
        continue
    new_block = frontmatter_block(new)
    if new_block is None:
        errors.append(f"{path}: frontmatter missing")
        continue
    try:
        new_fm = yaml.safe_load(new_block)
    except yaml.YAMLError as error:
        errors.append(f"{path}: frontmatter is not valid YAML ({error.__class__.__name__})")
        continue
    if not isinstance(new_fm, dict):
        errors.append(f"{path}: frontmatter is not a mapping")
        continue
    old_fm = lenient_keys(old_block)
    check(set(old_fm) == set(new_fm), f"{path}: frontmatter keys changed")
    for key in set(old_fm) - {"description", "argument-hint"}:
        value = new_fm.get(key)
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        check(old_fm[key] == rendered, f"{path}: frontmatter '{key}' changed")
    for key in {"description", "argument-hint"} & set(old_fm):
        value = new_fm.get(key)
        check(isinstance(value, str) and bool(value.strip()), f"{path}: frontmatter '{key}' empty")

# 3. Both sides of the merge kept: branch translation lines survive
for path in CONFLICTED:
    new_lines = set(read(path).split("\n"))
    replaced = REPLACED_BY_PR35.get(path, ())
    for line in git("show", f"{BRANCH}:{path}").split("\n"):
        if line.strip() and not line.startswith(replaced) and line not in new_lines:
            errors.append(f"{path}: branch line lost: {line[:80]!r}")
branch_rows = [
    table_cells(l)
    for l in git("show", f"{BRANCH}:docs/ROADMAP.md").split("\n")
    if l.startswith("|")
]
new_rows = [table_cells(l) for l in read("docs/ROADMAP.md").split("\n") if l.startswith("|")]
check(len(branch_rows) == len(new_rows), "ROADMAP.md: row count differs from the branch")
expected_status = ["Status", "---", "Completed (v0.1.0)"] + ["Pending"] * (len(new_rows) - 3)
for index, (branch_row, new_row) in enumerate(zip(branch_rows, new_rows)):
    status = new_row[2] if len(new_row) > 2 else None
    check(status == expected_status[index], f"ROADMAP.md: row {index + 1} status is {status!r}")
    check(
        new_row[:2] + new_row[3:] == branch_row, f"ROADMAP.md: row {index + 1} differs from branch"
    )

# 4. CLAUDE.md rule count, ARCHITECTURE diagram and JSON
rules = r"^\d+\. \*\*"
old_rules = len(re.findall(rules, git("show", f"{BASE}:CLAUDE.md"), re.M))
new_rules = len(re.findall(rules, read("CLAUDE.md"), re.M))
print(f"numbered bold items in CLAUDE.md: {BASE}={old_rules} working tree={new_rules}")
check(new_rules == old_rules + 1, "CLAUDE.md: expected exactly one new unbreakable rule")
old_diagram = fences(git("show", f"{BASE}:docs/ARCHITECTURE.md"))[0]
new_diagram = fences(read("docs/ARCHITECTURE.md"))[0]
check(len(old_diagram) == len(new_diagram), "ARCHITECTURE.md: diagram line count changed")
for number, (a, b) in enumerate(zip(old_diagram, new_diagram), 1):
    boxes_a = [(m.start(), m.group()) for m in BOX.finditer(a)]
    boxes_b = [(m.start(), m.group()) for m in BOX.finditer(b)]
    check(boxes_a == boxes_b, f"ARCHITECTURE.md: diagram line {number} box alignment changed")
try:
    json.loads("\n".join(fences(read("docs/ARCHITECTURE.md"))[1]))
except json.JSONDecodeError:
    errors.append("ARCHITECTURE.md: rule JSON example is not valid JSON")

# 5. Blocks of the Dependabot spec that are copies of real files stay copies
old_blocks = fences(git("show", f"{BASE}:{DEPENDABOT_SPEC}"))
new_blocks = fences(read(DEPENDABOT_SPEC))
if len(old_blocks) == len(new_blocks):
    for index, block in enumerate(old_blocks):
        for target in SYNC_TARGETS:
            if "\n".join(block) in git("show", f"{BASE}:{target}"):
                check(
                    "\n".join(new_blocks[index]) in read(target),
                    f"{DEPENDABOT_SPEC}: code block {index + 1} no longer matches {target}",
                )

# 6. Every relative link and anchor resolves (all Markdown files, outside code)
for source in (p for p in text_files if p.endswith(".md")):
    prose = "\n".join(prose_lines(read(source)))
    prose = re.sub(r"``.*?``|`[^`\n]*`", "", prose)
    for target in re.findall(r"\]\(([^)\s]+)\)", prose):
        if re.match(r"^[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (
            os.path.normpath(os.path.join(os.path.dirname(source), file_part))
            if file_part
            else source
        )
        if not os.path.exists(resolved):
            errors.append(f"{source}: link target does not exist: {target}")
        elif anchor and anchor not in github_slugs(resolved):
            errors.append(f"{source}: anchor does not resolve: {target}")

print("\n".join(errors) or "FIDELITY OK")
sys.exit(1 if errors else 0)
```

Notes on `fidelity_check.py`:

- Placeholders are compared with a pattern that also matches non-ASCII and multi-word placeholders, after `RENAMES` normalizes the base text.
- Inline code spans that still look Spanish after normalization (for example fragments produced by double-backtick spans) are treated as translatable and are not required to survive.
- Code fences are counted by lines that start with three backticks at column 0, identically on both sides, so `ruff format` rewrapping Python blocks does not change any count; block contents are only compared for the architecture diagram, the rule JSON example and the copies checked in step 5.
- `REPLACED_BY_PR35` lists, by line prefix, the branch lines that PR #35 legitimately rewrote; any other branch line missing from a conflicted file is reported.

## Risks and security

- **Weakened security rules through translation.** The biggest risk: a "never"/"forbidden" turning into a softer "avoid". Mitigated by AC7 (side-by-side review) and by keeping the structure identical (AC6).
- **Losing one side of the merge.** A conflict resolved by picking one side would drop either the translation or PR #35's content. Mitigated by AC9 (branch lines survive; structure matches `origin/main`) and T9.
- **Broken anchors.** Translating headings changes GitHub anchors; `CLAUDE.md`, `docs/DEPLOYMENT.md` and the PR #35 spec link to them. Mitigated by AC10 (every anchor resolves).
- **Divergence between the PR #35 spec and the real files.** Its copied blocks must match the English files (AC11, step 5 of the fidelity check).
- **Broken agent loading.** Invalid frontmatter could make an agent or the `/feature` skill fail to load. Mitigated by AC5 (strict YAML parse), which also fixes the latent invalid YAML in `tester.md`.
- **Hook coupling.** `.claude/hooks/task_gate.py` gates tasks by the `[impl]`/`[test]`/`[review]` prefixes; they must stay literally identical (AC6, T5). The hook does not parse verdict/report formats, so translating those is safe.
- **Sensitive data.** Translating `docs/DEPLOYMENT.md` and the PR #35 spec must not replace placeholders with real values; the public repo slug already present is the only concrete identifier. No IPs, hostnames, users, tokens or keys may be introduced (AC8, T7, T8).
- **Moving base.** If `origin/main` advances before the merge commit, the constants `BASE` and `BRANCH` and this spec must be revisited.
- **Deploy impact.** None: no migrations, no new `TB_*` variables, no changes to `secrets.env`, and Markdown is excluded from the Docker build context. The merge also brings PR #35's `.github/dependabot.yml`, already deployed through `main`.
- **Out-of-repo state.** Any assistant memory or notes stating "docs in Spanish" are outside the repository and must be updated by the lead, not by teammates.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md (including the new rule 9)
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Verification scripts (T1–T10) cover the acceptance criteria
- [ ] `scripts/check.py` green
