"""
Biomechanical investigation script — recurring left fibular stress fracture.

Pulls 6+ years of Garmin data and produces a structured JSON investigation report
documenting activity patterns, load spikes around injury events, and cycling power trends.

Usage:
    python scripts/investigate_fibula.py           # Full run (Phase 1 probes first)
    python scripts/investigate_fibula.py --skip-probe  # Skip Phase 1, go straight to data pull
    python scripts/investigate_fibula.py --report-only  # Skip fetching, just re-generate report
"""
import json
import sys
import time
import argparse
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# Project root on sys.path so we can import project modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from coach.garmin_client import garmin_api_call  # noqa: E402
from coach.parsers import parse_activity  # noqa: E402
from coach.fitness import calculate_fitness_metrics  # noqa: E402
from coach.config import SPORT_GROUPS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"
INVESTIGATION_DIR = DATA_DIR / "investigation"
PROBE_FILE = INVESTIGATION_DIR / "probe_details.json"
CYCLING_FILE = INVESTIGATION_DIR / "cycling_activities.json"
IMPACT_FILE = INVESTIGATION_DIR / "impact_activities.json"
REPORT_FILE = INVESTIGATION_DIR / "investigation_report.json"

# ── Constants ────────────────────────────────────────────────────────────────
YEARS_BACK = 6

CYCLING_TYPES = set(SPORT_GROUPS['cycling'])  # from config.py

IMPACT_TYPES = {
    'running', 'trail_running', 'treadmill_running', 'track_running',
    'padel', 'ultimate_disc', 'multi_sport',
}

# Known injury events — extracted from athlete.json injury_history + progress_notes
INJURY_EVENTS = [
    {
        "date": "2023-04-01",
        "label": "First fibular stress fracture (approx)",
        "source": "Referenced in Jan 7 2026 entry: 'Previous stress fracture same area 2 years ago'",
    },
    {
        "date": "2026-01-06",
        "label": "Shin/anterior tibialis pain after Padel",
        "source": "injury_history[0]",
    },
    {
        "date": "2026-01-26",
        "label": "Ultimate Frisbee flare-up (restricted activity)",
        "source": "progress_notes Feb 1 entry",
    },
    {
        "date": "2026-02-01",
        "label": "Double Padel + Ultimate setback",
        "source": "progress_notes Feb 1 entry",
    },
    {
        "date": "2026-03-05",
        "label": "MRI confirms acute-on-chronic fibular stress fracture",
        "source": "progress_notes Mar 7 entry",
    },
]

# L/R balance keys to search for in activity details
LR_BALANCE_KEYS = {
    'leftBalance', 'rightBalance', 'leftRightBalance',
    'pedalSmoothness', 'torqueEffectiveness', 'leftPedalSmoothness',
    'rightPedalSmoothness', 'leftTorqueEffectiveness', 'rightTorqueEffectiveness',
    'metricDescriptors',
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def save_json(path: Path, data):
    """Atomic JSON save."""
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)
    logger.info("Saved %s", path.name)


