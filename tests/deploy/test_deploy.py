from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from tests.script_loader import load_script

dp = load_script("deploy/deploy.py", "pi_deploy")

REVISION = "a" * 40
OLD_REVISION = "c" * 40
IMAGE = "ghcr.io/emanuelturtula/trading-bot@sha256:" + "b" * 64
OLD_IMAGE = "ghcr.io/emanuelturtula/trading-bot@sha256:" + "d" * 64
RUN_URL = "https://github.com/emanuelturtula/trading-bot/actions/runs/99"


class FakeDocker:
    """Minimal docker/compose simulator driven by the command arguments."""

    def __init__(self, *, healthy_images: set[str], labels: dict[str, str]) -> None:
        self.healthy_images = healthy_images
        self.labels = labels
        self.running: str | None = None
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str], *, env: dict[str, str] | None = None) -> str:
        self.commands.append(args)
        if args[:2] == ["docker", "pull"]:
            return ""
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Config": {"Labels": self.labels}}])
        if args[:2] == ["docker", "compose"]:
            assert env is not None
            action = args[6]
            if action == "up":
                self.running = env["TB_IMAGE"]
            elif action == "down":
                self.running = None
            elif action == "ps":
                return "container-id" if self.running else ""
            return ""
        if args[:2] == ["docker", "inspect"]:
            health = "healthy" if self.running in self.healthy_images else "unhealthy"
            return json.dumps(
                [{"Config": {"Image": self.running}, "State": {"Health": {"Status": health}}}]
            )
        if args[:2] == ["docker", "exec"]:
            return "absent"
        return ""


@pytest.fixture(autouse=True)
def no_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dp, "deployment_lock", lambda root: nullcontext())


def install(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> None:
    monkeypatch.setattr(dp, "run", docker)


def labels(revision: str = REVISION, version: str = "v0.2.0") -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": version,
    }


def deploy_prod(root: Path, **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "environment": "prod",
        "image": IMAGE,
        "revision": REVISION,
        "version": "v0.2.0",
        "run_number": 10,
        "source_run_url": RUN_URL,
        "root": root,
    }
    args.update(overrides)
    return dp.deploy(**args)


def seed_previous(root: Path, **overrides: Any) -> dict[str, Any]:
    attempt = root / "prod" / "attempts" / "20260101T000000Z-previous"
    attempt.mkdir(parents=True)
    (attempt / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    previous: dict[str, Any] = {
        "environment": "prod",
        "image": OLD_IMAGE,
        "revision": OLD_REVISION,
        "version": "v0.1.0",
        "compose": str(attempt / "compose.yml"),
        "secrets_env_file": str(root / "prod" / "secrets.env"),
        "status": "healthy",
        "attempt": str(attempt),
        "delivery": {"run_number": 5, "source_run_url": RUN_URL},
    }
    previous.update(overrides)
    dp.write_json(root / "prod" / "current.json", previous)
    return previous


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "test"},
        {"image": "ghcr.io/other/trading-bot@sha256:" + "b" * 64},
        {"image": "ghcr.io/emanuelturtula/trading-bot:latest"},
        {"revision": "main"},
        {"version": "v0.2.0-beta.aaaaaaa"},
        {"run_number": 0},
        {"source_run_url": "https://example.invalid/runs/1"},
    ],
)
def test_invalid_inputs_fail_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    docker = FakeDocker(healthy_images={IMAGE}, labels=labels())
    install(monkeypatch, docker)
    with pytest.raises(dp.DeploymentError):
        deploy_prod(tmp_path, **overrides)
    assert docker.commands == []


def test_first_deploy_writes_current_manifest_and_uses_prod_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(healthy_images={IMAGE}, labels=labels())
    install(monkeypatch, docker)
    envs: list[dict[str, str]] = []
    original: Callable[..., str] = docker.__call__

    def spy(args: list[str], *, env: dict[str, str] | None = None) -> str:
        if env:
            envs.append(env)
        return original(args, env=env)

    monkeypatch.setattr(dp, "run", spy)
    result = deploy_prod(tmp_path)

    assert result["status"] == "healthy"
    assert dp.read_json(tmp_path / "prod" / "current.json")["image"] == IMAGE
    assert {env["TB_PORT"] for env in envs} == {"8081"}
    assert (tmp_path / "prod" / "secrets.env").exists()


