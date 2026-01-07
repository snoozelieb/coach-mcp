"""
Daily Loop - Morning audit automation script.

This script orchestrates the morning training review:
1. INGEST - Pull fresh data from Garmin
2. AUDIT - Compare actual vs planned activities
3. REASON - LLM analyzes context and makes decisions
4. PLAN - LLM generates/adjusts the 7-day plan
5. NOTIFY - Send morning brief to user

Run via Task Scheduler at 05:00 daily.
"""
import os
import json
import logging
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('daily_loop.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Import MCP server functions directly (for standalone mode)
# In full MCP mode, these would be called via the LLM
from server import (
    get_planning_context,
    get_weekly_plan,
    update_weekly_plan,
    get_compliance_report,
    get_daily_metrics,
    get_activities_range,
)
from planner import (
    get_current_plan,
    save_weekly_plan,
    create_empty_week_template,
    load_json_file,
    save_json_file,
)

# Coach system prompt for LLM
COACH_SYSTEM_PROMPT = """You are a direct, evidence-based training coach.

ATHLETE CONTEXT:
- Location: South Africa (SAST timezone, UTC+2)
- Sports: Running, Cycling (MTB), Ultimate Frisbee, Padel
- Injury-prone: Must track Mobility + Strength pillars

RACE CALENDAR (CRITICAL):
- ALWAYS call list_races() at the start of every planning session
- This returns the current race calendar with priorities (A/B/C) and days_until
- A-race = peak performance target, structure entire training block around it
- B-races = important but not peak, use as training stimulus or prep races
- C-races = participate but don't taper, treat as hard training days
- Periodization decisions MUST reference the race calendar, not assumptions

Your approach:
- Direct and honest - give clear feedback
- Evidence-based - cite the data behind recommendations
- Supportive - care about the athlete's goals
- Action-oriented - every message should guide the next step

CRITICAL PLANNING RULES:
1. ALWAYS check today's activities BEFORE generating a plan
   - Use get_activities_range(today, today) to see what's already done
   - Mark completed activities as 'actual' in today's plan entry
   - Only suggest REMAINING sessions for today, not ones already done
2. If athlete has already trained today, acknowledge it
3. Ask about planned sessions (e.g., "planning strength this evening?") before assuming
4. The plan starts from NOW, not from midnight

Your job is to:
1. Call list_races() to get current race calendar with days_until each event
2. Call get_planning_context() - this includes a_race_requirements with key sessions
3. Review the athlete's training data (including TODAY's activities)
4. Check compliance against their pillars (Strength 2x, Mobility 90min, Long Effort 1x)
5. Identify recovery status and any safety concerns
6. Generate or adjust the 7-day plan (respecting what's already done today)
7. Deliver a concise morning brief referencing upcoming races

7-DAY PLAN CONSTRUCTION (use this framework):
1. Check a_race_requirements.key_sessions - these are priority sessions for the A-race
2. Check a_race_requirements.current_phase_guidance - this tells you the focus for current phase
3. Schedule sessions by priority:
   - "critical" sessions MUST appear each week (e.g., long_trail_run, strength)
   - "high" priority sessions should appear when scheduled (e.g., back_to_back monthly)
   - "medium" priority fills remaining slots
4. Apply pillars: Strength 2x, Mobility 90min, Long Effort 1x per week
5. Apply constraints: max 2 consecutive hard days, 10% volume increase limit
6. Factor recovery: reduce intensity/volume on LOW recovery days
7. Place long effort on weekend, strength mid-week, rest day before/after hard blocks

You have access to these tools:
- get_planning_context(): Full context including a_race_requirements, compliance, recovery
- get_activities_range(start, end): Get activities for date range
- get_weekly_plan(): Current 7-day plan
- update_weekly_plan(plan_json): Save a new/updated plan
- get_compliance_report(): Weekly pillar compliance status
- list_races(): See upcoming events with days_until
- research_race(name): Get race details for training context
- propose_suggestion(type, description, rationale, change): Suggest config changes
- list_pending_suggestions(): See pending suggestions

When you identify patterns that warrant configuration changes, use propose_suggestion
to suggest changes - but NEVER make changes without user approval.

Keep responses concise. The athlete wants actionable information, not essays."""


def run_morning_audit() -> dict[str, Any]:
    """
    Execute the morning audit loop.

    Returns a summary of the audit results.
    """
    logger.info("=" * 50)
    logger.info("Starting morning audit")
    logger.info("=" * 50)

    today = date.today()
    results = {
        'date': today.isoformat(),
        'status': 'success',
        'steps': {},
    }

    try:
        # Step 1: INGEST - Pull fresh data
        logger.info("Step 1: INGEST - Pulling fresh data")
        daily_metrics = get_daily_metrics()
        results['steps']['ingest'] = {
            'status': 'complete',
            'daily_metrics': daily_metrics,
        }
        logger.info(f"Daily metrics: {daily_metrics}")

        # Step 2: AUDIT - Compare actual vs planned
        logger.info("Step 2: AUDIT - Comparing actual vs planned")
        audit_result = audit_yesterday(today)
        results['steps']['audit'] = audit_result
        logger.info(f"Audit result: {audit_result}")

        # Step 3: Get compliance report
        logger.info("Step 3: Checking compliance")
        compliance_json = get_compliance_report(days=7)
        compliance = json.loads(compliance_json)
        results['steps']['compliance'] = compliance
        logger.info(f"Compliance: {json.dumps(compliance, indent=2)}")

        # Step 4: Build context for LLM
        logger.info("Step 4: Building planning context")
        context_json = get_planning_context()
        context = json.loads(context_json)
        results['steps']['context'] = {'status': 'built', 'keys': list(context.keys())}

        # Step 5: Generate morning brief
        logger.info("Step 5: Generating morning brief")
        brief = generate_morning_brief(context, compliance, audit_result)
        results['steps']['brief'] = brief
        results['morning_brief'] = brief

        logger.info("=" * 50)
        logger.info("Morning audit complete")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"Morning audit failed: {str(e)}")
        results['status'] = 'error'
        results['error'] = str(e)

    return results


