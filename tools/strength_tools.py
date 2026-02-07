"""Strength sync tools - sync Garmin strength sessions, manage baselines, progressions, exercises."""

from mcp_app import mcp
from garmin_client import garmin_api_call
from parsers import parse_activities
from planner import load_json_file, save_json_file, load_athlete
from config import (
    DEFAULT_EQUIVALENCE_GROUPS,
    PROGRESSION_INCREMENT_KG,
    MIN_SETS_FOR_PROGRESSION,
    WEIGHT_GRAM_TO_KG,
    ATHLETE_FILE,
    DATA_DIR,
)
from datetime import date, timedelta
import json


# ============================================================================
# Helper functions (no decorator)
# ============================================================================

def _get_canonical_exercise_group(exercise_name: str, category: str, equivalence_groups: dict) -> str:
    """Map exercise name to canonical group."""

    # Merge default with custom groups
    groups = {**DEFAULT_EQUIVALENCE_GROUPS, **equivalence_groups}

    # Check if category is a known group
    if category in groups:
        return category

    # Check if exercise is in any group
    for group, exercises in groups.items():
        if exercise_name in exercises:
            return group

    # Fallback: use category as group
    return category


def _calculate_progression(current_weight: float, target_reps: int, actual_reps: int, actual_sets: int) -> dict:
    """Calculate progression suggestion based on performance."""
    if actual_sets >= MIN_SETS_FOR_PROGRESSION and actual_reps >= target_reps:
        return {
            "suggested_weight_kg": current_weight + PROGRESSION_INCREMENT_KG,
            "suggested_reps": target_reps,
            "rationale": f"Completed {actual_sets}x{actual_reps} @ {current_weight}kg - ready for +{PROGRESSION_INCREMENT_KG}kg",
            "status": "pending"
        }
    return None


def _get_strength_baseline_data() -> dict:
    """Load strength baseline from athlete profile."""

    athlete = load_athlete()
    baseline = athlete.get('strength_baseline', {})

    # Ensure equivalence groups exist
    if 'equivalence_groups' not in baseline:
        baseline['equivalence_groups'] = DEFAULT_EQUIVALENCE_GROUPS

    if 'exercises' not in baseline:
        baseline['exercises'] = {}

    return baseline


def _save_strength_baseline(baseline: dict) -> None:
    """Save strength baseline to athlete profile."""


    athlete = load_athlete()

    # Remove read-only fields before saving
    athlete.pop('baseline', None)
    athlete.pop('personal_records', None)
    athlete.pop('baseline_last_refreshed', None)

    athlete['strength_baseline'] = baseline

    save_json_file(ATHLETE_FILE, athlete)


# ============================================================================
# MCP Tools
# ============================================================================

