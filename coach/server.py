"""AI Training Coach MCP Server - Orchestrator.

Imports all tool modules to register @mcp.tool() decorators,
prompt templates, and resources, then runs the MCP server.
All tool implementations live in coach/tools/.

Entry points:
    garmin-coach-mcp      Console script (pip/uvx install; coach-mcp is an alias) -> main()
    python server.py      Repo-checkout shim at the project root -> main()

Environment variables:
    COACH_DATA_DIR        Data directory override (default: git checkout
                          data/ dir, else a per-user data dir)
    COACH_TOKEN_DIR       Garmin token store override
    COACH_CODE_MODE=1     Enable Code Mode (search/execute meta-tools).
                          Requires the Monty sandbox:
                          pip install "garmin-coach-mcp[code-mode]"
    COACH_TRANSPORT=http  Transport: "stdio" (default), "http", "streamable-http"
                          ("sse" is legacy — HTTP+SSE was deprecated by MCP spec 2026-07-28)
    FASTMCP_HOST=0.0.0.0 HTTP host (default: 127.0.0.1)
    FASTMCP_PORT=8000     HTTP port (default: 8000)

The FastMCP startup banner is disabled: it would query PyPI for a newer
FastMCP release on every start, and this server makes no network calls
besides Garmin Connect and the explicit research tools.
"""
import os
import sys

from coach.mcp_app import mcp
from coach.parsers import check_setup

# Import tool modules to register all @mcp.tool() decorators
import coach.tools.data_tools
import coach.tools.fitness_tools
import coach.tools.athlete_tools
import coach.tools.planning_tools
import coach.tools.race_tools
import coach.tools.coaching_tools
import coach.tools.injury_tools
import coach.tools.strength_tools
import coach.tools.research_tools
import coach.tools.decision_tools
import coach.tools.interactive_tools

# Import prompt templates and resources
import coach.prompts
import coach.resources

# Preload the modules the code above reaches only through function-local
# (cycle-breaking) imports. A long-running server must never import a
# coach module for the first time inside a tool call: a code update under
# a running process would load the new file against old siblings
# (2026-10-27: push_plan_to_garmin died with ImportError on the first lazy
# import of coach.workout_builder). tests/test_imports.py pins the set.
import coach.storage  # noqa: F401 — fitness/rules/planner import it lazily

CODE_MODE_INSTALL_HINT = 'pip install "garmin-coach-mcp[code-mode]"'


def _enable_code_mode() -> bool:
    """Enable Code Mode (search/execute meta-tools) when its sandbox is present.

    FastMCP imports the Monty sandbox (``pydantic_monty``) lazily, at execute
    time. Without the probe below, a missing sandbox would surface only on the
    first call — after the execute tool had already been advertised to the
    client. Probe up front and never advertise a tool that cannot run.
    """
    import importlib.util
    import logging

    log = logging.getLogger(__name__)
    if importlib.util.find_spec("pydantic_monty") is None:
        log.warning(
            "COACH_CODE_MODE requested but the Monty sandbox is not installed; "
            "Code Mode stays off. Install with: %s", CODE_MODE_INSTALL_HINT,
        )
        return False
    try:
        from fastmcp.experimental.transforms.code_mode import (
            CodeMode, MontySandboxProvider, Search, GetSchemas, GetTags,
        )
    except ImportError:
        log.warning(
            "COACH_CODE_MODE requested but this FastMCP build has no Code Mode "
            "transform; Code Mode stays off. Install with: %s",
            CODE_MODE_INSTALL_HINT,
        )
        return False

    code_mode = CodeMode(
        sandbox_provider=MontySandboxProvider(),
        discovery_tools=[
            Search(name="search_tools", default_detail="brief"),
            GetSchemas(name="get_tool_schemas", default_detail="detailed"),
            GetTags(name="list_tool_tags", default_detail="brief"),
        ],
        execute_tool_name="execute_coaching_tools",
        execute_description=(
            "Execute Python code that chains coaching tool calls. "
            "Use call_tool(name, params) to invoke any coaching tool."
        ),
    )
    mcp.add_transform(code_mode)
    return True


def main() -> None:
    """Console entry point: bootstrap data dir, configure transport, run."""
    if not check_setup():
        sys.exit(1)

    if os.environ.get("COACH_CODE_MODE", "").strip() in ("1", "true", "yes"):
        _enable_code_mode()

    transport = os.environ.get("COACH_TRANSPORT", "stdio").strip().lower()
    mcp.run(transport=transport, show_banner=False)


if __name__ == "__main__":
    main()
