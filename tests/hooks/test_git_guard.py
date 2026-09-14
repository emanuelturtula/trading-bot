from __future__ import annotations

import pytest

from tests.script_loader import load_script

guard = load_script(".claude/hooks/git_guard.py", "git_guard")


@pytest.fixture(autouse=True)
def fake_environment(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scans: list[str] = []
    monkeypatch.setattr(guard, "run_scanner", lambda mode: scans.append(mode) or 0)
    monkeypatch.setattr(guard, "current_branch", lambda: "feature/x")
    return scans


def check(command: str) -> int:
    return int(guard.evaluate(command))


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "git commit -nm x",
        "git commit -am x",
        "git commit --all -m x",
        "git add -f .env",
        "git add --force secrets.env",
        "git push --force origin feature/x",
        "git push --force-with-lease",
        "git push origin +feature/x",
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/main",
        "git -C . push origin main",
        "cd repo && git push origin main",
        "/usr/bin/git push origin main",
    ],
)
def test_dangerous_git_commands_are_blocked(command: str) -> None:
    assert check(command) == guard.BLOCK


def test_plain_push_from_main_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "current_branch", lambda: "main")
    assert check("git push") == guard.BLOCK


def test_add_and_commit_in_one_command_is_blocked(fake_environment: list[str]) -> None:
    # The hook runs before `git add`, so the scan would miss the newly staged files.
    assert check("git add src/x.py && git commit -m 'feat: x'") == guard.BLOCK
    assert fake_environment == []


def test_commit_runs_staged_scan_and_push_runs_history_scan(fake_environment: list[str]) -> None:
    assert check("git commit -m 'feat: x'") == 0
    assert check("git push -u origin feature/x") == 0
    assert fake_environment == ["--staged", "--history"]


@pytest.mark.parametrize("command", ["git status", "git diff --staged", "ls -la", "echo git"])
def test_harmless_commands_pass_without_scanning(command: str, fake_environment: list[str]) -> None:
    assert check(command) == 0
    assert fake_environment == []
