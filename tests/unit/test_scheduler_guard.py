"""Isolation guard for ``scheduler/`` (spec 016, T10, AC20).

An AST scan enforces decision D141: the package depends on ports and pure functions only,
APScheduler lives in exactly two modules, the provider quirk in exactly one, the clock is always
the injected one, and no call ever hands a traceback to the log (decision D154, issue #50).

Each scanner is exercised against synthetic snippets first, so a green result on the real
modules is not merely "the scanner never triggers".
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import trading_bot.domain
import trading_bot.scheduler

# The scheduler depends on the calendar, the run report, the unit-of-work port, the injected
# clock and the state records. Nothing else, and never an implementation.
_ALLOWED_PREFIXES = (
    "trading_bot.domain",
    "trading_bot.engine.results",
    "trading_bot.engine.unit_of_work",
    "trading_bot.persistence.clock",
    "trading_bot.persistence.state",
    "trading_bot.scheduler",
)
# The two modules that may know APScheduler exists, and the one that may know the provider.
_APSCHEDULER_MODULES = frozenset({"scheduler/trigger.py", "scheduler/service.py"})
_PROVIDER_MODULE = "scheduler/slots.py"
_PROVIDER_PREFIX = "trading_bot.data.yahoo.provider"

_FORBIDDEN_EVERYWHERE = (
    "sqlalchemy",
    "trading_bot.persistence.database",
    "trading_bot.persistence.repositories",
    "trading_bot.cli",
    "trading_bot.config",
    "telegram",
    "yfinance",
    "talib",
    "exchange_calendars",
    "fastapi",
    "pandas",
)

_CLOCK_ATTRIBUTES = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})
_CLOCK_MODULES = frozenset({"datetime", "date", "time"})
_BARE_CLOCK_NAMES = frozenset({"utcnow", "monotonic", "perf_counter", "time", "time_ns"})
_TRACEBACK_KEYWORDS = frozenset({"exc_info", "stack_info"})
# ``nominal_close`` would fire a day late for ``1d`` and skip the last ``1h`` candle of every
# session (spec 004); ``time.sleep`` would block the loop that drives the whole bot.
_BANNED_NAMES = frozenset({"nominal_close", "sleep"})

SCHEDULER_DIR = Path(trading_bot.scheduler.__file__).resolve().parent
DOMAIN_DIR = Path(trading_bot.domain.__file__).resolve().parent
PACKAGE_ROOT = SCHEDULER_DIR.parent
SCANNED_FILES = sorted(SCHEDULER_DIR.rglob("*.py"))


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
    """Names of another layer, forbidden in every module of the package."""
    return sorted({name for name in imported_names(tree) if _matches(name, _FORBIDDEN_EVERYWHERE)})


def clock_calls(tree: ast.Module) -> list[str]:
    """Reads of the wall clock: the clock is always the injected one (decision D152)."""
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
    """``exc_info=`` and ``stack_info=``: a traceback is never redacted (issue #50)."""
    return [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg in _TRACEBACK_KEYWORDS
    ]


def banned_names(tree: ast.Module) -> list[str]:
    """``nominal_close`` anywhere, and ``sleep`` only as the blocking ``time.sleep``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _BANNED_NAMES:
            module = node.value.id if isinstance(node.value, ast.Name) else ""
            if node.attr == "nominal_close" or module == "time":
                found.append(f"{module}.{node.attr}" if module else node.attr)
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            # ``sleep`` as a bare name is only banned when it came from ``time``.
            if node.id == "nominal_close" or "time" in _time_imports(tree):
                found.append(node.id)
        elif isinstance(node, ast.ImportFrom):
            found.extend(alias.name for alias in node.names if alias.name == "nominal_close")
    return found


def _time_imports(tree: ast.Module) -> set[str]:
    """``{"time"}`` when the module does ``from time import sleep``."""
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "time"
        and any(alias.name == "sleep" for alias in node.names)
    }


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


# --- The real modules -----------------------------------------------------------------------


def test_at_least_the_expected_modules_were_scanned() -> None:
    """Guards against an empty glob silently making every parametrized test vacuous."""
    assert {_relative(path) for path in SCANNED_FILES} == {
        "scheduler/__init__.py",
        "scheduler/policy.py",
        "scheduler/runner.py",
        "scheduler/service.py",
        "scheduler/slots.py",
        "scheduler/state.py",
        "scheduler/trigger.py",
    }


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_module_imports_stay_within_the_allowlist(path: Path) -> None:
    extra: tuple[str, ...] = ()
    if _relative(path) in _APSCHEDULER_MODULES:
        extra = ("apscheduler",)
    elif _relative(path) == _PROVIDER_MODULE:
        extra = (_PROVIDER_PREFIX,)
    found = disallowed_imports(_parse(path), extra_prefixes=extra)

    assert found == [], f"{_relative(path)} imports outside the scheduler allowlist: {found}"


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
    """Decision D154: APScheduler would render a traceback the filter cannot redact."""
    found = traceback_keywords(_parse(path))

    assert found == [], f"{_relative(path)} passes {found} to a call"


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_relative)
def test_no_module_names_a_nominal_close_or_a_blocking_sleep(path: Path) -> None:
    found = banned_names(_parse(path))

    assert found == [], f"{_relative(path)} names {found}"


def test_apscheduler_lives_in_exactly_two_modules() -> None:
    importers = {
        _relative(path)
        for path in SCANNED_FILES
        if _matches_any(imported_names(_parse(path)), "apscheduler")
    }

    assert importers == set(_APSCHEDULER_MODULES)


def test_the_provider_quirk_lives_in_exactly_one_module() -> None:
    importers = {
        _relative(path)
        for path in SCANNED_FILES
        if _matches_any(imported_names(_parse(path)), "trading_bot.data")
    }

    assert importers == {_PROVIDER_MODULE}
    assert _PROVIDER_PREFIX in imported_names(_parse(SCHEDULER_DIR / "slots.py"))


def test_the_domain_imports_nothing_from_the_scheduler() -> None:
    """The dependency only goes one way (CLAUDE.md rule 3)."""
    for path in sorted(DOMAIN_DIR.rglob("*.py")):
        offenders = sorted(
            name
            for name in imported_names(_parse(path))
            if name.startswith("trading_bot.scheduler")
        )
        assert offenders == [], f"{path.name} imports {offenders}"


def _matches_any(names: list[str], prefix: str) -> bool:
    return any(_matches(name, (prefix,)) for name in names)


# --- Self-tests: the scanners reject synthetic violations ------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy\n",
        "from sqlalchemy.orm import Session\n",
        "from trading_bot.persistence.database import Database\n",
        "from trading_bot.persistence.repositories.protocols import BotStateRepository\n",
        "from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository\n",
        "import telegram\n",
        "import yfinance\n",
        "import talib\n",
        "import pandas as pd\n",
        "from fastapi import FastAPI\n",
        "from trading_bot.cli.main import main\n",
        "from trading_bot.config import Settings\n",
        "from apscheduler.triggers.base import BaseTrigger\n",
        "from trading_bot.data.yahoo.provider import is_unpublished_hour\n",
        "from trading_bot.engine.signal_engine import SignalEngine\n",
        "from . import policy\n",
    ],
)
def test_the_import_scanner_flags_a_forbidden_import(source: str) -> None:
    assert disallowed_imports(ast.parse(source)) != []


def test_the_import_scanner_accepts_every_form_the_scheduler_needs() -> None:
    source = (
        "from __future__ import annotations\n"
        "import asyncio\n"
        "import logging\n"
        "from collections.abc import Callable\n"
        "from datetime import datetime, timedelta\n"
        "from trading_bot.domain.market_calendar.sessions import MarketCalendar\n"
        "from trading_bot.domain.timeframe import Timeframe\n"
        "from trading_bot.engine.results import RunReport\n"
        "from trading_bot.engine.unit_of_work import in_unit_of_work\n"
        "from trading_bot.persistence.clock import Clock\n"
        "from trading_bot.persistence.state import LastRun\n"
        "from trading_bot.scheduler.policy import DEFAULT_POLICY\n"
    )

    assert disallowed_imports(ast.parse(source)) == []


def test_the_import_scanner_accepts_apscheduler_only_with_the_allowance() -> None:
    tree = ast.parse("from apscheduler.schedulers.base import BaseScheduler\nimport telegram\n")

    assert disallowed_imports(tree, extra_prefixes=("apscheduler",)) == ["telegram"]


def test_the_import_scanner_does_not_treat_a_longer_name_as_a_prefix() -> None:
    assert disallowed_imports(ast.parse("import apschedulerx\nimport sqlalchemyx\n")) == [
        "apschedulerx",
        "sqlalchemyx",
    ]


def test_the_layer_scanner_flags_only_the_other_layers() -> None:
    tree = ast.parse("import telegram\nimport asyncio\nfrom trading_bot.config import Settings\n")

    assert forbidden_imports(tree) == ["telegram", "trading_bot.config"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from datetime import datetime\nvalue = datetime.now()\n", ["now"]),
        ("x = a.utcnow()\ny = b.today()\n", ["utcnow", "today"]),
        ("import time\nx = time.time()\n", ["time"]),
        ("import time\nx = time.monotonic()\n", ["monotonic"]),
        ("from time import monotonic\nx = monotonic()\n", ["monotonic"]),
        ("clock = datetime.now\n", ["now"]),
        ("from datetime import date\nd = date.today()\n", ["today"]),
    ],
)
def test_the_clock_scanner_flags_a_read_of_the_wall_clock(source: str, expected: list[str]) -> None:
    assert clock_calls(ast.parse(source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "instant = self._clock()\n",
        "text = close.isoformat()\n",
        "seconds = wait.total_seconds()\n",
        "await asyncio.sleep(0)\n",
    ],
)
def test_the_clock_scanner_ignores_the_injected_clock(source: str) -> None:
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
    source = "logger.error('%s run failed: %s', code, type(error).__name__)\n"

    assert traceback_keywords(ast.parse(source)) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("close = timeframe.nominal_close(label)\n", ["timeframe.nominal_close"]),
        ("from trading_bot.domain.timeframe import nominal_close\n", ["nominal_close"]),
        ("import time\ntime.sleep(5)\n", ["time.sleep"]),
        ("from time import sleep\nsleep(5)\n", ["sleep"]),
    ],
)
def test_the_banned_name_scanner_flags_the_two_forbidden_calls(
    source: str, expected: list[str]
) -> None:
    assert banned_names(ast.parse(source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "await self._sleep(seconds)\n",
        "await asyncio.sleep(0)\n",
        "sleep: Sleep = asyncio.sleep\n",
    ],
)
def test_the_banned_name_scanner_accepts_the_injected_sleep(source: str) -> None:
    assert banned_names(ast.parse(source)) == []


def test_the_state_scanner_flags_module_level_containers() -> None:
    tree = ast.parse("_A = {}\n_B = []\n_C = set()\n_D: dict[str, int] = {}\n")

    assert mutable_module_level_names(tree) == ["_A", "_B", "_C", "_D"]


def test_the_state_scanner_allows_dunder_all_tuples_and_scalars() -> None:
    tree = ast.parse("__all__ = ['a']\n_TUPLE = (1, 2)\n_NAME: str = 'trading_bot.scheduler'\n")

    assert mutable_module_level_names(tree) == []
