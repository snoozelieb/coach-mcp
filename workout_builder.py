"""
Workout Builder - Converts plan sessions to Garmin workout format.

Uses the garminconnect.workout module to create structured workouts
that can be uploaded and scheduled to Garmin Connect.
"""
import json
from pathlib import Path
from typing import Any

from fitness import get_athlete_hr_zones
from garminconnect.workout import (
    CyclingWorkout,
    RunningWorkout,
    WorkoutSegment,
    ExecutableStep,
    RepeatGroup,
    create_warmup_step,
    create_cooldown_step,
    create_interval_step,
    create_recovery_step,
)
import logging

logger = logging.getLogger(__name__)

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

PADEL_SPORT = {
    "sportTypeId": 219,
    "sportTypeKey": "paddelball",
    "displayOrder": 219
}

# Step type definitions
STEP_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1}
STEP_COOLDOWN = {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2}
STEP_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
STEP_RECOVERY = {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}
STEP_REST = {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5}
STEP_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}

# End condition definitions
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
END_DISTANCE = {"conditionTypeId": 1, "conditionTypeKey": "distance", "displayOrder": 1, "displayable": True}
END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
END_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}

# Default rest duration for strength (seconds) - used for time estimation only
DEFAULT_REST_SECS = 45

# Target type definitions
TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
TARGET_HR = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 2}
TARGET_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence.zone", "displayOrder": 3}
TARGET_POWER = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "power.zone", "displayOrder": 4}
TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}

# Session types that map to cycling
CYCLING_TYPES = {"long_ride", "easy_ride", "cycling", "ride", "mtb", "road_ride", "ftp_test", "indoor_cycling", "wattbike", "trainer", "tempo_ride"}

# Indoor cycling types - use power targets (athlete has Wattbike indoors)
INDOOR_CYCLING_TYPES = {"indoor_cycling", "wattbike", "trainer", "ftp_test"}

# Long outdoor ride types - skip warmup section (messes with lap timing)
LONG_OUTDOOR_RIDE_TYPES = {"long_ride", "long_mtb_ride"}

# Session types that map to running
RUNNING_TYPES = {"run", "long_run", "easy_run", "running", "trail_run", "interval_run"}

# Session types that map to yoga/pilates
YOGA_TYPES = {"yoga", "mobility", "stretching"}

# Session types that map to pilates (used for rehab/mobility work)
PILATES_TYPES = {"pilates", "rehab", "rehabilitation"}

# Session types that map to strength
STRENGTH_TYPES = {"strength", "strength_training", "gym", "weights", "strength_plus_rehab"}

# Garmin workout API only accepts these categories (exercise DB has 47, workout API accepts 13).
# Exercises with non-valid categories get category remapped + exerciseName cleared.
VALID_WORKOUT_CATEGORIES = {
    "BENCH_PRESS", "ROW", "PULL_UP", "LATERAL_RAISE", "CURL",
    "TRICEPS_EXTENSION", "CORE", "DEADLIFT", "SQUAT",
    "SHOULDER_PRESS", "LUNGE", "CARDIO", "OTHER",
}

