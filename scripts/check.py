"""Single quality gate used locally, by agent hooks and mirrored in CI.

Usage:
    uv run python scripts/check.py          # full gate (lint, types, tests + coverage, secrets)
    uv run python scripts/check.py --fast   # quick gate for task completion hooks
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def steps(fast: bool) -> list[tuple[str, list[str]]]:
    python = sys.executable
    pytest_args = ["-q", "-x"] if fast else ["--cov", "--cov-report=term-missing"]
    gate = [
        ("ruff check", [python, "-m", "ruff", "check", "."]),
        ("ruff format", [python, "-m", "ruff", "format", "--check", "."]),
        ("mypy", [python, "-m", "mypy"]),
        ("pytest", [python, "-m", "pytest", *pytest_args]),
    ]
    if not fast:
        gate.append(("secret scan", [python, "scripts/secret_scan.py", "--history"]))
    return gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip coverage and secret scan")
    args = parser.parse_args(argv)

    failures: list[str] = []
    for name, command in steps(args.fast):
        print(f"==> {name}", flush=True)
        if subprocess.run(command, cwd=REPO_ROOT, check=False).returncode != 0:
            failures.append(name)
            if args.fast:
                break
    if failures:
        print(f"FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
