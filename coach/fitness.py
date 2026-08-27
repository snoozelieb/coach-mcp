"""
Fitness tracking and training load calculations.

Implements science-based metrics:
- CTL (Chronic Training Load) - 42-day exponentially weighted fitness
- ATL (Acute Training Load) - 7-day exponentially weighted fatigue
- TSB (Training Stress Balance) - Form = CTL - ATL
- ACWR (Acute:Chronic Workload Ratio) - Injury risk indicator. Primary model
  is the classic rolling 7d:28d coupled-window ratio (Hulin/Gabbett) since
  the 2026-06-10 cutover; the legacy EWMA ratio is kept as a labeled
  reference (acwr_ewma)
- Intensity distribution tracking (80/20 polarized model)

Based on research from TrainingPeaks, Firstbeat, and Norwegian Olympic methodology.
"""
import json
from datetime import date, timedelta
from typing import Any
from collections import defaultdict

from .config import (
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
    SLEEP_HISTORY_RETENTION_DAYS,
    READINESS_HISTORY_RETENTION_DAYS,
    SNAPSHOT_RETENTION_DAYS,
    POLARIZATION_TARGETS,
    SLEEP_DEEP_PCT_MIN,
    SLEEP_DEEP_PCT_EXCELLENT,
    SLEEP_SCORE_ADEQUATE,
    SLEEP_SCORE_GOOD,
    SLEEP_SCORE_EXCELLENT,
    SLEEP_NAP_EFFECTIVE_MINS,
    SLEEP_VARIANCE_THRESHOLD_HRS,
    SLEEP_TARGET_DEFAULT_HRS,
    get_sport_group,
)
from .garmin_client import garmin_api_call
from .parsers import epoch_ms_to_local_iso
import logging

logger = logging.getLogger(__name__)


def calculate_training_load(activity: dict[str, Any]) -> float:
    """Return Garmin's training load for an activity, or 0.0 if unavailable."""
    garmin_load = activity.get('garmin_training_load')
    if garmin_load and garmin_load > 0:
        return round(float(garmin_load), 1)
    return 0.0


def calculate_daily_load(activities: list[dict[str, Any]]) -> float:
    """Calculate total training load for a day's activities."""
    return sum(calculate_training_load(a) for a in activities)


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


# Classic rolling-average ACWR windows (Hulin/Gabbett 7d:28d methodology).
# These are the windows the 0.8/1.3/1.5 thresholds were derived against.
ACWR_ROLLING_ACUTE_DAYS = 7
ACWR_ROLLING_CHRONIC_DAYS = 28

# Label attached to the demoted EWMA ACWR wherever it is exposed.
ACWR_EWMA_REFERENCE_NOTE = (
    "legacy EWMA model — reference only, retired as primary 2026-06-10"
)


def classify_acwr_zone(acwr: float) -> tuple[str, str]:
    """Classify an ACWR value into a zone using the research thresholds.

    Thresholds (config): < 0.8 low, 0.8-1.3 optimal, 1.3-1.5 elevated,
    > 1.5 danger. These thresholds come from classic 7d:28d rolling-average
    research and are NATIVE to the rolling primary model (cutover
    2026-06-10). The legacy EWMA reference value reuses the same labels
    purely for comparability.

    Returns:
        (zone, risk_description) tuple.
    """
    if acwr < ACWR_LOW_THRESHOLD:
        return "low", "Undertrained - fitness may be declining"
    elif acwr <= ACWR_HIGH_THRESHOLD:
        return "optimal", "Sweet spot - good balance of load and recovery"
    elif acwr <= ACWR_DANGER_THRESHOLD:
        return "elevated", "Elevated injury risk - consider reducing load"
    else:
        return "danger", "High injury risk - reduce load significantly"


def calculate_rolling_acwr(loads_list: list[float]) -> float:
    """Classic rolling-average ACWR (PRIMARY decision model since 2026-06-10).

    acute   = mean daily load over the last 7 days
    chronic = mean daily load over the last 28 days (coupled windows: the
              chronic window includes the acute week, per the original
              Hulin/Gabbett 7d:28d methodology)

    This is the model the 0.8/1.3/1.5 thresholds were actually derived
    against. It ran in shadow alongside the EWMA model for 90 days; the
    owner-approved cutover (mean abs diff 0.264, 42% zone mismatch, and the
    May 11-13 post-stage-race danger window the EWMA model missed) made it
    the primary on 2026-06-10. The EWMA value survives as a clearly-labeled
    reference (``acwr_ewma``).

    Args:
        loads_list: Daily loads, oldest first. Rest days must be present
            as 0.0. Histories shorter than 28 days use what is available.

    Returns:
        Rolling ACWR rounded to 2 decimals. With zero chronic load:
        1.0 when acute is also zero (no data), else 2.0 — mirroring the
        EWMA guard so the two models degrade identically.
    """
    if not loads_list:
        return 1.0
    acute_window = loads_list[-ACWR_ROLLING_ACUTE_DAYS:]
    chronic_window = loads_list[-ACWR_ROLLING_CHRONIC_DAYS:]
    acute = sum(acute_window) / len(acute_window)
    chronic = sum(chronic_window) / len(chronic_window)
    if chronic > 0:
        return round(acute / chronic, 2)
    return 1.0 if acute == 0 else 2.0


