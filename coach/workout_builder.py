"""
Workout Builder - renders plan sessions as Garmin workouts.

The renderer is deliberately thin: it draws exactly what the session says
(the authoring contract in ``coach/authoring.py``) and never defaults a
value the plan did not state. A session that does not satisfy the contract
raises ``WorkoutAuthoringError`` carrying the teaching errors; nothing is
silently degraded. ``build_workout_report`` wraps the same rendering with
a human-readable step tree and the warnings, for the push preview and the
``preview_workout`` tool.

Uses the garminconnect.workout models for running and cycling and plain
dicts for the sports garminconnect uploads generically.
"""

from .config import DATA_DIR, ATHLETE_FILE, EXERCISE_LIBRARY_FILE
from .web_utils import sanitize_text
from .fitness import get_athlete_hr_zones
from .taxonomy import types_in_family, workout_family_for
from .authoring import (
    COMPOUND_CATEGORIES,
    PLAIN_SPLIT,
    INTENSITY_ZONES,
    STEP_NOTE_MAX_CHARS,
    WORKOUT_NAME_MAX_CHARS,
    VALID_WORKOUT_CATEGORIES,
    CATALOGUE_CATEGORY_TO_WORKOUT,
    AuthoringReport,
    Circuit,
    Exercise,
    CircuitExercise,
    Phase,
    RepeatBlock,
    normalise_intensity,
    resolve_exercise_category,
    validate_exercises,
    validate_session,
    validate_structure,
)
from garminconnect.workout import (
    CyclingWorkout,
    RunningWorkout,
    WorkoutSegment,
    ExecutableStep,
    RepeatGroup,
)
import logging

logger = logging.getLogger(__name__)

__all__ = [
    'WorkoutAuthoringError', 'build_workout', 'build_workout_report', 'describe_workout',
    'VALID_WORKOUT_CATEGORIES', 'CATALOGUE_CATEGORY_TO_WORKOUT',
]

# Sport type definitions
RUNNING_SPORT = {
    "sportTypeId": 1,
    "sportTypeKey": "running",
    "displayOrder": 1
}

CYCLING_SPORT = {
    "sportTypeId": 2,
    "sportTypeKey": "cycling",
    "displayOrder": 2
}

STRENGTH_SPORT = {
    "sportTypeId": 5,
    "sportTypeKey": "strength_training",
    "displayOrder": 5
}

YOGA_SPORT = {
    "sportTypeId": 7,
    "sportTypeKey": "yoga",
    "displayOrder": 7
}

PILATES_SPORT = {
    "sportTypeId": 8,
    "sportTypeKey": "pilates",
    "displayOrder": 8
}

SWIMMING_SPORT = {
    "sportTypeId": 4,
    "sportTypeKey": "swimming",
    "displayOrder": 4
}

# Padel is Garmin ACTIVITY type 226 'paddelball' (child of 219 racket_sports),
# which is what the watch records. Garmin's workout service has no padel or
# racket sport (workout-service/workout/types), so padel workouts are stored
# with no sport type whatever is sent: the calendar entry is a reminder and
# the session is recorded with the watch's Padel activity. Don't remap padel
# to cardio/other: a workout started from the watch records as its sport.
PADEL_SPORT = {
    "sportTypeId": 226,
    "sportTypeKey": "paddelball",
    "displayOrder": 226
}

# Step type definitions
STEP_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1}
STEP_COOLDOWN = {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2}
STEP_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
STEP_RECOVERY = {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}
STEP_REST = {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5}
STEP_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}

PHASE_STEP_TYPES = {
    "warmup": STEP_WARMUP,
    "cooldown": STEP_COOLDOWN,
    "recovery": STEP_RECOVERY,
    "rest": STEP_REST,
    "interval": STEP_INTERVAL,
}

# End condition definitions (Garmin Connect workout-API ids: 1 lap.button,
# 2 time, 3 distance, 7 iterations, 10 reps). Distance was once sent as 1 —
# the lap-button id — the same bug garminconnect fixed upstream in 0.3.6.
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
END_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
END_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
END_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}

# Rest duration used ONLY to estimate a strength session's length when the
# plan states no rest_secs (the rest step itself is a lap button then).
DEFAULT_REST_SECS = 45

# Target type definitions
TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
TARGET_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}
TARGET_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence.zone", "displayOrder": 3}
TARGET_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}

WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# Session types that map to cycling — derived from the canonical taxonomy
# (coach/taxonomy.py) so Garmin types like "mountain_biking" and
# "gravel_cycling" dispatch to the cycling builder instead of being skipped
# as unknown when pushing plans to Garmin.
CYCLING_TYPES = set(types_in_family("cycling"))

# Indoor cycling types - use power targets (athlete has Wattbike indoors)
INDOOR_CYCLING_TYPES = {"indoor_cycling", "wattbike", "trainer", "turbo_trainer",
                        "ftp_test", "threshold_test", "indoor_technique"}

# Long outdoor ride types - skip warmup section (messes with lap timing)
LONG_OUTDOOR_RIDE_TYPES = {"long_ride", "long_mtb_ride"}

# Session types that map to running
RUNNING_TYPES = set(types_in_family("running"))

# Session types that map to yoga (includes mobility/stretching plan aliases)
YOGA_TYPES = set(types_in_family("yoga"))

# Session types that map to pilates (used for rehab/mobility work)
PILATES_TYPES = set(types_in_family("pilates"))

# Session types that map to strength
STRENGTH_TYPES = set(types_in_family("strength"))

# Session types that map to swimming
SWIMMING_TYPES = set(types_in_family("swimming"))

# Session types that map to padel
PADEL_TYPES = set(types_in_family("padel"))

# Session types to skip (not pushable to Garmin)
SKIP_TYPES = set(types_in_family("rest"))

# Test session types (special handling)
TEST_TYPES = {"ftp_test", "threshold_test"}


class WorkoutAuthoringError(ValueError):
    """The session does not satisfy the authoring contract. ``problems`` is
    the list of {'field', 'message'} teaching errors; ``warnings`` rides
    along so a preview can show both."""

    def __init__(self, problems: list[dict], warnings: list[dict] | None = None):
        self.problems = list(problems)
        self.warnings = list(warnings or [])
        super().__init__('; '.join(f"{p['field']}: {p['message']}" for p in self.problems))


def _smart_truncate(text: str, max_len: int) -> str:
    """Truncate at word boundary with ellipsis if needed."""
    if not text or len(text) <= max_len:
        return text
    # Leave room for ellipsis
    cutoff = max_len - 1
    # Find last word boundary before cutoff
    space_idx = text.rfind(" ", 0, cutoff)
    if space_idx > max_len // 2:  # Only use boundary if it's not cutting too much
        return text[:space_idx] + "…"
    return text[:cutoff] + "…"


def _workout_name(session: dict, default: str) -> str:
    """The plan's `name` wins, then `description`; cut to Garmin's 40."""
    raw = session.get("name") or session.get("description") or default
    return str(raw)[:WORKOUT_NAME_MAX_CHARS]


# ---------------------------------------------------------------------------
# Zones and targets (ONE intensity vocabulary, in coach/authoring.py)
# ---------------------------------------------------------------------------

# Default HR zones if athlete profile not configured
DEFAULT_HR_ZONES = {
    "z1_recovery": [0, 120],
    "z2_aerobic": [120, 140],
    "z3_tempo": [140, 155],
    "z4_threshold": [155, 170],
    "z5_max": [170, 184]
}


def _read_data_json(filename: str) -> dict:
    """Locked read of a data-dir JSON file ({} when missing).

    DATA_DIR is read at call time so tests can monkeypatch it. The old
    hard-coded <repo>/data path returned {} on every installed copy and its
    bare open() could break another process's atomic write on Windows.
    """
    from . import storage  # function-local: avoids module-level import cycle
    return storage.read_json(filename, data_dir=DATA_DIR) or {}


def get_hr_target_for_intensity(intensity: str) -> tuple[int, int] | None:
    """HR range (low, high) for an intensity word; None when the word is not
    in the vocabulary or the zone is not configured."""
    word = normalise_intensity(intensity)
    if word is None:
        return None
    hr_zones = get_athlete_hr_zones() or DEFAULT_HR_ZONES
    zone_range = hr_zones.get(INTENSITY_ZONES[word][0])
    if zone_range and len(zone_range) == 2:
        return (zone_range[0], zone_range[1])
    return None


