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

# Re-exports for backward compatibility (tests, daily_loop.py)
from parsers import (  # noqa: F401
    parse_resting_heart_rate,
    parse_sleep_score,
    parse_body_battery,
    parse_activity,
    parse_activities,
    parse_training_readiness,
    parse_personal_records,
    calculate_baseline,
)
from config import DATA_DIR  # noqa: F401
from tools.data_tools import get_daily_metrics, get_activities_range, get_personal_records  # noqa: F401
from tools.fitness_tools import get_training_readiness, refresh_athlete_baseline  # noqa: F401
from tools.planning_tools import get_planning_context  # noqa: F401
from tools.coaching_tools import get_compliance_report  # noqa: F401


if __name__ == "__main__":
    if check_setup():
        mcp.run()
    else:
        import sys
        sys.exit(1)
