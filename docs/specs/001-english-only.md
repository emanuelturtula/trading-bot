# 001 — English-only repository

- **Status:** approved
- **Branch:** `feature/english-language-rules-agents-5a3121`
- **Spec author:** tech-lead
- **Expected commit type:** docs

## Goal

Make English the single language of every repository and GitHub artifact, so that a public repository is readable by anyone and the agent team produces consistent output. Two parts: (1) add an explicit, enforceable "English only" rule to `CLAUDE.md`, the three agent definitions, the `/feature` skill and the PR template; (2) translate every existing Spanish artifact to English without changing its meaning.

Background: code (`src/`, `tests/`, `scripts/`, `deploy/`, `.claude/hooks/`, workflows, configs) and `SECURITY.md` are already in English. Spanish text lives only in 11 Markdown files (listed under Design). `CLAUDE.md` currently states that docs and specs are written in Spanish; that convention is replaced.

## Out of scope

- Any change to Python code, hooks, workflows, configs, `Dockerfile`, `.gitleaks.toml`, `pyproject.toml` or `uv.lock`.
- A new automated Spanish-detection check in `scripts/check.py`, pre-commit or CI (false-positive risk). The scripts in the Test plan are run ad hoc from a scratch directory and are **not** committed.
- Rewriting git history, or editing already published PR/issue/release text on GitHub (for example PR #1).
- Content changes to the docs beyond faithful translation and the new language rule: no new sections, no rewording of security rules, no updates to the roadmap, architecture or deploy procedure.
- `SECURITY.md` (already English; must remain byte-identical).
- The language of conversation with the user (not a repository artifact; it may follow the user's language).

## Acceptance criteria

- [ ] AC1: The language scan (Test plan, script `language_scan.py`) exits 0 over every tracked and untracked, non-ignored file: no accented Latin-1 letters and no inverted punctuation marks anywhere, and none of the listed Spanish marker words anywhere except inside the scanner block of this spec.
- [ ] AC2: `CLAUDE.md` contains the new unbreakable rule 9 with the exact text given in Design §2.1, the replaced language convention (§2.2) and the English Telegram disclaimer "Not financial advice." (§2.3). `git grep -n -i --untracked "spanish"` returns matches only in `docs/specs/001-english-only.md`.
- [ ] AC3: Each of `.claude/agents/tech-lead.md`, `.claude/agents/developer.md` and `.claude/agents/tester.md` contains the shared language block of Design §2.4 verbatim, plus its role-specific additions (§2.5–§2.7).
- [ ] AC4: `.claude/skills/feature/SKILL.md` contains the lead language instructions of Design §2.8; `.github/PULL_REQUEST_TEMPLATE.md` contains the additions of §2.9; `docs/specs/_TEMPLATE.md` contains the addition of §2.10.
- [ ] AC5: Frontmatter of the three agents and the skill parses with `yaml.safe_load` into a mapping with the same keys as on `main`; every value except `description` (and `argument-hint` in the skill) is unchanged; `description` and `argument-hint` are non-empty English strings. (Note: `tester.md` on `main` is not valid strict YAML because its `description` contains an unquoted `": "`; the translated version must be valid.)
- [ ] AC6: The structural fidelity check (Test plan, script `fidelity_check.py`) exits 0: only the 12 in-scope files differ from `main`; per file the number of headings, table rows and code fences is unchanged; link targets are identical; no `<PLACEHOLDER>`, `TB_*` or `$ARGUMENTS`-style token is lost; every inline code span without Spanish is preserved; occurrences of ports `8081`/`8082` are unchanged; task prefixes `[spec]`, `[impl]`, `[test]`, `[review]` are not lost; `CLAUDE.md` has exactly one more numbered bold item (rule 9); the architecture diagram keeps every box-drawing character at the same column; the rule JSON example is still valid JSON.
- [ ] AC7: Faithful translation, verified by the tech-lead reading `git diff main` side by side: same meaning, same structure and ordering, all security rules and prohibitions keep their strength ("never", "must", "forbidden" stay absolute), agent names, commands, paths, ports and versions unchanged.
- [ ] AC8: `uv run python scripts/check.py` is green, `python scripts/secret_scan.py --history` is clean, and the added lines of `git diff main` contain no IP address, hostname, username, token or key (placeholders only).

## Design

### 1. Affected files and owners

All edits are Markdown. **The developer is explicitly authorized by this spec to edit `.claude/` and `.github/PULL_REQUEST_TEMPLATE.md`** (normally outside the developer's scope). The tester does not modify any file: verification uses the scripts below from the scratch directory. No tests are added under `tests/` (no code changes; no automated language check, see Out of scope).

| File | Owner | Change |
|------|-------|--------|
| `CLAUDE.md` | developer | Translate; add rule 9 (§2.1); replace language convention (§2.2); English disclaimer (§2.3) |
| `README.md` | developer | Translate; disclaimer blockquote starts with `**Not financial advice.**` |
| `docs/ARCHITECTURE.md` | developer | Translate (including diagram labels and the rule `name` in the JSON example) |
| `docs/DEPLOYMENT.md` | developer | Translate (including shell comments and descriptive placeholders) |
| `docs/ROADMAP.md` | developer | Translate |
| `docs/specs/_TEMPLATE.md` | developer | Translate using the headings of this spec; add §2.10 |
| `.github/PULL_REQUEST_TEMPLATE.md` | developer | Translate; add §2.9 |
| `.claude/agents/tech-lead.md` | developer | Translate body and `description`; add §2.4 and §2.5 |
| `.claude/agents/developer.md` | developer | Translate body and `description`; add §2.4 and §2.6 |
| `.claude/agents/tester.md` | developer | Translate body and `description` (valid YAML); add §2.4 and §2.7 |
| `.claude/skills/feature/SKILL.md` | developer | Translate body, `description` and `argument-hint`; add §2.8 |
| `docs/specs/001-english-only.md` | tech-lead | This spec |

No interfaces, Protocols, migrations or `TB_*` variables are added or changed.

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
- **Never change:** commands, paths, file names, identifiers, `TB_*` names, GitHub secret names, ports, versions (`vX.Y.Z-beta.<sha7>`), URLs, link targets, agent names (`tech-lead`, `developer`, `tester`, `lead`), task prefixes, `$ARGUMENTS`, Conventional Commit types, emphasized "public"/"never" warnings, and quoted pipeline output (for example "deployment NOT performed").
- **Placeholders:** identifier-style placeholders stay as they are (`<DEPLOY_HOST>`, `<DEPLOY_USER>`, `<OWNER_ID>`, `<REPO_ID>`, `<slug>`, `<sha7>`, `<id>`, `<n>`, `<hash>`, `<chat id>`). Descriptive Spanish placeholders are translated: `gh secret set <NOMBRE>` → `gh secret set <NAME>`; `uv add <paquete>` → `uv add <package>`; the beta token and the random session value placeholders in the `secrets.env` heredoc → `<beta bot token>` and `<long random value>`; the `/feature` argument → `<description>` in `CLAUDE.md` and `<feature description>` in the skill; the template title → `<feature title>`.
- **Code blocks:** translate only human-language comments/labels inside them (shell `#` comments, the `CLAUDE.md` structure tree descriptions, diagram labels, the JSON rule `name`, report formats). In the `CLAUDE.md` structure tree keep the description column aligned. In the `docs/ARCHITECTURE.md` diagram, every box-drawing and arrow character must stay at the same column: pad or shorten labels with spaces (text to the right of the last box character on a line may change length freely). JSON example `name`: `"RSI oversold in uptrend"`.
- **Headings used by cross-references and templates** (use exactly):
  - `CLAUDE.md`: `Unbreakable rules`, `Commands`, `Structure`, `Code conventions`, `Mandatory feature workflow (Agent Team)`, `Git, versioning and delivery`, `Active protections (do not disable)`; top doc links labelled `Architecture · Roadmap · Deploy · Specs · Security`.
  - `docs/specs/_TEMPLATE.md`: title `NNN — <feature title>`; metadata `Status: draft | approved | implemented`, `Branch`, `Spec author`, `Expected commit type`; sections `Goal`, `Out of scope`, `Acceptance criteria` (items `AC1`, `AC2`), `Design`, `Test plan` (columns `Case | Type (unit/integration) | What it verifies`), `Risks and security`, `Review checklist (tech-lead)`.
  - Agents: `Your tasks`, `` `[spec]` — specification ``, `` `[review]` — final review ``, ``How you work (`[impl]`)``, ``How you work (`[test]`)``, `Limits`.
  - `SKILL.md`: `/feature — mandatory Agent Team workflow`, `0. Preparation`, `1. Team`, `2. Shared tasks`, `3. Coordination`, `4. Integration and delivery (lead only)`, `5. Merge (only with explicit user approval)`.
- **Glossary:** signal, candle, closed candle, open (in-progress) candle, rule, indicator, acceptance criterion (AC), finding, verdict, unbreakable rule, secret, placeholder, beta/prod, lead, teammate.
- **Frontmatter:** keep `description` on one line. Do not use `": "` inside a plain scalar; if unavoidable, quote the whole value with double quotes.
- **Anchors:** there are no `#anchor` links in the repository today (verified); do not introduce any.

## Test plan

No pytest tests: there is no code change, and a committed language check is out of scope. The mandatory cases of the template (anti look-ahead, idempotency, authorization, secret redaction) **do not apply**, since no indicator, rule, signal, endpoint, config or log code changes. `docker build` does not apply either: `.dockerignore` excludes `*.md`, `docs`, `.claude` and `.github`, so the image is unaffected (report it as N/A with this justification, not as BLOCKED).

The tester copies both scripts below into the session scratch directory (never into the repository) and runs them from the repository root with `PYTHONIOENCODING=utf-8` (on Windows). `fidelity_check.py` needs PyYAML (`uv run python ...` provides it through the dev environment).

| Case | Type | What it verifies |
|------|------|------------------|
| T1 language scan | script | AC1: `python <scratch>/language_scan.py` exits 0 and prints `TOTAL 0` |
| T2 fidelity check | script | AC5, AC6: `uv run python <scratch>/fidelity_check.py` exits 0 and prints `FIDELITY OK` |
| T3 rule text present | grep | AC2–AC4: `git grep -n -F` for a distinctive fragment of each block in §2.1–§2.10 (for example `"9. **English only.**"`, `"**Language: English only**"`, `"VERDICT: APPROVE"`, `"RESULT: PASS"`, `"<feature description>"`, `"Everything in English"`, `"Every artifact is in English"`, `"Not financial advice."`) returns a match in each expected file |
| T4 "spanish" mentions | grep | AC2: `git grep -n -i --untracked spanish` matches only `docs/specs/001-english-only.md` |
| T5 hook prefixes intact | grep | `.claude/hooks/task_gate.py` is unchanged and `git grep -n -F "[review] <slug>"` still matches the skill |
| T6 quality gate | command | AC8: `uv run python scripts/check.py` green |
| T7 secret scan | command | AC8: `python scripts/secret_scan.py --history` clean |
| T8 no infrastructure data | grep | AC8: `git diff main -U0 \| grep -E '^\+' \| grep -E '([0-9]{1,3}\.){3}[0-9]{1,3}'` returns nothing |
| T9 faithful translation | manual (tech-lead review) | AC7: side-by-side read of `git diff main` |
| T10 negative control | script | Proves both scripts can fail: create a temporary worktree of `main` in the scratch directory (`git worktree add <scratch>/main-check main`), run T1 and T2 from inside it, confirm T1 reports hits in the 11 Spanish files and T2 reports the invalid `tester.md` frontmatter, then `git worktree remove <scratch>/main-check` |

### Script `language_scan.py`

```python
"""Ad hoc language scan for spec 001 (run from the repository root; never commit)."""

import re
import subprocess
import sys

SPEC = "docs/specs/001-english-only.md"
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
for path in filter(None, listed):
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
"""Ad hoc structural fidelity check for spec 001: main vs working tree (never commit)."""

import json
import re
import subprocess
import sys

import yaml

TRANSLATED = [
    "CLAUDE.md",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOYMENT.md",
    "docs/ROADMAP.md",
    "docs/specs/_TEMPLATE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".claude/agents/tech-lead.md",
    ".claude/agents/developer.md",
    ".claude/agents/tester.md",
    ".claude/skills/feature/SKILL.md",
]
ALLOWED_CHANGES = set(TRANSLATED) | {"docs/specs/001-english-only.md"}
ACCENTED = re.compile("[\u00c0-\u00ff\u00a1\u00bf]")
BOX = re.compile("[\u2500-\u257f\u25b6\u25c0]")
TRANSLATABLE_SPANS = {"gh secret set <NOMBRE>", "uv add <paquete>"}
RENAMED_PLACEHOLDERS = {"<NOMBRE>", "<paquete>"}
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


def frontmatter_block(text: str) -> str | None:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else None


def lenient_keys(block: str) -> dict[str, str]:
    # main contains a plain scalar with ": " (invalid strict YAML); parse line by line.
    pairs = (re.match(r"^([\w-]+):\s?(.*)$", line) for line in block.split("\n"))
    return {m.group(1): m.group(2).strip().strip('"') for m in pairs if m}


changed = set(git("diff", "--name-only", "main").split())
changed |= set(git("ls-files", "--others", "--exclude-standard").split())
check(
    changed <= ALLOWED_CHANGES, f"files changed outside scope: {sorted(changed - ALLOWED_CHANGES)}"
)

for path in TRANSLATED:
    old, new = git("show", f"main:{path}"), read(path)
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
    for token in (r"<[A-Za-z0-9_]+>", r"\bTB_[A-Z_]+\b", r"\$[A-Z_]+\b"):
        lost = set(re.findall(token, old)) - RENAMED_PLACEHOLDERS - set(re.findall(token, new))
        check(not lost, f"{path}: tokens lost {sorted(lost)}")
    span = r"`([^`\n]+)`"
    kept = {s for s in re.findall(span, old) if not ACCENTED.search(s)} - TRANSLATABLE_SPANS
    missing = kept - set(re.findall(span, new))
    check(not missing, f"{path}: code spans lost {sorted(missing)}")
    for port in ("8081", "8082"):
        check(old.count(port) == new.count(port), f"{path}: occurrences of port {port} changed")
    for prefix in ("[spec]", "[impl]", "[test]", "[review]"):
        check(new.count(prefix) >= old.count(prefix), f"{path}: task prefix {prefix} lost")
    old_block = frontmatter_block(old)
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

rules = r"^\d+\. \*\*"
old_rules = len(re.findall(rules, git("show", "main:CLAUDE.md"), re.M))
new_rules = len(re.findall(rules, read("CLAUDE.md"), re.M))
print(f"numbered bold items in CLAUDE.md: main={old_rules} working tree={new_rules}")
if "CLAUDE.md" in changed:
    check(new_rules == old_rules + 1, "CLAUDE.md: expected exactly one new unbreakable rule")

old_diagram = fences(git("show", "main:docs/ARCHITECTURE.md"))[0]
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

print("\n".join(errors) or "FIDELITY OK")
sys.exit(1 if errors else 0)
```

## Risks and security

- **Weakened security rules through translation.** The biggest risk: a "never"/"forbidden" turning into a softer "avoid". Mitigated by AC7 (side-by-side review) and by keeping the structure identical (AC6).
- **Broken agent loading.** Invalid frontmatter could make an agent or the `/feature` skill fail to load. Mitigated by AC5 (strict YAML parse), which also fixes the latent invalid YAML in `tester.md`.
- **Hook coupling.** `.claude/hooks/task_gate.py` gates tasks by the `[impl]`/`[test]`/`[review]` prefixes; they must stay literally identical (AC6, T5). The hook does not parse verdict/report formats, so translating those is safe.
- **Sensitive data.** Translating `docs/DEPLOYMENT.md` must not replace placeholders with real values. No IPs, hostnames, users, tokens or keys may be introduced (AC8, T7, T8).
- **Deploy impact.** None: no migrations, no new `TB_*` variables, no changes to `secrets.env`, and Markdown is excluded from the Docker build context.
- **Out-of-repo state.** Any assistant memory or notes stating "docs in Spanish" are outside the repository and must be updated by the lead, not by teammates.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md (including the new rule 9)
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Verification scripts (T1–T10) cover the acceptance criteria
- [ ] `scripts/check.py` green
