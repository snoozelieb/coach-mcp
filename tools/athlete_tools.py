"""Athlete tools - profile updates, threshold/FTP settings, methodology management."""

from mcp_app import mcp
from garmin_client import garmin_api_call
from planner import load_json_file, save_json_file, load_athlete, load_methodology
from config import ATHLETE_FILE, METHODOLOGY_FILE
from datetime import date, timedelta
import json


@mcp.tool()
def update_athlete(
    section: str,
    data: str
) -> str:
    """
    Update a section of the athlete profile.

    Args:
        section: Which section to update. One of:
            - 'personal': name, age, max_hr, resting_hr, hr_zones, ftp, weight_kg
            - 'life_constraints': recurring_commitments, preferred_training_times, work_schedule
            - 'preferences': likes, dislikes, equipment, notes
            - 'coaching_notes': free-form coaching notes (string, not object)
            - 'add_commitment': add a recurring commitment
            - 'add_injury': add an injury to history
            - 'training_pillars': personalized training pillars (from onboarding)
            - 'swimming': swimming profile (experience, pace, strokes)
            - 'pilates': pilates profile (experience, focus areas)
            - 'strength_baseline': strength exercise baselines (exercises, equivalence_groups)
        data: JSON string with the data to update/add

    Examples:
        update_athlete('personal', '{"max_hr": 185, "weight_kg": 75}')
        update_athlete('add_commitment', '{"day": "Tuesday", "activity": "swimming", "time": "morning"}')
        update_athlete('add_injury', '{"date": "2026-01-01", "type": "ankle", "description": "Rolled ankle"}')
        update_athlete('preferences', '{"likes": ["MTB", "trail running"]}')
        update_athlete('coaching_notes', '"Responds well to data-driven feedback"')
        update_athlete('training_pillars', '{"based_on_persona": "endurance_athlete", "pillars": [...]}')

    Returns confirmation with updated section.
    """


    try:
        athlete = load_athlete()
        # Remove baseline data before saving (it's from athlete_baseline.json)
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        parsed_data = json.loads(data)

        if section == 'personal':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'personal data must be an object'})
            athlete.setdefault('personal', {}).update(parsed_data)
            updated = athlete['personal']

        elif section == 'life_constraints':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'life_constraints data must be an object'})
            athlete.setdefault('life_constraints', {}).update(parsed_data)
            updated = athlete['life_constraints']

        elif section == 'preferences':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'preferences data must be an object'})
            athlete.setdefault('preferences', {}).update(parsed_data)
            updated = athlete['preferences']

        elif section == 'coaching_notes':
            if not isinstance(parsed_data, str):
                return json.dumps({'error': 'coaching_notes must be a string'})
            athlete['coaching_notes'] = parsed_data
            updated = parsed_data

        elif section == 'add_commitment':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'commitment must be an object with day, activity, time'})
            required = ['day', 'activity']
            if not all(k in parsed_data for k in required):
                return json.dumps({'error': f'commitment requires: {required}'})
            athlete.setdefault('life_constraints', {}).setdefault('recurring_commitments', [])
            athlete['life_constraints']['recurring_commitments'].append(parsed_data)
            updated = parsed_data

        elif section == 'add_injury':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'injury must be an object with date, type, description'})
            required = ['date', 'type', 'description']
            if not all(k in parsed_data for k in required):
                return json.dumps({'error': f'injury requires: {required}'})
            parsed_data.setdefault('status', 'active')
            athlete.setdefault('injury_history', []).append(parsed_data)
            updated = parsed_data

        elif section == 'training_pillars':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'training_pillars must be an object with pillars array'})
            if 'pillars' not in parsed_data:
                return json.dumps({'error': 'training_pillars requires pillars array'})
            # Add metadata
            from datetime import date
            parsed_data['last_updated'] = date.today().isoformat()
            athlete['training_pillars'] = parsed_data
            updated = parsed_data

        elif section == 'swimming':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'swimming data must be an object'})
            athlete.setdefault('swimming', {}).update(parsed_data)
            updated = athlete['swimming']

        elif section == 'pilates':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'pilates data must be an object'})
            athlete.setdefault('pilates', {}).update(parsed_data)
            updated = athlete['pilates']

        elif section == 'strength_baseline':
            # Merge update into existing strength baseline
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'strength_baseline data must be an object'})
            athlete.setdefault('strength_baseline', {'exercises': {}, 'equivalence_groups': {}})
            if 'exercises' in parsed_data:
                athlete['strength_baseline']['exercises'].update(parsed_data['exercises'])
            if 'equivalence_groups' in parsed_data:
                athlete['strength_baseline']['equivalence_groups'].update(parsed_data['equivalence_groups'])
            if 'last_synced' in parsed_data:
                athlete['strength_baseline']['last_synced'] = parsed_data['last_synced']
            updated = athlete['strength_baseline']

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: personal, life_constraints, preferences, coaching_notes, add_commitment, add_injury, training_pillars, swimming, pilates, strength_baseline"
            })

        # Save updated athlete profile
        save_json_file(ATHLETE_FILE, athlete)

        return json.dumps({
            'status': 'success',
            'section': section,
            'updated': updated
        }, indent=2)

    except json.JSONDecodeError as e:
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def set_threshold_pace(
    pace: str = None,
    time_trial_mins: int = None,
    time_trial_distance_km: float = None
) -> str:
    """
    Set running threshold pace from a test result.

    The threshold pace is the running equivalent of FTP - the pace you can
    sustain for approximately 60 minutes. Pace zones are automatically
    calculated using Jack Daniels methodology.

    Provide ONE of:
    - pace: Direct pace input as "MM:SS" per km (e.g., "5:30" = 5min 30sec/km)
    - time_trial_mins + time_trial_distance_km: Calculate from a time trial
      (e.g., 30 min for 6.5 km)

    For 30-min time trials, pace is adjusted +5% (slightly slower than threshold).
    For 60-min time trials, pace equals threshold.

    Examples:
        set_threshold_pace(pace="5:15")  # Set directly to 5:15/km
        set_threshold_pace(time_trial_mins=30, time_trial_distance_km=6.2)  # From 30-min TT
        set_threshold_pace(time_trial_mins=60, time_trial_distance_km=11.5)  # From 60-min TT

    Returns the calculated threshold pace and derived pace zones.
    """


    try:
        athlete = load_athlete()
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        threshold_sec_per_km = None

        if pace:
            # Parse MM:SS format
            parts = pace.strip().split(':')
            if len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                threshold_sec_per_km = mins * 60 + secs
            else:
                return json.dumps({'error': 'pace must be in MM:SS format (e.g., "5:30")'})

        elif time_trial_mins and time_trial_distance_km:
            # Calculate pace from time trial
            total_secs = time_trial_mins * 60
            pace_sec_per_km = total_secs / time_trial_distance_km

            # Adjust for test duration (30-min TT is ~5% faster than threshold)
            if time_trial_mins <= 35:
                threshold_sec_per_km = int(pace_sec_per_km * 1.05)
            elif time_trial_mins <= 50:
                threshold_sec_per_km = int(pace_sec_per_km * 1.02)
            else:
                threshold_sec_per_km = int(pace_sec_per_km)
        else:
            return json.dumps({'error': 'Provide either pace (MM:SS) or time_trial_mins + time_trial_distance_km'})

        # Calculate pace zones using Jack Daniels methodology
        pace_zones = {
            "z1_recovery": [int(threshold_sec_per_km * 1.25), int(threshold_sec_per_km * 1.30)],
            "z2_easy": [int(threshold_sec_per_km * 1.15), int(threshold_sec_per_km * 1.24)],
            "z3_tempo": [int(threshold_sec_per_km * 1.05), int(threshold_sec_per_km * 1.14)],
            "z4_threshold": [int(threshold_sec_per_km * 0.96), int(threshold_sec_per_km * 1.04)],
            "z5_interval": [int(threshold_sec_per_km * 0.85), int(threshold_sec_per_km * 0.95)],
        }

        # Update athlete profile
        athlete.setdefault('personal', {})['threshold_pace_sec_per_km'] = threshold_sec_per_km
        athlete['personal']['pace_zones'] = pace_zones

        save_json_file(ATHLETE_FILE, athlete)

        # Format for display
        def format_pace(sec_per_km):
            mins = sec_per_km // 60
            secs = sec_per_km % 60
            return f"{mins}:{secs:02d}/km"

        zones_formatted = {
            zone: f"{format_pace(vals[1])} - {format_pace(vals[0])}"
            for zone, vals in pace_zones.items()
        }

        return json.dumps({
            'status': 'success',
            'threshold_pace': format_pace(threshold_sec_per_km),
            'threshold_sec_per_km': threshold_sec_per_km,
            'pace_zones': zones_formatted
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def set_ftp(
    ftp_watts: int = None,
    test_avg_watts: int = None,
    test_duration_mins: int = 20
) -> str:
    """
    Set cycling FTP (Functional Threshold Power) from a test result.

    FTP is the maximum power you can sustain for approximately 60 minutes.
    Power zones are automatically calculated.

    Provide ONE of:
    - ftp_watts: Direct FTP value in watts
    - test_avg_watts: Average power from a test (default assumes 20-min test)
      - 20-min test: FTP = avg_power x 0.95
      - 8-min test: FTP = avg_power x 0.90
      - 60-min test: FTP = avg_power

    Examples:
        set_ftp(ftp_watts=250)  # Set directly
        set_ftp(test_avg_watts=265, test_duration_mins=20)  # From 20-min test
        set_ftp(test_avg_watts=280, test_duration_mins=8)  # From 8-min test

    Returns the FTP value and derived power zones.
    """


    try:
        athlete = load_athlete()
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        ftp = None

        if ftp_watts:
            ftp = ftp_watts
        elif test_avg_watts:
            # Apply adjustment factor based on test duration
            if test_duration_mins <= 10:
                ftp = int(test_avg_watts * 0.90)
            elif test_duration_mins <= 25:
                ftp = int(test_avg_watts * 0.95)
            elif test_duration_mins <= 40:
                ftp = int(test_avg_watts * 0.98)
            else:
                ftp = test_avg_watts
        else:
            return json.dumps({'error': 'Provide either ftp_watts or test_avg_watts'})

        # Calculate power zones (using standard 7-zone model)
        power_zones = {
            "z1_recovery": [0, int(ftp * 0.55)],
            "z2_endurance": [int(ftp * 0.56), int(ftp * 0.75)],
            "z3_tempo": [int(ftp * 0.76), int(ftp * 0.90)],
            "z4_threshold": [int(ftp * 0.91), int(ftp * 1.05)],
            "z5_vo2max": [int(ftp * 1.06), int(ftp * 1.20)],
            "z6_anaerobic": [int(ftp * 1.21), int(ftp * 1.50)],
            "z7_neuromuscular": [int(ftp * 1.51), None],
        }

        # Update athlete profile
        athlete.setdefault('personal', {})['ftp'] = ftp
        athlete['personal']['power_zones'] = power_zones

        save_json_file(ATHLETE_FILE, athlete)

        # Format zones for display
        zones_formatted = {
            zone: f"{vals[0]}-{vals[1]}W" if vals[1] else f"{vals[0]}W+"
            for zone, vals in power_zones.items()
        }

        return json.dumps({
            'status': 'success',
            'ftp': ftp,
            'power_zones': zones_formatted
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def analyze_ftp_test(activity_id: str = None) -> str:
    """
    Analyze a completed FTP cycling test in detail.

    Provides structured analysis including:
    - Protocol phases (warmup, blowout, recovery, test, cooldown)
    - Pacing analysis (power consistency, surges, crashes)
    - FTP estimate with adjustment factor
    - Coach recommendation

    Args:
        activity_id: Specific activity ID. If omitted, finds most recent FTP test
                     (looks for cycling activities with 'ftp', 'test', or 'threshold' in name).

    Returns:
        Structured JSON with complete test analysis for coaching decisions.
    """
    try:
        today = date.today()

        # 1. FIND TEST ACTIVITY
        if activity_id:
            target_activity_id = int(activity_id)
            # Fetch activity details
            week_ago = today - timedelta(days=30)
            raw_activities = garmin_api_call(
                lambda c: c.get_activities_by_date(
                    week_ago.isoformat(),
                    today.isoformat()
                )
            )
            activity_summary = None
            for act in raw_activities:
                if act.get('activityId') == target_activity_id:
                    activity_summary = act
                    break
            if not activity_summary:
                return json.dumps({
                    'status': 'not_found',
                    'error': f'Activity {activity_id} not found in last 30 days'
                })
        else:
            # Search recent activities for FTP test
            week_ago = today - timedelta(days=30)
            raw_activities = garmin_api_call(
                lambda c: c.get_activities_by_date(
                    week_ago.isoformat(),
                    today.isoformat()
                )
            )

            # Filter: cycling + name contains FTP-related keywords
            ftp_keywords = ['ftp', 'test', 'threshold', '20min', '20-min', 'baseline']
            ftp_tests = [
                a for a in raw_activities
                if a.get('activityType', {}).get('typeKey') in ['cycling', 'indoor_cycling']
                and any(keyword in a.get('activityName', '').lower() for keyword in ftp_keywords)
            ]

            if not ftp_tests:
                return json.dumps({
                    'status': 'not_found',
                    'error': 'No FTP tests found in last 30 days. Look for cycling activities with "ftp", "test", or "threshold" in name.'
                })

            activity_summary = ftp_tests[0]  # Most recent
            target_activity_id = activity_summary.get('activityId')

        # 2. FETCH LAP DATA
        try:
            splits = garmin_api_call(lambda c: c.get_activity_splits(target_activity_id))
            laps = splits.get('lapDTOs', [])
        except Exception:
            laps = []

        # 3. EXTRACT SESSION SUMMARY
        session_summary = {
            'total_duration_mins': round(activity_summary.get('duration', 0) / 60, 1),
            'total_distance_km': round(activity_summary.get('distance', 0) / 1000, 1),
            'avg_power': activity_summary.get('avgPower'),
            'max_power': activity_summary.get('maxPower'),
            'norm_power': activity_summary.get('normPower'),
            'avg_hr': activity_summary.get('averageHR'),
            'max_hr': activity_summary.get('maxHR'),
            'avg_cadence': activity_summary.get('averageBikingCadenceInRevPerMinute'),
            'max_20min_power': activity_summary.get('max20MinPower'),
        }

        # 4. PARSE PROTOCOL PHASES FROM LAPS
        protocol_phases = []
        phase_map = {
            'WARMUP': 'warmup',
            'ACTIVE': 'active',
            'RECOVERY': 'recovery',
            'COOLDOWN': 'cooldown',
            'REST': 'rest',
        }

        for lap in laps:
            intensity = lap.get('intensityType', 'ACTIVE')
            phase_name = phase_map.get(intensity, 'active')

            protocol_phases.append({
                'phase': phase_name,
                'duration_mins': round(lap.get('duration', 0) / 60, 1),
                'avg_power': lap.get('averagePower'),
                'max_power': lap.get('maxPower'),
                'min_power': lap.get('minPower'),
                'norm_power': lap.get('normalizedPower'),
                'avg_hr': lap.get('averageHR'),
                'max_hr': lap.get('maxHR'),
                'avg_cadence': lap.get('averageBikeCadence'),
            })

        # 5. IDENTIFY TEST PORTION
        # Test laps are ACTIVE laps after recovery (typically laps 4+ in standard FTP test)
        # Find recovery lap, then get subsequent ACTIVE laps
        recovery_idx = None
        for i, phase in enumerate(protocol_phases):
            if phase['phase'] == 'recovery' and phase['duration_mins'] >= 3:
                recovery_idx = i
                break

        test_laps = []
        if recovery_idx is not None:
            for phase in protocol_phases[recovery_idx + 1:]:
                if phase['phase'] == 'active':
                    test_laps.append(phase)
                elif phase['phase'] == 'cooldown':
                    break

        # Calculate test metrics
        if test_laps:
            test_duration = sum(lap['duration_mins'] for lap in test_laps)
            test_powers = [lap['avg_power'] for lap in test_laps if lap['avg_power']]
            test_avg_power = round(sum(p * d for p, d in zip(
                [lap['avg_power'] for lap in test_laps if lap['avg_power']],
                [lap['duration_mins'] for lap in test_laps if lap['avg_power']]
            )) / test_duration, 0) if test_powers else None

            max_powers = [lap['max_power'] for lap in test_laps if lap['max_power']]
            min_powers = [lap['min_power'] for lap in test_laps if lap['min_power']]
            test_max_power = max(max_powers) if max_powers else None
            test_min_power = min(min_powers) if min_powers else None

            # Pacing analysis
            if len(test_laps) >= 2:
                first_half = test_laps[:len(test_laps)//2]
                second_half = test_laps[len(test_laps)//2:]

                first_half_avg = round(sum(l['avg_power'] for l in first_half if l['avg_power']) / len(first_half), 0) if first_half else None
                second_half_avg = round(sum(l['avg_power'] for l in second_half if l['avg_power']) / len(second_half), 0) if second_half else None
            else:
                first_half_avg = test_avg_power
                second_half_avg = test_avg_power

            # Detect surges and crashes
            surge_detected = test_max_power and test_avg_power and test_max_power > test_avg_power * 1.30
            crash_detected = test_min_power is not None and test_min_power < 100

            # Pacing verdict
            if crash_detected and surge_detected:
                pacing_verdict = f"Surged to {test_max_power}W then crashed to {test_min_power}W. Pacing error."
            elif crash_detected:
                pacing_verdict = f"Power dropped to {test_min_power}W. Blew up before completion."
            elif surge_detected:
                pacing_verdict = f"Large surge to {test_max_power}W detected. Consider steadier pacing."
            elif first_half_avg and second_half_avg and abs(first_half_avg - second_half_avg) <= 5:
                pacing_verdict = "Excellent pacing - very consistent power throughout."
            elif first_half_avg and second_half_avg and first_half_avg > second_half_avg:
                pacing_verdict = f"Started too hard ({first_half_avg}W) and faded ({second_half_avg}W)."
            else:
                pacing_verdict = "Pacing acceptable."

            # Test completion
            target_duration = 20  # Standard FTP test
            test_completed = test_duration >= target_duration - 1  # Allow 1 min tolerance
            completion_pct = round(min(100, test_duration / target_duration * 100), 1)

        else:
            # Fallback if can't identify test laps
            test_duration = 0
            test_avg_power = session_summary.get('avg_power')
            test_max_power = session_summary.get('max_power')
            test_min_power = None
            first_half_avg = None
            second_half_avg = None
            surge_detected = False
            crash_detected = False
            pacing_verdict = "Could not identify test portion from laps."
            test_completed = False
            completion_pct = 0

        # 6. ESTIMATE FTP
        # Use max_20min_power if available, otherwise estimate from test portion
        if session_summary.get('max_20min_power'):
            raw_power = round(session_summary['max_20min_power'], 0)
            adjustment_factor = 0.95
            method = '20min_garmin'
        elif test_avg_power and test_duration >= 18:
            raw_power = test_avg_power
            adjustment_factor = 0.95
            method = '20min_test'
        elif test_avg_power and test_duration >= 13:
            raw_power = test_avg_power
            adjustment_factor = 0.88  # 15-min adjustment
            method = f'{int(test_duration)}min_adjusted'
        elif test_avg_power and test_duration >= 8:
            raw_power = test_avg_power
            adjustment_factor = 0.85  # 10-min adjustment
            method = f'{int(test_duration)}min_adjusted'
        else:
            raw_power = test_avg_power or session_summary.get('avg_power')
            adjustment_factor = 0.80  # Very conservative
            method = 'estimated_conservative'

        estimated_ftp = int(raw_power * adjustment_factor) if raw_power else None

        # Confidence level
        if test_completed and not crash_detected:
            confidence = 'high'
        elif test_duration >= 15 and not crash_detected:
            confidence = 'medium'
        elif crash_detected:
            confidence = 'low'
        else:
            confidence = 'low'

        # 7. COACH RECOMMENDATION
        if crash_detected:
            suggested_ftp = int(estimated_ftp * 0.95) if estimated_ftp else None  # Extra conservative
            rationale = f"Athlete crashed during test. Set conservative FTP to ensure proper zone training."
            retest_weeks = 4
        elif not test_completed:
            suggested_ftp = int(estimated_ftp * 0.97) if estimated_ftp else None
            rationale = f"Test incomplete ({completion_pct}%). Slightly conservative FTP recommended."
            retest_weeks = 6
        else:
            suggested_ftp = estimated_ftp
            rationale = "Clean test completion. FTP estimate is reliable."
            retest_weeks = 8

        # 8. IDENTIFY BLOWOUT PHASE (first ACTIVE lap before recovery)
        blowout_phase = None
        for i, phase in enumerate(protocol_phases):
            if phase['phase'] == 'active' and recovery_idx and i < recovery_idx:
                blowout_phase = phase
                break

        # Build result
        result = {
            'status': 'success',
            'activity_id': target_activity_id,
            'test_date': activity_summary.get('startTimeLocal', '')[:10],
            'test_name': activity_summary.get('activityName'),

            'session_summary': session_summary,
            'protocol_phases': protocol_phases,

            'test_analysis': {
                'test_duration_mins': round(test_duration, 1) if test_duration else None,
                'test_avg_power': test_avg_power,
                'test_max_power': test_max_power,
                'test_min_power': test_min_power,
                'test_completed': test_completed,
                'completion_pct': completion_pct,

                'pacing': {
                    'first_half_avg': first_half_avg,
                    'second_half_avg': second_half_avg,
                    'power_drop': round(first_half_avg - second_half_avg, 0) if first_half_avg and second_half_avg else None,
                    'surge_detected': surge_detected,
                    'crash_detected': crash_detected,
                    'pacing_verdict': pacing_verdict,
                },

                'blowout_phase': {
                    'duration_mins': blowout_phase['duration_mins'] if blowout_phase else None,
                    'avg_power': blowout_phase['avg_power'] if blowout_phase else None,
                    'max_power': blowout_phase['max_power'] if blowout_phase else None,
                    'effective': blowout_phase['max_power'] > 300 if blowout_phase and blowout_phase.get('max_power') else None,
                } if blowout_phase else None,

                'hr_analysis': {
                    'peak_hr': session_summary.get('max_hr'),
                    'avg_hr': session_summary.get('avg_hr'),
                    'max_effort_likely': session_summary.get('max_hr') and session_summary['max_hr'] >= 180,
                },
            },

            'ftp_estimate': {
                'method': method,
                'raw_power': raw_power,
                'adjustment_factor': adjustment_factor,
                'estimated_ftp': estimated_ftp,
                'confidence': confidence,
            },

            'coach_recommendation': {
                'suggested_ftp': suggested_ftp,
                'rationale': rationale,
                'retest_in_weeks': retest_weeks,
            },
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


@mcp.tool()
def get_methodology() -> str:
    """
    Returns the complete training methodology.

    Includes:
    - pillars: weekly training requirements (strength sessions, mobility mins, long effort)
    - safety_constraints: max consecutive hard days, rest after race, volume increase limits
    - race_templates: training guidance for different race types

    This data controls how compliance is calculated and what the LLM considers
    when building training plans.
    """
    try:
        methodology = load_methodology()
        return json.dumps(methodology, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_methodology(
    section: str,
    data: str
) -> str:
    """
    Update a section of the training methodology.

    Args:
        section: Which section to update. One of:
            - 'pillars': training requirements (strength_sessions_per_week, mobility_minutes_per_week, etc.)
            - 'safety_constraints': training limits (max_consecutive_hard_days, etc.)
            - 'add_race_template': add a new race type template
            - 'update_race_template': update existing race template
        data: JSON string with the data to update/add

    Examples:
        update_methodology('pillars', '{"strength_sessions_per_week": 3}')
        update_methodology('safety_constraints', '{"max_consecutive_hard_days": 3}')
        update_methodology('add_race_template', '{"name": "gravel", "description": "...", "key_sessions": [...]}')

    Returns confirmation with updated section.
    """
    try:
        methodology = load_json_file(METHODOLOGY_FILE)
        parsed_data = json.loads(data)

        if section == 'pillars':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'pillars data must be an object'})
            methodology.setdefault('pillars', {}).update(parsed_data)
            updated = methodology['pillars']

        elif section == 'safety_constraints':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'safety_constraints data must be an object'})
            methodology.setdefault('safety_constraints', {}).update(parsed_data)
            updated = methodology['safety_constraints']

        elif section == 'add_race_template':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'race template must be an object'})
            if 'name' not in parsed_data:
                return json.dumps({'error': 'race template requires a name'})
            template_name = parsed_data.pop('name')
            methodology.setdefault('race_templates', {})[template_name] = parsed_data
            updated = {template_name: parsed_data}

        elif section == 'update_race_template':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'race template update must be an object'})
            if 'name' not in parsed_data:
                return json.dumps({'error': 'race template update requires a name'})
            template_name = parsed_data.pop('name')
            if template_name not in methodology.get('race_templates', {}):
                return json.dumps({'error': f"Race template '{template_name}' not found"})
            methodology['race_templates'][template_name].update(parsed_data)
            updated = {template_name: methodology['race_templates'][template_name]}

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: pillars, safety_constraints, add_race_template, update_race_template"
            })

        # Save updated methodology
        save_json_file(METHODOLOGY_FILE, methodology)

        return json.dumps({
            'status': 'success',
            'section': section,
            'updated': updated
        }, indent=2)

    except json.JSONDecodeError as e:
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})
