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

# The cached catalogue keeps ONE category per exercise name, and files the
# plain bodyweight movements (PLANK, CRUNCH, PUSH_UP, SIT_UP) under
# SUSPENSION, where Garmin also lists them. When the plan omits the category
# these corrections pick the category the watch animates — only for an exact
# name match, or for a name the cache files under SUSPENSION. Any other
# cached category is trusted as is (a substring match used to override 31
# valid categories silently). They never override a category the plan states.
CATALOGUE_CATEGORY_CORRECTIONS: tuple[tuple[str, str], ...] = (
    ("CRUNCH", "CRUNCH"),
    ("PUSH_UP", "PUSH_UP"),
    ("PUSHUP", "PUSH_UP"),
    ("PLANK", "PLANK"),
    ("SIT_UP", "SIT_UP"),
    ("SITUP", "SIT_UP"),
    ("LEG_RAISE", "LEG_RAISE"),
    ("LEG_LIFT", "LEG_RAISE"),
)
MISFILED_CATEGORY = "SUSPENSION"

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
    return text.strip('_')


def normalise_category(value: Any) -> str:
    return normalise_exercise_name(value)


# Garmin shows roughly 50 characters of a step description on the watch
# (our cap; Garmin's own limit is unverified) and 40 of a workout name.
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
    Target: at most one of hr_target, pace_sec_per_km, pace, power_watts,
    power_pct, cadence (Garmin shows one target per step); intensity may
    accompany it as a label and is the target when no numeric one is given.
    """
    phase: str = Field(description="warmup | interval | recovery | cooldown | rest")
    duration_secs: OpenOrNumber | None = Field(default=None, description="seconds, or \"open\"")
    duration_mins: OpenOrNumber | None = Field(default=None, description="minutes (fractions allowed), or \"open\"")
    distance_m: OpenOrNumber | None = Field(default=None, description="metres, or \"open\"")
    intensity: str | None = Field(default=None, description="recovery | easy | tempo | sweet_spot | threshold | vo2 | max_effort (z1-z6 accepted)")
    hr_target: list[float] | None = Field(default=None, description="[low_bpm, high_bpm]")
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
    NUMERIC_TARGET_KEYS: ClassVar[tuple[str, ...]] = ('hr_target', 'pace_sec_per_km', 'pace',
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
    reps: int | Literal["open"] | None = Field(default=None, description="reps per set, or \"open\" to end the set with the lap button")
    duration_secs: float | None = Field(default=None, ge=1, description="hold/carry time per set in seconds — timed sets instead of reps")
    each_side: bool = Field(default=False, description="reps/time are per side; the cue says so on the watch")
    weight_kg: float | None = Field(default=None, gt=0, description="load; only an explicit value is sent")
    notes: str | None = Field(default=None, description=f"form cue shown on the step (about {STEP_NOTE_MAX_CHARS} characters visible)")

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


def resolve_exercise_category(spec: CircuitExercise, catalogue: dict | None) -> tuple[str | None, str, str | None]:
    """(workout category or None, exercise name to send, warning or None).

    Rules (Garmin, not judgment):
    - a stated category wins; the catalogue only tells us whether Garmin will
      keep the name (it keeps it only for non-custom catalogue entries whose
      catalogue category equals the step category);
    - no stated category: the catalogue category (mapped through the four
      the workout API rejects, corrected for the cache's one-category-per-
      name limit); none -> the caller must state one.
    """
    entry = _catalogue_entry(catalogue, spec.name)
    cat_category = normalise_category(entry.get('category')) if entry else ''
    custom = bool(entry.get('custom')) if entry else False
    corrected = None
    for pattern, cat in CATALOGUE_CATEGORY_CORRECTIONS:
        if spec.name == pattern or (cat_category == MISFILED_CATEGORY and pattern in spec.name):
            corrected = cat
            break

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
        if cat_category == category or corrected == category:
            return category, spec.name, None
        return category, '', (f"{spec.name} is listed under {cat_category} in the catalogue, "
                              f"not {category}; Garmin will strip the name (keep {category} "
                              "for the animation, or use the catalogue category to keep the name)")

    # No category stated
    if entry is None:
        return None, '', None
    if custom:
        mapped = CATALOGUE_CATEGORY_TO_WORKOUT.get(cat_category, cat_category)
        return (mapped or None), '', (f"{spec.name} is a custom catalogue entry; Garmin strips the name, "
                                      "the description carries it")
    if corrected:
        return corrected, spec.name, None
    if cat_category in VALID_WORKOUT_CATEGORIES:
        return cat_category, spec.name, None
    mapped = CATALOGUE_CATEGORY_TO_WORKOUT.get(cat_category, '')
    if mapped:
        return mapped, '', (f"{spec.name} is catalogued under {cat_category}, which the workout "
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
    if spec.notes and len(spec.notes) > STEP_NOTE_MAX_CHARS:
        report.warn(path, f"notes are {len(spec.notes)} characters; the watch shows about "
                          f"{STEP_NOTE_MAX_CHARS}, so the cue is cut to a word boundary")


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
                     strength_baseline: dict | None = None) -> AuthoringReport:
    """Validate the authoring surface of ONE plan session.

    Checks `structure` (running, cycling) and `exercises` (strength) against
    the strict models, resolves exercise categories against the Garmin
    catalogue when one is supplied, and warns about session-level intensity
    words outside the vocabulary. With `strength_baseline` (the athlete's
    `strength_baseline.exercises` mapping) it also warns when a loaded
    movement states no load although the baseline holds one — a fact about
    the athlete's record, never a value the renderer would pick. Rest days
    and families without structure pass through with an empty report.
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

    duration = session.get('duration_mins')
    if duration is not None and not isinstance(duration, bool):
        try:
            float(duration)
        except (TypeError, ValueError):
            report.error('duration_mins', f"duration_mins must be a number of minutes, got {duration!r}")

    raw_intensity = session.get('intensity')
    if raw_intensity is not None and str(raw_intensity).strip().lower() != 'discretion':
        word = normalise_intensity(raw_intensity)
        report.session_intensity = word
        if word is None and family in ('running', 'cycling'):
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
    elif family in ('running', 'cycling') and _looks_like_intervals(prose):
        report.warn('structure', ("the description reads like intervals or repeats but the session has "
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
        extra = {k: session[k] for k in ('warmup_mins', 'cooldown_mins') if k in session}
        try:
            SessionFields.model_validate(extra)
        except ValidationError as exc:
            report.errors.extend(_format_pydantic_errors(exc, 'session', ALLOWED_KEYS))
    if family == 'strength':
        if session.get('skip_warmup') is not None:
            report.warn('skip_warmup', "skip_warmup is retired: no warm-up is inserted unless warmup_mins is set")
        if report.exercises:
            _check_athlete_facing(session, report, catalogue, strength_baseline)

    return report


def _iter_exercise_specs(items) -> list[tuple[str, CircuitExercise]]:
    out = []
    for i, item in enumerate(items or []):
        if isinstance(item, Circuit):
            out.extend((f"exercises[{i}].exercises[{j}]", ex) for j, ex in enumerate(item.exercises))
        else:
            out.append((f"exercises[{i}]", item))
    return out


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
            report.warn('warmup_mins', (f"loaded compound work ({', '.join(sorted(set(compounds)))}) with no "
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


def _looks_like_intervals(text: str) -> bool:
    """'5 x 3 min', '4x4', '6 x 400m', 'intervals', 'repeats', 'strides', 'ladder'."""
    import re
    text = text or ''
    if re.search(r'\b\d+\s*[xX×]\s*\d+', text):
        return True
    return bool(re.search(r'\b(intervals?|repeats?|reps|strides|ladder|fartlek|hill reps?)\b', text, re.I))


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
             "| word | HR zone | power zone (indoor rides) | pace zone (runs) |", "|---|---|---|---|"]
    for word, (hr, pw, pace) in INTENSITY_ZONES.items():
        parts.append(f"| {word} | {hr} | {pw} | {pace} |")
    parts.append("\nAliases: z1-z6, zone1-zone5, endurance/aerobic (easy), hard (threshold), "
                 "vo2max (vo2), anaerobic/sprint (max_effort); 'none' on a phase means no target "
                 "even when the session has an intensity. Any other word is an error inside "
                 "a phase and a warning at session level. A running step resolves intensity to a "
                 "pace zone when threshold pace is set, else HR; an indoor ride to a power zone "
                 "when power zones are set, else HR; an outdoor ride to HR.\n")
    if sport in (None, 'running', 'cycling'):
        parts += ["## Running and cycling: `structure`\n",
                  "A list of phases and repeat blocks. Each phase: exactly one end condition "
                  "(duration_secs, duration_mins or distance_m; a number, or \"open\" for a lap-button "
                  "step) and at most one numeric target (intensity may accompany it as a label). Repeat: {'phase': 'repeat', 'iterations': N, "
                  "'steps': [...]} (nests). Cycling steps take power_watts, power_pct (list, or one "
                  "number for a ±5 % band of the athlete's FTP), hr_target, cadence or intensity; running "
                  "steps take pace_sec_per_km, pace (m/s), hr_target, cadence or intensity. A session "
                  "without structure renders from duration_mins and intensity: a plain outdoor ride is "
                  "ONE block; a plain run gets a warm-up and cool-down of 10 % each (up to 5 min); a "
                  "plain indoor ride 10 % warm-up (up to 10 min) and 5 % cool-down (up to 5 min) — "
                  "state warmup_mins / cooldown_mins on the session to replace those splits. A "
                  "description that reads like intervals without a structure list is a warning: the "
                  "watch shows one block. On a 'discretion' day the main block is untargeted and the "
                  "session's constraints are written on it.\n",
                  "Plausibility (errors outside): hr_target 30-230 bpm, pace_sec_per_km 120-1200, "
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
                  "{'rounds': N, 'exercises': [...], 'rest_secs': S}. Session-level warmup_mins adds a "
                  "timed warm-up block; none is inserted otherwise. Sets and reps written in the "
                  "description without an exercises list are an error. Garmin keeps the exercise name "
                  "only for catalogue entries whose category matches; otherwise the watch shows the "
                  "category and the description (readable name + cue). When the plan gives no notes, "
                  "the researched exercise-library cue is used and flagged as such.\n",
                  f"Allowed categories: {', '.join(sorted(VALID_WORKOUT_CATEGORIES))}\n",
                  "```json\n" + json.dumps(_schema(Exercise), indent=1) + "\n```\n",
                  "```json\n" + json.dumps(_schema(Circuit), indent=1) + "\n```\n"]
    parts.append(f"Display limits: workout name {WORKOUT_NAME_MAX_CHARS} characters, step cue about "
                 f"{STEP_NOTE_MAX_CHARS} (cut at a word boundary; the preview warns).")
    return '\n'.join(parts)
