"""
Daily Loop - Morning audit automation script.

This script orchestrates the morning training review:
1. INGEST - Pull fresh data from Garmin
2. AUDIT - Compare actual vs planned activities
3. BRIEF - Generate morning brief with recovery status and today's plan

Modes:
  python scripts/daily_loop.py          # Template-based brief (no LLM)
  python scripts/daily_loop.py --llm    # LLM-powered brief via the Anthropic API

Run via Task Scheduler at 05:00 daily.
"""
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Add project root to path so we can import tool modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging. The FileHandler is best-effort: if a scheduler wrapper
# redirects output into daily_loop.log itself (cmd's >> holds the file with no
# write sharing on Windows), opening it here raises PermissionError — log to
# console only in that case rather than crashing before the run starts.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.insert(0, logging.FileHandler(str(Path(__file__).resolve().parent.parent / 'daily_loop.log')))
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_handlers,
)
logger = logging.getLogger(__name__)

# Model for --llm brief generation. Override via the COACH_LLM_MODEL env var.
DEFAULT_LLM_MODEL = 'claude-sonnet-4-6'

# Import tool functions directly from their modules
from coach.tools.coaching_tools import get_compliance_report, get_coaching_snapshot
from coach.tools.data_tools import get_activities_range
from coach.tools.fitness_tools import query_metrics
from coach.planner import (
    get_current_plan,
    save_weekly_plan,
)


class _NullContext:
    """No-op context for calling async tools outside MCP.

    Implements all Context methods that tools might call so that
    adding new ctx.info()/ctx.warning() calls in tools won't break
    this standalone script.
    """
    async def report_progress(self, *args, **kwargs):
        pass

    async def info(self, *args, **kwargs):
        pass

    async def debug(self, *args, **kwargs):
        pass

    async def warning(self, *args, **kwargs):
        pass

    async def error(self, *args, **kwargs):
        pass

    async def log(self, *args, **kwargs):
        pass


def _normalize_planned(raw: Any) -> list[dict[str, Any]]:
    """Normalize a plan day's 'planned' field to a list of session dicts.

    Live plan data contains both shapes: a single session dict (older plans)
    and a list of session dicts (newer plans). Every read of planned sessions
    in this script goes through this helper so both shapes are handled.
    """
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


async def run_morning_audit(use_llm: bool = False) -> dict[str, Any]:
    """
    Execute the morning audit loop.

    Args:
        use_llm: If True, use MCP sampling for LLM-powered brief generation.

    Returns a summary of the audit results.
    """
    logger.info("=" * 50)
    logger.info("Starting morning audit")
    logger.info("=" * 50)

    today = date.today()
    ctx = _NullContext()
    results = {
        'date': today.isoformat(),
        'status': 'success',
        'steps': {},
    }

    try:
        # Step 1: INGEST - Pull fresh data
        logger.info("Step 1: INGEST - Pulling fresh data")
        daily_metrics = query_metrics(kind='daily')
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

        # Step 4: Build context for LLM (canonical snapshot).
        # The default snapshot is the compact core payload — request the
        # recovery + plan sections explicitly so the brief keeps readiness
        # detail and today's full plan (core alone degrades recovery to {}).
        logger.info("Step 4: Building coaching snapshot")
        context_json = await get_coaching_snapshot(
            ctx, sections=['core', 'recovery', 'plan'])
        context = json.loads(context_json)
        # upcoming_events moved out of the snapshot core — backfill from the
        # compliance report so the template brief keeps the next-event line.
        context.setdefault('upcoming_events', compliance.get('upcoming_events', []))
        results['steps']['context'] = {'status': 'built', 'keys': list(context.keys())}

        # Step 5: Generate morning brief
        logger.info("Step 5: Generating morning brief")
        if use_llm:
            logger.info("Using LLM-powered brief generation")
            brief = await _generate_llm_brief(context, compliance, audit_result)
        else:
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


