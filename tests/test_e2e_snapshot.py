"""End-to-end golden-schema tests for get_coaching_snapshot (Phase 4).

The two tools that shipped the last 5 production bugs (get_coaching_snapshot
and push_plan_to_garmin) had zero happy-path coverage. This file drives the
REAL snapshot pipeline — seeded data files in a tmp DATA_DIR + the canonical
FakeGarminClient answering every endpoint with realistic shapes — and pins
the response contract:

- current_time_context is the FIRST key, with correct per-field types/values
- every core-mandated key present with the right type and shape
- week_grid is a continuous 7-day window ending today
- plan_adherence carries deterministic per-pillar counts
- fitness_metrics / flags / coaching_memory / sleep_gate / data_quality shapes
- sections=['full'] adds every named section with the right inner shapes,
  including the HRV overlay (readiness hrvStatus null -> HRV endpoint wins)
  and epoch-ms -> ISO bedtime conversion in sleep nights
- persistent anomalies carry the open/asked lifecycle fields
- the happy path produces NO error envelope and an EMPTY data_quality
"""
import json
from datetime import date, timedelta

import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod

from coach.tools.coaching_tools import (
    get_coaching_snapshot,
    SNAPSHOT_NAMED_SECTIONS,
)
from conftest import (
    FakeGarminClient,
    make_garmin_activity,
    patch_garmin_everywhere,
)

TODAY = date.today()
TIME_PERIODS = ('early_morning', 'morning', 'afternoon', 'evening', 'night')

CORE_MANDATED_KEYS = [
    'current_time_context', 'snapshot_date', 'day_of_week', 'sections',
    'flags', 'week_grid', 'week_grid_today', 'weekly_plan', 'plan_adherence',
    'planned_vs_actual', 'fitness_metrics', 'acwr_warnings', 'injuries',
    'coaching_memory', 'sleep_gate', 'data_quality',
]

FULL_ONLY_KEYS = [
    'compliance', 'activities_this_week', 'volume_data', 'trends',
    'intensity_distribution', 'sleep', 'recovery', 'readiness_baselines',
    'strength', 'goal_progress', 'adaptation_patterns', 'activity_patterns',
    'compliance_diagnostics', 'sport_priorities',
]


# ---------------------------------------------------------------------------
# Environment: tmp DATA_DIR + seeded athlete/plan/config/history files
# ---------------------------------------------------------------------------

