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
# gets fixed, the test passes and the entry can be removed). Currently empty:
# the Phase 1 Garmin auth rebuild removed the last broken module
# (coach.playwright_auth, which imported the dropped garth library).
KNOWN_BROKEN: dict[str, str] = {}


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


# Modules that are console-script entry points of their own and are never part
# of the running server. Everything else under coach/ must be loaded by the
# time coach.server has been imported.
STARTUP_EXEMPT: dict[str, str] = {
    "coach.garmin_login": "garmin-coach-login console script; never imported by the server",
    "coach.setup_wizard": "garmin-coach-setup console script; never imported by the server",
}


def test_server_startup_loads_every_coach_module(tmp_path):
    """The running server must never import a coach module for the first time
    inside a tool call.

    Function-local imports (``from . import storage`` to break a cycle) are
    fine only while the module is already in sys.modules. Otherwise a code
    update underneath a long-running server process loads the NEW file
    against OLD siblings: on 2026-10-27 push_plan_to_garmin crashed with
    ``ImportError: cannot import name 'sanitize_text' from 'coach.web_utils'``
    because coach.workout_builder was first imported at push time, hours after
    the process had started. A fresh interpreter imports the real entry point
    (coach.server, what server.py and the console scripts run) and reports
    what is loaded; every module on disk must be in that set.
    """
    import json
    import os
    import subprocess
    import sys

    code = (
        "import json, sys\n"
        "import coach.server\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m == 'coach' or m.startswith('coach.'))))\n"
    )
    env = {
        **os.environ,
        "COACH_DATA_DIR": str(tmp_path / "data"),
        "COACH_TOKEN_DIR": str(tmp_path / "tokens"),
        "FASTMCP_MCP_CAMELCASE_COMPAT": "false",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(COACH_DIR.parent), env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))

    on_disk = set(_discover_coach_modules())
    stale_exemptions = sorted(set(STARTUP_EXEMPT) - on_disk)
    assert not stale_exemptions, f"STARTUP_EXEMPT names modules that no longer exist: {stale_exemptions}"

    missing = sorted(on_disk - set(STARTUP_EXEMPT) - loaded)
    assert not missing, (
        "These coach modules are not imported when the server starts, so a tool "
        "call would import them lazily from disk — after a code update that mixes "
        f"old and new modules in one process: {missing}. Import them at module "
        "level, or preload them in coach/server.py."
    )
