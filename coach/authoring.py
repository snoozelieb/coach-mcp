"""The session authoring contract: what a plan session may say about its
structure, and how the renderer reads it.

This module is the ONE vocabulary for the three sport families that carry
structure (running and cycling `structure` phases, strength `exercises`).
It is deliberately strict where the lenient data-file schemas
(``coach/schemas.py``) are not: a typo'd key inside a phase or an exercise is
an error that names the allowed keys, a missing end condition or rep count is
an error, and an intensity word outside the vocabulary is an error. The
renderer (``coach/workout_builder.py``) never defaults a value the plan did
not state — the whole point of this contract is that what the plan says is
what the watch shows.

Judgment (how a Wattbike threshold session should be composed, which Garmin
key to pick for a side lunge) does not live here; it lives in the sport
skills and the coaching doctrine. This module holds invariants only:
vocabulary, validation with teaching errors, and the contract document.

Entry points:
  validate_session(session, catalogue=None) -> AuthoringReport
  contract_document(sport=None) -> str   (markdown + JSON schema)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
import re
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    BaseModel, ConfigDict, Discriminator, Field, Tag, ValidationError,
    field_validator, model_validator,
)

from .taxonomy import workout_family_for

# ---------------------------------------------------------------------------
# Intensity vocabulary — ONE table for every sport
# ---------------------------------------------------------------------------

# Canonical word -> (HR zone key, power zone key, running pace zone key).
INTENSITY_ZONES: dict[str, tuple[str, str, str]] = {
    'recovery':   ('z1_recovery',  'z1_recovery',  'z1_recovery'),
    'easy':       ('z2_aerobic',   'z2_endurance', 'z2_easy'),
    'tempo':      ('z3_tempo',     'z3_tempo',     'z3_tempo'),
    'sweet_spot': ('z3_tempo',     'z3_tempo',     'z3_tempo'),
    'threshold':  ('z4_threshold', 'z4_threshold', 'z4_threshold'),
    'vo2':        ('z5_max',       'z5_vo2max',    'z5_interval'),
    'max_effort': ('z5_max',       'z6_anaerobic', 'z5_interval'),
}

# Accepted spellings -> canonical word. Everything else is rejected inside a
# phase (teaching error) and warned about at session level.
INTENSITY_ALIASES: dict[str, str] = {
    'z1': 'recovery', 'zone1': 'recovery', 'active_recovery': 'recovery',
    'z2': 'easy', 'zone2': 'easy', 'endurance': 'easy', 'aerobic': 'easy',
    'z3': 'tempo', 'zone3': 'tempo', 'sweetspot': 'sweet_spot', 'sweet spot': 'sweet_spot',
    'z4': 'threshold', 'zone4': 'threshold', 'hard': 'threshold', 'lactate_threshold': 'threshold',
    'z5': 'vo2', 'zone5': 'vo2', 'vo2max': 'vo2', 'vo2_max': 'vo2',
    'z6': 'max_effort', 'anaerobic': 'max_effort', 'sprint': 'max_effort', 'all_out': 'max_effort',
    'max': 'max_effort',
}

INTENSITY_WORDS = tuple(INTENSITY_ZONES)

# Target shape. A target is an ALERT: the watch sounds whenever the athlete is
# outside it, in both directions. easy and recovery are CEILINGS — the alert
# fires only above the zone top; the low bound is the athlete's aerobic floor
# (below it the athlete has stopped, not eased off). The other words are BANDS
# the athlete must hit. Power targets are always bands (no lag).
CEILING_INTENSITIES = frozenset({'recovery', 'easy'})
# Aerobic floor: resting HR + this share of heart-rate reserve (Karvonen).
# 30 % of HRR is the ACSM boundary between very light and light aerobic
# activity (Garber et al. 2011): a brisk walk sits above it, standing still
# below it. personal.aerobic_floor_pct_hrr on the profile overrides the share.
AEROBIC_FLOOR_PCT_HRR = 30
AEROBIC_FLOOR_FALLBACK_BPM = 30  # no resting_hr / max_hr on the profile: the contract's plausibility floor
PACE_FLOOR_FACTOR = 2.0  # a pace ceiling's slow bound: twice the threshold pace is a walk
HR_LAG_MIN_STEP_SECS = 180  # an HR band on a shorter timed step reads 'too low' for most of it

# Strength: the spot in the gym a movement is done at ('incline bench',
# 'cable', 'mat'). The author states it — Garmin's catalogue carries no
# equipment — and the preview prints the walk through the stations and
# warns when a session returns to one it left. methodology.strength
# .station_order sets whether that is a warning, an error or unchecked.
STATION_MAX_CHARS = 30
STATION_ORDER_LEVELS = ('off', 'warn', 'error')
STATION_ORDER_DEFAULT = 'warn'
# Equipment words a Garmin key may spell — words of the key, not Garmin
# data: LAT_PULLDOWN spells none, DUMBBELL and BAND travel, BENCH_DIP's
# bench is no press bench. A hint for the station label, never a decision.
STATION_KEY_WORDS = ('BARBELL', 'DUMBBELL', 'KETTLEBELL', 'CABLE', 'BAND', 'BANDED', 'MACHINE', 'SMITH',
                     'BENCH', 'INCLINE', 'DECLINE', 'SWISS_BALL', 'MEDICINE_BALL', 'BOSU', 'HANGING',
                     'RING', 'SUSPENSION', 'TRX', 'BOX', 'WALL', 'EZ_BAR', 'TRAP_BAR', 'LANDMINE', 'SLED',
                     'ROPE', 'PLATE', 'FOAM_ROLLER', 'WHEEL', 'PULL_UP', 'BAR')


def normalise_intensity(value: Any) -> str | None:
    """Canonical intensity word, or None when the word is not in the vocabulary."""
    if value is None:
        return None
    word = str(value).strip().lower().replace('-', '_')
    if word in INTENSITY_ZONES:
        return word
    word_us = word.replace(' ', '_')
    if word_us in INTENSITY_ZONES:
        return word_us
    return INTENSITY_ALIASES.get(word) or INTENSITY_ALIASES.get(word_us)


# ---------------------------------------------------------------------------
# Garmin strength categories (the workout API's accepted set — verified by a
# live upload) and the four exercise-catalogue categories it rejects.
# ---------------------------------------------------------------------------

VALID_WORKOUT_CATEGORIES = frozenset({
    "BANDED_EXERCISES", "BATTLE_ROPE", "BENCH_PRESS", "CALF_RAISE", "CARDIO",
    "CARRY", "CHOP", "CORE", "CRUNCH", "CURL", "DEADLIFT", "FLYE",
    "HIP_RAISE", "HIP_STABILITY", "HIP_SWING", "HYPEREXTENSION",
    "LATERAL_RAISE", "LEG_CURL", "LEG_RAISE", "LUNGE",
    "OLYMPIC_LIFT", "PLANK", "PLYO", "PULL_UP", "PUSH_UP", "ROW",
    "SHOULDER_PRESS", "SHOULDER_STABILITY", "SHRUG", "SIT_UP",
    "SQUAT", "SUSPENSION", "TOTAL_BODY", "TRICEPS_EXTENSION", "WARM_UP",
})

# Catalogue categories Garmin's workout API does not accept -> the category
# it does. This is a Garmin quirk (data), not coaching judgment: the
# exercise catalogue lists e.g. LYING_LEG_CURL under HAMSTRING_CURL, the
# workout API only knows LEG_CURL.
CATALOGUE_CATEGORY_TO_WORKOUT: dict[str, str] = {
    "HAMSTRING_CURL": "LEG_CURL",
    "LAT_PULLDOWN": "PULL_UP",
    "SHOULDER_RAISE": "LATERAL_RAISE",
    "CUSTOM": "",          # custom entries carry no workout category
    # cardio-equipment catalogue categories
    "BIKE_OUTDOOR": "CARDIO", "INDOOR_BIKE": "CARDIO", "ELLIPTICAL": "CARDIO",
    "FLOOR_CLIMB": "CARDIO", "RUN": "CARDIO", "RUN_INDOOR": "CARDIO",
    "STAIR_STEPPER": "CARDIO", "LADDER": "CARDIO",
    "SANDBAG": "DEADLIFT", "SLED": "SQUAT", "SLEDGE_HAMMER": "CORE", "TIRE": "DEADLIFT",
    "OTHER": "",
}

# Garmin files 26 of its 1,495 exercise names under more than one category
# (SIDE_PLANK under PLANK and SUSPENSION; ROW under five). The cached
# catalogue (scripts/fetch_exercises.py) keeps every category in
# `categories` and the primary one in `category`. When the plan states no
# category the primary is sent; a stated category keeps the exercise name
# whenever it is one of the listed ones. Equipment categories Garmin
# cross-files bodyweight movements under never take the primary slot while
# a movement category is listed (a one-category cache that called SIDE_PLANK
# a suspension exercise is how "Weighted Side Plank" reached the watch for a
# bodyweight hold, 2026-10).
CROSS_FILED_CATEGORIES: frozenset[str] = frozenset({
    "BANDED_EXERCISES", "SUSPENSION", "SANDBAG", "SLED", "TIRE", "BATTLE_ROPE",
})


def primary_catalogue_category(name: str, categories) -> str:
    """The category sent for a catalogue name the plan states none for.

    Data rules, not judgment: one listed category wins outright; then a
    category whose tokens appear in the name (PLANK for SIDE_PLANK, ROW for
    ROW); then the first workout-API category that is not an equipment
    cross-filing; then the first workout-API category; then the first listed.
    """
    cats: list[str] = []
    for raw in categories or []:
        cat = normalise_category(raw)
        if cat and cat not in cats:
            cats.append(cat)
    if not cats:
        return ''
    if len(cats) == 1:
        return cats[0]
    name_tokens = [t for t in normalise_exercise_name(name).split('_') if t]
    for cat in cats:
        parts = cat.split('_')
        if any(name_tokens[i:i + len(parts)] == parts for i in range(len(name_tokens) - len(parts) + 1)):
            return cat
    for cat in cats:
        if cat in VALID_WORKOUT_CATEGORIES and cat not in CROSS_FILED_CATEGORIES:
            return cat
    for cat in cats:
        if cat in VALID_WORKOUT_CATEGORIES:
            return cat
    return cats[0]


# A hold is timed. Garmin's catalogue carries no "static" flag, so the
# contract reads the name: a name ending in PLANK, HOLD, HANG or CARRY, or
# carrying L_SIT / WALL_SIT, is a hold and takes duration_secs, not a rep
# count - unless the name says the movement moves (PLANK_JACKS,
# ROLLING_SIDE_PLANK, SWISS_BALL_PLANK_LEG_LIFT_AND_HOLD). Calibrated on
# Garmin's 43 *_PLANK keys (2026-10): the static planks are flagged, the
# inching, rolling, plyometric, kicking and "to side plank" variants stay
# on reps. reps "open" (lap button) is allowed: "hold as long as you can".
HOLD_FINAL_TOKENS: frozenset[str] = frozenset({"PLANK", "HOLD", "HANG", "CARRY"})
HOLD_TOKEN_PAIRS: frozenset[tuple[str, str]] = frozenset({("L", "SIT"), ("WALL", "SIT")})
DYNAMIC_TOKENS: frozenset[str] = frozenset({
    "JUMP", "JUMPS", "JUMPING", "JACK", "JACKS", "CLIMBER", "CLIMBERS", "CRAWL", "CRAWLING",
    "TWIST", "TWISTS", "ROLL", "ROLLS", "ROLLING", "ROCK", "ROCKING", "WALK", "WALKS", "WALKING",
    "WALKOUT", "WALKOUTS", "INCHING", "INCHWORM", "PLYO", "PLYOMETRIC", "SPIDERMAN", "DYNAMIC",
    "TO", "AND", "WITH", "KICK", "KICKS", "TAP", "TAPS", "TOUCH", "TOUCHES", "REACH", "REACHES",
    "CRUNCH", "CRUNCHES", "PIKE", "PIKES", "TUCK", "TUCKS", "LIFT", "LIFTS", "RAISE", "RAISES",
    "ROTATION", "ROTATIONS", "DRAG", "DRAGS", "SLIDE", "SLIDES", "DIP", "DIPS", "UP", "UPS",
    "DOWN", "DOWNS", "PUSH", "PULL", "ROW", "ROWS", "PRESS", "SHIFT", "SHIFTS", "STEP", "STEPS",
})
# Trailing qualifiers a coach appends ("Side plank left", "Plank 30s") are
# stripped before the rule reads the last token.
_HOLD_QUALIFIER = re.compile(r"^(LEFT|RIGHT|L|R|\d+(S|SEC|SECS|SECOND|SECONDS)?)$")


def is_timed_hold(name: str) -> bool:
    """True when the Garmin key (or off-catalogue name) names a static hold."""
    tokens = [t for t in normalise_exercise_name(name).split('_') if t]
    while len(tokens) > 1 and _HOLD_QUALIFIER.match(tokens[-1]):
        tokens.pop()
    if not tokens:
        return False
    if any((tokens[i], tokens[i + 1]) in HOLD_TOKEN_PAIRS for i in range(len(tokens) - 1)):
        return True
    last = tokens[-1]
    if last not in HOLD_FINAL_TOKENS:
        return False
    if last == "HOLD":
        # "..._WITH_STATIC_HOLD" / "..._LIFT_AND_HOLD" are reps with a pause
        return not ({"WITH", "AND"} & set(tokens))
    if last == "PLANK":
        return not (DYNAMIC_TOKENS & set(tokens[:-1]))
    return True

# Injury restrictions are free text ("no jumping", "overhead pressing", "deep
# squats"). The injury gate matches them against strength exercises by
# movement word: every content word of the restriction must appear, stemmed,
# among the tokens of the exercise's Garmin key or category. Negations and
# qualifiers are dropped on purpose ("no heavy squats" gates every squat —
# the gate fails safe and an override needs the athlete's words). Sport words
# (running, cycling) are judged on the session type, never on exercise names.
RESTRICTION_STOP_WORDS: frozenset[str] = frozenset({
    "no", "not", "never", "avoid", "any", "all", "of", "the", "a", "an", "and", "or",
    "with", "without", "work", "exercise", "exercises", "movement", "movements",
    "activity", "activities", "training", "for", "now", "until", "heavy", "light",
    "deep", "high", "low", "fast", "slow", "explosive", "loaded", "weighted", "max",
    "maximal", "full", "range", "long", "hard", "any", "type", "kind",
})

# A restriction word Garmin spells differently: the stemmed word -> token
# sequences that count as a hit in a key or category (contiguous tokens).
RESTRICTION_SYNONYMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "plyometric": (("plyo",), ("jump",), ("hop",)),
    "plyo": (("plyo",), ("jump",), ("hop",)),
    "jump": (("jump",), ("plyo",), ("hop",)),
    "hop": (("hop",), ("jump",), ("plyo",)),
    "impact": (("jump",), ("plyo",), ("hop",), ("run",)),
    "overhead": (("overhead",), ("shoulder", "press")),
}


def stem_word(word: str) -> str:
    """jumping -> jump, hopping -> hop, lunges -> lunge, crunches -> crunch,
    kneeling -> kneel (so a 'knee' restriction does not hit it)."""
    w = str(word or "").lower()
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
        if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "sflz":
            w = w[:-1]
    elif w.endswith(("ches", "shes", "sses", "xes")) and len(w) > 5:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    return w


def restriction_terms(restriction: str) -> list[str]:
    """The content words of a free-text restriction, stemmed."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(restriction or "").lower()) if t]
    terms = [stem_word(t) for t in tokens if t not in RESTRICTION_STOP_WORDS]
    return [t for t in terms if len(t) >= 3]