def audit_yesterday(today: date) -> dict[str, Any]:
    """
    Audit yesterday's activities against the plan.

    Marks planned sessions as completed, missed, or modified.
    """
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.isoformat()

    # Get the current plan
    plan = get_current_plan()
    if not plan or 'days' not in plan:
        return {'status': 'no_plan', 'message': 'No weekly plan found'}

    # Check if yesterday is in the plan
    if yesterday_str not in plan['days']:
        return {'status': 'not_in_plan', 'date': yesterday_str}

    yesterday_plan = plan['days'][yesterday_str]
    planned = yesterday_plan.get('planned')

    # Get yesterday's actual activities
    activities_json = get_activities_range(yesterday_str, yesterday_str)
    actual_activities = json.loads(activities_json)

    # Determine status
    if not planned:
        # Rest day planned
        if actual_activities:
            status = 'bonus'  # Did activity on rest day
            message = f"Rest day but completed {len(actual_activities)} activity(s)"
        else:
            status = 'rest_taken'
            message = "Rest day taken as planned"
    else:
        # Activity planned
        if not actual_activities:
            status = 'missed'
            message = f"Planned {planned.get('type', 'session')} was missed"
        else:
            # Compare planned vs actual
            planned_type = planned.get('type', '').lower()
            actual_types = [a.get('type', '').lower() for a in actual_activities]

            if planned_type in actual_types:
                status = 'completed'
                message = f"Completed planned {planned_type}"
            else:
                status = 'substituted'
                message = f"Planned {planned_type}, did {', '.join(actual_types)}"

    # Update the plan with actual result
    yesterday_plan['actual'] = actual_activities
    yesterday_plan['status'] = status

    # Save updated plan
    save_weekly_plan(plan)

    return {
        'status': status,
        'message': message,
        'date': yesterday_str,
        'planned': planned,
        'actual_count': len(actual_activities),
    }