def get_athlete_power_zones() -> dict:
    """Load athlete power zones from athlete.json."""
    athlete_path = DATA_DIR / ATHLETE_FILE
    try:
        athlete = _read_data_json(ATHLETE_FILE)
        if not athlete:
            logger.warning("Athlete file not found at %s — power targets unavailable", athlete_path)
            return {}
        return athlete.get("personal", {}).get("power_zones", {}) or {}
    except Exception:
        logger.warning("Failed to load power zones from %s", athlete_path, exc_info=True)
        return {}


def get_athlete_ftp() -> float | None:
    """The athlete's FTP (personal.ftp) — the only source for power_pct."""
    try:
        ftp = (_read_data_json(ATHLETE_FILE).get("personal", {}) or {}).get("ftp")
        return float(ftp) if ftp else None
    except Exception:
        logger.warning("Failed to load FTP from %s", DATA_DIR / ATHLETE_FILE, exc_info=True)
        return None


def get_power_target_for_intensity(intensity: str) -> tuple[int, int] | None:
    """Power range (low, high) in watts for an intensity word; None when the
    word is unknown or power zones are not configured."""
    word = normalise_intensity(intensity)
    if word is None:
        return None
    power_zones = get_athlete_power_zones()
    if not power_zones:
        return None
    zone_range = power_zones.get(INTENSITY_ZONES[word][1])
    if zone_range and len(zone_range) == 2:
        low_watts, high_watts = zone_range
        if high_watts is None:
            high_watts = low_watts + 100  # open-ended top zone: cap at +100 W
        return (low_watts, high_watts)
    return None


def is_indoor_cycling(session: dict) -> bool:
    """
    Determine if a cycling session is indoor (use power) or outdoor (use HR).

    Indoor indicators:
    - Session type contains 'indoor', 'wattbike', 'trainer'
    - Session has 'indoor: true' field
    - FTP tests are always indoor
    """
    session_type = session.get("type", "").lower()

    # Check explicit indoor types
    if session_type in INDOOR_CYCLING_TYPES:
        return True

    # Check for 'indoor' in type name
    if "indoor" in session_type or "wattbike" in session_type or "trainer" in session_type:
        return True

    # Check explicit indoor flag
    if session.get("indoor", False):
        return True

    return False


def is_simple_outdoor_ride(session: dict) -> bool:
    """
    Determine if this is a simple outdoor ride that should skip warmup.

    Simple outdoor rides (single zone, no structure) don't need warmup sections.
    Athlete warms up naturally. Adding warmup/cooldown messes with lap timing.

    Skip warmup if:
    - Outdoor (not indoor)
    - No 'structure' field (no intervals/tempo sections defined)
    - Single intensity ride (easy, recovery, endurance, z2)

    Keep warmup for:
    - Indoor rides (structured training on Wattbike)
    - Structured rides with intervals/tempo (have 'structure' field)
    """
    # Don't skip warmup for indoor rides - they're structured training
    if is_indoor_cycling(session):
        return False

    # If ride has a 'structure' field, it's a structured workout - keep sections
    if session.get("structure"):
        return False

    # Simple outdoor rides - skip warmup
    return True


def get_athlete_running_zones() -> dict:
    """
    Load athlete running zones from athlete.json.

    If threshold_pace is set but pace_zones are null, calculates zones
    using sports science percentages based on threshold pace.

    Zone calculations (based on Jack Daniels methodology):
    - Z1 Recovery: 70-75% of threshold pace (25-30% slower)
    - Z2 Easy: 76-85% of threshold pace (15-24% slower)
    - Z3 Tempo: 86-95% of threshold pace (5-14% slower)
    - Z4 Threshold: 96-102% of threshold pace (±2-4%)
    - Z5 Interval: 103-115% of threshold pace (3-15% faster)

    Returns pace ranges as [slow_sec_per_km, fast_sec_per_km]
    """
    athlete_path = DATA_DIR / ATHLETE_FILE
    try:
        athlete = _read_data_json(ATHLETE_FILE)
        if not athlete:
            raise FileNotFoundError(athlete_path)

        personal = athlete.get("personal", {})
        threshold_pace = personal.get("threshold_pace_sec_per_km")
        pace_zones = personal.get("pace_zones", {})

        # If zones are already set, use them
        if pace_zones and pace_zones.get("z2_easy"):
            return pace_zones

        # If no threshold pace, can't calculate zones
        if not threshold_pace:
            return {}

        # Calculate zones from threshold pace
        # Slower pace = higher sec/km, faster = lower sec/km
        return {
            "z1_recovery": [int(threshold_pace * 1.25), int(threshold_pace * 1.30)],  # 25-30% slower
            "z2_easy": [int(threshold_pace * 1.15), int(threshold_pace * 1.24)],      # 15-24% slower
            "z3_tempo": [int(threshold_pace * 1.05), int(threshold_pace * 1.14)],     # 5-14% slower
            "z4_threshold": [int(threshold_pace * 0.96), int(threshold_pace * 1.04)], # ±4%
            "z5_interval": [int(threshold_pace * 0.85), int(threshold_pace * 0.95)],  # 5-15% faster
        }
    except FileNotFoundError:
        logger.warning("Athlete file not found at %s — pace targets unavailable", athlete_path)
        return {}
    except Exception:
        logger.warning("Failed to load running zones from %s", athlete_path, exc_info=True)
        return {}


def _pace_pair_to_mps(slow_sec_per_km: float, fast_sec_per_km: float) -> tuple[float, float]:
    """(slow_mps, fast_mps) — Garmin takes speeds, low first."""
    if slow_sec_per_km < fast_sec_per_km:
        slow_sec_per_km, fast_sec_per_km = fast_sec_per_km, slow_sec_per_km
    return (1000.0 / slow_sec_per_km, 1000.0 / fast_sec_per_km)


def get_pace_target_for_intensity(intensity: str) -> tuple[float, float] | None:
    """Pace range (slow_mps, fast_mps) for an intensity word; None when the
    word is unknown or no threshold pace / pace zones are configured."""
    word = normalise_intensity(intensity)
    if word is None:
        return None
    pace_zones = get_athlete_running_zones()
    if not pace_zones:
        return None
    zone_range = pace_zones.get(INTENSITY_ZONES[word][2])
    if zone_range and len(zone_range) == 2:
        return _pace_pair_to_mps(float(zone_range[0]), float(zone_range[1]))
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _has_structure(session: dict) -> bool:
    return bool(session.get("structure")) or bool(session.get("exercises"))


def _is_discretion(session: dict) -> bool:
    return str(session.get("intensity") or "").strip().lower() == "discretion"


def _discretion_note(session: dict) -> str:
    """The constraints of an athlete-choice day, as the main step's cue."""
    constraints = session.get("constraints") or []
    if isinstance(constraints, str):
        constraints = [constraints]
    text = "; ".join(str(c) for c in constraints if c)
    return _smart_truncate(f"Your call: {text}" if text else "Your call", STEP_NOTE_MAX_CHARS)


def _split_default(total_secs: float, fraction: float, cap_mins: int) -> float:
    """Seconds for a default warm-up/cool-down: a fraction of the session, capped."""
    return min(cap_mins * 60.0, total_secs * fraction)


def _plain_split(session: dict, total_secs: float, warmup_default: float, cooldown_default: float
                 ) -> tuple[float, float, float]:
    """(warmup, main, cooldown) seconds for a plain session: the plan's
    warmup_mins / cooldown_mins when stated, else the documented defaults."""
    warmup = float(session["warmup_mins"]) * 60 if session.get("warmup_mins") else warmup_default
    cooldown = float(session["cooldown_mins"]) * 60 if session.get("cooldown_mins") else cooldown_default
    main = max(0.0, total_secs - warmup - cooldown)
    return warmup, main, cooldown


