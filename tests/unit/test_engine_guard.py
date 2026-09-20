"""Isolation guard for ``engine/`` and ``notifications/`` (spec 015, T10, AC21).

An AST scan enforces the six rules of spec 015 Design 11: no clock, no SQLAlchemy or ``Sql*``
repository outside ``engine/sql_unit_of_work.py``, no library of another layer, no mutable
module-level state, no ``exc_info``/``stack_info`` keyword in any log call (decision D135), and
``domain/`` importing neither package.

Each scanner is exercised against synthetic snippets first, so a green result on the real
modules is not just "the scanner never triggers". The clock scanner in particular must tell
``datetime.now()`` from ``report.now``, a field of the run report.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import trading_bot.domain
import trading_bot.engine
import trading_bot.notifications

# The engine depends on ports and pure functions: pandas, the domain, the provider port and the
# light persistence modules that define what a repository returns.
_ALLOWED_PREFIXES = (
    "pandas",
    "trading_bot.data",
    "trading_bot.domain",
    "trading_bot.engine",
    "trading_bot.notifications",
    "trading_bot.persistence.clock",
    "trading_bot.persistence.errors",
    "trading_bot.persistence.records",
    "trading_bot.persistence.repositories.protocols",
    "trading_bot.persistence.signal_records",
    "trading_bot.persistence.state",
)
# The single module allowed to name the implementations and the handle (decision D124).
_SQL_MODULE = "engine/sql_unit_of_work.py"
_SQL_PREFIXES = (
    "sqlalchemy",
    "trading_bot.persistence.database",
    "trading_bot.persistence.repositories",
)
_FORBIDDEN_EVERYWHERE = (
    "telegram",
    "yfinance",
    "talib",
    "exchange_calendars",
    "fastapi",
    "trading_bot.cli",
    "trading_bot.config",
)

_CLOCK_ATTRIBUTES = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})
_CLOCK_MODULES = frozenset({"datetime", "date", "time"})
_BARE_CLOCK_NAMES = frozenset({"utcnow", "monotonic", "perf_counter", "time", "time_ns"})
_TRACEBACK_KEYWORDS = frozenset({"exc_info", "stack_info"})

ENGINE_DIR = Path(trading_bot.engine.__file__).resolve().parent
NOTIFICATIONS_DIR = Path(trading_bot.notifications.__file__).resolve().parent
DOMAIN_DIR = Path(trading_bot.domain.__file__).resolve().parent
PACKAGE_ROOT = ENGINE_DIR.parent
SCANNED_FILES = sorted([*ENGINE_DIR.rglob("*.py"), *NOTIFICATIONS_DIR.rglob("*.py")])


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    dots = "." * node.level
    if node.module:
        return [f"{dots}{node.module}"]
    return [f"{dots}{alias.name}" for alias in node.names]


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def imported_names(tree: ast.Module) -> list[str]:
    return [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_module_names(node)
    ]


def disallowed_imports(tree: ast.Module, *, extra_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Names imported by ``tree`` that are outside the allowlist, in source order."""
    prefixes = (*_ALLOWED_PREFIXES, *extra_prefixes)
    return [
        name
        for name in imported_names(tree)
        if not (
            (name.split(".")[0] in sys.stdlib_module_names and not name.startswith("."))
            or _matches(name, prefixes)
        )
    ]


def forbidden_imports(tree: ast.Module) -> list[str]:
    """Names of another layer, forbidden in every module of both packages."""
    return sorted({name for name in imported_names(tree) if _matches(name, _FORBIDDEN_EVERYWHERE)})


def clock_calls(tree: ast.Module) -> list[str]:
    """Reads of the wall clock, without flagging ``report.now``, a field of the run report."""
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _CLOCK_ATTRIBUTES:
            on_a_clock_module = isinstance(node.value, ast.Name) and node.value.id in _CLOCK_MODULES
            if id(node) in called or on_a_clock_module:
                found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in _BARE_CLOCK_NAMES and id(node) in called:
            found.append(node.id)
    return found


def traceback_keywords(tree: ast.Module) -> list[str]:
    """``exc_info=`` and ``stack_info=`` arguments: a traceback is never redacted (D135)."""
    return [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg in _TRACEBACK_KEYWORDS
    ]


