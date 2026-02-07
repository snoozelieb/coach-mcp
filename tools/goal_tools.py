from mcp_app import mcp
from garmin_client import garmin_api_call
from parsers import parse_activities
from rules import load_training_config
from datetime import date, timedelta
import json


@mcp.tool()
def get_goal_progress(days: int = 14) -> str:
    """
    Get progress toward the three goal categories.

    Tracks balance across:
    - Race Preparation (50%): Training volume and key sessions
    - Fun Activities (25%): Padel, Ultimate, social activities
    - Aesthetics (25%): Strength sessions, upper body focus

    Args:
        days: Number of days to look back (default 14)

    Returns:
        JSON with progress for each goal category and recommendations.
    """
    try:
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Get activities
        raw_activities = garmin_api_call(lambda c: c.get_activities_by_date(start, today.isoformat()))
        activities = parse_activities(raw_activities)

        # Load training config for goal definitions
        training_config = load_training_config()
        goal_balance = training_config.get('goal_balance', {})

        # Categorize activities
        race_prep_mins = 0
        fun_mins = 0
        aesthetics_mins = 0
        last_fun_date = None
        strength_count = 0

        fun_types = ['padel', 'ultimate_disc', 'social_ride', 'tennis', 'squash', 'badminton']
        strength_types = ['strength_training', 'indoor_cardio', 'functional_strength']

        for activity in activities:
            act_type = activity.get('type', '').lower()
            duration = activity.get('duration_mins', 0)
            act_date = activity.get('date')

            # Fun activities
            if any(f in act_type for f in fun_types):
                fun_mins += duration
                if last_fun_date is None or act_date > last_fun_date:
                    last_fun_date = act_date
            # Strength/aesthetics
            elif any(s in act_type for s in strength_types):
                aesthetics_mins += duration
                strength_count += 1
            # All others count as race prep
            else:
                race_prep_mins += duration

        total_mins = race_prep_mins + fun_mins + aesthetics_mins

        # Calculate percentages
        if total_mins > 0:
            race_prep_pct = round(race_prep_mins / total_mins * 100)
            fun_pct = round(fun_mins / total_mins * 100)
            aesthetics_pct = round(aesthetics_mins / total_mins * 100)
        else:
            race_prep_pct = fun_pct = aesthetics_pct = 0

        # Calculate days since last fun activity
        days_since_fun = None
        if last_fun_date:
            try:
                fun_date = date.fromisoformat(last_fun_date)
                days_since_fun = (today - fun_date).days
            except ValueError:
                pass

        # Generate recommendations
        recommendations = []
        prompt_fun = goal_balance.get('fun_activities', {}).get('prompt_if_missing_days', 14)

        if days_since_fun is not None and days_since_fun > prompt_fun:
            recommendations.append(f"Fun activity missing for {days_since_fun} days - schedule Padel or Frisbee soon!")
        elif days_since_fun is None:
            recommendations.append("No fun activities found recently - remember to include Padel or Frisbee!")

        if aesthetics_pct < 20 and strength_count < 2:
            recommendations.append("Upper body/aesthetics underrepresented - add a strength session")

        if race_prep_pct > 80:
            recommendations.append("Heavy race prep focus - make sure to balance with fun and gym")

        # Target vs actual
        targets = {
            'race_preparation': {'target': 50, 'actual': race_prep_pct},
            'fun_activities': {'target': 25, 'actual': fun_pct},
            'aesthetics': {'target': 25, 'actual': aesthetics_pct}
        }

        return json.dumps({
            'period_days': days,
            'total_training_mins': round(total_mins),
            'goal_progress': {
                'race_preparation': {
                    'mins': round(race_prep_mins),
                    'pct': race_prep_pct,
                    'target_pct': 50,
                    'status': 'on_track' if race_prep_pct >= 40 else 'low'
                },
                'fun_activities': {
                    'mins': round(fun_mins),
                    'pct': fun_pct,
                    'target_pct': 25,
                    'days_since_last': days_since_fun,
                    'status': 'on_track' if fun_pct >= 15 else ('missing' if days_since_fun and days_since_fun > prompt_fun else 'low')
                },
                'aesthetics': {
                    'mins': round(aesthetics_mins),
                    'pct': aesthetics_pct,
                    'target_pct': 25,
                    'strength_sessions': strength_count,
                    'status': 'on_track' if strength_count >= 2 else 'low'
                }
            },
            'recommendations': recommendations,
            'balance_score': 'good' if len(recommendations) == 0 else ('needs_attention' if len(recommendations) <= 1 else 'rebalance_needed')
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})