def build_workout(session: dict, date: str, report: AuthoringReport | None = None
                  ) -> CyclingWorkout | RunningWorkout | dict | None:
    """
    Convert a plan session to a Garmin workout.

    Args:
        session: Plan session dict (type, duration_mins, description, structure/exercises ...)
        date: Date string for naming (YYYY-MM-DD)
        report: an AuthoringReport from validate_session (built here when omitted);
            rendering warnings are appended to it.

    Returns:
        CyclingWorkout, RunningWorkout, dict (yoga/strength/...), or None when
        there is nothing to push (rest day, unknown type, zero-length plain
        session).

    Raises:
        WorkoutAuthoringError: the session violates the authoring contract.
    """
    session_type = session.get("type", "").lower()
    duration_mins = session.get("duration_mins", 0)

    # Skip rest days
    if session_type in SKIP_TYPES:
        return None

    # The contract first: a session that says something wrong (0 minutes,
    # text where a number belongs) is an error, not a placeholder.
    if report is None:
        report = validate_session(session, load_exercise_db(), load_strength_baseline())
    if report.errors:
        raise WorkoutAuthoringError(report.errors, report.warnings)

    # Plain sessions with nothing to draw (type + purpose only) stay off the watch
    if not duration_mins and not _has_structure(session):
        return None

    # Determine sport via taxonomy workout family (substring fallbacks keep
    # free-form plan types like "morning_run" working)
    family = workout_family_for(session_type)

    if session_type in TEST_TYPES or "ftp_test" in session_type:
        return build_ftp_test_workout(session, date)
    elif family == "cycling" or "ride" in session_type or "cycling" in session_type:
        return build_cycling_workout(session, date, report)
    elif family == "running" or "run" in session_type:
        return build_running_workout(session, date, report)
    elif family == "swimming":
        return build_swimming_workout(session, date)
    elif family == "yoga":
        return build_yoga_workout(session, date)
    elif family == "pilates":
        return build_pilates_workout(session, date)
    elif family == "strength":
        return build_strength_workout(session, date, report)
    elif family == "padel":
        return build_padel_workout(session, date)

    return None


def build_workout_report(session: dict, date: str) -> dict:
    """Render a session and describe the result: what the watch will show.

    Returns {'ok', 'errors', 'warnings', 'workout', 'workout_type', 'name',
    'sport', 'estimated_mins', 'steps'}. Never raises for a contract
    violation: `errors` carries the teaching messages and `workout` is None.
    """
    workout_type = get_workout_type_name(session)
    result = {'ok': False, 'errors': [], 'warnings': [], 'workout': None,
              'workout_type': workout_type, 'name': None, 'sport': None,
              'estimated_mins': None, 'steps': []}
    if workout_type == 'skipped':
        result['ok'] = True
        result['warnings'].append({'field': 'type', 'message': 'rest day — nothing is pushed'})
        return result
    if workout_type == 'unknown':
        result['errors'].append({'field': 'type', 'message':
                                 f"{session.get('type')!r} has no Garmin workout family; nothing can be pushed"})
        return result
    report = validate_session(session, load_exercise_db(), load_strength_baseline())
    try:
        workout = build_workout(session, date, report)
    except WorkoutAuthoringError as exc:
        result['errors'] = exc.problems
        result['warnings'] = exc.warnings
        return result
    except Exception as exc:  # a malformed value in a lenient lane must name itself, not kill the push
        logger.exception("build_workout failed for %s on %s", session.get('type'), date)
        result['errors'] = [{'field': 'session', 'message': f"cannot render: {exc.__class__.__name__}: {exc}"}]
        result['warnings'] = report.warnings
        return result
    result['warnings'] = report.warnings
    if workout is None:
        # A placeholder (type + purpose, no duration, structure or exercises)
        # is a legitimate plan entry; it just never reaches the watch.
        result['ok'] = True
        result['warnings'].append({'field': 'duration_mins', 'message':
                                   'nothing to render: no duration_mins, structure or exercises — '
                                   'nothing is pushed for this session'})
        return result
    result['ok'] = True
    result['workout'] = workout
    result.update(describe_workout(workout))
    if (workout_type in ('running', 'cycling') and not get_athlete_hr_zones()
            and any('@ HR ' in line for line in result['steps'])):
        result['warnings'].append({'field': 'intensity', 'message': (
            'the profile has no HR zones (personal.hr_zones), so the HR targets on these steps '
            'are the generic defaults, not this athlete\'s — set the zones or state hr_target')})
    return result


# ---------------------------------------------------------------------------
# FTP test (protocol-driven, unchanged contract: `protocol` phases)
# ---------------------------------------------------------------------------

