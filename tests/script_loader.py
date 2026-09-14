"""Shared helpers: load standalone scripts (not part of the package) as modules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(relative_path: str, name: str) -> ModuleType:
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
