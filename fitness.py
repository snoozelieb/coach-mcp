"""
Fitness tracking and training load calculations.

Implements science-based metrics:
- CTL (Chronic Training Load) - 42-day exponentially weighted fitness
- ATL (Acute Training Load) - 7-day exponentially weighted fatigue
- TSB (Training Stress Balance) - Form = CTL - ATL
- ACWR (Acute:Chronic Workload Ratio) - Injury risk indicator
- Intensity distribution tracking (80/20 polarized model)

Based on research from TrainingPeaks, Firstbeat, and Norwegian Olympic methodology.
"""
import json
import math
from datetime import date, timedelta
from typing import Any
from collections import defaultdict

from config import (
    DATA_DIR,
    FITNESS_HISTORY_FILE,
    ATHLETE_FILE,
    CTL_TIME_CONSTANT_DAYS,
    ATL_TIME_CONSTANT_DAYS,
    ACWR_LOW_THRESHOLD,
    ACWR_HIGH_THRESHOLD,
    ACWR_DANGER_THRESHOLD,
    MIN_DAYS_FOR_CTL,
    MIN_DAYS_FOR_TRENDS,
    CTL_TARGETS,
    TSS_PER_HOUR_ESTIMATE,
    MAX_WEEKLY_LOAD_INCREASE_PCT,
    get_sport_group,
)
from garmin_client import garmin_api_call


def calculate_training_load(
    activity: dict[str, Any],
    athlete_max_hr: int = None,
    athlete_ftp: int = None,
) -> float:
    """
    Calculate training load (stress) for a single activity.

    Uses power-based TSS for cycling when Normalized Power and FTP are available,
    TRIMP-like calculation when HR data available, or falls back to
    duration-based estimate otherwise.

    Args:
        activity: Parsed activity dict with duration_mins, avg_hr, max_hr, type, norm_power
        athlete_max_hr: Athlete's max HR for intensity calculation
        athlete_ftp: Athlete's FTP in watts for power-based TSS

    Returns:
        Training load score (arbitrary units, higher = more stress)
    """
    duration_mins = activity.get('duration_mins', 0) or 0
    avg_hr = activity.get('avg_hr', 0) or 0
    activity_type = activity.get('type', '').lower()
    norm_power = activity.get('norm_power') or 0

    if duration_mins == 0:
        return 0.0

    # Power-based TSS for cycling when NP and FTP are available
    # TSS = (duration_secs / 3600) * (NP / FTP)^2 * 100
    sport_group = get_sport_group(activity_type)
    if sport_group == 'cycling' and norm_power > 0 and athlete_ftp and athlete_ftp > 0:
        duration_secs = duration_mins * 60
        intensity_factor = norm_power / athlete_ftp
        tss = (duration_secs / 3600) * (intensity_factor ** 2) * 100
        # Scale to match TRIMP-like units (TSS ~100 for 1hr at FTP)
        load = tss / 10
        return round(load, 1)

    # If we have HR data and athlete max HR, use HR-based calculation
    if avg_hr > 0 and athlete_max_hr and athlete_max_hr > 0:
        # Simplified TRIMP: duration * intensity factor
        # Intensity = (avg_hr / max_hr) with exponential weighting
        hr_ratio = min(avg_hr / athlete_max_hr, 1.0)
        # Exponential factor gives more weight to higher intensities
        intensity_factor = hr_ratio * math.exp(1.92 * hr_ratio)
        load = duration_mins * intensity_factor / 10  # Scale down
    else:
        # No HR data - estimate based on activity type and duration
        # Base multiplier by activity type (estimates)
        type_multipliers = {
            'running': 1.2,
            'trail_running': 1.3,
            'cycling': 0.9,
            'swimming': 1.0,
            'strength_training': 0.8,
            'hiit': 1.5,
            'interval_training': 1.4,
            'ultimate_disc': 1.3,
            'padel': 1.1,
            'yoga': 0.3,
            'pilates': 0.4,
            'stretching': 0.2,
            'walking': 0.4,
        }
        multiplier = type_multipliers.get(activity_type, 0.8)
        load = duration_mins * multiplier / 10

    return round(load, 1)


def calculate_daily_load(
    activities: list[dict[str, Any]],
    athlete_max_hr: int = None,
    athlete_ftp: int = None,
) -> float:
    """
    Calculate total training load for a day's activities.
    """
    return sum(calculate_training_load(a, athlete_max_hr, athlete_ftp) for a in activities)


def calculate_ewma(values: list[float], time_constant: int) -> float:
    """
    Calculate Exponentially Weighted Moving Average.

    Args:
        values: List of daily values, oldest first
        time_constant: Time constant in days (42 for CTL, 7 for ATL)

    Returns:
        EWMA value
    """
    if not values:
        return 0.0

    # Decay factor: k = 2 / (time_constant + 1)
    k = 2.0 / (time_constant + 1)

    ewma = values[0]
    for value in values[1:]:
        ewma = value * k + ewma * (1 - k)

    return round(ewma, 1)


