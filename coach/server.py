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
                          Requires: pip install fastmcp[code-mode]
    COACH_TRANSPORT=http  Transport: "stdio" (default), "http", "streamable-http"
                          ("sse" is legacy — HTTP+SSE was deprecated by MCP spec 2026-07-28)
    FASTMCP_HOST=0.0.0.0 HTTP host (default: 127.0.0.1)
    FASTMCP_PORT=8000     HTTP port (default: 5000)
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


def _enable_code_mode():
    """Enable Code Mode transform for tool discovery via search + execute."""
    try:
        from fastmcp.experimental.transforms.code_mode import (
            CodeMode, MontySandboxProvider, Search, GetSchemas, GetTags,
        )
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
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "Code Mode requested but fastmcp[code-mode] not installed. "
            "Install with: pip install fastmcp[code-mode]"
        )
        return False


def main() -> None:
    """Console entry point: bootstrap data dir, configure transport, run."""
    if not check_setup():
        sys.exit(1)

    if os.environ.get("COACH_CODE_MODE", "").strip() in ("1", "true", "yes"):
        _enable_code_mode()

    transport = os.environ.get("COACH_TRANSPORT", "stdio").strip().lower()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