def build_ftp_test_workout(session: dict, date: str) -> CyclingWorkout:
    """
    Build an FTP test workout with proper protocol phases and cadence targets.

    Creates a structured 5-phase workout with RPM guidance:
    1. Warmup (15 min) - 90-100 RPM easy spinning
    2. Blowout (5 min) - 100-110 RPM all-out effort to deplete anaerobic
    3. Recovery (5 min) - 80-90 RPM easy spinning
    4. Test (20 min) - 85-95 RPM sustainable cadence for threshold effort
    5. Cooldown (5 min) - 80-90 RPM easy spinning

    The blowout phase is critical - it prevents anaerobic contribution
    from inflating the 20-minute power number.

    Cadence targets are used because they're independent of power output,
    making the workout accessible to beginners who don't have FTP yet.
    """
    description = session.get("name") or session.get("description", "FTP Test")
    protocol = session.get("protocol", [])

    # FTP tests are always indoor - add tag to name (skip if already tagged)
    if "(Indoor)" in description or "(Outdoor)" in description:
        workout_name = description[:40]
    else:
        workout_name = f"{description[:32]} (Indoor)"

    # Default FTP test structure with cadence targets if no protocol provided
    # Blowout is 3 x 1-min ALL OUT with 1-min recovery between (5 min total)
    if not protocol:
        protocol = [
            # Warmup: 15 min easy
            {"phase": "warmup", "duration_mins": 15, "cadence_min": 90, "cadence_max": 100,
             "notes": "Easy spin @ 90-100rpm"},
            # Blowout: 3 x 1-min ALL OUT with 1-min recovery between
            {"phase": "blowout", "duration_mins": 1, "cadence_min": 100, "cadence_max": 110,
             "notes": "ALL OUT 1/3 - GO!"},
            {"phase": "recovery", "duration_mins": 1, "cadence_min": 80, "cadence_max": 90,
             "notes": "Easy spin"},
            {"phase": "blowout", "duration_mins": 1, "cadence_min": 100, "cadence_max": 110,
             "notes": "ALL OUT 2/3 - GO!"},
            {"phase": "recovery", "duration_mins": 1, "cadence_min": 80, "cadence_max": 90,
             "notes": "Easy spin"},
            {"phase": "blowout", "duration_mins": 1, "cadence_min": 100, "cadence_max": 110,
             "notes": "ALL OUT 3/3 - DONE!"},
            # Main recovery: 5 min before test
            {"phase": "recovery", "duration_mins": 5, "cadence_min": 80, "cadence_max": 90,
             "notes": "Easy spin. Let HR drop."},
            # Test: 20 min max effort
            {"phase": "test", "duration_mins": 20, "cadence_min": 85, "cadence_max": 95,
             "notes": "20min MAX - steady pace!"},
            # Cooldown
            {"phase": "cooldown", "duration_mins": 5, "cadence_min": 80, "cadence_max": 90,
             "notes": "Easy spin. Cool down."}
        ]

    steps = []
    step_order = 1
    total_secs = 0

    for phase in protocol:
        phase_name = phase.get("phase", phase.get("name", "interval")).lower()
        duration_mins = phase.get("duration_mins", 5)
        duration_secs = duration_mins * 60
        total_secs += duration_secs
        notes = phase.get("notes", "")

        # Get cadence targets if specified
        cadence_min = phase.get("cadence_min")
        cadence_max = phase.get("cadence_max")

        # Map phase to step type
        if phase_name == "warmup":
            step_type = STEP_WARMUP
        elif phase_name == "cooldown":
            step_type = STEP_COOLDOWN
        elif phase_name == "recovery":
            step_type = STEP_RECOVERY
        else:
            step_type = STEP_INTERVAL

        # Create step - use cadence target if provided, otherwise no target
        if cadence_min and cadence_max:
            step = ExecutableStep(
                stepOrder=step_order,
                stepType=step_type,
                endCondition=END_TIME,
                endConditionValue=duration_secs,
                targetType=TARGET_CADENCE,
                targetValueOne=cadence_min,
                targetValueTwo=cadence_max
            )
        else:
            step = ExecutableStep(
                stepOrder=step_order,
                stepType=step_type,
                endCondition=END_TIME,
                endConditionValue=duration_secs,
                targetType=TARGET_NONE
            )

        # Add description/notes for the phase
        if notes:
            step.description = _smart_truncate(notes, STEP_NOTE_MAX_CHARS)

        steps.append(step)
        step_order += 1

    return CyclingWorkout(
        workoutName=workout_name,
        description="FTP Test: Warmup > Blowout > Recovery > 20min Test > Cooldown",
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


# ---------------------------------------------------------------------------
# Shared phase renderer (running + cycling `structure`)
# ---------------------------------------------------------------------------

class _PhaseRenderer:
    """Turns validated Phase / RepeatBlock items into Garmin steps.

    sport: 'running' | 'cycling'; indoor decides how an intensity word
    resolves on a ride (power zones indoors, HR outdoors).
    """

    def __init__(self, sport: str, indoor: bool, session_intensity: str | None,
                 session: dict, report: AuthoringReport):
        self.sport = sport
        self.indoor = indoor
        self.session_intensity = session_intensity
        self.session = session
        self.report = report
        self._order = 0
        self._ftp = None
        self._ftp_loaded = False

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def _ftp_value(self) -> float | None:
        if not self._ftp_loaded:
            self._ftp_loaded = True
            raw = self.session.get("ftp") or (self.session.get("power_targets") or {}).get("ftp")
            self._ftp = float(raw) if raw else get_athlete_ftp()
        return self._ftp

    def _intensity_target(self, word: str, path: str):
        if self.sport == "running":
            pace = get_pace_target_for_intensity(word)
            if pace:
                return TARGET_PACE, float(pace[0]), float(pace[1])
            hr = get_hr_target_for_intensity(word)
            if hr:
                return TARGET_HR, float(hr[0]), float(hr[1])
        else:
            if self.indoor:
                power = get_power_target_for_intensity(word)
                if power:
                    return TARGET_POWER, float(power[0]), float(power[1])
            hr = get_hr_target_for_intensity(word)
            if hr:
                return TARGET_HR, float(hr[0]), float(hr[1])
        self.report.warn(path, f"intensity '{word}' has no configured zone on this profile; the step is untargeted")
        return TARGET_NONE, None, None

    def resolve_target(self, phase: Phase, path: str):
        key = phase.target_key()
        if key is None:
            if self.session_intensity:
                return self._intensity_target(self.session_intensity, path)
            return TARGET_NONE, None, None
        if key == "intensity":
            if phase.intensity == "none":
                return TARGET_NONE, None, None  # explicitly untargeted
            return self._intensity_target(phase.intensity, path)
        if key == "hr_target":
            lo, hi = sorted(phase.hr_target)
            return TARGET_HR, float(lo), float(hi)
        if key == "cadence":
            lo, hi = sorted(phase.cadence)
            return TARGET_CADENCE, float(lo), float(hi)
        if key == "power_watts":
            lo, hi = sorted(phase.power_watts)
            return TARGET_POWER, float(lo), float(hi)
        if key == "power_pct":
            ftp = self._ftp_value()
            if not ftp:
                self.report.error(path, "power_pct needs the athlete's FTP (set_ftp) or the session's 'ftp'; "
                                        "give power_watts instead")
                return TARGET_NONE, None, None
            pct = phase.power_pct
            if isinstance(pct, list):
                lo_pct, hi_pct = sorted(pct)
            else:
                lo_pct, hi_pct = float(pct) - 5, float(pct) + 5
            return TARGET_POWER, float(int(ftp * lo_pct / 100)), float(int(ftp * hi_pct / 100))
        if key == "pace_sec_per_km":
            slow, fast = _pace_pair_to_mps(float(phase.pace_sec_per_km[0]), float(phase.pace_sec_per_km[1]))
            return TARGET_PACE, slow, fast
        if key == "pace":
            lo, hi = sorted(float(v) for v in phase.pace)
            return TARGET_PACE, lo, hi
        return TARGET_NONE, None, None

    def build_step(self, phase: Phase, path: str, inside_repeat: bool) -> tuple[ExecutableStep, int]:
        kind, value = phase.end_condition()
        target_type, lo, hi = self.resolve_target(phase, path)
        step = ExecutableStep(
            stepOrder=self._next_order(),
            stepType=PHASE_STEP_TYPES[phase.phase],
            endCondition={"open": END_LAP_BUTTON, "time": END_TIME, "distance": END_DISTANCE}[kind],
            targetType=target_type,
        )
        est_secs = 0
        if kind == "time":
            step.endConditionValue = float(value)
            est_secs = int(round(value))
        elif kind == "distance":
            step.endConditionValue = float(value)
        if lo is not None and hi is not None:
            step.targetValueOne = lo
            step.targetValueTwo = hi
        if inside_repeat:
            step.childStepId = 1
        if phase.notes:
            step.description = _smart_truncate(str(phase.notes), STEP_NOTE_MAX_CHARS)
        return step, est_secs

    def build_repeat(self, block: RepeatBlock, path: str, inside_repeat: bool) -> tuple[RepeatGroup, int]:
        group_order = self._next_order()
        children, per_iter_secs = [], 0
        for i, sub in enumerate(block.steps):
            sub_path = f"{path}.steps[{i}]"
            if isinstance(sub, RepeatBlock):
                child, secs = self.build_repeat(sub, sub_path, inside_repeat=True)
            else:
                child, secs = self.build_step(sub, sub_path, inside_repeat=True)
            children.append(child)
            per_iter_secs += secs
        group = RepeatGroup(
            stepOrder=group_order,
            stepType=STEP_REPEAT,
            numberOfIterations=block.iterations,
            workoutSteps=children,
            endCondition=dict(END_ITERATIONS),
            endConditionValue=float(block.iterations),
        )
        if inside_repeat:
            group.childStepId = 1
        return group, block.iterations * per_iter_secs

    def render(self, items: list) -> tuple[list, int]:
        steps, total = [], 0
        for i, item in enumerate(items):
            path = f"structure[{i}]"
            if isinstance(item, RepeatBlock):
                step, secs = self.build_repeat(item, path, inside_repeat=False)
            else:
                step, secs = self.build_step(item, path, inside_repeat=False)
            steps.append(step)
            total += secs
        return steps, total


def _ensure_report(session: dict, report: AuthoringReport | None) -> AuthoringReport:
    if report is None:
        report = validate_session(session, load_exercise_db(), load_strength_baseline())
    if report.errors:
        raise WorkoutAuthoringError(report.errors, report.warnings)
    return report


def _structure_items(session: dict, report: AuthoringReport, structure=None) -> list:
    """Validated structure items: from the report when it saw them, else parsed now."""
    if report.structure is not None:
        return report.structure
    items, problems = validate_structure(structure if structure is not None else session.get("structure", []))
    if problems:
        raise WorkoutAuthoringError(problems, report.warnings)
    return items


# ---------------------------------------------------------------------------
# Cycling
# ---------------------------------------------------------------------------

def build_cycling_workout(session: dict, date: str, report: AuthoringReport | None = None) -> CyclingWorkout:
    """
    Build a cycling workout from a plan session.

    Target selection:
    - Indoor cycling (Wattbike/trainer): POWER targets (HR when no power zones)
    - Outdoor cycling: HR targets

    A session with `structure` renders exactly those phases (repeats,
    seconds, fractional minutes, lap-button steps). Without structure: a
    plain outdoor ride is ONE block for the whole duration; an indoor ride
    gets a warm-up (10 % up to 10 min), the main block and a cool-down
    (5 % up to 5 min).
    """
    duration_mins = session.get("duration_mins") or 0
    description = session.get("name") or session.get("description", "Cycling workout")
    intensity = session.get("intensity", "")
    discretion = _is_discretion(session)
    # No usable intensity word (none / missing / unknown): every step is
    # untargeted — the warm-up and cool-down do not get a recovery target the
    # session never asked for.
    untargeted = discretion or normalise_intensity(intensity) is None

    # Calculate total duration
    total_secs = duration_mins * 60

    # Determine if indoor (power) or outdoor (HR)
    indoor = is_indoor_cycling(session)
    logger.info("Building cycling workout: type=%s, indoor=%s, target=%s",
                session.get("type"), indoor, "POWER" if indoor else "HR")

    # Create workout name with indoor/outdoor tag for easy planning
    # Skip if description already has the tag
    if "(Indoor)" in description or "(Outdoor)" in description:
        workout_name = description[:40]
    else:
        tag = "(Indoor)" if indoor else "(Outdoor)"
        workout_name = f"{description[:32]} {tag}"

    # Check if this is a structured workout (has phases defined)
    structure = session.get("structure", [])

    # If structured session (indoor or outdoor), build from structure
    if structure:
        return build_structured_cycling_workout(session, workout_name, total_secs, structure, indoor, report)

    # Determine if we should skip warmup (simple outdoor rides without structure)
    skip_warmup = is_simple_outdoor_ride(session)

    # Get targets based on indoor/outdoor. A discretion day states no
    # intensity: the main block is untargeted and carries the constraints,
    # and the warm-up/cool-down are untargeted too (the plan granted choice).
    if untargeted:
        main_target = warmup_target = None
        target_type = warmup_target_type = TARGET_NONE
    elif indoor:
        # Indoor: use power zones
        main_target = get_power_target_for_intensity(intensity)
        warmup_target = get_power_target_for_intensity("recovery")
        target_type = TARGET_POWER if main_target else TARGET_NONE
        warmup_target_type = TARGET_POWER if warmup_target else TARGET_NONE
    else:
        # Outdoor: use HR zones
        main_target = get_hr_target_for_intensity(intensity)
        warmup_target = get_hr_target_for_intensity("recovery")
        target_type = TARGET_HR if main_target else TARGET_NONE
        warmup_target_type = TARGET_HR if warmup_target else TARGET_NONE

    steps = []
    step_order = 1

    if skip_warmup and not (session.get("warmup_mins") or session.get("cooldown_mins")):
        # Plain outdoor ride: single main interval, no warmup/cooldown
        main_secs = total_secs

        main_step = ExecutableStep(
            stepOrder=step_order,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=target_type
        )
        if main_target:
            main_step.targetValueOne = main_target[0]
            main_step.targetValueTwo = main_target[1]
            if not indoor:
                main_step.description = f"HR {main_target[0]}-{main_target[1]} bpm"
        if discretion:
            main_step.description = _discretion_note(session)

        steps = [main_step]
    else:
        # Regular ride: warmup + main + cooldown (the plan's warmup_mins /
        # cooldown_mins when stated; else 10 % up to 10 min / 5 % up to 5 min)
        warmup_secs, main_secs, cooldown_secs = _plain_split(
            session, total_secs, _split_default(total_secs, *PLAIN_SPLIT['indoor_cycling']['warmup']),
            _split_default(total_secs, *PLAIN_SPLIT['indoor_cycling']['cooldown']))

        # Build warmup step
        warmup_step = ExecutableStep(
            stepOrder=step_order,
            stepType=STEP_WARMUP,
            endCondition=END_TIME,
            endConditionValue=warmup_secs,
            targetType=warmup_target_type
        )
        if warmup_target:
            warmup_step.targetValueOne = warmup_target[0]
            warmup_step.targetValueTwo = warmup_target[1]
        steps.append(warmup_step)
        step_order += 1

        # Build main interval step
        main_step = ExecutableStep(
            stepOrder=step_order,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=target_type
        )
        if main_target:
            main_step.targetValueOne = main_target[0]
            main_step.targetValueTwo = main_target[1]
            if not indoor:
                main_step.description = f"HR {main_target[0]}-{main_target[1]} bpm"
        if discretion:
            main_step.description = _discretion_note(session)
        steps.append(main_step)
        step_order += 1

        # Build cooldown step
        cooldown_step = ExecutableStep(
            stepOrder=step_order,
            stepType=STEP_COOLDOWN,
            endCondition=END_TIME,
            endConditionValue=cooldown_secs,
            targetType=warmup_target_type
        )
        if warmup_target:
            cooldown_step.targetValueOne = warmup_target[0]
            cooldown_step.targetValueTwo = warmup_target[1]
        steps.append(cooldown_step)

    return CyclingWorkout(
        workoutName=workout_name,
        description=str(session.get("notes") or session.get("purpose") or "")[:255],
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


def build_structured_cycling_workout(session: dict, workout_name: str, total_secs: int, structure: list,
                                     indoor: bool = True, report: AuthoringReport | None = None) -> CyclingWorkout:
    """
    Build a structured cycling workout from `structure` phases.

    Shares the phase contract with running (coach/authoring.py): phases with
    one end condition (duration_secs / duration_mins / distance_m, or
    "open") and at most one target (power_watts, power_pct, hr_target,
    cadence, intensity), plus repeat blocks that nest. A phase with no
    target inherits the session intensity. Indoor rides resolve an
    intensity word to the power zone, outdoor rides to the HR zone.
    """
    report = _ensure_report(session, report)
    items = _structure_items(session, report, structure)
    renderer = _PhaseRenderer("cycling", indoor, report.session_intensity, session, report)
    steps, calculated_total_secs = renderer.render(items)
    if report.errors:
        raise WorkoutAuthoringError(report.errors, report.warnings)

    # Use calculated total if structure defines it, otherwise use passed total
    final_total_secs = calculated_total_secs if calculated_total_secs > 0 else total_secs

    return CyclingWorkout(
        workoutName=workout_name,
        description=str(session.get("notes") or session.get("purpose") or "")[:255],
        estimatedDurationInSecs=int(final_total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def build_running_workout(session: dict, date: str, report: AuthoringReport | None = None) -> RunningWorkout:
    """
    Build a running workout from a plan session.

    Uses pace targets if threshold_pace is set in athlete profile,
    otherwise falls back to HR zone targets.

    If session contains a 'structure' field, delegates to the structured
    builder for full interval/repeat support. Otherwise produces the
    simple 3-step (warmup, main, cooldown) shape from duration_mins and
    intensity: warm-up and cool-down 10 % each (up to 5 min).
    """
    if session.get("structure"):
        return build_structured_running_workout(session, date, report)

    duration_mins = session.get("duration_mins") or 0
    intensity = session.get("intensity", "")
    discretion = _is_discretion(session)
    untargeted = discretion or normalise_intensity(intensity) is None  # see build_cycling_workout

    # Segment durations: the plan's warmup_mins / cooldown_mins when stated,
    # else 10 % each up to 5 min
    total_secs = duration_mins * 60
    warmup_secs, main_secs, cooldown_secs = _plain_split(
        session, total_secs, _split_default(total_secs, *PLAIN_SPLIT['running']['warmup']),
        _split_default(total_secs, *PLAIN_SPLIT['running']['cooldown']))

    workout_name = _workout_name(session, "Running workout")

    # Try pace targets first (preferred for running), fall back to HR. A
    # discretion day is untargeted throughout and carries its constraints.
    if untargeted:
        main_pace = warmup_pace = hr_target = warmup_hr = None
    else:
        main_pace = get_pace_target_for_intensity(intensity)
        warmup_pace = get_pace_target_for_intensity("recovery")
        hr_target = get_hr_target_for_intensity(intensity)
        warmup_hr = get_hr_target_for_intensity("recovery")

    # Determine which target type to use
    use_pace = main_pace is not None

    def _step(order: int, step_type: dict, secs: float, pace, hr) -> ExecutableStep:
        if use_pace and pace:
            step = ExecutableStep(stepOrder=order, stepType=step_type, endCondition=END_TIME,
                                  endConditionValue=secs, targetType=TARGET_PACE)
            step.targetValueOne = pace[0]  # slow speed m/s
            step.targetValueTwo = pace[1]  # fast speed m/s
            return step
        step = ExecutableStep(stepOrder=order, stepType=step_type, endCondition=END_TIME,
                              endConditionValue=secs, targetType=TARGET_HR if hr else TARGET_NONE)
        if hr:
            step.targetValueOne = hr[0]
            step.targetValueTwo = hr[1]
        return step

    steps = [
        _step(1, STEP_WARMUP, warmup_secs, warmup_pace, warmup_hr),
        _step(2, STEP_INTERVAL, main_secs, main_pace, hr_target),
        _step(3, STEP_COOLDOWN, cooldown_secs, warmup_pace, warmup_hr),
    ]
    if discretion:
        steps[1].description = _discretion_note(session)

    return RunningWorkout(
        workoutName=workout_name,
        description=str(session.get("notes") or session.get("purpose") or "")[:255],
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=RUNNING_SPORT,
                workoutSteps=steps
            )
        ]
    )


def build_structured_running_workout(session: dict, date: str, report: AuthoringReport | None = None) -> RunningWorkout:
    """Build a structured running workout from session['structure'].

    Phase contract (coach/authoring.py, shared with cycling):
      phase: 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest', or a
        repeat block {'phase': 'repeat', 'iterations': N, 'steps': [...]}
      End condition (exactly one): distance_m, duration_secs, duration_mins;
        "open" => lap-button
      Target (at most one): pace_sec_per_km [slow, fast], pace [slow_mps,
        fast_mps], hr_target [low, high], cadence [low, high], intensity
        (pace zone when threshold pace is set, else HR zone). A phase
        without a target inherits the session intensity.
      notes: the cue on the step (about 50 characters shown)
    """
    report = _ensure_report(session, report)
    workout_name = _workout_name(session, "Running workout")
    items = _structure_items(session, report)
    renderer = _PhaseRenderer("running", False, report.session_intensity, session, report)
    workout_steps, total_secs = renderer.render(items)
    if report.errors:
        raise WorkoutAuthoringError(report.errors, report.warnings)

    if total_secs <= 0:
        total_secs = int(session.get("duration_mins") or 30) * 60

    return RunningWorkout(
        workoutName=workout_name,
        description=str(session.get("notes") or session.get("purpose") or "")[:255],
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=RUNNING_SPORT,
                workoutSteps=workout_steps,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Swimming, yoga, pilates, padel (dict payloads)
# ---------------------------------------------------------------------------

def build_swimming_workout(session: dict, date: str) -> dict:
    """
    Build a swimming workout from a plan session.

    Supports two modes:
    1. Structured: If session has 'structure' field, creates multi-step workout
       structure: [{phase, distance_m, stroke, pace, notes}, ...]
       phases: warmup, drills, main, cooldown
    2. Simple: Falls back to timed workout if no structure provided

    Pool length defaults to 25m but can be overridden via pool_length_m.
    """
    duration_mins = session.get("duration_mins", 30)
    structure = session.get("structure", [])
    pool_length = session.get("pool_length_m", 25.0)

    workout_name = _workout_name(session, "Swimming session")
    steps = []

    if structure:
        # Structured workout with multiple phases
        step_order = 1
        total_distance_m = 0

        for phase in structure:
            phase_type = phase.get("phase", "main").lower()
            distance_m = phase.get("distance_m", 100)
            total_distance_m += distance_m

            # Map phase to step type
            if phase_type == "warmup":
                step_type = STEP_WARMUP
            elif phase_type == "cooldown":
                step_type = STEP_COOLDOWN
            elif phase_type == "recovery":
                step_type = STEP_RECOVERY
            else:
                step_type = STEP_INTERVAL

            step = {
                "type": "ExecutableStepDTO",
                "stepOrder": step_order,
                "stepType": step_type,
                "endCondition": END_DISTANCE,
                "endConditionValue": float(distance_m),
                "targetType": TARGET_NONE,
            }
            if phase.get("notes"):
                step["description"] = _smart_truncate(str(phase["notes"]), STEP_NOTE_MAX_CHARS)
            steps.append(step)
            step_order += 1

        # Estimate duration from distance (assume ~2:00/100m for beginner)
        estimated_secs = int(total_distance_m * 1.2)  # 120 secs per 100m
    else:
        # Simple timed workout fallback
        total_secs = duration_mins * 60
        estimated_secs = int(total_secs)
        steps = [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": STEP_INTERVAL,
                "endCondition": END_TIME,
                "endConditionValue": float(total_secs),
                "targetType": TARGET_NONE,
            }
        ]

    return {
        "workoutName": workout_name,
        "sportType": SWIMMING_SPORT,
        "estimatedDurationInSecs": estimated_secs,
        "poolLength": pool_length,
        "poolLengthUnit": {"unitKey": "meter"},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": SWIMMING_SPORT,
                "workoutSteps": steps
            }
        ]
    }