def calculate_fitness_metrics(
    daily_loads: dict[str, float],
    as_of_date: date = None
) -> dict[str, Any]:
    """
    Calculate CTL, ATL, TSB, and ACWR from daily training loads.

    Args:
        daily_loads: Dict mapping date strings to training load values
        as_of_date: Calculate metrics as of this date (default: today)

    Returns:
        Dict with ctl, atl, tsb, acwr, and metadata
    """
    if as_of_date is None:
        as_of_date = date.today()

    # Build list of daily loads for the calculation window
    # Need CTL_TIME_CONSTANT_DAYS of history for accurate CTL
    start_date = as_of_date - timedelta(days=CTL_TIME_CONSTANT_DAYS + 7)

    # Create list of daily loads, filling gaps with 0
    loads_list = []
    current = start_date
    while current <= as_of_date:
        date_str = current.isoformat()
        loads_list.append(daily_loads.get(date_str, 0.0))
        current += timedelta(days=1)

    # Calculate CTL (chronic - fitness)
    ctl = calculate_ewma(loads_list, CTL_TIME_CONSTANT_DAYS)

    # Calculate ATL (acute - fatigue) - use last 7+ days
    atl_window = loads_list[-(ATL_TIME_CONSTANT_DAYS + 7):]
    atl = calculate_ewma(atl_window, ATL_TIME_CONSTANT_DAYS)

    # Calculate TSB (form) = CTL - ATL
    tsb = round(ctl - atl, 1)

    # Calculate ACWR (Acute:Chronic Workload Ratio)
    if ctl > 0:
        acwr = round(atl / ctl, 2)
    else:
        acwr = 1.0 if atl == 0 else 2.0  # No chronic fitness = high ratio

    # Determine ACWR status
    if acwr < ACWR_LOW_THRESHOLD:
        acwr_status = "low"
        acwr_risk = "Undertrained - fitness may be declining"
    elif acwr <= ACWR_HIGH_THRESHOLD:
        acwr_status = "optimal"
        acwr_risk = "Sweet spot - good balance of load and recovery"
    elif acwr <= ACWR_DANGER_THRESHOLD:
        acwr_status = "elevated"
        acwr_risk = "Elevated injury risk - consider reducing load"
    else:
        acwr_status = "danger"
        acwr_risk = "High injury risk - reduce load significantly"

    # Count days with data
    days_with_data = sum(1 for d in daily_loads.values() if d > 0)

    return {
        'ctl': ctl,
        'atl': atl,
        'tsb': tsb,
        'acwr': acwr,
        'acwr_status': acwr_status,
        'acwr_risk': acwr_risk,
        'days_analyzed': len(loads_list),
        'days_with_data': days_with_data,
        'data_sufficient': days_with_data >= MIN_DAYS_FOR_CTL,
        'as_of_date': as_of_date.isoformat(),
    }


def calculate_ctl_target(
    race_date: str,
    race_type: str,
    current_ctl: float,
    current_weekly_tss: float = None
) -> dict[str, Any]:
    """
    Calculate target CTL for race day and required weekly training.

    Uses CTL modeling to determine:
    - Target CTL based on race type
    - Weekly TSS required to reach target
    - Whether current training is on track
    - Safe load increase limits

    Args:
        race_date: Race date in YYYY-MM-DD format
        race_type: Type of race (from CTL_TARGETS keys)
        current_ctl: Current chronic training load
        current_weekly_tss: Recent weekly TSS (optional, for pace assessment)

    Returns:
        Dict with target_ctl, weekly_tss_required, hours_required, on_track, etc.
    """
    # Parse race date
    try:
        race_dt = date.fromisoformat(race_date)
    except (ValueError, TypeError):
        return {"error": f"Invalid race date: {race_date}"}

    today = date.today()
    days_until_race = (race_dt - today).days

    if days_until_race <= 0:
        return {"error": "Race date is in the past"}

    # Get target CTL for race type
    target_config = CTL_TARGETS.get(race_type, CTL_TARGETS["default"])
    target_ctl_min = target_config["min"]
    target_ctl_ideal = target_config["ideal"]

    # Use ideal target, but min is acceptable
    target_ctl = target_ctl_ideal
    ctl_gap = target_ctl - current_ctl

    # Calculate required daily TSS to reach target
    # CTL formula: CTL_new = CTL_old + (TSS - CTL_old) / time_constant
    # Rearranging: TSS = CTL_new * time_constant - CTL_old * (time_constant - 1)
    # Simplified: To raise CTL by X over D days, need avg daily TSS of approximately:
    # daily_tss = target_ctl + (ctl_gap * CTL_TIME_CONSTANT_DAYS / days_until_race)

    if days_until_race >= CTL_TIME_CONSTANT_DAYS:
        # Enough time - gradual build
        # Required TSS per day to hit target (exponential decay formula)
        required_daily_tss = target_ctl + (ctl_gap * CTL_TIME_CONSTANT_DAYS / days_until_race)
    else:
        # Not much time - need higher TSS but be careful
        required_daily_tss = target_ctl * 1.2  # Aim higher since less time for adaptation

    required_weekly_tss = round(required_daily_tss * 7, 0)
    required_weekly_hours = round(required_weekly_tss / TSS_PER_HOUR_ESTIMATE, 1)

    # Assess if on track
    on_track = current_ctl >= target_ctl_min
    ctl_deficit = max(0, target_ctl_min - current_ctl)

    # Calculate safe increase from current load
    if current_weekly_tss and current_weekly_tss > 0:
        max_safe_tss = current_weekly_tss * (1 + MAX_WEEKLY_LOAD_INCREASE_PCT / 100)
        recommended_tss = min(required_weekly_tss, max_safe_tss)
        safe_to_increase = required_weekly_tss <= max_safe_tss
    else:
        max_safe_tss = required_weekly_tss
        recommended_tss = required_weekly_tss
        safe_to_increase = True

    # Return DATA only - no prescriptions, no directives
    # LLM uses load_increase_guidance ranges to decide based on adaptation signals
    return {
        "race_date": race_date,
        "race_type": race_type,
        "race_type_description": target_config.get("description", ""),
        "days_until_race": days_until_race,
        "weeks_until_race": round(days_until_race / 7, 1),
        "current_ctl": current_ctl,
        "target_ctl_min": target_ctl_min,
        "target_ctl_ideal": target_ctl_ideal,
        "ctl_gap": round(ctl_gap, 1),
        "on_track": on_track,
        "ctl_deficit": round(ctl_deficit, 1),
        "weekly_tss_required": required_weekly_tss,
        "weekly_hours_required": required_weekly_hours,
        "daily_tss_required": round(required_daily_tss, 1),
        "current_weekly_tss": current_weekly_tss,
        "max_safe_weekly_tss": round(max_safe_tss, 0) if current_weekly_tss else None,
    }