@pytest.fixture
def data_env(data_dir, monkeypatch):
    """Redirect DATA_DIR in every module that does file I/O to a tmp dir."""
    for mod in (planner, rules, fitness_mod, parsers_mod, workout_builder,
                coaching_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


@pytest.fixture(autouse=True)
def _clear_garmin_cache():
    coaching_mod._garmin_fetch_cache.clear()
    yield
    coaching_mod._garmin_fetch_cache.clear()


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


def _v2_day(load, day_iso, act_type='cycling', sport='cycling'):
    return {
        'total': load,
        'by_sport': {sport: load},
        'activities': [{
            'id': int(day_iso.replace('-', '')), 'type': act_type,
            'sport': sport, 'duration_mins': 60, 'load': load,
            'avg_hr': 130, 'date': day_iso,
        }],
    }


ACTIVE_RUN_INJURY = {
    'date': (TODAY - timedelta(days=10)).isoformat(),
    'type': 'shin splints', 'body_region': 'shin',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running', 'jumping'],
    'safe_activities': ['cycling'],
}

# Plan with all three pillars represented (deterministic adherence):
#   long_effort: cycling 90' on D-2 (done), D0 (done), D+4 (pending)
#   strength:    45' on D-1 (done) and D+1 (pending)
#   mobility:    yoga 30' on D+3 (pending)
#   rest:        D+2
PLAN_SHAPE = {
    -2: {'type': 'cycling', 'duration_mins': 90,
         'purpose': 'Z2 aerobic base', 'intensity': 'easy'},
    -1: {'type': 'strength_training', 'duration_mins': 45,
         'purpose': 'Posterior chain maintenance'},
    0: {'type': 'cycling', 'duration_mins': 90,
        'purpose': 'Z2 aerobic base', 'intensity': 'easy'},
    1: {'type': 'strength_training', 'duration_mins': 45,
        'purpose': 'Posterior chain maintenance'},
    2: {'type': 'rest'},
    3: {'type': 'yoga', 'duration_mins': 30,
        'purpose': 'Hip mobility for the gravel position'},
    4: {'type': 'cycling', 'duration_mins': 120,
        'purpose': 'Long-ride durability block', 'intensity': 'easy'},
}


def _seed_env(data_dir, history_days=35):
    """A realistic mid-block athlete: multi-sport history, current plan with
    every pillar, an active injury, coaching memory, persisted sleep."""
    daily_loads = {}
    for i in range(history_days):
        d = TODAY - timedelta(days=i)
        act_type = 'cycling' if i % 3 else 'strength_training'
        sport = 'cycling' if i % 3 else 'strength'
        daily_loads[d.isoformat()] = _v2_day(
            45.0 + (i % 5) * 10, d.isoformat(), act_type=act_type, sport=sport)

    sleep_history = [
        {'date': (TODAY - timedelta(days=i)).isoformat(),
         'bedtime': f'{(TODAY - timedelta(days=i + 1)).isoformat()}T22:30:00',
         'wake_time': f'{(TODAY - timedelta(days=i)).isoformat()}T06:00:00',
         'duration_hrs': 8.1, 'score': 78, 'deep_pct': 19, 'rem_pct': 21,
         'light_pct': 52, 'awake_mins': 30, 'avg_hr': 49,
         'respiration': 14.2, 'sleep_stress': 15}
        for i in range(1, 7)  # last night intentionally NOT persisted
    ]

    _write(data_dir, 'fitness_history.json', {
        'schema_version': 2,
        'daily_loads': daily_loads,
        'snapshots': [],
        'sleep_history': sleep_history,
        'readiness_history': [],
        'last_updated': TODAY.isoformat(),
        'last_activity_ingest_date': TODAY.isoformat(),
    })

    _write(data_dir, 'athlete.json', {
        'personal': {'name': 'Test Athlete', 'age': 36, 'weight_kg': 70,
                     'max_hr': 188, 'resting_hr': 52},
        'injury_history': [ACTIVE_RUN_INJURY],
        'training_pillars': {
            'strength': {'target_type': 'sessions',
                         'target_sessions_per_week': 2,
                         'types': ['strength_training']},
            'mobility': {'target_type': 'minutes', 'target_mins_per_week': 60,
                         'types': ['yoga']},
        },
        'life_constraints': {}, 'preferences': {},
    })

    _write(data_dir, 'training_config.json', {
        'current_block': {'phase': 'build', 'weekly_volume_target_hrs': 8.0},
        'events': [
            {'name': 'Big Gravel Race',
             'date': (TODAY + timedelta(days=60)).isoformat(),
             'type': 'gravel', 'priority': 'A'},
            {'name': 'Fun Trail Run',
             'date': (TODAY + timedelta(days=90)).isoformat(),
             'type': 'trail_run', 'priority': 'C'},
        ],
    })

    days = {}
    for offset, session in PLAN_SHAPE.items():
        d = (TODAY + timedelta(days=offset)).isoformat()
        days[d] = {'planned': dict(session)}
    _write(data_dir, 'weekly_plan.json', {
        'week_start': (TODAY - timedelta(days=2)).isoformat(),
        'week_end': (TODAY + timedelta(days=4)).isoformat(),
        'days': days,
    })

    decisions = [
        {'id': f'd{i}', 'date': (TODAY - timedelta(days=i * 3)).isoformat(),
         'type': 'load_adjustment', 'decision': f'decision {i}',
         'rationale': 'because data', 'status': 'active'}
        for i in range(7)  # d0 today ... d6 18 days ago (4 are >7 days old)
    ]
    _write(data_dir, 'coaching_log.json', {
        'decisions': decisions,
        'athlete_responses': [
            {'date': (TODAY - timedelta(days=2)).isoformat(),
             'stimulus': 'big week', 'response': 'good',
             'pattern': 'handles_volume_well'},
        ],
        'pending_approvals': [
            {'id': 'p1', 'change': 'increase volume 15%',
             'expires': (TODAY + timedelta(days=5)).isoformat()},
        ],
    })


def _matching_client():
    """Garmin actuals that MATCH the plan for D-2..D0 (happy path). Last
    night's sleep clears the 8h personalized need the fake's sleepNeed sets."""
    from conftest import make_sleep_payload
    return FakeGarminClient(
        activities=[
            make_garmin_activity(TODAY, 'cycling', duration_secs=5400, load=65.0),
            make_garmin_activity(TODAY - timedelta(days=1), 'strength_training',
                                 duration_secs=2700, distance_m=None, load=35.0),
            make_garmin_activity(TODAY - timedelta(days=2), 'cycling',
                                 duration_secs=5400, load=65.0),
        ],
        overrides={'get_sleep_data':
                   lambda d: make_sleep_payload(d, score=82, duration_hrs=8.2)},
    )


@pytest.fixture
def happy_env(data_env, monkeypatch):
    """Seeded files + a plan-matching FakeGarminClient patched everywhere."""
    _seed_env(data_env)
    client = _matching_client()
    patch_garmin_everywhere(monkeypatch, client)
    return data_env, client


async def _snapshot(mock_ctx, **kwargs):
    raw = await get_coaching_snapshot(mock_ctx, **kwargs)
    return json.loads(raw), raw


# ---------------------------------------------------------------------------
# CORE golden schema
# ---------------------------------------------------------------------------

class TestCoreGoldenSchema:
    async def test_no_error_and_first_key_is_time_context(self, happy_env, mock_ctx):
        result, raw = await _snapshot(mock_ctx)

        assert 'error' not in result
        assert next(iter(result)) == 'current_time_context'
        # And in the raw JSON text, not just the parsed dict
        assert raw.lstrip('{').lstrip().startswith('"current_time_context"')

    async def test_every_core_mandated_key_present(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        for key in CORE_MANDATED_KEYS:
            assert key in result, f"core payload missing mandated key '{key}'"
        assert result['sections']['included'] == ['core']
        for key in FULL_ONLY_KEYS:
            assert key not in result, f"core payload must not carry '{key}'"

    async def test_current_time_context_grounds_the_coach(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        ctx = result['current_time_context']
        assert ctx['date'] == TODAY.isoformat()
        assert ctx['day_of_week'] == TODAY.strftime('%A')
        assert isinstance(ctx['hour'], int) and 0 <= ctx['hour'] <= 23
        assert isinstance(ctx['minute'], int) and 0 <= ctx['minute'] <= 59
        assert ctx['time_period'] in TIME_PERIODS
        assert ctx['is_weekend'] == (TODAY.weekday() >= 5)
        assert ctx['timestamp'].startswith(TODAY.isoformat())
        assert result['snapshot_date'] == TODAY.isoformat()
        assert result['day_of_week'] == TODAY.strftime('%A')

    async def test_week_grid_is_continuous_seven_days_ending_today(
            self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        grid = result['week_grid']
        expected_days = [(TODAY - timedelta(days=i)).isoformat()
                         for i in range(6, -1, -1)]
        assert list(grid.keys()) == expected_days, \
            "week_grid must be a continuous 7-day window ending today"
        for day_iso, entry in grid.items():
            d = date.fromisoformat(day_iso)
            assert entry['day_of_week'] == d.strftime('%A')
            assert isinstance(entry['activity_count'], int)
            assert isinstance(entry['types'], list)
            assert isinstance(entry['types_summary'], str)
            assert isinstance(entry['total_duration_mins'], (int, float))
            assert isinstance(entry['is_rest'], bool)
            assert entry['is_today'] == (d == TODAY)
            assert entry['days_ago'] == (TODAY - d).days
            assert entry['is_rest'] == (entry['activity_count'] == 0)
            if entry['is_rest']:
                assert entry['types_summary'] == 'REST'
        # 35 days of seeded daily loads + fresh activities: today is not REST
        assert grid[TODAY.isoformat()]['is_rest'] is False
        # The grid names its own anchor date (temporal self-anchoring)
        assert result['week_grid_today'] == TODAY.isoformat()

    async def test_weekly_plan_carries_today_and_tomorrow_only(
            self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        plan = result['weekly_plan']
        assert plan['has_plan'] is True
        assert plan['week_start'] == (TODAY - timedelta(days=2)).isoformat()
        assert plan['week_end'] == (TODAY + timedelta(days=4)).isoformat()
        assert plan['today']['planned']['type'] == 'cycling'
        assert plan['today']['planned']['purpose'] == 'Z2 aerobic base'
        assert plan['tomorrow']['planned']['type'] == 'strength_training'
        assert 'days' not in plan

    async def test_plan_adherence_per_pillar_counts(self, happy_env, mock_ctx):
        """Deterministic by construction: see PLAN_SHAPE + _matching_client."""
        result, _ = await _snapshot(mock_ctx)

        adherence = result['plan_adherence']
        assert set(adherence) == {'strength', 'mobility', 'long_effort'}
        for pillar, data in adherence.items():
            assert isinstance(data['planned'], int)
            assert isinstance(data['completed'], int)
            assert isinstance(data['skipped_dates'], list)
            assert isinstance(data['pending_dates'], list)
            assert data['deficit'] == data['planned'] - data['completed']

        strength = adherence['strength']
        assert strength['planned'] == 2
        assert strength['completed'] == 1   # yesterday done, tomorrow pending
        assert strength['skipped_dates'] == []
        assert strength['pending_dates'] == [(TODAY + timedelta(days=1)).isoformat()]

        mobility = adherence['mobility']
        assert mobility['planned'] == 1     # yoga on D+3, still pending
        assert mobility['completed'] == 0
        assert mobility['pending_dates'] == [(TODAY + timedelta(days=3)).isoformat()]

        long_effort = adherence['long_effort']
        assert long_effort['planned'] == 3  # D-2, D0 done; D+4 pending
        assert long_effort['completed'] == 2
        assert long_effort['pending_dates'] == [(TODAY + timedelta(days=4)).isoformat()]

    async def test_fitness_metrics_structure(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        fm = result['fitness_metrics']
        status = fm['acwr_status']
        assert set(status) == {'value', 'zone', 'safe'}
        assert isinstance(status['value'], (int, float))
        assert isinstance(status['zone'], str)
        assert isinstance(status['safe'], bool)
        # Legacy EWMA reference (replaced acwr_shadow at the 2026-06-10 cutover)
        ewma = fm['acwr_ewma']
        assert {'value', 'zone', 'safe', 'note'} <= set(ewma)
        assert 'acwr_shadow' not in fm
        # 35 days of cycling + strength history -> both sports tracked
        assert {'cycling', 'strength'} <= set(fm['by_sport'])
        for sport_metrics in fm['by_sport'].values():
            assert {'ctl', 'atl', 'tsb', 'acwr'} == set(sport_metrics)
        hierarchy = fm['load_hierarchy']
        assert isinstance(hierarchy['overall_acwr_safe'], bool)
        assert isinstance(hierarchy['sport_acwr_concerns'], list)
        assert isinstance(result['acwr_warnings'], list)

    async def test_injury_gate_data_in_core(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        injuries = result['injuries']
        assert len(injuries) == 1
        assert injuries[0]['status'] == 'active'
        assert injuries[0]['restricted_activities'] == ['running', 'jumping']

    async def test_coaching_memory_shape(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        memory = result['coaching_memory']
        active = memory['active_decisions']
        assert len(active) == 5  # capped, newest first
        assert active[0]['id'] == 'd0'
        dates = [d['date'] for d in active]
        assert dates == sorted(dates, reverse=True)
        assert len(memory['pending_approvals']) == 1
        due = memory['decisions_due_review']
        assert len(due) == 4  # d3..d6 are 9/12/15/18 days old
        assert {'id', 'decision', 'review_date', 'status'} <= set(due[0])
        assert len(memory['recent_responses']) == 1
        assert 'adaptation_patterns' not in memory, \
            "adaptation patterns are memory-section only"

    async def test_sleep_gate_signal(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx)

        gate = result['sleep_gate']
        assert {'avg_hours', 'deficit', 'status', 'acute_status',
                'last_night_score', 'last_night_hrs',
                'nights_analyzed'} <= set(gate)
        assert isinstance(gate['deficit'], bool)
        assert gate['deficit'] is False          # ~8.1h seeded vs 8h need
        assert 7.5 < gate['avg_hours'] < 8.6
        assert gate['last_night_score'] == 82    # freshly fetched from the fake
        assert gate['nights_analyzed'] == 7

    async def test_happy_path_has_empty_data_quality_and_calm_flags(
            self, happy_env, mock_ctx):
        """Fully seeded data + healthy Garmin: every quality check passes."""
        result, _ = await _snapshot(mock_ctx)

        assert result['data_quality'] == {}
        flags = result['flags']
        assert flags['active_injuries'] == 1
        assert flags['pending_approvals'] == 1
        assert flags['decisions_due_for_review'] == 4
        assert 'plan_expired' not in flags
        assert 'anomaly_count' not in flags  # actuals match the plan

    async def test_core_skips_hr_zone_enrichment(self, happy_env, mock_ctx):
        _, client = happy_env
        await _snapshot(mock_ctx)
        assert client.call_counts['get_activity_hr_in_timezones'] == 0


# ---------------------------------------------------------------------------
# Anomaly lifecycle in the core payload
# ---------------------------------------------------------------------------

class TestAnomalies:
    async def test_missed_session_surfaces_persistent_anomaly(
            self, data_env, mock_ctx, monkeypatch):
        _seed_env(data_env)
        # Yesterday's planned strength session never happened on Garmin
        client = FakeGarminClient(activities=[
            make_garmin_activity(TODAY, 'cycling', duration_secs=5400),
            make_garmin_activity(TODAY - timedelta(days=2), 'cycling',
                                 duration_secs=5400),
        ])
        patch_garmin_everywhere(monkeypatch, client)

        result, _ = await _snapshot(mock_ctx)

        anomalies = result['planned_vs_actual']['anomalies']
        assert anomalies, "missed session must surface as an anomaly"
        missing = [a for a in anomalies
                   if a['date'] == (TODAY - timedelta(days=1)).isoformat()]
        assert missing, "the missed strength day must be flagged"
        # Phase 3 persistence: anomalies carry the open/asked lifecycle
        for anomaly in anomalies:
            assert anomaly['id']
            assert anomaly['status'] in ('open', 'asked')
            assert anomaly['summary']
        assert result['flags']['anomaly_count'] == len(anomalies)


# ---------------------------------------------------------------------------
# FULL golden schema
# ---------------------------------------------------------------------------

class TestFullGoldenSchema:
    async def test_full_carries_every_section(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        assert 'error' not in result
        assert next(iter(result)) == 'current_time_context'
        for key in CORE_MANDATED_KEYS + FULL_ONLY_KEYS:
            assert key in result, f"full payload missing '{key}'"
        assert sorted(result['sections']['included']) == sorted(
            ['core'] + list(SNAPSHOT_NAMED_SECTIONS))
        assert len(result['weekly_plan']['days']) == len(PLAN_SHAPE)

    async def test_sleep_nights_full_detail_with_epoch_ms_conversion(
            self, happy_env, mock_ctx):
        """The fake serves epoch-ms '...TimestampLocal' ints (live shape) —
        the snapshot must surface ISO-8601 local strings."""
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        sleep = result['sleep']
        assert sleep['days_analyzed'] == 7
        nights = sleep['nights']
        assert len(nights) == 7
        assert nights[0]['date'] == TODAY.isoformat()
        # Last night was freshly fetched as epoch-ms and converted
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        assert nights[0]['bedtime'] == f'{yesterday}T22:30:00'
        assert nights[0]['wake_time'] == f'{TODAY.isoformat()}T06:00:00'
        assert nights[0]['score'] == 82
        for night in nights:
            assert isinstance(night['duration_hrs'], (int, float))

    async def test_recovery_carries_hrv_overlay(self, happy_env, mock_ctx):
        """Readiness returns hrvStatus null (the live Garmin bug) — the
        dedicated HRV endpoint's data must win."""
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        recovery = result['recovery']
        assert recovery['score'] == 72
        assert recovery['level'] == 'HIGH'
        assert recovery['sleep_score'] == 85
        assert recovery['hrv_status'] == 'BALANCED'   # from get_hrv_data
        assert recovery['hrv_last_night_avg'] == 58
        assert recovery['hrv_weekly_avg'] == 55
        assert 'hrv_trend' in recovery

    async def test_activities_section_enriches_hr_zones(self, happy_env, mock_ctx):
        _, client = happy_env
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        section = result['activities_this_week']
        assert isinstance(section['count'], int)
        assert section['count'] == len(section['activities'])
        assert section['count'] >= 1  # today's ride is always in this week
        assert isinstance(section['total_duration_mins'], (int, float))
        assert client.call_counts['get_activity_hr_in_timezones'] >= 1
        today_acts = [a for a in section['activities']
                      if a['date'] == TODAY.isoformat()]
        assert today_acts and 'z2' in today_acts[0]['hr_time_in_zones']

    async def test_volume_data_targets_the_a_race(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        volume = result['volume_data']
        assert volume['a_race'] == 'Big Gravel Race'
        assert volume['race_sport'] == 'cycling'
        assert 55 <= volume['days_until_race'] <= 60
        assert volume['load_increase_pcts'] == [10, 15, 25]
        assert isinstance(volume['current_ctl'], (int, float))
        assert isinstance(volume['current_ctl_overall'], (int, float))

    async def test_trends_and_patterns_shapes(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        trends = result['trends']
        assert len(trends['volume_trajectory_4wk']) == 4
        assert all(isinstance(v, (int, float))
                   for v in trends['volume_trajectory_4wk'])
        assert {'cycling', 'running', 'strength'} == set(
            trends['volume_by_sport_4wk'])
        assert trends['overall_ctl_4wk']['direction']
        assert isinstance(result['adaptation_patterns'], dict)
        assert isinstance(result['activity_patterns'], dict)
        assert isinstance(result['intensity_distribution'], dict)
        assert isinstance(result['compliance_diagnostics'], dict)
        assert isinstance(result['goal_progress'], dict)
        assert isinstance(result['strength'], dict)
        # memory section adds the learned patterns list
        assert 'adaptation_patterns' in result['coaching_memory']

    async def test_sport_priorities_rank_the_a_race_sport(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        priorities = result['sport_priorities']
        assert priorities['has_multi_sport'] is True
        assert 'strength' in priorities['shared_sessions']
        sports = priorities['sports']
        # A-priority gravel race outweighs the C-priority trail run
        assert sports['cycling']['primary_focus'] is True
        assert sports['cycling']['volume_pct'] > sports['running']['volume_pct']

    async def test_compliance_present_with_rate(self, happy_env, mock_ctx):
        result, _ = await _snapshot(mock_ctx, sections=['full'])

        assert 'compliance_rate_pct' in result['compliance']

    async def test_full_is_strict_superset_of_core(self, happy_env, mock_ctx):
        core, _ = await _snapshot(mock_ctx)
        full, _ = await _snapshot(mock_ctx, sections=['full'])

        assert set(core.keys()) < set(full.keys())
        assert set(core['coaching_memory']) < set(full['coaching_memory'])
        assert set(core['weekly_plan']) < set(full['weekly_plan'])