def calculate_fitness_metrics(
    daily_loads: dict[str, float],
    as_of_date: date,
) -> dict[str, Any]:
    """
    Calculate CTL, ATL, TSB, and ACWR from daily training loads.

    Two ACWR models are returned side by side:

    - ``acwr`` / ``acwr_status`` / ``acwr_risk`` (classic rolling model —
      PRIMARY since the 2026-06-10 cutover): 7-day mean / 28-day mean daily
      load (coupled windows, Hulin/Gabbett), the model the 0.8/1.3/1.5
      thresholds were derived against. Promoted after the owner reviewed
      the 90-day shadow report (scripts/acwr_shadow_report.py).

    - ``acwr_ewma`` (legacy EWMA model — REFERENCE ONLY): ATL/CTL where
      both are EWMAs with k = 2/(N+1) (N=7 acute, N=42 chronic). Retired
      as primary 2026-06-10; kept as a labeled ``{value, zone, safe, note}``
      block for continuity with pre-cutover analyses.

    CTL/ATL/TSB math is UNCHANGED by the cutover — their EWMA scale is
    self-consistent and CTL_TARGETS / volume steps are tuned against it.

    Args:
        daily_loads: Dict mapping date strings to training load values
        as_of_date: Calculate metrics as of this date. Required — resolve
            date.today() once at the tool boundary and thread it through
            (clock discipline; see tests/test_clock_discipline.py).

    Returns:
        Dict with ctl, atl, tsb, acwr (rolling primary), acwr_status,
        acwr_risk, acwr_ewma (legacy reference), and metadata
    """
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

    # Calculate ACWR (Acute:Chronic Workload Ratio) — PRIMARY: classic
    # rolling 7d:28d coupled windows (see calculate_rolling_acwr).
    acwr = calculate_rolling_acwr(loads_list)
    acwr_status, acwr_risk = classify_acwr_zone(acwr)

    # Legacy EWMA ACWR (ATL/CTL) — reference only since the 2026-06-10
    # cutover. The 90-day shadow report showed mean abs diff 0.264 and 42%
    # zone mismatch vs the rolling model; rolling correctly flagged the
    # May 11-13 post-stage-race danger window the EWMA model read as
    # optimal/low.
    if ctl > 0:
        acwr_ewma_value = round(atl / ctl, 2)
    else:
        acwr_ewma_value = 1.0 if atl == 0 else 2.0  # No chronic fitness = high ratio
    ewma_zone, _ewma_risk = classify_acwr_zone(acwr_ewma_value)

    # Count days with data
    days_with_data = sum(1 for d in daily_loads.values() if d > 0)

    return {
        'ctl': ctl,
        'atl': atl,
        'tsb': tsb,
        'acwr': acwr,
        'acwr_status': acwr_status,
        'acwr_risk': acwr_risk,
        'acwr_ewma': {
            'value': acwr_ewma_value,
            'zone': ewma_zone,
            'safe': ewma_zone in ('optimal', 'low'),
            'note': ACWR_EWMA_REFERENCE_NOTE,
        },
        'days_analyzed': len(loads_list),
        'days_with_data': days_with_data,
        'data_sufficient': days_with_data >= MIN_DAYS_FOR_CTL,
        'as_of_date': as_of_date.isoformat(),
    }


