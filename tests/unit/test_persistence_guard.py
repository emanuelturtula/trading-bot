"""Isolation guard for ``src/trading_bot/persistence`` (spec 012, T14, AC24).

An AST scan enforces the import allowlist: the standard library, ``sqlalchemy``, ``alembic``,
``trading_bot.domain.utc`` and the package itself, with ``trading_bot.config`` allowed in
``migrations/env.py`` only (the developer CLI path of Design 7.4). Nothing in the package may
reach pandas, numpy, TA-Lib, yfinance, the market calendar library, FastAPI or ``data/``, so
importing ``Base`` never drags a heavy dependency in and the package stays reusable by the
Telegram and API layers. A subprocess check confirms it in a fresh interpreter, and the
scanner is exercised against synthetic snippets first, so a green result is not vacuous.

``domain/`` never imports ``persistence``: the purity guard of ``test_domain_purity.py``
covers the domain allowlist, and the last test here pins the direction of the dependency.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import trading_bot.domain
import trading_bot.persistence

_ALLOWED_PREFIXES = ("sqlalchemy", "alembic", "trading_bot.domain.utc", "trading_bot.persistence")
# ``env.py`` is the single module allowed to read the settings, and only when no caller
# supplied a connection (the developer CLI path).
_EXTRA_PREFIXES_BY_FILE = {"migrations/env.py": ("trading_bot.config",)}
_FORBIDDEN_EVERYWHERE = (
    "pandas",
    "numpy",
    "talib",
    "yfinance",
    "exchange_calendars",
    "fastapi",
    "trading_bot.data",
)

PERSISTENCE_DIR = Path(trading_bot.persistence.__file__).resolve().parent
PERSISTENCE_FILES = sorted(PERSISTENCE_DIR.rglob("*.py"))
DOMAIN_DIR = Path(trading_bot.domain.__file__).resolve().parent


def _relative(path: Path) -> str:
    return path.relative_to(PERSISTENCE_DIR).as_posix()


def _imported_module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    dots = "." * node.level
    if node.module:
        return [f"{dots}{node.module}"]
    return [f"{dots}{alias.name}" for alias in node.names]


def _is_allowed_module(name: str, prefixes: tuple[str, ...]) -> bool:
    if name.split(".")[0] in sys.stdlib_module_names and not name.startswith("."):
        return True
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def disallowed_imports(tree: ast.Module, *, extra_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Names imported by ``tree`` that are outside the allowlist, in source order."""
    prefixes = (*_ALLOWED_PREFIXES, *extra_prefixes)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.extend(
                name
                for name in _imported_module_names(node)
                if not _is_allowed_module(name, prefixes)
            )
    return found


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- The real persistence modules ----------------------------------------------------------


@pytest.mark.parametrize("path", PERSISTENCE_FILES, ids=_relative)
def test_persistence_module_imports_stay_within_the_allowlist(path: Path) -> None:
    extra = _EXTRA_PREFIXES_BY_FILE.get(_relative(path), ())
    found = disallowed_imports(_parse(path), extra_prefixes=extra)

    assert found == [], f"{_relative(path)} imports outside the persistence allowlist: {found}"


@pytest.mark.parametrize("path", PERSISTENCE_FILES, ids=_relative)
def test_no_persistence_module_imports_a_heavy_or_layered_dependency(path: Path) -> None:
    imported = {
        name
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_module_names(node)
    }
    forbidden = sorted(
        name
        for name in imported
        for banned in _FORBIDDEN_EVERYWHERE
        if name == banned or name.startswith(f"{banned}.")
    )

    assert forbidden == [], f"{_relative(path)} imports {forbidden}"


def test_at_least_the_expected_modules_were_scanned() -> None:
    """Guards against an empty glob silently making every parametrized test vacuous."""
    assert {_relative(path) for path in PERSISTENCE_FILES} == {
        "__init__.py",
        "base.py",
        "database.py",
        "engine.py",
        "migrator.py",
        "models.py",
        "types.py",
        "migrations/env.py",
        "migrations/versions/0001_baseline.py",
    }