def movement_tokens(value) -> list[str]:
    """Stemmed tokens of a Garmin key or category: BOX_JUMP -> [box, jump]."""
    return [stem_word(t) for t in re.split(r"[^a-z0-9]+", str(value or "").lower()) if t]


def _term_in_tokens(term: str, tokens: list[str]) -> bool:
    if term in tokens:
        return True
    for seq in RESTRICTION_SYNONYMS.get(term, ()):
        n = len(seq)
        if any(tuple(tokens[i:i + n]) == seq for i in range(len(tokens) - n + 1)):
            return True
    return False


def exercise_matches_restriction(name, category, restriction: str) -> bool:
    """True when every content word of the restriction is found in the
    exercise's key or category tokens (BOX_JUMP / PLYO for 'no jumping';
    SHOULDER_PRESS and OVERHEAD_PRESS, not LEG_PRESS, for 'overhead pressing')."""
    terms = restriction_terms(restriction)
    if not terms:
        return False
    name_tokens, category_tokens = movement_tokens(name), movement_tokens(category)
    return all(_term_in_tokens(t, name_tokens) or _term_in_tokens(t, category_tokens) for t in terms)

# Muscle-group words coaches write instead of a Garmin category. They are
# NOT silently mapped (that is judgment); the error suggests them.
CATEGORY_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "GLUTE": ("SQUAT", "HIP_RAISE", "LUNGE"), "GLUTES": ("SQUAT", "HIP_RAISE", "LUNGE"),
    "HIP": ("HIP_STABILITY", "HIP_RAISE", "HIP_SWING"), "HIPS": ("HIP_STABILITY", "HIP_RAISE"),
    "CALF": ("CALF_RAISE",), "CALVES": ("CALF_RAISE",),
    "HAMSTRING": ("LEG_CURL", "DEADLIFT"), "HAMSTRINGS": ("LEG_CURL", "DEADLIFT"),
    "CHEST": ("BENCH_PRESS", "PUSH_UP", "FLYE"),
    "BACK": ("ROW", "PULL_UP", "HYPEREXTENSION"),
    "BICEPS": ("CURL",), "TRICEPS": ("TRICEPS_EXTENSION",),
    "SHOULDERS": ("SHOULDER_PRESS", "LATERAL_RAISE", "SHOULDER_STABILITY"),
    "SHOULDER": ("SHOULDER_PRESS", "LATERAL_RAISE", "SHOULDER_STABILITY"),
    "ABS": ("CORE", "CRUNCH", "PLANK", "SIT_UP"), "CORE_STABILITY": ("CORE", "PLANK"),
    "LEGS": ("SQUAT", "LUNGE", "DEADLIFT"), "QUADS": ("SQUAT", "LUNGE"),
    "BAND": ("BANDED_EXERCISES",), "BANDS": ("BANDED_EXERCISES",), "BANDED": ("BANDED_EXERCISES",),
    "MOBILITY": ("WARM_UP", "HIP_STABILITY", "SHOULDER_STABILITY"),
    "BALANCE": ("HIP_STABILITY", "SQUAT"),
}