def calculate_intensity_distribution(
    activities: list[dict[str, Any]],
    athlete_hr_zones: dict[str, list[int]] = None
) -> dict[str, Any]:
    """
    Calculate intensity distribution across training zones.

    Target is Norwegian 80/20 model:
    - 80% low intensity (Zone 1-2: recovery + aerobic)
    - 15% moderate intensity (Zone 3: tempo)
    - 5% high intensity (Zone 4-5: threshold + VO2max)

    Args:
        activities: List of activities with duration and HR data
        athlete_hr_zones: HR zone definitions from athlete profile

    Returns:
        Dict with zone distribution, compliance score, recommendations
    """
    if not activities:
        return {
            'zone_distribution': {},
            'time_in_zones_mins': {},
            'polarization_score': 0,
            'recommendation': "No activities to analyze",
        }

    # Default zones if not provided
    if not athlete_hr_zones:
        athlete_hr_zones = {
            'z1_recovery': [0, 120],
            'z2_aerobic': [120, 140],
            'z3_tempo': [140, 155],
            'z4_threshold': [155, 170],
            'z5_max': [170, 220],
        }

    # Track time in each intensity category
    time_low = 0  # Z1 + Z2
    time_moderate = 0  # Z3
    time_high = 0  # Z4 + Z5
    time_unknown = 0  # No HR data

    total_duration = 0

    for activity in activities:
        duration = activity.get('duration_mins', 0) or 0
        avg_hr = activity.get('avg_hr', 0) or 0
        total_duration += duration

        if avg_hr == 0:
            # No HR data - estimate from activity type
            activity_type = activity.get('type', '').lower()
            low_intensity_types = {'yoga', 'pilates', 'stretching', 'walking', 'breathwork'}
            high_intensity_types = {'hiit', 'interval_training', 'ultimate_disc', 'track_running'}

            if activity_type in low_intensity_types:
                time_low += duration
            elif activity_type in high_intensity_types:
                time_high += duration
            else:
                time_unknown += duration
        else:
            # Use HR to determine zone
            z2_upper = athlete_hr_zones.get('z2_aerobic', [0, 140])[1]
            z3_upper = athlete_hr_zones.get('z3_tempo', [0, 155])[1]

            if avg_hr <= z2_upper:
                time_low += duration
            elif avg_hr <= z3_upper:
                time_moderate += duration
            else:
                time_high += duration

    # Calculate percentages
    if total_duration > 0:
        # Distribute unknown time proportionally to known distribution
        known_time = time_low + time_moderate + time_high
        if known_time > 0 and time_unknown > 0:
            ratio_low = time_low / known_time
            ratio_mod = time_moderate / known_time
            ratio_high = time_high / known_time
            time_low += time_unknown * ratio_low
            time_moderate += time_unknown * ratio_mod
            time_high += time_unknown * ratio_high

        pct_low = round(time_low / total_duration * 100, 1)
        pct_moderate = round(time_moderate / total_duration * 100, 1)
        pct_high = round(time_high / total_duration * 100, 1)
    else:
        pct_low = pct_moderate = pct_high = 0

    # Calculate polarization score (how well they follow 80/20)
    # Perfect score = 100 when hitting exactly 80/15/5
    # Penalize deviation from target
    low_deviation = abs(pct_low - 80)
    moderate_deviation = abs(pct_moderate - 15)
    high_deviation = abs(pct_high - 5)
    total_deviation = low_deviation + moderate_deviation + high_deviation
    polarization_score = max(0, round(100 - total_deviation, 0))

    # Generate recommendation
    if pct_low < 70:
        recommendation = "Too much intensity - add more easy aerobic sessions"
    elif pct_low > 90:
        recommendation = "Consider adding threshold work for fitness gains"
    elif pct_high > 15:
        recommendation = "High intensity volume elevated - watch for fatigue"
    elif polarization_score >= 75:
        recommendation = "Good intensity distribution - maintain current balance"
    else:
        recommendation = "Moderate polarization - aim for more separation between easy and hard"

    return {
        'zone_distribution': {
            'low_z1_z2_pct': pct_low,
            'moderate_z3_pct': pct_moderate,
            'high_z4_z5_pct': pct_high,
        },
        'time_in_zones_mins': {
            'low': round(time_low),
            'moderate': round(time_moderate),
            'high': round(time_high),
        },
        'total_duration_mins': round(total_duration),
        'polarization_score': int(polarization_score),
        'target_distribution': '80% low / 15% moderate / 5% high',
        'recommendation': recommendation,
    }


def migrate_fitness_history(history: dict[str, Any]) -> dict[str, Any]:
    """
    Migrate fitness history from schema v1 (flat) to v2 (sport-aware).

    Non-destructive: old data is preserved, just restructured.
    v1 daily_loads: {"2026-02-02": 17.1}
    v2 daily_loads: {"2026-02-02": {"total": 17.1, "by_sport": {}, "activities": []}}
    """
    if history.get('schema_version', 0) >= 2:
        return history  # Already migrated

    daily_loads = history.get('daily_loads', {})
    migrated_loads = {}

    for date_str, load_val in daily_loads.items():
        if isinstance(load_val, (int, float)):
            # v1 format: flat float
            migrated_loads[date_str] = {
                'total': load_val,
                'by_sport': {},  # No sport breakdown for historical data
                'activities': [],
            }
        else:
            # Already v2 format (dict)
            migrated_loads[date_str] = load_val

    # Migrate snapshots
    snapshots = history.get('snapshots', [])
    migrated_snapshots = []
    for snap in snapshots:
        if 'total' not in snap and 'ctl' in snap:
            # v1 format: flat metrics
            migrated_snapshots.append({
                'date': snap['date'],
                'total': {
                    'ctl': snap.get('ctl', 0),
                    'atl': snap.get('atl', 0),
                    'tsb': snap.get('tsb', 0),
                    'acwr': snap.get('acwr', 0),
                },
            })
        else:
            migrated_snapshots.append(snap)

    history['daily_loads'] = migrated_loads
    history['snapshots'] = migrated_snapshots
    history['schema_version'] = 2
    if 'sleep_history' not in history:
        history['sleep_history'] = []

    return history


