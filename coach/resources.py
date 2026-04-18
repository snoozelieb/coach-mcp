"""MCP Resources for exposing athlete data to clients.

Resources provide structured read-only data that clients can access
without calling tools. Useful for IDE integrations and dashboards.
"""

from .mcp_app import mcp
from .parsers import build_current_time_context
from .planner import load_athlete, get_current_plan, load_coaching_log
from .rules import load_training_config
from .config import DATA_DIR, ATHLETE_FILE, WEEKLY_PLAN_FILE, TRAINING_CONFIG_FILE
from datetime import date
import json
import logging

logger = logging.getLogger(__name__)


@mcp.resource(
    "coach://athlete/profile",
    name="athlete_profile",
    description="Current athlete profile: personal info, pillars, constraints, injuries",
    mime_type="application/json",
)
def athlete_profile_resource() -> str:
    """Full athlete profile as a resource."""
    try:
        athlete = load_athlete()
        return json.dumps(athlete, indent=2)
    except Exception as e:
        logger.exception("Failed to load athlete profile resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://plan/current",
    name="weekly_plan",
    description="Current 7-day rolling training plan with session PURPOSE for each day",
    mime_type="application/json",
)
def weekly_plan_resource() -> str:
    """Current weekly training plan as a resource."""
    try:
        plan = get_current_plan()
        if not plan:
            return json.dumps({"status": "no_plan", "message": "No weekly plan set"})
        return json.dumps(plan, indent=2)
    except Exception as e:
        logger.exception("Failed to load weekly plan resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://config/training",
    name="training_config",
    description="Training configuration: events, periodization, goals, current block",
    mime_type="application/json",
)
def training_config_resource() -> str:
    """Training configuration as a resource."""
    try:
        config = load_training_config()
        return json.dumps(config, indent=2)
    except Exception as e:
        logger.exception("Failed to load training config resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://context/now",
    name="current_time_context",
    description="Current date, day of week, hour, and time_period — mandatory time grounding before any coaching advice",
    mime_type="application/json",
)
def current_time_context_resource() -> str:
    """Current time/day context the coach must verify before recommending."""
    try:
        return json.dumps(build_current_time_context(), indent=2)
    except Exception as e:
        logger.exception("Failed to build current time context resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://coaching/decisions",
    name="coaching_decisions",
    description="Active coaching decisions and pending approvals from coaching memory",
    mime_type="application/json",
)
def coaching_decisions_resource() -> str:
    """Active coaching decisions as a resource."""
    try:
        log = load_coaching_log()
        active = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'active'
        ]
        pending = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'pending_approval'
        ]
        return json.dumps({
            "active_decisions": active[:10],
            "pending_approvals": pending,
            "total_decisions": len(log.get('decisions', [])),
        }, indent=2)
    except Exception as e:
        logger.exception("Failed to load coaching decisions resource")
        return json.dumps({"error": str(e)})