def _is_mutable_container_literal(value: ast.expr | None) -> bool:
    if isinstance(value, ast.Dict | ast.List | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"dict", "list", "set"}
    )


def mutable_module_level_names(tree: ast.Module) -> list[str]:
    """Module-level names bound to a ``dict``/``list``/``set``, other than ``__all__``."""
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if names == ["__all__"]:
            continue
        if _is_mutable_container_literal(value):
            found.extend(names)
    return found


# --- The real modules ---------------------------------------------------------------------


def test_at_least_the_expected_modules_were_scanned() -> None:
    """Guards against an empty glob silently making every parametrized test vacuous."""
    assert {_relative(path) for path in SCANNED_FILES} == {
        "engine/__init__.py",
        "engine/cooldown.py",
        "engine/planning.py",
        "engine/results.py",
        "engine/signal_engine.py",
        "engine/sql_unit_of_work.py",
        "engine/unit_of_work.py",
        "notifications/__init__.py",
        "notifications/notifier.py",
    }


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_module_imports_stay_within_the_allowlist(path: Path) -> None:
    extra = _SQL_PREFIXES if _relative(path) == _SQL_MODULE else ()
    found = disallowed_imports(_parse(path), extra_prefixes=extra)

    assert found == [], f"{_relative(path)} imports outside the engine allowlist: {found}"


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_no_module_imports_a_library_of_another_layer(path: Path) -> None:
    found = forbidden_imports(_parse(path))

    assert found == [], f"{_relative(path)} imports {found}"


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_no_module_reads_the_clock(path: Path) -> None:
    found = clock_calls(_parse(path))

    assert found == [], f"{_relative(path)} reads the clock: {found}"


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_no_module_has_mutable_module_level_state(path: Path) -> None:
    found = mutable_module_level_names(_parse(path))

    assert found == [], f"{_relative(path)} has mutable module-level state: {found}"


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_no_call_passes_a_traceback_to_the_log(path: Path) -> None:
    """Decision D135: ``RedactingFilter`` rewrites ``record.msg`` only (issue #50)."""
    found = traceback_keywords(_parse(path))

    assert found == [], f"{_relative(path)} passes {found} to a call"


def names_an_implementation(tree: ast.Module) -> list[str]:
    """SQLAlchemy, the ``Database`` handle and any repository module but the ports."""
    return sorted(
        {
            name
            for name in imported_names(tree)
            if _matches(name, ("sqlalchemy", "trading_bot.persistence.database"))
            or (
                _matches(name, ("trading_bot.persistence.repositories",))
                and not _matches(name, ("trading_bot.persistence.repositories.protocols",))
            )
        }
    )


def test_sqlalchemy_and_the_repository_implementations_live_in_one_module() -> None:
    offenders = {
        _relative(path): names_an_implementation(_parse(path))
        for path in SCANNED_FILES
        if _relative(path) != _SQL_MODULE and names_an_implementation(_parse(path))
    }

    assert offenders == {}
    # The ports are the documented exception: every module may name them.
    assert (
        names_an_implementation(
            ast.parse(
                "from trading_bot.persistence.repositories.protocols import SignalRepository\n"
            )
        )
        == []
    )
    assert names_an_implementation(
        ast.parse("from trading_bot.persistence.repositories.signals import SqlSignalRepository\n")
    ) == ["trading_bot.persistence.repositories.signals"]


def test_the_sql_unit_of_work_does_name_the_implementations() -> None:
    """Control: the allowance is not vacuous, and it really is confined to that module."""
    imported = set(imported_names(_parse(ENGINE_DIR / "sql_unit_of_work.py")))

    assert "trading_bot.persistence.database" in imported
    assert "trading_bot.persistence.repositories.signals" in imported


def test_the_domain_imports_neither_package() -> None:
    """The dependency only goes one way (CLAUDE.md rule 3)."""
    for path in sorted(DOMAIN_DIR.rglob("*.py")):
        offenders = sorted(
            name
            for name in imported_names(_parse(path))
            if name.startswith(("trading_bot.engine", "trading_bot.notifications"))
        )
        assert offenders == [], f"{path.name} imports {offenders}"


