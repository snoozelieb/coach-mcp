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
)


def calculate_training_load(activity: dict[str, Any], athlete_max_hr: int = None) -> float:
    """
    Calculate training load (stress) for a single activity.

    Uses TRIMP-like calculation when HR data available, falls back to
    duration-based estimate otherwise.

    Args:
        activity: Parsed activity dict with duration_mins, avg_hr, max_hr, type
        athlete_max_hr: Athlete's max HR for intensity calculation

    Returns:
        Training load score (arbitrary units, higher = more stress)
    """
    duration_mins = activity.get('duration_mins', 0) or 0
    avg_hr = activity.get('avg_hr', 0) or 0
    activity_type = activity.get('type', '').lower()

    if duration_mins == 0:
        return 0.0

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


def calculate_daily_load(activities: list[dict[str, Any]], athlete_max_hr: int = None) -> float:
    """
    Calculate total training load for a day's activities.
    """
    return sum(calculate_training_load(a, athlete_max_hr) for a in activities)


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


def load_fitness_history() -> dict[str, Any]:
    """Load fitness history from file."""
    history_path = DATA_DIR / FITNESS_HISTORY_FILE
    if history_path.exists():
        with open(history_path) as f:
            return json.load(f)
    return {
        'daily_loads': {},
        'snapshots': [],
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
    athlete_max_hr: int = None
) -> dict[str, Any]:
    """
    Update fitness history with new activity data.

    Args:
        activities: List of activities with date and training data
        athlete_max_hr: Athlete's max HR for load calculation

    Returns:
        Updated fitness history dict
    """
    history = load_fitness_history()
    daily_loads = history.get('daily_loads', {})

    # Group activities by date and calculate daily loads
    activities_by_date = defaultdict(list)
    for activity in activities:
        activity_date = activity.get('date', '')
        if activity_date:
            activities_by_date[activity_date].append(activity)

    # Calculate load for each day
    for date_str, day_activities in activities_by_date.items():
        daily_load = calculate_daily_load(day_activities, athlete_max_hr)
        daily_loads[date_str] = daily_load

    history['daily_loads'] = daily_loads

    # Calculate current metrics and add snapshot
    metrics = calculate_fitness_metrics(daily_loads)
    snapshot = {
        'date': metrics['as_of_date'],
        'ctl': metrics['ctl'],
        'atl': metrics['atl'],
        'tsb': metrics['tsb'],
        'acwr': metrics['acwr'],
    }

    # Keep last 90 days of snapshots
    snapshots = history.get('snapshots', [])
    snapshots.append(snapshot)
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    snapshots = [s for s in snapshots if s['date'] >= cutoff]
    history['snapshots'] = snapshots

    save_fitness_history(history)
    return history


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

    # Calculate trend
    first_ctl = recent_snapshots[0]['ctl']
    last_ctl = recent_snapshots[-1]['ctl']
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