def load_fitness_history() -> dict[str, Any]:
    """Load fitness history from file, auto-migrating to v2 if needed."""
    history_path = DATA_DIR / FITNESS_HISTORY_FILE
    if history_path.exists():
        with open(history_path) as f:
            history = json.load(f)
        return migrate_fitness_history(history)
    return {
        'schema_version': 2,
        'daily_loads': {},
        'snapshots': [],
        'sleep_history': [],
        'last_updated': None,
    }


def save_fitness_history(history: dict[str, Any]) -> None:
    """Save fitness history to file."""
    history_path = DATA_DIR / FITNESS_HISTORY_FILE
    history['last_updated'] = date.today().isoformat()
    with open(history_path, 'w') as f:
        json.dump(history, f, indent=2)


def update_fitness_history(
    activities: list[dict[str, Any]],
    athlete_max_hr: int = None,
    athlete_ftp: int = None,
) -> dict[str, Any]:
    """
    Update fitness history with new activity data (v2 sport-aware format).

    Args:
        activities: List of parsed activities with date, type, HR, power data
        athlete_max_hr: Athlete's max HR for load calculation
        athlete_ftp: Athlete's FTP in watts for power-based TSS

    Returns:
        Updated fitness history dict
    """
    history = load_fitness_history()
    daily_loads = history.get('daily_loads', {})

    # Group activities by date
    activities_by_date = defaultdict(list)
    for activity in activities:
        activity_date = activity.get('date', '')
        if activity_date:
            activities_by_date[activity_date].append(activity)

    # Calculate load for each day in v2 format
    for date_str, day_activities in activities_by_date.items():
        by_sport = defaultdict(float)
        activity_details = []

        for act in day_activities:
            load = calculate_training_load(act, athlete_max_hr, athlete_ftp)
            sport = get_sport_group(act.get('type', ''))
            by_sport[sport] += load
            activity_details.append({
                'id': act.get('activity_id'),
                'type': act.get('type', 'unknown'),
                'sport': sport,
                'duration_mins': act.get('duration_mins', 0),
                'load': load,
                'avg_hr': act.get('avg_hr'),
                'norm_power': act.get('norm_power'),
            })

        total_load = sum(by_sport.values())
        daily_loads[date_str] = {
            'total': round(total_load, 1),
            'by_sport': {k: round(v, 1) for k, v in by_sport.items()},
            'activities': activity_details,
        }

    history['daily_loads'] = daily_loads

    # Calculate overall metrics from total loads
    total_loads_flat = _extract_total_loads(daily_loads)
    metrics = calculate_fitness_metrics(total_loads_flat)

    # Calculate per-sport metrics for the snapshot
    sport_metrics = {}
    for sport in ['cycling', 'running', 'strength']:
        sport_loads = _extract_sport_loads(daily_loads, sport)
        if any(v > 0 for v in sport_loads.values()):
            sm = calculate_fitness_metrics(sport_loads)
            sport_metrics[sport] = {
                'ctl': sm['ctl'],
                'atl': sm['atl'],
                'tsb': sm['tsb'],
                'acwr': sm['acwr'],
            }

    # Build v2 snapshot
    snapshot = {
        'date': metrics['as_of_date'],
        'total': {
            'ctl': metrics['ctl'],
            'atl': metrics['atl'],
            'tsb': metrics['tsb'],
            'acwr': metrics['acwr'],
        },
    }
    snapshot.update(sport_metrics)

    # Keep last 90 days of snapshots
    snapshots = history.get('snapshots', [])
    snapshots.append(snapshot)
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    snapshots = [s for s in snapshots if s['date'] >= cutoff]
    history['snapshots'] = snapshots

    save_fitness_history(history)
    return history


def _extract_total_loads(daily_loads: dict[str, Any]) -> dict[str, float]:
    """Extract flat {date: total_load} from v2 daily_loads for overall CTL/ATL."""
    flat = {}
    for date_str, val in daily_loads.items():
        if isinstance(val, dict):
            flat[date_str] = val.get('total', 0.0)
        else:
            flat[date_str] = float(val)  # v1 fallback
    return flat


def _extract_sport_loads(daily_loads: dict[str, Any], sport: str) -> dict[str, float]:
    """Extract flat {date: sport_load} from v2 daily_loads for sport-specific CTL/ATL."""
    flat = {}
    for date_str, val in daily_loads.items():
        if isinstance(val, dict):
            flat[date_str] = val.get('by_sport', {}).get(sport, 0.0)
        else:
            flat[date_str] = 0.0  # v1 data has no sport breakdown
    return flat


def calculate_sport_fitness_metrics(
    daily_loads: dict[str, Any],
    sport: str,
    as_of_date: date = None,
) -> dict[str, Any]:
    """
    Calculate CTL/ATL/TSB/ACWR for a specific sport.

    Extracts that sport's load from each day's by_sport dict and runs
    the same EWMA calculation used for overall metrics.

    Args:
        daily_loads: v2 daily_loads dict
        sport: Sport group name ('cycling', 'running', 'strength')
        as_of_date: Calculate as of this date (default: today)

    Returns:
        Dict with sport-specific ctl, atl, tsb, acwr, acwr_status
    """
    sport_loads = _extract_sport_loads(daily_loads, sport)
    metrics = calculate_fitness_metrics(sport_loads, as_of_date)
    return metrics


