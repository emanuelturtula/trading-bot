"""TA-Lib isolation and global settings (spec 005, T16; AC12).

``test_domain_purity.py`` (the developer's T12) already scans ``domain/`` for a wider import
allowlist and confirms that ``registry``/``params``/``errors`` never load pandas or numpy in a
fresh interpreter; ``test_indicator_catalog.py`` (T11) already drives the unstable-period guard
and confirms TA-Lib's global settings are back at their defaults after computing every indicator.
This file adds the three checks specific to AC12 that neither covers: that ``talib`` itself (not
just pandas/numpy) stays out of a fresh interpreter's modules for the lightweight modules and
loads for ``catalog``; that no file under ``src/`` (not only ``domain/``) references TA-Lib's
global setters or its ``abstract``/``stream`` APIs; and the ``set_compatibility(1)`` no-op
canary, restored in ``finally`` (spec 005, Design 9, corrected after review: TA-Lib C 0.8.1
removed the MetaStock compatibility mode, so the setter is a documented no-op and
``get_compatibility()`` never reflects it).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import talib

import trading_bot
from tests.fixtures.candles import Scenario, synthetic_candles
from trading_bot.domain.indicators.catalog import REGISTRY

# --- No file under src/ references a forbidden TA-Lib global or the abstract/stream APIs ----

SRC_DIR = Path(trading_bot.__file__).resolve().parent
SRC_FILES = sorted(SRC_DIR.rglob("*.py"))

_FORBIDDEN_ATTRIBUTES = frozenset({"set_unstable_period", "set_compatibility"})
_FORBIDDEN_TALIB_ATTRIBUTES = frozenset({"abstract", "stream"})


def _relative(path: Path) -> str:
    return path.relative_to(SRC_DIR).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_talib(tree: ast.Module) -> bool:
    """Whether ``tree`` imports the ``talib`` package, directly or through a submodule."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(_is_talib(alias.name) for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and _is_talib(node.module):
            return True
    return False


def _is_talib(module: str) -> bool:
    return module == "talib" or module.startswith("talib.")


