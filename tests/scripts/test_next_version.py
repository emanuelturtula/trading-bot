from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.script_loader import load_script

nv = load_script("scripts/next_version.py", "next_version")
SHA = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        (["fix: handle empty candles"], "patch"),
        (["chore: bump deps", "docs: readme"], "patch"),
        (["fix: x", "feat(engine): add cooldown"], "minor"),
        (["feat!: new rule schema"], "major"),
        (["refactor(api)!: drop v0 endpoints"], "major"),
        (["feat: x\n\nBREAKING CHANGE: config renamed"], "major"),
        (["Merge pull request #3 from feature/x"], "patch"),
    ],
)
def test_classify(messages: list[str], expected: str) -> None:
    assert nv.classify(messages) == expected


def test_first_release_is_v0_1_0() -> None:
    assert str(nv.next_version(None, ["feat!: everything"], [])) == "v0.1.0"


def test_breaking_change_bumps_minor_before_1_0() -> None:
    assert str(nv.next_version(nv.Version(0, 3, 2), ["feat!: x"], [])) == "v0.4.0"
    assert str(nv.next_version(nv.Version(1, 3, 2), ["feat!: x"], [])) == "v2.0.0"


def test_feat_and_fix_bumps() -> None:
    assert str(nv.next_version(nv.Version(1, 2, 3), ["feat: x"], [])) == "v1.3.0"
    assert str(nv.next_version(nv.Version(1, 2, 3), ["fix: x"], [])) == "v1.2.4"


def test_tagged_head_is_idempotent() -> None:
    assert str(nv.next_version(nv.Version(1, 2, 3), ["feat: x"], ["v1.2.3"])) == "v1.2.3"


def test_latest_version_ignores_non_semver_tags() -> None:
    tags = ["v0.9.0", "v0.10.0", "latest", "v1.0.0-beta.abcdef1", "v0.2.x"]
    assert str(nv.latest_version(tags)) == "v0.10.0"


def test_beta_channel_appends_short_sha() -> None:
    assert nv.format_version(nv.Version(0, 2, 0), "beta", SHA) == "v0.2.0-beta.1a2b3c4"
    assert nv.format_version(nv.Version(0, 2, 0), "stable", SHA) == "v0.2.0"


def test_main_rejects_invalid_sha() -> None:
    assert nv.main(["--channel", "beta", "--sha", "HEAD"]) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_compute_against_real_repository(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("commit", "-q", "--allow-empty", "-m", "chore: init")
    assert nv.compute(tmp_path, "stable", SHA) == "v0.1.0"

    git("tag", "v0.1.0")
    git("commit", "-q", "--allow-empty", "-m", "fix: a")
    git("commit", "-q", "--allow-empty", "-m", "feat: b")
    assert nv.compute(tmp_path, "stable", SHA) == "v0.2.0"
    assert nv.compute(tmp_path, "beta", SHA) == "v0.2.0-beta.1a2b3c4"
