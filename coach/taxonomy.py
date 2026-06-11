"""Canonical activity-type taxonomy — the single registry for activity vocabulary.

At least five vocabularies coexisted before this module:
  - plan session types ("strength", "long_ride", "mtb", ...)
  - Garmin activity typeKeys ("strength_training", "mountain_biking", ...)
  - methodology.json activity_classification sets
  - config.SPORT_GROUPS / workout_builder CYCLING_TYPES, RUNNING_TYPES, ...
  - three diverging race-type -> sport maps

This module is the one place that knows how those vocabularies relate.
It is intentionally a *leaf* module: it imports nothing from the rest of
the coach package so that config/rules/tools can all depend on it.

Public API:
  canonical_type(t)        -> canonical name for any known alias/Garmin type
  is_known_type(t)         -> True when the registry recognizes the name
  types_match(plan, garmin)-> True when a plan type and a Garmin type refer
                              to the same kind of training
  types_match_with_name(plan, garmin, name)
                           -> types_match plus a name-hint fallback for
                              mobility logged as Garmin type 'other'
  is_mobility_by_name(garmin, name)
                           -> True when an unclassifiable Garmin type's
                              activity NAME marks it as mobility work
  pillar_for(t)            -> 'strength' | 'mobility' | 'long_effort' | None
  sport_group_for(t)       -> 'cycling' | 'running' | 'strength' | 'other'
  workout_family_for(t)    -> builder family ('cycling', 'running', 'strength',
                              'yoga', 'pilates', 'swimming', 'padel', 'rest',
                              'other')
  race_sport_for(race_type)-> sport for a race type ('cycling', 'running',
                              'triathlon', 'swimming', 'multi_sport') or None
  types_in_family(family)  -> frozenset of every name in a workout family
  garmin_types_in_sport_group(group) -> Garmin typeKeys for a sport group
  pillar_types(pillar)     -> frozenset of every name contributing to a pillar
  high_intensity_types()   -> frozenset of inherently high-intensity names
"""
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityType:
    """One canonical activity type and everything that maps onto it.

    canonical:      the canonical name (usually the primary Garmin typeKey)
    garmin_types:   Garmin activity typeKeys that report as this type
    plan_aliases:   plan-session / race-template spellings of this type
    pillar:         training pillar this type contributes to
                    ('strength' | 'mobility' | 'long_effort' | None).
                    'long_effort' types only count toward the pillar when the
                    session is long enough (rules.classify_activity applies
                    the duration threshold).
    sport_group:    sport-specific load bucket ('cycling' | 'running' |
                    'strength' | 'other') — must stay aligned with
                    fitness.calculate_sport_fitness_metrics consumers.
    workout_family: which workout_builder builder handles this type
                    ('cycling' | 'running' | 'strength' | 'yoga' | 'pilates' |
                    'swimming' | 'padel' | 'rest' | 'other'). 'other' means
                    the type cannot be pushed to Garmin as a workout.
    high_intensity: inherently hard regardless of HR (games, intervals).
    """
    canonical: str
    garmin_types: tuple = ()
    plan_aliases: tuple = ()
    pillar: str | None = None
    sport_group: str = 'other'
    workout_family: str = 'other'
    high_intensity: bool = False

    def all_names(self) -> frozenset:
        return frozenset((self.canonical,) + self.garmin_types + self.plan_aliases)


