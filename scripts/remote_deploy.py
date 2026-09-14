"""Deploy an immutable image to the Raspberry Pi from GitHub Actions.

Runs on the GitHub runner after it joined the tailnet. It validates every input
before touching the network, uploads ``deploy/deploy.py`` + ``deploy/compose.yml``
to a random temporary directory on the Pi, logs in to GHCR with the short-lived
job token (via stdin only) and runs the host-side deployment.

Host and user come from repository secrets so they are masked in the public logs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "emanuelturtula/trading-bot"
WORKFLOW = f"{REPOSITORY}/.github/workflows/delivery.yml"
IMAGE_PATTERN = rf"ghcr\.io/{re.escape(REPOSITORY)}@sha256:[0-9a-f]{{64}}"
FEATURE_REF_PATTERN = r"refs/heads/feature/[A-Za-z0-9._/-]+"
VERSION_PATTERNS = {
    "beta": r"v\d+\.\d+\.\d+-beta\.[0-9a-f]{7}",
    "prod": r"v\d+\.\d+\.\d+",
}
SSH_TIMEOUT_SECONDS = 1020  # headroom for the bounded host-lock wait


@dataclass(frozen=True)
class Request:
    host: str
    user: str
    environment: str
    revision: str
    image: str
    version: str
    run_number: str
    source_run_url: str
    registry_user: str
    registry_token: str


def checked(value: str, pattern: str, name: str) -> str:
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {name}")
    return value


def parse_request(env: Mapping[str, str]) -> Request:
    environment = checked(env.get("DEPLOY_ENVIRONMENT", ""), r"beta|prod", "environment")
    revision = checked(env.get("DEPLOY_REVISION", ""), r"[0-9a-f]{40}", "revision")
    image = checked(env.get("DEPLOY_IMAGE", ""), IMAGE_PATTERN, "image")
    version = checked(env.get("DEPLOY_VERSION", ""), VERSION_PATTERNS[environment], "version")
    host = checked(env.get("DEPLOY_HOST", "").strip(), r"[a-zA-Z0-9][a-zA-Z0-9.-]*", "host")
    user = checked(env.get("DEPLOY_USER", "").strip(), r"[a-z_][a-z0-9_-]*", "user")

    ref = env.get("GITHUB_REF", "")
    if (
        env.get("GITHUB_EVENT_NAME") != "push"
        or env.get("GITHUB_REPOSITORY") != REPOSITORY
        or env.get("GITHUB_SHA") != revision
        or env.get("GITHUB_WORKFLOW_REF") != f"{WORKFLOW}@{ref}"
    ):
        raise ValueError("Deployments require a push to this repository's delivery workflow")
    if environment == "prod" and ref != "refs/heads/main":
        raise ValueError("Production deploys only from main")
    if environment == "beta" and not re.fullmatch(FEATURE_REF_PATTERN, ref):
        raise ValueError("Beta deploys only from feature/** branches")

    run_number = checked(env.get("GITHUB_RUN_NUMBER", ""), r"[1-9][0-9]*", "run number")
    run_id = checked(env.get("GITHUB_RUN_ID", ""), r"[1-9][0-9]*", "run id")
    token = env.get("GHCR_TOKEN", "")
    if not token.strip():
        raise ValueError("Missing registry credential")
    return Request(
        host=host,
        user=user,
        environment=environment,
        revision=revision,
        image=image,
        version=version,
        run_number=run_number,
        source_run_url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        registry_user=checked(
            env.get("GHCR_USER", ""), r"[a-zA-Z0-9][a-zA-Z0-9_-]*", "registry user"
        ),
        registry_token=token,
    )


def deploy_command(request: Request, remote_dir: str) -> list[str]:
    return [
        "env",
        f"DOCKER_CONFIG={remote_dir}/docker",
        "python3",
        f"{remote_dir}/deploy.py",
        "--environment",
        request.environment,
        "--image",
        request.image,
        "--revision",
        request.revision,
        "--version",
        request.version,
        "--run-number",
        request.run_number,
        "--source-run-url",
        request.source_run_url,
    ]


def main(env: Mapping[str, str] = os.environ) -> None:
    request = parse_request(env)
    # Relative to the remote user's home directory.
    remote_dir = f".cache/trading-bot-delivery/{uuid.uuid4().hex}"
    repo = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory(prefix="trading-bot-ssh-") as temp:
        key = Path(temp) / "identity"
        known_hosts = Path(temp) / "known_hosts"
        key.write_text(env["DEPLOY_SSH_KEY"].strip() + "\n", encoding="utf-8")
        known_hosts.write_text(env["DEPLOY_KNOWN_HOSTS"].strip() + "\n", encoding="utf-8")
        key.chmod(0o600)
        options = [
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ConnectTimeout=20",
        ]
        destination = f"{request.user}@{request.host}"

        def ssh(command: str, *, data: str | None = None) -> None:
            subprocess.run(
                ["ssh", *options, destination, command],
                input=data,
                text=True,
                check=True,
                timeout=SSH_TIMEOUT_SECONDS,
            )

        try:
            ssh(f"umask 077; mkdir -p -- {shlex.quote(remote_dir)}")
            subprocess.run(
                [
                    "scp",
                    *options,
                    str(repo / "deploy" / "deploy.py"),
                    str(repo / "deploy" / "compose.yml"),
                    f"{destination}:{remote_dir}/",
                ],
                check=True,
                timeout=60,
            )
            login = [
                "docker",
                "--config",
                f"{remote_dir}/docker",
                "login",
                "ghcr.io",
                "--username",
                request.registry_user,
                "--password-stdin",
            ]
            ssh(shlex.join(login), data=request.registry_token + "\n")
            ssh(shlex.join(deploy_command(request, remote_dir)))
        finally:
            try:
                ssh(f"rm -rf -- {shlex.quote(remote_dir)}")
            except (subprocess.SubprocessError, OSError):
                print(
                    "Warning: could not remove the temporary delivery directory; "
                    "the registry token expires with this job.",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
