from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.script_loader import REPO_ROOT, load_script
from trading_bot.persistence.engine import database_path

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
        self.database = "absent"
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
            return self.database
        if args[:2] == ["docker", "cp"]:
            Path(args[3]).write_bytes(b"sqlite")
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


OLD_COMPOSE = "services: {}\n"


def seed_previous(root: Path, **overrides: Any) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "compose.yml").write_text(OLD_COMPOSE, encoding="utf-8")
    previous: dict[str, Any] = {
        "environment": "prod",
        "image": OLD_IMAGE,
        "revision": OLD_REVISION,
        "version": "v0.1.0",
        "compose": str(root / "compose.yml"),
        "secrets_env_file": str(root / "secrets.env"),
        "status": "healthy",
        "delivery": {"run_number": 5, "source_run_url": RUN_URL},
    }
    previous.update(overrides)
    dp.write_json(root / "current.json", previous)
    return previous


def seed_legacy(legacy: Path) -> dict[str, Any]:
    """The ``~/trading-bot-deploy/prod`` layout of older releases."""
    prod = legacy / "prod"
    attempts = [prod / "attempts" / f"2026010{day}T000000Z-old" for day in (1, 2)]
    for attempt in attempts:
        attempt.mkdir(parents=True)
        (attempt / "compose.yml").write_text(OLD_COMPOSE, encoding="utf-8")
        (attempt / "database.sqlite3").write_bytes(attempt.name.encode())
    (prod / "secrets.env").write_text("TB_SOMETHING=kept\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "environment": "prod",
        "image": OLD_IMAGE,
        "revision": OLD_REVISION,
        "version": "v0.1.0",
        "compose": str(attempts[-1] / "compose.yml"),
        "secrets_env_file": str(prod / "secrets.env"),
        "status": "healthy",
        "attempt": str(attempts[-1]),
        "backup": str(attempts[0] / "database.sqlite3"),
        "delivery": {"run_number": 5, "source_run_url": RUN_URL},
    }
    dp.write_json(prod / "current.json", manifest)
    return manifest


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "test"},
        {"environment": "beta"},
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
    assert dp.read_json(tmp_path / "current.json")["image"] == IMAGE
    assert {env["TB_PORT"] for env in envs} == {"8081"}
    assert (tmp_path / "secrets.env").exists()


def test_a_healthy_deploy_leaves_a_flat_root_with_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_previous(tmp_path)
    docker = FakeDocker(healthy_images={IMAGE, OLD_IMAGE}, labels=labels())
    docker.running = OLD_IMAGE
    docker.database = "ok"
    install(monkeypatch, docker)

    result = deploy_prod(tmp_path)

    assert {path.name for path in tmp_path.iterdir()} == {
        "backups",
        "compose.yml",
        "current.json",
        "deploy.py",
        "secrets.env",
    }
    assert (tmp_path / "compose.yml").read_text(encoding="utf-8") == (
        REPO_ROOT / "deploy" / "compose.yml"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "deploy.py").read_bytes() == (
        REPO_ROOT / "deploy" / "deploy.py"
    ).read_bytes()
    assert result["compose"] == str(tmp_path / "compose.yml")
    assert Path(result["backup"]).parent == tmp_path / "backups"
    assert Path(result["backup"]).is_file()
    assert dp.read_json(tmp_path / "current.json") == result


def test_prod_keeps_its_compose_project_so_the_data_volume_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(healthy_images={IMAGE}, labels=labels())
    install(monkeypatch, docker)
    deploy_prod(tmp_path)
    compose_calls = [c for c in docker.commands if c[:2] == ["docker", "compose"]]
    assert compose_calls
    assert all(c[3] == "trading-bot-prod" for c in compose_calls)
    assert dp.PORTS == {"prod": "8081"}


def test_default_root_is_trading_bot_in_the_home_directory() -> None:
    assert Path.home() / "trading-bot" == dp.DEFAULT_ROOT
    assert Path.home() / "trading-bot-deploy" == dp.LEGACY_ROOT


def test_legacy_state_is_copied_into_the_flat_root(tmp_path: Path) -> None:
    legacy = tmp_path / "trading-bot-deploy"
    root = tmp_path / "trading-bot"
    old = seed_legacy(legacy)

    assert dp.migrate_legacy_root(legacy, root) is True

    assert (legacy / "prod" / "current.json").exists()  # never deleted by the deploy
    assert (root / "secrets.env").read_text(encoding="utf-8") == "TB_SOMETHING=kept\n"
    assert (root / "compose.yml").read_text(encoding="utf-8") == OLD_COMPOSE
    assert sorted(p.name for p in (root / "backups").iterdir()) == [
        "20260101T000000Z-old.sqlite3",
        "20260102T000000Z-old.sqlite3",
    ]
    current = dp.read_json(root / "current.json")
    assert current["compose"] == str(root.resolve() / "compose.yml")
    assert current["secrets_env_file"] == str(root.resolve() / "secrets.env")
    assert current["backup"] == str(root.resolve() / "backups" / "20260101T000000Z-old.sqlite3")
    assert "attempt" not in current
    kept = {"environment", "image", "revision", "version", "status", "delivery"}
    assert {k: current[k] for k in kept} == {k: old[k] for k in kept}