def build_yoga_workout(session: dict, date: str) -> dict:
    """
    Build a yoga/mobility workout from a plan session.

    Yoga workouts use a simple timed structure since Garmin doesn't
    support detailed yoga pose sequences via API.
    """
    duration_mins = session.get("duration_mins", 45)
    session_type = session.get("type", "yoga").lower()

    # Calculate segment durations (in seconds)
    total_secs = duration_mins * 60

    workout_name = _workout_name(session, "Yoga/Mobility session")

    # Determine sport type (yoga or pilates)
    sport_type = PILATES_SPORT if session_type == "pilates" else YOGA_SPORT

    # Build simple timed steps for yoga - must match Garmin's expected format
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": 1,
        "stepType": STEP_INTERVAL,
        "endCondition": END_TIME,
        "endConditionValue": float(total_secs),
        "targetType": TARGET_NONE,
    }
    notes = session.get("notes")
    if notes:
        step["description"] = _smart_truncate(str(notes), STEP_NOTE_MAX_CHARS)

    return {
        "workoutName": workout_name,
        "sportType": sport_type,
        "estimatedDurationInSecs": int(total_secs),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": [step]
            }
        ]
    }


def build_pilates_workout(session: dict, date: str) -> dict:
    """
    Build a pilates workout from a plan session.

    Used for rehab/mobility work that the user tracks as Pilates on Garmin.

    If session contains an 'exercises' list, builds structured workout with
    RepeatGroupDTO for each exercise (sets x reps/time + rest). Exercises
    can use reps (END_REPS), duration_secs (END_TIME), or default to
    END_LAP_BUTTON.

    Sessions without exercises get a simple timed fallback.
    Notes are included as the workout description so rehab routines show
    on the watch.
    """
    duration_mins = session.get("duration_mins", 15)
    description = session.get("name") or session.get("description", "")
    session_type = session.get("type", "").lower()
    notes = session.get("notes", "")
    exercises = session.get("exercises", [])

    # Generate proper name if not provided or if it's just notes
    if not description or description in ["Rehab/Mobility", "Do at pool or after", "3rd rehab session of week"]:
        if session_type == "rehab":
            description = str(session.get("purpose") or "Rehab")
        else:
            description = "Mobility Session"

    # Calculate segment durations (in seconds)
    total_secs = duration_mins * 60

    # Create workout name from description
    workout_name = description[:40]

    # Simple timed fallback when no exercises provided
    if not exercises:
        step = {
            "type": "ExecutableStepDTO",
            "stepOrder": 1,
            "stepType": STEP_INTERVAL,
            "endCondition": END_TIME,
            "endConditionValue": float(total_secs),
            "targetType": TARGET_NONE,
        }
        if notes:
            step["description"] = notes[:50]

        return {
            "workoutName": workout_name,
            "description": notes[:255] if notes else "",
            "sportType": PILATES_SPORT,
            "estimatedDurationInSecs": int(total_secs),
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "sportType": PILATES_SPORT,
                    "workoutSteps": [step]
                }
            ]
        }

    # Structured rehab workout with RepeatGroupDTOs
    steps = []
    step_order = 1
    child_step_order = 1
    total_time = 0

    for exercise in exercises:
        ex_name = exercise.get("name", "Exercise")
        sets = exercise.get("sets", 1)
        reps = exercise.get("reps")
        duration_secs = exercise.get("duration_secs")
        ex_notes = exercise.get("notes", "")

        # Determine end condition: reps > duration_secs > lap button
        if reps:
            end_condition = END_REPS
            end_value = float(reps)
            est_secs_per_set = 45
        elif duration_secs:
            end_condition = END_TIME
            end_value = float(duration_secs)
            est_secs_per_set = duration_secs
        else:
            end_condition = END_LAP_BUTTON
            end_value = None
            est_secs_per_set = 30

        # Exercise step
        exercise_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": child_step_order,
            "stepType": STEP_INTERVAL,
            "childStepId": 1,
            "endCondition": end_condition,
            "targetType": TARGET_NONE,
        }
        if end_value is not None:
            exercise_step["endConditionValue"] = end_value
        # Build step description: exercise name + notes
        readable_name = ex_name.replace("_", " ").title()
        if ex_notes:
            description = f"{readable_name}: {ex_notes}"
        else:
            description = readable_name
        exercise_step["description"] = description[:50]
        child_step_order += 1

        # Rest step (lap button between sets)
        rest_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": child_step_order,
            "stepType": STEP_REST,
            "childStepId": 1,
            "endCondition": END_LAP_BUTTON,
            "targetType": TARGET_NONE,
        }
        child_step_order += 1

        # RepeatGroupDTO
        repeat_group = {
            "type": "RepeatGroupDTO",
            "stepOrder": step_order,
            "stepType": STEP_REPEAT,
            "childStepId": 1,
            "numberOfIterations": sets,
            "workoutSteps": [exercise_step, rest_step],
            "endCondition": dict(END_ITERATIONS),
            "endConditionValue": float(sets),
        }

        steps.append(repeat_group)
        step_order += 1
        total_time += sets * (est_secs_per_set + 15)  # 15s rest estimate

    return {
        "workoutName": workout_name,
        "description": notes[:255] if notes else "",
        "sportType": PILATES_SPORT,
        "estimatedDurationInSecs": int(total_time) if total_time > 0 else int(total_secs),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": PILATES_SPORT,
                "workoutSteps": steps,
            }
        ],
        "exercise_count": len(exercises),
    }