# --- Self-tests: the scanners reject synthetic violations ----------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy\n",
        "from sqlalchemy.orm import Session\n",
        "from trading_bot.persistence.database import Database\n",
        "from trading_bot.persistence.repositories.signals import SqlSignalRepository\n",
        "import telegram\n",
        "import yfinance\n",
        "import talib\n",
        "from fastapi import FastAPI\n",
        "from trading_bot.cli.main import main\n",
        "from trading_bot.config import Settings\n",
        "from . import cooldown\n",
    ],
)
def test_the_import_scanner_flags_a_forbidden_import(source: str) -> None:
    assert disallowed_imports(ast.parse(source)) != []


def test_the_import_scanner_accepts_every_form_the_engine_needs() -> None:
    source = (
        "from __future__ import annotations\n"
        "import asyncio\n"
        "import logging\n"
        "from collections.abc import Callable\n"
        "from datetime import datetime\n"
        "import pandas as pd\n"
        "from trading_bot.data.provider import MarketDataProvider\n"
        "from trading_bot.domain.rules.evaluator import evaluate\n"
        "from trading_bot.engine.results import RunReport\n"
        "from trading_bot.notifications.notifier import Notifier\n"
        "from trading_bot.persistence.errors import UnknownSignalError\n"
        "from trading_bot.persistence.repositories.protocols import SignalRepository\n"
    )

    assert disallowed_imports(ast.parse(source)) == []


def test_the_import_scanner_accepts_sqlalchemy_only_with_the_allowance() -> None:
    tree = ast.parse("import sqlalchemy\nimport telegram\n")

    assert disallowed_imports(tree, extra_prefixes=_SQL_PREFIXES) == ["telegram"]


def test_the_import_scanner_does_not_treat_a_longer_name_as_a_prefix() -> None:
    assert disallowed_imports(ast.parse("import pandasx\nimport sqlalchemyx\n")) == [
        "pandasx",
        "sqlalchemyx",
    ]


def test_the_layer_scanner_flags_only_the_other_layers() -> None:
    tree = ast.parse("import telegram\nimport pandas\nfrom trading_bot.cli import main\n")

    assert forbidden_imports(tree) == ["telegram", "trading_bot.cli"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from datetime import datetime\nvalue = datetime.now()\n", ["now"]),
        ("x = a.utcnow()\ny = b.today()\n", ["utcnow", "today"]),
        ("import time\nx = time.time()\n", ["time"]),
        ("import time\nx = time.monotonic()\n", ["monotonic"]),
        ("from time import monotonic\nx = monotonic()\n", ["monotonic"]),
        ("clock = datetime.now\n", ["now"]),  # captured without being called
        ("from datetime import date\nd = date.today()\n", ["today"]),
    ],
)
def test_the_clock_scanner_flags_a_read_of_the_wall_clock(source: str, expected: list[str]) -> None:
    assert clock_calls(ast.parse(source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "value = report.now\n",  # the field of RunReport, not a clock
        "text = report.now.isoformat()\n",
        "instant = to_utc(self.now)\n",
        "record = RunReport(timeframe=timeframe, now=now, paused=False, tickers=())\n",
        "delta = value.total_seconds()\n",
    ],
)
def test_the_clock_scanner_ignores_the_report_field(source: str) -> None:
    assert clock_calls(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [
        "logger.error('failed', exc_info=True)\n",
        "logger.warning('failed', exc_info=error)\n",
        "logger.log(level, 'failed', stack_info=True)\n",
    ],
)
def test_the_traceback_scanner_flags_a_logged_traceback(source: str) -> None:
    assert traceback_keywords(ast.parse(source)) != []


def test_the_traceback_scanner_accepts_a_plain_record() -> None:
    source = "logger.error('%s failed (%s): %s', symbol, kind, type(error).__name__)\n"

    assert traceback_keywords(ast.parse(source)) == []


def test_the_state_scanner_flags_module_level_containers() -> None:
    tree = ast.parse("_A = {}\n_B = []\n_C = set()\n_D: dict[str, int] = {}\n")

    assert mutable_module_level_names(tree) == ["_A", "_B", "_C", "_D"]


def test_the_state_scanner_allows_dunder_all_tuples_and_scalars() -> None:
    tree = ast.parse("__all__ = ['a']\n_TUPLE = (1, 2)\n_NAME: str = 'trading_bot.engine'\n")

    assert mutable_module_level_names(tree) == []
