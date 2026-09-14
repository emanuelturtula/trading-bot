"""Compute the next semantic version from git tags and Conventional Commits.

Rules (applied to commits since the latest ``vX.Y.Z`` tag reachable from HEAD):

* breaking change (``type!:`` or ``BREAKING CHANGE:``) -> major (minor while < 1.0.0)
* ``feat`` -> minor
* anything else -> patch
* no previous tag -> ``v0.1.0``
* HEAD already tagged -> that same version (idempotent reruns)

The ``beta`` channel appends ``-beta.<sha7>``: ``v0.2.0-beta.1a2b3c4``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TAG_PATTERN = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
BREAKING_HEADER = re.compile(r"^[a-zA-Z]+(\([^)]*\))?!:", re.MULTILINE)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
FEAT_HEADER = re.compile(r"^feat(\([^)]*\))?:")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
FIRST_VERSION = (0, 1, 0)

Bump = Literal["major", "minor", "patch"]


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, tag: str) -> Version | None:
        match = TAG_PATTERN.fullmatch(tag.strip())
        if not match:
            return None
        return cls(*(int(part) for part in match.groups()))

    def bump(self, kind: Bump) -> Version:
        if kind == "major" and self.major == 0:
            kind = "minor"
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"


def classify(messages: list[str]) -> Bump:
    kind: Bump = "patch"
    for message in messages:
        if BREAKING_HEADER.search(message) or BREAKING_FOOTER.search(message):
            return "major"
        if FEAT_HEADER.match(message.strip()):
            kind = "minor"
    return kind


def latest_version(tags: list[str]) -> Version | None:
    versions = [v for v in (Version.parse(tag) for tag in tags) if v is not None]
    return max(versions, default=None)


def next_version(latest: Version | None, messages: list[str], head_tags: list[str]) -> Version:
    tagged_head = latest_version(head_tags)
    if tagged_head is not None:
        return tagged_head
    if latest is None:
        return Version(*FIRST_VERSION)
    return latest.bump(classify(messages))


def format_version(version: Version, channel: Literal["beta", "stable"], sha: str) -> str:
    if channel == "beta":
        return f"{version}-beta.{sha[:7]}"
    return str(version)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def compute(repo: Path, channel: Literal["beta", "stable"], sha: str) -> str:
    tags = git("tag", "--list", "v*", "--merged", "HEAD", cwd=repo).split()
    head_tags = git("tag", "--list", "v*", "--points-at", "HEAD", cwd=repo).split()
    latest = latest_version(tags)
    revision_range = f"{latest}..HEAD" if latest else "HEAD"
    log = git("log", "--format=%B%x00", revision_range, cwd=repo)
    messages = [message.strip() for message in log.split("\x00") if message.strip()]
    return format_version(next_version(latest, messages, head_tags), channel, sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("beta", "stable"), required=True)
    parser.add_argument("--sha", required=True, help="full 40-character commit SHA")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args(argv)

    if not SHA_PATTERN.fullmatch(args.sha):
        print("--sha must be a full lowercase 40-character commit SHA", file=sys.stderr)
        return 1
    version = compute(args.repo, args.channel, args.sha)
    print(version)
    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