def normalise_exercise_name(value: Any) -> str:
    """Garmin exercise keys are UPPER_SNAKE_CASE: 'Bulgarian split squat' ->
    'BULGARIAN_SPLIT_SQUAT'. Catalogue lookups and uploads use this form."""
    text = str(value or '').strip().upper()
    for ch in (' ', '-', '/'):
        text = text.replace(ch, '_')
    while '__' in text:
        text = text.replace('__', '_')
    text = text.strip('_')
    # Garmin prefixes keys that would start with a digit: _45_DEGREE_PLANK
    return f"_{text}" if text[:1].isdigit() else text


def normalise_category(value: Any) -> str:
    return normalise_exercise_name(value)


# Garmin shows roughly 50 characters of a step description on the watch
# (our cap; Garmin's own limit is unverified) and 40 of a workout name.
def normalise_station(value: Any) -> str | None:
    """Casefold, collapse whitespace, strip; '' -> None. No synonym map: the
    author reuses one label per spot, so 'bench' and 'incline bench' are two
    spots until the plan says otherwise."""
    if value is None:
        return None
    text = re.sub(r'\s+', ' ', str(value)).strip().casefold()
    return text or None


def _validate_station(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("station must be a short text label of the spot ('incline bench', 'cable', "
                         f"'mat'), got {value!r}")
    text = normalise_station(value)
    if text is None:
        return None
    if len(text) > STATION_MAX_CHARS:
        raise ValueError(f"station {text!r} is {len(text)} characters; keep it a short label of the "
                         f"spot (up to {STATION_MAX_CHARS}), e.g. 'incline bench'")
    return text


def key_words(name: str) -> list[str]:
    """The equipment words a Garmin key spells, in key order (STATION_KEY_WORDS):
    facts about the spelling of the key, not about the gym."""
    key = normalise_exercise_name(name)
    if not key:
        return []
    tokens = key.split('_')
    padded = f'_{key}_'
    found = [w for w in STATION_KEY_WORDS
             if (f'_{w}_' in padded if '_' in w else w in tokens)]
    return sorted(found, key=lambda w: padded.find(f'_{w}_'))


STEP_NOTE_MAX_CHARS = 50
WORKOUT_NAME_MAX_CHARS = 40


# ---------------------------------------------------------------------------
# Strict models
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    """Authoring input: unknown keys are errors that name the allowed keys."""
    model_config = ConfigDict(extra="forbid")


OpenOrNumber = Union[float, Literal["open"]]


def _is_open(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "open"


class Phase(StrictModel):
    """One timed, distance or lap-button step of a running or cycling session.

    End condition: exactly one of duration_secs, duration_mins, distance_m —
    a number, or the string "open" for a lap-button step.
    Target: at most one of hr_target, hr_max, pace_sec_per_km, pace,
    power_watts, power_pct, cadence (Garmin shows one target per step);
    intensity may accompany it as a label and is the target when no numeric
    one is given. A target is an alert in both directions: hr_target is a
    band, hr_max a ceiling above the athlete's aerobic floor.
    """
    phase: str = Field(description="warmup | interval | recovery | cooldown | rest")
    duration_secs: OpenOrNumber | None = Field(default=None, description="seconds, or \"open\"")
    duration_mins: OpenOrNumber | None = Field(default=None, description="minutes (fractions allowed), or \"open\"")
    distance_m: OpenOrNumber | None = Field(default=None, description="metres, or \"open\"")
    intensity: str | None = Field(default=None, description="recovery | easy | tempo | sweet_spot | threshold | vo2 | max_effort (z1-z6 accepted)")
    hr_target: list[float] | None = Field(default=None, description="[low_bpm, high_bpm] — a band the athlete must hit")
    hr_max: float | None = Field(default=None, description="ceiling in bpm: the watch alerts only above it (low bound = the athlete's aerobic floor)")
    pace_sec_per_km: list[float] | None = Field(default=None, description="[slow_sec_per_km, fast_sec_per_km], e.g. [330, 315] for 5:30-5:15/km")
    pace: list[float] | None = Field(default=None, description="[slow_mps, fast_mps] — prefer pace_sec_per_km")
    power_watts: list[float] | None = Field(default=None, description="[low_w, high_w] (cycling)")
    power_pct: float | list[float] | None = Field(default=None, description="% of FTP: [low, high], or one number for a ±5 % band (cycling)")
    cadence: list[float] | None = Field(default=None, description="[low, high] rpm (cycling) or spm (running)")
    notes: str | None = Field(default=None, description=f"cue shown on the step; the watch displays about {STEP_NOTE_MAX_CHARS} characters")

    PHASE_WORDS: ClassVar[tuple[str, ...]] = ('warmup', 'interval', 'recovery', 'cooldown', 'rest')
    PHASE_ALIASES: ClassVar[dict[str, str]] = {
        'warm_up': 'warmup', 'warm-up': 'warmup', 'wu': 'warmup',
        'cool_down': 'cooldown', 'cool-down': 'cooldown', 'cd': 'cooldown',
        'work': 'interval', 'main': 'interval', 'effort': 'interval', 'on': 'interval',
        'walk': 'recovery', 'jog': 'recovery', 'off': 'recovery', 'easy': 'recovery',
        'active_recovery': 'recovery', 'float': 'recovery',
    }
    # One numeric target per step (Garmin shows one); `intensity` may ride
    # along as a label and is the target only when no numeric one is given.
    NUMERIC_TARGET_KEYS: ClassVar[tuple[str, ...]] = ('hr_target', 'hr_max', 'pace_sec_per_km', 'pace',
                                                      'power_watts', 'power_pct', 'cadence')
    TARGET_KEYS: ClassVar[tuple[str, ...]] = NUMERIC_TARGET_KEYS + ('intensity',)
    END_KEYS: ClassVar[tuple[str, ...]] = ('duration_secs', 'duration_mins', 'distance_m')

    @field_validator('phase', mode='before')
    @classmethod
    def _phase_word(cls, v):
        word = str(v or '').strip().lower().replace(' ', '_')
        word = cls.PHASE_ALIASES.get(word, word)
        if word not in cls.PHASE_WORDS:
            raise ValueError(
                f"phase {v!r} is not one of {', '.join(cls.PHASE_WORDS)} "
                "(a repeat block is {'phase': 'repeat', 'iterations': N, 'steps': [...]})")
        return word

    @field_validator('intensity', mode='before')
    @classmethod
    def _intensity_word(cls, v):
        if v is None:
            return None
        if str(v).strip().lower() == 'none':
            return 'none'  # explicitly untargeted, even when the session has an intensity
        word = normalise_intensity(v)
        if word is None:
            raise ValueError(
                f"intensity {v!r} is not in the vocabulary {', '.join(INTENSITY_WORDS)} "
                "(z1-z6 are accepted as aliases; 'none' = no target on this step)")
        return word

    @field_validator('duration_secs', 'duration_mins', 'distance_m', mode='before')
    @classmethod
    def _open_or_positive(cls, v, info):
        if v is None:
            return None
        if _is_open(v):
            return "open"
        try:
            number = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{info.field_name} must be a number or \"open\", got {v!r}")
        if number <= 0:
            raise ValueError(f"{info.field_name} must be greater than 0, got {v!r}")
        return number

    @field_validator('hr_target', 'pace_sec_per_km', 'pace', 'power_watts', 'cadence', mode='after')
    @classmethod
    def _pair(cls, v, info):
        if v is None:
            return None
        if len(v) != 2:
            raise ValueError(f"{info.field_name} must be [low, high], got {v!r}")
        return v

    @field_validator('power_pct', mode='after')
    @classmethod
    def _pct(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            if len(v) != 2:
                raise ValueError(f"power_pct must be [low_pct, high_pct] or one number, got {v!r}")
            return v
        return v

    RANGES: ClassVar[dict[str, tuple[float, float, str]]] = {
        'hr_target': (30, 230, 'bpm'),
        'hr_max': (30, 230, 'bpm'),
        'pace_sec_per_km': (120, 1200, 'sec/km (2:00-20:00 per km)'),
        'pace': (0.5, 10, 'm/s (prefer pace_sec_per_km)'),
        'power_watts': (0, 2000, 'W'),
        'power_pct': (20, 200, '% of FTP (a fraction such as 1.0 is not a percentage)'),
        'cadence': (20, 250, 'rpm/spm'),
    }

    @model_validator(mode='after')
    def _plausible(self):
        for key, (lo, hi, unit) in self.RANGES.items():
            value = getattr(self, key)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            if any(v < lo or v > hi for v in values):
                raise ValueError(f"{key} values must be {lo:g}-{hi:g} {unit}, got {value!r}")
        if self.distance_m is not None and not _is_open(self.distance_m) and float(self.distance_m) < 20:
            raise ValueError(f"distance_m must be at least 20 m, got {self.distance_m!r}")
        if self.power_watts is not None and max(self.power_watts) < 30:
            raise ValueError(f"power_watts are watts: a band topping out under 30 W reads like W/kg "
                             f"or a fraction, got {self.power_watts!r}")
        return self

    @model_validator(mode='after')
    def _one_end_one_target(self):
        ends = [k for k in self.END_KEYS if getattr(self, k) is not None]
        if len(ends) != 1:
            raise ValueError(
                "a phase needs exactly one end condition: duration_secs, duration_mins "
                "or distance_m (a number, or \"open\" for a lap-button step); "
                f"got {ends or 'none'}")
        targets = [k for k in self.NUMERIC_TARGET_KEYS if getattr(self, k) is not None]
        if len(targets) > 1:
            raise ValueError(
                f"a phase takes ONE target (Garmin shows one per step); got {targets}. "
                "Put the secondary cue in notes.")
        return self

    # -- helpers used by the renderer -------------------------------------
    def end_condition(self) -> tuple[str, float | None]:
        """('open', None) | ('time', secs) | ('distance', metres)."""
        if self.distance_m is not None:
            return ('open', None) if _is_open(self.distance_m) else ('distance', float(self.distance_m))
        if self.duration_secs is not None:
            return ('open', None) if _is_open(self.duration_secs) else ('time', float(self.duration_secs))
        if _is_open(self.duration_mins):
            return ('open', None)
        return ('time', round(float(self.duration_mins) * 60))

    def target_key(self) -> str | None:
        """The key that becomes the Garmin target: the numeric one, else intensity."""
        for key in self.NUMERIC_TARGET_KEYS:
            if getattr(self, key) is not None:
                return key
        return 'intensity' if self.intensity is not None else None


class RepeatBlock(StrictModel):
    """`{'phase': 'repeat', 'iterations': N, 'steps': [...]}` — steps may nest."""
    phase: Literal['repeat']
    iterations: int = Field(ge=1, description="how many times the steps run")
    steps: list["StructureItem"] = Field(min_length=1)
    notes: str | None = None


def _structure_discriminator(value: Any) -> str:
    if isinstance(value, dict):
        word = str(value.get('phase', '') or '').strip().lower()
        if word == 'repeat' or (not word and ('iterations' in value or 'steps' in value)):
            return 'repeat'
        return 'phase'
    return 'repeat' if isinstance(value, RepeatBlock) else 'phase'


StructureItem = Annotated[
    Union[Annotated[Phase, Tag('phase')], Annotated[RepeatBlock, Tag('repeat')]],
    Discriminator(_structure_discriminator),
]
RepeatBlock.model_rebuild()


class Structure(BaseModel):
    """The `structure` list of a running or cycling session."""
    model_config = ConfigDict(extra="forbid")
    items: list[StructureItem] = Field(min_length=1)


class CircuitExercise(StrictModel):
    """One movement. Garmin needs a key from its exercise catalogue to show the
    name and the animation; an off-catalogue name still renders, with the
    readable name in the step description."""
    name: str = Field(min_length=1, description="Garmin exercise key, e.g. GOBLET_SQUAT (spaces are normalised)")
    category: str | None = Field(default=None, description="Garmin workout category; required when the name is not in the catalogue")
    reps: int | Literal["open"] | None = Field(default=None, description="reps per set, or \"open\" to end the set with the lap button; a hold (PLANK, HOLD, HANG, CARRY, L_SIT, WALL_SIT names) is rejected with a rep count")
    duration_secs: float | None = Field(default=None, ge=1, description="hold/carry time per set in seconds — timed sets instead of reps (required for a hold)")
    each_side: bool = Field(default=False, description="reps/time are per side: the watch gets a Left step and a Right step per set (or per round), each with the stated reps or time")
    weight_kg: float | None = Field(default=None, gt=0, description="load; only an explicit value is sent")
    notes: str | None = Field(default=None, description=f"form cue shown on the step (about {STEP_NOTE_MAX_CHARS} characters visible)")
    station: str | None = Field(default=None, description=(
        "the spot in the gym where this movement is done ('incline bench', 'cable', 'mat'; up to "
        f"{STATION_MAX_CHARS} characters, one label per spot reused verbatim). Not sent to the watch: the "
        "preview prints the station walk and warns when the session returns to a station it left"))

    @field_validator('station', mode='before')
    @classmethod
    def _station(cls, v):
        return _validate_station(v)

    @field_validator('name', mode='before')
    @classmethod
    def _name(cls, v):
        name = normalise_exercise_name(v)
        if not name:
            raise ValueError("name is required (a Garmin exercise key such as GOBLET_SQUAT)")
        return name

    @field_validator('category', mode='before')
    @classmethod
    def _category(cls, v):
        if v is None:
            return None
        cat = normalise_category(v)
        if cat in VALID_WORKOUT_CATEGORIES:
            return cat
        hint = CATEGORY_SUGGESTIONS.get(cat)
        if hint:
            raise ValueError(
                f"category {v!r} is a muscle group, not a Garmin category — "
                f"did you mean {' or '.join(hint)}? Allowed: {', '.join(sorted(VALID_WORKOUT_CATEGORIES))}")
        raise ValueError(
            f"category {v!r} is not a Garmin workout category. Allowed: "
            f"{', '.join(sorted(VALID_WORKOUT_CATEGORIES))}")

    @field_validator('reps', mode='before')
    @classmethod
    def _reps(cls, v):
        if v is None:
            return None
        if _is_open(v):
            return "open"
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"reps must be a whole number or \"open\", got {v!r} "
                             "(a range such as '8-10' goes in notes)")
        if n < 1:
            raise ValueError(f"reps must be at least 1, got {v!r}")
        return n

    @model_validator(mode='after')
    def _reps_or_time(self):
        if (self.reps is None) == (self.duration_secs is None):
            raise ValueError(
                f"exercise {self.name}: give exactly one of reps (per set) or "
                "duration_secs (timed hold/carry per set); neither is defaulted")
        if isinstance(self.reps, int) and is_timed_hold(self.name):
            raise ValueError(
                f"exercise {self.name}: a hold is timed — give duration_secs (seconds per set) "
                f"instead of reps {self.reps}; reps \"open\" ends the hold with the lap button")
        return self


class Exercise(CircuitExercise):
    sets: int = Field(ge=1, description="number of sets (required — never defaulted)")
    rest_secs: float | None = Field(default=None, ge=0, description="rest between sets in seconds; a timed rest step when given, a lap-button rest otherwise")


class Circuit(StrictModel):
    """`{'rounds': N, 'exercises': [...]}` — the exercises run back to back,
    N times (supersets, Tabata rounds, rehab circuits)."""
    rounds: int = Field(ge=1)
    exercises: list[CircuitExercise] = Field(min_length=1)
    rest_secs: float | None = Field(default=None, ge=0, description="rest after each round, in seconds")
    notes: str | None = None
    name: str | None = Field(default=None, description="label for the preview only")
    station: str | None = Field(default=None, description=(
        "the spot for every exercise of the circuit that states none ('mat'); a circuit whose "
        "members state different stations walks between them every round, and the preview says so"))

    @field_validator('station', mode='before')
    @classmethod
    def _station(cls, v):
        return _validate_station(v)


def _exercise_discriminator(value: Any) -> str:
    if isinstance(value, dict):
        return 'circuit' if ('rounds' in value or 'exercises' in value) else 'exercise'
    return 'circuit' if isinstance(value, Circuit) else 'exercise'


ExerciseItem = Annotated[
    Union[Annotated[Exercise, Tag('exercise')], Annotated[Circuit, Tag('circuit')]],
    Discriminator(_exercise_discriminator),
]


class ExerciseList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ExerciseItem] = Field(min_length=1)


