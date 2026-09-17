"""yfinance isolation (spec 011, T15, AC33; decisions D49 and D59).

``data/yahoo/client.py`` is the only module of ``src/`` that imports yfinance, and no module
imports an HTTP or socket library. An AST scan enforces it, exercised first on synthetic
sources so a green result is not just "the scanner never triggers". Fresh interpreters then
prove that the provider, its types, the transport and the resampler load without yfinance,
``curl_cffi``, ``peewee`` or ``requests``, with the factory as the control that does load
yfinance, and that the replay fixtures never load yfinance either.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import trading_bot

SRC_PACKAGE = Path(trading_bot.__file__).resolve().parent
REPO_ROOT = SRC_PACKAGE.parent.parent
SOURCE_FILES = sorted(SRC_PACKAGE.rglob("*.py"))
RECORDINGS_MODULE = REPO_ROOT / "tests" / "fixtures" / "yahoo_recordings.py"

YFINANCE_ALLOWED_IN = frozenset({"data/yahoo/client.py"})
NETWORK_MODULES = (
    "curl_cffi",
    "requests",
    "urllib3",
    "peewee",
    "httpx",
    "aiohttp",
    "websockets",
    "socket",
    "http.client",
    "urllib.request",
)
_REPORTED_MODULES = ("yfinance", "curl_cffi", "peewee", "requests")


def _relative(path: Path) -> str:
    return path.relative_to(SRC_PACKAGE).as_posix()


def imported_modules(tree: ast.Module) -> list[str]:
    """Every absolute module name imported by ``tree``, in source order."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def _matches(name: str, module: str) -> bool:
    return name == module or name.startswith(f"{module}.")


def isolation_violations(sources: dict[str, str]) -> list[str]:
    """``"<file>: <module>"`` for each forbidden import in ``sources`` (relative path to code)."""
    violations: list[str] = []
    for relative, source in sources.items():
        for name in imported_modules(ast.parse(source)):
            forbidden_yfinance = _matches(name, "yfinance") and relative not in YFINANCE_ALLOWED_IN
            network = any(_matches(name, module) for module in NETWORK_MODULES)
            if forbidden_yfinance or network:
                violations.append(f"{relative}: {name}")
    return violations


# --- Scanner self-tests ------------------------------------------------------------------------


def test_the_scanner_flags_import_yfinance_outside_the_client() -> None:
    assert isolation_violations({"data/yahoo/provider.py": "import yfinance\n"}) == [
        "data/yahoo/provider.py: yfinance"
    ]


def test_the_scanner_flags_a_from_import_of_a_yfinance_submodule_outside_the_client() -> None:
    source = "from yfinance.exceptions import YFRateLimitError\n"

    assert isolation_violations({"data/transport.py": source}) == [
        "data/transport.py: yfinance.exceptions"
    ]


def test_the_scanner_accepts_yfinance_in_the_client_only() -> None:
    source = "import yfinance\nfrom yfinance.exceptions import YFDataException\n"

    assert isolation_violations({"data/yahoo/client.py": source}) == []


def test_the_scanner_flags_curl_cffi_even_in_the_client() -> None:
    sources = {
        "data/yahoo/client.py": "import curl_cffi.requests\n",
        "data/yahoo/history.py": "import curl_cffi.requests\n",
    }

    assert isolation_violations(sources) == [
        "data/yahoo/client.py: curl_cffi.requests",
        "data/yahoo/history.py: curl_cffi.requests",
    ]


@pytest.mark.parametrize("module", NETWORK_MODULES)
def test_the_scanner_flags_every_network_module(module: str) -> None:
    assert isolation_violations({"main.py": f"import {module}\n"}) == [f"main.py: {module}"]


def test_the_scanner_does_not_treat_a_longer_name_as_a_forbidden_module() -> None:
    source = "import requests_toolbelt_like\nimport socketserver_like\nimport yfinancex\n"

    assert isolation_violations({"main.py": source}) == []


# --- The real source tree ----------------------------------------------------------------------


def test_the_source_tree_was_scanned() -> None:
    relative = {_relative(path) for path in SOURCE_FILES}

    assert {
        "data/transport.py",
        "data/yahoo/__init__.py",
        "data/yahoo/history.py",
        "data/yahoo/instruments.py",
        "data/yahoo/client.py",
        "data/yahoo/provider.py",
        "data/yahoo/factory.py",
        "domain/candle_resampling.py",
        "main.py",
    } <= relative


def test_yfinance_is_imported_only_by_the_client_and_no_module_imports_a_network_library() -> None:
    sources = {_relative(path): path.read_text(encoding="utf-8") for path in SOURCE_FILES}

    assert isolation_violations(sources) == []
    assert "yfinance" in imported_modules(ast.parse(sources["data/yahoo/client.py"]))


def test_the_replay_fixtures_do_not_import_yfinance() -> None:
    names = imported_modules(ast.parse(RECORDINGS_MODULE.read_text(encoding="utf-8")))

    assert [name for name in names if _matches(name, "yfinance")] == []


# --- Fresh interpreters ------------------------------------------------------------------------


def _fresh_interpreter_modules(module: str) -> dict[str, bool]:
    """Import ``module`` in a brand-new interpreter and report which libraries it loaded."""
    script = "\n".join(
        [
            f"import {module}",
            "import json",
            "import sys",
            f"print(json.dumps({{name: name in sys.modules for name in {_REPORTED_MODULES!r}}}))",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_PACKAGE.parent), str(REPO_ROOT)])
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise AssertionError(
            f"fresh interpreter import of {module} failed: {error.stderr}"
        ) from error
    report: dict[str, bool] = json.loads(completed.stdout)
    return report


@pytest.mark.parametrize(
    "module",
    [
        "trading_bot.data.yahoo.provider",
        "trading_bot.data.yahoo.instruments",
        "trading_bot.data.yahoo.history",
        "trading_bot.data.transport",
        "trading_bot.domain.candle_resampling",
        "tests.fixtures.yahoo_recordings",
    ],
)
def test_modules_without_yfinance_load_no_yahoo_library(module: str) -> None:
    report = _fresh_interpreter_modules(module)

    assert report == {"yfinance": False, "curl_cffi": False, "peewee": False, "requests": False}


def test_the_factory_loads_yfinance_so_the_fresh_interpreter_check_is_not_vacuous() -> None:
    report = _fresh_interpreter_modules("trading_bot.data.yahoo.factory")

    assert report["yfinance"] is True
