"""Claude Code TaskCompleted quality gate for the feature agent team.

Tasks whose subject starts with ``[impl]``, ``[test]`` or ``[review]`` can only be
marked complete when ``scripts/check.py --fast`` passes. Exit code 2 keeps the task
open and sends the failure output back to the teammate as feedback.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATED_PREFIXES = ("[impl]", "[test]", "[review]")
BLOCK = 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    subject = str(payload.get("task_subject") or "").strip().lower()
    if not subject.startswith(GATED_PREFIXES):
        return 0

    uv = shutil.which("uv")
    command = (
        [uv, "run", "python", "scripts/check.py", "--fast"]
        if uv
        else [sys.executable, "scripts/check.py", "--fast"]
    )
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"task_gate: quality gate could not run ({error})", file=sys.stderr)
        return BLOCK
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-4000:]
        print(
            "task_gate: the task cannot be completed until `uv run python scripts/check.py "
            f"--fast` passes.\n{output}",
            file=sys.stderr,
        )
        return BLOCK
    return 0


if __name__ == "__main__":
    sys.exit(main())
