"""
First-run setup wizard for new users.

Creates personal data files with user-provided info.
Run this before starting the server for the first time.
"""
import json
from pathlib import Path
from datetime import date

DATA_DIR = Path(__file__).parent.parent / "data"


def get_input(prompt: str, default: str = None) -> str:
    """Get input with optional default value."""
    if default:
        result = input(f"{prompt} [{default}]: ").strip()
        return result if result else default
    return input(f"{prompt}: ").strip()


def get_int(prompt: str, default: int = None) -> int:
    """Get integer input with validation."""
    while True:
        try:
            value = get_input(prompt, str(default) if default else None)
            return int(value)
        except ValueError:
            print("Please enter a valid number.")


def calculate_max_hr(age: int) -> int:
    """Estimate max HR from age (220 - age formula)."""
    return 220 - age


def create_athlete_file() -> dict:
    """Interactive setup for athlete.json."""
    print("\n=== Athlete Profile Setup ===\n")

    name = get_input("Your name (optional)", "")
    age = get_int("Your age", 30)

    # Calculate or ask for max HR
    estimated_max = calculate_max_hr(age)
    print(f"\nEstimated max HR based on age: {estimated_max} bpm")
    use_estimated = get_input("Use this value? (y/n)", "y").lower() == "y"

    if use_estimated:
        max_hr = estimated_max
    else:
        max_hr = get_int("Enter your known max HR")

    resting_hr = get_int("Resting HR (from Garmin or manual)", 50)

    # Calculate HR zones (5-zone model)
    hr_reserve = max_hr - resting_hr
    zones = {
        "z1_recovery": [int(resting_hr + 0.5 * hr_reserve), int(resting_hr + 0.6 * hr_reserve)],
        "z2_aerobic": [int(resting_hr + 0.6 * hr_reserve), int(resting_hr + 0.7 * hr_reserve)],
        "z3_tempo": [int(resting_hr + 0.7 * hr_reserve), int(resting_hr + 0.8 * hr_reserve)],
        "z4_threshold": [int(resting_hr + 0.8 * hr_reserve), int(resting_hr + 0.9 * hr_reserve)],
        "z5_max": [int(resting_hr + 0.9 * hr_reserve), max_hr],
    }

    print("\n=== Life Constraints ===\n")
    print("Do you have any recurring commitments that affect training?")
    print("(e.g., 'Wednesday afternoon padel', 'Sunday family time')")
    commitments = []
    while True:
        commitment = get_input("Add commitment (or press Enter to skip)", "")
        if not commitment:
            break
        commitments.append({"description": commitment})

    print("\n=== Preferences ===\n")
    likes = get_input("Activities you enjoy (comma-separated)", "outdoor rides, variety")
    dislikes = get_input("Activities you avoid (comma-separated)", "treadmill")

    athlete = {
        "personal": {
            "name": name if name else None,
            "age": age,
            "max_hr": max_hr,
            "resting_hr": resting_hr,
            "hr_zones": zones,
            "ftp": None,
            "threshold_pace_sec_per_km": None,
            "pace_zones": {
                "z1_recovery": None,
                "z2_easy": None,
                "z3_tempo": None,
                "z4_threshold": None,
                "z5_interval": None
            },
            "weight_kg": None,
        },
        "life_constraints": {
            "recurring_commitments": commitments,
            "preferred_training_times": ["morning", "evening"],
            "no_training_days": [],
        },
        "injury_history": [],
        "preferences": {
            "likes": [x.strip() for x in likes.split(",")],
            "dislikes": [x.strip() for x in dislikes.split(",")],
            "equipment": [],
            "gym_access": None,
        },
        "coaching_notes": "",
    }

    return athlete


