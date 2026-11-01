"""
Fetch the exercise catalogue from Garmin Connect into data/exercises.json.

Garmin files some names under several categories (SIDE_PLANK under PLANK and
SUSPENSION, ROW under five). Every category is kept in `categories` and the
primary one — the category sent when a plan states none — in `category`
(coach.authoring.primary_catalogue_category). `is_bodyweight` and
`counterpart` (the loaded or bodyweight twin: SIDE_PLANK <-> WEIGHTED_SIDE_PLANK)
come straight from Garmin. Custom entries the athlete added with add_exercise
survive a refresh, and the previous file is copied to data/backups first.

A one-category-per-name cache (the output of this script before 2026-10) is
flagged by get_coaching_snapshot under data_quality.exercise_catalogue and by
list_exercises; running this script replaces it.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.authoring import primary_catalogue_category  # noqa: E402
from coach.config import DATA_DIR  # noqa: E402

GARMIN_EXERCISES_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
OUTPUT_FILE = Path(DATA_DIR) / "exercises.json"
CATALOGUE_SCHEMA = 2  # 1: one category per name; 2: categories, is_bodyweight, counterpart

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


def _ordered_union(*lists) -> list:
    out: list = []
    for items in lists:
        for item in items or []:
            if item not in out:
                out.append(item)
    return out


def process_garmin_exercises(raw_data: dict, existing: dict | None = None) -> dict:
    """Process raw Garmin data into the catalogue format.

    Garmin data structure: {'categories': {CATEGORY: {'exercises': {EXERCISE: {...}}}}}
    with the same EXERCISE allowed under several categories. One entry per
    name: every category in `categories`, the primary in `category`, muscles
    merged, `is_bodyweight` / `counterpart` when Garmin states them. Custom
    entries from `existing` (add_exercise) are carried over when Garmin has
    no entry of that name.
    """
    merged: dict[str, dict] = {}
    categories_found = set()

    for category_name, category_data in (raw_data.get('categories', {}) or {}).items():
        categories_found.add(category_name)
        for exercise_name, exercise_data in (category_data.get('exercises', {}) or {}).items():
            exercise_data = exercise_data or {}
            entry = merged.setdefault(exercise_name, {
                'categories': [], 'primary_muscles': [], 'secondary_muscles': [],
            })
            if category_name not in entry['categories']:
                entry['categories'].append(category_name)
            entry['primary_muscles'] = _ordered_union(
                entry['primary_muscles'], [str(m).lower() for m in exercise_data.get('primaryMuscles', []) or []])
            entry['secondary_muscles'] = _ordered_union(
                entry['secondary_muscles'], [str(m).lower() for m in exercise_data.get('secondaryMuscles', []) or []])
            if exercise_data.get('isBodyWeight') is not None:
                entry['is_bodyweight'] = bool(exercise_data.get('isBodyWeight'))
            if exercise_data.get('counterpart'):
                entry['counterpart'] = str(exercise_data['counterpart'])

    exercises: dict[str, dict] = {}
    for exercise_name, entry in merged.items():
        categories = sorted(entry['categories'])
        primary = primary_catalogue_category(exercise_name, categories)
        record = {
            "category": primary,
            "categories": categories,
            "garmin_category": primary,  # the field the pre-2026-10 tools read
            "garmin_name": exercise_name,
            "muscles": _ordered_union(entry['primary_muscles'], entry['secondary_muscles']),
            "primary_muscles": entry['primary_muscles'],
            "secondary_muscles": entry['secondary_muscles'],
            "injury_prevention": get_injury_prevention_for_exercise(exercise_name),
        }
        if 'is_bodyweight' in entry:
            record["is_bodyweight"] = entry['is_bodyweight']
        if 'counterpart' in entry:
            record["counterpart"] = entry['counterpart']
        exercises[exercise_name] = record

    # Custom entries survive a refresh (Garmin wins when it now has the name)
    custom_names: list[str] = []
    existing_exercises = (existing or {}).get('exercises') or {}
    candidates = _ordered_union(
        (existing or {}).get('custom_exercises') or [],
        [n for n, e in existing_exercises.items() if isinstance(e, dict) and e.get('custom')])
    for name in candidates:
        entry = existing_exercises.get(name)
        if name in exercises or not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry['custom'] = True
        if not entry.get('categories'):
            entry['categories'] = [entry['category']] if entry.get('category') else []
        exercises[name] = entry
        custom_names.append(name)

    return {
        "exercises": exercises,
        "categories": sorted(categories_found),
        "injury_mappings": INJURY_PREVENTION,
        "custom_exercises": custom_names,
        "metadata": {
            "source": GARMIN_EXERCISES_URL,
            "schema": CATALOGUE_SCHEMA,
            "fetched_on": date.today().isoformat(),
            "exercise_count": len(exercises),
            "category_count": len(categories_found),
            "multi_category_count": sum(1 for e in exercises.values() if len(e.get('categories') or []) > 1),
            "bodyweight_count": sum(1 for e in exercises.values() if e.get('is_bodyweight')),
            "counterpart_count": sum(1 for e in exercises.values() if e.get('counterpart')),
            "custom_count": len(custom_names),
        }
    }


def load_existing(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"Existing catalogue unreadable, custom entries cannot be carried over: {exc}")
        return None


def backup_existing(path: Path) -> Path | None:
    """Copy the current catalogue into data/backups before it is replaced."""
    if not path.exists():
        return None
    backups = path.parent / "backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups / f"{path.name}.{stamp}.bak"
    target.write_bytes(path.read_bytes())
    return target


def main():
    """Fetch the catalogue, merge custom entries, back up and save."""
    try:
        raw_data = fetch_garmin_exercises()
        existing = load_existing(OUTPUT_FILE)
        library = process_garmin_exercises(raw_data, existing)

        DATA_DIR.mkdir(exist_ok=True)
        backup = backup_existing(OUTPUT_FILE)
        if backup:
            print(f"Previous catalogue backed up to {backup}")

        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(library, f, indent=2)

        meta = library['metadata']
        print(f"Saved {meta['exercise_count']} exercises in {meta['category_count']} categories "
              f"({meta['multi_category_count']} filed under more than one category, "
              f"{meta['bodyweight_count']} bodyweight, {meta['counterpart_count']} with a counterpart, "
              f"{meta['custom_count']} custom carried over)")
        print(f"Output: {OUTPUT_FILE}")
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
