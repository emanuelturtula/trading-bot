"""Purity guard for ``src/trading_bot/domain`` (spec 004, T15, AC15; spec 005, T12; spec 007, T12;
spec 009, T12; spec 010, T14).

An AST scan enforces the import allowlist, the ban on reading the clock and the ban on
mutable module-level state (everything except ``__all__``). ``talib`` is allowed only in
``indicators/talib_kernels.py``, so TA-Lib stays replaceable (spec 005, Design 9), the rule
evaluator passes with the default allowlist (spec 007, AC15), and ``exchange_calendars`` is
allowed only in ``market_calendar/nyse.py`` while the calendar model in
``market_calendar/sessions.py`` needs no allowance (spec 009, AC14), and neither do
``candle_normalization.py`` and ``closed_candles.py`` (spec 010, AC18). A subprocess
check confirms that importing the lightweight modules (``utc``, ``timeframe``, ``signals``,
``indicators.errors``, ``indicators.params``, ``market_calendar.sessions``) in a fresh
interpreter never pulls ``pandas``, ``numpy`` or ``exchange_calendars`` into ``sys.modules``
(CLAUDE.md rule 3).

The scanner itself is exercised against synthetic source snippets first, so a green result
on the real domain modules is not just "the scanner never triggers".
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

# --- The allowlist (spec 004, AC15) --------------------------------------------------------

_ALLOWED_EXACT_MODULES = frozenset(
    {
        "__future__",
        "bisect",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "math",
        "numbers",
        "types",
        "typing",
    }
)
_ALLOWED_PREFIXES = ("numpy", "pandas", "trading_bot.domain")
# Extra import prefixes allowed in some modules only, by path relative to the domain package.
# ``talib`` stays behind the kernels (spec 005, Design 9) and ``pydantic`` behind the rule
# schema (spec 006, AC22), so the rest of the domain keeps both replaceable. ``pydantic_core``
# only provides the ``ErrorDetails`` type of a ``ValidationError``, and ``json`` is the strict
# JSON reader of ``parse_rule`` (spec 006, Design 9); both are pure and side-effect free.
# ``exchange_calendars`` stays behind the NYSE calendar builder (spec 009, D15), so the calendar
# model and its queries do not depend on the library.
_EXTRA_PREFIXES_BY_FILE = {
    "indicators/talib_kernels.py": ("talib",),
    "rules/schema.py": ("json", "pydantic", "pydantic_core"),
    "rules/json_schema.py": ("pydantic",),
    "market_calendar/nyse.py": ("exchange_calendars",),
}
_CLOCK_ATTRIBUTES = frozenset({"now", "utcnow", "today"})

DOMAIN_DIR = Path(trading_bot.domain.__file__).resolve().parent
DOMAIN_FILES = sorted(DOMAIN_DIR.rglob("*.py"))


def _relative(path: Path) -> str:
    """``path`` relative to the domain package, with forward slashes on every platform."""
    return path.relative_to(DOMAIN_DIR).as_posix()


def _imported_module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    dots = "." * node.level  # a relative import: "from . import x" or "from .mod import y"
    if node.module:
        return [f"{dots}{node.module}"]
    return [f"{dots}{alias.name}" for alias in node.names]


def _is_allowed_module(name: str, prefixes: tuple[str, ...]) -> bool:
    if name in _ALLOWED_EXACT_MODULES:
        return True
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def disallowed_imports(tree: ast.Module, *, extra_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Names imported by ``tree`` that are outside the allowlist, in source order.

    ``extra_prefixes`` extends the allowed prefixes for one module (``talib`` for the kernels).
    """
    prefixes = (*_ALLOWED_PREFIXES, *extra_prefixes)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = _imported_module_names(node)
            found.extend(name for name in names if not _is_allowed_module(name, prefixes))
    return found