class SessionFields(StrictModel):
    """Session-level keys the renderer reads (validated in isolation so the
    lenient Session schema stays lenient).

    Strength: warmup_mins renders a WARM_UP block before the first exercise;
    none is inserted otherwise. Plain runs and rides (no structure): when
    stated, warmup_mins / cooldown_mins replace the documented default split
    (runs: 10 % each up to 5 min; indoor rides: 10 % up to 10 min / 5 % up to
    5 min; plain outdoor rides: one block).
    """
    warmup_mins: float | None = Field(default=None, gt=0, description="explicit warm-up block in minutes")
    cooldown_mins: float | None = Field(default=None, gt=0, description="explicit cool-down block in minutes (plain runs and rides)")


StrengthSessionFields = SessionFields  # backward-compatible name

# Categories where a set normally carries a load; used only to WARN when the
# plan states none (never to pick one).
# Warm-up / cool-down a PLAIN session gets when the plan states no
# warmup_mins / cooldown_mins: (fraction of the session, cap in minutes).
# The renderer applies these; the contract document prints them.
PLAIN_SPLIT: dict[str, dict[str, tuple[float, int]]] = {
    "running": {"warmup": (0.10, 5), "cooldown": (0.10, 5)},
    "indoor_cycling": {"warmup": (0.10, 10), "cooldown": (0.05, 5)},
}


