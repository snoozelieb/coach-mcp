"""Planning tools - weekly plan management, periodization, prescriptions, Garmin push."""

from fastmcp import Context
from ..mcp_app import mcp
from ..garmin_client import garmin_api_call, schedule_workout
from ..parsers import (parse_activities, parse_training_readiness,
                     parse_resting_heart_rate, parse_sleep_score, parse_body_battery)
from ..planner import (build_planning_context, get_current_plan, save_weekly_plan,
                     create_empty_week_template, get_pending_suggestions as get_suggestions,
                     load_athlete, load_methodology, load_coaching_log, save_coaching_log,
                     get_week_constraints as _get_week_constraints)
from ..rules import load_training_config, check_weekly_compliance
from ..fitness import load_fitness_history, calculate_fitness_metrics
from ..config import DATA_DIR, RECENT_ACTIVITY_DAYS, TRAINING_CONFIG_FILE
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool()
async def get_planning_context(ctx: Context) -> str:
    """
    Full context for building or adjusting a training plan.

    Returns WHO (athlete profile, constraints, injuries), WHAT (current block,
    upcoming events, A-race requirements), HOW (pillars, safety constraints,
    race templates), recent activities, compliance, recovery, and coaching
    continuity (active decisions, adaptation patterns).

    Use this when creating a new weekly plan or making significant plan changes.
    For quick coaching checks, use get_coaching_snapshot() instead.
    """
    try:
        today = date.today()
        await ctx.report_progress(0, 4, "Loading athlete profile")

        # Load configurations from new file structure
        athlete_profile = load_athlete()
        training_config = load_training_config()
        methodology = load_methodology()

        # Get recent activities (14 days)
        start_14_days = today - timedelta(days=RECENT_ACTIVITY_DAYS)
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                start_14_days.isoformat(),
                today.isoformat()
            )
        )
        recent_activities = parse_activities(raw_activities)
        await ctx.report_progress(1, 4, "Activities fetched")

        # Get compliance for current week
        start_7_days = today - timedelta(days=7)
        week_activities = [
            a for a in recent_activities
            if a.get('date') and date.fromisoformat(a['date']) >= start_7_days
        ]
        compliance = check_weekly_compliance(week_activities)

        # Get today's recovery metrics
        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
        today_recovery = parse_training_readiness(readiness_data)

        stats = garmin_api_call(lambda c: c.get_user_summary(today.isoformat()))
        body_battery = garmin_api_call(lambda c: c.get_body_battery(today.isoformat()))

        today_recovery['rhr'] = parse_resting_heart_rate(stats)
        today_recovery['body_battery'] = parse_body_battery(body_battery)
        today_recovery['sleep_score'] = parse_sleep_score(stats)

        # Calculate load status
        week_ago = (today - timedelta(days=7)).isoformat()
        two_weeks_ago = (today - timedelta(days=14)).isoformat()

        recent_load_activities = garmin_api_call(lambda c: c.get_activities_by_date(week_ago, today.isoformat()))
        prior_load_activities = garmin_api_call(lambda c: c.get_activities_by_date(two_weeks_ago, week_ago))

        recent_duration_mins = sum(
            a.get('duration', 0) / 60 for a in recent_load_activities
        ) if recent_load_activities else 0
        prior_duration_mins = sum(
            a.get('duration', 0) / 60 for a in prior_load_activities
        ) if prior_load_activities else 0

        if prior_duration_mins > 0:
            load_ratio = recent_duration_mins / prior_duration_mins
        else:
            load_ratio = 1.0

        readiness_score = today_recovery.get('score', 0)
        readiness_level = today_recovery.get('level', 'UNKNOWN')

        if readiness_level == 'PRIME' or readiness_score >= 80:
            load_recommendation = "high"
        elif readiness_level == 'HIGH' or readiness_score >= 60:
            load_recommendation = "moderate_to_high"
        elif readiness_level == 'MODERATE' or readiness_score >= 40:
            load_recommendation = "moderate"
        elif readiness_level == 'LOW' or readiness_score >= 20:
            load_recommendation = "low"
        else:
            load_recommendation = "rest"

        load_status = {
            'readiness_score': readiness_score,
            'readiness_level': readiness_level,
            'acute_load': today_recovery.get('acute_load'),
            'load_ratio': round(load_ratio, 2),
            'load_trend': 'decreasing' if load_ratio < 0.8 else ('increasing' if load_ratio > 1.15 else 'stable'),
            'recommended_intensity': load_recommendation,
            'recent_volume_mins': round(recent_duration_mins),
            'prior_volume_mins': round(prior_duration_mins),
        }

        await ctx.report_progress(3, 4, "Recovery and load assessed")

        # Get pending suggestions
        pending = get_suggestions()

        # Build full context with new file structure
        context = build_planning_context(
            athlete_profile=athlete_profile,
            training_config=training_config,
            recent_activities=recent_activities,
            compliance_status=compliance,
            today_recovery=today_recovery,
            pending_suggestions=pending,
            methodology=methodology,
        )

        # Add load status to context
        context['load_status'] = load_status

        # Calculate goal balance
        fun_types = ['padel', 'ultimate_disc', 'social_ride', 'tennis', 'squash', 'badminton']
        strength_types = ['strength_training', 'indoor_cardio', 'functional_strength']

        race_prep_mins = 0
        fun_mins = 0
        aesthetics_mins = 0
        last_fun_date = None
        strength_count = 0

        for activity in recent_activities:
            act_type = activity.get('type', '').lower()
            duration = activity.get('duration_mins', 0)
            act_date = activity.get('date')

            if any(f in act_type for f in fun_types):
                fun_mins += duration
                if last_fun_date is None or (act_date and act_date > last_fun_date):
                    last_fun_date = act_date
            elif any(s in act_type for s in strength_types):
                aesthetics_mins += duration
                strength_count += 1
            else:
                race_prep_mins += duration

        total_mins = race_prep_mins + fun_mins + aesthetics_mins
        days_since_fun = None
        if last_fun_date:
            try:
                fun_date = date.fromisoformat(last_fun_date)
                days_since_fun = (today - fun_date).days
            except ValueError:
                pass

        context['goal_progress'] = {
            'race_preparation': {
                'mins': round(race_prep_mins),
                'pct': round(race_prep_mins / total_mins * 100) if total_mins > 0 else 0
            },
            'fun_activities': {
                'mins': round(fun_mins),
                'pct': round(fun_mins / total_mins * 100) if total_mins > 0 else 0,
                'days_since_last': days_since_fun,
                'needs_attention': days_since_fun is not None and days_since_fun > 14
            },
            'aesthetics': {
                'mins': round(aesthetics_mins),
                'strength_sessions': strength_count,
                'needs_attention': strength_count < 2
            }
        }

        return json.dumps(context, indent=2, default=str)

    except Exception as e:
        logger.exception("get_planning_context failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_periodization_status() -> str:
    """
    Where are we in the season? Current phase, progress, and what comes next.

    Returns current phase (base/build/peak/taper), week within phase, days
    until A-race, remaining phases, fitness trajectory, and phase-specific
    guidance (key sessions, intensity targets, volume trend).

    The current phase determines training priorities: base = volume over
    intensity, build = race-specific intensity, peak = race simulation,
    taper = sharp volume reduction. Use this to ensure weekly plans align
    with the season plan.
    """
    try:
        today = date.today()
        config = load_training_config()

        periodization = config.get('periodization', {})
        current_block = config.get('current_block', {})
        events = config.get('events', [])

        # Find A-race
        a_race = None
        for event in events:
            if event.get('priority') == 'A':
                try:
                    race_date = date.fromisoformat(event.get('date', ''))
                    if race_date > today:
                        a_race = {
                            'name': event.get('name'),
                            'date': event.get('date'),
                            'days_until': (race_date - today).days,
                            'weeks_until': (race_date - today).days // 7,
                            'type': event.get('type'),
                        }
                        break
                except ValueError:
                    continue

        # Current phase info
        current_phase = periodization.get('current_phase', current_block.get('phase', 'unknown'))
        phases = periodization.get('phases', {})
        phase_info = phases.get(current_phase, {})

        # Calculate week within phase
        phase_start = periodization.get('phase_start', current_block.get('start_date'))
        if phase_start:
            try:
                start = date.fromisoformat(phase_start)
                weeks_in_phase = (today - start).days // 7 + 1
            except ValueError:
                weeks_in_phase = 1
        else:
            weeks_in_phase = 1

        typical_weeks = phase_info.get('typical_weeks', 4)

        # Get fitness status if available
        fitness_metrics = None
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(daily_loads)
        except Exception:
            pass

        # Build remaining phases
        phase_order = ['base', 'build', 'peak', 'taper']
        remaining_phases = []
        found_current = False
        for phase in phase_order:
            if phase == current_phase:
                found_current = True
                # Add remaining weeks of current phase
                remaining_weeks = max(0, typical_weeks - weeks_in_phase)
                if remaining_weeks > 0:
                    remaining_phases.append({
                        'phase': phase,
                        'weeks': remaining_weeks,
                        'status': 'current',
                    })
            elif found_current and phase in phases:
                remaining_phases.append({
                    'phase': phase,
                    'weeks': phases[phase].get('typical_weeks', 4),
                    'status': 'upcoming',
                })

        # Phase-specific guidance
        guidance = {
            'focus': phase_info.get('focus', 'General training'),
            'intensity_distribution': phase_info.get('intensity_distribution', {}),
            'volume_trend': phase_info.get('volume_trend', 'stable'),
            'key_sessions': phase_info.get('key_sessions', []),
        }

        result = {
            'current_phase': {
                'name': current_phase,
                'week': weeks_in_phase,
                'of_weeks': typical_weeks,
                'progress_pct': round(weeks_in_phase / typical_weeks * 100) if typical_weeks > 0 else 0,
            },
            'a_race': a_race,
            'remaining_phases': remaining_phases,
            'phase_guidance': guidance,
            'weekly_volume_target_hrs': current_block.get('weekly_volume_target_hrs'),
        }

        if fitness_metrics:
            result['fitness_status'] = {
                'ctl': fitness_metrics['ctl'],
                'tsb': fitness_metrics['tsb'],
                'acwr': fitness_metrics['acwr'],
                'acwr_status': fitness_metrics['acwr_status'],
            }

        # Add coaching notes based on phase
        notes = []
        if current_phase == 'base':
            notes.append("Focus on volume over intensity. Build aerobic foundation.")
            notes.append("Strength work is critical now - easier to build when load is lower.")
        elif current_phase == 'build':
            notes.append("Add race-specific intensity. Maintain (don't increase) volume.")
            notes.append("Start practicing race nutrition and pacing strategies.")
        elif current_phase == 'peak':
            notes.append("Race simulation efforts. Start reducing volume.")
            notes.append("Confidence-building sessions - you're ready.")
        elif current_phase == 'taper':
            notes.append("Sharp volume reduction. Maintain short sharp efforts.")
            notes.append("Trust your fitness. Rest is training now.")

        if a_race and a_race['days_until'] < 14:
            notes.append(f"RACE WEEK APPROACHING: {a_race['days_until']} days to {a_race['name']}")

        result['coaching_notes'] = notes

        return json.dumps(result, indent=2)

    except Exception as e:
        logger.exception("get_periodization_status failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_weekly_prescription() -> str:
    """
    What should this week look like? Volume, intensity, and key sessions.

    Combines phase demands, fitness status (CTL/ACWR/TSB), recovery readiness,
    and pillar compliance into a weekly prescription. Volume is auto-adjusted:
    reduced if ACWR > 1.3 (injury risk), increased if ACWR < 0.8 (undertrained).

    The prescription is a starting point — adapt it through conversation with
    the athlete based on how they're feeling, life events, and injuries.

    Returns target volume, key sessions, intensity targets, pillar priorities,
    injury constraints, life events, and flexibility notes.
    """
    try:
        today = date.today()

        # Load all context
        config = load_training_config()
        athlete = load_athlete()
        periodization = config.get('periodization', {})
        current_block = config.get('current_block', {})

        # Get current phase info
        current_phase = periodization.get('current_phase', current_block.get('phase', 'base'))
        phases = periodization.get('phases', {})
        phase_info = phases.get(current_phase, {})

        # Get fitness metrics
        fitness_metrics = None
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(daily_loads)
        except Exception:
            pass

        # Get today's readiness
        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
        readiness = parse_training_readiness(readiness_data)

        # Get recent compliance
        start_7_days = today - timedelta(days=7)
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                start_7_days.isoformat(),
                today.isoformat()
            )
        )
        recent_activities = parse_activities(raw_activities)
        compliance = check_weekly_compliance(recent_activities)

        # Calculate prescribed volume
        base_volume_hrs = current_block.get('weekly_volume_target_hrs', 6.0)
        volume_trend = phase_info.get('volume_trend', 'stable')

        # Adjust volume based on ACWR if available
        if fitness_metrics:
            acwr = fitness_metrics['acwr']
            if acwr > 1.3:
                volume_adjustment = 0.85  # Reduce 15% if overloaded
                volume_note = "Reduced due to elevated ACWR"
            elif acwr < 0.8:
                volume_adjustment = 1.10  # Can increase 10% if undertrained
                volume_note = "Safe to push - load ratio is low"
            else:
                volume_adjustment = 1.0
                volume_note = "Load ratio in sweet spot"
        else:
            volume_adjustment = 1.0
            volume_note = "No fitness history - using base target"

        prescribed_volume_hrs = round(base_volume_hrs * volume_adjustment, 1)

        # Get intensity targets from phase
        intensity_targets = phase_info.get('intensity_distribution', {
            'z1_z2_pct': 80,
            'z3_pct': 15,
            'z4_z5_pct': 5,
        })

        # Get key sessions for this phase
        key_sessions = phase_info.get('key_sessions', ['long_effort', 'strength'])

        # Check what pillars need attention
        pillar_priorities = []
        if not compliance.get('strength', {}).get('compliant', True):
            deficit = compliance.get('strength', {}).get('deficit', 0)
            pillar_priorities.append(f"Strength: {deficit} session(s) behind")
        if not compliance.get('mobility', {}).get('compliant', True):
            deficit = compliance.get('mobility', {}).get('deficit', 0)
            pillar_priorities.append(f"Mobility: {deficit} mins behind")

        # Check for active injuries
        injury_constraints = []
        injuries = athlete.get('injury_history', [])
        for injury in injuries:
            if injury.get('status') == 'active':
                restricted = injury.get('restricted_activities', [])
                injury_constraints.append({
                    'injury': injury.get('type', 'Unknown'),
                    'restricted': restricted,
                    'safe': injury.get('safe_activities', []),
                })

        # Check for life events this week
        life_events = []
        constraints = athlete.get('life_constraints', {})
        travel = constraints.get('travel', [])
        for trip in travel:
            try:
                trip_date = date.fromisoformat(trip.get('date', ''))
                trip_end = date.fromisoformat(trip.get('end_date', trip.get('date', '')))
                # Check if trip overlaps with this week
                week_end = today + timedelta(days=7)
                if trip_date <= week_end and trip_end >= today:
                    life_events.append({
                        'event': trip.get('type', 'travel'),
                        'dates': f"{trip.get('date')} to {trip.get('end_date', trip.get('date'))}",
                        'notes': trip.get('notes'),
                    })
            except ValueError:
                continue

        # Build prescription
        prescription = {
            'volume': {
                'target_hrs': prescribed_volume_hrs,
                'base_target_hrs': base_volume_hrs,
                'adjustment': volume_adjustment,
                'adjustment_reason': volume_note,
            },
            'intensity': {
                'targets': intensity_targets,
                'note': f"Phase: {current_phase} - {phase_info.get('focus', 'General')}",
            },
            'key_sessions': {
                'required': key_sessions,
                'count': len([k for k in key_sessions if k not in ['mobility', 'rest']]),
                'note': "These sessions drive adaptation - prioritize them",
            },
            'pillar_priorities': pillar_priorities if pillar_priorities else ["All pillars on track"],
            'constraints': {
                'injuries': injury_constraints,
                'life_events': life_events,
            },
            'readiness': {
                'score': readiness.get('score'),
                'level': readiness.get('level'),
                'recommendation': 'Rest or easy' if readiness.get('score', 50) < 40 else 'Normal training',
            },
            'flexibility': {
                'swappable': "Long efforts can move within week based on weather/life",
                'fixed': "Strength days benefit from consistency (same days each week)",
                'note': "Adapt the plan through conversation - this is guidance, not law",
            },
        }

        if fitness_metrics:
            prescription['current_fitness'] = {
                'ctl': fitness_metrics['ctl'],
                'tsb': fitness_metrics['tsb'],
                'form_status': 'Fresh' if fitness_metrics['tsb'] > 0 else 'Fatigued',
            }

        return json.dumps(prescription, indent=2)

    except Exception as e:
        logger.exception("get_weekly_prescription failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_phase(new_phase: str, notes: str = None) -> str:
    """
    Transition to a new training phase.

    Use this when the athlete is ready to move to the next phase of their
    periodization plan. This is a significant coaching decision.

    Args:
        new_phase: The phase to transition to (base, build, peak, taper)
        notes: Optional notes about why the transition is happening

    Returns:
        Confirmation with updated phase information.
    """
    valid_phases = ['base', 'build', 'peak', 'taper', 'recovery', 'maintenance']

    if new_phase.lower() not in valid_phases:
        return json.dumps({
            'error': f"Invalid phase '{new_phase}'. Valid phases: {valid_phases}"
        })

    try:
        config_path = DATA_DIR / TRAINING_CONFIG_FILE
        with open(config_path) as f:
            config = json.load(f)

        today = date.today()
        old_phase = config.get('periodization', {}).get('current_phase', 'unknown')

        # Update periodization
        if 'periodization' not in config:
            config['periodization'] = {}

        config['periodization']['current_phase'] = new_phase.lower()
        config['periodization']['phase_start'] = today.isoformat()

        # Update current_block
        if 'current_block' not in config:
            config['current_block'] = {}

        config['current_block']['phase'] = new_phase.lower()
        config['current_block']['start_date'] = today.isoformat()

        # Get phase-specific defaults
        phases = config.get('periodization', {}).get('phases', {})
        phase_info = phases.get(new_phase.lower(), {})

        if phase_info:
            config['current_block']['focus'] = phase_info.get('key_sessions', [])
            config['current_block']['notes'] = phase_info.get('focus', f'{new_phase} phase')

        # Save
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)

        # Log the decision
        try:
            log = load_coaching_log()
            if 'decisions' not in log:
                log['decisions'] = []
            log['decisions'].append({
                'id': f"phase_{today.isoformat()}",
                'date': today.isoformat(),
                'type': 'phase_transition',
                'decision': f"Transitioned from {old_phase} to {new_phase}",
                'rationale': notes or "Athlete ready for next phase",
                'status': 'active',
            })
            save_coaching_log(log)
        except Exception:
            pass  # Non-critical

        return json.dumps({
            'status': 'success',
            'transition': {
                'from': old_phase,
                'to': new_phase.lower(),
                'date': today.isoformat(),
            },
            'phase_info': phase_info if phase_info else 'Custom phase - no template',
            'notes': notes,
        }, indent=2)

    except Exception as e:
        logger.exception("update_phase failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_weekly_plan() -> str:
    """
    Returns the current 7-day training plan.

    The plan includes for each day:
    - planned: The planned session (type, duration, intensity, notes)
    - actual: What was actually done (filled by audit)
    - status: pending, completed, missed, modified
    """
    try:
        plan = get_current_plan()
        if not plan:
            plan = create_empty_week_template()
        return json.dumps(plan, indent=2)
    except Exception as e:
        logger.exception("get_weekly_plan failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_weekly_plan(plan_json: str) -> str:
    """
    Saves a new or updated weekly training plan.

    Args:
        plan_json: JSON string containing the plan with structure:
            {
                'days': {
                    'YYYY-MM-DD': {
                        'planned': {
                            'type': 'running',
                            'duration_mins': 45,
                            'intensity': 'easy',
                            'description': 'Easy recovery run'
                        },
                        'notes': 'Focus on form'
                    },
                    ...
                },
                'rationale': 'Why this plan was generated'
            }

    Returns confirmation or error.
    """
    try:
        plan = json.loads(plan_json)

        # Validate plan structure
        if not isinstance(plan, dict):
            return json.dumps({'error': 'Plan must be a JSON object, not ' + type(plan).__name__})
        if 'days' not in plan:
            return json.dumps({'error': "Plan must contain a 'days' key"})
        if not isinstance(plan['days'], dict):
            return json.dumps({'error': "'days' must be a dict keyed by YYYY-MM-DD date strings"})

        save_weekly_plan(plan)
        return json.dumps({
            'status': 'success',
            'message': 'Weekly plan saved',
            'last_updated': date.today().isoformat()
        })
    except json.JSONDecodeError as e:
        logger.exception("update_weekly_plan failed: invalid JSON")
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        logger.exception("update_weekly_plan failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def push_plan_to_garmin() -> str:
    """
    Push the current 7-day plan to Garmin Connect calendar.

    AUTOMATICALLY DELETES existing workouts created during plan period
    before pushing to prevent duplicates.

    Converts each session to a Garmin workout (running, cycling, strength, yoga),
    uploads it, and schedules it to the appropriate date.

    Supported workout types:
    - Running: timed segments with warmup/main/cooldown
    - Cycling: timed segments with warmup/main/cooldown
    - Strength: exercises with sets/reps, rest uses LAP BUTTON
    - Yoga/Mobility: scheduled as Yoga activity type
    - Pilates: scheduled as Pilates activity type

    Rest days are skipped.

    Returns:
        JSON summary with count of pushed workouts, dates, and any errors.
    """
    from ..workout_builder import build_workout, get_workout_type_name

    try:
        plan = get_current_plan()

        if not plan or 'days' not in plan:
            return json.dumps({'error': 'No weekly plan found. Generate a plan first.'})

        # DUPLICATE PREVENTION: Delete previously pushed workouts by stored IDs
        previous_ids = plan.get('pushed_workout_ids', [])
        deleted_count = 0

        if previous_ids:
            # Normal path: delete the specific workouts we pushed last time
            existing_workouts = garmin_api_call(lambda c: c.get_workouts())
            existing_ids = {w.get('workoutId') for w in existing_workouts}
            for wid in previous_ids:
                if wid in existing_ids:
                    try:
                        garmin_api_call(
                            lambda c, wid=wid: c.garth.delete(
                                'connectapi', f'/workout-service/workout/{wid}', api=True
                            )
                        )
                        deleted_count += 1
                    except Exception:
                        pass
        else:
            # First run after fix: no stored IDs, delete ALL workouts to clear duplicates
            existing_workouts = garmin_api_call(lambda c: c.get_workouts())
            for workout in existing_workouts:
                wid = workout.get('workoutId')
                try:
                    garmin_api_call(
                        lambda c, wid=wid: c.garth.delete(
                            'connectapi', f'/workout-service/workout/{wid}', api=True
                        )
                    )
                    deleted_count += 1
                except Exception:
                    pass

        results = {
            'status': 'success',
            'duplicates_deleted': deleted_count,
            'pushed': [],
            'skipped': [],
            'errors': [],
        }

        for date_str, day_data in plan['days'].items():
            # Get the planned session(s)
            planned = day_data.get('planned')
            if not planned:
                results['skipped'].append({'date': date_str, 'reason': 'rest day'})
                continue

            # Handle both single session and list of sessions
            sessions = planned if isinstance(planned, list) else [planned]

            # Expand sessions with nested 'sessions' array into individual sub-sessions
            expanded_sessions = []
            for session in sessions:
                if 'sessions' in session and isinstance(session.get('sessions'), list):
                    # Get parent-level fields (for nested sessions)
                    parent_exercises = session.get('exercises', [])
                    parent_intensity = session.get('intensity', 'easy')
                    # Extract sub-sessions
                    for sub in session.get('sessions', []):
                        # Skip optional sessions like pool_sauna
                        if sub.get('time') == 'optional':
                            continue
                        # Generate proper description for strength sub-sessions
                        if sub.get('type', '').lower() == 'strength' and 'description' not in sub:
                            focus = sub.get('focus', '')
                            if focus:
                                # Convert focus like "lower_posterior" to "Lower Posterior Strength"
                                focus_name = focus.replace('_', ' ').title()
                                sub['description'] = f"{focus_name} Strength"
                            else:
                                sub['description'] = "Strength Training"
                        # Generate proper description for rehab sub-sessions
                        elif sub.get('type', '').lower() == 'rehab' and 'description' not in sub:
                            sub['description'] = "Ankle Rehab"
                        # Copy parent description if sub doesn't have one (non-strength/rehab)
                        elif 'description' not in sub:
                            sub['description'] = sub.get('notes', session.get('description', ''))
                        # Copy parent intensity if sub doesn't have one
                        if 'intensity' not in sub:
                            sub['intensity'] = parent_intensity
                        # Copy exercises to strength sub-sessions
                        if sub.get('type', '').lower() == 'strength' and parent_exercises:
                            sub['exercises'] = parent_exercises
                        # Copy protocol for test sessions (FTP, threshold tests)
                        if 'protocol' in sub or 'protocol' in session:
                            sub['protocol'] = sub.get('protocol', session.get('protocol', []))
                        # Copy structure to indoor cycling sub-sessions (for technique/interval workouts)
                        if session.get('structure') and sub.get('type', '').lower() in ['indoor_cycling', 'wattbike', 'trainer']:
                            sub['structure'] = session['structure']
                            # Also copy FTP and power_targets if present
                            if session.get('ftp'):
                                sub['ftp'] = session['ftp']
                            if session.get('power_targets'):
                                sub['power_targets'] = session['power_targets']
                            if session.get('technique_goals'):
                                sub['technique_goals'] = session['technique_goals']
                        expanded_sessions.append(sub)
                else:
                    expanded_sessions.append(session)

            for session in expanded_sessions:
                workout_type = get_workout_type_name(session)

                if workout_type == 'skipped':
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'rest day - not pushed'
                    })
                    continue

                if workout_type == 'unknown':
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'unknown workout type'
                    })
                    continue

                # Build the Garmin workout
                workout = build_workout(session, date_str)

                if not workout:
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'could not convert to workout'
                    })
                    continue

                try:
                    # Upload the workout based on type
                    if workout_type == 'cycling':
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_cycling_workout(w))
                        workout_name = workout.workoutName
                    elif workout_type == 'running':
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_running_workout(w))
                        workout_name = workout.workoutName
                    elif workout_type in ['yoga', 'strength', 'swimming', 'pilates', 'padel']:
                        # Yoga, strength, swimming, pilates, padel use generic upload with dict format
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_workout(w))
                        workout_name = workout.get('workoutName', 'Workout')
                    else:
                        results['skipped'].append({
                            'date': date_str,
                            'type': workout_type,
                            'reason': 'upload method not implemented'
                        })
                        continue

                    # Get the workout ID from the upload result
                    workout_id = upload_result.get('workoutId')

                    if not workout_id:
                        results['errors'].append({
                            'date': date_str,
                            'type': workout_type,
                            'error': 'No workout ID returned from upload'
                        })
                        continue

                    # Schedule the workout to the date
                    schedule_workout(workout_id, date_str)

                    result_entry = {
                        'date': date_str,
                        'type': workout_type,
                        'workout_id': workout_id,
                        'name': workout_name
                    }

                    # Add exercise count for strength workouts
                    if workout_type == 'strength' and isinstance(workout, dict):
                        result_entry['exercise_count'] = workout.get('exercise_count', 0)

                    results['pushed'].append(result_entry)

                except Exception as e:
                    results['errors'].append({
                        'date': date_str,
                        'type': workout_type,
                        'error': str(e)
                    })

        # Store pushed workout IDs for cleanup on next push
        pushed_ids = [p['workout_id'] for p in results['pushed'] if 'workout_id' in p]
        if pushed_ids:
            plan['pushed_workout_ids'] = pushed_ids
            from ..planner import save_json_file
            save_json_file(DATA_DIR / 'weekly_plan.json', plan)

        # Build summary message
        pushed_count = len(results['pushed'])
        if pushed_count > 0:
            dates = [p['date'] for p in results['pushed']]
            types_pushed = set(p['type'] for p in results['pushed'])
            results['summary'] = f"Pushed {pushed_count} workout(s) to Garmin ({', '.join(types_pushed)}): {dates[0]} to {dates[-1]}"
        else:
            results['summary'] = "No workouts pushed (all sessions were rest days)"

        return json.dumps(results, indent=2)

    except Exception as e:
        logger.exception("push_plan_to_garmin failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_week_constraints() -> str:
    """Get constraints and requirements for building next week's plan.

    Returns blocked days, pillar requirements, phase-appropriate session
    guidelines with duration ranges and principles, key session types from
    race template, injury restrictions, and chronic compliance misses.

    The LLM assembles the actual plan using these constraints as guardrails.
    Call this before building or adjusting a weekly plan.

    Returns:
        JSON with structured constraints for week planning.
    """
    try:
        athlete = load_athlete()
        injuries = athlete.get('injury_history', [])

        # Load compliance diagnostics if available (from coaching snapshot)
        compliance_diagnostics = None
        try:
            from tools.coaching_tools import _build_compliance_diagnostics
            training_pillars = athlete.get('training_pillars')
            if training_pillars:
                history = load_fitness_history()
                daily_loads = history.get('daily_loads', {})
                if daily_loads:
                    today = date.today()
                    weekly_activities_4wk = []
                    for week_offset in range(4):
                        w_start = today - timedelta(days=today.weekday() + week_offset * 7)
                        w_end = w_start + timedelta(days=7)
                        week_acts = []
                        for day_data in daily_loads.values():
                            if not isinstance(day_data, dict):
                                continue
                            for act in day_data.get('activities', []):
                                act_date = act.get('date', '')
                                if w_start.isoformat() <= act_date < w_end.isoformat():
                                    week_acts.append(act)
                        weekly_activities_4wk.append(week_acts)
                    compliance_diagnostics = _build_compliance_diagnostics(
                        weekly_activities_4wk, training_pillars
                    )
        except Exception:
            logger.warning("Could not load compliance diagnostics", exc_info=True)

        constraints = _get_week_constraints(
            athlete=athlete,
            injuries=injuries,
            compliance_diagnostics=compliance_diagnostics,
        )

        return json.dumps(constraints, indent=2)

    except Exception as e:
        logger.exception("get_week_constraints failed")
        return json.dumps({'error': str(e)})