def test_beta_uses_port_8082_and_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(healthy_images={IMAGE}, labels=labels(version="v0.2.0-beta.aaaaaaa"))
    install(monkeypatch, docker)
    deploy_prod(tmp_path, environment="beta", version="v0.2.0-beta.aaaaaaa")
    compose_calls = [c for c in docker.commands if c[:2] == ["docker", "compose"]]
    assert all(c[3] == "trading-bot-beta" for c in compose_calls)
    assert dp.PORTS["beta"] == "8082"


@pytest.mark.parametrize(
    "bad_labels",
    [labels(revision=OLD_REVISION), labels(version="v9.9.9"), {}],
)
def test_image_provenance_mismatch_is_refused_before_replacing_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_labels: dict[str, str]
) -> None:
    docker = FakeDocker(healthy_images={IMAGE}, labels=bad_labels)
    install(monkeypatch, docker)
    with pytest.raises(dp.DeploymentError, match="label"):
        deploy_prod(tmp_path)
    assert not any(c[:2] == ["docker", "compose"] for c in docker.commands)


def test_unhealthy_candidate_rolls_back_to_previous_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_previous(tmp_path)
    docker = FakeDocker(healthy_images={OLD_IMAGE}, labels=labels())
    docker.running = OLD_IMAGE
    install(monkeypatch, docker)

    with pytest.raises(dp.DeploymentError, match="rollback=healthy"):
        deploy_prod(tmp_path)

    assert docker.running == OLD_IMAGE
    assert dp.read_json(tmp_path / "prod" / "current.json")["image"] == OLD_IMAGE


def test_unhealthy_first_deploy_is_taken_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(healthy_images=set(), labels=labels())
    install(monkeypatch, docker)
    with pytest.raises(dp.DeploymentError, match="rollback=no_previous_deployment"):
        deploy_prod(tmp_path)
    assert docker.running is None
    assert not (tmp_path / "prod" / "current.json").exists()


@pytest.mark.parametrize(
    ("run_number", "image", "revision", "allowed"),
    [
        (4, IMAGE, REVISION, False),  # older run
        (5, IMAGE, REVISION, False),  # same run number, different artifact
        (5, OLD_IMAGE, OLD_REVISION, True),  # rerun of the deployed run
        (6, IMAGE, REVISION, True),  # newer run
    ],
)
def test_older_workflow_runs_never_replace_newer_deployments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_number: int,
    image: str,
    revision: str,
    allowed: bool,
) -> None:
    seed_previous(tmp_path)
    docker = FakeDocker(healthy_images={IMAGE, OLD_IMAGE}, labels=labels(revision=revision))
    docker.running = OLD_IMAGE
    install(monkeypatch, docker)
    if allowed:
        assert deploy_prod(tmp_path, run_number=run_number, image=image, revision=revision)
    else:
        with pytest.raises(dp.DeploymentError, match="older"):
            deploy_prod(tmp_path, run_number=run_number, image=image, revision=revision)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_secrets_file_is_created_private_and_loose_permissions_are_refused(tmp_path: Path) -> None:
    path = dp.prepare_secrets_env_file(tmp_path, "prod")
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(dp.DeploymentError, match="chmod 600"):
        dp.prepare_secrets_env_file(tmp_path, "prod")


def test_existing_secrets_file_is_never_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "beta" / "secrets.env"
    path.parent.mkdir(parents=True)
    path.write_text("TB_SOMETHING=kept\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    dp.prepare_secrets_env_file(tmp_path, "beta")
    assert path.read_text(encoding="utf-8") == "TB_SOMETHING=kept\n"


def test_old_attempts_are_pruned_but_protected_ones_are_kept(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    for index in range(15):
        (attempts / f"2026010{index:02d}").mkdir(parents=True)
    protected = str(attempts / "202601000")
    dp.prune_attempts(attempts, {protected})
    remaining = sorted(p.name for p in attempts.iterdir())
    assert len(remaining) == dp.KEEP_ATTEMPTS + 1
    assert "202601000" in remaining
