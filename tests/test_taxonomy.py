"""Tests for the canonical activity-type taxonomy (coach/taxonomy.py) and its
wiring into rules, coaching_tools, and workout_builder.

Covers:
  - registry integrity (no collisions, valid field values)
  - coverage of every vocabulary the taxonomy replaced (SPORT_GROUPS,
    methodology classification sets, workout_builder type sets, race maps)
  - types_match truth table
  - plan_adherence / planned-vs-actual integration (plan "strength" +
    Garmin "strength_training" => completed, not type_mismatch)
  - workout_builder mountain_biking/gravel/e-bike dispatch (push-skip fix)
  - check_safety_rules day-counting golden cases
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from coach import taxonomy
from coach.taxonomy import (
    REGISTRY,
    canonical_type,
    types_match,
    pillar_for,
    sport_group_for,
    workout_family_for,
    race_sport_for,
    types_in_family,
    garmin_types_in_sport_group,
    pillar_types,
    high_intensity_types,
)
import coach.rules as rules
from coach.rules import check_safety_rules, classify_activity
from coach.tools.coaching_tools import (
    _compare_planned_actual,
    _summarize_plan_adherence_by_pillar,
    _analyze_sport_priorities,
)
from coach.workout_builder import (
    build_workout,
    get_workout_type_name,
    CYCLING_TYPES,
    RUNNING_TYPES,
    YOGA_TYPES,
    PILATES_TYPES,
    STRENGTH_TYPES,
    SWIMMING_TYPES,
    PADEL_TYPES,
    SKIP_TYPES,
)

try:
    from garminconnect.workout import CyclingWorkout
except ImportError:  # pragma: no cover
    CyclingWorkout = None


TODAY = date.today()


@pytest.fixture
def empty_data_dir(data_dir, monkeypatch):
    """Redirect rules.DATA_DIR to an empty tmp dir so classification uses
    pure taxonomy defaults (no live methodology.json)."""
    monkeypatch.setattr(rules, 'DATA_DIR', data_dir)
    return data_dir


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

class TestRegistryIntegrity:
    VALID_PILLARS = {None, 'strength', 'mobility', 'long_effort'}
    VALID_SPORT_GROUPS = {'cycling', 'running', 'strength', 'other'}
    VALID_FAMILIES = {'cycling', 'running', 'strength', 'yoga', 'pilates',
                      'swimming', 'padel', 'rest', 'other'}

    def test_no_name_collisions_across_entries(self):
        seen = {}
        for entry in REGISTRY:
            for name in entry.all_names():
                assert name not in seen or seen[name] == entry.canonical, (
                    f"{name!r} appears in both {seen[name]!r} and {entry.canonical!r}"
                )
                seen[name] = entry.canonical

    def test_field_values_are_valid(self):
        for entry in REGISTRY:
            assert entry.pillar in self.VALID_PILLARS, entry.canonical
            assert entry.sport_group in self.VALID_SPORT_GROUPS, entry.canonical
            assert entry.workout_family in self.VALID_FAMILIES, entry.canonical

    def test_all_names_lowercase_snake_case(self):
        for entry in REGISTRY:
            for name in entry.all_names():
                assert name == name.lower(), name
                assert ' ' not in name and '-' not in name, name


# ---------------------------------------------------------------------------
# Coverage: every vocabulary the taxonomy replaced must map
# ---------------------------------------------------------------------------

class TestVocabularyCoverage:
    # Golden copies of the vocabularies the registry consolidated.
    LEGACY_SPORT_GROUPS = {
        'cycling': ['cycling', 'mountain_biking', 'indoor_cycling',
                    'virtual_ride', 'gravel_cycling', 'road_biking'],
        'running': ['running', 'trail_running', 'treadmill_running',
                    'track_running'],
        'strength': ['strength_training', 'indoor_cardio', 'functional_strength'],
    }
    METHODOLOGY_CLASSIFICATION = {
        'strength_types': ['strength_training', 'indoor_cardio', 'functional_strength'],
        'mobility_types': ['yoga', 'pilates', 'stretching', 'breathwork'],
        'cardio_types': ['running', 'cycling', 'swimming', 'trail_running',
                         'open_water_swimming'],
        'high_intensity_types': ['ultimate_disc', 'hiit', 'interval_training',
                                 'track_running'],
    }
    LEGACY_WORKOUT_SETS = {
        'cycling': ['long_ride', 'easy_ride', 'cycling', 'ride', 'mtb',
                    'road_ride', 'ftp_test', 'indoor_cycling', 'wattbike',
                    'trainer', 'tempo_ride'],
        'running': ['run', 'long_run', 'easy_run', 'running', 'trail_run',
                    'interval_run'],
        'yoga': ['yoga', 'mobility', 'stretching'],
        'pilates': ['pilates', 'rehab', 'rehabilitation'],
        'strength': ['strength', 'strength_training', 'gym', 'weights',
                     'strength_plus_rehab'],
        'swimming': ['swim', 'swimming', 'pool'],
        'padel': ['padel', 'paddelball', 'paddle'],
        'rest': ['rest', 'rest_or_easy'],
    }
    RACE_TEMPLATE_KEY_SESSIONS = [
        # methodology.json race_templates[*].key_sessions[*].type
        'long_trail_run', 'back_to_back_long', 'vertical_work', 'strength',
        'long_mtb_ride', 'back_to_back_rides', 'tempo_climbs',
        'indoor_technique', 'long_ride', 'tempo_intervals',
        'interval_training', 'agility_work',
    ]
    LEGACY_RACE_MAPS = {
        # config.RACE_TYPE_SPORT_MAP (pre-taxonomy)
        'multi_day_mtb': 'cycling', 'road_cycling': 'cycling',
        'trail_ultra': 'running', 'running_marathon': 'running',
        'running_half': 'running', 'running_ultra': 'running',
        # coaching_tools._analyze_sport_priorities sport_mapping (pre-taxonomy)
        'marathon': 'running', 'half_marathon': 'running',
        '10k': 'running', '5k': 'running',
        'triathlon': 'triathlon', 'swimming': 'swimming',
        'tournament': 'multi_sport',
    }

    def test_legacy_sport_groups_preserved(self):
        for group, type_list in self.LEGACY_SPORT_GROUPS.items():
            for t in type_list:
                assert sport_group_for(t) == group, t
            # Garmin typeKeys for the group must still include the old list
            garmin = set(garmin_types_in_sport_group(group))
            for t in type_list:
                assert t in garmin, f"{t} dropped from SPORT_GROUPS[{group}]"

    def test_methodology_classification_preserved(self):
        for t in self.METHODOLOGY_CLASSIFICATION['strength_types']:
            assert pillar_for(t) == 'strength', t
        for t in self.METHODOLOGY_CLASSIFICATION['mobility_types']:
            assert pillar_for(t) == 'mobility', t
        for t in self.METHODOLOGY_CLASSIFICATION['cardio_types']:
            assert pillar_for(t) == 'long_effort', t
        hit = high_intensity_types()
        for t in self.METHODOLOGY_CLASSIFICATION['high_intensity_types']:
            assert t in hit, t

    def test_legacy_workout_sets_preserved(self):
        for family, type_list in self.LEGACY_WORKOUT_SETS.items():
            family_names = types_in_family(family)
            for t in type_list:
                assert t in family_names, f"{t} not in family {family}"

    def test_race_template_session_types_known(self):
        for t in self.RACE_TEMPLATE_KEY_SESSIONS:
            assert taxonomy._entry(t) is not None, (
                f"race-template session type {t!r} unknown to taxonomy"
            )

    def test_legacy_race_maps_unified(self):
        for race_type, sport in self.LEGACY_RACE_MAPS.items():
            assert race_sport_for(race_type) == sport, race_type

    def test_ctl_target_race_types_map(self):
        from coach.config import CTL_TARGETS
        for race_type in CTL_TARGETS:
            if race_type in ('casual', 'default'):
                continue
            assert race_sport_for(race_type) is not None, race_type

    def test_workout_builder_sets_are_supersets_of_legacy(self):
        legacy_to_current = {
            'cycling': CYCLING_TYPES, 'running': RUNNING_TYPES,
            'yoga': YOGA_TYPES, 'pilates': PILATES_TYPES,
            'strength': STRENGTH_TYPES, 'swimming': SWIMMING_TYPES,
            'padel': PADEL_TYPES, 'rest': SKIP_TYPES,
        }
        for family, current in legacy_to_current.items():
            for t in self.LEGACY_WORKOUT_SETS[family]:
                assert t in current, f"{t} dropped from workout_builder {family} set"

    def test_garmin_fixture_activity_types_map(self, garmin_fixtures):
        """Every Garmin activity typeKey in the captured fixtures must be known."""
        activities = garmin_fixtures.get('activities') or []
        if not activities:
            pytest.skip("no activities captured in test_fixtures.json")
        for raw in activities:
            type_key = (raw.get('activityType') or {}).get('typeKey', '')
            assert taxonomy._entry(type_key) is not None, (
                f"Garmin type {type_key!r} missing from taxonomy registry"
            )


# ---------------------------------------------------------------------------
# canonical_type / helper lookups
# ---------------------------------------------------------------------------

class TestCanonicalLookups:
    def test_garmin_type_resolves_to_canonical(self):
        assert canonical_type('road_biking') == 'cycling'
        assert canonical_type('functional_strength') == 'strength_training'
        assert canonical_type('paddelball') == 'padel'

    def test_plan_alias_resolves_to_canonical(self):
        assert canonical_type('strength') == 'strength_training'
        assert canonical_type('long_ride') == 'cycling'
        assert canonical_type('mtb') == 'mountain_biking'
        assert canonical_type('rehab') == 'pilates'

    def test_unknown_passes_through_normalized(self):
        assert canonical_type('Basketball Game') == 'basketball_game'

    def test_case_and_separator_insensitive(self):
        assert canonical_type('Mountain Biking') == 'mountain_biking'
        assert canonical_type('STRENGTH_TRAINING') == 'strength_training'

    def test_pillar_for(self):
        assert pillar_for('strength') == 'strength'
        assert pillar_for('mountain_biking') == 'long_effort'
        assert pillar_for('yoga') == 'mobility'
        assert pillar_for('padel') is None
        assert pillar_for('unknown_thing') is None

    def test_sport_group_for(self):
        assert sport_group_for('long_ride') == 'cycling'
        assert sport_group_for('treadmill_running') == 'running'
        assert sport_group_for('gym') == 'strength'
        assert sport_group_for('yoga') == 'other'
        assert sport_group_for('unknown_thing') == 'other'

    def test_workout_family_for(self):
        assert workout_family_for('mountain_biking') == 'cycling'
        assert workout_family_for('gravel_cycling') == 'cycling'
        assert workout_family_for('e_bike_fitness') == 'cycling'
        assert workout_family_for('trail_run') == 'running'
        assert workout_family_for('mobility') == 'yoga'
        assert workout_family_for('rest_day') == 'rest'
        assert workout_family_for('breathwork') == 'other'

    def test_race_sport_for_unknown_returns_none(self):
        assert race_sport_for('chess_boxing') is None
        assert race_sport_for(None) is None
        assert race_sport_for('') is None

    def test_race_sport_for_activity_type_fallback(self):
        # Race typed with an activity type falls back to its sport group
        assert race_sport_for('track_running') == 'running'
        assert race_sport_for('indoor_cycling') == 'cycling'


# ---------------------------------------------------------------------------
# types_match truth table
# ---------------------------------------------------------------------------

class TestTypesMatch:
    TRUTH_TABLE = [
        # (plan_type, garmin_type, expected)
        ('strength', 'strength_training', True),
        ('strength', 'functional_strength', True),
        ('strength', 'indoor_cardio', True),
        ('gym', 'strength_training', True),
        ('long_ride', 'cycling', True),
        ('long_ride', 'mountain_biking', True),
        ('mtb', 'gravel_cycling', True),
        ('easy_ride', 'virtual_ride', True),
        ('indoor_cycling', 'cycling', True),
        ('long_run', 'running', True),
        ('long_run', 'trail_running', True),
        ('run', 'treadmill_running', True),
        ('yoga', 'pilates', True),          # both mobility pillar
        ('mobility', 'stretching', True),
        ('rehab', 'yoga', True),
        ('swim', 'lap_swimming', True),
        ('swim', 'open_water_swimming', True),
        ('padel', 'paddelball', True),
        ('ultimate', 'ultimate_disc', True),
        ('cycling', 'cycling', True),
        # cross-sport must NOT match
        ('long_ride', 'running', False),
        ('long_run', 'cycling', False),
        ('strength', 'cycling', False),
        ('strength', 'yoga', False),
        ('swim', 'running', False),
        ('padel', 'ultimate_disc', False),
        ('rest', 'cycling', False),
        ('race', 'cycling', False),          # unknown plan type, no match
        # unknown types only match themselves
        ('basketball', 'basketball', True),
        ('basketball', 'cycling', False),
    ]

    @pytest.mark.parametrize("plan_type,garmin_type,expected", TRUTH_TABLE)
    def test_truth_table(self, plan_type, garmin_type, expected):
        assert types_match(plan_type, garmin_type) is expected

    def test_symmetric(self):
        for plan_type, garmin_type, expected in self.TRUTH_TABLE:
            assert types_match(garmin_type, plan_type) is expected

    def test_empty_or_none_never_match(self):
        assert types_match('', 'cycling') is False
        assert types_match('cycling', '') is False
        assert types_match(None, 'cycling') is False
        assert types_match('', '') is False

    def test_case_insensitive(self):
        assert types_match('Strength', 'STRENGTH_TRAINING') is True


# ---------------------------------------------------------------------------
# classify_activity integration (plan aliases + Garmin types)
# ---------------------------------------------------------------------------

class TestClassifyActivityTaxonomy:
    def test_plan_alias_strength_classifies(self, empty_data_dir):
        result = classify_activity({'type': 'strength', 'duration_mins': 45})
        assert result['is_strength'] is True

    def test_mountain_biking_long_ride_is_long_effort(self, empty_data_dir):
        result = classify_activity({'type': 'mountain_biking', 'duration_mins': 120})
        assert result['is_long_effort'] is True

    def test_plan_long_ride_is_long_effort(self, empty_data_dir):
        result = classify_activity({'type': 'long_ride', 'duration_mins': 150})
        assert result['is_long_effort'] is True

    def test_short_mountain_bike_not_long_effort(self, empty_data_dir):
        result = classify_activity({'type': 'mountain_biking', 'duration_mins': 30})
        assert result['is_long_effort'] is False

    def test_rehab_classifies_as_mobility(self, empty_data_dir):
        result = classify_activity({'type': 'rehab', 'duration_mins': 20})
        assert result['is_mobility'] is True

    def test_methodology_custom_types_still_extend(self, empty_data_dir):
        """User-added classification types in methodology.json union with taxonomy."""
        (empty_data_dir / 'methodology.json').write_text(json.dumps({
            'activity_classification': {'strength_types': ['boulder_session']},
        }), encoding='utf-8')
        result = classify_activity({'type': 'boulder_session', 'duration_mins': 60})
        assert result['is_strength'] is True
        # Canonical taxonomy types still classify despite the custom override
        result2 = classify_activity({'type': 'strength_training', 'duration_mins': 45})
        assert result2['is_strength'] is True


# ---------------------------------------------------------------------------
# planned-vs-actual + plan adherence integration
# ---------------------------------------------------------------------------

class TestPlannedVsActualTaxonomy:
    def test_strength_plan_matches_strength_training_activity(self, empty_data_dir):
        """Plan 'strength' + Garmin 'strength_training' => matched, NOT type_mismatch."""
        plan = {'days': {
            '2026-01-13': {'planned': {'type': 'strength', 'duration_mins': 45}},
        }}
        activities = [
            {'date': '2026-01-13', 'type': 'strength_training', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        mismatches = [a for a in result['anomalies'] if a['flag'] == 'type_mismatch']
        assert mismatches == []
        assert result['details'][0]['status'] == 'matched'

    def test_long_ride_plan_matches_mountain_biking(self, empty_data_dir):
        plan = {'days': {
            '2026-01-13': {'planned': {'type': 'long_ride', 'duration_mins': 120}},
        }}
        activities = [
            {'date': '2026-01-13', 'type': 'mountain_biking', 'duration_mins': 118},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['details'][0]['status'] == 'matched'
        assert result['anomalies'] == []

    def test_cross_sport_yields_missing_plus_unplanned(self, empty_data_dir):
        """A taxonomy-known plan type (long_run) is never paired with a
        non-matching actual (cycling) — that crosswire is the June 2026
        defect. The honest verdict is missing + unplanned."""
        plan = {'days': {
            '2026-01-13': {'planned': {'type': 'long_run', 'duration_mins': 60}},
        }}
        activities = [
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 60},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert [a['flag'] for a in result['anomalies']].count('type_mismatch') == 0
        missing = [a for a in result['anomalies'] if a['flag'] == 'missing']
        assert len(missing) == 1 and missing[0]['planned_type'] == 'long_run'
        unplanned = [a for a in result['anomalies'] if a['flag'] == 'unplanned']
        assert len(unplanned) == 1 and unplanned[0]['activity_type'] == 'cycling'

    def test_taxonomy_match_preferred_over_first_activity_fallback(self, empty_data_dir):
        """With a run + a ride on the same day, plan 'long_ride' picks the ride."""
        plan = {'days': {
            '2026-01-13': {'planned': {'type': 'long_ride', 'duration_mins': 120}},
        }}
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 30},
            {'date': '2026-01-13', 'type': 'mountain_biking', 'duration_mins': 120},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        detail = result['details'][0]
        assert detail['actual_type'] == 'mountain_biking'
        assert detail['status'] == 'matched'

    def test_plan_adherence_counts_plan_alias_pillars(self, empty_data_dir):
        """Plan 'strength' planned + Garmin 'strength_training' done => completed."""
        monday = '2026-04-13'
        plan = {'days': {
            monday: {'planned': {'type': 'strength', 'duration_mins': 45}},
        }}
        acts = [
            {'date': monday, 'type': 'strength_training', 'duration_mins': 50},
        ]
        result = _summarize_plan_adherence_by_pillar(plan, acts, date(2026, 4, 18))
        assert result['strength']['planned'] == 1
        assert result['strength']['completed'] == 1
        assert result['strength']['skipped_dates'] == []
        assert result['strength']['deficit'] == 0

    def test_plan_adherence_long_ride_vs_mountain_biking(self, empty_data_dir):
        plan = {'days': {
            '2026-04-14': {'planned': {'type': 'long_ride', 'duration_mins': 150}},
        }}
        acts = [
            {'date': '2026-04-14', 'type': 'mountain_biking', 'duration_mins': 140},
        ]
        result = _summarize_plan_adherence_by_pillar(plan, acts, date(2026, 4, 18))
        assert result['long_effort']['planned'] == 1
        assert result['long_effort']['completed'] == 1

    def test_plan_adherence_skipped_still_reported(self, empty_data_dir):
        plan = {'days': {
            '2026-04-13': {'planned': {'type': 'strength', 'duration_mins': 45}},
        }}
        result = _summarize_plan_adherence_by_pillar(plan, [], date(2026, 4, 18))
        assert result['strength']['planned'] == 1
        assert result['strength']['completed'] == 0
        assert result['strength']['skipped_dates'] == ['2026-04-13']


# ---------------------------------------------------------------------------
# _analyze_sport_priorities race map unification
# ---------------------------------------------------------------------------

class TestSportPrioritiesRaceMap:
    def test_marathon_and_running_half_both_map_to_running(self):
        """Race types from the two formerly-diverging maps land in one sport."""
        d1 = (TODAY + timedelta(days=40)).isoformat()
        d2 = (TODAY + timedelta(days=80)).isoformat()
        events = [
            {'name': 'City Marathon', 'date': d1, 'priority': 'A', 'type': 'marathon'},
            {'name': 'Winter Half', 'date': d2, 'priority': 'B', 'type': 'running_half'},
        ]
        result = _analyze_sport_priorities(events, {}, {}, TODAY)
        assert set(result['sports'].keys()) == {'running'}
        assert result['has_multi_sport'] is False

    def test_race_template_keys_map_to_sports(self):
        d = (TODAY + timedelta(days=40)).isoformat()
        events = [
            {'name': 'sani2c', 'date': d, 'priority': 'A', 'type': 'multi_day_mtb'},
        ]
        race_templates = {
            'multi_day_mtb': {'key_sessions': [{'type': 'long_mtb_ride'}]},
            'trail_ultra': {'key_sessions': [{'type': 'long_trail_run'}]},
        }
        result = _analyze_sport_priorities(events, {}, race_templates, TODAY)
        assert 'cycling' in result['sport_specific_sessions']
        assert 'long_mtb_ride' in result['sport_specific_sessions']['cycling']
        assert 'running' in result['sport_specific_sessions']
        assert 'long_trail_run' in result['sport_specific_sessions']['running']


# ---------------------------------------------------------------------------
# workout_builder dispatch (the push-skip fix)
# ---------------------------------------------------------------------------

class TestWorkoutBuilderDispatch:
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_mountain_biking_builds_cycling_workout(self, mock_hr):
        """Garmin type 'mountain_biking' no longer skipped as unknown."""
        session = {"type": "mountain_biking", "duration_mins": 90, "intensity": "easy"}
        result = build_workout(session, "2026-06-10")
        assert isinstance(result, CyclingWorkout)

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_gravel_cycling_builds_cycling_workout(self, mock_hr):
        session = {"type": "gravel_cycling", "duration_mins": 120, "intensity": "easy"}
        result = build_workout(session, "2026-06-10")
        assert isinstance(result, CyclingWorkout)

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_e_bike_builds_cycling_workout(self, mock_hr):
        session = {"type": "e_bike_fitness", "duration_mins": 60, "intensity": "easy"}
        result = build_workout(session, "2026-06-10")
        assert isinstance(result, CyclingWorkout)

    def test_get_workout_type_name_mountain_biking(self):
        assert get_workout_type_name({"type": "mountain_biking"}) == "cycling"
        assert get_workout_type_name({"type": "gravel_cycling"}) == "cycling"
        assert get_workout_type_name({"type": "e_bike_mountain"}) == "cycling"

    def test_get_workout_type_name_legacy_types_unchanged(self):
        assert get_workout_type_name({"type": "long_ride"}) == "cycling"
        assert get_workout_type_name({"type": "easy_run"}) == "running"
        assert get_workout_type_name({"type": "strength"}) == "strength"
        assert get_workout_type_name({"type": "yoga"}) == "yoga"
        assert get_workout_type_name({"type": "rehab"}) == "pilates"
        assert get_workout_type_name({"type": "swim"}) == "swimming"
        assert get_workout_type_name({"type": "padel"}) == "padel"
        assert get_workout_type_name({"type": "rest"}) == "skipped"
        assert get_workout_type_name({"type": "basketball"}) == "unknown"

    def test_rest_day_aliases_skipped(self):
        assert get_workout_type_name({"type": "rest_day"}) == "skipped"
        assert build_workout({"type": "rest_day", "duration_mins": 0}, "2026-06-10") is None

    def test_unknown_type_still_returns_none(self):
        assert build_workout({"type": "basketball", "duration_mins": 60}, "2026-06-10") is None

    def test_breathwork_not_pushable(self):
        assert build_workout({"type": "breathwork", "duration_mins": 10}, "2026-06-10") is None


# ---------------------------------------------------------------------------
# check_safety_rules day-counting golden cases
# ---------------------------------------------------------------------------

class TestSafetyRulesDayCounting:
    CONSTRAINTS = {'max_consecutive_hard_days': 2, 'mandatory_rest_after_race_days': 1}

    def _iso(self, days_ago: int) -> str:
        return (TODAY - timedelta(days=days_ago)).isoformat()

    def test_two_hard_activities_same_day_is_one_hard_day(self, empty_data_dir):
        """Doubles day: two hard sessions on one date must NOT trip the
        consecutive-hard-days gate (counting activities did)."""
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(1)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(1)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is True
        assert not any('consecutive hard days' in w for w in result['warnings'])

    def test_two_consecutive_hard_days_blocks(self, empty_data_dir):
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(1)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(2)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is False
        assert any('consecutive hard days' in b for b in result['blocked'])

    def test_rest_day_gap_breaks_streak(self, empty_data_dir):
        """Hard yesterday + hard 3 days ago with a rest day between => no gate
        (counting activities saw 2 consecutive)."""
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(1)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(3)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is True
        assert not any('consecutive hard days' in w for w in result['warnings'])

    def test_easy_day_between_hard_days_breaks_streak(self, empty_data_dir):
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(1)},
            {'type': 'yoga', 'duration_mins': 30, 'date': self._iso(2)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(3)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is True

    def test_stale_hard_streak_does_not_block_today(self, empty_data_dir):
        """Hard streak that ended 3+ days ago must not gate today's session."""
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(4)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(5)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is True
        assert not any('consecutive hard days' in w for w in result['warnings'])

    def test_hard_today_extends_streak(self, empty_data_dir):
        """Hard activity already logged today + hard yesterday => gate fires."""
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': self._iso(0)},
            {'type': 'hiit', 'duration_mins': 30, 'date': self._iso(1)},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is False

    def test_race_outside_rest_window_does_not_block(self, empty_data_dir):
        """Race 5 days ago with a 1-day rest rule must NOT block today
        (slicing by activity count did when the race was a recent activity)."""
        activities = [
            {'type': 'running', 'name': 'Park Run Race', 'duration_mins': 25,
             'date': self._iso(5)},
        ]
        today_plan = {'type': 'easy_run', 'duration_mins': 30}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is True
        assert not any('race' in w.lower() for w in result['warnings'])

    def test_race_yesterday_blocks_today(self, empty_data_dir):
        activities = [
            {'type': 'running', 'name': 'Park Run Race', 'duration_mins': 25,
             'date': self._iso(1)},
        ]
        today_plan = {'type': 'easy_run', 'duration_mins': 30}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is False
        assert any('race' in b.lower() for b in result['blocked'])

    def test_race_today_blocks(self, empty_data_dir):
        activities = [
            {'type': 'triathlon', 'duration_mins': 180, 'date': self._iso(0)},
        ]
        today_plan = {'type': 'easy_run', 'duration_mins': 30}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is False

    def test_race_within_longer_rest_window_blocks(self, empty_data_dir):
        """rest_after_race=2: a race 2 days ago still gates today."""
        constraints = dict(self.CONSTRAINTS, mandatory_rest_after_race_days=2)
        activities = [
            {'type': 'marathon', 'duration_mins': 200, 'date': self._iso(2)},
        ]
        today_plan = {'type': 'easy_run', 'duration_mins': 30}
        result = check_safety_rules(activities, today_plan, constraints=constraints, today=TODAY)

        assert result['safe'] is False

    def test_race_buried_under_later_activities_still_blocks(self, empty_data_dir):
        """Race yesterday + several activities after it in the list: the date
        check must find it (the old [:rest+1] activity slice missed it)."""
        activities = [
            {'type': 'yoga', 'duration_mins': 20, 'date': self._iso(0)},
            {'type': 'walking', 'duration_mins': 30, 'date': self._iso(0)},
            {'type': 'running', 'name': 'Club Race', 'duration_mins': 40,
             'date': self._iso(1)},
        ]
        today_plan = {'type': 'easy_run', 'duration_mins': 30}
        result = check_safety_rules(activities, today_plan, constraints=self.CONSTRAINTS, today=TODAY)

        assert result['safe'] is False
        assert any('race' in b.lower() for b in result['blocked'])

    def test_undated_race_treated_as_recent(self, empty_data_dir):
        """Back-compat: undated race activities warn conservatively."""
        activities = [
            {'type': 'running', 'name': 'Park Run Race', 'duration_mins': 25},
        ]
        result = check_safety_rules(activities, constraints=self.CONSTRAINTS, today=TODAY)
        assert any('race' in w.lower() for w in result['warnings'])

    def test_no_activities_safe(self, empty_data_dir):
        result = check_safety_rules([], {'type': 'interval_training'},
                                    constraints=self.CONSTRAINTS, today=TODAY)
        assert result['safe'] is True