def targets_are_alerts_text() -> str:
    """How a target behaves on the wrist. The contract document and the
    doctrine resource both print this, so the rule has one home."""
    return (
        "A target is an alert: the watch sounds whenever the athlete is outside it, in both "
        "directions. easy and recovery render as CEILINGS. The alert fires only above the zone "
        "top. The low bound is the athlete's aerobic floor: resting HR plus "
        f"{AEROBIC_FLOOR_PCT_HRR} % of heart-rate reserve (max HR minus resting HR), the ACSM "
        "boundary of light aerobic activity; a brisk walk stays above it, standing still drops "
        "below it. personal.aerobic_floor_pct_hrr on the profile changes the share. Without "
        f"resting_hr and max_hr on the profile the low bound is {AEROBIC_FLOOR_FALLBACK_BPM} bpm "
        f"and the preview warns. A pace ceiling's slow bound is {PACE_FLOOR_FACTOR:g} x the "
        "threshold pace (a walk). tempo, sweet_spot, threshold, vo2 and max_effort render as the "
        "full zone BAND, which the athlete must hit; hr_target is a band; power targets are always "
        "bands. hr_max [bpm] is an explicit ceiling. Only an interval phase with no target "
        "inherits the session intensity: warm-up, cool-down, recovery and rest phases render "
        "untargeted unless they state one, and so do the warm-up and cool-down of a plain run or "
        f"indoor ride. HR lags 1-2 minutes: an HR band on a timed step under "
        f"{HR_LAG_MIN_STEP_SECS // 60} minutes is a warning, because the watch reads 'too low' "
        "for most of the step. Use pace, a cue in notes, intensity 'none' or hr_max there. "
        "These are defaults: the methodology 'targets' block (update_methodology) overrides the "
        "floor share, the lag threshold, the pace factor and the ceiling words for this athlete, "
        "and personal.aerobic_floor_pct_hrr wins for the share. get_athlete().derived shows the "
        "floor next to the median HR of the athlete's recorded walks.")


def plain_split_text() -> str:
    r, c = PLAIN_SPLIT["running"], PLAIN_SPLIT["indoor_cycling"]
    return (f"a plain run gets a warm-up and cool-down of {r['warmup'][0]:.0%} each "
            f"(up to {r['warmup'][1]} min); a plain indoor ride {c['warmup'][0]:.0%} warm-up "
            f"(up to {c['warmup'][1]} min) and {c['cooldown'][0]:.0%} cool-down "
            f"(up to {c['cooldown'][1]} min)")


LOADED_CATEGORIES = frozenset({
    "SQUAT", "DEADLIFT", "BENCH_PRESS", "ROW", "SHOULDER_PRESS", "OLYMPIC_LIFT",
    "CURL", "TRICEPS_EXTENSION", "LUNGE", "SHRUG", "CARRY", "FLYE", "LATERAL_RAISE",
})
# Compound lifts a coach would not start cold; the plan must say how to warm up.
COMPOUND_CATEGORIES = frozenset({"SQUAT", "DEADLIFT", "BENCH_PRESS", "OLYMPIC_LIFT", "SHOULDER_PRESS"})


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------

@dataclass
class AuthoringReport:
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    structure: list[Phase | RepeatBlock] | None = None
    exercises: list[Exercise | Circuit] | None = None
    session_intensity: str | None = None
    family: str = 'other'
    station_walk: dict | None = None   # strength: station_walk() of the exercises

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, field_path: str, message: str, **extra) -> None:
        self.errors.append({'field': field_path, 'message': message, **extra})

    def warn(self, field_path: str, message: str, **extra) -> None:
        self.warnings.append({'field': field_path, 'message': message, **extra})


def _format_pydantic_errors(exc: ValidationError, root: str, allowed_keys_for: dict[str, tuple[str, ...]]) -> list[dict]:
    """Pydantic errors -> [{'field': 'structure[1].steps[0]', 'message': ...}].

    Discriminated-union locations carry the chosen tag ('phase', 'repeat',
    'exercise', 'circuit'); the tag names the model that owns the failing
    key, so an unknown-key error can list that model's allowed keys.
    """
    tags = ('phase', 'repeat', 'exercise', 'circuit')
    problems = []
    for err in exc.errors():
        raw = [p for p in err.get('loc', ()) if p != 'items']
        owner = root if root != 'structure' else 'phase'
        if root == 'exercises':
            owner = 'exercise'
        path_parts: list[str] = []
        prev = None
        for part in raw:
            if isinstance(part, str) and part in tags:
                owner = part
            elif isinstance(part, int):
                path_parts.append(f'[{part}]')
                if prev == 'exercises' and owner == 'circuit':
                    owner = 'circuit_exercise'
            else:
                path_parts.append(f'.{part}')
            prev = part
        kind = err.get('type')
        msg = err.get('msg', '')
        if msg.startswith('Value error, '):
            msg = msg[len('Value error, '):]
        if kind == 'extra_forbidden':
            key = path_parts.pop()[1:] if path_parts else '?'
            allowed = allowed_keys_for.get(owner, ())
            msg = f"unknown key {key!r}; allowed keys: {', '.join(allowed)}"
        elif kind == 'missing':
            key = path_parts.pop()[1:] if path_parts else '?'
            msg = f"{key} is required"
        elif kind in ('int_parsing', 'int_type', 'float_parsing', 'float_type', 'int_from_float'):
            key = path_parts[-1][1:] if path_parts and path_parts[-1].startswith('.') else owner
            msg = f"{key} must be a number, got {err.get('input')!r}"
        elif kind == 'list_type':
            key = path_parts[-1][1:] if path_parts and path_parts[-1].startswith('.') else owner
            msg = f"{key} must be a [low, high] pair, got {err.get('input')!r}"
        elif kind in ('union_tag_invalid', 'dict_type', 'model_type', 'model_attributes_type'):
            what = "a phase or a repeat block" if root == 'structure' else "an exercise or a circuit"
            msg = f"each entry must be an object ({what}), got {err.get('input')!r}"
        elif kind == 'literal_error' and path_parts and path_parts[-1] == '.phase':
            msg = "a block with iterations/steps is {'phase': 'repeat', ...}; a plain step has no steps"
        problems.append({'field': root + ''.join(path_parts), 'message': msg})
    seen, unique = set(), []
    for p in problems:
        key = (p['field'], p['message'])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


ALLOWED_KEYS = {
    'phase': tuple(Phase.model_fields),
    'repeat': tuple(RepeatBlock.model_fields),
    'exercise': tuple(Exercise.model_fields),
    'circuit': tuple(Circuit.model_fields),
    'circuit_exercise': tuple(CircuitExercise.model_fields),
    'session': tuple(SessionFields.model_fields),
}


def validate_structure(structure: Any) -> tuple[list[Phase | RepeatBlock] | None, list[dict]]:
    if not isinstance(structure, list):
        return None, [{'field': 'structure', 'message': 'structure must be a list of phases'}]
    try:
        parsed = Structure.model_validate({'items': structure})
    except ValidationError as exc:
        return None, _format_pydantic_errors(exc, 'structure', ALLOWED_KEYS)
    return parsed.items, []


def validate_exercises(exercises: Any) -> tuple[list[Exercise | Circuit] | None, list[dict]]:
    if not isinstance(exercises, list):
        return None, [{'field': 'exercises', 'message': 'exercises must be a list'}]
    try:
        parsed = ExerciseList.model_validate({'items': exercises})
    except ValidationError as exc:
        problems = _format_pydantic_errors(exc, 'exercises', ALLOWED_KEYS)
        # A circuit item that failed as an exercise: say so plainly
        for p in problems:
            if p['message'].startswith("unknown key 'rounds'") or p['message'].startswith("unknown key 'exercises'"):
                p['message'] = ("a circuit is {'rounds': N, 'exercises': [...], 'rest_secs': S}; "
                                "its exercises carry reps or duration_secs, not sets")
        return None, problems
    return parsed.items, []


def _catalogue_entry(catalogue: dict | None, name: str) -> dict | None:
    if not catalogue:
        return None
    entry = catalogue.get(name)
    return entry if isinstance(entry, dict) else None


def catalogue_categories(name: str, entry: dict | None) -> list[str]:
    """Every Garmin category a catalogue entry is filed under, primary first.
    An entry from the one-category cache format has only `category`; a
    current one carries `categories` and the primary is derived from them."""
    if not entry:
        return []
    listed = [normalise_category(c) for c in (entry.get('categories') or []) if normalise_category(c)]
    if listed:
        primary = primary_catalogue_category(name, listed)
        return [primary] + [c for c in listed if c != primary]
    primary = normalise_category(entry.get('category'))
    return [primary] if primary else []


def readable_exercise_name(name: str) -> str:
    """GOBLET_SQUAT -> 'Goblet Squat', _45_DEGREE_PLANK -> '45 Degree Plank'."""
    return str(name or '').replace('_', ' ').title().strip()


def step_description(spec: CircuitExercise, name_kept: bool, cue: str, side: str | None = None) -> str:
    """The step text as the watch shows it, before the display cut. With the
    exercise name kept, Garmin shows the name itself and the text is the side
    and the cue ("Left: slow"); otherwise the readable name leads ("Banded
    Ankle Eversion left: slow")."""
    cue = str(cue or '').strip()
    if name_kept:
        parts = [p for p in (side, cue) if p]
        return ': '.join(parts) if len(parts) == 2 else ''.join(parts)
    head = readable_exercise_name(spec.name)
    if side:
        head = f"{head} {side.lower()}"
    return f"{head}: {cue}" if cue else head