def calculate_ctl_target(
    race_date: str,
    race_type: str,
    current_ctl: float,
    current_weekly_tss: float = None,
    *,
    today: date,
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
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        Dict with target_ctl, weekly_tss_required, hours_required, on_track, etc.
    """
    # Parse race date
    try:
        race_dt = date.fromisoformat(race_date)
    except (ValueError, TypeError):
        return {"error": f"Invalid race date: {race_date}"}

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

    # Track how many activities used each data source
    source_zone_data = 0
    source_avg_hr = 0
    source_type_estimate = 0

    for activity in activities:
        duration = activity.get('duration_mins', 0) or 0
        avg_hr = activity.get('avg_hr', 0) or 0
        total_duration += duration

        hr_zones_data = activity.get('hr_time_in_zones')
        if hr_zones_data:
            # Actual time-in-zone from Garmin (preferred)
            source_zone_data += 1
            time_low += hr_zones_data.get('z1', 0) + hr_zones_data.get('z2', 0)
            time_moderate += hr_zones_data.get('z3', 0)
            time_high += hr_zones_data.get('z4', 0) + hr_zones_data.get('z5', 0)
            # Time below Z1 floor (or unaccounted) = low intensity
            accounted = sum(hr_zones_data.values())
            time_low += max(0, duration - accounted)
        elif avg_hr == 0:
            # No HR data - estimate from activity type
            activity_type = activity.get('type', '').lower()
            low_intensity_types = {'yoga', 'pilates', 'stretching', 'walking', 'breathwork'}
            high_intensity_types = {'hiit', 'interval_training', 'ultimate_disc', 'track_running'}

            if activity_type in low_intensity_types:
                time_low += duration
                source_type_estimate += 1
            elif activity_type in high_intensity_types:
                time_high += duration
                source_type_estimate += 1
            else:
                time_unknown += duration
                source_type_estimate += 1
        else:
            # Fallback: classify entire activity by avg HR
            source_avg_hr += 1
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
    low_deviation = abs(pct_low - POLARIZATION_TARGETS['low_pct'])
    moderate_deviation = abs(pct_moderate - POLARIZATION_TARGETS['moderate_pct'])
    high_deviation = abs(pct_high - POLARIZATION_TARGETS['high_pct'])
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
        'target_distribution': f"{POLARIZATION_TARGETS['low_pct']}% low / {POLARIZATION_TARGETS['moderate_pct']}% moderate / {POLARIZATION_TARGETS['high_pct']}% high",
        'recommendation': recommendation,
        'data_source': {
            'zone_data': source_zone_data,
            'avg_hr': source_avg_hr,
            'type_estimate': source_type_estimate,
        },
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
    """Save fitness history to file (atomic write).

    Delegates to coach.storage: atomic write, utf-8, cross-process locked.
    DATA_DIR is read at call time so tests can monkeypatch it on this module.

    The date.today() here is a write-time audit stamp (last_updated), not
    date logic — allowlisted in tests/test_clock_discipline.py.
    """
    from . import storage  # function-local: avoids module-level import cycle
    history['last_updated'] = date.today().isoformat()
    storage.write_json(FITNESS_HISTORY_FILE, history, data_dir=DATA_DIR)


def update_fitness_history(
    activities: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """
    Update fitness history with new activity data (v2 sport-aware format).

    Args:
        activities: List of parsed activities with date, type, garmin_training_load
        today: Current date, resolved at the tool boundary (clock discipline)

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
            load = calculate_training_load(act)
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
    metrics = calculate_fitness_metrics(total_loads_flat, today)

    # Calculate per-sport metrics for the snapshot
    sport_metrics = {}
    for sport in ['cycling', 'running', 'strength']:
        sport_loads = _extract_sport_loads(daily_loads, sport)
        if any(v > 0 for v in sport_loads.values()):
            sm = calculate_fitness_metrics(sport_loads, today)
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

    # Upsert by date — re-running on the same day must REPLACE that day's
    # snapshot, not append a duplicate. Same-date duplicates corrupt trend
    # math (get_fitness_trend treats entries as data points over time).
    snapshots = [
        s for s in history.get('snapshots', [])
        if s.get('date') != snapshot['date']
    ]
    snapshots.append(snapshot)
    snapshots.sort(key=lambda s: s.get('date', ''))

    # Retention is config-driven; default keeps the full trajectory
    history['snapshots'] = _apply_retention(
        snapshots, today, SNAPSHOT_RETENTION_DAYS)

    # Dedicated activity-ingestion marker. save_fitness_history bumps
    # last_updated on EVERY save (including sleep/readiness persistence),
    # so staleness checks must use this field instead.
    history['last_activity_ingest_date'] = today.isoformat()

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
    as_of_date: date,
) -> dict[str, Any]:
    """
    Calculate CTL/ATL/TSB/ACWR for a specific sport.

    Extracts that sport's load from each day's by_sport dict and runs the
    same calculation used for overall metrics — EWMA CTL/ATL/TSB plus the
    rolling 7d:28d primary ACWR, so the load hierarchy never mixes models.

    Args:
        daily_loads: v2 daily_loads dict
        sport: Sport group name ('cycling', 'running', 'strength')
        as_of_date: Calculate as of this date, resolved at the tool boundary

    Returns:
        Dict with sport-specific ctl, atl, tsb, acwr, acwr_status
    """
    sport_loads = _extract_sport_loads(daily_loads, sport)
    metrics = calculate_fitness_metrics(sport_loads, as_of_date)
    return metrics


