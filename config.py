"""
Shared configuration and constants for coach-mcp.

Centralizes paths, thresholds, and magic numbers to avoid duplication
and make the codebase easier to maintain.
"""
from pathlib import Path

# Paths
DATA_DIR = Path(__file__).parent / "data"
TOKEN_DIR = str(Path(__file__).parent / ".garth")  # String for garth compatibility

# Data file names
ATHLETE_FILE = "athlete.json"
ATHLETE_BASELINE_FILE = "athlete_baseline.json"
TRAINING_CONFIG_FILE = "training_config.json"
METHODOLOGY_FILE = "methodology.json"
WEEKLY_PLAN_FILE = "weekly_plan.json"
SUGGESTIONS_FILE = "suggestions.json"

# Activity classification thresholds
LONG_EFFORT_MIN_MINS = 60
HARD_HR_AVG_THRESHOLD = 150
HARD_HR_MAX_THRESHOLD = 175

# Compliance thresholds
VOLUME_COMPLIANCE_MIN_PERCENT = 80

# API timeouts (seconds)
HTTP_TIMEOUT_SECONDS = 15

# Data retention (days)
PROFILE_HISTORY_DAYS = 180
RECENT_ACTIVITY_DAYS = 35  # 5 weeks to view current week within 4-week block context

# Web scraping limits
PAGE_TEXT_MAX_CHARS = 8000
ELEVATION_SIGNIFICANCE_THRESHOLD = 1000
HIGH_ALTITUDE_THRESHOLD = 1500

# Valid race priorities
VALID_PRIORITIES = ['A', 'B', 'C', 'D']

# Injury assessment configuration
INJURY_SEVERITY_LEVELS = ['mild', 'moderate', 'severe']
INJURY_STATUS_OPTIONS = ['active', 'improving', 'resolved']

# Clinical assessment questions by body region
INJURY_ASSESSMENT_QUESTIONS = {
    "default": [
        {"id": "onset", "question": "When did the pain start?", "options": ["Sudden (during activity)", "Gradual (over days/weeks)"]},
        {"id": "pain_type", "question": "How would you describe the pain?", "options": ["Sharp/stabbing", "Dull/aching", "Burning", "Throbbing"]},
        {"id": "timing", "question": "When does it hurt?", "options": ["Only during activity", "During and after activity", "At rest too", "Constant"]},
        {"id": "swelling", "question": "Is there visible swelling or bruising?", "options": ["Yes, significant", "Slight swelling", "No"]},
        {"id": "history", "question": "Any previous injury to this area?", "options": ["Yes", "No"]},
        {"id": "recent_changes", "question": "Any recent training changes?", "options": ["Increased volume", "New equipment", "New surface/terrain", "No changes"]},
    ],
    "shin": [
        {"id": "location_specific", "question": "Where exactly on the shin?", "options": ["Front (anterior)", "Inner side (medial)", "Outer side (lateral)", "Along the bone"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Walking", "Running", "Going up stairs", "Pointing toes up", "Pushing off"]},
    ],
    "knee": [
        {"id": "location_specific", "question": "Where exactly on the knee?", "options": ["Front (kneecap)", "Inner side (medial)", "Outer side (lateral)", "Behind the knee"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Stairs (up)", "Stairs (down)", "Squatting", "Running", "Sitting long time"]},
        {"id": "locking", "question": "Does the knee lock, catch, or give way?", "options": ["Yes, locks/catches", "Yes, gives way", "No"]},
    ],
    "ankle": [
        {"id": "location_specific", "question": "Where exactly on the ankle?", "options": ["Front", "Inner side (medial)", "Outer side (lateral)", "Back (Achilles)"]},
        {"id": "mechanism", "question": "Did you roll or twist it?", "options": ["Yes, inward", "Yes, outward", "No twist"]},
        {"id": "weight_bearing", "question": "Can you put weight on it?", "options": ["Yes, normal", "Yes, with pain", "No, too painful"]},
    ],
    "back": [
        {"id": "location_specific", "question": "Where exactly on the back?", "options": ["Lower back (lumbar)", "Mid back (thoracic)", "Upper back/neck"]},
        {"id": "radiation", "question": "Does pain radiate down your leg or arm?", "options": ["Yes, past the knee/elbow", "Yes, but only partly", "No"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Bending forward", "Bending backward", "Twisting", "Sitting", "Standing"]},
    ],
    "shoulder": [
        {"id": "location_specific", "question": "Where exactly on the shoulder?", "options": ["Front", "Top", "Back", "Side (deltoid)"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Overhead movements", "Reaching behind back", "Lying on it", "Pushing", "Pulling"]},
        {"id": "weakness", "question": "Any weakness or inability to lift arm?", "options": ["Yes, significant weakness", "Some weakness", "No weakness"]},
    ],
    "hip": [
        {"id": "location_specific", "question": "Where exactly is the pain?", "options": ["Front (groin)", "Side (lateral)", "Back (buttock)", "Deep inside"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Walking", "Running", "Sitting", "Stairs", "Getting out of car"]},
    ],
    "foot": [
        {"id": "location_specific", "question": "Where exactly on the foot?", "options": ["Heel (bottom)", "Heel (back)", "Arch", "Ball of foot", "Toes"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["First steps in morning", "Walking barefoot", "Running", "After rest"]},
    ],
    "calf": [
        {"id": "location_specific", "question": "Where exactly on the calf?", "options": ["Upper calf (near knee)", "Mid calf", "Lower calf (near Achilles)"]},
        {"id": "aggravating", "question": "What makes it worse?", "options": ["Walking", "Running", "Pushing off", "Going up stairs", "Stretching"]},
    ],
}

# Activity restrictions by injury type (common patterns)
INJURY_ACTIVITY_RESTRICTIONS = {
    "tendinitis": {
        "avoid": ["running", "jumping", "high_impact"],
        "caution": ["hiking", "stairs"],
        "safe": ["cycling", "swimming", "upper_body_strength"],
    },
    "muscle_strain": {
        "avoid": ["stretching_affected", "explosive_movements"],
        "caution": ["light_activity"],
        "safe": ["rest", "gentle_movement"],
    },
    "stress_fracture": {
        "avoid": ["all_weight_bearing", "impact"],
        "caution": [],
        "safe": ["swimming", "upper_body"],
    },
    "sprain": {
        "avoid": ["lateral_movements", "running"],
        "caution": ["walking", "balance"],
        "safe": ["cycling", "swimming"],
    },
}
