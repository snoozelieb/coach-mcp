"""AI Training Coach MCP Server - Orchestrator.

Imports all tool modules to register @mcp.tool() decorators,
then runs the MCP server. All tool implementations live in coach/tools/.
"""
from coach.mcp_app import mcp
from coach.parsers import check_setup

# Import tool modules to register all @mcp.tool() decorators
import coach.tools.data_tools
import coach.tools.fitness_tools
import coach.tools.athlete_tools
import coach.tools.planning_tools
import coach.tools.suggestion_tools
import coach.tools.race_tools
import coach.tools.coaching_tools
import coach.tools.injury_tools
import coach.tools.strength_tools
import coach.tools.research_tools
import coach.tools.decision_tools
import coach.tools.goal_tools


if __name__ == "__main__":
    if check_setup():
        mcp.run()
    else:
        import sys
        sys.exit(1)
