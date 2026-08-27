"""
Shared configuration and constants for coach-mcp.

Centralizes paths, thresholds, and magic numbers to avoid duplication
and make the codebase easier to maintain.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .taxonomy import (
    RACE_TYPE_TO_SPORT,
    garmin_types_in_sport_group,
    sport_group_for,
)

# ---------------------------------------------------------------------------
# Path resolution (packaging-aware)
#
# Precedence for the data dir:
#   1. COACH_DATA_DIR env var (explicit override)
#   2. a data/ dir next to the package parent — i.e. running from a git
#      checkout (repo layout: <repo>/coach/ + <repo>/data/)
#   3. a per-user data dir (pip/uvx installs), no third-party deps
#
# DATA_DIR / TOKEN_DIR stay module-level bindings: tests monkeypatch them
# per module (see tests/conftest.py sandbox_data_dir).
# ---------------------------------------------------------------------------

_CHECKOUT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Packaged default data files (shipped inside the wheel; see
# coach/parsers.py bootstrap_data_dir which seeds a fresh data dir from here)
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


def user_data_dir(app_name: str = "coach-mcp") -> Path:
    """Per-user application data dir without third-party dependencies.

    Windows: %LOCALAPPDATA%/coach-mcp
    macOS:   ~/Library/Application Support/coach-mcp
    Linux:   $XDG_DATA_HOME/coach-mcp or ~/.local/share/coach-mcp
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    base = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share")
    return Path(base) / app_name