def get_sleep_trend(history: dict[str, Any] = None, days: int = 30,
                    *, today: date) -> dict[str, Any]:
    """
    Get sleep trend from persisted sleep_history.

    Args:
        history: Fitness history dict (loads from file if None)
        days: Number of days to analyze (default 30)
        today: Current date, resolved at the tool boundary (clock discipline)

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

    cutoff = (today - timedelta(days=days)).isoformat()
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


def detect_bedtime_drift(sleep_nights: list[dict], min_nights: int = 8) -> dict[str, Any]:
    """Detect whether bedtime is drifting later, earlier, or stable.

    Compares the average bedtime of the first half of the window against the
    second half. A drift of >15 min/wk is a meaningful overtraining/stress
    signal. Bedtimes crossing midnight are normalised onto a 24h circle to
    avoid the 23:30 → 00:15 "drifting earlier" false positive.

    Args:
        sleep_nights: list of sleep records (any order). Needs `bedtime` ISO
            strings and `date` to work.
        min_nights: minimum nights required to compute drift.

    Returns:
        Dict with direction ('later'/'earlier'/'stable'),
        drift_mins_per_wk (signed float), avg_bedtime (HH:MM string),
        sample_size, status ('ok' or 'insufficient_data').
    """
    from datetime import datetime as _dt

    if not sleep_nights or len(sleep_nights) < min_nights:
        return {'status': 'insufficient_data', 'direction': 'unknown'}

    nights = sorted(
        [n for n in sleep_nights if n.get('bedtime') and n.get('date')],
        key=lambda n: n['date'],
    )
    if len(nights) < min_nights:
        return {'status': 'insufficient_data', 'direction': 'unknown'}

    # Convert bedtime to minutes-after-noon (so 20:00 → 480, 01:00 → 780)
    # Anything between 12:00 and 23:59 gets its real hour*60+min.
    # Anything between 00:00 and 11:59 gets +1440 (so 01:00 lands after 23:00).
    # Defensive: bedtime may be an ISO string, an epoch-ms int (raw Garmin
    # payloads), or None — none of these may crash the snapshot.
    def _bedtime_mins(value) -> float | None:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                iso = epoch_ms_to_local_iso(value)
                if iso is None:
                    return None
                dt = _dt.fromisoformat(iso)
            elif isinstance(value, str):
                dt = _dt.fromisoformat(value.replace('Z', '+00:00'))
            else:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        mins = dt.hour * 60 + dt.minute
        if dt.hour < 12:
            mins += 24 * 60
        return mins

    values = [(n['date'], _bedtime_mins(n['bedtime'])) for n in nights]
    values = [(d, v) for d, v in values if v is not None]
    if len(values) < min_nights:
        return {'status': 'insufficient_data', 'direction': 'unknown'}

    half = len(values) // 2
    first_half_avg = sum(v for _, v in values[:half]) / half
    second_half_avg = sum(v for _, v in values[half:]) / (len(values) - half)

    delta_mins = second_half_avg - first_half_avg
    span_days = max(1, len(values) - 1)
    drift_mins_per_wk = round((delta_mins / span_days) * 7, 1)

    if drift_mins_per_wk > 15:
        direction = 'later'
    elif drift_mins_per_wk < -15:
        direction = 'earlier'
    else:
        direction = 'stable'

    # Format average bedtime as HH:MM (using second-half mean as "current")
    hh = int(second_half_avg // 60) % 24
    mm = int(second_half_avg % 60)
    avg_bedtime = f"{hh:02d}:{mm:02d}"

    return {
        'status': 'ok',
        'direction': direction,
        'drift_mins_per_wk': drift_mins_per_wk,
        'current_avg_bedtime': avg_bedtime,
        'sample_size': len(values),
    }


def _apply_retention(entries: list, today: date, retention_days: int | None,
                     date_key: str = 'date') -> list:
    """Sort history entries by date and apply a retention window.

    None = keep everything. History is the athlete's training diary; the old
    fixed prunes (sleep 30d, readiness 60d, snapshots 90d) erased it and were
    retired as a defect — the config.*_RETENTION_DAYS defaults are None.
    """
    entries = sorted(entries, key=lambda r: r.get(date_key, ''))
    if retention_days is None:
        return entries
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    return [r for r in entries if r.get(date_key, '') >= cutoff]


def persist_sleep_data(sleep_records: list[dict], history: dict[str, Any] = None,
                       *, today: date) -> dict[str, Any]:
    """
    Save nightly sleep records to fitness_history.json → sleep_history.

    Stores: date, bedtime, wake_time, duration_hrs, score, deep_pct, rem_pct,
    light_pct, awake_mins, avg_hr, respiration, sleep_stress.
    Retention is config-driven (SLEEP_HISTORY_RETENTION_DAYS; default None =
    keep the full training diary).

    Args:
        sleep_records: List of sleep record dicts from get_sleep_summary
        history: Fitness history dict (loads from file if None)
        today: Current date, resolved at the tool boundary (clock discipline)

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
            'bedtime': rec.get('bedtime'),
            'wake_time': rec.get('wake_time'),
            'duration_hrs': rec.get('duration_hrs'),
            'score': rec.get('score'),
            'deep_pct': rec.get('deep_pct'),
            'rem_pct': rec.get('rem_pct'),
            'light_pct': rec.get('light_pct'),
            'awake_mins': rec.get('awake_mins'),
            'avg_hr': rec.get('avg_hr'),
            'respiration': rec.get('respiration'),
            'sleep_stress': rec.get('sleep_stress'),
        })
        existing_dates.add(rec_date)

    history['sleep_history'] = _apply_retention(
        existing, today, SLEEP_HISTORY_RETENTION_DAYS)
    return history


