"""
Workout Builder - Converts plan sessions to Garmin workout format.

Uses the garminconnect.workout module to create structured workouts
that can be uploaded and scheduled to Garmin Connect.
"""
from typing import Any

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

# Session types that map to cycling
CYCLING_TYPES = {"long_ride", "easy_ride", "cycling", "ride", "mtb", "road_ride"}

# Session types that map to running
RUNNING_TYPES = {"run", "long_run", "easy_run", "running", "trail_run", "interval_run"}

# Session types that map to yoga
YOGA_TYPES = {"yoga", "mobility", "stretching", "pilates"}

# Session types that map to strength
STRENGTH_TYPES = {"strength", "strength_training", "gym", "weights"}

# Session types to skip (not pushable to Garmin)
SKIP_TYPES = {"rest", "rest_or_easy"}


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
    if session_type in CYCLING_TYPES or "ride" in session_type or "cycling" in session_type:
        return build_cycling_workout(session, date)
    elif session_type in RUNNING_TYPES or "run" in session_type:
        return build_running_workout(session, date)
    elif session_type in YOGA_TYPES:
        return build_yoga_workout(session, date)
    elif session_type in STRENGTH_TYPES:
        return build_strength_workout(session, date)

    return None


def build_cycling_workout(session: dict, date: str) -> CyclingWorkout:
    """Build a cycling workout from a plan session."""
    duration_mins = session.get("duration_mins", 60)
    description = session.get("description", "Cycling workout")
    intensity = session.get("intensity", "easy")

    # Calculate segment durations (in seconds)
    total_secs = duration_mins * 60
    warmup_secs = min(600, total_secs * 0.1)  # 10 min or 10% max
    cooldown_secs = min(300, total_secs * 0.05)  # 5 min or 5% max
    main_secs = total_secs - warmup_secs - cooldown_secs

    # Create workout name from description
    workout_name = description[:40]

    # Build steps
    steps = [
        ExecutableStep(
            stepOrder=1,
            stepType=STEP_WARMUP,
            endCondition=END_TIME,
            endConditionValue=warmup_secs,
            targetType=TARGET_NONE
        ),
        ExecutableStep(
            stepOrder=2,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=TARGET_NONE if intensity == "easy" else TARGET_HR
        ),
        ExecutableStep(
            stepOrder=3,
            stepType=STEP_COOLDOWN,
            endCondition=END_TIME,
            endConditionValue=cooldown_secs,
            targetType=TARGET_NONE
        )
    ]

    return CyclingWorkout(
        workoutName=workout_name,
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType=CYCLING_SPORT,
                workoutSteps=steps
            )
        ]
    )


def build_running_workout(session: dict, date: str) -> RunningWorkout:
    """Build a running workout from a plan session."""
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

    # Build steps
    steps = [
        ExecutableStep(
            stepOrder=1,
            stepType=STEP_WARMUP,
            endCondition=END_TIME,
            endConditionValue=warmup_secs,
            targetType=TARGET_NONE
        ),
        ExecutableStep(
            stepOrder=2,
            stepType=STEP_INTERVAL,
            endCondition=END_TIME,
            endConditionValue=main_secs,
            targetType=TARGET_NONE if intensity == "easy" else TARGET_HR
        ),
        ExecutableStep(
            stepOrder=3,
            stepType=STEP_COOLDOWN,
            endCondition=END_TIME,
            endConditionValue=cooldown_secs,
            targetType=TARGET_NONE
        )
    ]

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


def build_strength_workout(session: dict, date: str) -> dict:
    """
    Build a strength workout from a plan session.

    If session contains 'exercises' list, builds detailed workout with
    RepeatGroupDTO for each exercise (containing sets x reps + rest).
    Otherwise creates a simple timed strength workout.
    """
    duration_mins = session.get("duration_mins", 45)
    description = session.get("description", "Strength training")
    exercises = session.get("exercises", [])

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

    # Each exercise becomes a RepeatGroupDTO
    for exercise in exercises:
        ex_name = exercise.get("name", "UNKNOWN")
        category = exercise.get("category", "OTHER")
        sets = exercise.get("sets", 3)
        reps = exercise.get("reps", 10)
        # rest_secs is for time ESTIMATION only - actual rest uses lap button
        rest_secs = exercise.get("rest_secs", DEFAULT_REST_SECS)
        weight = exercise.get("weight_kg")

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
            "exerciseName": ex_name,
        }

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
    elif session_type in YOGA_TYPES:
        return "yoga"
    elif session_type in STRENGTH_TYPES:
        return "strength"
    elif session_type in SKIP_TYPES:
        return "skipped"
    return "unknown"
