from __future__ import annotations

import subprocess
from typing import Any

import pytest

from tests.script_loader import load_script

rd = load_script("scripts/remote_deploy.py", "remote_deploy")

REVISION = "a" * 40
IMAGE = "ghcr.io/emanuelturtula/trading-bot@sha256:" + "b" * 64
FAKE_REGISTRY_TOKEN = "fake-registry-" + "token"
WORKFLOW = "emanuelturtula/trading-bot/.github/workflows/delivery.yml"


def make_env(**overrides: str) -> dict[str, str]:
    env = {
        "DEPLOY_ENVIRONMENT": "beta",
        "DEPLOY_REVISION": REVISION,
        "DEPLOY_IMAGE": IMAGE,
        "DEPLOY_VERSION": "v0.2.0-beta.aaaaaaa",
        "DEPLOY_HOST": "pi-host",
        "DEPLOY_USER": "deployer",
        "DEPLOY_SSH_KEY": "fake-key-material",
        "DEPLOY_KNOWN_HOSTS": "pi-host ssh-ed25519 AAAAfake",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "emanuelturtula/trading-bot",
        "GITHUB_REF": "refs/heads/feature/new-rule",
        "GITHUB_WORKFLOW_REF": (f"{WORKFLOW}@refs/heads/feature/new-rule"),
        "GITHUB_SHA": REVISION,
        "GITHUB_RUN_NUMBER": "42",
        "GITHUB_RUN_ID": "123456",
        "GHCR_USER": "emanuelturtula",
        "GHCR_TOKEN": FAKE_REGISTRY_TOKEN,
    }
    env.update(overrides)
    return env


def prod_env(**overrides: str) -> dict[str, str]:
    base = {
        "DEPLOY_ENVIRONMENT": "prod",
        "DEPLOY_VERSION": "v0.2.0",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": (f"{WORKFLOW}@refs/heads/main"),
    }
    base.update(overrides)
    return make_env(**base)


def test_valid_beta_request() -> None:
    request = rd.parse_request(make_env())
    assert request.environment == "beta"
    assert (
        request.source_run_url
        == "https://github.com/emanuelturtula/trading-bot/actions/runs/123456"
    )


def test_valid_prod_request() -> None:
    assert rd.parse_request(prod_env()).environment == "prod"


@pytest.mark.parametrize(
    "overrides",
    [
        {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_REPOSITORY": "someone/fork"},
        {"GITHUB_SHA": "c" * 40},
        {"GITHUB_WORKFLOW_REF": WORKFLOW.replace("delivery", "other") + "@refs/heads/main"},
        {"GITHUB_REF": "refs/heads/feature/x"},
        {"DEPLOY_VERSION": "v0.2.0-beta.aaaaaaa"},
        {"GITHUB_RUN_NUMBER": "0"},
        {"GHCR_TOKEN": " "},
    ],
)
def test_prod_rejects_anything_but_a_main_push(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        rd.parse_request(prod_env(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": f"{WORKFLOW}@refs/heads/main",
        },
        {
            "GITHUB_REF": "refs/heads/hotfix",
            "GITHUB_WORKFLOW_REF": f"{WORKFLOW}@refs/heads/hotfix",
        },
        {"DEPLOY_VERSION": "v0.2.0"},
    ],
)
def test_beta_rejects_non_feature_branches_and_stable_versions(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        rd.parse_request(make_env(**overrides))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DEPLOY_HOST", "pi-host; rm -rf /"),
        ("DEPLOY_USER", "root$(id)"),
        ("DEPLOY_IMAGE", "ghcr.io/evil/trading-bot@sha256:" + "b" * 64),
        ("DEPLOY_IMAGE", "ghcr.io/emanuelturtula/trading-bot:latest"),
        ("DEPLOY_REVISION", "HEAD"),
        ("DEPLOY_ENVIRONMENT", "test"),
        ("GHCR_USER", "user name"),
    ],
)
def test_malicious_or_malformed_inputs_fail_before_any_command(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        rd.main(make_env(**{key: value}))
    assert calls == []


def test_token_is_only_sent_via_stdin_and_remote_dir_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    rd.main(prod_env())

    for args, kwargs in calls:
        assert FAKE_REGISTRY_TOKEN not in " ".join(args)
        assert "shell" not in kwargs
    stdin_payloads = [kwargs.get("input") for _, kwargs in calls if kwargs.get("input")]
    assert stdin_payloads == [FAKE_REGISTRY_TOKEN + "\n"]
    remote_commands = [args[-1] for args, _ in calls if args[0] == "ssh"]
    assert "--run-number 42" in remote_commands[2]
    assert remote_commands[-1].startswith("rm -rf -- .cache/trading-bot-delivery/")


def test_cleanup_failure_does_not_hide_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "scp" or args[-1].startswith("rm -rf"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as error:
        rd.main(make_env())
    assert error.value.cmd[0] == "scp"