def test_the_settings_are_allowed_in_the_alembic_environment_only() -> None:
    assert _EXTRA_PREFIXES_BY_FILE == {"migrations/env.py": ("trading_bot.config",)}

    others = [path for path in PERSISTENCE_FILES if _relative(path) != "migrations/env.py"]
    for path in others:
        assert disallowed_imports(_parse(path)) == [], _relative(path)
        source = path.read_text(encoding="utf-8")
        assert "trading_bot.config" not in source, _relative(path)


def test_the_domain_never_imports_the_persistence_package() -> None:
    """The dependency only goes one way: ``persistence`` imports ``domain.utc`` (rule 3)."""
    for path in sorted(DOMAIN_DIR.rglob("*.py")):
        imported = {
            name
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in _imported_module_names(node)
        }
        offenders = sorted(name for name in imported if name.startswith("trading_bot.persistence"))
        assert offenders == [], f"{path.name} imports {offenders}"


# --- Self-tests: the scanner rejects synthetic violations ----------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import pandas as pd\n",
        "import numpy as np\n",
        "import talib\n",
        "import yfinance\n",
        "import exchange_calendars\n",
        "from fastapi import FastAPI\n",
        "from trading_bot.data.provider import MarketDataProvider\n",
        "from trading_bot.config import Settings\n",
        "from trading_bot.domain.candles import validate_candles\n",
        "from . import engine\n",
    ],
)
def test_scanner_flags_a_forbidden_import(source: str) -> None:
    assert disallowed_imports(ast.parse(source)) != []


def test_scanner_accepts_every_form_of_allowed_import() -> None:
    source = (
        "from __future__ import annotations\n"
        "import logging\n"
        "from contextlib import contextmanager\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "import sqlalchemy as sa\n"
        "from sqlalchemy.orm import Session\n"
        "from alembic import command\n"
        "from trading_bot.domain.utc import to_utc\n"
        "from trading_bot.persistence.base import Base\n"
    )

    assert disallowed_imports(ast.parse(source)) == []


def test_scanner_accepts_the_settings_only_with_the_environment_allowance() -> None:
    tree = ast.parse("from trading_bot.config import Settings\nimport pandas\n")

    assert disallowed_imports(tree, extra_prefixes=("trading_bot.config",)) == ["pandas"]


def test_scanner_does_not_treat_a_longer_name_as_an_allowed_prefix() -> None:
    assert disallowed_imports(ast.parse("import alembicx\nimport sqlalchemyx\n")) == [
        "alembicx",
        "sqlalchemyx",
    ]


# --- Fresh-interpreter import: the engine loads no heavy library ---------------------------

_WATCHED_MODULES = ("pandas", "numpy", "alembic", "fastapi")


def _fresh_interpreter_sys_modules(module: str) -> dict[str, bool]:
    src_dir = str(Path(trading_bot.__file__).resolve().parent.parent)
    report = f"print(json.dumps({{name: name in sys.modules for name in {_WATCHED_MODULES!r}}}))"
    script = "\n".join([f"import {module}", "import json", "import sys", report])
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    result: dict[str, bool] = json.loads(completed.stdout)
    return result


@pytest.mark.parametrize(
    "module",
    [
        "trading_bot.persistence.engine",
        "trading_bot.persistence.base",
        "trading_bot.persistence.models",
        "trading_bot.persistence.types",
    ],
)
def test_the_lightweight_persistence_modules_load_no_heavy_library(module: str) -> None:
    assert _fresh_interpreter_sys_modules(module) == {
        "pandas": False,
        "numpy": False,
        "alembic": False,
        "fastapi": False,
    }


def test_the_fresh_interpreter_check_actually_detects_alembic() -> None:
    """Control: the migrator does load Alembic, so the checks above are not vacuous."""
    report = _fresh_interpreter_sys_modules("trading_bot.persistence.migrator")

    assert report["alembic"] is True
    assert report["pandas"] is False
    assert report["fastapi"] is False