def build_padel_workout(session: dict, date: str) -> dict:
    """
    Build a padel workout from a plan session.

    Padel workouts use a simple timed structure since the activity
    duration is variable based on game play.
    """
    duration_mins = session.get("duration_mins", 90)
    description = session.get("name") or session.get("description", "")

    # Generate workout name
    if description and description.lower() != "padel":
        workout_name = description[:40]
    else:
        workout_name = "Padel Session"

    # Calculate segment duration (in seconds)
    total_secs = duration_mins * 60

    # Build simple timed step
    steps = [
        {
            "type": "ExecutableStepDTO",
            "stepOrder": 1,
            "stepType": STEP_INTERVAL,
            "endCondition": END_TIME,
            "endConditionValue": float(total_secs),
            "targetType": TARGET_NONE,
        }
    ]

    return {
        "workoutName": workout_name,
        "sportType": PADEL_SPORT,
        "estimatedDurationInSecs": int(total_secs),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": PADEL_SPORT,
                "workoutSteps": steps
            }
        ]
    }


# ---------------------------------------------------------------------------
# Strength
# ---------------------------------------------------------------------------

def load_exercise_db() -> dict:
    """Load Garmin exercise database for (category, exerciseName) validation."""
    return _read_data_json("exercises.json").get("exercises", {})


