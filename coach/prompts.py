"""MCP Prompt templates for coaching workflows.

Registers reusable prompt templates that MCP clients can invoke
for structured coaching interactions.
"""

from .mcp_app import mcp
from .planner import load_athlete, load_methodology, get_current_plan, load_coaching_log
from .rules import load_training_config, get_upcoming_events
from .config import DATA_DIR, TRAINING_CONFIG_FILE
from fastmcp.prompts import PromptResult, Message
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


@mcp.prompt(
    name="weekly_planning",
    description="Structured prompt for building next week's training plan. "
    "Gathers athlete context, current phase, and constraints.",
)
def weekly_planning_prompt(notes: str = "") -> PromptResult:
    """Build next week's training plan with full coaching context."""
    athlete = load_athlete()
    plan = get_current_plan()
    training_config = load_training_config()
    methodology = load_methodology()

    today = date.today()
    next_monday = today + timedelta(days=(7 - today.weekday()))
    events = get_upcoming_events()

    # Build context summary
    personal = athlete.get('personal', {})
    pillars = athlete.get('training_pillars', {})
    constraints = athlete.get('life_constraints', {})
    injuries = [
        i for i in athlete.get('injury_history', [])
        if i.get('status') in ('active', 'improving')
    ]
    current_block = training_config.get('current_block', {})

    context = {
        'athlete_name': personal.get('name', 'Athlete'),
        'planning_week': next_monday.isoformat(),
        'current_phase': current_block.get('phase', 'unknown'),
        'pillars': pillars,
        'blocked_days': constraints.get('blocked_days', []),
        'preferred_times': constraints.get('preferred_times', {}),
        'active_injuries': injuries,
        'upcoming_events': events[:3],
        'notes': notes,
    }

    return PromptResult(
        messages=[
            Message(
                f"Plan the training week starting {next_monday.isoformat()} for "
                f"{context['athlete_name']}.\n\n"
                f"## Context\n"
                f"```json\n{json.dumps(context, indent=2)}\n```\n\n"
                f"## Instructions\n"
                f"1. Call get_coaching_snapshot() FIRST. Check `current_time_context` "
                f"at the top — ground every planning decision in the current date, "
                f"day of week, and time_period.\n"
                f"2. Call get_week_constraints() for structured planning constraints\n"
                f"3. Check the load hierarchy (overall ACWR > sport ACWR > sport CTL)\n"
                f"4. Build the plan respecting blocked days and injury restrictions\n"
                f"5. Each session needs a PURPOSE (why this session, this day)\n"
                f"6. Use update_weekly_plan() to save\n"
                f"7. Log significant decisions with log_coaching_decision()"
            )
        ],
        description="Weekly planning prompt with athlete context",
    )


@mcp.prompt(
    name="morning_brief",
    description="Daily coaching check-in. Reviews yesterday, today's plan, "
    "and readiness. Use at the start of each day.",
)
def morning_brief_prompt() -> PromptResult:
    """Generate a morning coaching brief for today."""
    return PromptResult(
        messages=[
            Message(
                f"Generate a coaching brief grounded in the current time.\n\n"
                f"## Your task\n"
                f"1. Call get_coaching_snapshot() — the first key is "
                f"`current_time_context`. Greet the athlete appropriate to "
                f"the time_period ('Good morning' only if morning; otherwise "
                f"match the period honestly).\n"
                f"2. Check yesterday's planned vs actual — investigate any anomalies\n"
                f"3. Review today's planned session and readiness/recovery. If "
                f"time_period is evening or night and today's session is "
                f"already done, shift the brief toward recovery and tomorrow.\n"
                f"4. Check sleep data — is the athlete recovered enough for today's plan?\n"
                f"5. Adapt today's session if needed (readiness gate)\n\n"
                f"## Output format\n"
                f"- **Time-appropriate greeting** referencing day and time_period\n"
                f"- **Yesterday**: What happened vs what was planned (flag anomalies)\n"
                f"- **Today's session**: What's planned and why (note if already completed)\n"
                f"- **Readiness check**: Score, sleep, recovery — is today's plan appropriate?\n"
                f"- **Adjustments**: Any modifications needed based on current state\n"
                f"- **One thing to focus on**: The single most important thing for the rest of today"
            )
        ],
        description="Coaching brief grounded in current time",
    )


