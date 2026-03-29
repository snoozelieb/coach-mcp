"""Interactive coaching tools using MCP sampling and elicitation.

These tools leverage fastmcp v3 features:
- Sampling: server requests LLM completions for analysis
- Elicitation: server asks the user structured questions mid-tool

These features require client support. If the client doesn't support
sampling or elicitation, the tools will fall back gracefully.
"""

from fastmcp import Context
from ..mcp_app import mcp
from ..planner import get_current_plan, load_athlete, load_coaching_log
from ..rules import load_training_config, get_upcoming_events
from ..fitness import load_fitness_history, calculate_fitness_metrics, _extract_total_loads
from ..config import DATA_DIR, ATHLETE_FILE
from datetime import date
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool()
async def generate_smart_brief(ctx: Context) -> str:
    """
    Generate an LLM-powered morning brief using MCP sampling.

    Uses the connected LLM client to analyze current coaching data and
    produce an intelligent morning brief. Requires client sampling support.

    Falls back to a structured data summary if sampling is unavailable.

    Returns:
        Markdown morning brief with yesterday's review, today's plan,
        recovery assessment, and coaching recommendations.
    """
    try:
        today = date.today()
        day_name = today.strftime('%A')
        await ctx.report_progress(0, 3, "Gathering coaching data")

        # Gather data for the brief
        athlete = load_athlete()
        plan = get_current_plan()
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})
        training_config = load_training_config()

        name = athlete.get('personal', {}).get('name', 'Athlete')
        today_str = today.isoformat()

        # Today's planned session
        today_plan = None
        if plan and 'days' in plan and today_str in plan['days']:
            today_plan = plan['days'][today_str].get('planned')

        # Recent fitness metrics
        metrics = {}
        if daily_loads:
            total_loads = _extract_total_loads(daily_loads)
            metrics = calculate_fitness_metrics(total_loads)

        # Active injuries
        injuries = [
            i for i in athlete.get('injury_history', [])
            if i.get('status') in ('active', 'improving')
        ]

        # Upcoming events
        events = get_upcoming_events()

        # Coaching decisions
        log = load_coaching_log()
        recent_decisions = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'active'
        ][:3]

        data_summary = {
            'date': today_str,
            'day': day_name,
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
                {'type': d.get('type'), 'summary': d.get('summary', d.get('decision', ''))}
                for d in recent_decisions
            ],
        }

        await ctx.report_progress(1, 3, "Data gathered, requesting analysis")

        # Try sampling (LLM-powered brief)
        try:
            result = await ctx.sample(
                messages=(
                    f"Generate a concise morning coaching brief for {name} "
                    f"on {day_name}, {today_str}.\n\n"
                    f"Data:\n```json\n{json.dumps(data_summary, indent=2)}\n```\n\n"
                    f"Format: Markdown, under 200 words. "
                    f"Sections: Yesterday, Today's Session, Recovery/Fitness Check, Key Focus. "
                    f"Be direct and prescriptive — you are the coach."
                ),
                system_prompt=(
                    "You are an expert adaptive training coach. Be direct: "
                    "'You need rest' not 'Maybe consider taking it easy.' "
                    "Base recommendations on the data provided."
                ),
                max_tokens=500,
            )
            await ctx.report_progress(3, 3, "Brief generated")
            return result.text

        except Exception as e:
            logger.info("Sampling unavailable (%s), returning data summary", e)
            # Fall back to structured data (client doesn't support sampling)
            await ctx.report_progress(3, 3, "Returning data summary")
            return json.dumps({
                'note': 'Sampling not available — use this data to generate a brief',
                **data_summary,
            }, indent=2)

    except Exception as e:
        logger.exception("generate_smart_brief failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
async def interactive_check_in(ctx: Context) -> str:
    """
    Interactive coaching check-in using MCP elicitation.

    Asks the athlete structured questions about how they're feeling,
    then provides coaching recommendations based on their responses
    combined with objective data.

    Requires client elicitation support. Falls back to returning
    a question list if elicitation is unavailable.

    Returns:
        JSON with athlete responses and coaching recommendations.
    """
    try:
        await ctx.report_progress(0, 3, "Starting check-in")

        # Try elicitation for structured input
        try:
            # Question 1: How are you feeling?
            feeling = await ctx.elicit(
                "How are you feeling today?",
                response_type=["Great - ready to push", "Good - normal", "Tired but OK", "Beat up - need easy", "Injured/pain"],
            )

            if not hasattr(feeling, 'data'):
                return json.dumps({
                    'status': 'cancelled',
                    'message': 'Check-in cancelled by user',
                })

            await ctx.report_progress(1, 3, "First response received")

            # Question 2: Sleep quality
            sleep = await ctx.elicit(
                "How did you sleep last night?",
                response_type=["Excellent (8+ hrs, felt rested)", "Good (7-8 hrs)", "OK (6-7 hrs)", "Poor (< 6 hrs or restless)"],
            )

            sleep_response = sleep.data if hasattr(sleep, 'data') else 'unknown'

            # Question 3: Any niggles?
            niggles = await ctx.elicit(
                "Any new aches, pains, or niggles? (type 'none' if all clear)",
                response_type=None,
            )

            niggle_response = niggles.data if hasattr(niggles, 'data') else 'none'

            await ctx.report_progress(2, 3, "Analyzing responses")

            # Build coaching response based on subjective + objective data
            athlete = load_athlete()
            plan = get_current_plan()
            today_str = date.today().isoformat()
            today_plan = None
            if plan and 'days' in plan and today_str in plan['days']:
                today_plan = plan['days'][today_str].get('planned')

            return json.dumps({
                'status': 'complete',
                'responses': {
                    'feeling': feeling.data,
                    'sleep': sleep_response,
                    'niggles': niggle_response,
                },
                'today_planned': today_plan,
                'coaching_note': (
                    'Use these subjective responses together with '
                    'get_coaching_snapshot() objective data to decide '
                    'whether today\'s plan should be adjusted.'
                ),
            }, indent=2)

        except Exception as e:
            logger.info("Elicitation unavailable (%s), returning question list", e)
            # Fall back: return questions for the LLM to ask conversationally
            return json.dumps({
                'note': 'Elicitation not available — ask these questions conversationally',
                'questions': [
                    {
                        'id': 'feeling',
                        'question': 'How are you feeling today?',
                        'options': ["Great - ready to push", "Good - normal", "Tired but OK", "Beat up - need easy", "Injured/pain"],
                    },
                    {
                        'id': 'sleep',
                        'question': 'How did you sleep last night?',
                        'options': ["Excellent (8+ hrs)", "Good (7-8 hrs)", "OK (6-7 hrs)", "Poor (< 6 hrs)"],
                    },
                    {
                        'id': 'niggles',
                        'question': 'Any new aches, pains, or niggles?',
                        'type': 'free_text',
                    },
                ],
            }, indent=2)

    except Exception as e:
        logger.exception("interactive_check_in failed")
        return json.dumps({'error': str(e)})