REGISTRY: tuple = (
    # ----- cycling ---------------------------------------------------------
    ActivityType(
        canonical='cycling',
        garmin_types=('cycling', 'road_biking', 'virtual_ride', 'e_bike_fitness'),
        plan_aliases=('ride', 'road_ride', 'easy_ride', 'tempo_ride',
                      'recovery_ride', 'long_ride', 'endurance_ride',
                      'tempo_intervals', 'tempo_climbs', 'back_to_back_rides'),
        pillar='long_effort', sport_group='cycling', workout_family='cycling',
    ),
    ActivityType(
        canonical='mountain_biking',
        garmin_types=('mountain_biking', 'e_bike_mountain'),
        plan_aliases=('mtb', 'mtb_ride', 'long_mtb_ride'),
        pillar='long_effort', sport_group='cycling', workout_family='cycling',
    ),
    ActivityType(
        canonical='gravel_cycling',
        garmin_types=('gravel_cycling',),
        plan_aliases=('gravel', 'gravel_ride'),
        pillar='long_effort', sport_group='cycling', workout_family='cycling',
    ),
    ActivityType(
        canonical='indoor_cycling',
        garmin_types=('indoor_cycling',),
        plan_aliases=('wattbike', 'trainer', 'turbo_trainer',
                      'ftp_test', 'threshold_test', 'indoor_technique'),
        pillar='long_effort', sport_group='cycling', workout_family='cycling',
    ),
    # ----- running ---------------------------------------------------------
    ActivityType(
        canonical='running',
        garmin_types=('running', 'treadmill_running'),
        plan_aliases=('run', 'easy_run', 'long_run', 'recovery_run',
                      'tempo_run', 'interval_run', 'vertical_work'),
        pillar='long_effort', sport_group='running', workout_family='running',
    ),
    ActivityType(
        canonical='trail_running',
        garmin_types=('trail_running',),
        plan_aliases=('trail_run', 'long_trail_run', 'back_to_back_long'),
        pillar='long_effort', sport_group='running', workout_family='running',
    ),
    ActivityType(
        canonical='track_running',
        garmin_types=('track_running',),
        plan_aliases=('track_run', 'track_workout'),
        pillar='long_effort', sport_group='running', workout_family='running',
        high_intensity=True,
    ),
    # ----- strength --------------------------------------------------------
    ActivityType(
        canonical='strength_training',
        garmin_types=('strength_training', 'functional_strength'),
        plan_aliases=('strength', 'gym', 'weights', 'lifting',
                      'strength_plus_rehab'),
        pillar='strength', sport_group='strength', workout_family='strength',
    ),
    ActivityType(
        canonical='indoor_cardio',
        garmin_types=('indoor_cardio',),
        pillar='strength', sport_group='strength', workout_family='strength',
    ),
    # ----- mobility --------------------------------------------------------
    ActivityType(
        canonical='yoga',
        garmin_types=('yoga',),
        plan_aliases=('mobility',),
        pillar='mobility', workout_family='yoga',
    ),
    ActivityType(
        canonical='stretching',
        garmin_types=('stretching',),
        plan_aliases=('stretch', 'foam_rolling'),
        pillar='mobility', workout_family='yoga',
    ),
    ActivityType(
        canonical='pilates',
        garmin_types=('pilates',),
        plan_aliases=('rehab', 'rehabilitation'),
        pillar='mobility', workout_family='pilates',
    ),
    ActivityType(
        canonical='breathwork',
        garmin_types=('breathwork',),
        plan_aliases=('breathing',),
        pillar='mobility', workout_family='other',  # no Garmin workout builder
    ),
    # ----- swimming --------------------------------------------------------
    ActivityType(
        canonical='swimming',
        garmin_types=('swimming', 'lap_swimming'),
        plan_aliases=('swim', 'pool'),
        pillar='long_effort', workout_family='swimming',
    ),
    ActivityType(
        canonical='open_water_swimming',
        garmin_types=('open_water_swimming',),
        plan_aliases=('open_water_swim',),
        pillar='long_effort', workout_family='swimming',
    ),
    # ----- racquet / games -------------------------------------------------
    ActivityType(
        canonical='padel',
        garmin_types=('paddelball',),
        plan_aliases=('padel', 'paddle'),
        workout_family='padel',
    ),
    ActivityType(
        canonical='ultimate_disc',
        garmin_types=('ultimate_disc',),
        plan_aliases=('ultimate', 'frisbee'),
        high_intensity=True,
    ),
    # ----- high-intensity conditioning --------------------------------------
    ActivityType(
        canonical='hiit',
        garmin_types=('hiit',),
        high_intensity=True,
    ),
    ActivityType(
        canonical='interval_training',
        plan_aliases=('intervals',),
        high_intensity=True,
    ),
    ActivityType(
        canonical='agility_work',
        plan_aliases=('agility',),
    ),
    # ----- low-intensity / other Garmin types --------------------------------
    ActivityType(
        canonical='walking',
        garmin_types=('walking', 'casual_walking', 'speed_walking'),
    ),
    ActivityType(
        canonical='hiking',
        garmin_types=('hiking',),
    ),
    ActivityType(
        canonical='rowing',
        garmin_types=('rowing', 'indoor_rowing'),
    ),
    # ----- rest -------------------------------------------------------------
    ActivityType(
        canonical='rest',
        plan_aliases=('rest_day', 'rest_or_easy', 'off', 'day_off',
                      'recovery_day'),
        workout_family='rest',
    ),
)


