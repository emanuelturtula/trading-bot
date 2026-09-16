"""Independent typing and import checks for the rule schema (spec 006, T13, AC21, AC22).

``test_domain_purity.py`` (developer-owned) already scans every ``domain/`` module against one
allowlist that happens to grant ``pydantic`` to ``rules/schema.py`` and ``rules/json_schema.py``.
This file re-derives the same guarantees with an independent implementation (its own AST walk,
its own text patterns) so a bug shared by both scanners would not hide a real violation, and adds
two checks the purity guard does not attempt: that no domain module writes ``typing.Any`` in a
way that is not prose, and that the ``pydantic.mypy`` plugin is not enabled.

Every check reads files directly (never ``git grep``): the new ``rules/`` package is untracked
while this feature is under review, and a plain ``git grep`` silently skips untracked files,
which would make a git-based check pass vacuously without scanning anything.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

import trading_bot.domain
import trading_bot.domain.rules as rules_package

DOMAIN_DIR = Path(trading_bot.domain.__file__).resolve().parent
RULES_DIR = Path(rules_package.__file__).resolve().parent
REPO_ROOT = Path(trading_bot.domain.__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"

_PYDANTIC_ALLOWED_FILES = frozenset({"schema.py", "json_schema.py"})

# The exact pattern of spec 004, AC14: usage of ``Any`` as a type, not the English word in prose
# ("Any tz-aware datetime is accepted", in domain/utc.py, must not trip this).
_ANY_USAGE = re.compile(r"import .*\bAny\b|\bAny\b[],)]|: Any\b|-> Any\b")
_TYPE_IGNORE = re.compile(r"type:\s*ignore")


def _source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _matching_lines(path: Path, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    lines = enumerate(_source_lines(path), start=1)
    return [(number, line) for number, line in lines if pattern.search(line)]


def _imported_module_names(path: Path) -> set[str]:
    """Every module named by a plain or ``from`` import in ``path``, independent AST walk."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _touches(names: set[str], prefix: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in names)


RULES_FILES = sorted(RULES_DIR.rglob("*.py"))
DOMAIN_FILES = sorted(DOMAIN_DIR.rglob("*.py"))


def test_at_least_the_expected_rules_files_were_scanned() -> None:
    """Guards against an empty glob making the parametrized tests below vacuously green."""
    expected = {"__init__.py", "errors.py", "schema.py", "json_schema.py"}

    assert {path.name for path in RULES_FILES} == expected


# --- pydantic confined to schema.py and json_schema.py (AC22), independent scan -------------


@pytest.mark.parametrize("path", RULES_FILES, ids=lambda path: path.name)
def test_pydantic_is_imported_only_by_schema_and_json_schema(path: Path) -> None:
    """Only the two allowed files may import pydantic; ``schema.py`` is the one that must."""
    imported = _imported_module_names(path)
    touches_pydantic = _touches(imported, "pydantic") or _touches(imported, "pydantic_core")

    if path.name not in _PYDANTIC_ALLOWED_FILES:
        assert not touches_pydantic, f"{path.name} unexpectedly imports pydantic: {imported}"


def test_schema_module_does_import_pydantic() -> None:
    """The affirmative half of the check above: the allowance is not simply unused everywhere."""
    imported = _imported_module_names(RULES_DIR / "schema.py")

    assert _touches(imported, "pydantic")


def test_errors_module_imports_neither_pydantic_nor_the_catalog() -> None:
    imported = _imported_module_names(RULES_DIR / "errors.py")

    assert not _touches(imported, "pydantic")
    assert not _touches(imported, "pydantic_core")
    assert not _touches(imported, "trading_bot.domain.indicators")


def test_the_scanner_detects_a_synthetic_plain_import(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import pydantic\n", encoding="utf-8")

    assert _touches(_imported_module_names(sample), "pydantic")


def test_the_scanner_detects_a_synthetic_from_import_of_a_submodule(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("from pydantic_core import ErrorDetails\n", encoding="utf-8")

    assert _touches(_imported_module_names(sample), "pydantic_core")


def test_the_scanner_does_not_flag_an_unrelated_module(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import json\nfrom collections.abc import Mapping\n", encoding="utf-8")

    assert not _touches(_imported_module_names(sample), "pydantic")


# --- no `Any` anywhere in domain/ (AC21), independent of git tracking state ------------------


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=lambda path: str(path.relative_to(DOMAIN_DIR)))
def test_no_domain_module_writes_typing_any(path: Path) -> None:
    found = _matching_lines(path, _ANY_USAGE)

    assert found == [], f"{path.relative_to(DOMAIN_DIR)} writes typing.Any: {found}"


@pytest.mark.parametrize(
    "line",
    [
        "from typing import Any",
        "def f(x: Any) -> None: ...",
        "def f() -> Any: ...",
        "values: list[Any] = []",
        "pair: tuple[Any, int]",
        "callback: Callable[[Any], None]",
    ],
)
def test_the_any_pattern_catches_every_documented_form(line: str) -> None:
    assert _ANY_USAGE.search(line) is not None


def test_the_any_pattern_ignores_prose_in_a_docstring() -> None:
    """The word "Any" in English prose (spec 004, AC14) is not a violation."""
    assert _ANY_USAGE.search("    Any tz-aware ``datetime`` is accepted, also wall times.") is None


# --- no `type: ignore` inside domain/rules (V4) -----------------------------------------------


@pytest.mark.parametrize("path", RULES_FILES, ids=lambda path: path.name)
def test_no_type_ignore_appears_in_the_rules_package(path: Path) -> None:
    found = _matching_lines(path, _TYPE_IGNORE)

    assert found == [], f"{path.name} has a type: ignore comment: {found}"


def test_the_type_ignore_pattern_matches_every_spelling() -> None:
    assert _TYPE_IGNORE.search("x = 1  # type: ignore") is not None
    assert _TYPE_IGNORE.search("x = 1  # type:ignore[arg-type]") is not None
    assert _TYPE_IGNORE.search("x = 1  # nothing to see here") is None


# --- the pydantic.mypy plugin is not enabled (Design §0, §2) --------------------------------


def test_the_pydantic_mypy_plugin_is_not_enabled() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    mypy_config = config.get("tool", {}).get("mypy", {})

    assert "plugins" not in mypy_config
    assert "pydantic.mypy" not in PYPROJECT.read_text(encoding="utf-8")


def test_pyproject_declares_strict_mypy_over_the_domain() -> None:
    """The scoped extra-flag command (V4) only makes sense on top of the strict base gate."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    mypy_config = config.get("tool", {}).get("mypy", {})

    assert mypy_config.get("strict") is True
    assert "src" in mypy_config.get("files", [])
