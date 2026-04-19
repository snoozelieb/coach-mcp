"""Interactive coaching tools — data gathering for the LLM coach.

These tools collect and structure data for the LLM to reason over. Earlier
versions used MCP sampling (`ctx.sample`) and elicitation (`ctx.elicit`) to
get the server to drive the LLM/user directly, but Claude Code and most
current MCP clients don't support those features, so the sampling/elicit
paths effectively never fired. The current design returns the structured
data directly; the coach (LLM-in-the-conversation) writes the brief or asks
the questions. Cleaner and works everywhere.
"""

from ..mcp_app import mcp
from ..parsers import build_current_time_context
from ..planner import get_current_plan, load_athlete, load_coaching_log
from ..rules import get_upcoming_events
from ..fitness import load_fitness_history, calculate_fitness_metrics, _extract_total_loads
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool()
def generate_smart_brief() -> str:
    """
    Gather structured data for a time-aware coaching brief.

    Returns a JSON payload the coach (LLM-in-the-conversation) can render
    into a brief grounded in the current time_period, yesterday's activity,
    today's plan, recent fitness, injuries, upcoming events, and active
    decisions. The coach decides tone and length; this tool only shapes
    the data.

    Returns:
        JSON with current_time_context, date, day, time_period, athlete_name,
        today_plan, fitness (ctl/atl/tsb/acwr), active_injuries,
        upcoming_events, recent_decisions. Use the `framing` hint to match
        the greeting to the time_period.
    """
    try:
        now = datetime.now()
        today = now.date()
        day_name = today.strftime('%A')
        time_ctx = build_current_time_context(now)
        time_period = time_ctx['time_period']

        athlete = load_athlete()
        plan = get_current_plan()
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})

        name = athlete.get('personal', {}).get('name', 'Athlete')
        today_str = today.isoformat()

        today_plan = None
        if plan and 'days' in plan and today_str in plan['days']:
            today_plan = plan['days'][today_str].get('planned')

        metrics = {}
        if daily_loads:
            total_loads = _extract_total_loads(daily_loads)
            metrics = calculate_fitness_metrics(total_loads)

        injuries = [
            i for i in athlete.get('injury_history', [])
            if i.get('status') in ('active', 'improving')
        ]

        events = get_upcoming_events()

        log = load_coaching_log()
        recent_decisions = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'active'
        ][:3]

        return json.dumps({
            'framing': (
                f"Brief for {name} — it's {day_name}, {today_str}, "
                f"{time_period} ({time_ctx['hour']:02d}:{time_ctx['minute']:02d}). "
                f"Greeting must match time_period; if evening/night, "
                f"focus on today's completion and tomorrow's prep."
            ),
            'current_time_context': time_ctx,
            'date': today_str,
            'day': day_name,
            'time_period': time_period,
            'athlete_name': name,
            'today_plan': today_plan,
            'fitness': {
                'ctl': metrics.get('ctl'),
                'atl': metrics.get('atl'),
                'tsb': metrics.get('tsb'),
                'acwr': metrics.get('acwr'),
                'acwr_status': metrics.get('acwr_status'),
            } if metrics else None,
            'active_injuries': [
                {'location': i.get('location'), 'status': i.get('status')}
                for i in injuries
            ],
            'upcoming_events': [
                {'name': e.get('name'), 'days_until': e.get('days_until')}
                for e in events[:2]
            ],
            'recent_decisions': [
                {'type': d.get('type'),
                 'summary': d.get('summary', d.get('decision', ''))}
                for d in recent_decisions
            ],
        }, indent=2)

    except Exception as e:
        logger.exception("generate_smart_brief failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def interactive_check_in() -> str:
    """
    Return the check-in question set + context for the coach to ask conversationally.

    The coach asks these three questions in its own voice, captures the
    athlete's replies, then combines them with get_coaching_snapshot()
    objective data to decide whether today's plan needs adjustment.

    Returns:
        JSON with current_time_context, today_planned, and three questions
        (feeling, sleep, niggles) with suggested options.
    """
    try:
        plan = get_current_plan()
        time_ctx = build_current_time_context()
        today_str = time_ctx['date']
        today_plan = None
        if plan and 'days' in plan and today_str in plan['days']:
            today_plan = plan['days'][today_str].get('planned')

        return json.dumps({
            'current_time_context': time_ctx,
            'today_planned': today_plan,
            'questions': [
                {
                    'id': 'feeling',
                    'question': 'How are you feeling today?',
                    'options': [
                        'Great - ready to push',
                        'Good - normal',
                        'Tired but OK',
                        'Beat up - need easy',
                        'Injured/pain',
                    ],
                },
                {
                    'id': 'sleep',
                    'question': 'How did you sleep last night?',
                    'options': [
                        'Excellent (8+ hrs, felt rested)',
                        'Good (7-8 hrs)',
                        'OK (6-7 hrs)',
                        'Poor (< 6 hrs or restless)',
                    ],
                },
                {
                    'id': 'niggles',
                    'question': "Any new aches, pains, or niggles? (free text)",
                    'type': 'free_text',
                },
            ],
            'coaching_note': (
                'Ask the athlete these three questions conversationally, '
                'then combine their answers with get_coaching_snapshot() '
                f'objective data. Current time_period is {time_ctx["time_period"]}.'
            ),
        }, indent=2)

    except Exception as e:
        logger.exception("interactive_check_in failed")
        return json.dumps({'error': str(e)})