def test_migration_runs_once(tmp_path: Path) -> None:
    legacy = tmp_path / "trading-bot-deploy"
    root = tmp_path / "trading-bot"
    seed_legacy(legacy)
    assert dp.migrate_legacy_root(legacy, root) is True
    assert dp.migrate_legacy_root(legacy, root) is False


def test_migration_is_a_no_op_without_legacy_prod_state(tmp_path: Path) -> None:
    root = tmp_path / "trading-bot"
    root.mkdir()
    (tmp_path / "trading-bot-deploy" / "beta").mkdir(parents=True)
    assert dp.migrate_legacy_root(tmp_path / "trading-bot-deploy", root) is False
    assert list(root.iterdir()) == []


def test_migration_refuses_a_root_that_is_something_else(tmp_path: Path) -> None:
    legacy = tmp_path / "trading-bot-deploy"
    root = tmp_path / "trading-bot"
    seed_legacy(legacy)
    (root / ".git").mkdir(parents=True)
    with pytest.raises(dp.DeploymentError, match="not a deploy root"):
        dp.migrate_legacy_root(legacy, root)
    assert not (root / "current.json").exists()


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
    assert dp.read_json(tmp_path / "current.json")["image"] == OLD_IMAGE
    assert (tmp_path / "compose.yml").read_text(encoding="utf-8") == OLD_COMPOSE
    assert not (tmp_path / dp.STAGED_COMPOSE).exists()


def test_unhealthy_first_deploy_is_taken_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(healthy_images=set(), labels=labels())
    install(monkeypatch, docker)
    with pytest.raises(dp.DeploymentError, match="rollback=no_previous_deployment"):
        deploy_prod(tmp_path)
    assert docker.running is None
    assert not (tmp_path / "current.json").exists()
    assert not (tmp_path / dp.STAGED_COMPOSE).exists()


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
    path = dp.prepare_secrets_env_file(tmp_path)
    assert path == tmp_path / "secrets.env"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(dp.DeploymentError, match="chmod 600"):
        dp.prepare_secrets_env_file(tmp_path)


def test_existing_secrets_file_is_never_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("TB_SOMETHING=kept\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    dp.prepare_secrets_env_file(tmp_path)
    assert path.read_text(encoding="utf-8") == "TB_SOMETHING=kept\n"


def test_only_the_newest_backups_are_kept(tmp_path: Path) -> None:
    for index in range(15):
        (tmp_path / f"202601{index:02d}T000000Z-x.sqlite3").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("kept", encoding="utf-8")
    dp.prune_backups(tmp_path)
    remaining = sorted(p.name for p in tmp_path.glob("*.sqlite3"))
    assert len(remaining) == dp.KEEP_BACKUPS
    assert remaining[0] == "20260105T000000Z-x.sqlite3"
    assert (tmp_path / "notes.txt").exists()


# --- The database path the application, the deploy script and compose must agree on ---------
# (spec 012, T13, AC2, D80)

CONTAINER_DATA_DIR = PurePosixPath("/app/data")


def compose_environment_value(key: str) -> str:
    """Read ``key`` from the ``environment`` block of ``deploy/compose.yml`` as plain text."""
    compose = (REPO_ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8")
    match = re.search(rf"^\s+{re.escape(key)}:\s*(\S+)\s*$", compose, re.MULTILINE)
    assert match is not None, f"{key} is not set in deploy/compose.yml"
    return match.group(1)


def test_compose_sets_the_data_directory_the_application_expects() -> None:
    assert PurePosixPath(compose_environment_value("TB_DATA_DIR")) == CONTAINER_DATA_DIR


def test_the_container_database_path_is_the_one_the_deploy_backs_up() -> None:
    """A silent rename would make the pre-deploy backup copy nothing (spec 012, D80)."""
    container_path = PurePosixPath(database_path(Path("/app/data")).as_posix())

    assert container_path == PurePosixPath("/app/data/trading_bot.db")
    assert container_path == PurePosixPath(dp.DATABASE)
    assert container_path.parent == PurePosixPath(compose_environment_value("TB_DATA_DIR"))
