"""
Daily Loop - Morning audit automation script.

This script orchestrates the morning training review:
1. INGEST - Pull fresh data from Garmin
2. AUDIT - Compare actual vs planned activities for yesterday, then re-check
   the last few days still marked 'missed' or 'unverified' so a late Garmin
   sync (or a Garmin outage) heals instead of staying a false miss
3. BRIEF - Print a template morning brief (recovery, yesterday, today)

Usage:
  python scripts/daily_loop.py

Run via Task Scheduler ("CoachMCP Morning Audit") at 06:30 daily. The exit
code is non-zero when yesterday could not be verified (Garmin unreachable,
login required) or the run failed, so the scheduler's history shows it.

The audit never writes fiction: when Garmin cannot be reached the day is
marked 'unverified' and no 'actual' activities are persisted.
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

from coach.config import DATA_DIR

# Configure logging. The log lives in the data directory, with the rest of
# the personal data. The FileHandler is best-effort: if a scheduler wrapper
# redirects output into the same file (cmd's >> holds the file with no write
# sharing on Windows), opening it raises PermissionError — log to the console
# only in that case rather than crashing before the run starts. Health
# metrics and compliance payloads are logged at DEBUG, so the INFO log
# carries step names and audit statuses only.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.insert(0, logging.FileHandler(str(Path(DATA_DIR) / 'daily_loop.log')))
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_handlers,
)
logger = logging.getLogger(__name__)

# Import tool functions directly from their modules
from coach.tools.coaching_tools import get_compliance_report, get_coaching_snapshot
from coach.tools.data_tools import get_activities_range
from coach.tools.fitness_tools import query_metrics
from coach.planner import (
    get_current_plan,
    save_weekly_plan,
)

# Days counted back from yesterday whose audit is re-checked on every run while
# it is still 'missed' or 'unverified'. A session that synced late, or a day
# audited while Garmin was down, heals instead of staying a false miss.
REAUDIT_WINDOW_DAYS = 7  # late syncs heal within the 9-day plan retention
REAUDIT_STATUSES = ('missed', 'unverified')


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


async def run_morning_audit() -> dict[str, Any]:
    """
    Execute the morning audit loop.

    Returns a summary of the audit results. `status` is 'success',
    'unverified' (yesterday could not be checked against Garmin) or 'error'.
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
        logger.debug(f"Daily metrics: {daily_metrics}")

        # Step 2: AUDIT - Compare actual vs planned (yesterday + re-check window)
        logger.info("Step 2: AUDIT - Comparing actual vs planned")
        audit_result = audit_recent(today)
        results['steps']['audit'] = audit_result
        logger.info("Audit: %s — %s", audit_result.get('status'), audit_result.get('message'))
        logger.debug(f"Audit result: {audit_result}")
        if audit_result.get('status') == 'unverified':
            results['status'] = 'unverified'

        # Step 3: Get compliance report
        logger.info("Step 3: Checking compliance")
        compliance_json = get_compliance_report(days=7)
        compliance = json.loads(compliance_json)
        results['steps']['compliance'] = compliance
        logger.debug(f"Compliance: {json.dumps(compliance, indent=2)}")

        # Step 4: Build context (canonical snapshot).
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

        # Step 5: Generate morning brief (template; no LLM in this script)
        logger.info("Step 5: Generating morning brief")
        brief = generate_morning_brief(context, compliance, audit_result)
        results['steps']['brief'] = brief
        results['morning_brief'] = brief

        logger.info("=" * 50)
        logger.info("Morning audit complete (%s)", results['status'])
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"Morning audit failed: {str(e)}")
        results['status'] = 'error'
        results['error'] = str(e)

    return results