# Race type -> sport. Union of the three previously-diverging maps
# (config.RACE_TYPE_SPORT_MAP, coaching_tools._analyze_sport_priorities,
# and CTL_TARGETS race-type keys), plus the race types suggested by
# race_tools.add_race docs.
RACE_TYPE_TO_SPORT: dict = {
    # cycling
    'multi_day_mtb': 'cycling', 'road_cycling': 'cycling', 'mtb': 'cycling',
    'gravel': 'cycling', 'cycling': 'cycling', 'mountain_biking': 'cycling',
    'gravel_cycling': 'cycling',
    # running
    'trail_ultra': 'running', 'running_marathon': 'running',
    'running_half': 'running', 'running_ultra': 'running',
    'marathon': 'running', 'half_marathon': 'running',
    '10k': 'running', '5k': 'running', 'running': 'running',
    'trail_running': 'running',
    # triathlon
    'triathlon': 'triathlon', 'triathlon_olympic': 'triathlon',
    'triathlon_half': 'triathlon', 'triathlon_full': 'triathlon',
    # swimming
    'swimming': 'swimming', 'open_water_swimming': 'swimming',
    # multi-sport tournaments (ultimate, padel)
    'tournament': 'multi_sport',
}


def _normalize(name) -> str:
    """Lowercase + underscore normalization for type lookups."""
    if name is None:
        return ''
    return str(name).strip().lower().replace(' ', '_').replace('-', '_')


def _build_lookup() -> dict:
    lookup: dict = {}
    for entry in REGISTRY:
        for name in entry.all_names():
            normalized = _normalize(name)
            existing = lookup.get(normalized)
            if existing is not None and existing is not entry:
                raise ValueError(
                    f"Taxonomy collision: {normalized!r} maps to both "
                    f"{existing.canonical!r} and {entry.canonical!r}"
                )
            lookup[normalized] = entry
    return lookup


_LOOKUP: dict = _build_lookup()


def _entry(activity_type) -> ActivityType | None:
    """Registry entry for any known canonical name, Garmin type, or alias."""
    return _LOOKUP.get(_normalize(activity_type))


def canonical_type(activity_type) -> str:
    """Canonical name for an activity type.

    Unknown types pass through normalized (lowercased, underscored) so
    callers can still compare them by string equality.
    """
    entry = _entry(activity_type)
    if entry is not None:
        return entry.canonical
    return _normalize(activity_type)


def is_known_type(activity_type) -> bool:
    """True when the registry recognizes the name (canonical, Garmin, alias)."""
    return _entry(activity_type) is not None


def pillar_for(activity_type) -> str | None:
    """Training pillar a type contributes to, or None.

    'long_effort' means the type *can* count toward the long-effort pillar —
    callers must still apply the duration threshold.
    """
    entry = _entry(activity_type)
    return entry.pillar if entry is not None else None


def sport_group_for(activity_type) -> str:
    """Sport group ('cycling' | 'running' | 'strength' | 'other')."""
    entry = _entry(activity_type)
    return entry.sport_group if entry is not None else 'other'


def workout_family_for(activity_type) -> str:
    """Workout-builder family, or 'other' when not pushable to Garmin."""
    entry = _entry(activity_type)
    return entry.workout_family if entry is not None else 'other'


def is_high_intensity(activity_type) -> bool:
    """True for inherently high-intensity types (games, intervals)."""
    entry = _entry(activity_type)
    return entry.high_intensity if entry is not None else False