@mcp.prompt(
    name="injury_assessment",
    description="Structured injury assessment workflow. Guides through "
    "diagnosis, severity, and training modifications.",
)
def injury_assessment_prompt(body_region: str, description: str = "") -> PromptResult:
    """Assess an injury and determine training modifications."""
    return PromptResult(
        messages=[
            Message(
                f"The athlete is reporting an issue with their {body_region}.\n"
                f"{'Description: ' + description if description else ''}\n\n"
                f"## Assessment workflow\n"
                f"0. Call get_coaching_snapshot() first and check "
                f"`current_time_context` — a niggle reported at 5am is different "
                f"from one reported mid-training.\n"
                f"1. Ask clarifying questions about:\n"
                f"   - When it started (acute vs chronic)\n"
                f"   - Pain level (1-10) and type (sharp, dull, burning)\n"
                f"   - What makes it worse/better\n"
                f"   - Impact on daily activities\n"
                f"2. Call diagnose_injury() with the body region and symptoms\n"
                f"3. Call research_injury() for clinical evidence\n"
                f"4. Determine severity and training modifications\n"
                f"5. Update injury status with update_injury_status()\n"
                f"6. Modify the weekly plan if needed\n\n"
                f"## Key principles\n"
                f"- If in doubt about severity, be CONSERVATIVE\n"
                f"- Never prescribe through pain that could worsen injury\n"
                f"- Suggest professional evaluation for anything beyond mild\n"
                f"- Log the decision with log_coaching_decision()"
            )
        ],
        description="Injury assessment workflow",
    )


@mcp.prompt(
    name="week_review",
    description="End-of-week review and adaptation. Analyzes compliance, "
    "load response, and plans adjustments for next week.",
)
def week_review_prompt() -> PromptResult:
    """Review the training week and plan adaptations."""
    return PromptResult(
        messages=[
            Message(
                f"## End-of-week coaching review\n\n"
                f"1. Call get_coaching_snapshot() for current state. Verify "
                f"`current_time_context` — confirm it's actually end-of-week "
                f"(day_of_week Sunday/Monday, or the athlete's review day).\n"
                f"2. Call get_compliance_report() for detailed pillar analysis\n"
                f"3. Call get_intensity_distribution() for zone analysis\n\n"
                f"## Review checklist\n"
                f"- **Compliance**: What was hit, what was missed, and WHY?\n"
                f"- **Load response**: How did the athlete handle this week's load?\n"
                f"- **Sleep & recovery**: Trends over the week\n"
                f"- **Key sessions**: Were the important ones completed with quality?\n"
                f"- **Anomalies**: Anything unusual that needs discussion?\n\n"
                f"## Adaptation decisions\n"
                f"- Record athlete response: call record_athlete_response() with load/compliance data\n"
                f"- Should volume change next week? Check adaptation_patterns\n"
                f"- Any pillar adjustments needed?\n"
                f"- Log decisions with log_coaching_decision()\n\n"
                f"## Output\n"
                f"- Summary of the week (what went well, what didn't)\n"
                f"- Specific adaptation recommendations for next week\n"
                f"- One thing to celebrate, one thing to improve"
            )
        ],
        description="End-of-week review and adaptation prompt",
    )


@mcp.prompt(
    name="onboarding",
    description="New athlete onboarding conversation. Discovers goals, "
    "constraints, and history to prescribe a training approach.",
)
def onboarding_prompt() -> PromptResult:
    """Guide through new athlete onboarding."""
    return PromptResult(
        messages=[
            Message(
                f"## New athlete onboarding\n\n"
                f"You are onboarding a new athlete. Your job is to UNDERSTAND them "
                f"deeply, then PRESCRIBE their training approach with authority.\n\n"
                f"### Phase 0: Ground the conversation\n"
                f"Read the `coach://context/now` resource (or call "
                f"get_coaching_snapshot() and check `current_time_context`) so "
                f"your greeting, scheduling suggestions, and first-session "
                f"timing match the current day and time_period.\n\n"
                f"### Phase 1: Understand\n"
                f"Ask about (one area at a time, conversationally):\n"
                f"- Sports and training background\n"
                f"- Current fitness level and recent training\n"
                f"- Goals — what does success look like?\n"
                f"- Events or races they're targeting\n"
                f"- Injuries, limitations, health concerns\n"
                f"- Life constraints (work schedule, family, travel)\n"
                f"- Available training hours per week (be realistic)\n"
                f"- Equipment and facilities access\n\n"
                f"### Phase 2: Prescribe\n"
                f"Based on what you learned:\n"
                f"1. Call refresh_athlete_baseline() to pull Garmin data\n"
                f"2. Tell them what their training pillars will be and WHY\n"
                f"3. Use update_athlete() to save their profile\n"
                f"4. If they have races, use add_race() to register them\n"
                f"5. Build their first weekly plan\n\n"
                f"### Key principle\n"
                f"Don't offer a menu of options. You are the expert — "
                f"TELL them what they need based on evidence and experience."
            )
        ],
        description="New athlete onboarding prompt",
    )