def _fetch_actual(day_str: str) -> list[dict[str, Any]] | None:
    """Activities Garmin recorded on `day_str`, or None when they cannot be
    verified.

    get_activities_range returns a JSON error object — not a list — when
    Garmin is unreachable or a login is required. Treating that dict as a
    truthy activity list once recorded an outage as a 'bonus' workout on a
    rest day; None here means "verify later", never "nothing happened".
    """
    try:
        payload = json.loads(get_activities_range(day_str, day_str))
    except (TypeError, ValueError) as e:
        logger.warning("Could not parse Garmin activities for %s: %s", day_str, e)
        return None
    if isinstance(payload, dict):
        logger.warning("Garmin activities unavailable for %s: %s",
                       day_str, payload.get('error', payload))
        return None
    if not isinstance(payload, list):
        logger.warning("Unexpected activities payload for %s: %s",
                       day_str, type(payload).__name__)
        return None
    return [a for a in payload if isinstance(a, dict)]


def _audit_day(plan: dict[str, Any], day_str: str) -> dict[str, Any]:
    """Audit one plan day against Garmin and update the plan day in place.

    Does not save — callers persist the plan once. Returns the audit record.
    """
    day_plan = plan['days'][day_str]
    planned_sessions = _normalize_planned(day_plan.get('planned'))
    non_rest = [s for s in planned_sessions if 'rest' not in str(s.get('type', '')).lower()]

    actual_activities = _fetch_actual(day_str)
    if actual_activities is None:
        # Garmin could not be checked: mark for re-audit, persist no 'actual'.
        day_plan['status'] = 'unverified'
        return {
            'status': 'unverified',
            'message': f"Garmin unavailable — {day_str} not verified",
            'date': day_str,
            'planned': planned_sessions,
            'actual_count': 0,
        }

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
        actual_types = [str(a.get('type', '')).lower() for a in actual_activities]

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

    # Update the plan day with the verified result
    day_plan['actual'] = actual_activities
    day_plan['status'] = status

    return {
        'status': status,
        'message': message,
        'date': day_str,
        'planned': planned_sessions,
        'actual_count': len(actual_activities),
    }


def audit_yesterday(today: date) -> dict[str, Any]:
    """
    Audit yesterday's activities against the plan.

    Marks the planned sessions as completed, missed, modified — or
    'unverified' when Garmin could not be checked.
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

    result = _audit_day(plan, yesterday_str)
    save_weekly_plan(plan)
    return result


def audit_recent(today: date, window_days: int = REAUDIT_WINDOW_DAYS) -> dict[str, Any]:
    """Audit yesterday, then re-check earlier days still 'missed'/'unverified'.

    Returns yesterday's audit record (the brief's input) with a `reaudited`
    list of the earlier days whose status changed — a session that synced
    late, or a day first audited while Garmin was down, now shows as it was.
    """
    result = audit_yesterday(today)
    if result.get('status') == 'no_plan':
        return result

    plan = get_current_plan()
    if not plan or 'days' not in plan:
        return result

    changed = []
    for back in range(2, window_days + 1):
        day_str = (today - timedelta(days=back)).isoformat()
        day_plan = plan['days'].get(day_str)
        if not isinstance(day_plan, dict) or day_plan.get('status') not in REAUDIT_STATUSES:
            continue
        before = day_plan.get('status')
        record = _audit_day(plan, day_str)
        if record['status'] != before:
            changed.append({'date': day_str, 'from': before, 'to': record['status']})
    if changed:
        save_weekly_plan(plan)
        logger.info("Re-audit updated earlier days: %s", changed)
    result['reaudited'] = changed
    return result


def generate_morning_brief(
    context: dict[str, Any],
    compliance: dict[str, Any],
    audit: dict[str, Any]
) -> str:
    """
    Generate a concise morning brief from the audit context (template).
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
    if audit.get('status') in ('completed', 'missed', 'substituted', 'partial'):
        lines.append(f"**Yesterday:** {audit.get('message')}")
    elif audit.get('status') == 'unverified':
        lines.append("**Yesterday:** not verified — Garmin was unavailable; "
                     "the next run re-checks it")
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


def main() -> int:
    """Run the audit; exit non-zero unless yesterday was verified and the run succeeded."""
    results = asyncio.run(run_morning_audit())
    print("\n" + results.get('morning_brief', 'No brief generated'))
    return 0 if results.get('status') == 'success' else 1


if __name__ == "__main__":
    sys.exit(main())