def create_training_config() -> dict:
    """Create default training_config.json."""
    print("\n=== Training Configuration ===\n")

    volume = get_int("Weekly training volume target (hours)", 8)

    print("\nWhat phase are you in?")
    print("  1. Base (building aerobic foundation)")
    print("  2. Build (adding intensity)")
    print("  3. Peak (race-specific)")
    print("  4. Recovery (easy week)")

    phase_map = {"1": "base", "2": "build", "3": "peak", "4": "recovery"}
    phase_choice = get_input("Choose phase (1-4)", "1")
    phase = phase_map.get(phase_choice, "base")

    config = {
        "events": [],
        "current_block": {
            "phase": phase,
            "start_date": date.today().isoformat(),
            "weekly_volume_target_hrs": volume,
            "focus": ["endurance"],
            "notes": "Created by setup wizard",
        },
        "thresholds": {
            "hard_hr_avg": 150,
            "hard_hr_max": 175,
            "long_effort_min_mins": 60,
            "volume_compliance_percent": 80,
        },
        "race_analysis": {
            "elevation_significance_m": 1000,
            "high_altitude_m": 1500,
        },
    }

    print("\nYou can add races later using the races(action='add') tool or by")
    print("editing data/training_config.json directly.")

    return config


def create_weekly_plan() -> dict:
    """Create empty weekly_plan.json."""
    return {
        "week_start": date.today().isoformat(),
        "days": {},
        "notes": "No plan yet - call get_coaching_snapshot() then update_weekly_plan() to create one.",
    }


def create_coaching_log() -> dict:
    """Create empty coaching_log.json for LLM memory."""
    return {
        "decisions": [],
        "pending_approvals": [],
        "athlete_responses": [],
        "metadata": {
            "created": date.today().isoformat(),
            "last_updated": date.today().isoformat(),
            "version": "1.0"
        }
    }


def check_setup_needed() -> bool:
    """Check if setup is needed (missing required files)."""
    required_files = [
        DATA_DIR / "athlete.json",
        DATA_DIR / "training_config.json",
    ]
    return not all(f.exists() for f in required_files)


def run_setup():
    """Run the full setup wizard."""
    print("=" * 50)
    print("  AI Training Coach - First Run Setup")
    print("=" * 50)

    DATA_DIR.mkdir(exist_ok=True)

    # Check what's already there
    athlete_exists = (DATA_DIR / "athlete.json").exists()
    config_exists = (DATA_DIR / "training_config.json").exists()
    redo = False  # Initialize to avoid scope issues

    if athlete_exists and config_exists:
        print("\nSetup already complete! Your data files exist:")
        print(f"  - {DATA_DIR / 'athlete.json'}")
        print(f"  - {DATA_DIR / 'training_config.json'}")

        redo = get_input("\nRe-run setup anyway? (y/n)", "n").lower() == "y"
        if not redo:
            return

    # Create athlete profile
    if not athlete_exists or redo:
        athlete = create_athlete_file()
        with open(DATA_DIR / "athlete.json", "w") as f:
            json.dump(athlete, f, indent=2)
        print(f"\nCreated: {DATA_DIR / 'athlete.json'}")

    # Create training config
    if not config_exists or redo:
        config = create_training_config()
        with open(DATA_DIR / "training_config.json", "w") as f:
            json.dump(config, f, indent=2)
        print(f"Created: {DATA_DIR / 'training_config.json'}")

    # Create empty weekly plan (only if doesn't exist - preserve existing plans)
    plan_path = DATA_DIR / "weekly_plan.json"
    if not plan_path.exists():
        plan = create_weekly_plan()
        with open(plan_path, "w") as f:
            json.dump(plan, f, indent=2)
        print(f"Created: {plan_path}")
    else:
        print(f"Preserved existing: {plan_path}")

    # Create empty coaching log (only if doesn't exist - preserve LLM memory)
    log_path = DATA_DIR / "coaching_log.json"
    if not log_path.exists():
        coaching_log = create_coaching_log()
        with open(log_path, "w") as f:
            json.dump(coaching_log, f, indent=2)
        print(f"Created: {log_path}")
    else:
        print(f"Preserved existing: {log_path}")

    print("\n" + "=" * 50)
    print("  Setup Complete!")
    print("=" * 50)
    print("\nNext steps:")
    print("  1. Make sure your .env file has Garmin credentials")
    print("  2. Register with Claude Code:")
    print("       claude mcp add coach-mcp -- python server.py")
    print("  3. Open Claude Code and say:")
    print('       "I\'d like to set up my training plan"')
    print("  The coach will pull your Garmin data and build your plan.")
    print()


if __name__ == "__main__":
    run_setup()