def resolve_exercise_category(spec: CircuitExercise, catalogue: dict | None) -> tuple[str | None, str, str | None]:
    """(workout category or None, exercise name to send, warning or None).

    Rules (Garmin, not judgment):
    - a stated category wins; the catalogue only tells us whether Garmin will
      keep the name (it keeps it for a non-custom catalogue entry filed under
      that category — any of the entry's categories);
    - no stated category: the entry's primary category (mapped through the
      catalogue categories the workout API rejects); none -> the caller must
      state one.
    """
    entry = _catalogue_entry(catalogue, spec.name)
    listed = catalogue_categories(spec.name, entry)
    primary = listed[0] if listed else ''
    custom = bool(entry.get('custom')) if entry else False

    if spec.category:
        category = spec.category
        if entry is None:
            if catalogue:
                return category, '', (f"{spec.name} is not in the Garmin exercise catalogue; "
                                      f"the watch shows '{category.replace('_', ' ').title()}' "
                                      "plus the description, not the name")
            return category, spec.name, None  # no catalogue on this install: trust the pair
        if custom:
            return category, '', (f"{spec.name} is a custom catalogue entry; Garmin strips the name, "
                                  "the description carries it")
        if category in listed:
            return category, spec.name, None
        return category, '', (f"{spec.name} is listed under {', '.join(listed) or 'no category'} in the "
                              f"catalogue, not {category}; Garmin will strip the name (keep {category} "
                              f"for the animation, or use {primary or 'the catalogue category'} to keep the name)")

    # No category stated
    if entry is None:
        return None, '', None
    if custom:
        mapped = CATALOGUE_CATEGORY_TO_WORKOUT.get(primary, primary)
        return (mapped or None), '', (f"{spec.name} is a custom catalogue entry; Garmin strips the name, "
                                      "the description carries it")
    if primary in VALID_WORKOUT_CATEGORIES:
        return primary, spec.name, None
    mapped = CATALOGUE_CATEGORY_TO_WORKOUT.get(primary, '')
    if mapped:
        return mapped, '', (f"{spec.name} is catalogued under {primary}, which the workout "
                            f"API rejects; sent as {mapped} without the name")
    return None, '', None


def _check_exercise(spec: CircuitExercise, path: str, catalogue: dict | None, report: AuthoringReport) -> None:
    category, _name, warning = resolve_exercise_category(spec, catalogue)
    if category is None:
        allowed = ', '.join(sorted(VALID_WORKOUT_CATEGORIES))
        entry = _catalogue_entry(catalogue, spec.name)
        if entry is not None:
            report.error(path, (f"{spec.name} is catalogued under {entry.get('category')!s}, which "
                                "carries no Garmin workout category, so 'category' is required "
                                f"(the watch shows the category and the description). Allowed: {allowed}"))
        elif catalogue:
            report.error(path, (f"{spec.name} is not in the Garmin exercise catalogue, so "
                                "'category' is required (the watch shows the category and the "
                                f"description). Allowed: {allowed}"))
        else:
            report.error(path, (f"{spec.name}: 'category' is required (no exercise catalogue "
                                f"on this install to look it up). Allowed: {allowed}"))
    elif warning:
        report.warn(path, warning)
    text = step_description(spec, bool(_name), spec.notes or '', 'Right' if spec.each_side else None)
    if len(text) > STEP_NOTE_MAX_CHARS:
        report.warn(path, (f"the step text is {len(text)} characters (\"{text}\"); the watch shows "
                           f"about {STEP_NOTE_MAX_CHARS}, so it is cut to a word boundary"))
    _check_counterpart(spec, path, catalogue, report)


def _check_counterpart(spec: CircuitExercise, path: str, catalogue: dict | None, report: AuthoringReport) -> None:
    """Garmin pairs a bodyweight movement with its loaded twin (SIDE_PLANK <->
    WEIGHTED_SIDE_PLANK: `counterpart` + `is_bodyweight` in the catalogue).
    The loaded key with no weight_kg is an error: the watch would announce a
    weight the plan never stated. The bodyweight key with a load is a hint."""
    entry = _catalogue_entry(catalogue, spec.name)
    if not entry or entry.get('custom'):
        return
    counterpart = normalise_exercise_name(entry.get('counterpart') or '')
    twin = _catalogue_entry(catalogue, counterpart) if counterpart else None
    if twin is None:
        return
    if spec.weight_kg is None and twin.get('is_bodyweight') and not entry.get('is_bodyweight'):
        report.error(path, (f"{spec.name} is the loaded version of {counterpart} but states no weight_kg: "
                            f"the watch would show \"{readable_exercise_name(spec.name)}\" for a bodyweight "
                            f"movement. Use {counterpart}, or state the load"))
    elif spec.weight_kg is not None and entry.get('is_bodyweight') and not twin.get('is_bodyweight'):
        report.warn(path, (f"{spec.name} is the bodyweight version; with weight_kg {spec.weight_kg:g} "
                           f"Garmin has {counterpart}"))


def _check_phase(item: Phase | RepeatBlock, path: str, family: str, report: AuthoringReport,
                 session_intensity: str | None = None) -> None:
    if isinstance(item, RepeatBlock):
        for i, sub in enumerate(item.steps):
            _check_phase(sub, f"{path}.steps[{i}]", family, report, session_intensity)
        return
    if item.notes and len(item.notes) > STEP_NOTE_MAX_CHARS:
        report.warn(path, f"notes are {len(item.notes)} characters; the watch shows about "
                          f"{STEP_NOTE_MAX_CHARS}, so the cue is cut to a word boundary")
    if family == 'running' and (item.power_watts is not None or item.power_pct is not None):
        report.warn(path, "power targets on a running step are sent as given; the watch needs a running power source")
    if family == 'cycling' and (item.pace is not None or item.pace_sec_per_km is not None):
        report.error(path, "pace targets are for running; cycling steps take power_watts, power_pct, hr_target, cadence or intensity")
    if item.phase == 'interval' and item.target_key() is None and session_intensity is None:
        report.warn(path, ("interval step has no target (no intensity, hr_target, pace or power) and "
                           "the session intensity is not a zone word; it renders as an untargeted step"))


def validate_session(session: dict, catalogue: dict | None = None,
                     strength_baseline: dict | None = None,
                     station_order: str = STATION_ORDER_DEFAULT) -> AuthoringReport:
    """Validate the authoring surface of ONE plan session.

    Checks `structure` (running, cycling) and `exercises` (strength) against
    the strict models, resolves exercise categories against the Garmin
    catalogue when one is supplied, and warns about session-level intensity
    words outside the vocabulary. With `strength_baseline` (the athlete's
    `strength_baseline.exercises` mapping) it also warns when a loaded
    movement states no load although the baseline holds one — a fact about
    the athlete's record, never a value the renderer would pick. `station_order`
    (methodology.strength.station_order: off | warn | error) sets whether a
    strength session that returns to a station it left, a circuit whose rounds
    change spot, or unstated stations are warnings, errors or unchecked. Rest
    days and families without structure pass through with an empty report.
    """
    report = AuthoringReport()
    if not isinstance(session, dict):
        report.error('session', 'session must be an object')
        return report
    session_type = str(session.get('type', '') or '').lower()
    family = workout_family_for(session_type)
    if family == 'other':
        if 'ride' in session_type or 'cycling' in session_type:
            family = 'cycling'
        elif 'run' in session_type:
            family = 'running'
    report.family = family
    if family == 'rest':
        return report

    bad_minutes: set[str] = set()
    for key in ('duration_mins', 'warmup_mins', 'cooldown_mins'):
        value = session.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            report.error(key, f"{key} must be a number of minutes (not text, not a flag), got {value!r}")
            bad_minutes.add(key)
        elif key == 'duration_mins' and value <= 0:
            report.error(key, f"duration_mins must be more than 0 minutes, got {value!r}")
            bad_minutes.add(key)
        elif value < 0:
            report.error(key, f"{key} cannot be negative, got {value!r}")
            bad_minutes.add(key)

    raw_intensity = session.get('intensity')
    if raw_intensity is not None and str(raw_intensity).strip().lower() != 'discretion':
        word = normalise_intensity(raw_intensity)
        report.session_intensity = word
        if word is None and family in ('running', 'cycling') and str(raw_intensity).strip().lower() != 'none':
            report.warn('intensity', (f"intensity {raw_intensity!r} is not in the zone vocabulary "
                                      f"({', '.join(INTENSITY_WORDS)}); the main step renders "
                                      "without a target"))
    elif raw_intensity is None and family in ('running', 'cycling') and not session.get('structure') \
            and not _is_ftp_test(session_type):
        report.warn('intensity', ("no intensity stated: the main block renders without a target "
                                  f"(state one of {', '.join(INTENSITY_WORDS)}, or 'discretion' "
                                  "for an athlete-choice day)"))

    if _is_ftp_test(session_type):
        _check_protocol(session, report)
        return report

    prose = ' '.join(str(session.get(k) or '') for k in ('description', 'notes'))
    structure = session.get('structure')
    if structure not in (None, [], {}):
        if family in ('running', 'cycling'):
            items, problems = validate_structure(structure)
            report.errors.extend(problems)
            if items is not None:
                report.structure = items
                for i, item in enumerate(items):
                    _check_phase(item, f"structure[{i}]", family, report, report.session_intensity)
        elif family == 'swimming':
            _check_swimming(session, report)  # lenient shape, but the numbers must be numbers
        else:
            report.warn('structure', f"'structure' is read for running and cycling sessions only; a {family} session ignores it")
    elif family in ('running', 'cycling') and _looks_like_sets(prose):
        report.error('structure', ("the description reads like a rep scheme ('5 x 3 min') but the session "
                                   "has no 'structure', so the watch would show ONE block at the session "
                                   "intensity; write the phases in structure (preview_workout() shows the contract)"))
    elif family in ('running', 'cycling') and _looks_like_interval_words(prose):
        report.warn('structure', ("the description mentions intervals or repeats but the session has "
                                  "no 'structure', so the watch shows ONE block at the session intensity; "
                                  "write the phases in structure (preview_workout() shows the contract)"))

    exercises = session.get('exercises')
    if exercises not in (None, [], {}):
        if family == 'strength':
            items, problems = validate_exercises(exercises)
            report.errors.extend(problems)
            if items is not None:
                report.exercises = items
                for i, item in enumerate(items):
                    if isinstance(item, Circuit):
                        for j, ex in enumerate(item.exercises):
                            _check_exercise(ex, f"exercises[{i}].exercises[{j}]", catalogue, report)
                    else:
                        _check_exercise(item, f"exercises[{i}]", catalogue, report)
        elif family == 'pilates':
            _check_pilates(exercises, report)  # lenient reps-or-time-or-lap shape; numbers must be numbers
        else:
            report.warn('exercises', f"'exercises' is read for strength and pilates sessions only; a {family} session ignores it")
    elif family == 'strength' and _looks_like_sets(prose):
        report.error('exercises', ("the description reads like sets and reps but the session has "
                                   "no 'exercises' list, so the watch would get ONE timed block; "
                                   "put each movement in exercises (name, sets, reps or duration_secs)"))

    if family in ('strength', 'running', 'cycling'):
        extra = {k: session[k] for k in ('warmup_mins', 'cooldown_mins')
                 if k in session and k not in bad_minutes}
        try:
            SessionFields.model_validate(extra)
        except ValidationError as exc:
            report.errors.extend(_format_pydantic_errors(exc, 'session', ALLOWED_KEYS))
    if family == 'strength':
        if session.get('skip_warmup') is not None:
            report.warn('skip_warmup', "skip_warmup is retired: no warm-up is inserted unless warmup_mins is set")
        if report.exercises:
            _check_athlete_facing(session, report, catalogue, strength_baseline)
            _check_stations(report, station_order)

    return report