# Map non-valid categories to their closest valid workout API category.
# When a category is remapped, exerciseName must be cleared (the exercise
# doesn't exist under the new category in Garmin's DB).
GARMIN_CATEGORY_MAP = {
    # --- Generic muscle group names (LLM-written plans) ---
    "GLUTE": "SQUAT",
    "HIP": "SQUAT",
    "CALF": "SQUAT",
    "HAMSTRING": "DEADLIFT",
    "CHEST": "BENCH_PRESS",
    "BACK": "ROW",
    "BICEPS": "CURL",
    "TRICEPS": "TRICEPS_EXTENSION",
    "SHOULDERS": "SHOULDER_PRESS",
    "ABS": "CORE",
    "LEGS": "SQUAT",
    # --- Exercise DB categories not valid in workout API ---
    "SUSPENSION": "ROW",              # Face pulls, TRX rows
    "HAMSTRING_CURL": "DEADLIFT",     # Lying/seated leg curls
    "LEG_CURL": "DEADLIFT",           # Leg curl variations
    "CALF_RAISE": "SQUAT",            # Calf raises
    "HIP_RAISE": "SQUAT",             # Hip thrusts, glute bridges
    "HIP_STABILITY": "SQUAT",         # Hip stability drills
    "HIP_SWING": "DEADLIFT",          # Kettlebell swings
    "HYPEREXTENSION": "DEADLIFT",     # Back extensions
    "LEG_RAISE": "CORE",              # Hanging leg raises
    "PLANK": "CORE",                  # Plank variations
    "CRUNCH": "CORE",                 # Crunch variations
    "SIT_UP": "CORE",                 # Sit-up variations
    "CHOP": "CORE",                   # Wood chops
    "FLYE": "BENCH_PRESS",            # Chest flies
    "PUSH_UP": "BENCH_PRESS",         # Push-up variations
    "SHRUG": "ROW",                   # Trap shrugs
    "SHOULDER_STABILITY": "SHOULDER_PRESS",  # Rotator cuff work
    "OLYMPIC_LIFT": "DEADLIFT",       # Cleans, snatches
    "PLYO": "SQUAT",                  # Box jumps, plyometrics
    "BANDED_EXERCISES": "OTHER",      # Resistance band work
    "CARRY": "OTHER",                 # Farmer's walks
    "TOTAL_BODY": "OTHER",            # Full-body movements
    "BATTLE_ROPE": "CARDIO",          # Battle rope drills
    "WARM_UP": "CARDIO",              # Warmup movements
}

# Session types that map to swimming
SWIMMING_TYPES = {"swim", "swimming", "pool"}

# Session types that map to padel
PADEL_TYPES = {"padel", "paddelball", "paddle"}

# Session types to skip (not pushable to Garmin)
SKIP_TYPES = {"rest", "rest_or_easy"}

# Test session types (special handling)
TEST_TYPES = {"ftp_test", "threshold_test"}

# Intensity to HR zone mapping
INTENSITY_ZONE_MAP = {
    "easy": "z2_aerobic",
    "recovery": "z1_recovery",
    "tempo": "z3_tempo",
    "threshold": "z4_threshold",
    "hard": "z4_threshold",
    "max_effort": "z5_max",
    "intervals": "z4_threshold",
}


# Default HR zones if athlete profile not configured
DEFAULT_HR_ZONES = {
    "z1_recovery": [0, 120],
    "z2_aerobic": [120, 140],
    "z3_tempo": [140, 155],
    "z4_threshold": [155, 170],
    "z5_max": [170, 184]
}


def get_hr_target_for_intensity(intensity: str) -> tuple[int, int] | None:
    """Get HR range (low, high) for a given intensity."""
    hr_zones = get_athlete_hr_zones() or DEFAULT_HR_ZONES
    zone_key = INTENSITY_ZONE_MAP.get(intensity.lower(), "z2_aerobic")
    zone_range = hr_zones.get(zone_key)
    if zone_range and len(zone_range) == 2:
        return (zone_range[0], zone_range[1])
    return None


# Power zone mapping (intensity -> power zone key)
INTENSITY_POWER_ZONE_MAP = {
    "recovery": "z1_recovery",
    "easy": "z2_endurance",
    "tempo": "z3_tempo",
    "threshold": "z4_threshold",
    "hard": "z4_threshold",
    "sweet_spot": "z3_tempo",  # Sweet spot is upper Z3 to lower Z4
    "intervals": "z5_vo2max",
    "max_effort": "z6_anaerobic",
}


def get_athlete_power_zones() -> dict:
    """Load athlete power zones from athlete.json."""
    athlete_path = Path(__file__).parent / "data" / "athlete.json"
    try:
        with open(athlete_path) as f:
            athlete = json.load(f)
        return athlete.get("personal", {}).get("power_zones", {})
    except Exception:
        return {}