def persist_readiness_data(readiness_record: dict, history: dict[str, Any] = None,
                           *, today: date) -> dict[str, Any]:
    """
    Persist daily readiness data to fitness_history.json → readiness_history.

    Modeled on persist_sleep_data(). Retention is config-driven
    (READINESS_HISTORY_RETENTION_DAYS; default None = keep everything).

    Args:
        readiness_record: Dict with {date, score, level, hrv_status, body_battery}
        history: Fitness history dict (loads from file if None)
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        Updated fitness history dict
    """
    if history is None:
        history = load_fitness_history()

    existing = history.get('readiness_history', [])
    existing_dates = {r['date'] for r in existing if r.get('date')}

    rec_date = readiness_record.get('date')
    if rec_date and rec_date not in existing_dates:
        existing.append({
            'date': rec_date,
            'score': readiness_record.get('score'),
            'level': readiness_record.get('level'),
            'hrv_status': readiness_record.get('hrv_status'),
            'body_battery': readiness_record.get('body_battery'),
        })

    history['readiness_history'] = _apply_retention(
        existing, today, READINESS_HISTORY_RETENTION_DAYS)
    return history


def calculate_readiness_baselines(sleep_history: list, readiness_history: list,
                                  today: date) -> dict:
    """Calculate rolling averages for personal baseline comparison.

    Provides 14-day and 30-day averages so the LLM can compare today's
    values against the athlete's personal normal, not population norms.

    Args:
        sleep_history: List of sleep records from fitness_history
        readiness_history: List of readiness records from fitness_history
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        Dict with rolling averages and data sufficiency status.
    """
    cutoff_14d = (today - timedelta(days=14)).isoformat()
    cutoff_30d = (today - timedelta(days=30)).isoformat()

    result = {}

    # Sleep baselines
    sleep_14d = [r for r in sleep_history if r.get('date', '') >= cutoff_14d]
    sleep_30d = [r for r in sleep_history if r.get('date', '') >= cutoff_30d]

    if sleep_14d:
        durations = [r['duration_hrs'] for r in sleep_14d if r.get('duration_hrs') is not None]
        scores = [r['score'] for r in sleep_14d if r.get('score') is not None]
        if durations:
            result['sleep_duration_14d_avg'] = round(sum(durations) / len(durations), 1)
        if scores:
            result['sleep_score_14d_avg'] = round(sum(scores) / len(scores), 0)

    if sleep_30d:
        durations = [r['duration_hrs'] for r in sleep_30d if r.get('duration_hrs') is not None]
        scores = [r['score'] for r in sleep_30d if r.get('score') is not None]
        if durations:
            result['sleep_duration_30d_avg'] = round(sum(durations) / len(durations), 1)
        if scores:
            result['sleep_score_30d_avg'] = round(sum(scores) / len(scores), 0)

    # Readiness baselines
    readiness_14d = [r for r in readiness_history if r.get('date', '') >= cutoff_14d]
    readiness_30d = [r for r in readiness_history if r.get('date', '') >= cutoff_30d]

    if readiness_14d:
        scores = [r['score'] for r in readiness_14d if r.get('score') is not None]
        if scores:
            result['readiness_14d_avg'] = round(sum(scores) / len(scores), 0)

    if readiness_30d:
        scores = [r['score'] for r in readiness_30d if r.get('score') is not None]
        if scores:
            result['readiness_30d_avg'] = round(sum(scores) / len(scores), 0)

    # Data sufficiency
    total_days = len(sleep_30d) + len(readiness_30d)
    result['status'] = 'sufficient' if total_days >= 7 else 'insufficient_data'

    return result


