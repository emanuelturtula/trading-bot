"""Claude Code PreToolUse guard for git commands (Bash and PowerShell tools).

Blocks (exit code 2) any git operation that could publish sensitive data or
bypass the protections of this PUBLIC repository:

* ``git commit`` -> gitleaks over staged changes must pass.
* ``git push``   -> gitleaks over the full history must pass.
* ``--no-verify``, ``git add -f``, ``git commit -a``, force pushes and direct
  pushes to ``main`` are always rejected.

Fails closed: if the scanner cannot run, the command is blocked.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "secret_scan.py"
BLOCK = 2

SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")


def block(reason: str) -> int:
    print(f"git_guard: BLOCKED - {reason}", file=sys.stderr)
    return BLOCK


def git_invocations(command: str) -> list[list[str]]:
    """Return the argument lists of every ``git`` invocation in a command line."""
    invocations: list[list[str]] = []
    for segment in SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        for index, token in enumerate(tokens):
            name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name in ("git", "git.exe"):
                invocations.append(tokens[index + 1 :])
                break
    return invocations


def subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Skip global options such as ``-C <path>`` or ``-c key=value``."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, args[index + 1 :]
    return None, []


def current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def run_scanner(mode: str) -> int:
    try:
        result = subprocess.run(
            [sys.executable, str(SCANNER), mode],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return block(f"secret scanner could not run ({error})")
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-3000:]
        return block(f"secret scan failed:\n{output}")
    return 0


def check_git(args: list[str]) -> int:
    if "--no-verify" in args:
        return block("--no-verify bypasses the secret-scanning hooks")
    name, rest = subcommand(args)
    if name == "add" and any(flag in rest for flag in ("-f", "--force")):
        return block("git add --force would stage gitignored (possibly sensitive) files")
    if name == "commit":
        short_flags = "".join(arg[1:] for arg in rest if re.fullmatch(r"-[a-zA-Z]+", arg))
        if "n" in short_flags:
            return block("git commit -n bypasses the secret-scanning hooks")
        if "a" in short_flags or "--all" in rest:
            return block("stage files explicitly (git add <paths>) instead of commit -a")
        return run_scanner("--staged")
    if name == "push":
        if any(arg in ("-f", "--force", "--mirror") or arg.startswith("--force") for arg in rest):
            return block("force pushes are not allowed")
        if any(arg.startswith("+") for arg in rest):
            return block("force-push refspecs (+ref) are not allowed")
        if any(re.search(r"(^|:)(refs/heads/)?main$", arg) for arg in rest):
            return block("never push to main directly; open a pull request")
        positional = [arg for arg in rest if not arg.startswith("-")]
        if len(positional) < 2 and current_branch() == "main":
            return block("never push to main directly; open a pull request")
        return run_scanner("--history")
    return 0


def evaluate(command: str) -> int:
    if "git" not in command.lower():
        return 0
    invocations = git_invocations(command)
    names = [subcommand(args)[0] for args in invocations]
    if "commit" in names and "add" in names[: names.index("commit")]:
        # The hook runs before the whole command, so it would scan the index as it was
        # before `git add`. Staging and committing must be separate tool calls.
        return block(
            "run `git add` and `git commit` as separate commands so staged changes are scanned"
        )
    for args in invocations:
        status = check_git(args)
        if status:
            return status
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    return evaluate(str((payload.get("tool_input") or {}).get("command") or ""))


if __name__ == "__main__":
    sys.exit(main())
