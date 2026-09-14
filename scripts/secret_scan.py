"""Run gitleaks against the repository and fail closed when it cannot run.

Used by pre-commit (``--staged``), pre-push and CI (``--history``) and by the
Claude Code git guard hook. The repository is public, so a missing scanner is
treated exactly like a detected secret: the operation is blocked.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / ".gitleaks.toml"
BLOCKED = 2


def find_gitleaks() -> str | None:
    override = os.environ.get("GITLEAKS_BIN")
    if override and Path(override).is_file():
        return override
    found = shutil.which("gitleaks")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # winget installs are not on PATH until the shell restarts.
        pattern = os.path.join(
            local_app_data, "Microsoft", "WinGet", "Packages", "Gitleaks.Gitleaks_*", "gitleaks.exe"
        )
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def build_command(binary: str, mode: str) -> list[str]:
    command = [
        binary,
        "git",
        "--config",
        str(CONFIG),
        "--redact",
        "--verbose",
        "--no-banner",
        # Inline "gitleaks:allow" comments must never silence a finding.
        "--ignore-gitleaks-allow",
        "--exit-code",
        "1",
    ]
    if mode == "staged":
        command += ["--staged", "--pre-commit"]
    command.append(str(REPO_ROOT))
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="scan staged changes")
    group.add_argument("--history", action="store_true", help="scan every commit")
    args = parser.parse_args(argv)

    if (REPO_ROOT / ".gitleaksignore").exists():
        print(
            "BLOCKED: .gitleaksignore is not allowed. Use a narrow, reviewed allowlist "
            "in .gitleaks.toml instead.",
            file=sys.stderr,
        )
        return BLOCKED

    binary = find_gitleaks()
    if binary is None:
        print(
            "BLOCKED: gitleaks is not installed, so secrets cannot be ruled out.\n"
            "Install it (Windows: winget install Gitleaks.Gitleaks, macOS: brew install "
            "gitleaks) or set GITLEAKS_BIN.",
            file=sys.stderr,
        )
        return BLOCKED

    mode = "staged" if args.staged else "history"
    result = subprocess.run(build_command(binary, mode), check=False)
    if result.returncode != 0:
        print(
            "BLOCKED: gitleaks reported possible secrets or sensitive data. "
            "Remove them (and rewrite history if already committed) before continuing.",
            file=sys.stderr,
        )
        return BLOCKED
    return 0


if __name__ == "__main__":
    sys.exit(main())