def load_exercise_library() -> dict:
    """Load the exercise form cues library."""
    return _read_data_json(EXERCISE_LIBRARY_FILE)


def load_strength_baseline() -> dict:
    """The athlete's strength_baseline.exercises mapping (athlete.json) — read
    only to WARN when a loaded movement states no load; never to pick one."""
    try:
        return (_read_data_json(ATHLETE_FILE).get("strength_baseline") or {}).get("exercises") or {}
    except Exception:
        logger.warning("Failed to load the strength baseline", exc_info=True)
        return {}


# Exercise notes come from researched web pages; Garmin shows ~250 chars.
GARMIN_NOTE_MAX_CHARS = 250


def get_exercise_note(exercise_name: str, library: dict) -> str:
    """Get form cues note for an exercise from the library."""
    # Try exact match first
    normalized = exercise_name.strip().lower()
    if normalized in library:
        return sanitize_text(library[normalized].get("garmin_note", ""), GARMIN_NOTE_MAX_CHARS)

    # Try partial match (e.g., "ROMANIAN_DEADLIFT" -> "romanian deadlift")
    garmin_name = exercise_name.replace("_", " ").lower()
    if garmin_name in library:
        return sanitize_text(library[garmin_name].get("garmin_note", ""), GARMIN_NOTE_MAX_CHARS)

    return ""


def _timed_step(order: int, step_type: dict, secs: float, *, category: str | None = None,
                child: bool = False, description: str | None = None) -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": step_type,
        "endCondition": END_TIME,
        "endConditionValue": float(secs),
        "targetType": TARGET_NONE,
    }
    if category is not None:
        step["category"] = category
        step["exerciseName"] = ""
    if child:
        step["childStepId"] = 1
    if description:
        step["description"] = description
    return step


def _lap_rest_step(order: int, child: bool = False) -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": STEP_REST,
        "endCondition": END_LAP_BUTTON,
        "targetType": TARGET_NONE,
    }
    if child:
        step["childStepId"] = 1
    return step


def _exercise_step(spec: CircuitExercise, order: int, exercise_db: dict, library: dict,
                   path: str = "", report: AuthoringReport | None = None) -> tuple[dict, int]:
    """One exercise step inside a repeat group, plus its estimated seconds."""
    category, garmin_exercise_name, _warning = resolve_exercise_category(spec, exercise_db)
    if not category:  # validate_session has already reported this; defensive
        raise WorkoutAuthoringError([{'field': spec.name, 'message': "category is required"}])

    # The coach's cue wins. The researched exercise-library cue fills in when
    # the plan is silent — web-derived text, so it is flagged, never silent.
    cue = str(spec.notes or "").strip()
    if not cue:
        cue = get_exercise_note(spec.name, library)
        if cue and report is not None:
            report.warn(path or spec.name, (f"{spec.name}: no notes in the plan; the researched "
                                            f"exercise-library cue goes on the watch: \"{cue}\""))
    readable_name = spec.name.replace("_", " ").title()
    side = "each side" if spec.each_side else ""
    if garmin_exercise_name:
        # Watch shows the exercise name already: description = cue (+ side)
        parts = [p for p in (side, cue) if p]
        description = ": ".join(parts) if len(parts) == 2 else "".join(parts)
    else:
        head = f"{readable_name} {side}".strip()
        description = f"{head}: {cue}" if cue else head
    description = _smart_truncate(description, STEP_NOTE_MAX_CHARS)

    if spec.duration_secs is not None:
        end_condition, end_value, est = END_TIME, float(spec.duration_secs), int(spec.duration_secs)
    elif spec.reps == "open":
        end_condition, end_value, est = END_LAP_BUTTON, None, 45
    else:
        end_condition, end_value, est = END_REPS, float(spec.reps), max(25, int(spec.reps) * 4)
    if spec.each_side:
        est *= 2

    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": STEP_INTERVAL,
        "childStepId": 1,
        "endCondition": end_condition,
        "targetType": TARGET_NONE,
        "category": category,
        "exerciseName": garmin_exercise_name,
    }
    if end_value is not None:
        step["endConditionValue"] = end_value
    if description:
        step["description"] = description
    if spec.weight_kg is not None:
        step["weightValue"] = float(spec.weight_kg)
        step["weightUnit"] = dict(WEIGHT_UNIT_KG)
    return step, est


def _rest_estimate(category: str | None) -> int:
    """Seconds assumed for a lap-button rest in the duration ESTIMATE only:
    loaded compounds rest longer than accessories. Never sent to Garmin."""
    return 120 if category in COMPOUND_CATEGORIES else DEFAULT_REST_SECS


def _repeat_group(order: int, iterations: int, children: list, notes: str | None = None) -> dict:
    group = {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": STEP_REPEAT,
        "childStepId": 1,
        "numberOfIterations": iterations,
        "workoutSteps": children,
        "endCondition": dict(END_ITERATIONS),
        "endConditionValue": float(iterations),
    }
    if notes:
        group["description"] = _smart_truncate(str(notes), STEP_NOTE_MAX_CHARS)
    return group


