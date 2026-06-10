"""
Typed schema layer for the coach data files (pydantic v2).

Design principles (Phase 1.2 of the upgrade roadmap):

- LENIENT, NOT DESTRUCTIVE. Validation exists to FLAG problems, never to
  reject or rewrite athlete data. Every model uses ``ConfigDict(extra="allow")``
  and optional fields with safe defaults, so partially-populated, legacy-shaped
  or hand-edited files still parse.
- TOLERATE KNOWN VOCABULARY DRIFT. The live files contain both spellings of
  several keys ('no_training_days'/'blocked_days', 'restrictions'/
  'restricted_activities', 'target_mins_per_week'/'target_minutes_per_week',
  legacy-list vs nested-dict 'training_pillars', dict-vs-list 'planned').
  All forms are accepted; canonicalization is the migration registry's job
  (coach.storage), not the validator's.
- THE ACCEPTANCE BAR is that every live data/*.json file parses cleanly.
  ``python -m coach.storage --check`` verifies this without writing.

Consumers should validate via ``coach.storage`` (which logs warnings naming
the offending file and always returns the raw data) rather than calling these
models directly in tool code.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, RootModel, field_validator

import logging

logger = logging.getLogger(__name__)


class LenientModel(BaseModel):
    """Base for all data-file models: unknown keys are preserved, not errors."""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# athlete.json — AthleteProfile
# ---------------------------------------------------------------------------

class Personal(LenientModel):
    name: str | None = None
    age: int | None = None
    max_hr: int | None = None
    resting_hr: int | None = None
    hr_zones: dict[str, Any] | None = None
    ftp: float | None = None
    threshold_pace_sec_per_km: float | None = None
    pace_zones: dict[str, Any] | None = None
    weight_kg: float | None = None
    power_zones: dict[str, Any] | None = None


class LifeConstraints(LenientModel):
    """Both 'no_training_days' (legacy) and 'blocked_days' are tolerated."""
    recurring_commitments: list[dict[str, Any]] | None = None
    preferred_training_times: list[str] | None = None
    no_training_days: list[str] | None = None
    blocked_days: list[str] | None = None
    available_days: list[str] | None = None
    work_schedule: dict[str, Any] | None = None
    travel: list[dict[str, Any]] | None = None


class InjuryRecord(LenientModel):
    """One injury_history entry.

    Real records use 'type' + 'restricted_activities'; some legacy records
    use 'name' + 'restrictions' (free-text list). Both are tolerated —
    rules.normalize_injury() canonicalizes at point of use.
    """
    date: str | None = None
    type: str | None = None
    name: str | None = None                       # legacy alias for type
    description: str | None = None
    location: str | None = None
    body_region: str | None = None
    severity: str | None = None
    status: str | None = None
    cause: str | None = None
    restricted_activities: list[str] | None = None  # optional by design
    restrictions: list[str] | None = None           # legacy alias
    safe_activities: list[str] | None = None
    treatment: str | None = None
    follow_up: str | None = None
    notes: str | None = None
    rehab_protocol: dict[str, Any] | None = None
    progress_notes: list[dict[str, Any]] | None = None


class Pillar(LenientModel):
    """A single training pillar. Both 'target_mins_per_week' and
    'target_minutes_per_week' spellings are tolerated (see rules.pillar_target_minutes)."""
    name: str | None = None
    description: str | None = None
    target_type: str | None = None  # 'sessions' | 'hours' | 'minutes'
    target_sessions_per_week: float | None = None
    target_hours_per_week: float | None = None
    target_mins_per_week: float | None = None
    target_minutes_per_week: float | None = None
    types: list[str] | None = None
    priority: str | None = None
    notes: str | None = None


class TrainingPillarsNested(LenientModel):
    """New-format training_pillars: metadata wrapper around a pillars list."""
    based_on_persona: str | None = None
    customized: bool | None = None
    last_updated: str | None = None
    pillars: list[Pillar] | None = None


# training_pillars appears in three shapes in the wild:
#   1. new nested dict:    {"based_on_persona": ..., "pillars": [...]}
#   2. legacy list:        [{"name": "strength", ...}, ...]
#   3. name-keyed dict:    {"strength": {...}, "mobility": {...}}
TrainingPillars = Union[TrainingPillarsNested, list[Pillar], dict[str, Any]]


class StrengthBaseline(LenientModel):
    """Strength tracking state. 'exercises' values are heterogeneous in the
    live file (some use current/history/progression, others flat
    current_weight_kg fields) so they stay free-form dicts."""
    equivalence_groups: dict[str, list[str]] | None = None
    exercises: dict[str, dict[str, Any]] | None = None
    last_synced: str | None = None


class AthleteProfile(LenientModel):
    """data/athlete.json — WHO the athlete is."""
    schema_version: int | None = None
    personal: Personal | None = None
    life_constraints: LifeConstraints | None = None
    injury_history: list[InjuryRecord] | None = None
    training_pillars: TrainingPillars | None = None
    strength_baseline: StrengthBaseline | None = None
    swimming: dict[str, Any] | None = None
    pilates: dict[str, Any] | None = None
    cycling_technique: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None
    coaching_notes: str | None = None
    baseline_last_refreshed: str | None = None


# ---------------------------------------------------------------------------
# weekly_plan.json — WeeklyPlan
# ---------------------------------------------------------------------------

class SessionExercise(LenientModel):
    name: str | None = None
    category: str | None = None
    sets: int | None = None
    reps: int | None = None
    rest_secs: int | None = None
    duration_secs: int | None = None
    notes: str | None = None


class Session(LenientModel):
    """One planned session. 'structure' entries are free-form (phases,
    distance/duration steps, nested repeat blocks) so they stay dicts.

    'type' is the one REQUIRED field — a session without a type can't be
    matched against actuals, gated against injuries, or pushed to Garmin.
    (This is the typed input contract for update_weekly_plan; storage
    validation of legacy files stays flag-only.)

    'intensity' is a free string. The special value 'discretion' marks an
    athlete-discretion day: the coach grants the athlete the choice of effort
    instead of the plan pretending an intensity was prescribed. Pair it with
    'constraints' to bound the choice (e.g. ['Z2 only', 'no running']).
    """
    type: str
    name: str | None = None
    duration_mins: float | None = None
    intensity: str | None = None       # free string; 'discretion' = athlete's choice
    constraints: list[str] | None = None  # bounds on a discretion day, e.g. ['Z2 only']
    purpose: str | None = None         # WHY this session matters (required by the
                                       # update_weekly_plan purpose gate for non-rest
                                       # sessions; Optional here so storage validation
                                       # of legacy files stays flag-only)
    description: str | None = None
    structure: list[dict[str, Any]] | None = None
    exercises: list[SessionExercise] | None = None


class PlanDay(LenientModel):
    """'planned' is a single session dict OR a list of sessions (both live)."""
    day_name: str | None = None
    planned: Union[list[Session], Session, None] = None
    actual: Any = None
    status: str | None = None
    notes: str | None = None


class WeeklyPlan(LenientModel):
    """data/weekly_plan.json — rolling 7-day plan, days keyed by ISO date."""
    schema_version: int | None = None
    days: dict[str, PlanDay] | None = None
    week_start: str | None = None
    week_end: str | None = None
    rationale: str | None = None
    pushed_workout_ids: list[int] | None = None
    generated_by: str | None = None
    last_updated: str | None = None

    @field_validator('days')
    @classmethod
    def _day_keys_are_iso_dates(cls, v: dict[str, PlanDay] | None) -> dict[str, PlanDay] | None:
        """FLAG (don't fix) non-ISO day keys — the raw data is still returned
        by storage; this only surfaces in the validation warning."""
        if v:
            for key in v:
                try:
                    date.fromisoformat(key)
                except (ValueError, TypeError):
                    raise ValueError(f"day key is not an ISO date: {key!r}")
        return v


# ---------------------------------------------------------------------------
# training_config.json — TrainingConfig
# ---------------------------------------------------------------------------

class RaceEvent(LenientModel):
    date: str | None = None
    name: str | None = None
    priority: str | None = None
    type: str | None = None
    distance_km: float | None = None
    target_time: Any = None
    url: str | None = None
    notes: str | None = None


class CurrentBlock(LenientModel):
    phase: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    weekly_volume_target_hrs: float | None = None
    focus: list[str] | None = None
    notes: str | None = None


class Periodization(LenientModel):
    current_phase: str | None = None
    phase_start: str | None = None
    target_transition: str | None = None
    a_race_date: str | None = None
    phases: dict[str, Any] | None = None


class Goal(LenientModel):
    id: str | None = None
    name: str | None = None
    type: str | None = None
    priority: str | None = None
    target_date: str | None = None
    description: str | None = None
    success_criteria: str | None = None
    key_metrics: list[Any] | None = None
    linked_event: str | None = None
    status: str | None = None
    achieved_date: str | None = None
    outcome: str | None = None


class TrainingConfig(LenientModel):
    """data/training_config.json — WHAT we're training for.

    Tolerates the derived-view keys that historically got written back into
    the live file (race_analysis, race_requirements, personas, pillars,
    athlete_pillars, pillars_source, weekly_structure, constraints,
    thresholds). Stored-state vs derived-view separation is Phase 1.3 work;
    until then they are valid content that must never be destroyed.
    """
    schema_version: int | None = None
    events: list[RaceEvent] | None = None
    periodization: Periodization | None = None
    current_block: CurrentBlock | None = None
    goals: list[Goal] | None = None
    # Derived-view pollution — tolerated:
    race_analysis: dict[str, Any] | None = None
    race_requirements: dict[str, Any] | None = None
    personas: dict[str, Any] | None = None
    pillars: dict[str, Any] | None = None
    athlete_pillars: dict[str, Any] | None = None
    pillars_source: str | None = None
    weekly_structure: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    thresholds: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# fitness_history.json — FitnessHistory
# ---------------------------------------------------------------------------

class DailyLoad(LenientModel):
    """v2 daily_loads value: {total, by_sport, activities[]}."""
    total: float | None = None
    by_sport: dict[str, float] | None = None
    activities: list[dict[str, Any]] | None = None


class SnapshotMetrics(LenientModel):
    ctl: float | None = None
    atl: float | None = None
    tsb: float | None = None
    acwr: float | None = None


class FitnessSnapshot(LenientModel):
    """Per-sport metric dicts (cycling/running/strength/...) ride along as
    extra keys — sport names are open-ended."""
    date: str | None = None
    total: SnapshotMetrics | None = None


class SleepNight(LenientModel):
    """bedtime/wake_time appear both as epoch-ms ints (legacy persistence)
    and ISO8601 strings — both tolerated."""
    date: str | None = None
    bedtime: Union[int, float, str, None] = None
    wake_time: Union[int, float, str, None] = None
    duration_hrs: float | None = None
    score: float | None = None
    deep_mins: float | None = None
    deep_pct: float | None = None
    rem_mins: float | None = None
    rem_pct: float | None = None
    light_mins: float | None = None
    light_pct: float | None = None
    awake_mins: float | None = None
    avg_hr: float | None = None
    respiration: float | None = None
    sleep_stress: float | None = None


class ReadinessEntry(LenientModel):
    date: str | None = None
    score: float | None = None
    level: str | None = None
    hrv_status: str | None = None
    body_battery: float | None = None


class FitnessHistory(LenientModel):
    """data/fitness_history.json — schema lineage owned by coach.fitness
    (v2 = sport-aware daily_loads). v1 flat-float daily_loads values are
    tolerated; coach.fitness.migrate_fitness_history converts them."""
    schema_version: int | None = None
    daily_loads: dict[str, Union[DailyLoad, float]] | None = None
    snapshots: list[FitnessSnapshot] | None = None
    sleep_history: list[SleepNight] | None = None
    readiness_history: list[ReadinessEntry] | None = None
    last_updated: str | None = None
    last_activity_ingest_date: str | None = None


# ---------------------------------------------------------------------------
# coaching_log.json — CoachingLog
# ---------------------------------------------------------------------------

class CoachingDecision(LenientModel):
    id: str | None = None
    date: str | None = None
    type: str | None = None
    decision: str | None = None
    rationale: str | None = None
    status: str | None = None
    outcome: str | None = None
    review_date: str | None = None
    status_updated: str | None = None


class AthleteResponse(LenientModel):
    date: str | None = None
    stimulus: str | None = None
    response: str | None = None
    pattern: str | None = None


class CoachingLog(LenientModel):
    """data/coaching_log.json — persistent coaching memory."""
    schema_version: int | None = None
    decisions: list[CoachingDecision] | None = None
    pending_approvals: list[dict[str, Any]] | None = None
    athlete_responses: list[AthleteResponse] | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# methodology.json — Methodology
# ---------------------------------------------------------------------------

class RaceTemplate(LenientModel):
    description: str | None = None
    key_sessions: list[dict[str, Any]] | None = None
    phase_guidance: dict[str, Any] | None = None


class Methodology(LenientModel):
    """data/methodology.json — HOW to train (rarely changes)."""
    schema_version: int | None = None
    default_pillar_templates: dict[str, Any] | None = None
    personas: dict[str, Any] | None = None
    safety_constraints: dict[str, Any] | None = None
    race_templates: dict[str, RaceTemplate] | None = None
    activity_classification: dict[str, Any] | None = None
    recovery_protocols: dict[str, Any] | None = None
    training_protocols: dict[str, Any] | None = None
    pillars: dict[str, Any] | None = None
    strength_programs: dict[str, Any] | None = None
    session_guidelines: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# athlete_baseline.json — AthleteBaseline
# ---------------------------------------------------------------------------

class BaselineCapacity(LenientModel):
    avg_weekly_volume_hrs: float | None = None
    max_weekly_volume_hrs: float | None = None
    activity_distribution: dict[str, int] | None = None
    typical_week: dict[str, float] | None = None
    total_activities: int | None = None
    weeks_analyzed: int | None = None


class PersonalRecord(LenientModel):
    record_type: str | None = None
    value: float | None = None
    value_formatted: Any = None
    unit: str | None = None
    date: str | None = None
    activity_id: int | None = None


class GarminProfile(LenientModel):
    full_name: str | None = None
    display_name: str | None = None
    weight_kg: float | None = None
    weight_date: str | None = None
    birth_date: str | None = None
    age: int | None = None
    max_hr: int | None = None
    hr_zones: dict[str, Any] | None = None


class AthleteBaseline(LenientModel):
    """data/athlete_baseline.json — Garmin-derived capacity (auto-generated)."""
    schema_version: int | None = None
    last_refreshed: str | None = None
    baseline: BaselineCapacity | None = None
    personal_records: list[PersonalRecord] | None = None
    garmin_profile: GarminProfile | None = None


# ---------------------------------------------------------------------------
# exercise_library.json — ExerciseLibrary
# ---------------------------------------------------------------------------

class ExerciseLibrary(RootModel[dict[str, dict[str, Any]]]):
    """data/exercise_library.json — flat mapping of exercise name -> form-cue
    entry. Entries are heterogeneous (two generations of research_exercise
    output live side by side) so values stay free-form dicts.

    NOTE: this file is a root mapping, so it does NOT get a schema_version
    key stamped into it (that would pollute the exercise namespace for
    consumers that iterate keys, e.g. list_exercises).
    """
    root: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Filename -> model registry (consumed by coach.storage)
# ---------------------------------------------------------------------------

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    'athlete.json': AthleteProfile,
    'athlete_baseline.json': AthleteBaseline,
    'training_config.json': TrainingConfig,
    'methodology.json': Methodology,
    'weekly_plan.json': WeeklyPlan,
    'coaching_log.json': CoachingLog,
    'fitness_history.json': FitnessHistory,
    'exercise_library.json': ExerciseLibrary,
}