def derive_adaptation_thresholds(responses: list) -> dict:
    """Derive athlete-specific thresholds from recorded responses with numeric data.

    Groups responses by load_change_pct buckets and calculates success rates
    per bucket. Only produces quantified output when n >= 8 responses have
    numeric data.

    Args:
        responses: List of athlete response dicts from coaching_log

    Returns:
        Dict with quantified thresholds or accumulating status.
    """
    # Filter responses with numeric load data
    numeric = [
        r for r in responses
        if r.get('load_change_pct') is not None
    ]

    if len(numeric) < 8:
        return {
            'status': 'accumulating',
            'data_points': len(numeric),
        }

    # Group by load change buckets
    buckets = {
        'conservative': {'range': (-5, 10), 'responses': []},
        'standard': {'range': (10, 20), 'responses': []},
        'aggressive': {'range': (20, 40), 'responses': []},
    }

    for r in numeric:
        pct = r['load_change_pct']
        if pct < 10:
            buckets['conservative']['responses'].append(r)
        elif pct < 20:
            buckets['standard']['responses'].append(r)
        else:
            buckets['aggressive']['responses'].append(r)

    # Calculate per-bucket outcomes
    volume_tolerance = {}
    for bucket_name, bucket in buckets.items():
        resps = bucket['responses']
        if not resps:
            continue

        n = len(resps)
        avg_load_change = round(sum(r['load_change_pct'] for r in resps) / n, 1)

        # Success = compliant AND no injury
        successes = sum(
            1 for r in resps
            if r.get('compliance_result', True) and not r.get('injury_flag', False)
        )
        success_rate = round(successes / n, 2)

        volume_tolerance[bucket_name] = {
            'avg_load_change_pct': avg_load_change,
            'success_rate': success_rate,
            'n': n,
        }

    # Determine safe max from highest bucket with >= 80% success and n >= 3
    safe_max = None
    for bucket_name in ['aggressive', 'standard', 'conservative']:
        bt = volume_tolerance.get(bucket_name)
        if bt and bt['n'] >= 3 and bt['success_rate'] >= 0.8:
            safe_max = round(bt['avg_load_change_pct'])
            break

    # Confidence based on total data points
    confidence = 'high' if len(numeric) >= 20 else 'moderate'

    result = {
        'status': 'quantified',
        'volume_tolerance': volume_tolerance,
        'confidence': confidence,
        'data_points': len(numeric),
    }
    if safe_max is not None:
        result['safe_load_increase_max_pct'] = safe_max

    return result


def analyze_activity_patterns(
    daily_loads: dict[str, Any],
    today: date,
    days: int = 28,
) -> dict[str, Any]:
    """
    Analyze activity patterns from stored fitness history.

    Returns last activity date by sport, sessions per week by sport,
    and alerts for concerning patterns.
    """
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


def get_fitness_trend(days: int = 28, *, today: date) -> dict[str, Any]:
    """
    Get fitness trend over specified period.

    Args:
        days: Number of days to analyze
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        Dict with CTL trend, direction, and projection
    """
    history = load_fitness_history()
    snapshots = history.get('snapshots', [])

    # Legacy histories may contain same-date duplicates (pre-upsert bug).
    # Keep the LAST write per date so trend math sees one point per day.
    by_date: dict[str, dict] = {}
    for s in snapshots:
        if s.get('date'):
            by_date[s['date']] = s
    snapshots = [by_date[d] for d in sorted(by_date)]

    if len(snapshots) < 2:
        return {
            'trend': 'unknown',
            'ctl_change': 0,
            'data_points': len(snapshots),
            'note': 'Insufficient data for trend analysis',
        }

    # Get snapshots within the period
    cutoff = (today - timedelta(days=days)).isoformat()
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

    # Project future CTL if trend continues. Divide by the ACTUAL day span
    # between first and last snapshot — snapshot COUNT is not days (gaps
    # between snapshots made the old per-"day" rate wildly overstated).
    try:
        span_days = (
            date.fromisoformat(recent_snapshots[-1]['date'])
            - date.fromisoformat(recent_snapshots[0]['date'])
        ).days
    except (KeyError, TypeError, ValueError):
        span_days = 0
    daily_change = ctl_change / span_days if span_days > 0 else 0
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


def get_day_context(day_str: str, daily_loads: dict, sleep_history: list) -> dict:
    """Return context for a specific date from already-loaded data.

    Provides surrounding context (sleep, prior day load) so anomalies
    can be reasoned about without additional API calls.

    Args:
        day_str: ISO date string (e.g. '2026-03-14')
        daily_loads: daily_loads dict from fitness_history
        sleep_history: sleep_history list from fitness_history

    Returns:
        Dict with available context fields. Empty dict if no data found.
    """
    context = {}

    # Sleep data for this date
    if sleep_history:
        sleep_rec = next((r for r in sleep_history if r.get('date') == day_str), None)
        if sleep_rec:
            if sleep_rec.get('score') is not None:
                context['sleep_score'] = sleep_rec['score']
            if sleep_rec.get('duration_hrs') is not None:
                context['sleep_hours'] = sleep_rec['duration_hrs']

    # Prior day load
    try:
        day_date = date.fromisoformat(day_str)
        prior_str = (day_date - timedelta(days=1)).isoformat()
        prior_data = daily_loads.get(prior_str)
        if isinstance(prior_data, dict):
            if prior_data.get('total') is not None:
                context['prior_day_load'] = round(prior_data['total'], 1)
            # Check if prior day had a hard activity
            for act in prior_data.get('activities', []):
                if act.get('sport') in ('running', 'cycling') and prior_data.get('total', 0) > 80:
                    context['prior_day_hard'] = True
                    break
        elif isinstance(prior_data, (int, float)) and prior_data > 0:
            context['prior_day_load'] = round(prior_data, 1)
    except (ValueError, TypeError):
        pass

    return context