def clock_calls(tree: ast.Module) -> list[str]:
    """Attribute names that read the clock (``now``, ``utcnow``, ``today``, ``time.time``)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _CLOCK_ATTRIBUTES:
            found.append(node.attr)
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "time"
            and isinstance(node.value, ast.Name)
            and node.value.id == "time"
        ):
            found.append("time.time")
    return found


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


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- The real domain modules (T15) ----------------------------------------------------------


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=_relative)
def test_domain_module_imports_stay_within_the_allowlist(path: Path) -> None:
    extra = _EXTRA_PREFIXES_BY_FILE.get(_relative(path), ())
    found = disallowed_imports(_parse(path), extra_prefixes=extra)

    assert found == [], f"{_relative(path)} imports outside the domain allowlist: {found}"


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=_relative)
def test_domain_module_never_reads_the_clock(path: Path) -> None:
    found = clock_calls(_parse(path))

    assert found == [], f"{_relative(path)} reads the clock: {found}"


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=_relative)
def test_domain_module_has_no_mutable_module_level_state(path: Path) -> None:
    found = mutable_module_level_names(_parse(path))

    assert found == [], f"{_relative(path)} has mutable module-level state: {found}"


def test_at_least_the_expected_modules_were_scanned() -> None:
    """Guards against an empty glob silently making every parametrized test vacuous."""
    assert {_relative(path) for path in DOMAIN_FILES} == {
        "__init__.py",
        "utc.py",
        "timeframe.py",
        "candles.py",
        "signals.py",
        "candle_normalization.py",
        "closed_candles.py",
        "indicators/__init__.py",
        "indicators/errors.py",
        "indicators/params.py",
        "indicators/spec.py",
        "indicators/registry.py",
        "indicators/talib_kernels.py",
        "indicators/catalog.py",
        "rules/__init__.py",
        "rules/errors.py",
        "rules/schema.py",
        "rules/json_schema.py",
        "rules/evaluator.py",
        "market_calendar/__init__.py",
        "market_calendar/sessions.py",
        "market_calendar/nyse.py",
    }


def test_talib_pydantic_and_exchange_calendars_are_allowed_only_in_their_own_modules() -> None:
    assert _EXTRA_PREFIXES_BY_FILE == {
        "indicators/talib_kernels.py": ("talib",),
        "rules/schema.py": ("json", "pydantic", "pydantic_core"),
        "rules/json_schema.py": ("pydantic",),
        "market_calendar/nyse.py": ("exchange_calendars",),
    }


def test_the_rule_evaluator_needs_no_allowance() -> None:
    """The evaluator consumes the rule models without defining any (spec 007, AC15).

    It must pass with the default allowlist: no pydantic, no ``json``, no clock, no module-level
    cache, and the models come through ``trading_bot.domain.rules.schema``.
    """
    tree = _parse(DOMAIN_DIR / "rules" / "evaluator.py")
    imported = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_module_names(node)
    }

    assert "rules/evaluator.py" not in _EXTRA_PREFIXES_BY_FILE
    assert disallowed_imports(tree) == []
    assert clock_calls(tree) == []
    assert mutable_module_level_names(tree) == []
    assert not {"json", "pydantic", "pydantic_core"} & {name.split(".")[0] for name in imported}
    assert "trading_bot.domain.rules.schema" in imported


def test_the_market_calendar_model_needs_no_allowance() -> None:
    """The calendar model imports only the standard library and the domain (spec 009, AC14).

    It must pass with the default allowlist: no ``exchange_calendars``, pandas, numpy or
    ``zoneinfo`` (the time zone is injected), no clock and no module-level cache.
    """
    tree = _parse(DOMAIN_DIR / "market_calendar" / "sessions.py")
    imported = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_module_names(node)
    }
    standard_library = {"__future__", "bisect", "dataclasses", "datetime", "enum", "typing"}

    assert "market_calendar/sessions.py" not in _EXTRA_PREFIXES_BY_FILE
    assert disallowed_imports(tree) == []
    assert clock_calls(tree) == []
    assert mutable_module_level_names(tree) == []
    assert {name for name in imported if not name.startswith("trading_bot.domain.")} <= (
        standard_library
    )


@pytest.mark.parametrize("relative", ["candle_normalization.py", "closed_candles.py"])
def test_the_candle_normalization_modules_need_no_allowance(relative: str) -> None:
    """Normalization and open-candle removal pass with the default allowlist (spec 010, AC18).

    No per-file allowance, no clock and no module-level mutable state: both take a frame, a
    timeframe, an instant and the injected calendar, and return a result.
    """
    tree = _parse(DOMAIN_DIR / relative)

    assert relative not in _EXTRA_PREFIXES_BY_FILE
    assert disallowed_imports(tree) == []
    assert clock_calls(tree) == []
    assert mutable_module_level_names(tree) == []


def test_scanner_flags_exchange_calendars_without_the_nyse_allowance() -> None:
    tree = ast.parse(
        "import exchange_calendars\n"
        "from exchange_calendars.exchange_calendar_xnys import XNYSExchangeCalendar\n"
    )

    assert disallowed_imports(tree) == [
        "exchange_calendars",
        "exchange_calendars.exchange_calendar_xnys",
    ]
    assert disallowed_imports(tree, extra_prefixes=("exchange_calendars",)) == []


def test_scanner_flags_pydantic_without_the_rule_schema_allowance() -> None:
    tree = ast.parse("import pydantic\nfrom pydantic import BaseModel\n")

    assert disallowed_imports(tree) == ["pydantic", "pydantic"]


# --- Self-tests: the scanner rejects synthetic violations, not just real code ---------------


def test_scanner_flags_a_plain_import_outside_the_allowlist() -> None:
    assert disallowed_imports(ast.parse("import os\n")) == ["os"]


def test_scanner_flags_a_from_import_outside_the_allowlist() -> None:
    assert disallowed_imports(ast.parse("from pathlib import Path\n")) == ["pathlib"]


def test_scanner_flags_a_relative_import() -> None:
    found = disallowed_imports(ast.parse("from . import utc\n"))
    assert found == [".utc"]


def test_scanner_accepts_every_form_of_allowed_import() -> None:
    source = (
        "from __future__ import annotations\n"
        "import bisect\n"
        "from bisect import bisect_right\n"
        "from collections.abc import Mapping\n"
        "from dataclasses import dataclass\n"
        "from datetime import UTC\n"
        "from enum import StrEnum\n"
        "from math import isfinite\n"
        "from numbers import Real\n"
        "from types import MappingProxyType\n"
        "from typing import Final\n"
        "import numpy as np\n"
        "import numpy.typing as npt\n"
        "import pandas as pd\n"
        "from trading_bot.domain.utc import to_utc\n"
        "import trading_bot.domain.timeframe\n"
    )
    assert disallowed_imports(ast.parse(source)) == []


def test_scanner_flags_talib_without_the_kernels_allowance() -> None:
    tree = ast.parse("import talib\nfrom talib._ta_lib import MA_Type\n")

    assert disallowed_imports(tree) == ["talib", "talib._ta_lib"]


def test_scanner_accepts_talib_with_the_kernels_allowance() -> None:
    tree = ast.parse("import talib\nfrom talib._ta_lib import MA_Type\nimport os\n")

    assert disallowed_imports(tree, extra_prefixes=("talib",)) == ["os"]


def test_scanner_does_not_treat_a_longer_name_as_an_allowed_prefix() -> None:
    assert disallowed_imports(ast.parse("import talibx\n"), extra_prefixes=("talib",)) == ["talibx"]


def test_scanner_flags_datetime_now() -> None:
    tree = ast.parse("from datetime import datetime\nvalue = datetime.now()\n")
    assert clock_calls(tree) == ["now"]


def test_scanner_flags_utcnow_and_today() -> None:
    tree = ast.parse("x = a.utcnow()\ny = b.today()\n")
    assert clock_calls(tree) == ["utcnow", "today"]


def test_scanner_flags_time_time() -> None:
    tree = ast.parse("import time\nx = time.time()\n")
    assert clock_calls(tree) == ["time.time"]


def test_scanner_ignores_unrelated_attribute_names() -> None:
    tree = ast.parse("x = value.total_seconds()\ny = value.notoday\n")
    assert clock_calls(tree) == []


def test_scanner_flags_module_level_dict_list_and_set() -> None:
    tree = ast.parse(
        "_A = {}\n_B = []\n_C = set()\n_D: dict[str, int] = {}\n_E = {k: k for k in 'ab'}\n"
    )
    assert mutable_module_level_names(tree) == ["_A", "_B", "_C", "_D", "_E"]


def test_scanner_allows_dunder_all_tuples_and_scalars() -> None:
    tree = ast.parse("__all__ = ['a', 'b']\n_TUPLE = (1, 2, 3)\n_N: int = 5\n_S = 'text'\n")
    assert mutable_module_level_names(tree) == []


def test_scanner_ignores_mutable_containers_built_inside_functions() -> None:
    tree = ast.parse("def f() -> list[int]:\n    values = []\n    return values\n")
    assert mutable_module_level_names(tree) == []


# --- Fresh-interpreter import: heavy libraries stay unloaded (T15, AC15; spec 009, AC14) ----

_HEAVY_MODULES = ("pandas", "numpy", "exchange_calendars")


def _fresh_interpreter_sys_modules(*modules: str) -> dict[str, bool]:
    """Import ``modules`` in a brand-new interpreter and report which heavy libraries loaded."""
    src_dir = str(Path(trading_bot.__file__).resolve().parent.parent)
    report_line = f"print(json.dumps({{name: name in sys.modules for name in {_HEAVY_MODULES!r}}}))"
    lines = [*(f"import {module}" for module in modules), "import json", "import sys", report_line]
    script = "\n".join(lines)
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise AssertionError(
            f"fresh interpreter import of {modules} failed: {error.stderr}"
        ) from error
    result: dict[str, bool] = json.loads(completed.stdout)
    return result


@pytest.mark.parametrize(
    "module",
    [
        "trading_bot.domain.utc",
        "trading_bot.domain.timeframe",
        "trading_bot.domain.signals",
        "trading_bot.domain.indicators.errors",
        "trading_bot.domain.indicators.params",
        "trading_bot.domain.rules.errors",
        "trading_bot.domain.market_calendar.sessions",
    ],
)
def test_lightweight_domain_modules_do_not_load_heavy_libraries(module: str) -> None:
    report = _fresh_interpreter_sys_modules(module)

    assert report == {"pandas": False, "numpy": False, "exchange_calendars": False}


@pytest.mark.parametrize(
    ("module", "loads_exchange_calendars"),
    [
        ("trading_bot.domain.candles", False),
        ("trading_bot.domain.rules.schema", False),
        ("trading_bot.domain.market_calendar.nyse", True),
    ],
)
def test_the_fresh_interpreter_check_actually_detects_heavy_libraries(
    module: str, loads_exchange_calendars: bool
) -> None:
    """Control: these modules do load heavy libraries, so the checks above are not vacuous.

    ``rules.schema`` imports the indicator catalog, so it loads pandas, numpy and TA-Lib;
    ``rules.errors`` must not (spec 006, AC22). ``market_calendar.nyse`` imports
    ``exchange_calendars``, which loads pandas and numpy; ``market_calendar.sessions`` must not
    (spec 009, AC14).
    """
    report = _fresh_interpreter_sys_modules(module)

    assert report == {
        "pandas": True,
        "numpy": True,
        "exchange_calendars": loads_exchange_calendars,
    }