def get_sleep_trend(history: dict[str, Any] = None, days: int = 30) -> dict[str, Any]:
    """
    Get sleep trend from persisted sleep_history.

    Args:
        history: Fitness history dict (loads from file if None)
        days: Number of days to analyze (default 30)

    Returns:
        Dict with avg_duration, avg_score, direction, weeks_in_deficit
    """
    if history is None:
        history = load_fitness_history()

    sleep_records = history.get('sleep_history', [])
    if not sleep_records:
        return {
            'status': 'no_data',
            'note': 'No persisted sleep data. Sleep history builds as coaching snapshots are taken.',
        }

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = sorted(
        [r for r in sleep_records if r.get('date', '') >= cutoff],
        key=lambda r: r.get('date', ''),
    )

    if not recent:
        return {
            'status': 'no_data',
            'note': f'No sleep data in last {days} days',
        }

    avg_duration = round(
        sum(r.get('duration_hrs', 0) for r in recent) / len(recent), 1
    )
    scores = [r.get('score') for r in recent if r.get('score')]
    avg_score = round(sum(scores) / len(scores), 0) if scores else None

    # Determine direction by comparing first half vs second half
    mid = len(recent) // 2
    if mid >= 2:
        first_half_avg = sum(r.get('duration_hrs', 0) for r in recent[:mid]) / mid
        second_half_avg = sum(r.get('duration_hrs', 0) for r in recent[mid:]) / (len(recent) - mid)
        diff = second_half_avg - first_half_avg
        if diff > 0.3:
            direction = 'improving'
        elif diff < -0.3:
            direction = 'declining'
        else:
            direction = 'stable'
    else:
        direction = 'unknown'

    # Count weeks in deficit (avg < 7hrs per week)
    weeks_in_deficit = 0
    week_groups = defaultdict(list)
    for r in recent:
        try:
            d = date.fromisoformat(r['date'])
            week_key = d.isocalendar()[1]
            week_groups[week_key].append(r.get('duration_hrs', 0))
        except (ValueError, KeyError):
            pass

    for week_durations in week_groups.values():
        if week_durations:
            week_avg = sum(week_durations) / len(week_durations)
            if week_avg < 7.0:
                weeks_in_deficit += 1

    return {
        'avg_duration': avg_duration,
        'avg_score': avg_score,
        'direction': direction,
        'weeks_in_deficit': weeks_in_deficit,
        'days_analyzed': len(recent),
    }


def persist_sleep_data(sleep_records: list[dict], history: dict[str, Any] = None) -> dict[str, Any]:
    """
    Save nightly sleep records to fitness_history.json → sleep_history.

    Stores: date, duration_hrs, score, deep_pct, rem_pct, avg_hr.
    Maintains a rolling 30-day window (auto-prunes older entries).

    Args:
        sleep_records: List of sleep record dicts from get_sleep_summary
        history: Fitness history dict (loads from file if None)

    Returns:
        Updated fitness history dict
    """
    if history is None:
        history = load_fitness_history()

    existing = history.get('sleep_history', [])
    existing_dates = {r['date'] for r in existing}

    for rec in sleep_records:
        rec_date = rec.get('date')
        if not rec_date or rec_date in existing_dates:
            continue
        existing.append({
            'date': rec_date,
            'duration_hrs': rec.get('duration_hrs'),
            'score': rec.get('score'),
            'deep_pct': rec.get('deep_pct'),
            'rem_pct': rec.get('rem_pct'),
            'avg_hr': rec.get('avg_hr'),
        })
        existing_dates.add(rec_date)

    # Prune to 30 days
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    existing = [r for r in existing if r.get('date', '') >= cutoff]
    existing.sort(key=lambda r: r.get('date', ''))

    history['sleep_history'] = existing
    return history


def analyze_activity_patterns(
    daily_loads: dict[str, Any],
    today: date = None,
    days: int = 28,
) -> dict[str, Any]:
    """
    Analyze activity patterns from stored fitness history.

    Returns last activity date by sport, sessions per week by sport,
    and alerts for concerning patterns.
    """
    if today is None:
        today = date.today()

    cutoff = (today - timedelta(days=days)).isoformat()

    # Track last activity and weekly sessions per sport
    last_activity_by_sport = {}
    weekly_sessions = defaultdict(lambda: defaultdict(int))

    for date_str, day_data in daily_loads.items():
        if date_str < cutoff:
            continue
        if not isinstance(day_data, dict):
            continue

        for act in day_data.get('activities', []):
            sport = act.get('sport', 'other')
            act_date = date_str

            # Track last activity
            if sport not in last_activity_by_sport or act_date > last_activity_by_sport[sport]:
                last_activity_by_sport[sport] = act_date

            # Track weekly counts
            try:
                d = date.fromisoformat(date_str)
                week_idx = (today - d).days // 7  # 0 = this week, 1 = last week, etc.
                if week_idx < 4:
                    weekly_sessions[sport][week_idx] += 1
            except ValueError:
                pass

    # Build last_activity summary
    last_activity_summary = {}
    for sport, last_date in last_activity_by_sport.items():
        try:
            d = date.fromisoformat(last_date)
            days_ago = (today - d).days
        except ValueError:
            days_ago = None
        last_activity_summary[sport] = {
            'date': last_date,
            'days_ago': days_ago,
        }

    # Build sessions per week (4 weeks, oldest first)
    sessions_per_week = {}
    for sport in ['cycling', 'running', 'strength']:
        weeks = []
        for week_idx in range(3, -1, -1):  # oldest to newest
            weeks.append(weekly_sessions[sport].get(week_idx, 0))
        sessions_per_week[sport] = weeks

    # Generate alerts
    alerts = []
    for sport in ['cycling', 'running', 'strength']:
        info = last_activity_summary.get(sport)
        if info and info['days_ago'] is not None and info['days_ago'] > 14:
            alerts.append(
                f"No {sport} in {info['days_ago']} days. "
                f"Return-to-{sport} protocol may be needed."
            )
        elif sport not in last_activity_summary and sport != 'strength':
            alerts.append(
                f"No {sport} activity recorded in last {days} days."
            )

        # Check trending down
        weeks = sessions_per_week.get(sport, [0, 0, 0, 0])
        if len(weeks) >= 3 and weeks[-1] < weeks[-3] and weeks[-3] > 0:
            alerts.append(
                f"{sport.capitalize()} sessions trending down: "
                f"{weeks[-3]}→{weeks[-1]}/week over last 3 weeks."
            )

    return {
        'last_activity_by_sport': last_activity_summary,
        'sessions_per_week_4wk': sessions_per_week,
        'alerts': alerts,
    }