def _iter_exercise_specs(items) -> list[tuple[str, CircuitExercise]]:
    out = []
    for i, item in enumerate(items or []):
        if isinstance(item, Circuit):
            out.extend((f"exercises[{i}].exercises[{j}]", ex) for j, ex in enumerate(item.exercises))
        else:
            out.append((f"exercises[{i}]", item))
    return out


def _exercise_label(spec: CircuitExercise) -> str:
    return f"{spec.name} {spec.weight_kg:g} kg" if spec.weight_kg is not None else spec.name


def station_walk(items) -> dict:
    """The stations a strength session walks through, from the stated labels.

    {'blocks': [{'station', 'paths', 'exercises': ['NAME 10 kg', ...]}, ...]
    — consecutive movements at one station form a block; a movement with no
    station is transparent (it neither starts nor ends a block) and is listed
    under 'unstated'; 'revisits': [{'path', 'station', 'between'}] — a block
    at a station an earlier block already used, 'between' the stations since
    it was left; 'circuits_spanning': [{'path', 'stations'}] — a circuit whose
    members state different stations (a circuit's own station covers the
    members that state none); 'stated': how many movements state one.
    Pure: no catalogue, no clock.
    """
    blocks: list[dict] = []
    revisits: list[dict] = []
    spanning: list[dict] = []
    unstated: list[str] = []
    stated = 0

    def visit(path: str, station: str | None, label: str) -> None:
        nonlocal stated
        if station is None:
            unstated.append(path)
            return
        stated += 1
        if blocks and blocks[-1]['station'] == station:
            blocks[-1]['paths'].append(path)
            blocks[-1]['exercises'].append(label)
            return
        earlier = [b['station'] for b in blocks]
        if station in earlier:
            last = len(earlier) - 1 - earlier[::-1].index(station)
            revisits.append({'path': path, 'station': station, 'between': earlier[last + 1:]})
        blocks.append({'station': station, 'paths': [path], 'exercises': [label]})

    for i, item in enumerate(items or []):
        path = f"exercises[{i}]"
        if isinstance(item, Circuit):
            members = [(f"{path}.exercises[{j}]", ex, ex.station or item.station)
                       for j, ex in enumerate(item.exercises)]
            stations: list[str] = []
            for _p, _ex, st in members:
                if st is not None and st not in stations:
                    stations.append(st)
            if len(stations) > 1:
                spanning.append({'path': path, 'stations': stations})
            for p, ex, st in members:
                visit(p, st, _exercise_label(ex))
        else:
            visit(path, item.station, _exercise_label(item))
    return {'blocks': blocks, 'revisits': revisits, 'circuits_spanning': spanning,
            'unstated': unstated, 'stated': stated}


def station_walk_line(walk: dict) -> str:
    """'incline bench → cable → incline bench (again) → mat' — the line the
    preview prints ahead of the steps; unstated movements are counted."""
    seen: set[str] = set()
    parts = []
    for block in walk.get('blocks') or []:
        station = block['station']
        parts.append(f"{station} (again)" if station in seen else station)
        seen.add(station)
    line = ' → '.join(parts)
    missing = len(walk.get('unstated') or [])
    if missing:
        line += f"; {missing} movement{'s' if missing != 1 else ''} with no station"
    return line


def _check_stations(report: AuthoringReport, level: str) -> None:
    """The station walk and its problems, at the athlete's level: 'error'
    rejects the save, 'warn' reports, 'off' only records the walk."""
    walk = station_walk(report.exercises)
    report.station_walk = walk
    level = str(level or '').strip().lower()
    if level not in STATION_ORDER_LEVELS:
        level = STATION_ORDER_DEFAULT
    if level == 'off':
        return
    say = report.error if level == 'error' else report.warn
    for hit in walk['revisits']:
        between = ', '.join(f"'{s}'" for s in hit['between'])
        say(hit['path'], (f"returns to '{hit['station']}' after {between}: finish a station and the "
                          "weights carried there before moving. Reorder the session, or state the "
                          "station this movement is really done at"))
    for hit in walk['circuits_spanning']:
        stations = ', '.join(f"'{s}'" for s in hit['stations'])
        say(hit['path'], (f"a circuit whose rounds change spot ({stations}): every round walks between "
                          "them. Write the movements as straight sets at their stations, or keep a "
                          "circuit to one station or to equipment that travels (band, mat)"))
    if walk['unstated'] and len(_iter_exercise_specs(report.exercises)) >= 2:
        shown = ', '.join(walk['unstated'][:6])
        if len(walk['unstated']) > 6:
            shown += f" and {len(walk['unstated']) - 6} more"
        say('exercises', (f"no station on {shown}: the station walk cannot be checked there. State the "
                          "spot on every movement ('incline bench', 'cable', 'mat'; one label per spot, "
                          "reused verbatim; a circuit's station covers its members)"))


def _baseline_load_for(category: str | None, baseline: dict | None) -> tuple[str, float] | None:
    """(baseline key, current weight) when the athlete's strength baseline
    holds a load for this category; None otherwise. Data only."""
    if not category or not baseline:
        return None
    entry = baseline.get(category.lower())
    if not isinstance(entry, dict):
        return None
    current = entry.get('current') if isinstance(entry.get('current'), dict) else {}
    weight = current.get('weight_kg') if current else entry.get('current_weight_kg')
    try:
        weight = float(weight) if weight is not None else None
    except (TypeError, ValueError):
        weight = None
    if not weight:
        return None
    return category.lower(), weight


def _check_athlete_facing(session: dict, report: AuthoringReport, catalogue: dict | None,
                          baseline: dict | None) -> None:
    """Warnings about the athlete, not about Garmin: a loaded movement with no
    stated load while the baseline records one; loaded compounds with no
    warm-up stated. Facts for the coach to act on — nothing is filled in."""
    specs = _iter_exercise_specs(report.exercises)
    categories = []
    for path, spec in specs:
        category, _name, _warning = resolve_exercise_category(spec, catalogue)
        categories.append(category)
        if spec.weight_kg is None and category in LOADED_CATEGORIES:
            found = _baseline_load_for(category, baseline)
            if found:
                key, weight = found
                variation = (baseline.get(key) or {}).get('preferred_variation') if baseline else None
                report.warn(path, (f"{spec.name} states no load; the strength baseline records "
                                   f"{weight:g} kg for {key}"
                                   + (f" ({variation})" if variation else "")
                                   + " — write weight_kg if a load is meant, the watch shows none"))
    compounds = [c for c in categories if c in COMPOUND_CATEGORIES]
    if compounds and not session.get('warmup_mins'):
        first = categories[0] if categories else None
        if first != 'WARM_UP':
            report.warn('warmup_mins', (f"compound work ({', '.join(sorted(set(compounds)))}) with no "
                                        "warm-up stated: set warmup_mins or start with a WARM_UP-category "
                                        "movement; nothing is inserted"))


def _is_ftp_test(session_type: str) -> bool:
    return session_type in ('ftp_test', 'threshold_test') or 'ftp_test' in session_type


class ProtocolPhase(StrictModel):
    """One phase of an FTP-test `protocol` (the built-in protocol is used when
    the session gives none). Timed phases with optional cadence guidance."""
    phase: str | None = None
    name: str | None = None
    duration_mins: float = Field(gt=0)
    cadence_min: float | None = Field(default=None, ge=20, le=250)
    cadence_max: float | None = Field(default=None, ge=20, le=250)
    notes: str | None = None

    @model_validator(mode='after')
    def _cadence_pair(self):
        if (self.cadence_min is None) != (self.cadence_max is None):
            raise ValueError("cadence_min and cadence_max go together")
        return self