@mcp.tool()
def sync_strength_session(activity_id: str = None) -> str:
    """
    Sync completed strength session from Garmin and update exercise baselines.

    Pulls exercise data (sets, reps, weights) from a completed strength workout
    and updates the athlete's strength baseline. Suggests progression when
    target reps are completed.

    Args:
        activity_id: Specific activity ID to sync. If omitted, syncs the most
                     recent strength session.

    Returns:
        JSON with synced exercises, baseline updates, PRs, and progression suggestions.

    Usage:
        sync_strength_session()  # Sync most recent strength session
        sync_strength_session("21536055257")  # Sync specific activity
    """
    try:
        today = date.today()

        # Find strength activity to sync
        if activity_id:
            target_activity_id = int(activity_id)
        else:
            # Find most recent strength activity
            week_ago = today - timedelta(days=7)
            raw_activities = garmin_api_call(
                lambda c: c.get_activities_by_date(
                    week_ago.isoformat(),
                    today.isoformat()
                )
            )
            activities = parse_activities(raw_activities)

            strength_activities = [
                a for a in activities
                if a.get('type') in ['strength_training', 'indoor_cardio', 'gym']
            ]

            if not strength_activities:
                return json.dumps({
                    "status": "no_activity",
                    "message": "No strength sessions found in the last 7 days"
                })

            # Get most recent
            target_activity_id = strength_activities[0]['activity_id']

        # Fetch exercise sets from Garmin
        try:
            exercise_sets = garmin_api_call(lambda c: c.get_activity_exercise_sets(target_activity_id))
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Could not fetch exercise sets: {str(e)}"
            })

        if not exercise_sets or 'exerciseSets' not in exercise_sets:
            return json.dumps({
                "status": "no_data",
                "message": "No exercise set data found for this activity"
            })

        # Load current baseline
        baseline = _get_strength_baseline_data()
        equivalence_groups = baseline.get('equivalence_groups', DEFAULT_EQUIVALENCE_GROUPS)

        # Process exercise sets
        exercise_summary = {}  # group -> {sets, reps, weight, variation}

        for exercise_set in exercise_sets['exerciseSets']:
            if exercise_set.get('setType') != 'ACTIVE':
                continue

            exercises = exercise_set.get('exercises', [])
            if not exercises:
                continue

            # Take the first exercise (highest probability)
            exercise = exercises[0]
            exercise_name = exercise.get('name')
            category = exercise.get('category')

            if not exercise_name or not category:
                continue

            # Map to canonical group
            group = _get_canonical_exercise_group(exercise_name, category, equivalence_groups)
            group_key = group.lower().replace('_', ' ').replace(' ', '_')

            # Get set data
            reps = exercise_set.get('repetitionCount', 0)
            weight_grams = exercise_set.get('weight', 0) or 0
            weight_kg = weight_grams / WEIGHT_GRAM_TO_KG

            # Aggregate by group
            if group_key not in exercise_summary:
                exercise_summary[group_key] = {
                    'canonical_name': group,
                    'variation': exercise_name,
                    'sets': 0,
                    'total_reps': 0,
                    'max_weight_kg': 0,
                    'weights': []
                }

            exercise_summary[group_key]['sets'] += 1
            exercise_summary[group_key]['total_reps'] += reps
            if weight_kg > 0:
                exercise_summary[group_key]['weights'].append(weight_kg)
                exercise_summary[group_key]['max_weight_kg'] = max(
                    exercise_summary[group_key]['max_weight_kg'],
                    weight_kg
                )

        # Update baselines
        updates = []
        prs = []
        progression_suggestions = []
        activity_date = today.isoformat()

        for group_key, data in exercise_summary.items():
            sets = data['sets']
            avg_reps = data['total_reps'] // sets if sets > 0 else 0
            weight_kg = data['max_weight_kg']
            variation = data['variation']

            # Get or create exercise baseline
            if group_key not in baseline['exercises']:
                baseline['exercises'][group_key] = {
                    'canonical_name': data['canonical_name'],
                    'preferred_variation': variation,
                    'current': None,
                    'history': [],
                    'progression': None
                }

            exercise_baseline = baseline['exercises'][group_key]
            previous = exercise_baseline.get('current')

            # Check for PR (handle None values with 'or 0')
            previous_weight = (previous.get('weight_kg') or 0) if previous else 0
            if previous and weight_kg > previous_weight:
                prs.append({
                    'exercise': group_key,
                    'previous_kg': previous_weight,
                    'new_kg': weight_kg,
                    'improvement_kg': weight_kg - previous_weight
                })

            # Update current (use previous weight if current is 0, avoid storing None)
            exercise_baseline['current'] = {
                'weight_kg': weight_kg if weight_kg > 0 else previous_weight if previous_weight > 0 else None,
                'reps': avg_reps,
                'sets': sets,
                'last_performed': activity_date
            }

            # Update preferred variation
            exercise_baseline['preferred_variation'] = variation

            # Add to history
            exercise_baseline['history'].append({
                'date': activity_date,
                'weight_kg': weight_kg,
                'reps': avg_reps,
                'sets': sets,
                'variation': variation
            })

            # Keep history to last 20 entries
            exercise_baseline['history'] = exercise_baseline['history'][-20:]

            # Calculate progression suggestion
            current_weight = exercise_baseline['current'].get('weight_kg', 0)
            if current_weight and current_weight > 0:
                progression = _calculate_progression(
                    current_weight,
                    target_reps=12,  # Default target
                    actual_reps=avg_reps,
                    actual_sets=sets
                )
                if progression:
                    exercise_baseline['progression'] = progression
                    progression_suggestions.append({
                        'exercise': group_key,
                        'current_kg': current_weight,
                        'suggested_kg': progression['suggested_weight_kg'],
                        'rationale': progression['rationale']
                    })

            updates.append({
                'exercise': group_key,
                'previous': previous,
                'current': exercise_baseline['current']
            })

        # Update last synced
        baseline['last_synced'] = activity_date

        # Save updated baseline
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "activity_id": target_activity_id,
            "activity_date": activity_date,
            "exercises_synced": len(exercise_summary),
            "updates": updates,
            "prs": prs,
            "progression_suggestions": progression_suggestions
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_strength_baseline(exercise: str = None) -> str:
    """
    View current strength baselines for exercises.

    Returns the current weights, reps, and progression status for all
    tracked exercises or a specific exercise group.

    Args:
        exercise: Specific exercise group to view (e.g., "bench_press").
                  If omitted, returns all baselines.

    Returns:
        JSON with current baselines, pending progressions, and history.

    Usage:
        get_strength_baseline()  # View all
        get_strength_baseline("bench_press")  # View specific
    """
    try:
        baseline = _get_strength_baseline_data()

        if exercise:
            # Normalize exercise name
            exercise_key = exercise.lower().replace(' ', '_')

            if exercise_key not in baseline.get('exercises', {}):
                return json.dumps({
                    "status": "not_found",
                    "exercise": exercise,
                    "available": list(baseline.get('exercises', {}).keys())
                })

            exercise_data = baseline['exercises'][exercise_key]
            return json.dumps({
                "exercise": exercise_key,
                "canonical_name": exercise_data.get('canonical_name'),
                "preferred_variation": exercise_data.get('preferred_variation'),
                "current": exercise_data.get('current'),
                "pending_progression": exercise_data.get('progression'),
                "recent_history": exercise_data.get('history', [])[-5:]
            }, indent=2)

        # Return summary of all exercises
        exercises_summary = {}
        pending_progressions = []

        for ex_key, ex_data in baseline.get('exercises', {}).items():
            current = ex_data.get('current', {})
            progression = ex_data.get('progression')

            exercises_summary[ex_key] = {
                'current_weight_kg': current.get('weight_kg'),
                'current_reps': current.get('reps'),
                'current_sets': current.get('sets'),
                'preferred_variation': ex_data.get('preferred_variation'),
                'last_performed': current.get('last_performed'),
                'has_pending_progression': progression is not None and progression.get('status') == 'pending'
            }

            if progression and progression.get('status') == 'pending':
                pending_progressions.append({
                    'exercise': ex_key,
                    'current_kg': current.get('weight_kg'),
                    'suggested_kg': progression.get('suggested_weight_kg'),
                    'rationale': progression.get('rationale')
                })

        return json.dumps({
            "last_synced": baseline.get('last_synced'),
            "exercises": exercises_summary,
            "pending_progressions": pending_progressions,
            "total_exercises_tracked": len(exercises_summary)
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def approve_progression(exercise: str) -> str:
    """
    Approve a suggested weight progression for an exercise.

    When a progression is approved, the suggested weight becomes the new
    baseline and will be used in future workout plans.

    Args:
        exercise: Exercise group to approve progression for (e.g., "bench_press")

    Returns:
        JSON confirmation with old and new weights.

    Usage:
        approve_progression("bench_press")
    """
    try:
        baseline = _get_strength_baseline_data()
        exercise_key = exercise.lower().replace(' ', '_')

        if exercise_key not in baseline.get('exercises', {}):
            return json.dumps({
                "status": "error",
                "message": f"Exercise '{exercise}' not found in baseline",
                "available": list(baseline.get('exercises', {}).keys())
            })

        exercise_data = baseline['exercises'][exercise_key]
        progression = exercise_data.get('progression')

        if not progression or progression.get('status') != 'pending':
            return json.dumps({
                "status": "error",
                "message": f"No pending progression for '{exercise}'"
            })

        # Get old and new weights
        old_weight = exercise_data['current'].get('weight_kg', 0)
        new_weight = progression['suggested_weight_kg']

        # Update current weight
        exercise_data['current']['weight_kg'] = new_weight

        # Mark progression as approved
        exercise_data['progression']['status'] = 'approved'
        exercise_data['progression']['approved_date'] = date.today().isoformat()

        # Save
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "exercise": exercise_key,
            "old_weight_kg": old_weight,
            "new_weight_kg": new_weight,
            "message": f"{exercise.replace('_', ' ').title()} progression approved. Next session: {exercise_data['current'].get('sets', 3)}x{exercise_data['current'].get('reps', 12)} @ {new_weight}kg"
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def set_exercise_preference(exercise_group: str, preferred_variation: str) -> str:
    """
    Set the preferred variation for an exercise group.

    When building workouts, the system will use your preferred variation
    instead of a generic exercise name.

    Args:
        exercise_group: Canonical group (e.g., "BENCH_PRESS", "ROW")
        preferred_variation: Specific exercise name (e.g., "BARBELL_BENCH_PRESS")

    Returns:
        JSON confirmation with updated preference.

    Usage:
        set_exercise_preference("BENCH_PRESS", "BARBELL_BENCH_PRESS")
    """

    try:
        baseline = _get_strength_baseline_data()
        exercise_key = exercise_group.lower().replace(' ', '_')

        # Validate the group exists
        all_groups = {**DEFAULT_EQUIVALENCE_GROUPS, **baseline.get('equivalence_groups', {})}
        group_upper = exercise_group.upper()

        if group_upper not in all_groups:
            return json.dumps({
                "status": "error",
                "message": f"Unknown exercise group: {exercise_group}",
                "available_groups": list(all_groups.keys())
            })

        # Validate the variation is in the group
        if preferred_variation not in all_groups[group_upper]:
            return json.dumps({
                "status": "error",
                "message": f"'{preferred_variation}' is not in the {group_upper} group",
                "available_variations": all_groups[group_upper]
            })

        # Update or create the exercise entry
        if exercise_key not in baseline['exercises']:
            baseline['exercises'][exercise_key] = {
                'canonical_name': group_upper,
                'preferred_variation': preferred_variation,
                'current': None,
                'history': [],
                'progression': None
            }
        else:
            baseline['exercises'][exercise_key]['preferred_variation'] = preferred_variation

        # Save
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "exercise_group": group_upper,
            "preferred_variation": preferred_variation,
            "message": f"Future {group_upper.lower().replace('_', ' ')} exercises will use {preferred_variation}"
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def generate_strength_workout(
    focus: str = "full_body",
    duration_mins: int = 45,
    equipment: str = "gym"
) -> str:
    """
    Generate a smart strength workout based on context.

    Automatically adjusts based on:
    - Recent activities (avoids legs after long cycle/frisbee/padel)
    - Injury history (always includes relevant prehab exercises)
    - Current training load and recovery

    Args:
        focus: Target area - "upper_body", "lower_body", "full_body", "core"
               Will be auto-adjusted based on recent activities.
        duration_mins: Target workout duration (default 45)
        equipment: "gym", "home", "minimal" (affects exercise selection)

    Returns:
        JSON with exercises, sets, reps, rationale, and auto-adjustments.
    """
    try:
        today = date.today()

        # Get today's and recent activities
        yesterday = today - timedelta(days=1)
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                yesterday.isoformat(),
                today.isoformat()
            )
        )
        recent_activities = parse_activities(raw_activities)

        # Get athlete profile for injury history
        athlete = load_athlete()
        injury_history = athlete.get("injury_history", [])
        active_injuries = [i for i in injury_history if i.get("status") == "active"]
        past_injuries = [i for i in injury_history if i.get("status") in ["resolved", "improving"]]

        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run fetch_exercises.py first."
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})
        injury_mappings = library.get("injury_mappings", {})

        # Analyze recent activities for auto-adjustment
        adjustments = []
        original_focus = focus
        avoid_muscle_groups = set()
        reduce_volume_groups = set()

        for activity in recent_activities:
            activity_type = activity.get("type", "").lower()
            duration = activity.get("duration_mins", 0) or 0

            # Long cycle (>60min) today → reduce leg volume
            if "cycling" in activity_type or "ride" in activity_type:
                if duration > 60:
                    reduce_volume_groups.update(["quadriceps", "glutes", "hamstrings", "calves"])
                    adjustments.append(f"Long cycle ({int(duration)}min) - reducing leg volume")
                    if focus == "lower_body":
                        focus = "upper_body"
                        adjustments.append("Switched focus to upper_body")
                    elif focus == "full_body":
                        adjustments.append("Will limit leg exercises in full_body workout")

            # Ultimate/Frisbee/Padel → avoid legs completely
            if any(sport in activity_type for sport in ["ultimate", "frisbee", "padel", "tennis", "squash"]):
                avoid_muscle_groups.update(["quadriceps", "glutes", "hamstrings", "calves"])
                adjustments.append(f"{activity_type.title()} done - avoiding leg exercises")
                if focus in ["lower_body", "full_body"]:
                    focus = "upper_body"
                    adjustments.append("Switched focus to upper_body")

            # Running → reduce calf/quad work
            if "running" in activity_type or "run" in activity_type:
                if duration > 30:
                    reduce_volume_groups.update(["calves", "quadriceps"])
                    adjustments.append(f"Running done ({int(duration)}min) - reducing calf/quad work")

        # Handle active injuries - avoid affected areas
        for injury in active_injuries:
            injury_type = injury.get("type", "").lower()
            if "ankle" in injury_type or "peroneal" in injury_type:
                avoid_muscle_groups.update(["calves"])
                adjustments.append(f"Active {injury_type} - avoiding calf exercises")
            if "knee" in injury_type:
                avoid_muscle_groups.update(["quadriceps"])
                adjustments.append(f"Active {injury_type} - reducing quad exercises")
            if "shoulder" in injury_type:
                adjustments.append(f"Active {injury_type} - avoiding overhead pressing")
            if "back" in injury_type:
                adjustments.append(f"Active {injury_type} - avoiding heavy spinal loading")

        # Determine prehab exercises from injury history
        prehab_exercises = []
        for injury in injury_history:
            injury_type = injury.get("type", "").lower()

            # Find matching injury prevention exercises
            for prevention_type, exercise_list in injury_mappings.items():
                if prevention_type in injury_type or injury_type in prevention_type:
                    for ex_name in exercise_list:
                        if ex_name in exercises:
                            ex_data = exercises[ex_name]
                            ex_muscles = ex_data.get("muscles", [])

                            # Skip if this exercise targets avoided muscles
                            if any(m in avoid_muscle_groups for m in ex_muscles):
                                continue

                            if ex_name not in [p["name"] for p in prehab_exercises]:
                                prehab_exercises.append({
                                    "name": ex_name,
                                    "reason": f"Injury prevention ({injury_type})",
                                    "sets": 2,
                                    "reps": 10,
                                    "rest_secs": 45
                                })
                                break  # One exercise per injury type

        # Select main exercises based on focus
        workout_exercises = []

        # Exercise templates by focus
        focus_templates = {
            "upper_body": {
                "push": ["BENCH_PRESS", "DUMBBELL_BENCH_PRESS", "PUSH_UP", "SHOULDER_PRESS", "DUMBBELL_SHOULDER_PRESS"],
                "pull": ["BENT_OVER_ROW", "DUMBBELL_ROW", "LAT_PULLDOWN", "SEATED_ROW", "PULL_UP"],
                "accessory": ["BICEP_CURL", "DUMBBELL_CURL", "TRICEP_EXTENSION", "FACE_PULL", "LATERAL_RAISE"]
            },
            "lower_body": {
                "compound": ["SQUAT", "BARBELL_SQUAT", "LEG_PRESS", "DEADLIFT", "ROMANIAN_DEADLIFT"],
                "isolation": ["LEG_EXTENSION", "LEG_CURL", "HAMSTRING_CURL", "CALF_RAISE", "HIP_THRUST"],
                "unilateral": ["LUNGE", "BULGARIAN_SPLIT_SQUAT", "STEP_UP", "SINGLE_LEG_DEADLIFT"]
            },
            "full_body": {
                "upper_push": ["BENCH_PRESS", "PUSH_UP", "SHOULDER_PRESS"],
                "upper_pull": ["BENT_OVER_ROW", "LAT_PULLDOWN", "PULL_UP"],
                "lower": ["SQUAT", "DEADLIFT", "LUNGE", "LEG_PRESS"],
                "core": ["PLANK", "DEAD_BUG", "RUSSIAN_TWIST"]
            },
            "core": {
                "anti_extension": ["PLANK", "DEAD_BUG", "ROLLOUT"],
                "anti_rotation": ["PALLOF_PRESS", "SIDE_PLANK", "BIRD_DOG"],
                "flexion": ["CRUNCH", "HANGING_LEG_RAISE", "CABLE_CRUNCH"]
            }
        }

        template = focus_templates.get(focus, focus_templates["full_body"])

        # Select exercises from each group
        for group_name, exercise_list in template.items():
            # Skip leg exercises if avoiding
            if avoid_muscle_groups and group_name in ["lower", "compound", "isolation", "unilateral"]:
                continue

            # Find available exercise from library
            for ex_name in exercise_list:
                if ex_name in exercises:
                    ex_data = exercises[ex_name]
                    muscles = ex_data.get("muscles", [])

                    # Skip if targets avoided muscles
                    if any(m in avoid_muscle_groups for m in muscles):
                        continue

                    # Determine sets/reps based on muscle group and volume adjustment
                    sets = 3
                    reps = 10
                    if any(m in reduce_volume_groups for m in muscles):
                        sets = 2
                        reps = 8

                    workout_exercises.append({
                        "name": ex_name,
                        "category": ex_data.get("garmin_category"),
                        "primary_muscles": ex_data.get("primary_muscles", []),
                        "sets": sets,
                        "reps": reps,
                        "rest_secs": 60 if "compound" not in group_name else 90
                    })
                    break  # One per group

        # Add prehab exercises at the end (limit to 2)
        prehab_to_add = prehab_exercises[:2]

        # Calculate estimated duration
        main_exercise_time = sum(
            (ex["sets"] * 45 + (ex["sets"] - 1) * ex["rest_secs"])
            for ex in workout_exercises
        ) / 60

        prehab_time = sum(
            (ex["sets"] * 30 + (ex["sets"] - 1) * ex["rest_secs"])
            for ex in prehab_to_add
        ) / 60

        warmup_time = 5
        estimated_duration = warmup_time + main_exercise_time + prehab_time

        # Build result
        result = {
            "focus": focus,
            "original_focus": original_focus if focus != original_focus else None,
            "auto_adjustments": adjustments if adjustments else ["No adjustments needed"],
            "exercises": workout_exercises,
            "prehab_exercises": prehab_to_add,
            "estimated_duration_mins": round(estimated_duration),
            "workout_structure": {
                "warmup": "5 mins dynamic stretching",
                "main_sets": len(workout_exercises),
                "prehab_sets": len(prehab_to_add)
            },
            "active_injuries": [i.get("type") for i in active_injuries] if active_injuries else None,
            "avoid_muscles": list(avoid_muscle_groups) if avoid_muscle_groups else None,
            "note": "Review and adjust exercises based on available equipment and preferences."
        }

        # Remove None values
        result = {k: v for k, v in result.items() if v is not None}

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def add_exercise(
    name: str,
    category: str,
    primary_muscles: str,
    secondary_muscles: str = None,
    injury_prevention: str = None
) -> str:
    """
    Add a custom exercise to the library.

    Use this when the LLM suggests an exercise not in Garmin's database.

    Args:
        name: Exercise name in UPPERCASE_WITH_UNDERSCORES (e.g., "NORDIC_CURL")
        category: Garmin category to group under (e.g., "HAMSTRING_CURL", "CUSTOM")
        primary_muscles: Comma-separated primary muscles (e.g., "hamstrings,glutes")
        secondary_muscles: Comma-separated secondary muscles (optional)
        injury_prevention: Comma-separated injury types this prevents (e.g., "hamstring,knee")

    Returns:
        Confirmation with the added exercise details.

    Example:
        add_exercise(
            name="NORDIC_CURL",
            category="HAMSTRING_CURL",
            primary_muscles="hamstrings",
            injury_prevention="hamstring"
        )
    """
    try:
        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run fetch_exercises.py first."
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})

        # Normalize name
        normalized_name = name.upper().replace(" ", "_").replace("-", "_")

        # Check if already exists
        if normalized_name in exercises:
            return json.dumps({
                "error": f"Exercise '{normalized_name}' already exists",
                "existing": exercises[normalized_name]
            })

        # Parse muscle lists
        primary = [m.strip().lower() for m in primary_muscles.split(",")]
        secondary = [m.strip().lower() for m in secondary_muscles.split(",")] if secondary_muscles else []
        prevention = [p.strip().lower() for p in injury_prevention.split(",")] if injury_prevention else []

        # Create exercise entry
        new_exercise = {
            "category": category.upper(),
            "garmin_category": category.upper(),
            "garmin_name": normalized_name,
            "muscles": primary + secondary,
            "primary_muscles": primary,
            "secondary_muscles": secondary,
            "injury_prevention": prevention,
            "custom": True  # Mark as user-added
        }

        # Add to library
        exercises[normalized_name] = new_exercise

        # Track in custom_exercises list
        if "custom_exercises" not in library:
            library["custom_exercises"] = []
        if normalized_name not in library["custom_exercises"]:
            library["custom_exercises"].append(normalized_name)

        # Update metadata
        library["metadata"]["exercise_count"] = len(exercises)

        # Save
        with open(exercises_file, 'w') as f:
            json.dump(library, f, indent=2)

        return json.dumps({
            "status": "success",
            "message": f"Added custom exercise: {normalized_name}",
            "exercise": new_exercise,
            "total_exercises": len(exercises),
            "custom_exercises_count": len(library["custom_exercises"])
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})