def load_json(path: Path):
    """Load JSON file, return None if missing."""
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def search_keys_recursive(obj, target_keys: set, path="") -> list[tuple[str, any]]:
    """Recursively search a nested dict/list for keys matching target_keys."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            current = f"{path}.{k}" if path else k
            if k in target_keys:
                found.append((current, v))
            found.extend(search_keys_recursive(v, target_keys, current))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:5]):  # Sample first 5 items
            found.extend(search_keys_recursive(item, target_keys, f"{path}[{i}]"))
    return found


def date_range_chunks(start: date, end: date, chunk_months: int = 6):
    """Yield (chunk_start, chunk_end) pairs stepping backwards from end to start."""
    chunks = []
    cursor = end
    while cursor > start:
        chunk_start = cursor - timedelta(days=chunk_months * 30)
        if chunk_start < start:
            chunk_start = start
        chunks.append((chunk_start, cursor))
        cursor = chunk_start - timedelta(days=1)
    return list(reversed(chunks))


# ── Phase 1: Probe activity details for L/R balance data ────────────────────

def probe_activity_details() -> dict:
    """
    Find a recent indoor_cycling activity and probe get_activity_details()
    for L/R balance data. Returns findings dict.
    """
    print("\n" + "=" * 60)
    print("  PHASE 1: Probing activity details for L/R balance data")
    print("=" * 60)

    # Find a recent cycling activity
    end = date.today()
    start = end - timedelta(days=60)

    print(f"Fetching activities from {start} to {end}...")
    raw = garmin_api_call(
        lambda c, s=start.isoformat(), e=end.isoformat(): c.get_activities_by_date(s, e)
    )

    cycling_activities = [
        a for a in (raw or [])
        if a.get('activityType', {}).get('typeKey', '') in CYCLING_TYPES
    ]

    if not cycling_activities:
        print("No cycling activities found in last 60 days!")
        return {"found": False, "reason": "no_cycling_activities"}

    # Use most recent cycling activity
    target = cycling_activities[0]
    activity_id = target.get('activityId')
    activity_name = target.get('activityName', 'Unknown')
    print(f"Probing activity: {activity_name} (ID: {activity_id})")

    # Fetch full details
    print("Calling get_activity_details()...")
    details = garmin_api_call(
        lambda c, aid=activity_id: c.get_activity_details(aid, maxchart=2000, maxpoly=2000)
    )

    # Save raw probe response
    save_json(PROBE_FILE, details)
    print(f"Raw response saved to {PROBE_FILE}")

    # Search for L/R balance keys
    print("\nSearching for L/R balance keys...")
    matches = search_keys_recursive(details, LR_BALANCE_KEYS)

    findings = {
        "activity_id": activity_id,
        "activity_name": activity_name,
        "activity_type": target.get('activityType', {}).get('typeKey'),
        "found": len(matches) > 0,
        "matches": [],
    }

    if matches:
        print(f"\nFOUND {len(matches)} L/R balance fields:")
        for path, value in matches:
            sample = str(value)[:200] if value is not None else "null"
            print(f"  {path} = {sample}")
            findings["matches"].append({"path": path, "sample": sample})
    else:
        print("\nNo L/R balance data found in activity details.")
        print("L/R power balance is likely Wattbike-proprietary (not in Garmin).")

    return findings


# ── Phase 2: Fetch all cycling activities ────────────────────────────────────

def fetch_cycling_activities(lr_available: bool = False) -> list[dict]:
    """Fetch all cycling activities going back YEARS_BACK years."""
    print("\n" + "=" * 60)
    print("  PHASE 2: Fetching cycling activities")
    print("=" * 60)

    # Check for existing data (resume support)
    existing = load_json(CYCLING_FILE)
    if existing and isinstance(existing, list) and len(existing) > 0:
        print(f"Found existing data with {len(existing)} activities.")
        existing_ids = {a['activity_id'] for a in existing}
    else:
        existing = []
        existing_ids = set()

    end = date.today()
    start = end - timedelta(days=YEARS_BACK * 365)
    chunks = date_range_chunks(start, end)

    all_activities = list(existing)
    new_count = 0

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        print(f"  Chunk {i+1}/{len(chunks)}: {chunk_start} to {chunk_end}...", end=" ")

        try:
            raw = garmin_api_call(
                lambda c, s=chunk_start.isoformat(), e=chunk_end.isoformat(): c.get_activities_by_date(s, e)
            )
        except Exception as e:
            logger.error("Failed to fetch chunk %s-%s: %s", chunk_start, chunk_end, e)
            print(f"ERROR: {e}")
            continue

        if not raw:
            print("0 activities")
            continue

        # Filter to cycling types and parse
        chunk_cycling = []
        for a in raw:
            type_key = a.get('activityType', {}).get('typeKey', '')
            if type_key in CYCLING_TYPES:
                parsed = parse_activity(a)
                if parsed['activity_id'] not in existing_ids:
                    chunk_cycling.append(parsed)
                    existing_ids.add(parsed['activity_id'])

        new_count += len(chunk_cycling)
        all_activities.extend(chunk_cycling)
        print(f"{len(chunk_cycling)} new cycling activities")

        # Incremental save after each chunk
        if chunk_cycling:
            save_json(CYCLING_FILE, all_activities)

    print(f"\nTotal cycling activities: {len(all_activities)} ({new_count} new)")

    # Phase 2b: Fetch per-activity details for L/R balance if available
    if lr_available:
        print("\nFetching per-activity L/R balance data...")
        enriched = 0
        for j, activity in enumerate(all_activities):
            if activity.get('lr_balance') is not None:
                continue  # Already fetched
            aid = activity['activity_id']
            try:
                details = garmin_api_call(
                    lambda c, a_id=aid: c.get_activity_details(a_id, maxchart=100, maxpoly=100)
                )
                matches = search_keys_recursive(details, LR_BALANCE_KEYS)
                if matches:
                    lr_data = {path: val for path, val in matches if 'balance' in path.lower()}
                    if lr_data:
                        activity['lr_balance'] = lr_data
                        enriched += 1
                time.sleep(1)  # Rate limiting
            except Exception as e:
                logger.warning("Failed L/R fetch for %s: %s", aid, e)

            if (j + 1) % 50 == 0:
                save_json(CYCLING_FILE, all_activities)
                print(f"  Progress: {j+1}/{len(all_activities)}, enriched: {enriched}")

        save_json(CYCLING_FILE, all_activities)
        print(f"  Enriched {enriched} activities with L/R data")

    save_json(CYCLING_FILE, all_activities)
    return all_activities


# ── Phase 3: Fetch all impact activities ─────────────────────────────────────

def fetch_impact_activities() -> list[dict]:
    """Fetch all impact-type activities going back YEARS_BACK years."""
    print("\n" + "=" * 60)
    print("  PHASE 3: Fetching impact activities")
    print("=" * 60)

    existing = load_json(IMPACT_FILE)
    if existing and isinstance(existing, list) and len(existing) > 0:
        print(f"Found existing data with {len(existing)} activities.")
        existing_ids = {a['activity_id'] for a in existing}
    else:
        existing = []
        existing_ids = set()

    end = date.today()
    start = end - timedelta(days=YEARS_BACK * 365)
    chunks = date_range_chunks(start, end)

    all_activities = list(existing)
    new_count = 0

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        print(f"  Chunk {i+1}/{len(chunks)}: {chunk_start} to {chunk_end}...", end=" ")

        try:
            raw = garmin_api_call(
                lambda c, s=chunk_start.isoformat(), e=chunk_end.isoformat(): c.get_activities_by_date(s, e)
            )
        except Exception as e:
            logger.error("Failed to fetch chunk %s-%s: %s", chunk_start, chunk_end, e)
            print(f"ERROR: {e}")
            continue

        if not raw:
            print("0 activities")
            continue

        chunk_impact = []
        for a in raw:
            type_key = a.get('activityType', {}).get('typeKey', '')
            if type_key in IMPACT_TYPES:
                parsed = parse_activity(a)
                if parsed['activity_id'] not in existing_ids:
                    chunk_impact.append(parsed)
                    existing_ids.add(parsed['activity_id'])

        new_count += len(chunk_impact)
        all_activities.extend(chunk_impact)
        print(f"{len(chunk_impact)} new impact activities")

        if chunk_impact:
            save_json(IMPACT_FILE, all_activities)

    print(f"\nTotal impact activities: {len(all_activities)} ({new_count} new)")
    save_json(IMPACT_FILE, all_activities)
    return all_activities


# ── Phase 4: Injury timeline cross-reference ─────────────────────────────────

def build_injury_timeline(impact_activities: list[dict], cycling_activities: list[dict]) -> list[dict]:
    """
    For each injury event, create +-8 week window and calculate load patterns.
    """
    print("\n" + "=" * 60)
    print("  PHASE 4: Injury timeline cross-reference")
    print("=" * 60)

    # Index activities by date
    impact_by_date = defaultdict(list)
    for a in impact_activities:
        impact_by_date[a['date']].append(a)

    cycling_by_date = defaultdict(list)
    for a in cycling_activities:
        cycling_by_date[a['date']].append(a)

    timeline = []

    for event in INJURY_EVENTS:
        event_date = date.fromisoformat(event['date'])
        window_start = event_date - timedelta(weeks=8)
        window_end = event_date + timedelta(weeks=8)

        print(f"\n  {event['label']} ({event['date']})")
        print(f"  Window: {window_start} to {window_end}")

        # Build weekly summaries
        weekly_data = []
        current_week_start = window_start - timedelta(days=window_start.weekday())  # Monday

        while current_week_start <= window_end:
            week_end = current_week_start + timedelta(days=6)
            week_label = current_week_start.isoformat()

            # Collect activities for the week
            impact_load = 0.0
            cycling_load = 0.0
            impact_sessions = 0
            cycling_sessions = 0
            sport_types = set()

            day = current_week_start
            while day <= week_end:
                ds = day.isoformat()
                for a in impact_by_date.get(ds, []):
                    load = a.get('garmin_training_load') or 0.0
                    impact_load += load
                    impact_sessions += 1
                    sport_types.add(a['type'])
                for a in cycling_by_date.get(ds, []):
                    load = a.get('garmin_training_load') or 0.0
                    cycling_load += load
                    cycling_sessions += 1
                day += timedelta(days=1)

            weeks_from_event = (current_week_start - event_date).days / 7

            weekly_data.append({
                "week_start": week_label,
                "weeks_from_event": round(weeks_from_event, 1),
                "impact_load": round(impact_load, 1),
                "cycling_load": round(cycling_load, 1),
                "total_load": round(impact_load + cycling_load, 1),
                "impact_sessions": impact_sessions,
                "cycling_sessions": cycling_sessions,
                "sport_types": sorted(sport_types),
                "sport_variety": len(sport_types),
            })

            current_week_start += timedelta(weeks=1)

        # Flag >30% week-over-week load spikes
        for i in range(1, len(weekly_data)):
            prev_load = weekly_data[i-1]['impact_load']
            curr_load = weekly_data[i]['impact_load']
            if prev_load > 0:
                change_pct = ((curr_load - prev_load) / prev_load) * 100
            elif curr_load > 0:
                change_pct = 100.0  # From zero to something
            else:
                change_pct = 0.0
            weekly_data[i]['impact_load_change_pct'] = round(change_pct, 1)
            weekly_data[i]['spike'] = change_pct > 30

        # Calculate ACWR for impact loads using the project's fitness module
        daily_loads = {}
        for a in impact_activities:
            d = a['date']
            load = a.get('garmin_training_load') or 0.0
            daily_loads[d] = daily_loads.get(d, 0.0) + load

        acwr_at_event = None
        try:
            metrics = calculate_fitness_metrics(daily_loads, as_of_date=event_date)
            acwr_at_event = {
                "acwr": metrics.get('acwr'),
                "ctl": metrics.get('ctl'),
                "atl": metrics.get('atl'),
                "tsb": metrics.get('tsb'),
            }
            print(f"  ACWR at event: {metrics.get('acwr')} (CTL: {metrics.get('ctl')}, ATL: {metrics.get('atl')})")
        except Exception as e:
            logger.warning("ACWR calculation failed for %s: %s", event['date'], e)

        timeline.append({
            "date": event['date'],
            "label": event['label'],
            "source": event['source'],
            "acwr_at_event": acwr_at_event,
            "weekly_data": weekly_data,
        })

    return timeline


# ── Phase 5: Analysis & report generation ────────────────────────────────────

def analyze_cycling_trends(cycling_activities: list[dict]) -> dict:
    """Compute monthly cycling power/cadence trends."""
    print("\n  Analyzing cycling power trends...")

    # Group by month
    monthly = defaultdict(list)
    for a in cycling_activities:
        if a.get('date'):
            month_key = a['date'][:7]  # YYYY-MM
            monthly[month_key].append(a)

    monthly_averages = []
    for month in sorted(monthly.keys()):
        activities = monthly[month]
        powers = [a['avg_power'] for a in activities if a.get('avg_power')]
        norm_powers = [a['norm_power'] for a in activities if a.get('norm_power')]
        cadences = [a['avg_cadence'] for a in activities if a.get('avg_cadence')]
        loads = [a['garmin_training_load'] for a in activities if a.get('garmin_training_load')]

        monthly_averages.append({
            "month": month,
            "sessions": len(activities),
            "avg_power": round(sum(powers) / len(powers), 1) if powers else None,
            "avg_norm_power": round(sum(norm_powers) / len(norm_powers), 1) if norm_powers else None,
            "avg_cadence": round(sum(cadences) / len(cadences), 1) if cadences else None,
            "total_load": round(sum(loads), 1) if loads else 0,
            "avg_duration_mins": round(
                sum(a.get('duration_mins', 0) for a in activities) / len(activities), 1
            ),
        })

    # Check for L/R balance data
    lr_activities = [a for a in cycling_activities if a.get('lr_balance')]
    lr_trend = None
    if lr_activities:
        lr_trend = [
            {"date": a['date'], "lr_balance": a['lr_balance']}
            for a in sorted(lr_activities, key=lambda x: x['date'])
        ]

    return {
        "monthly_averages": monthly_averages,
        "lr_balance_trend": lr_trend,
        "lr_balance_available": len(lr_activities) > 0,
        "total_activities": len(cycling_activities),
    }


def analyze_impact_patterns(impact_activities: list[dict]) -> dict:
    """Analyze weekly impact loading patterns."""
    print("  Analyzing impact loading patterns...")

    # Group by ISO week
    weekly = defaultdict(list)
    for a in impact_activities:
        if a.get('date'):
            try:
                d = date.fromisoformat(a['date'])
                week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                weekly[week_key].append(a)
            except ValueError:
                pass

    weekly_loads = []
    for week in sorted(weekly.keys()):
        activities = weekly[week]
        total_load = sum(a.get('garmin_training_load') or 0 for a in activities)
        sport_types = set(a['type'] for a in activities)

        weekly_loads.append({
            "week": week,
            "total_load": round(total_load, 1),
            "sessions": len(activities),
            "sport_types": sorted(sport_types),
            "multi_sport": len(sport_types) > 1,
        })

    # Pre-injury spike detection: 4-week rolling vs 1-week acute
    spike_analysis = []
    for i in range(4, len(weekly_loads)):
        chronic_4wk = sum(w['total_load'] for w in weekly_loads[i-4:i]) / 4
        acute_1wk = weekly_loads[i]['total_load']
        ratio = round(acute_1wk / chronic_4wk, 2) if chronic_4wk > 0 else 0

        if ratio > 1.3 or acute_1wk > chronic_4wk * 1.3:
            spike_analysis.append({
                "week": weekly_loads[i]['week'],
                "acute_load": acute_1wk,
                "chronic_4wk_avg": round(chronic_4wk, 1),
                "ratio": ratio,
                "sports": weekly_loads[i]['sport_types'],
            })

    # Sport combination analysis: weeks with 2+ impact sports
    multi_sport_weeks = [w for w in weekly_loads if w['multi_sport']]

    return {
        "weekly_loads": weekly_loads,
        "spike_weeks": spike_analysis,
        "multi_sport_weeks": len(multi_sport_weeks),
        "total_weeks_with_impact": len(weekly_loads),
        "sport_distribution": _count_sport_distribution(impact_activities),
    }


def _count_sport_distribution(activities: list[dict]) -> dict:
    """Count activities by sport type."""
    counts = defaultdict(int)
    for a in activities:
        counts[a.get('type', 'unknown')] += 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def detect_boom_bust(activities: list[dict]) -> list[dict]:
    """Detect gaps >14 days followed by sudden volume spikes in impact activities."""
    print("  Detecting boom/bust patterns...")

    if not activities:
        return []

    # Sort by date
    sorted_acts = sorted(activities, key=lambda a: a.get('date', ''))
    patterns = []

    for i in range(1, len(sorted_acts)):
        prev_date_str = sorted_acts[i-1].get('date', '')
        curr_date_str = sorted_acts[i].get('date', '')
        if not prev_date_str or not curr_date_str:
            continue

        try:
            prev_date = date.fromisoformat(prev_date_str)
            curr_date = date.fromisoformat(curr_date_str)
        except ValueError:
            continue

        gap_days = (curr_date - prev_date).days
        if gap_days > 14:
            # Look at load in the 7 days after the gap
            post_gap_load = 0.0
            post_gap_sessions = 0
            for a in sorted_acts[i:]:
                a_date_str = a.get('date', '')
                if not a_date_str:
                    continue
                try:
                    a_date = date.fromisoformat(a_date_str)
                except ValueError:
                    continue
                if a_date > curr_date + timedelta(days=7):
                    break
                post_gap_load += a.get('garmin_training_load') or 0.0
                post_gap_sessions += 1

            if post_gap_sessions > 0:
                patterns.append({
                    "gap_start": prev_date_str,
                    "gap_end": curr_date_str,
                    "gap_days": gap_days,
                    "post_gap_7d_load": round(post_gap_load, 1),
                    "post_gap_7d_sessions": post_gap_sessions,
                    "return_activity": sorted_acts[i].get('type'),
                })

    return patterns


def generate_findings(
    injury_timeline: list[dict],
    cycling_trends: dict,
    impact_patterns: dict,
    boom_bust: list[dict],
) -> tuple[list[dict], list[str], list[str]]:
    """Generate structured findings, data gaps, and recommendations."""
    findings = []
    data_gaps = []
    recommendations = []

    # Finding 1: Impact load spikes before injuries
    for event in injury_timeline:
        spikes_before = [
            w for w in event.get('weekly_data', [])
            if w.get('spike') and -8 <= w.get('weeks_from_event', 0) < 0
        ]
        if spikes_before:
            findings.append({
                "finding": f"Impact load spike detected before '{event['label']}'",
                "evidence": f"{len(spikes_before)} weeks with >30% load increase in 8 weeks pre-injury. "
                           f"Spikes at weeks: {[w['weeks_from_event'] for w in spikes_before]}",
                "confidence": "high",
            })

    # Finding 2: Multi-sport impact weeks
    multi_weeks = impact_patterns.get('multi_sport_weeks', 0)
    total_weeks = impact_patterns.get('total_weeks_with_impact', 1)
    if multi_weeks > 0:
        pct = round(multi_weeks / total_weeks * 100, 1)
        findings.append({
            "finding": f"Multi-impact-sport weeks occur {pct}% of the time ({multi_weeks}/{total_weeks} weeks)",
            "evidence": "Weeks combining running + padel + ultimate create compounding impact loads",
            "confidence": "high",
        })

    # Finding 3: Boom/bust patterns
    if boom_bust:
        findings.append({
            "finding": f"Detected {len(boom_bust)} boom/bust patterns (>14 day gap then return to impact)",
            "evidence": f"Average gap: {sum(b['gap_days'] for b in boom_bust) / len(boom_bust):.0f} days. "
                       f"Common return sport: {_most_common([b.get('return_activity') for b in boom_bust])}",
            "confidence": "medium",
        })

    # Finding 4: ACWR at injury events
    for event in injury_timeline:
        acwr_data = event.get('acwr_at_event')
        if acwr_data and acwr_data.get('acwr'):
            acwr_val = acwr_data['acwr']
            if acwr_val > 1.3:
                findings.append({
                    "finding": f"Elevated ACWR ({acwr_val}) at '{event['label']}'",
                    "evidence": f"CTL: {acwr_data['ctl']}, ATL: {acwr_data['atl']}, TSB: {acwr_data['tsb']}",
                    "confidence": "high",
                })
            elif acwr_val < 0.8:
                findings.append({
                    "finding": f"Low ACWR ({acwr_val}) at '{event['label']}' — possible detraining then spike",
                    "evidence": f"CTL: {acwr_data['ctl']}, ATL: {acwr_data['atl']}. "
                               "Low chronic load means any acute load creates a spike.",
                    "confidence": "medium",
                })

    # Data gaps
    if not cycling_trends.get('lr_balance_available'):
        data_gaps.append(
            "L/R power balance NOT available in Garmin activity details. "
            "This is a Wattbike-proprietary metric. Recommend exporting from Wattbike Hub "
            "or recording Fmax/PES data manually from Wattbike Polar View."
        )
    data_gaps.append(
        "Fmax angles (force application timing) are Wattbike-only. "
        "Not available in Garmin. Export .fit files or use Wattbike Hub API."
    )
    if not any(a.get('garmin_training_load') for a in []):
        data_gaps.append(
            "Activities before ~2020 may lack Garmin training load (EPOC) data. "
            "Load analysis accuracy decreases further back in time."
        )

    # Recommendations
    recommendations.append(
        "Export Wattbike Hub data to get L/R power balance trend and Fmax angles over time."
    )
    recommendations.append(
        "Track impact activity volume separately from cycling — use sport-specific ACWR "
        "to gate return-to-running decisions."
    )
    recommendations.append(
        "Avoid combining 2+ impact sports in the same week when returning from a gap >14 days."
    )
    recommendations.append(
        "Weekly impact load should increase <30% week-over-week (align with ACWR 0.8-1.3 target)."
    )

    return findings, data_gaps, recommendations


def _most_common(items: list) -> str:
    """Return most common non-None item."""
    counts = defaultdict(int)
    for item in items:
        if item:
            counts[item] += 1
    if not counts:
        return "unknown"
    return max(counts, key=counts.get)


def generate_report(
    probe_findings: dict,
    injury_timeline: list[dict],
    cycling_trends: dict,
    impact_patterns: dict,
    boom_bust: list[dict],
    cycling_count: int,
    impact_count: int,
) -> dict:
    """Assemble the final investigation report."""
    print("\n" + "=" * 60)
    print("  PHASE 5: Generating investigation report")
    print("=" * 60)

    findings, data_gaps, recommendations = generate_findings(
        injury_timeline, cycling_trends, impact_patterns, boom_bust
    )

    # Determine date coverage
    all_dates = []
    cycling = load_json(CYCLING_FILE) or []
    impact = load_json(IMPACT_FILE) or []
    for a in cycling + impact:
        if a.get('date'):
            all_dates.append(a['date'])
    all_dates.sort()

    report = {
        "investigation": "Recurring left fibular stress fracture",
        "generated": date.today().isoformat(),
        "context": {
            "history": "April 2023 first stress fracture, March 2026 MRI confirms acute-on-chronic — same bone, same spot",
            "bloods": "Normal (Vit D, calcium, magnesium, phosphate, thyroid, kidney, FBC) — cause is mechanical",
            "wattbike_observations": "L/R power imbalance 48/52%, late Fmax angles L:98° R:106°",
        },
        "injury_timeline": injury_timeline,
        "data_coverage": {
            "cycling_count": cycling_count,
            "impact_count": impact_count,
            "date_range": f"{all_dates[0]} to {all_dates[-1]}" if all_dates else "no data",
        },
        "probe_findings": probe_findings,
        "cycling_metrics": cycling_trends,
        "impact_analysis": impact_patterns,
        "boom_bust_patterns": boom_bust,
        "findings": findings,
        "data_gaps": data_gaps,
        "recommendations": recommendations,
    }

    save_json(REPORT_FILE, report)
    print(f"\nReport saved to {REPORT_FILE}")
    print(f"\nFindings: {len(findings)}")
    for f in findings:
        print(f"  [{f['confidence']}] {f['finding']}")
    print(f"\nData gaps: {len(data_gaps)}")
    for g in data_gaps:
        print(f"  - {g[:80]}...")
    print(f"\nRecommendations: {len(recommendations)}")
    for r in recommendations:
        print(f"  - {r[:80]}")

    return report


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fibular stress fracture investigation")
    parser.add_argument("--skip-probe", action="store_true", help="Skip Phase 1 L/R probe")
    parser.add_argument("--report-only", action="store_true", help="Skip fetching, regenerate report")
    args = parser.parse_args()

    INVESTIGATION_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "#" * 60)
    print("  Biomechanical Investigation: Recurring Fibular Stress Fracture")
    print("#" * 60)

    # Phase 1: Probe for L/R data
    if args.report_only:
        probe_findings = load_json(PROBE_FILE) or {"found": False, "reason": "skipped"}
        lr_available = probe_findings.get("found", False)
    elif args.skip_probe:
        probe_findings = {"found": False, "reason": "skipped"}
        lr_available = False
    else:
        probe_findings = probe_activity_details()
        lr_available = probe_findings.get("found", False)

        if not lr_available:
            print("\n" + "-" * 60)
            print("  L/R balance NOT found in Garmin activity details.")
            print("  The script will continue without L/R data.")
            print("  Recommend exporting from Wattbike Hub for L/R analysis.")
            print("-" * 60)

    # Phase 2 & 3: Fetch activities
    if args.report_only:
        cycling_activities = load_json(CYCLING_FILE) or []
        impact_activities = load_json(IMPACT_FILE) or []
    else:
        cycling_activities = fetch_cycling_activities(lr_available=lr_available)
        impact_activities = fetch_impact_activities()

    if not cycling_activities and not impact_activities:
        print("\nNo activity data found. Cannot generate report.")
        return

    # Phase 4: Injury timeline
    injury_timeline = build_injury_timeline(impact_activities, cycling_activities)

    # Phase 5: Analysis & report
    cycling_trends = analyze_cycling_trends(cycling_activities)
    impact_patterns = analyze_impact_patterns(impact_activities)
    boom_bust = detect_boom_bust(impact_activities)

    report = generate_report(
        probe_findings=probe_findings,
        injury_timeline=injury_timeline,
        cycling_trends=cycling_trends,
        impact_patterns=impact_patterns,
        boom_bust=boom_bust,
        cycling_count=len(cycling_activities),
        impact_count=len(impact_activities),
    )

    print("\n" + "#" * 60)
    print("  Investigation complete!")
    print(f"  Report: {REPORT_FILE}")
    print("#" * 60)


if __name__ == "__main__":
    main()