def _check_protocol(session: dict, report: AuthoringReport) -> None:
    if session.get('structure'):
        report.error('structure', ("an ftp_test renders its test protocol and ignores 'structure'; "
                                   "remove structure, or use type 'wattbike' with structure for a "
                                   "custom session"))
    protocol = session.get('protocol')
    if protocol in (None, []):
        return
    if not isinstance(protocol, list):
        report.error('protocol', "protocol must be a list of phases")
        return
    for i, phase in enumerate(protocol):
        try:
            ProtocolPhase.model_validate(phase)
        except ValidationError as exc:
            for problem in _format_pydantic_errors(exc, f'protocol[{i}]', {f'protocol[{i}]': tuple(ProtocolPhase.model_fields)}):
                if problem['message'].startswith('unknown key'):
                    problem['message'] = problem['message'].split('; allowed keys')[0] + \
                        f"; allowed keys: {', '.join(ProtocolPhase.model_fields)}"
                report.errors.append(problem)


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _check_swimming(session: dict, report: AuthoringReport) -> None:
    pool = session.get('pool_length_m')
    if pool is not None and not _positive_number(pool):
        report.error('pool_length_m', f"pool_length_m must be a number of metres, got {pool!r}")
    for i, phase in enumerate(session.get('structure') or []):
        if not isinstance(phase, dict):
            report.error(f'structure[{i}]', "each swimming phase is an object with distance_m")
            continue
        distance = phase.get('distance_m')
        if distance is not None and not _positive_number(distance):
            report.error(f'structure[{i}].distance_m', f"distance_m must be a number of metres, got {distance!r}")


PILATES_EXERCISE_KEYS = ('name', 'sets', 'reps', 'duration_secs', 'notes')


def _check_pilates(exercises: Any, report: AuthoringReport) -> None:
    if not isinstance(exercises, list):
        report.error('exercises', "exercises must be a list")
        return
    for i, ex in enumerate(exercises):
        if not isinstance(ex, dict):
            report.error(f'exercises[{i}]', "each rehab exercise is an object (name, sets, reps or duration_secs, notes)")
            continue
        for key in ('sets', 'reps', 'duration_secs'):
            value = ex.get(key)
            if value is not None and not _positive_number(value):
                report.error(f'exercises[{i}].{key}', f"{key} must be a whole number, got {value!r}")
        unknown = [k for k in ex if k not in PILATES_EXERCISE_KEYS]
        if unknown:
            report.warn(f'exercises[{i}]', (f"unknown key(s) {', '.join(map(repr, unknown))} are ignored on a rehab "
                                           f"exercise; keys read: {', '.join(PILATES_EXERCISE_KEYS)}"))


def _looks_like_sets(text: str) -> bool:
    import re
    return bool(re.search(r'\b\d+\s*[xX×]\s*\d+\b', text or ''))


def _looks_like_interval_words(text: str) -> bool:
    """'intervals', 'repeats', 'strides', 'ladder', 'fartlek' — but not when negated
    ('no intervals today', 'without repeats')."""
    text = re.sub(r'\b(no|not|without|skip|zero)\s+(\w+\s+)?(intervals?|repeats?|reps|strides|ladder|fartlek|hill reps?)\b',
                  ' ', text or '', flags=re.I)
    return bool(re.search(r'\b(intervals?|repeats?|reps|strides|ladder|fartlek|hill reps?)\b', text, re.I))


def _looks_like_intervals(text: str) -> bool:
    """Kept for callers: a numeric rep scheme or interval words."""
    return _looks_like_sets(text) or _looks_like_interval_words(text)


# ---------------------------------------------------------------------------
# The contract document (published as a resource and a tool result)
# ---------------------------------------------------------------------------

def _schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop('$defs', None)
    return schema


def contract_document(sport: str | None = None) -> str:
    """Markdown contract: vocabulary, rules, JSON schemas. `sport` narrows to
    'running', 'cycling' or 'strength'."""
    sport = (sport or '').strip().lower() or None
    parts = ["# Session authoring contract\n",
             "What a plan session may say about its structure and exactly how it lands on the "
             "watch. The renderer never defaults a value the plan did not state; a key outside "
             "this contract is an error that names the allowed keys. Call preview_workout(session) "
             "to see the rendering before saving with update_weekly_plan.\n",
             "## Intensity vocabulary (all sports)\n",
             "| word | shape | HR zone | power zone (indoor rides) | pace zone (runs) |", "|---|---|---|---|---|"]
    for word, (hr, pw, pace) in INTENSITY_ZONES.items():
        shape = 'ceiling' if word in CEILING_INTENSITIES else 'band'
        parts.append(f"| {word} | {shape} | {hr} | {pw} | {pace} |")
    parts.append("\nAliases: z1-z6, zone1-zone5, endurance/aerobic (easy), hard (threshold), "
                 "vo2max (vo2), anaerobic/sprint (max_effort); 'none' on a phase means no target "
                 "even when the session has an intensity. Any other word is an error inside "
                 "a phase and a warning at session level. A running step resolves intensity to a "
                 "pace zone when threshold pace is set, else HR; an indoor ride to a power zone "
                 "when power zones are set, else HR; an outdoor ride to HR.\n")
    parts.append(targets_are_alerts_text() + "\n")
    if sport in (None, 'running', 'cycling'):
        parts += ["## Running and cycling: `structure`\n",
                  "A list of phases and repeat blocks. Each phase: exactly one end condition "
                  "(duration_secs, duration_mins or distance_m; a number, or \"open\" for a lap-button "
                  "step) and at most one numeric target (intensity may accompany it as a label). Repeat: {'phase': 'repeat', 'iterations': N, "
                  "'steps': [...]} (nests). Cycling steps take power_watts, power_pct (list, or one "
                  "number for a ±5 % band of the athlete's FTP), hr_target, hr_max, cadence or intensity; running "
                  "steps take pace_sec_per_km, pace (m/s), hr_target, hr_max, cadence or intensity. A session "
                  "without structure renders from duration_mins and intensity: a plain outdoor ride is "
                  "ONE block; " + plain_split_text() + " — "
                  "state warmup_mins / cooldown_mins on the session to replace those splits. A "
                  "description that reads like intervals without a structure list is a warning: the "
                  "watch shows one block. On a 'discretion' day the main block is untargeted and the "
                  "session's constraints are written on it.\n",
                  "Plausibility (errors outside): hr_target and hr_max 30-230 bpm, pace_sec_per_km 120-1200, "
                  "pace 0.5-10 m/s, power_watts 0-2000, power_pct 20-200 (a percentage, not a "
                  "fraction), cadence 20-250, distance_m >= 20. An ftp_test takes an optional "
                  "'protocol' (phases with duration_mins and cadence_min/max) and ignores structure.\n",
                  "```json\n" + json.dumps(_schema(Phase), indent=1) + "\n```\n"]
    if sport in (None, 'strength'):
        parts += ["## Strength: `exercises`\n",
                  "A list of exercises and circuits. Exercise: name (Garmin key), sets, and exactly one "
                  "of reps or duration_secs; optional category (required when the name is not in the "
                  "catalogue), each_side, weight_kg (only an explicit value is sent), rest_secs (a timed "
                  "rest step when given, lap button otherwise), notes (the cue on the watch). Circuit: "
                  "{'rounds': N, 'exercises': [...], 'rest_secs': S}. A hold is timed: a name ending in "
                  "PLANK, HOLD, HANG or CARRY (or carrying L_SIT / WALL_SIT) takes duration_secs, and a rep "
                  "count on it is an error (reps \"open\" is allowed: the lap button ends the hold). "
                  "each_side renders a Left step and a Right step per set or round, each with the stated "
                  "reps or time, so the estimate counts both. Session-level warmup_mins adds a "
                  "timed warm-up block; none is inserted otherwise. Sets and reps written in the "
                  "description without an exercises list are an error. Garmin keeps the exercise name "
                  "only for a catalogue entry filed under the step's category (a name without a stated "
                  "category is sent under its primary catalogue category); otherwise the watch shows the "
                  "category and the description (readable name + cue), cut at about 50 characters. A "
                  "WEIGHTED_* key whose bodyweight counterpart exists is an error without weight_kg: write "
                  "the counterpart (SIDE_PLANK, not WEIGHTED_SIDE_PLANK) or the load. When the plan gives "
                  "no notes, the researched exercise-library cue is used and flagged as such. station: the "
                  "spot in the gym where the movement is done ('incline bench', 'cable', 'mat'; one label "
                  "per spot, reused verbatim; a circuit's station covers the members that state none). It "
                  "is not sent to the watch: the preview prints the station walk (a 'Stations:' line ahead "
                  "of the steps) and warns when the session returns to a station it left, when a circuit's "
                  "rounds change spot, and when stations are unstated. methodology strength.station_order "
                  "(off | warn | error, default warn; update_methodology) makes those save-time errors, "
                  "or switches the check off.\n",
                  f"Allowed categories: {', '.join(sorted(VALID_WORKOUT_CATEGORIES))}\n",
                  "```json\n" + json.dumps(_schema(Exercise), indent=1) + "\n```\n",
                  "```json\n" + json.dumps(_schema(Circuit), indent=1) + "\n```\n"]
    parts.append(f"Display limits: workout name {WORKOUT_NAME_MAX_CHARS} characters, step cue about "
                 f"{STEP_NOTE_MAX_CHARS} (cut at a word boundary; the preview warns).")
    return '\n'.join(parts)
