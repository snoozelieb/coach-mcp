"""Import-everything smoke test.

Catches dependency breaks at import time — e.g. a library dropped from
requirements, a renamed module, or a broken top-level import — without
needing any Garmin credentials, data files, or network access.

Module names are discovered from the filesystem (no imports during
collection), so a module that fails to import shows up as exactly one
failed parametrized test naming the broken module.
"""
import importlib
from pathlib import Path

import pytest

COACH_DIR = Path(__file__).parent.parent / "coach"

# Modules with a KNOWN import failure today. Each maps to the reason it is
# skipped (only skipped when the import actually fails — if the dependency
# gets fixed, the test passes and the entry can be removed).
#
# coach.playwright_auth: imports garth at module level, but garminconnect
# 0.3.2 dropped garth and it is no longer installed. The whole auth fallback
# stack is slated for replacement in the Phase 1 Garmin auth rebuild
# (see docs/UPGRADE_ROADMAP.md, Phase 1.1) — until then we skip rather than
# fail so this file still guards every other module.
KNOWN_BROKEN = {
    "coach.playwright_auth": (
        "module-level garth import fails (garth not installed; dropped by "
        "garminconnect 0.3.2) — pending Phase 1 auth rebuild"
    ),
}


def _discover_coach_modules() -> list[str]:
    """Walk coach/ on disk and return importable module names (coach.*)."""
    modules = []
    for py_file in sorted(COACH_DIR.rglob("*.py")):
        rel = py_file.relative_to(COACH_DIR.parent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    return modules


@pytest.mark.parametrize("module_name", _discover_coach_modules())
def test_coach_module_imports(module_name):
    """Every coach.* module must import cleanly (known breaks skip loudly)."""
    if module_name in KNOWN_BROKEN:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            pytest.skip(f"{module_name}: {KNOWN_BROKEN[module_name]} ({exc})")
    else:
        importlib.import_module(module_name)


def test_prompts_module_imports():
    """coach.prompts registers MCP prompt templates at import time."""
    importlib.import_module("coach.prompts")


def test_resources_module_imports():
    """coach.resources registers MCP resources at import time."""
    importlib.import_module("coach.resources")


def test_server_imports_cleanly():
    """server.py must import without starting the MCP loop.

    The run logic is __main__-guarded, so importing it only registers the
    tool/prompt/resource modules. This is the closest cheap proxy to "the
    server starts" and catches breaks in any transitively imported module.
    """
    module = importlib.import_module("server")
    # Sanity: the shared FastMCP app must be reachable through the entry point.
    assert hasattr(module, "mcp")