def get_power_target_for_intensity(intensity: str) -> tuple[int, int] | None:
    """
    Get power range (low, high) in watts for a given intensity.

    Returns (low_watts, high_watts) or None if power zones not configured.
    """
    power_zones = get_athlete_power_zones()
    if not power_zones:
        return None

    zone_key = INTENSITY_POWER_ZONE_MAP.get(intensity.lower(), "z2_endurance")
    zone_range = power_zones.get(zone_key)

    if zone_range and len(zone_range) == 2:
        low_watts = zone_range[0]
        high_watts = zone_range[1]
        # Handle None in z7 upper bound
        if high_watts is None:
            high_watts = low_watts + 100  # Cap at +100W above lower bound
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


# Running pace zone mapping (intensity -> pace zone key)
RUNNING_INTENSITY_PACE_MAP = {
    "recovery": "z1_recovery",
    "easy": "z2_easy",
    "tempo": "z3_tempo",
    "threshold": "z4_threshold",
    "hard": "z4_threshold",
    "intervals": "z5_interval",
    "max_effort": "z5_interval",
}


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
    athlete_path = Path(__file__).parent / "data" / "athlete.json"
    try:
        with open(athlete_path) as f:
            athlete = json.load(f)

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
    except Exception:
        return {}


def get_pace_target_for_intensity(intensity: str) -> tuple[float, float] | None:
    """
    Get pace range (slow, fast) in m/s for a given intensity.

    Garmin expects pace targets in meters per second.
    Returns (low_speed_mps, high_speed_mps) - note: low speed = slow pace, high speed = fast pace
    """
    pace_zones = get_athlete_running_zones()
    if not pace_zones:
        return None

    zone_key = RUNNING_INTENSITY_PACE_MAP.get(intensity.lower(), "z2_easy")
    zone_range = pace_zones.get(zone_key)

    if zone_range and len(zone_range) == 2:
        # Convert sec/km to m/s: 1000m / sec_per_km = m/s
        slow_pace_sec_per_km = zone_range[0]  # Higher number = slower
        fast_pace_sec_per_km = zone_range[1]  # Lower number = faster (for z5, it's reversed)

        # Handle z5 where fast is first in the array
        if slow_pace_sec_per_km < fast_pace_sec_per_km:
            slow_pace_sec_per_km, fast_pace_sec_per_km = fast_pace_sec_per_km, slow_pace_sec_per_km

        slow_speed_mps = 1000.0 / slow_pace_sec_per_km
        fast_speed_mps = 1000.0 / fast_pace_sec_per_km

        return (slow_speed_mps, fast_speed_mps)

    return None


def build_workout(session: dict, date: str) -> CyclingWorkout | RunningWorkout | dict | None:
    """
    Convert a plan session to a Garmin workout.

    Args:
        session: Plan session dict with type, duration_mins, description, etc.
        date: Date string for naming (YYYY-MM-DD)

    Returns:
        CyclingWorkout, RunningWorkout, dict (for yoga/strength), or None if not convertible
    """
    session_type = session.get("type", "").lower()
    duration_mins = session.get("duration_mins", 0)

    # Skip rest days
    if session_type in SKIP_TYPES or duration_mins == 0:
        return None

    # Determine sport and build appropriate workout
    if session_type in TEST_TYPES or "ftp_test" in session_type:
        return build_ftp_test_workout(session, date)
    elif session_type in CYCLING_TYPES or "ride" in session_type or "cycling" in session_type:
        return build_cycling_workout(session, date)
    elif session_type in RUNNING_TYPES or "run" in session_type:
        return build_running_workout(session, date)
    elif session_type in SWIMMING_TYPES:
        return build_swimming_workout(session, date)
    elif session_type in YOGA_TYPES:
        return build_yoga_workout(session, date)
    elif session_type in PILATES_TYPES:
        return build_pilates_workout(session, date)
    elif session_type in STRENGTH_TYPES:
        return build_strength_workout(session, date)
    elif session_type in PADEL_TYPES:
        return build_padel_workout(session, date)

    return None


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
    description = session.get("description", "FTP Test")
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
            step.description = notes[:50]  # Garmin has character limits

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