def forbidden_references(tree: ast.Module) -> list[str]:
    """Names of forbidden TA-Lib globals or ``talib.abstract``/``talib.stream`` uses in ``tree``.

    Matches a bare name (covers ``from talib import set_unstable_period``) or any attribute
    access ending in a forbidden name (covers ``talib.set_unstable_period(...)``), plus the
    dotted ``talib.abstract``/``talib.stream`` forms specifically (spec 005, Design 9).
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_ATTRIBUTES:
                found.append(node.attr)
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "talib"
                and node.attr in _FORBIDDEN_TALIB_ATTRIBUTES
            ):
                found.append(f"talib.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_ATTRIBUTES:
            found.append(node.id)
    return found


def test_only_the_kernels_module_imports_talib() -> None:
    importing = {_relative(path) for path in SRC_FILES if _imports_talib(_parse(path))}

    assert importing == {"domain/indicators/talib_kernels.py"}


@pytest.mark.parametrize("path", SRC_FILES, ids=_relative)
def test_no_file_references_forbidden_talib_globals_or_apis(path: Path) -> None:
    found = forbidden_references(_parse(path))

    assert found == [], f"{_relative(path)} references forbidden TA-Lib names: {found}"


def test_at_least_the_expected_files_were_scanned() -> None:
    """Guards against an empty glob silently making the parametrized test above vacuous."""
    assert {_relative(path) for path in SRC_FILES} >= {
        "__init__.py",
        "config.py",
        "main.py",
        "domain/indicators/talib_kernels.py",
        "domain/indicators/catalog.py",
    }


def test_scanner_flags_a_forbidden_setter_called_as_an_attribute() -> None:
    tree = ast.parse("talib.set_unstable_period('RSI', 3)\ntalib.set_compatibility(1)\n")

    assert forbidden_references(tree) == ["set_unstable_period", "set_compatibility"]


def test_scanner_flags_a_forbidden_setter_imported_by_name() -> None:
    tree = ast.parse("from talib import set_unstable_period\nset_unstable_period('RSI', 3)\n")

    assert forbidden_references(tree) == ["set_unstable_period"]


def test_scanner_flags_the_abstract_and_stream_apis() -> None:
    tree = ast.parse("import talib\ntalib.abstract.RSI\ntalib.stream.RSI\n")

    assert forbidden_references(tree) == ["talib.abstract", "talib.stream"]


def test_scanner_ignores_an_unrelated_abstract_attribute() -> None:
    tree = ast.parse("model.abstract\nx.stream()\n")

    assert forbidden_references(tree) == []


def test_scanner_accepts_get_unstable_period() -> None:
    tree = ast.parse("import talib\ntalib.get_unstable_period('RSI')\n")

    assert forbidden_references(tree) == []


def test_import_scanner_detects_every_import_form() -> None:
    assert _imports_talib(ast.parse("import talib\n"))
    assert _imports_talib(ast.parse("from talib import MA_Type\n"))
    assert _imports_talib(ast.parse("from talib._ta_lib import MA_Type\n"))
    assert not _imports_talib(ast.parse("import talibx\n"))
    assert not _imports_talib(ast.parse("import os\n"))


# --- talib itself stays out of a fresh interpreter for the lightweight modules (AC12) --------


def _fresh_interpreter_has_talib(module: str) -> bool:
    src_dir = str(Path(trading_bot.__file__).resolve().parent.parent)
    script = f"import {module}\nimport json, sys\nprint(json.dumps('talib' in sys.modules))"
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    result: bool = json.loads(completed.stdout)
    return result


@pytest.mark.parametrize(
    "module",
    [
        "trading_bot.domain.indicators.errors",
        "trading_bot.domain.indicators.params",
        "trading_bot.domain.indicators.spec",
        "trading_bot.domain.indicators.registry",
    ],
)
def test_lightweight_indicator_modules_leave_talib_unloaded(module: str) -> None:
    assert _fresh_interpreter_has_talib(module) is False


def test_catalog_does_load_talib_control() -> None:
    """Control: the check above is not vacuously green (``catalog`` genuinely needs TA-Lib)."""
    assert _fresh_interpreter_has_talib("trading_bot.domain.indicators.catalog") is True


# --- set_compatibility(1) no-op canary (AC12 last bullet, Design 9) --------------------------


def test_set_compatibility_is_a_no_op_canary() -> None:
    """``set_compatibility`` must stay a no-op, checked directly (not through output equality).

    TA-Lib C 0.8.1 removed the MetaStock compatibility mode: upstream ``include/ta_func.h``
    documents ``TA_SetCompatibility``/``TA_GetCompatibility`` as deprecated no-ops, so
    ``get_compatibility()`` reads back ``0`` immediately after ``set_compatibility(1)`` (spec
    005, Design 9). Comparing indicator outputs under the setting cannot be this canary: while
    the setter is a no-op, such a comparison can never fail regardless of what
    ``set_compatibility`` actually does. If this assertion ever fails, a TA-Lib upgrade brought
    the setting back and spec 005 Design 9 (and this whole guard) must be revisited.
    """
    talib.set_compatibility(1)
    try:
        assert talib.get_compatibility() == 0
    finally:
        talib.set_compatibility(0)

    assert talib.get_compatibility() == 0


def test_set_compatibility_secondary_check_outputs_stay_identical() -> None:
    """Secondary, non-canary sanity check: cheap, one indicator, one small frame.

    This only documents that toggling the (no-op) setting around a real ``compute`` call does
    not crash or otherwise disturb the result; it is not evidence that ``set_compatibility`` has
    no effect (see the no-op canary above for that).
    """
    frame = synthetic_candles(60, seed=41, scenario=Scenario.MIXED)
    before = REGISTRY.compute("macd", {}, frame)["macd"].to_numpy().tobytes()

    talib.set_compatibility(1)
    try:
        after = REGISTRY.compute("macd", {}, frame)["macd"].to_numpy().tobytes()
    finally:
        talib.set_compatibility(0)

    assert after == before