async def _generate_llm_brief(
    context: dict[str, Any],
    compliance: dict[str, Any],
    audit: dict[str, Any],
) -> str:
    """Generate a morning brief using the Anthropic API directly.

    This runs outside an MCP session, so we use the Anthropic SDK
    rather than MCP sampling (which requires a connected client).
    """
    import os
    try:
        import anthropic
    except ImportError:
        logger.warning(
            "--llm requested but the anthropic SDK is not installed. "
            "Install it with: pip install \"anthropic>=0.109\" "
            "(listed in requirements.txt). Falling back to template brief."
        )
        return generate_morning_brief(context, compliance, audit)

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — falling back to template brief")
        return generate_morning_brief(context, compliance, audit)

    today = date.today()
    day_name = today.strftime('%A')

    # Build a compact data payload for the LLM
    data = {
        'date': today.isoformat(),
        'day': day_name,
        'recovery': context.get('recovery', {}),
        'today_plan': None,
        'yesterday_audit': {
            'status': audit.get('status'),
            'message': audit.get('message'),
        },
        'compliance_deficits': compliance.get('compliance', {}).get('deficits', []),
        'upcoming_events': context.get('upcoming_events', [])[:2],
        'safety_warnings': compliance.get('safety', {}).get('warnings', []),
    }

    # Get today's plan
    plan = get_current_plan()
    today_str = today.isoformat()
    if plan and 'days' in plan and today_str in plan['days']:
        today_sessions = _normalize_planned(plan['days'][today_str].get('planned'))
        data['today_plan'] = today_sessions or None

    system_prompt = (
        "You are an expert adaptive training coach generating a concise morning brief. "
        "Be direct: 'You need rest' not 'Maybe consider taking it easy.' "
        "Output markdown. Keep it under 200 words. "
        "Sections: Yesterday, Today's Session, Recovery Check, Key Focus."
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=os.environ.get('COACH_LLM_MODEL', DEFAULT_LLM_MODEL),
            max_tokens=500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"Generate morning brief from this data:\n```json\n{json.dumps(data, indent=2)}\n```"
            }],
        )
        return response.content[0].text
    except Exception as e:
        logger.warning(f"LLM brief generation failed: {e} — falling back to template")
        return generate_morning_brief(context, compliance, audit)


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
    planned_sessions = _normalize_planned(yesterday_plan.get('planned'))
    non_rest = [s for s in planned_sessions if 'rest' not in str(s.get('type', '')).lower()]

    # Get yesterday's actual activities
    activities_json = get_activities_range(yesterday_str, yesterday_str)
    actual_activities = json.loads(activities_json)

    # Determine status
    if not non_rest:
        # Rest day planned
        if actual_activities:
            status = 'bonus'  # Did activity on rest day
            message = f"Rest day but completed {len(actual_activities)} activity(s)"
        else:
            status = 'rest_taken'
            message = "Rest day taken as planned"
    else:
        planned_types = [s.get('type', 'session') for s in non_rest]
        actual_types = [a.get('type', '').lower() for a in actual_activities]

        if not actual_activities:
            status = 'missed'
            message = f"Planned {' + '.join(planned_types)} was missed"
        else:
            matched = [t for t in planned_types if t.lower() in actual_types]
            unmatched_planned = [t for t in planned_types if t.lower() not in actual_types]

            if len(matched) == len(planned_types):
                status = 'completed'
                message = f"Completed planned {' + '.join(planned_types)}"
            elif matched:
                status = 'partial'
                message = (
                    f"Completed {' + '.join(matched)}; "
                    f"missed {' + '.join(unmatched_planned)}"
                )
            else:
                status = 'substituted'
                message = f"Planned {' + '.join(planned_types)}, did {', '.join(actual_types)}"

    # Update the plan with actual result
    yesterday_plan['actual'] = actual_activities
    yesterday_plan['status'] = status

    # Save updated plan
    save_weekly_plan(plan)

    return {
        'status': status,
        'message': message,
        'date': yesterday_str,
        'planned': planned_sessions,
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
        today_sessions = _normalize_planned(plan['days'][today_str].get('planned'))
        if today_sessions:
            descriptions = []
            for session in today_sessions:
                desc = session.get('description', session.get('type', 'Session'))
                if session.get('duration_mins'):
                    desc = f"{desc} ({session['duration_mins']} mins)"
                descriptions.append(desc)
            lines.append(f"**Today:** {' + '.join(descriptions)}")
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


if __name__ == "__main__":
    use_llm = '--llm' in sys.argv
    results = asyncio.run(run_morning_audit(use_llm=use_llm))
    print("\n" + results.get('morning_brief', 'No brief generated'))
