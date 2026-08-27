"""
Fetch exercise database from Garmin Connect.

This script fetches all exercises from Garmin's public API and creates
a local exercise library with muscle groups and injury prevention mappings.
"""
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.config import DATA_DIR

GARMIN_EXERCISES_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
OUTPUT_FILE = Path(DATA_DIR) / "exercises.json"

# Injury prevention mappings - which exercises help which injuries
INJURY_PREVENTION = {
    "hamstring": [
        "NORDIC_CURL", "ROMANIAN_DEADLIFT", "LYING_HAMSTRING_CURL",
        "GOOD_MORNING", "SINGLE_LEG_DEADLIFT", "HAMSTRING_STRETCH"
    ],
    "knee": [
        "TERMINAL_KNEE_EXTENSION", "STEP_UP", "WALL_SIT",
        "LEG_EXTENSION", "SINGLE_LEG_SQUAT", "QUAD_STRETCH"
    ],
    "shoulder": [
        "FACE_PULL", "EXTERNAL_ROTATION", "BAND_PULL_APART",
        "CUBAN_PRESS", "SHOULDER_DISLOCATE", "REAR_DELT_FLY"
    ],
    "back": [
        "DEAD_BUG", "BIRD_DOG", "CAT_COW", "SUPERMAN",
        "MCGILL_CURL_UP", "SIDE_PLANK"
    ],
    "ankle": [
        "STANDING_CALF_RAISE", "SEATED_CALF_RAISE", "ANKLE_CIRCLES",
        "TOE_RAISES", "HEEL_WALKS"
    ],
    "peroneal": [
        "CALF_RAISE", "ANKLE_EVERSION", "BALANCE_BOARD",
        "SINGLE_LEG_BALANCE", "PERONEAL_STRETCH"
    ],
    "shin": [
        "TOE_RAISES", "CALF_STRETCH", "TIBIALIS_RAISE",
        "HEEL_WALKS", "ANKLE_DORSIFLEXION"
    ],
}


def fetch_garmin_exercises():
    """Fetch exercises from Garmin's public API."""
    print(f"Fetching exercises from {GARMIN_EXERCISES_URL}...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    response = requests.get(GARMIN_EXERCISES_URL, headers=headers, timeout=30)
    response.raise_for_status()

    return response.json()


def get_injury_prevention_for_exercise(exercise_name: str) -> list:
    """Check if an exercise is good for injury prevention."""
    exercise_upper = exercise_name.upper()
    prevention = []

    for injury, exercises in INJURY_PREVENTION.items():
        for ex in exercises:
            if ex in exercise_upper or exercise_upper in ex:
                prevention.append(injury)
                break

    return prevention


def process_garmin_exercises(raw_data: dict) -> dict:
    """Process raw Garmin data into our exercise library format."""
    exercises = {}
    categories_found = set()

    # Garmin data structure: {'categories': {CATEGORY: {'exercises': {EXERCISE: {...}}}}}
    categories_data = raw_data.get('categories', {})

    for category_name, category_data in categories_data.items():
        categories_found.add(category_name)

        # Get exercises from category
        category_exercises = category_data.get('exercises', {})

        for exercise_name, exercise_data in category_exercises.items():
            # Garmin provides primaryMuscles and secondaryMuscles
            primary = exercise_data.get('primaryMuscles', [])
            secondary = exercise_data.get('secondaryMuscles', [])

            # Combine and normalize muscle names
            all_muscles = [m.lower() for m in primary + secondary]

            exercises[exercise_name] = {
                "category": category_name,
                "garmin_category": category_name,
                "garmin_name": exercise_name,
                "muscles": all_muscles,
                "primary_muscles": [m.lower() for m in primary],
                "secondary_muscles": [m.lower() for m in secondary],
                "injury_prevention": get_injury_prevention_for_exercise(exercise_name),
            }

    return {
        "exercises": exercises,
        "categories": sorted(list(categories_found)),
        "injury_mappings": INJURY_PREVENTION,
        "custom_exercises": [],
        "metadata": {
            "source": GARMIN_EXERCISES_URL,
            "exercise_count": len(exercises),
            "category_count": len(categories_found),
        }
    }


def main():
    """Main function to fetch and save exercise library."""
    try:
        # Fetch from Garmin
        raw_data = fetch_garmin_exercises()

        # Process into our format
        library = process_garmin_exercises(raw_data)

        # Ensure data directory exists
        DATA_DIR.mkdir(exist_ok=True)

        # Save to file
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(library, f, indent=2)

        print(f"Saved {library['metadata']['exercise_count']} exercises "
              f"in {library['metadata']['category_count']} categories")
        print(f"Output: {OUTPUT_FILE}")

        # Print some stats
        print(f"\nCategories: {', '.join(library['categories'][:10])}...")
        print(f"Injury prevention mappings: {list(INJURY_PREVENTION.keys())}")

    except requests.RequestException as e:
        print(f"Error fetching exercises: {e}")
        raise
    except Exception as e:
        print(f"Error processing exercises: {e}")
        raise


if __name__ == "__main__":
    main()