def build_cycling_workout(session: dict, date: str) -> CyclingWorkout:
    """
    Build a cycling workout from a plan session.

    Target selection:
    - Indoor cycling (Wattbike/trainer): Use POWER targets
    - Outdoor cycling: Use HR targets

    Warmup handling:
    - Long outdoor rides (90+ min easy): Skip warmup/cooldown (messes with laps)
    - Other rides: Include warmup and cooldown
    """
    duration_mins = session.get("duration_mins", 60)
    description = session.get("description", "Cycling workout")
    intensity = session.get("intensity", "easy")

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

    # If structured indoor session, build from structure
    if structure and indoor:
        return build_structured_indoor_workout(session, workout_name, total_secs, structure)

    # Determine if we should skip warmup (simple outdoor rides without structure)
    skip_warmup = is_simple_outdoor_ride(session)

    # Get targets based on indoor/outdoor
    if indoor:
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

    if skip_warmup:
        # Long outdoor ride: single main interval, no warmup/cooldown
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

        steps = [main_step]
    else:
        # Regular ride: warmup + main + cooldown
        warmup_secs = min(600, total_secs * 0.1)  # 10 min or 10% max
        cooldown_secs = min(300, total_secs * 0.05)  # 5 min or 5% max
        main_secs = total_secs - warmup_secs - cooldown_secs

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
        description=session.get("notes", session.get("purpose", ""))[:255],
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