def _match_key(activity_type) -> str:
    """Comparison key for types_match.

    Two types refer to the same kind of training when they share a sport
    group (cycling/running/strength), a non-cardio pillar (strength/mobility),
    or a sport-distinct workout family (swimming/padel). Everything else only
    matches its own canonical name; unknown types match on raw string only.
    """
    entry = _entry(activity_type)
    if entry is None:
        return f'raw:{_normalize(activity_type)}'
    if entry.sport_group in ('cycling', 'running', 'strength'):
        return f'group:{entry.sport_group}'
    if entry.pillar in ('strength', 'mobility'):
        return f'pillar:{entry.pillar}'
    if entry.workout_family in ('swimming', 'padel'):
        return f'family:{entry.workout_family}'
    return f'canonical:{entry.canonical}'


def types_match(plan_type, garmin_type) -> bool:
    """True when a plan session type and a Garmin activity type refer to the
    same kind of training.

    Examples:
        types_match('strength', 'strength_training')  -> True
        types_match('long_ride', 'mountain_biking')   -> True
        types_match('mobility', 'pilates')            -> True
        types_match('long_ride', 'running')           -> False
    """
    if not plan_type or not garmin_type:
        return False
    return _match_key(plan_type) == _match_key(garmin_type)


# Garmin logs mobility sessions as type 'other' with names like
# "Cape Town Mobility" — the type carries no signal, the NAME does.
MOBILITY_NAME_HINTS = ('mobility', 'stretch', 'yoga', 'pilates', 'foam roll')


def is_mobility_by_name(garmin_type, activity_name) -> bool:
    """True when an unclassifiable Garmin type's NAME marks it as mobility.

    Only fires when the type itself is unknown to the registry (e.g. Garmin
    'other') — a known type always speaks for itself, so 'walking' named
    "Mobility walk" is still walking.
    """
    if not activity_name or _entry(garmin_type) is not None:
        return False
    name = str(activity_name).lower()
    return any(hint in name for hint in MOBILITY_NAME_HINTS)


def types_match_with_name(plan_type, garmin_type, activity_name=None) -> bool:
    """types_match plus a name-hint fallback for the mobility family.

    Examples:
        types_match_with_name('mobility', 'yoga', None)              -> True
        types_match_with_name('mobility', 'other', 'CT Mobility')    -> True
        types_match_with_name('mobility', 'other', 'Random Workout') -> False
        types_match_with_name('padel', 'other', 'CT Mobility')       -> False
    """
    if types_match(plan_type, garmin_type):
        return True
    entry = _entry(plan_type)
    if entry is None or entry.pillar != 'mobility':
        return False
    return is_mobility_by_name(garmin_type, activity_name)


def race_sport_for(race_type) -> str | None:
    """Sport for a race/event type, or None when unknown.

    Falls back to the activity-type sport group for race types that are
    actually activity types (e.g. 'track_running' -> 'running').
    """
    normalized = _normalize(race_type)
    if not normalized:
        return None
    sport = RACE_TYPE_TO_SPORT.get(normalized)
    if sport is not None:
        return sport
    group = sport_group_for(normalized)
    if group in ('cycling', 'running'):
        return group
    return None


def types_in_family(family: str) -> frozenset:
    """Every name (canonical + Garmin + aliases) in a workout family."""
    names: set = set()
    for entry in REGISTRY:
        if entry.workout_family == family:
            names |= entry.all_names()
    return frozenset(names)


def types_in_sport_group(group: str) -> frozenset:
    """Every name (canonical + Garmin + aliases) in a sport group."""
    names: set = set()
    for entry in REGISTRY:
        if entry.sport_group == group:
            names |= entry.all_names()
    return frozenset(names)


def garmin_types_in_sport_group(group: str) -> list:
    """Garmin typeKeys reporting under a sport group (for SPORT_GROUPS)."""
    names: list = []
    for entry in REGISTRY:
        if entry.sport_group == group:
            names.extend(entry.garmin_types)
    return names


def pillar_types(pillar: str) -> frozenset:
    """Every name (canonical + Garmin + aliases) contributing to a pillar."""
    names: set = set()
    for entry in REGISTRY:
        if entry.pillar == pillar:
            names |= entry.all_names()
    return frozenset(names)


def high_intensity_types() -> frozenset:
    """Every name of inherently high-intensity types."""
    names: set = set()
    for entry in REGISTRY:
        if entry.high_intensity:
            names |= entry.all_names()
    return frozenset(names)