def get_fitness_trend(days: int = 28) -> dict[str, Any]:
    """
    Get fitness trend over specified period.

    Args:
        days: Number of days to analyze

    Returns:
        Dict with CTL trend, direction, and projection
    """
    history = load_fitness_history()
    snapshots = history.get('snapshots', [])

    if len(snapshots) < 2:
        return {
            'trend': 'unknown',
            'ctl_change': 0,
            'data_points': len(snapshots),
            'note': 'Insufficient data for trend analysis',
        }

    # Get snapshots within the period
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent_snapshots = [s for s in snapshots if s['date'] >= cutoff]

    if len(recent_snapshots) < 2:
        return {
            'trend': 'unknown',
            'ctl_change': 0,
            'data_points': len(recent_snapshots),
            'note': f'Need more data points in last {days} days',
        }

    # Calculate trend (handle both v1 flat and v2 nested snapshots)
    def _snap_ctl(snap):
        if 'total' in snap:
            return snap['total'].get('ctl', 0)
        return snap.get('ctl', 0)

    first_ctl = _snap_ctl(recent_snapshots[0])
    last_ctl = _snap_ctl(recent_snapshots[-1])
    ctl_change = last_ctl - first_ctl

    if ctl_change > 5:
        trend = 'building'
        trend_note = 'Fitness is increasing'
    elif ctl_change < -5:
        trend = 'declining'
        trend_note = 'Fitness is decreasing'
    else:
        trend = 'maintaining'
        trend_note = 'Fitness is stable'

    # Project future CTL if trend continues
    days_in_period = len(recent_snapshots)
    daily_change = ctl_change / days_in_period if days_in_period > 0 else 0
    projected_30_day = round(last_ctl + (daily_change * 30), 1)

    return {
        'trend': trend,
        'trend_note': trend_note,
        'ctl_start': first_ctl,
        'ctl_current': last_ctl,
        'ctl_change': round(ctl_change, 1),
        'ctl_change_pct': round((ctl_change / first_ctl * 100) if first_ctl > 0 else 0, 1),
        'projected_ctl_30_days': projected_30_day,
        'data_points': len(recent_snapshots),
        'period_days': days,
    }


def get_load_athlete_max_hr() -> int | None:
    """Load athlete's max HR from profile."""
    athlete_path = DATA_DIR / ATHLETE_FILE
    if athlete_path.exists():
        with open(athlete_path) as f:
            athlete = json.load(f)
        return athlete.get('personal', {}).get('max_hr')
    return None


def get_athlete_hr_zones() -> dict[str, list[int]] | None:
    """Load athlete's HR zones from profile."""
    athlete_path = DATA_DIR / ATHLETE_FILE
    if athlete_path.exists():
        with open(athlete_path) as f:
            athlete = json.load(f)
        return athlete.get('personal', {}).get('hr_zones')
    return None