def build_structured_indoor_workout(session: dict, workout_name: str, total_secs: int, structure: list) -> CyclingWorkout:
    """
    Build a structured indoor cycling workout with multiple phases.

    Supports Wattbike technique sessions and interval workouts with:
    - Power targets (watts or % of FTP)
    - Cadence targets
    - Multiple phases (warmup, intervals, recovery, cooldown)

    Structure format:
    [
        {"phase": "warmup", "duration_mins": 10, "power_watts": [93, 111], "cadence": [85, 95], "notes": "..."},
        {"phase": "interval", "duration_mins": 12, "power_pct": 90, "cadence": [85, 95], "notes": "..."},
        ...
    ]

    Target priority:
    1. power_watts - direct watt targets [low, high]
    2. power_pct - percentage of FTP (calculates watts from session/athlete FTP)
    3. cadence - cadence targets [low, high] (if no power specified)
    """
    # Get FTP from session or athlete profile
    ftp = session.get("ftp") or session.get("power_targets", {}).get("ftp")
    if not ftp:
        power_zones = get_athlete_power_zones()
        # Estimate FTP from z4 threshold zone if available
        z4 = power_zones.get("z4_threshold", [])
        if z4 and len(z4) == 2:
            ftp = z4[1]  # Upper bound of threshold is roughly FTP

    steps = []
    step_order = 1
    calculated_total_secs = 0

    for phase in structure:
        phase_name = phase.get("phase", "interval").lower()
        duration_mins = phase.get("duration_mins", 5)
        duration_secs = duration_mins * 60
        calculated_total_secs += duration_secs
        notes = phase.get("notes", "")

        # Get targets
        power_watts = phase.get("power_watts")  # [low, high] direct watts
        power_pct = phase.get("power_pct")       # percentage of FTP
        cadence = phase.get("cadence")           # [low, high] RPM

        # Calculate power target if using percentage
        if power_pct and ftp and not power_watts:
            pct = power_pct / 100.0
            # Create a +/- 5% range around the target percentage
            low_watts = int(ftp * (pct - 0.05))
            high_watts = int(ftp * (pct + 0.05))
            power_watts = [low_watts, high_watts]

        # Map phase name to step type
        if phase_name == "warmup":
            step_type = STEP_WARMUP
        elif phase_name == "cooldown":
            step_type = STEP_COOLDOWN
        elif phase_name == "recovery":
            step_type = STEP_RECOVERY
        else:
            # All other phases (interval, activation, cadence_build, single_leg, etc.)
            step_type = STEP_INTERVAL

        # Determine target type and values
        # For cadence-focused phases (single leg, cadence drills), prioritize cadence
        # For power-focused phases (sweet spot, intervals), prioritize power
        is_cadence_focused = any(x in phase_name for x in ['cadence', 'single_leg', 'spin'])

        if is_cadence_focused and cadence and len(cadence) == 2:
            # Cadence-focused phases: use cadence target
            target_type = TARGET_CADENCE
            target_low = cadence[0]
            target_high = cadence[1]
        elif power_watts and len(power_watts) == 2:
            # Power-focused phases: use power target
            target_type = TARGET_POWER
            target_low = power_watts[0]
            target_high = power_watts[1]
        elif cadence and len(cadence) == 2:
            # Fallback to cadence if no power specified
            target_type = TARGET_CADENCE
            target_low = cadence[0]
            target_high = cadence[1]
        else:
            target_type = TARGET_NONE
            target_low = None
            target_high = None

        # Create the step
        step = ExecutableStep(
            stepOrder=step_order,
            stepType=step_type,
            endCondition=END_TIME,
            endConditionValue=duration_secs,
            targetType=target_type
        )

        if target_low is not None and target_high is not None:
            step.targetValueOne = target_low
            step.targetValueTwo = target_high

        # Add notes as description
        if notes:
            step.description = notes[:50]  # Garmin character limit

        steps.append(step)
        step_order += 1

    # Use calculated total if structure defines it, otherwise use passed total
    final_total_secs = calculated_total_secs if calculated_total_secs > 0 else total_secs

    return CyclingWorkout(
        workoutName=workout_name,
        description=session.get("notes", session.get("purpose", ""))[:255],
        estimatedDurationInSecs=int(final_total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


def build_running_workout(session: dict, date: str) -> RunningWorkout:
    """
    Build a running workout from a plan session.

    Uses pace targets if threshold_pace is set in athlete profile,
    otherwise falls back to HR zone targets.

    Sports science approach:
    - Pace is more precise for running than HR (less lag, weather-independent)
    - Zones derived from threshold pace using Jack Daniels methodology
    - HR used as secondary metric when pace not available
    """
    duration_mins = session.get("duration_mins", 45)
    description = session.get("description", "Running workout")
    intensity = session.get("intensity", "easy")

    # Calculate segment durations (in seconds)
    total_secs = duration_mins * 60
    warmup_secs = min(300, total_secs * 0.1)  # 5 min or 10% max
    cooldown_secs = min(300, total_secs * 0.1)  # 5 min or 10% max
    main_secs = total_secs - warmup_secs - cooldown_secs

    # Create workout name from description
    workout_name = description[:40]

    # Try pace targets first (preferred for running), fall back to HR
    main_pace = get_pace_target_for_intensity(intensity)
    warmup_pace = get_pace_target_for_intensity("recovery")
    hr_target = get_hr_target_for_intensity(intensity)
    warmup_hr = get_hr_target_for_intensity("recovery")

    # Determine which target type to use
    use_pace = main_pace is not None

    # Build warmup step
    if use_pace and warmup_pace:
        warmup_step = ExecutableStep(
            stepOrder=1,
            stepType=STEP_WARMUP,
            endCondition=END_TIME,
            endConditionValue=warmup_secs,
            targetType=TARGET_PACE
        )
        warmup_step.targetValueOne = warmup_pace[0]  # slow speed m/s
        warmup_step.targetValueTwo = warmup_pace[1]  # fast speed m/s
    else:
        warmup_step = ExecutableStep(
            stepOrder=1,
            stepType=STEP_WARMUP,
            endCondition=END_TIME,
            endConditionValue=warmup_secs,
            targetType=TARGET_HR if warmup_hr else TARGET_NONE
        )
        if warmup_hr:
            warmup_step.targetValueOne = warmup_hr[0]
            warmup_step.targetValueTwo = warmup_hr[1]

    # Build main interval step
    if use_pace:
        main_step = ExecutableStep(
            stepOrder=2,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=TARGET_PACE
        )
        main_step.targetValueOne = main_pace[0]  # slow speed m/s
        main_step.targetValueTwo = main_pace[1]  # fast speed m/s
    else:
        main_step = ExecutableStep(
            stepOrder=2,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=TARGET_HR if hr_target else TARGET_NONE
        )
        if hr_target:
            main_step.targetValueOne = hr_target[0]
            main_step.targetValueTwo = hr_target[1]

    # Build cooldown step
    if use_pace and warmup_pace:
        cooldown_step = ExecutableStep(
            stepOrder=3,
            stepType=STEP_COOLDOWN,
            endCondition=END_TIME,
            endConditionValue=cooldown_secs,
            targetType=TARGET_PACE
        )
        cooldown_step.targetValueOne = warmup_pace[0]
        cooldown_step.targetValueTwo = warmup_pace[1]
    else:
        cooldown_step = ExecutableStep(
            stepOrder=3,
            stepType=STEP_COOLDOWN,
            endCondition=END_TIME,
            endConditionValue=cooldown_secs,
            targetType=TARGET_HR if warmup_hr else TARGET_NONE
        )
        if warmup_hr:
            cooldown_step.targetValueOne = warmup_hr[0]
            cooldown_step.targetValueTwo = warmup_hr[1]

    steps = [warmup_step, main_step, cooldown_step]

    return RunningWorkout(
        workoutName=workout_name,
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=RUNNING_SPORT,
                workoutSteps=steps
            )
        ]
    )


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
    description = session.get("description", "Swimming session")
    structure = session.get("structure", [])
    pool_length = session.get("pool_length_m", 25.0)

    workout_name = description[:40]
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

            steps.append({
                "type": "ExecutableStepDTO",
                "stepOrder": step_order,
                "stepType": step_type,
                "endCondition": END_DISTANCE,
                "endConditionValue": float(distance_m),
                "targetType": TARGET_NONE,
            })
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
    description = session.get("description", "Yoga/Mobility session")
    session_type = session.get("type", "yoga").lower()

    # Calculate segment durations (in seconds)
    total_secs = duration_mins * 60

    # Create workout name from description
    workout_name = description[:40]

    # Determine sport type (yoga or pilates)
    sport_type = PILATES_SPORT if session_type == "pilates" else YOGA_SPORT

    # Build simple timed steps for yoga - must match Garmin's expected format
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
        "sportType": sport_type,
        "estimatedDurationInSecs": int(total_secs),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": steps
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
    description = session.get("description", "")
    session_type = session.get("type", "").lower()
    notes = session.get("notes", "")
    exercises = session.get("exercises", [])

    # Generate proper name if not provided or if it's just notes
    if not description or description in ["Rehab/Mobility", "Do at pool or after", "3rd rehab session of week"]:
        if session_type == "rehab":
            description = "Ankle Rehab"
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
            "endCondition": {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
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
    description = session.get("description", "")
    notes = session.get("notes", "")

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


def load_exercise_db() -> dict:
    """Load Garmin exercise database for (category, exerciseName) validation."""
    db_path = Path(__file__).parent / "data" / "exercises.json"
    if db_path.exists():
        with open(db_path) as f:
            data = json.load(f)
        return data.get("exercises", {})
    return {}


def load_exercise_library() -> dict:
    """Load the exercise form cues library."""
    from pathlib import Path
    library_path = Path(__file__).parent / "data" / "exercise_library.json"
    if library_path.exists():
        with open(library_path) as f:
            return json.load(f)
    return {}


def get_exercise_note(exercise_name: str, library: dict) -> str:
    """Get form cues note for an exercise from the library."""
    # Try exact match first
    normalized = exercise_name.strip().lower()
    if normalized in library:
        return library[normalized].get("garmin_note", "")

    # Try partial match (e.g., "ROMANIAN_DEADLIFT" -> "romanian deadlift")
    garmin_name = exercise_name.replace("_", " ").lower()
    if garmin_name in library:
        return library[garmin_name].get("garmin_note", "")

    return ""


def load_strength_baseline() -> dict:
    """Load the strength baseline from athlete profile."""
    from pathlib import Path
    baseline_path = Path(__file__).parent / "data" / "athlete.json"
    if baseline_path.exists():
        with open(baseline_path) as f:
            athlete = json.load(f)
            return athlete.get("strength_baseline", {}).get("exercises", {})
    return {}


def get_baseline_weight(exercise_name: str, category: str, baseline: dict) -> float:
    """Get baseline weight for an exercise from strength baseline."""
    from config import DEFAULT_EQUIVALENCE_GROUPS

    # Normalize exercise lookup key
    category_key = category.lower().replace(' ', '_')

    # Check if we have a baseline for this category
    if category_key in baseline:
        current = baseline[category_key].get('current', {})
        return current.get('weight_kg')

    # Check via equivalence groups
    for group, exercises in DEFAULT_EQUIVALENCE_GROUPS.items():
        if exercise_name in exercises or category in exercises:
            group_key = group.lower().replace(' ', '_')
            if group_key in baseline:
                current = baseline[group_key].get('current', {})
                return current.get('weight_kg')

    return None


def build_strength_workout(session: dict, date: str) -> dict:
    """
    Build a strength workout from a plan session.

    If session contains 'exercises' list, builds detailed workout with
    RepeatGroupDTO for each exercise (containing sets x reps + rest).
    Otherwise creates a simple timed strength workout.

    Form cues from the exercise library are included as notes on each exercise.
    Weights are automatically pulled from strength baseline if not specified.
    """
    duration_mins = session.get("duration_mins", 45)
    description = session.get("description", "Strength training")
    exercises = session.get("exercises", [])

    # Load exercise library for form cues
    exercise_library = load_exercise_library()

    # Load strength baseline for weights
    strength_baseline = load_strength_baseline()

    # Load Garmin exercise DB for (category, exerciseName) validation
    exercise_db = load_exercise_db()

    # Create workout name from description
    workout_name = description[:40]

    # If no exercises provided, create simple timed workout
    if not exercises:
        total_secs = duration_mins * 60
        steps = [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": STEP_WARMUP,
                "endCondition": END_TIME,
                "endConditionValue": float(total_secs),
                "targetType": TARGET_NONE,
                "category": "CARDIO",
                "exerciseName": "",
            }
        ]
        return {
            "workoutName": workout_name,
            "sportType": STRENGTH_SPORT,
            "estimatedDurationInSecs": int(total_secs),
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "sportType": STRENGTH_SPORT,
                    "workoutSteps": steps
                }
            ]
        }

    # Build workout with exercise details using RepeatGroupDTO
    steps = []
    step_order = 1
    child_step_order = 1
    total_time = 300  # 5 min warmup

    # Add warmup step
    steps.append({
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": STEP_WARMUP,
        "endCondition": END_TIME,
        "endConditionValue": 300.0,  # 5 min warmup
        "targetType": TARGET_NONE,
        "category": "CARDIO",
        "exerciseName": "",
    })
    step_order += 1

    # Add REST step after warmup (press lap when ready for first exercise)
    steps.append({
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": STEP_REST,
        "endCondition": END_LAP_BUTTON,
        "targetType": TARGET_NONE,
    })
    step_order += 1

    # Each exercise becomes a RepeatGroupDTO
    for exercise in exercises:
        ex_name = exercise.get("name", "UNKNOWN")
        original_category = exercise.get("category", "OTHER")
        # Map non-standard categories to valid Garmin workout API categories.
        # Prefer exercise name as category key when it gives a better match
        # (e.g. PLANK with category SUSPENSION → name "PLANK" maps to CORE).
        if ex_name in GARMIN_CATEGORY_MAP:
            category = GARMIN_CATEGORY_MAP[ex_name]
        elif ex_name in VALID_WORKOUT_CATEGORIES:
            category = ex_name
        else:
            category = GARMIN_CATEGORY_MAP.get(original_category, original_category)
        sets = exercise.get("sets", 3)
        reps = exercise.get("reps", 10)
        # rest_secs is for time ESTIMATION only - actual rest uses lap button
        rest_secs = exercise.get("rest_secs", DEFAULT_REST_SECS)
        weight = exercise.get("weight_kg")

        # Validate (category, exerciseName) pair against Garmin's exercise DB.
        # If the exercise doesn't exist under the target category, Garmin
        # returns 400. Clear exerciseName and show in description instead.
        if category not in VALID_WORKOUT_CATEGORIES:
            garmin_exercise_name = ""
            category = "OTHER"
        elif category != original_category:
            # Category was remapped — exercise won't exist under new category
            garmin_exercise_name = ""
        else:
            # Category matches plan — verify against exercise DB
            db_entry = exercise_db.get(ex_name, {})
            db_category = db_entry.get("category")
            if db_category and db_category != category:
                # DB says this exercise is under a different category
                # (e.g. SQUAT exercise is under SUSPENSION, not SQUAT)
                garmin_exercise_name = ""
            elif not db_category and ex_name not in exercise_db:
                # Exercise not in DB at all — can't verify, be safe
                garmin_exercise_name = ""
            else:
                garmin_exercise_name = ex_name

        # If no explicit weight, try to get from strength baseline
        if not weight:
            weight = get_baseline_weight(ex_name, original_category, strength_baseline)

        # Get form cues from library if available
        exercise_note = get_exercise_note(ex_name, exercise_library)

        # Build step description: exercise name (when not in exerciseName) + form cues
        description = ""
        if not garmin_exercise_name:
            # Exercise name not in Garmin field — show it in description
            readable_name = ex_name.replace("_", " ").title()
            description = readable_name
            if exercise_note:
                # Append form cues after name, truncate to fit
                description = f"{readable_name}: {exercise_note}"
        elif exercise_note:
            description = exercise_note
        if description:
            description = description[:50]

        # Build exercise step (inside repeat group)
        exercise_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": child_step_order,
            "stepType": STEP_INTERVAL,
            "childStepId": 1,
            "endCondition": END_REPS,
            "endConditionValue": float(reps),
            "targetType": TARGET_NONE,
            "category": category,
            "exerciseName": garmin_exercise_name,
        }

        # Add description (exercise name and/or form cues)
        if description:
            exercise_step["description"] = description

        # Add weight if specified
        if weight:
            exercise_step["weightValue"] = float(weight)
            exercise_step["weightUnit"] = {
                "unitId": 8,
                "unitKey": "kilogram",
                "factor": 1000.0
            }

        child_step_order += 1

        # Build rest step (inside repeat group) - uses LAP BUTTON to end
        rest_step = {
            "type": "ExecutableStepDTO",
            "stepOrder": child_step_order,
            "stepType": STEP_REST,
            "childStepId": 1,
            "endCondition": END_LAP_BUTTON,
            "targetType": TARGET_NONE,
        }
        child_step_order += 1

        # Create RepeatGroupDTO containing exercise + rest
        repeat_group = {
            "type": "RepeatGroupDTO",
            "stepOrder": step_order,
            "stepType": STEP_REPEAT,
            "childStepId": 1,
            "numberOfIterations": sets,
            "workoutSteps": [exercise_step, rest_step],
            "endCondition": {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False
            },
            "endConditionValue": float(sets),
        }

        steps.append(repeat_group)
        step_order += 1

        # Estimate time for planning: ~45 secs per set + rest_secs
        total_time += sets * (45 + rest_secs)

    return {
        "workoutName": workout_name,
        "sportType": STRENGTH_SPORT,
        "estimatedDurationInSecs": int(total_time),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": STRENGTH_SPORT,
                "workoutSteps": steps
            }
        ],
        "exercise_count": len(exercises)
    }


def get_workout_type_name(session: dict) -> str:
    """Get human-readable workout type name."""
    session_type = session.get("type", "").lower()

    if session_type in CYCLING_TYPES or "ride" in session_type:
        return "cycling"
    elif session_type in RUNNING_TYPES or "run" in session_type:
        return "running"
    elif session_type in SWIMMING_TYPES:
        return "swimming"
    elif session_type in YOGA_TYPES:
        return "yoga"
    elif session_type in PILATES_TYPES:
        return "pilates"
    elif session_type in STRENGTH_TYPES:
        return "strength"
    elif session_type in PADEL_TYPES:
        return "padel"
    elif session_type in SKIP_TYPES:
        return "skipped"
    return "unknown"
