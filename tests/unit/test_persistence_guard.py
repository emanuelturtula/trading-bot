"""Isolation guard for ``src/trading_bot/persistence`` (spec 012 T14 AC24; spec 013 T16 AC28).

An AST scan enforces the import allowlist: the standard library, ``sqlalchemy``, ``alembic``,
``trading_bot.domain.utc`` and the package itself, plus the **per-file** allowances of spec 013
Design 12 and ``trading_bot.config`` in ``migrations/env.py`` only (the developer CLI path of
spec 012 Design 7.4). Nothing in the package may reach pandas, numpy, TA-Lib, yfinance, the
market calendar library, FastAPI or ``data/``, so importing ``Base`` never drags a heavy
dependency in and the package stays reusable by the Telegram and API layers.

The per-file allowances are what keep that true while one module parses rules: measured,
``trading_bot.domain.signals`` and ``trading_bot.domain.timeframe`` pull nothing heavy, while
``trading_bot.domain.rules.schema`` pulls pydantic, pandas, numpy and TA-Lib through the
indicator catalog (decision D99). Subprocess checks confirm both directions in a fresh
interpreter, and the scanner is exercised against synthetic snippets first, so a green result
is not vacuous.

``domain/`` never imports ``persistence``: the purity guard of ``test_domain_purity.py``
covers the domain allowlist, and one test here pins the direction of the dependency.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import trading_bot.domain
import trading_bot.persistence

_ALLOWED_PREFIXES = ("sqlalchemy", "alembic", "trading_bot.domain.utc", "trading_bot.persistence")
# The per-file allowances of spec 013 Design 12: the schema and the errors may name the value
# objects they store, and only the modules that must parse a rule document may load the rule
# schema, which brings pydantic and the indicator catalog with it.
_DOMAIN_VALUES = ("trading_bot.domain.timeframe", "trading_bot.domain.signals")
_DOMAIN_RULE_ERRORS = (*_DOMAIN_VALUES, "trading_bot.domain.rules.errors")
_DOMAIN_RULE_SCHEMA = (*_DOMAIN_RULE_ERRORS, "trading_bot.domain.rules.schema")
# ``env.py`` is the single module allowed to read the settings, and only when no caller
# supplied a connection (the developer CLI path).
_EXTRA_PREFIXES_BY_FILE = {
    "migrations/env.py": ("trading_bot.config",),
    "models.py": _DOMAIN_VALUES,
    "types.py": _DOMAIN_VALUES,
    "errors.py": _DOMAIN_RULE_ERRORS,
    "records.py": _DOMAIN_RULE_SCHEMA,
    "repositories/protocols.py": _DOMAIN_RULE_SCHEMA,
    "repositories/tickers.py": _DOMAIN_RULE_SCHEMA,
    "repositories/rules.py": _DOMAIN_RULE_SCHEMA,
    "repositories/assignments.py": _DOMAIN_RULE_SCHEMA,
}
_FORBIDDEN_EVERYWHERE = (
    "pandas",
    "numpy",
    "talib",
    "yfinance",
    "exchange_calendars",
    "fastapi",
    "trading_bot.data",
)

CREATE_ENGINE = re.compile(r"\bcreate_engine\b")
BARE_DATETIME = re.compile(r"\bDateTime\b")

SRC_DIR = Path(trading_bot.__file__).resolve().parent
PERSISTENCE_DIR = Path(trading_bot.persistence.__file__).resolve().parent
REVISIONS_DIR = PERSISTENCE_DIR / "migrations" / "versions"
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
        "clock.py",
        "database.py",
        "engine.py",
        "errors.py",
        "migrator.py",
        "models.py",
        "records.py",
        "types.py",
        "migrations/env.py",
        "migrations/versions/0001_baseline.py",
        "migrations/versions/0002_configuration_tables.py",
        "repositories/__init__.py",
        "repositories/assignments.py",
        "repositories/protocols.py",
        "repositories/rules.py",
        "repositories/tickers.py",
    }


def test_the_settings_are_allowed_in_the_alembic_environment_only() -> None:
    assert _EXTRA_PREFIXES_BY_FILE["migrations/env.py"] == ("trading_bot.config",)

    others = [path for path in PERSISTENCE_FILES if _relative(path) != "migrations/env.py"]
    for path in others:
        source = path.read_text(encoding="utf-8")
        assert "trading_bot.config" not in source, _relative(path)


def test_only_the_named_files_may_load_the_rule_schema() -> None:
    """Decision D99: the cost of pydantic and the catalog is confined to those modules."""
    allowed = {
        name
        for name, prefixes in _EXTRA_PREFIXES_BY_FILE.items()
        if "trading_bot.domain.rules.schema" in prefixes
    }

    assert allowed == {
        "records.py",
        "repositories/protocols.py",
        "repositories/tickers.py",
        "repositories/rules.py",
        "repositories/assignments.py",
    }
    for path in PERSISTENCE_FILES:
        if _relative(path) in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "domain.rules.schema" not in source, _relative(path)


def test_create_engine_is_named_only_by_the_engine_module() -> None:
    """One factory builds every engine, so no caller can end up without the pragmas."""
    offenders = [
        path.relative_to(SRC_DIR).as_posix()
        for path in sorted(SRC_DIR.rglob("*.py"))
        if path.name != "engine.py" and CREATE_ENGINE.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_sqlalchemy_datetime_type_is_named_only_by_the_types_module() -> None:
    """Every timestamp **column** goes through ``UtcDateTime`` (spec 012, D74).

    An Alembic revision is the documented exception: it must be self-contained, so it names the
    SQLAlchemy types its project types render instead of importing application code that a later
    refactor could rename, which would break ``upgrade head`` on a fresh database.
    """
    offenders = [
        path.relative_to(SRC_DIR).as_posix()
        for path in sorted(SRC_DIR.rglob("*.py"))
        if path.name != "types.py"
        and REVISIONS_DIR not in path.parents
        and BARE_DATETIME.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_a_revision_is_self_contained_and_imports_no_application_type() -> None:
    """A renamed or removed column type would otherwise break a fresh ``upgrade head``."""
    revisions = sorted(REVISIONS_DIR.glob("*.py"))

    assert [path.name for path in revisions] == [
        "0001_baseline.py",
        "0002_configuration_tables.py",
    ]
    for path in revisions:
        imported = {
            name
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in _imported_module_names(node)
        }
        offenders = sorted(name for name in imported if name.startswith("trading_bot"))
        assert offenders == [], f"{path.name} imports {offenders}"


def test_the_two_structural_guards_are_not_vacuous() -> None:
    assert CREATE_ENGINE.search("from sqlalchemy import create_engine")
    assert not CREATE_ENGINE.search("from x import create_database_engine")
    assert BARE_DATETIME.search("from sqlalchemy import DateTime")
    assert not BARE_DATETIME.search("class UtcDateTime(TypeDecorator[datetime]):")


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

_WATCHED_MODULES = ("pandas", "numpy", "talib", "pydantic", "alembic", "fastapi")


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
        "trading_bot.persistence.database",
    ],
)
def test_the_lightweight_persistence_modules_load_no_heavy_library(module: str) -> None:
    """Telegram and the API load the schema and the handle without the analysis stack."""
    assert _fresh_interpreter_sys_modules(module) == {
        "pandas": False,
        "numpy": False,
        "talib": False,
        "pydantic": False,
        "alembic": False,
        "fastapi": False,
    }


def test_the_fresh_interpreter_check_actually_detects_alembic() -> None:
    """Control: the migrator does load Alembic, so the checks above are not vacuous."""
    report = _fresh_interpreter_sys_modules("trading_bot.persistence.migrator")

    assert report["alembic"] is True
    assert report["pandas"] is False
    assert report["fastapi"] is False


def test_the_rule_repository_does_load_the_analysis_stack() -> None:
    """Control and decision D99: that cost is deliberate and confined to one module."""
    report = _fresh_interpreter_sys_modules("trading_bot.persistence.repositories.rules")

    assert report["pydantic"] is True
    assert report["pandas"] is True
    assert report["talib"] is True
    assert report["fastapi"] is False