def get_sleep_summary(today: date, days: int = 7) -> dict:
    """
    Get comprehensive sleep analysis for the last N days.

    This is a CORE coaching metric - sleep quality and quantity directly
    determine whether training adaptations can occur.

    Analyzes:
    - Duration vs personalized need (Garmin calculates based on training load)
    - Quality: deep sleep %, REM %, sleep stress, awake count
    - Consistency: variance night to night
    - Accumulated deficit

    Without adequate sleep, training is CATABOLIC not ANABOLIC.

    Args:
        today: Current date
        days: Number of days to analyze (default 7)

    Returns:
        Dict with sleep status, metrics, quality issues, and training modifications
    """
    sleep_records = []
    personalized_need_mins = None  # Garmin's calculated need based on training
    baseline_need_mins = None  # Athlete's base sleep need
    need_feedback = None  # Garmin's feedback on sleep need ("HIGHLY_INCREASED" etc)
    training_impact = None  # How chronic training is affecting sleep need

    for i in range(days):
        d = today - timedelta(days=i)
        try:
            sleep = garmin_api_call(lambda c, ds=d.isoformat(): c.get_sleep_data(ds))
            if sleep and sleep.get('dailySleepDTO'):
                dto = sleep['dailySleepDTO']
                scores = dto.get('sleepScores', {})

                duration_secs = dto.get('sleepTimeSeconds', 0)
                if not duration_secs or duration_secs <= 0:
                    continue

                # Extract quality metrics
                deep_secs = dto.get('deepSleepSeconds', 0)
                rem_secs = dto.get('remSleepSeconds', 0)
                light_secs = dto.get('lightSleepSeconds', 0)
                awake_secs = dto.get('awakeSleepSeconds', 0)

                # Extract nap data for this day
                nap_secs = dto.get('napTimeSeconds', 0) or 0
                nap_dtos = dto.get('dailyNapDTOS', []) or []
                nap_count = len(nap_dtos)

                record = {
                    'date': d.isoformat(),
                    'duration_hrs': round(duration_secs / 3600, 1),
                    'score': scores.get('overall', {}).get('value'),
                    'quality': scores.get('overall', {}).get('qualifierKey'),
                    # Quality breakdown
                    'deep_pct': round(deep_secs / duration_secs * 100, 0) if duration_secs else 0,
                    'deep_quality': scores.get('deepPercentage', {}).get('qualifierKey'),
                    'rem_pct': round(rem_secs / duration_secs * 100, 0) if duration_secs else 0,
                    'rem_quality': scores.get('remPercentage', {}).get('qualifierKey'),
                    'awake_mins': round(awake_secs / 60, 0),
                    'awake_count': dto.get('awakeCount', 0),
                    # Stress and restlessness
                    'sleep_stress': dto.get('avgSleepStress'),
                    'stress_quality': scores.get('stress', {}).get('qualifierKey'),
                    'restlessness': scores.get('restlessness', {}).get('qualifierKey'),
                    # Recovery indicators
                    'avg_hr': dto.get('avgHeartRate'),
                    'respiration': dto.get('averageRespirationValue'),
                    # Nap data
                    'nap_mins': round(nap_secs / 60, 0),
                    'nap_count': nap_count,
                }
                sleep_records.append(record)

                # Get personalized sleep need (most recent)
                if i == 0:  # Last night
                    sleep_need = dto.get('sleepNeed', {})
                    if sleep_need:
                        personalized_need_mins = sleep_need.get('actual')  # Training-adjusted need
                        baseline_need_mins = sleep_need.get('baseline')  # Base need without training
                        need_feedback = sleep_need.get('feedback')  # "HIGHLY_INCREASED" etc
                        training_impact = sleep_need.get('trainingFeedback')  # "CHRONIC" load impact

        except Exception:
            continue

    if not sleep_records:
        return {'status': 'no_data', 'note': 'Could not fetch sleep data'}

    # Calculate averages (7-day)
    avg_duration = round(sum(r['duration_hrs'] for r in sleep_records) / len(sleep_records), 1)
    scores_with_values = [r['score'] for r in sleep_records if r.get('score')]
    avg_score = round(sum(scores_with_values) / len(scores_with_values), 0) if scores_with_values else None
    avg_deep_pct = round(sum(r['deep_pct'] for r in sleep_records) / len(sleep_records), 0)
    avg_rem_pct = round(sum(r['rem_pct'] for r in sleep_records) / len(sleep_records), 0)

    # Nap totals
    total_nap_mins = sum(r.get('nap_mins', 0) for r in sleep_records)
    today_nap_mins = sleep_records[0].get('nap_mins', 0) if sleep_records else 0

    # ACUTE READINESS: Recent nights matter more for "can I do an FTP test tomorrow?"
    # Weight last 2-3 nights heavily for acute training decisions
    recent_nights = sleep_records[:3]  # Last 3 nights (most recent first)
    if recent_nights:
        recent_scores = [r['score'] for r in recent_nights if r.get('score')]
        recent_avg_score = round(sum(recent_scores) / len(recent_scores), 0) if recent_scores else None
        recent_avg_duration = round(sum(r['duration_hrs'] for r in recent_nights) / len(recent_nights), 1)
        recent_avg_deep = round(sum(r['deep_pct'] for r in recent_nights) / len(recent_nights), 0)
        # Trend: is sleep improving? (positive = getting better)
        if len(recent_nights) >= 2:
            trend = recent_nights[0]['duration_hrs'] - recent_nights[-1]['duration_hrs']
        else:
            trend = 0
    else:
        recent_avg_score = avg_score
        recent_avg_duration = avg_duration
        recent_avg_deep = avg_deep_pct
        trend = 0

    # Use personalized need if available, otherwise use athlete default
    if personalized_need_mins:
        target_hrs = round(personalized_need_mins / 60, 1)
        target_source = 'garmin_personalized'
    else:
        target_hrs = 7.5  # Fallback
        target_source = 'default'

    # Calculate deficit against PERSONALIZED target
    daily_deficit = target_hrs - avg_duration
    weekly_deficit = round(daily_deficit * 7, 1)

    # Quality assessment (not just duration)
    quality_issues = []

    # Deep sleep check (optimal 16-33% for adults)
    if avg_deep_pct < 15:
        quality_issues.append(f'Low deep sleep ({avg_deep_pct}%) - physical recovery impaired')
    elif avg_deep_pct < 20:
        quality_issues.append(f'Borderline deep sleep ({avg_deep_pct}%)')

    # REM check (optimal 21-31%)
    if avg_rem_pct < 18:
        quality_issues.append(f'Low REM ({avg_rem_pct}%) - cognitive recovery impaired')

    # Count poor quality nights
    poor_quality_nights = len([r for r in sleep_records if r.get('quality') in ['POOR']])
    fair_quality_nights = len([r for r in sleep_records if r.get('quality') in ['FAIR']])

    # Consistency check (high variance = poor sleep hygiene)
    durations = [r['duration_hrs'] for r in sleep_records]
    if len(durations) > 1:
        variance = max(durations) - min(durations)
        if variance > 2:
            quality_issues.append(f'Inconsistent sleep ({variance:.1f}hr variance) - poor sleep hygiene')

    # CHRONIC STATUS: 7-day average for overall training load decisions
    quantity_ok = avg_duration >= (target_hrs - 0.5)  # Within 30min of target
    quality_ok = avg_score and avg_score >= 70 and avg_deep_pct >= 15

    # ACUTE STATUS: Recent nights (last 2-3) for "can I do hard session tomorrow?"
    # High scores (80+) with good deep sleep can override moderate duration shortfall
    recent_quality_excellent = recent_avg_score and recent_avg_score >= 80 and recent_avg_deep >= 18
    recent_quality_good = recent_avg_score and recent_avg_score >= 75 and recent_avg_deep >= 15
    recent_duration_ok = recent_avg_duration >= 6.5
    improving_trend = trend > 0.3  # Getting noticeably more sleep

    # Nap bonus: a nap today adds to acute recovery capacity
    nap_recovery_boost = today_nap_mins >= 15  # 15+ min nap counts

    # CHRONIC status (7-day) - unchanged thresholds
    if avg_duration < 6:
        chronic_status = 'severe_deficit'
    elif avg_duration < 6.5 or (avg_score and avg_score < 50):
        chronic_status = 'severe_deficit' if not quality_ok else 'deficit'
    elif avg_duration < target_hrs - 0.5:
        chronic_status = 'deficit'
    elif avg_duration < target_hrs:
        chronic_status = 'borderline' if quality_ok else 'deficit'
    elif quality_ok:
        chronic_status = 'adequate'
    else:
        chronic_status = 'quality_issue'

    # ACUTE status (recent nights) - can be better than chronic if recent sleep is good
    # Key insight: 6.5hrs with score 86 is BETTER than 7.5hrs with score 60
    if recent_quality_excellent and recent_duration_ok:
        acute_status = 'ready'  # Good to go for hard efforts
    elif recent_quality_good and (recent_duration_ok or nap_recovery_boost):
        acute_status = 'ready'  # Scores override moderate duration shortfall
    elif recent_quality_good and improving_trend:
        acute_status = 'cautious'  # Trending right way, proceed with monitoring
    elif recent_avg_duration < 6 and not nap_recovery_boost:
        acute_status = 'not_ready'  # Recent nights too short
    else:
        acute_status = 'cautious'  # Default to cautious

    # Final status combines both views - use the more relevant one for context
    # Chronic status drives volume decisions, acute status drives intensity decisions
    status = chronic_status  # Keep backward compatibility

    # Generate recommendation based on BOTH chronic and acute status
    # Chronic status commentary
    if chronic_status == 'severe_deficit':
        chronic_note = f'CRITICAL: Severe sleep deficit. Training is catabolic.'
    elif chronic_status == 'deficit':
        chronic_note = f'Sleep deficit ({weekly_deficit:.0f}hrs/week vs target {target_hrs}hrs/night).'
    elif chronic_status == 'quality_issue':
        chronic_note = f'Duration OK but quality poor. Focus on sleep hygiene.'
    elif chronic_status == 'borderline':
        chronic_note = f'Close to target. Add 30 mins tonight.'
    else:
        chronic_note = 'Sleep adequate for training adaptation.'

    # Acute readiness commentary
    if acute_status == 'ready':
        if nap_recovery_boost:
            acute_note = f'Recent sleep good (avg score {recent_avg_score}, {recent_avg_duration}hrs) + nap today. Ready for hard efforts.'
        else:
            acute_note = f'Recent sleep good (avg score {recent_avg_score}, {recent_avg_duration}hrs). Ready for hard efforts.'
    elif acute_status == 'cautious':
        acute_note = f'Recent sleep mixed. Proceed with intensity but monitor how you feel.'
    else:
        acute_note = f'Recent sleep poor ({recent_avg_duration}hrs avg). Skip max efforts today.'

    # Combined recommendation
    if acute_status == 'ready' and chronic_status in ['deficit', 'borderline']:
        recommendation = f'{chronic_note} But recent nights are solid - {acute_note.lower()}'
    elif acute_status == 'not_ready':
        recommendation = f'{acute_note} {chronic_note}'
    else:
        recommendation = f'{chronic_note} {acute_note}'

    # Training modifications - ACUTE status drives intensity, CHRONIC drives volume
    # Key change: use acute_status for skip_sessions decisions
    if chronic_status == 'severe_deficit':
        # Severe chronic deficit overrides everything
        training_modifications = {
            'intensity_cap': 'recovery_only',
            'skip_sessions': ['ftp_test', 'intervals', 'hiit', 'tempo', 'threshold', 'race', 'time_trial'],
            'allowed_sessions': ['easy_ride', 'yoga', 'mobility', 'walking', 'easy_swim'],
            'early_am_workouts': 'BANNED - sleep is medicine right now',
            'volume_modifier': 0.5,
            'rationale': 'Severe deficit: your body cannot adapt. Training adds stress without benefit.',
        }
    elif acute_status == 'ready':
        # Recent nights are good - allow hard efforts even if chronic shows deficit
        training_modifications = {
            'intensity_cap': 'none',
            'skip_sessions': [],
            'allowed_sessions': ['all'],
            'early_am_workouts': 'OK if slept 7+ hrs last night',
            'volume_modifier': 0.9 if chronic_status == 'deficit' else 1.0,  # Slightly reduce volume if chronic deficit
            'rationale': f'Recent sleep supports hard efforts (score {recent_avg_score}, {recent_avg_duration}hrs avg).',
        }
    elif acute_status == 'cautious':
        training_modifications = {
            'intensity_cap': 'moderate',
            'skip_sessions': ['race_simulation', 'vo2max'],  # Skip only the hardest
            'allowed_sessions': ['ftp_test', 'intervals', 'strength', 'tempo', 'easy_ride', 'mobility'],
            'early_am_workouts': 'Only if 7+ hrs achieved',
            'volume_modifier': 0.85,
            'rationale': 'Mixed recent sleep. FTP test OK but monitor fatigue.',
        }
    else:  # acute_status == 'not_ready'
        training_modifications = {
            'intensity_cap': 'low',
            'skip_sessions': ['ftp_test', 'max_efforts', 'race_simulation', 'vo2max', 'intervals'],
            'allowed_sessions': ['easy_ride', 'strength', 'mobility', 'easy_run', 'swim'],
            'early_am_workouts': 'AVOID - prioritize sleep',
            'volume_modifier': 0.7,
            'rationale': 'Recent sleep inadequate. Save hard efforts for when rested.',
        }

    return {
        'status': status,  # Chronic status (backward compatible)
        'acute_status': acute_status,  # NEW: ready/cautious/not_ready for hard efforts
        'days_analyzed': len(sleep_records),

        # Quantity (7-day average)
        'avg_duration_hrs': avg_duration,
        'target_hrs': target_hrs,
        'target_source': target_source,
        'daily_deficit_hrs': round(max(0, daily_deficit), 1),
        'weekly_deficit_hrs': round(max(0, weekly_deficit), 1),

        # Garmin's sleep need analysis (valuable coaching context)
        'baseline_need_hrs': round(baseline_need_mins / 60, 1) if baseline_need_mins else None,
        'need_feedback': need_feedback,  # e.g., "HIGHLY_INCREASED"
        'training_impact_on_sleep': training_impact,  # How chronic load affects sleep need

        # Quality (7-day)
        'avg_score': avg_score,
        'avg_deep_pct': avg_deep_pct,
        'avg_rem_pct': avg_rem_pct,
        'poor_quality_nights': poor_quality_nights,
        'fair_quality_nights': fair_quality_nights,
        'quality_issues': quality_issues,

        # NEW: Recent nights analysis (for acute decisions)
        'recent_avg_score': recent_avg_score,
        'recent_avg_duration': recent_avg_duration,
        'recent_avg_deep': recent_avg_deep,
        'recent_trend': round(trend, 1),  # Positive = improving

        # NEW: Nap data
        'today_nap_mins': today_nap_mins,
        'weekly_nap_mins': total_nap_mins,
        'nap_recovery_boost': nap_recovery_boost,

        # Recent nights (detailed)
        'recent': sleep_records[:3],

        # Coaching output
        'recommendation': recommendation,
        'training_modifications': training_modifications,
    }