def build_strength_workout(session: dict, date: str, report: AuthoringReport | None = None) -> dict:
    """
    Build a strength workout from a plan session — exactly what the
    `exercises` list says (contract: coach/authoring.py).

    - `warmup_mins` (session level) renders a timed WARM_UP block followed by
      a lap-button rest; nothing is inserted otherwise.
    - Each exercise is a RepeatGroupDTO of `sets` iterations holding the
      exercise step (reps -> END_REPS, duration_secs -> END_TIME, "open"
      reps -> lap button) and a rest step (timed when rest_secs is given,
      lap button otherwise; none when rest_secs is 0).
    - A circuit is one RepeatGroupDTO of `rounds` iterations holding every
      exercise step back to back, then the rest step.
    - The exercise `notes` cue is the step description; the researched
      exercise-library cue fills in only when the plan gives no notes.
    - Weight is sent only when `weight_kg` is stated. Garmin keeps the
      exercise name only for catalogue entries whose category matches; the
      description carries the readable name otherwise.
    - No exercises: ONE timed block for duration_mins (the validator warns
      when the description reads like sets and reps).
    """
    report = _ensure_report(session, report)
    duration_mins = session.get("duration_mins") or 0
    workout_name = _workout_name(session, "Strength training")

    exercise_library = load_exercise_library()
    exercise_db = load_exercise_db()

    items = report.exercises
    if items is None and session.get("exercises"):
        items, problems = validate_exercises(session.get("exercises"))
        if problems:
            raise WorkoutAuthoringError(problems, report.warnings)

    # If no exercises provided, create simple timed workout
    if not items:
        total_secs = duration_mins * 60
        step = _timed_step(1, STEP_INTERVAL, total_secs, category="CARDIO")
        notes = session.get("notes")
        if notes:
            step["description"] = _smart_truncate(str(notes), STEP_NOTE_MAX_CHARS)
        return {
            "workoutName": workout_name,
            "description": str(session.get("notes") or session.get("purpose") or "")[:255],
            "sportType": STRENGTH_SPORT,
            "estimatedDurationInSecs": int(total_secs),
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "sportType": STRENGTH_SPORT,
                    "workoutSteps": [step]
                }
            ]
        }

    steps: list[dict] = []
    step_order = 1
    child_order = 1
    total_time = 0
    exercise_count = 0

    warmup_mins = session.get("warmup_mins")
    if warmup_mins:
        warmup_secs = float(warmup_mins) * 60
        steps.append(_timed_step(step_order, STEP_WARMUP, warmup_secs, category="WARM_UP"))
        step_order += 1
        steps.append(_lap_rest_step(step_order))  # press lap when ready for the first exercise
        step_order += 1
        total_time += int(warmup_secs)

    for index, item in enumerate(items):
        if isinstance(item, Circuit):
            children, round_secs = [], 0
            for j, spec in enumerate(item.exercises):
                step, est = _exercise_step(spec, child_order, exercise_db, library=exercise_library,
                                           path=f"exercises[{index}].exercises[{j}]", report=report)
                children.append(step)
                child_order += 1
                round_secs += est
                exercise_count += 1
            if item.rest_secs is None:
                children.append(_lap_rest_step(child_order, child=True))
                child_order += 1
                round_secs += DEFAULT_REST_SECS
            elif item.rest_secs > 0:
                children.append(_timed_step(child_order, STEP_REST, item.rest_secs, child=True))
                child_order += 1
                round_secs += item.rest_secs
            steps.append(_repeat_group(step_order, item.rounds, children, item.notes or item.name))
            step_order += 1
            total_time += item.rounds * round_secs
            continue

        spec: Exercise = item
        step, est = _exercise_step(spec, child_order, exercise_db, library=exercise_library,
                                   path=f"exercises[{index}]", report=report)
        children = [step]
        child_order += 1
        set_secs = est
        if spec.rest_secs is None:
            children.append(_lap_rest_step(child_order, child=True))
            child_order += 1
            set_secs += _rest_estimate(step.get("category"))
        elif spec.rest_secs > 0:
            children.append(_timed_step(child_order, STEP_REST, spec.rest_secs, child=True))
            child_order += 1
            set_secs += spec.rest_secs
        steps.append(_repeat_group(step_order, spec.sets, children))
        step_order += 1
        total_time += spec.sets * set_secs
        exercise_count += 1

    planned_secs = float(duration_mins) * 60 if duration_mins else 0
    if planned_secs and total_time and abs(total_time - planned_secs) > 0.4 * planned_secs:
        report.warn('duration_mins', (f"planned {duration_mins:g} min but the exercises estimate to "
                                      f"about {int(round(total_time / 60))} min (lap-button rests "
                                      f"assumed at {DEFAULT_REST_SECS} s, {_rest_estimate('SQUAT')} s "
                                      "after loaded compounds); check volume, rests or the duration"))

    return {
        "workoutName": workout_name,
        "description": str(session.get("notes") or session.get("purpose") or "")[:255],
        "sportType": STRENGTH_SPORT,
        "estimatedDurationInSecs": int(total_time),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": STRENGTH_SPORT,
                "workoutSteps": steps
            }
        ],
        "exercise_count": exercise_count
    }


# ---------------------------------------------------------------------------
# Preview: describe a rendered workout the way the watch will show it
# ---------------------------------------------------------------------------

def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fmt_secs(secs: float) -> str:
    secs = int(round(secs))
    if secs >= 3600:
        return f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    return f"{secs // 60}:{secs % 60:02d}"


def _fmt_pace(mps: float) -> str:
    if not mps:
        return "?"
    sec_per_km = 1000.0 / mps
    return f"{int(sec_per_km // 60)}:{int(round(sec_per_km % 60)):02d}/km"


def _describe_target(step) -> str:
    target = _get(step, "targetType") or {}
    key = _get(target, "workoutTargetTypeKey", "no.target")
    lo, hi = _get(step, "targetValueOne"), _get(step, "targetValueTwo")
    if key == "no.target" or lo is None or hi is None:
        return ""
    if key == "heart.rate.zone":
        return f"HR {int(lo)}-{int(hi)} bpm"
    if key == "power.zone":
        return f"{int(lo)}-{int(hi)} W"
    if key == "cadence.zone":
        return f"cadence {int(lo)}-{int(hi)}"
    if key == "pace.zone":
        return f"pace {_fmt_pace(lo)} to {_fmt_pace(hi)}"
    return f"{key} {lo}-{hi}"


def _describe_end(step) -> str:
    end = _get(step, "endCondition") or {}
    key = _get(end, "conditionTypeKey", "lap.button")
    value = _get(step, "endConditionValue")
    if key == "time" and value is not None:
        return _fmt_secs(value)
    if key == "distance" and value is not None:
        return f"{int(value)} m" if value < 1000 else f"{value / 1000:g} km"
    if key == "reps" and value is not None:
        return f"{int(value)} reps"
    return "lap button"


def _describe_step(step, index: str, lines: list[str], indent: str) -> None:
    kind = _get(step, "type") or ("RepeatGroupDTO" if _get(step, "numberOfIterations") is not None else "ExecutableStepDTO")
    if kind == "RepeatGroupDTO":
        n = _get(step, "numberOfIterations")
        label = _get(step, "description")
        header = f"{indent}{index}. {n} x" + (f" {label}" if label else "")
        lines.append(header)
        for i, child in enumerate(_get(step, "workoutSteps") or [], start=1):
            _describe_step(child, f"{index}.{i}", lines, indent + "   ")
        return
    step_type = _get(_get(step, "stepType") or {}, "stepTypeKey", "interval")
    category = _get(step, "category")
    name = _get(step, "exerciseName")
    description = _get(step, "description")
    end = _describe_end(step)
    if category is not None:  # strength step
        weight = _get(step, "weightValue")
        if step_type == "rest":
            label = "Rest"
        elif name:
            label = name
        else:
            label = f"[{str(category).replace('_', ' ').title()}]"
        bits = [label, end]
        if weight:
            bits.append(f"@ {weight:g} kg")
        line = f"{indent}{index}. {' '.join(bits)}"
    else:
        label = {"warmup": "Warm-up", "cooldown": "Cool-down", "recovery": "Recovery",
                 "rest": "Rest", "interval": "Interval"}.get(step_type, step_type.title())
        target = _describe_target(step)
        line = f"{indent}{index}. {label} {end}" + (f" @ {target}" if target else "")
    if description:
        line += f' — "{description}"'
    lines.append(line)


def describe_workout(workout) -> dict:
    """{'name', 'sport', 'estimated_mins', 'steps': [lines]} for a built
    workout (typed garminconnect model or dict payload)."""
    sport = _get(_get(workout, "sportType") or {}, "sportTypeKey", "")
    segments = _get(workout, "workoutSegments") or []
    steps = _get(segments[0], "workoutSteps") if segments else []
    lines: list[str] = []
    for i, step in enumerate(steps or [], start=1):
        _describe_step(step, str(i), lines, "")
    est = _get(workout, "estimatedDurationInSecs") or 0
    return {
        "name": _get(workout, "workoutName"),
        "sport": sport,
        "estimated_mins": int(round(est / 60)),
        "steps": lines,
    }


def get_workout_type_name(session: dict) -> str:
    """Get human-readable workout type name."""
    session_type = session.get("type", "").lower()

    if session_type in SKIP_TYPES:
        return "skipped"

    family = workout_family_for(session_type)

    if family == "cycling" or "ride" in session_type:
        return "cycling"
    elif family == "running" or "run" in session_type:
        return "running"
    elif family in ("swimming", "yoga", "pilates", "strength", "padel"):
        return family
    return "unknown"