def get_athlete_hr_zones() -> dict[str, list[int]] | None:
    """Load athlete's HR zones from profile."""
    athlete_path = DATA_DIR / ATHLETE_FILE
    if athlete_path.exists():
        with open(athlete_path) as f:
            athlete = json.load(f)
        return athlete.get('personal', {}).get('hr_zones')
    return None


def parse_sleep_payload(payload: dict | None, day_iso: str) -> tuple[dict | None, dict | None]:
    """Parse one night's raw get_sleep_data payload into a sleep record.

    Pure function — no I/O. Returns ``(record, sleep_need)``:
    - record: per-night dict (None when the payload has no usable sleep)
    - sleep_need: Garmin's personalized sleep-need analysis when present
      ({'actual_mins', 'baseline_mins', 'feedback', 'training_impact'})
    """
    if not payload or not payload.get('dailySleepDTO'):
        return None, None

    dto = payload['dailySleepDTO']
    scores = dto.get('sleepScores', {})

    duration_secs = dto.get('sleepTimeSeconds', 0)
    if not duration_secs or duration_secs <= 0:
        return None, None

    # Extract quality metrics
    deep_secs = dto.get('deepSleepSeconds', 0)
    rem_secs = dto.get('remSleepSeconds', 0)
    light_secs = dto.get('lightSleepSeconds', 0)
    awake_secs = dto.get('awakeSleepSeconds', 0)

    # Extract nap data for this day
    nap_secs = dto.get('napTimeSeconds', 0) or 0
    nap_dtos = dto.get('dailyNapDTOS', []) or []
    nap_count = len(nap_dtos)

    # Extract bedtime / wake time — Garmin returns either local
    # ISO8601 strings or epoch-ms ints depending on endpoint
    # version. Normalize to local ISO strings at parse time.
    bedtime = epoch_ms_to_local_iso(
        dto.get('sleepStartTimestampLocal')
        or dto.get('startTimestampLocal')
    )
    wake_time = epoch_ms_to_local_iso(
        dto.get('sleepEndTimestampLocal')
        or dto.get('endTimestampLocal')
    )

    record = {
        'date': day_iso,
        'bedtime': bedtime,
        'wake_time': wake_time,
        'duration_hrs': round(duration_secs / 3600, 1),
        'score': scores.get('overall', {}).get('value'),
        'quality': scores.get('overall', {}).get('qualifierKey'),
        # Quality breakdown
        'deep_mins': round(deep_secs / 60, 0),
        'deep_pct': round(deep_secs / duration_secs * 100, 0) if duration_secs else 0,
        'deep_quality': scores.get('deepPercentage', {}).get('qualifierKey'),
        'rem_mins': round(rem_secs / 60, 0),
        'rem_pct': round(rem_secs / duration_secs * 100, 0) if duration_secs else 0,
        'rem_quality': scores.get('remPercentage', {}).get('qualifierKey'),
        'light_mins': round(light_secs / 60, 0),
        'light_pct': round(light_secs / duration_secs * 100, 0) if duration_secs else 0,
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

    sleep_need = None
    need = dto.get('sleepNeed', {})
    if need:
        sleep_need = {
            'actual_mins': need.get('actual'),  # Training-adjusted need
            'baseline_mins': need.get('baseline'),  # Base need without training
            'feedback': need.get('feedback'),  # "HIGHLY_INCREASED" etc
            'training_impact': need.get('trainingFeedback'),  # "CHRONIC" load impact
        }
    return record, sleep_need


def get_sleep_summary(today: date, days: int = 7) -> dict:
    """
    Get comprehensive sleep analysis for the last N days.

    This is a CORE coaching metric - sleep quality and quantity directly
    determine whether training adaptations can occur.

    Fetches every night from Garmin then delegates to
    summarize_sleep_records(). Callers that already hold persisted nights
    (fitness_history sleep_history) should fetch only the missing dates and
    call summarize_sleep_records() directly — the coaching snapshot does.

    Args:
        today: Current date
        days: Number of days to analyze (default 7)

    Returns:
        Dict with sleep status, metrics, quality issues, and training modifications
    """
    sleep_records = []
    sleep_need = None

    for i in range(days):
        d = today - timedelta(days=i)
        try:
            payload = garmin_api_call(lambda c, ds=d.isoformat(): c.get_sleep_data(ds))
        except Exception:
            continue
        record, need = parse_sleep_payload(payload, d.isoformat())
        if record:
            sleep_records.append(record)
        if i == 0 and need:  # Last night carries the personalized need
            sleep_need = need

    return summarize_sleep_records(sleep_records, sleep_need=sleep_need)


def summarize_sleep_records(sleep_records: list[dict], sleep_need: dict | None = None) -> dict:
    """Aggregate per-night sleep records into the coaching sleep summary.

    Pure computation — no I/O. Records may come straight from
    parse_sleep_payload() (full detail) or from persisted fitness_history
    sleep_history nights (a subset of fields); missing fields degrade
    gracefully. Records are sorted most-recent-first internally.

    Analyzes:
    - Duration vs personalized need (Garmin calculates based on training load)
    - Quality: deep sleep %, REM %, sleep stress, awake count
    - Consistency: variance night to night
    - Accumulated deficit

    Without adequate sleep, training is CATABOLIC not ANABOLIC.
    """
    if not sleep_records:
        return {'status': 'no_data', 'note': 'Could not fetch sleep data'}

    sleep_records = sorted(
        sleep_records, key=lambda r: r.get('date', ''), reverse=True
    )

    sleep_need = sleep_need or {}
    personalized_need_mins = sleep_need.get('actual_mins')
    baseline_need_mins = sleep_need.get('baseline_mins')
    need_feedback = sleep_need.get('feedback')
    training_impact = sleep_need.get('training_impact')

    def _hrs(r):
        return r.get('duration_hrs') or 0

    def _deep(r):
        return r.get('deep_pct') or 0

    # Calculate averages (7-day)
    avg_duration = round(sum(_hrs(r) for r in sleep_records) / len(sleep_records), 1)
    scores_with_values = [r['score'] for r in sleep_records if r.get('score')]
    avg_score = round(sum(scores_with_values) / len(scores_with_values), 0) if scores_with_values else None
    avg_deep_pct = round(sum(_deep(r) for r in sleep_records) / len(sleep_records), 0)
    avg_rem_pct = round(sum(r.get('rem_pct') or 0 for r in sleep_records) / len(sleep_records), 0)

    # Nap totals
    total_nap_mins = sum(r.get('nap_mins', 0) for r in sleep_records)
    today_nap_mins = sleep_records[0].get('nap_mins', 0) if sleep_records else 0

    # ACUTE READINESS: Recent nights matter more for "can I do an FTP test tomorrow?"
    # Weight last 2-3 nights heavily for acute training decisions
    recent_nights = sleep_records[:3]  # Last 3 nights (most recent first)
    if recent_nights:
        recent_scores = [r['score'] for r in recent_nights if r.get('score')]
        recent_avg_score = round(sum(recent_scores) / len(recent_scores), 0) if recent_scores else None
        recent_avg_duration = round(sum(_hrs(r) for r in recent_nights) / len(recent_nights), 1)
        recent_avg_deep = round(sum(_deep(r) for r in recent_nights) / len(recent_nights), 0)
        # Trend: is sleep improving? (positive = getting better)
        if len(recent_nights) >= 2:
            trend = _hrs(recent_nights[0]) - _hrs(recent_nights[-1])
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
        target_hrs = SLEEP_TARGET_DEFAULT_HRS
        target_source = 'default'

    # Calculate deficit against PERSONALIZED target
    daily_deficit = target_hrs - avg_duration
    weekly_deficit = round(daily_deficit * 7, 1)

    # Quality assessment (not just duration)
    quality_issues = []

    # Deep sleep check (optimal 16-33% for adults)
    if avg_deep_pct < SLEEP_DEEP_PCT_MIN:
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
    durations = [_hrs(r) for r in sleep_records]
    if len(durations) > 1:
        variance = max(durations) - min(durations)
        if variance > SLEEP_VARIANCE_THRESHOLD_HRS:
            quality_issues.append(f'Inconsistent sleep ({variance:.1f}hr variance) - poor sleep hygiene')

    # CHRONIC STATUS: 7-day average for overall training load decisions
    quantity_ok = avg_duration >= (target_hrs - 0.5)  # Within 30min of target
    quality_ok = avg_score and avg_score >= SLEEP_SCORE_ADEQUATE and avg_deep_pct >= SLEEP_DEEP_PCT_MIN

    # ACUTE STATUS: Recent nights (last 2-3) for "can I do hard session tomorrow?"
    # High scores (80+) with good deep sleep can override moderate duration shortfall
    recent_quality_excellent = recent_avg_score and recent_avg_score >= SLEEP_SCORE_EXCELLENT and recent_avg_deep >= SLEEP_DEEP_PCT_EXCELLENT
    recent_quality_good = recent_avg_score and recent_avg_score >= SLEEP_SCORE_GOOD and recent_avg_deep >= SLEEP_DEEP_PCT_MIN
    recent_duration_ok = recent_avg_duration >= 6.5
    improving_trend = trend > 0.3  # Getting noticeably more sleep

    # Nap bonus: a nap today adds to acute recovery capacity
    nap_recovery_boost = today_nap_mins >= SLEEP_NAP_EFFECTIVE_MINS

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
        # Full 7-night detail for pattern detection (bedtime drift, deep-sleep drops)
        'nights': sleep_records[:7],

        # Coaching output
        'recommendation': recommendation,
        'training_modifications': training_modifications,
    }