def resolve_data_dir() -> Path:
    """Resolve the data dir: COACH_DATA_DIR > git checkout > per-user dir."""
    env = os.environ.get("COACH_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if _CHECKOUT_DATA_DIR.is_dir():
        return _CHECKOUT_DATA_DIR
    return user_data_dir()


def resolve_token_dir(data_dir: Path | None = None) -> str:
    """Resolve the Garmin token store dir (str — garminconnect's
    login(tokenstore=...) API takes a path string).

    COACH_TOKEN_DIR wins; otherwise <repo>/.garth when the data dir resolved
    to a git checkout, else <user-dir>/garmin-tokens.
    """
    env = os.environ.get("COACH_TOKEN_DIR", "").strip()
    if env:
        return str(Path(env).expanduser())
    if data_dir is None:
        data_dir = resolve_data_dir()
    if data_dir == _CHECKOUT_DATA_DIR:
        return str(_CHECKOUT_DATA_DIR.parent / ".garth")
    return str(user_data_dir() / "garmin-tokens")


# Paths
DATA_DIR = resolve_data_dir()
TOKEN_DIR = resolve_token_dir(DATA_DIR)  # String for garminconnect's tokenstore API

# History retention windows (days), applied at persist time in coach/fitness.py.
# None = keep everything. A coach keeps the athlete's full training diary —
# the old fixed prunes (sleep 30d, readiness 60d, CTL/ACWR snapshots 90d)
# silently erased history (14 sleep nights survived a year of coaching) and
# were retired as a defect on 2026-08-27.
SLEEP_HISTORY_RETENTION_DAYS: int | None = None
READINESS_HISTORY_RETENTION_DAYS: int | None = None
SNAPSHOT_RETENTION_DAYS: int | None = None

# .env loading: current working dir / project tree first (highest precedence —
# load_dotenv never overrides already-set values), then the resolved data dir
# as a fallback for installed (pip/uvx) deployments.
load_dotenv()
load_dotenv(DATA_DIR / ".env")

# Data file names
ATHLETE_FILE = "athlete.json"
ATHLETE_BASELINE_FILE = "athlete_baseline.json"
TRAINING_CONFIG_FILE = "training_config.json"
METHODOLOGY_FILE = "methodology.json"
WEEKLY_PLAN_FILE = "weekly_plan.json"
COACHING_LOG_FILE = "coaching_log.json"
FITNESS_HISTORY_FILE = "fitness_history.json"
EXERCISE_LIBRARY_FILE = "exercise_library.json"  # Cached exercise form cues

# Fitness tracking constants (based on TrainingPeaks/Firstbeat research)
# CTL = Chronic Training Load (fitness) - 42-day time constant
# ATL = Acute Training Load (fatigue) - 7-day time constant
CTL_TIME_CONSTANT_DAYS = 42
ATL_TIME_CONSTANT_DAYS = 7

# ACWR (Acute:Chronic Workload Ratio) thresholds
# Research shows 0.8-1.3 is the "sweet spot" for injury prevention
ACWR_LOW_THRESHOLD = 0.8    # Below this = undertrained/deconditioned
ACWR_HIGH_THRESHOLD = 1.3   # Above this = injury risk elevated
ACWR_DANGER_THRESHOLD = 1.5 # Above this = high injury risk

# CTL targets by race type (based on typical demands)
# These are target CTL values to be race-ready
CTL_TARGETS = {
    "multi_day_mtb": {"min": 55, "ideal": 65, "description": "3-day MTB stage race (e.g., sani2c)"},
    "road_cycling": {"min": 45, "ideal": 55, "description": "Road race 80-120km"},
    "running_marathon": {"min": 50, "ideal": 60, "description": "Marathon (42km)"},
    "running_ultra": {"min": 60, "ideal": 75, "description": "Ultra marathon (50km+)"},
    "running_half": {"min": 40, "ideal": 50, "description": "Half marathon"},
    "triathlon_olympic": {"min": 50, "ideal": 60, "description": "Olympic triathlon"},
    "triathlon_half": {"min": 55, "ideal": 70, "description": "70.3 triathlon"},
    "triathlon_full": {"min": 65, "ideal": 80, "description": "Ironman triathlon"},
    "casual": {"min": 25, "ideal": 35, "description": "Recreational fitness"},
    "default": {"min": 40, "ideal": 50, "description": "General endurance event"},
}

# TSS to hours approximation (varies by intensity, this is average)
TSS_PER_HOUR_ESTIMATE = 50  # Typical Z2 endurance work

# Safe load increase limits (injury prevention)
MAX_WEEKLY_LOAD_INCREASE_PCT = 15  # Don't increase > 15% week over week

# Minimum data requirements
MIN_DAYS_FOR_CTL = 14      # Need at least 2 weeks for meaningful CTL
MIN_DAYS_FOR_TRENDS = 28   # Need 4 weeks for trend analysis

# Activity classification thresholds
LONG_EFFORT_MIN_MINS = 60
HARD_HR_AVG_THRESHOLD = 150
HARD_HR_MAX_THRESHOLD = 175

# Compliance thresholds
VOLUME_COMPLIANCE_MIN_PERCENT = 80

# API timeouts (seconds)
HTTP_TIMEOUT_SECONDS = 15
GARMIN_RATE_LIMIT_WAIT_SECS = 10

# Data retention (days)
PROFILE_HISTORY_DAYS = 180
RECENT_ACTIVITY_DAYS = 35  # 5 weeks to view current week within 4-week block context

# Planning windows (days)
RACE_TEMPLATE_WINDOW_DAYS = 56  # 8 weeks - races within this window get template guidance

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

# Strength training progression
PROGRESSION_INCREMENT_KG = 2.5  # Default weight increase when progressing
MIN_SETS_FOR_PROGRESSION = 3    # Minimum sets completed to suggest progression
WEIGHT_GRAM_TO_KG = 1000        # Garmin returns weights in grams

# Default exercise equivalence groups (exercises that share progression)
DEFAULT_EQUIVALENCE_GROUPS = {
    "BENCH_PRESS": ["BARBELL_BENCH_PRESS", "DUMBBELL_BENCH_PRESS", "INCLINE_DUMBBELL_BENCH_PRESS", "DECLINE_BENCH_PRESS", "CLOSE_GRIP_BENCH_PRESS"],
    "ROW": ["SEATED_CABLE_ROW", "BENT_OVER_ROW", "BENT_OVER_ROW_WITH_DUMBELL", "SINGLE_ARM_DUMBBELL_ROW", "T_BAR_ROW"],
    "PULL_UP": ["LAT_PULLDOWN", "PULL_UP", "CHIN_UP", "ASSISTED_PULL_UP", "WIDE_GRIP_LAT_PULLDOWN"],
    "SHOULDER_PRESS": ["DUMBBELL_SHOULDER_PRESS", "BARBELL_SHOULDER_PRESS", "ARNOLD_PRESS", "SEATED_SHOULDER_PRESS"],
    "CURL": ["DUMBBELL_BICEPS_CURL", "BARBELL_BICEPS_CURL", "HAMMER_CURL", "PREACHER_CURL", "CONCENTRATION_CURL"],
    "TRICEPS_EXTENSION": ["DUMBBELL_LYING_TRICEPS_EXTENSION", "CABLE_OVERHEAD_TRICEPS_EXTENSION", "TRICEPS_DIP", "SKULL_CRUSHER", "TRICEPS_PUSHDOWN"],
    "SQUAT": ["BARBELL_SQUAT", "GOBLET_SQUAT", "FRONT_SQUAT", "SPLIT_SQUAT", "BULGARIAN_SPLIT_SQUAT"],
    "DEADLIFT": ["DEADLIFT", "ROMANIAN_DEADLIFT", "SUMO_DEADLIFT", "TRAP_BAR_DEADLIFT"],
    "LATERAL_RAISE": ["LATERAL_RAISE", "CABLE_LATERAL_RAISE", "SEATED_LATERAL_RAISE"],
    "CORE": ["DEAD_BUG", "PLANK", "RUSSIAN_TWIST", "BICYCLE_CRUNCH", "LEG_RAISE", "CYCLING_RUSSIAN_TWIST"],
}

# Activity restrictions by injury type (common patterns)
# Sport group mapping for sport-specific fitness tracking.
# Groups align with race calendar (cycling A-race, running events) and training
# pillars. Derived from the canonical taxonomy (coach/taxonomy.py) so the
# vocabulary cannot drift between modules.
SPORT_GROUPS = {
    'cycling': garmin_types_in_sport_group('cycling'),
    'running': garmin_types_in_sport_group('running'),
    'strength': garmin_types_in_sport_group('strength'),
    'other': [],  # catchall for padel, ultimate_disc, yoga, pilates, swimming, etc.
}


def get_sport_group(activity_type: str) -> str:
    """Return the sport group for an activity type ('cycling', 'running', 'strength', or 'other')."""
    return sport_group_for(activity_type)



# Polarization targets: Norwegian 80/20 model (Tønnessen et al)
POLARIZATION_TARGETS = {
    'low_pct': 80,       # Z1-Z2: easy aerobic
    'moderate_pct': 15,  # Z3: tempo
    'high_pct': 5,       # Z4-Z5: threshold/VO2max
}

# Canonical adaptation-pattern registry (Phase 3.2).
# record_athlete_response() normalizes its `pattern` argument against these
# keys (case/space tolerant + unique substring match) so the counts in
# get_response_patterns() / snapshot adaptation_patterns aggregate by
# canonical key and can actually trigger coaching behavior. Unknown patterns
# are still stored but flagged `unrecognized_pattern` in the tool response.
ADAPTATION_PATTERN_REGISTRY = {
    'handles_volume_well': 'Absorbs week-over-week volume increases without readiness, sleep or compliance decline — supports aggressive load steps.',
    'struggles_with_volume': 'Readiness, sleep or compliance degrade when weekly volume rises — keep load steps conservative.',
    'recovers_quickly': 'Readiness rebounds within a day after hard sessions — shorter gaps between quality sessions are fine.',
    'slow_recovery': 'Needs longer than the usual recovery window after hard sessions — space out intensity.',
    'needs_extra_rest_after_intensity': 'Requires an extra easy/rest day after interval or threshold work.',
    'responds_well_to_intensity': 'Adapts strongly to interval/threshold stimulus — fitness jumps after intensity blocks.',
    'struggles_with_early_sessions': 'Early-morning sessions get skipped or underperformed — schedule key work later in the day.',
    'running_impact_intolerance_during_bone_healing': 'Impact loading provokes symptoms while a bone injury heals — keep impact activities gated until cleared.',
    'sleep_sensitive': 'Performance and readiness drop sharply after short sleep — gate intensity on the sleep_gate signal.',
    'consistent_when_scheduled': 'Completes sessions reliably when they are explicitly scheduled in the plan — vague prescriptions get dropped.',
}

# Race proximity weights for sport priority calculation
# Maps max days_until → weight (closer race = higher weight)
RACE_TIME_WEIGHTS = [
    (14, 4),   # ≤2 weeks: peak/taper
    (28, 3),   # ≤4 weeks: build/peak
    (56, 2),   # ≤8 weeks: build
]
RACE_TIME_WEIGHT_DEFAULT = 1  # >8 weeks: base

# Sleep quality thresholds (Norwegian sports medicine + Garmin sleep science)
SLEEP_DEEP_PCT_MIN = 15            # 7-day avg deep sleep % for adequate recovery
SLEEP_DEEP_PCT_EXCELLENT = 18      # Recent nights deep sleep % for excellent recovery
SLEEP_SCORE_ADEQUATE = 70          # Chronic (7-day) sleep score
SLEEP_SCORE_GOOD = 75              # Recent nights good threshold
SLEEP_SCORE_EXCELLENT = 80         # Recent nights excellent threshold
SLEEP_NAP_EFFECTIVE_MINS = 15      # Minimum nap to count as recovery
SLEEP_VARIANCE_THRESHOLD_HRS = 2   # Flag inconsistent sleep hygiene
SLEEP_TARGET_DEFAULT_HRS = 7.5     # Fallback when athlete hasn't set personal target

# Race type → sport group mapping (used for sport-specific CTL lookups).
# Kept as a module constant for backward compatibility — the canonical map
# lives in coach/taxonomy.py (use taxonomy.race_sport_for for lookups).
RACE_TYPE_SPORT_MAP = dict(RACE_TYPE_TO_SPORT)

# Clinical reference sources
PHYSIOPEDIA_BASE_URL = "https://www.physio-pedia.com"
