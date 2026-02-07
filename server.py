"""AI Training Coach MCP Server - Orchestrator.

Imports all tool modules to register @mcp.tool() decorators,
then runs the MCP server. All tool implementations live in tools/.
"""
from mcp_app import mcp
from parsers import check_setup

# Import tool modules to register all @mcp.tool() decorators
import tools.data_tools
import tools.fitness_tools
import tools.athlete_tools
import tools.planning_tools
import tools.suggestion_tools
import tools.race_tools
import tools.coaching_tools
import tools.injury_tools
import tools.strength_tools
import tools.research_tools
import tools.decision_tools
import tools.goal_tools


if __name__ == "__main__":
    if check_setup():
        mcp.run()
    else:
        import sys
        sys.exit(1)