def generate_morning_brief(
    context: dict[str, Any],
    compliance: dict[str, Any],
    audit: dict[str, Any]
) -> str:
    """
    Generate a concise morning brief without LLM.

    This is a fallback - in full mode, the LLM generates this.
    """
    today = date.today()
    day_name = today.strftime('%A')

    lines = [
        f"## Morning Brief - {day_name}, {today.isoformat()}",
        "",
    ]

    # Recovery status
    recovery = context.get('recovery', {})
    rhr = recovery.get('rhr', 'N/A')
    bb = recovery.get('body_battery', 'N/A')
    readiness = recovery.get('score', 'N/A')
    level = recovery.get('level', 'N/A')

    lines.append(f"**Recovery:** RHR={rhr} | BB={bb} | Readiness={readiness} ({level})")
    lines.append("")

    # Yesterday's audit
    if audit.get('status') == 'completed':
        lines.append(f"**Yesterday:** {audit.get('message')}")
    elif audit.get('status') == 'missed':
        lines.append(f"**Yesterday:** {audit.get('message')}")
    elif audit.get('status') == 'substituted':
        lines.append(f"**Yesterday:** {audit.get('message')}")
    lines.append("")

    # Compliance status
    comp = compliance.get('compliance', {})
    deficits = comp.get('deficits', [])

    if deficits:
        lines.append(f"**Deficits:** {', '.join(deficits)}")
        for d in deficits:
            if d in comp:
                info = comp[d]
                lines.append(f"  - {d}: {info.get('completed', 0)}/{info.get('required', 0)}")
    else:
        lines.append("**Pillars:** All compliant")
    lines.append("")

    # Today's plan
    plan = get_current_plan()
    today_str = today.isoformat()
    if plan and 'days' in plan and today_str in plan['days']:
        today_plan = plan['days'][today_str].get('planned')
        if today_plan:
            lines.append(f"**Today:** {today_plan.get('description', today_plan.get('type', 'Session'))}")
            if today_plan.get('duration_mins'):
                lines.append(f"  Duration: {today_plan['duration_mins']} mins")
        else:
            lines.append("**Today:** Rest day")
    else:
        lines.append("**Today:** No plan set - generate one!")
    lines.append("")

    # Upcoming events
    events = context.get('upcoming_events', [])
    if events:
        next_event = events[0]
        lines.append(f"**Next Event:** {next_event.get('name')} in {next_event.get('days_until')} days")

    # Safety warnings
    safety = compliance.get('safety', {})
    warnings = safety.get('warnings', [])
    if warnings:
        lines.append("")
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"  - {w}")

    return "\n".join(lines)


def run_with_llm():
    """
    Run the full loop with LLM integration.

    This version uses Claude to analyze context and make planning decisions.
    Requires ANTHROPIC_API_KEY in environment.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.error("anthropic package not installed. Run: pip install anthropic")
        return

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set in environment")
        return

    client = Anthropic()

    # Get planning context
    context_json = get_planning_context()
    compliance_json = get_compliance_report(days=7)
    plan_json = get_weekly_plan()

    # Build prompt for LLM
    user_prompt = f"""Run the morning audit and generate my brief.

Here's the current context:
{context_json}

Compliance report:
{compliance_json}

Current plan:
{plan_json}

Please:
1. Analyze my recovery status
2. Review yesterday's activity vs plan
3. Check pillar compliance and note any deficits
4. If the plan needs adjustment, generate an updated plan
5. Give me a concise morning brief with today's focus

Be direct - I want actionable information, not essays."""

    logger.info("Calling Claude API...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=COACH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    brief = response.content[0].text
    logger.info("Morning brief generated by LLM:")
    print("\n" + brief)

    return brief


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--llm':
        # Full LLM mode
        run_with_llm()
    else:
        # Standalone mode (no LLM)
        results = run_morning_audit()
        print("\n" + results.get('morning_brief', 'No brief generated'))
