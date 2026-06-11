"""Tests for the sectioned, resilient coaching snapshot (Phase 2.1).

Covers:
1.  CORE payload contract: mandated keys present, heavy sections excluded
    (full sleep nights, full activity list, plan days beyond today/tomorrow)
2.  sections=['full'] is a strict superset of core
3.  Unknown section names error, listing the valid vocabulary
4.  Write side effects (trailing activity ingest, sleep + readiness
    persistence) run on EVERY call regardless of sections — the regression
    guard against the silent pipeline death fixed in Phase 0
5.  Per-section Garmin failures degrade to data_quality flags instead of
    failing the whole snapshot; GarminAuthRequiredError surfaces its
    actionable message ALONGSIDE locally derivable data
6.  Short-TTL Garmin fetch cache: hit on back-to-back calls, miss after TTL
    expiry, bypass via force_refresh
7.  Efficiency: sleep fetched only for nights missing from the persisted
    sleep_history; per-activity HR-zone enrichment only with the activities
    section; no indent in the output JSON
8.  Token budget: core payload stays small with realistic fixture data
"""
import json
from collections import defaultdict
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

from coach.garmin_client import GarminAuthRequiredError
from coach.tools.coaching_tools import (
    get_coaching_snapshot,
    SNAPSHOT_NAMED_SECTIONS,
    _resolve_snapshot_sections,
    _activities_from_history,
    _merge_sleep_nights,
    _build_sleep_gate,
    _count_decisions_due_review,
)

TODAY = date.today()


# ---------------------------------------------------------------------------
# Environment helpers (pattern from tests/test_phase0_fixes.py)
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
    """Cache keys embed DATA_DIR so cross-test pollution can't happen, but
    keep each test's cache cold anyway."""
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


def _raw_activity(d, type_key='cycling', load=50.0, duration_secs=3600):
    return {
        'activityId': int(d.strftime('%Y%m%d')),
        'activityName': 'Session',
        'startTimeLocal': f'{d.isoformat()} 08:00:00',
        'activityType': {'typeKey': type_key, 'parentTypeId': 2},
        'eventType': {'typeKey': 'training'},
        'duration': duration_secs,
        'distance': 20000,
        'averageHR': 120,
        'maxHR': 150,
        'activityTrainingLoad': load,
    }


def _sleep_payload(day, score=82, duration_hrs=7.4):
    secs = int(duration_hrs * 3600)
    prev = day - timedelta(days=1)
    return {'dailySleepDTO': {
        'sleepTimeSeconds': secs,
        'sleepScores': {
            'overall': {'value': score, 'qualifierKey': 'GOOD'},
            'deepPercentage': {'qualifierKey': 'GOOD'},
            'remPercentage': {'qualifierKey': 'GOOD'},
        },
        'deepSleepSeconds': int(secs * 0.20),
        'remSleepSeconds': int(secs * 0.22),
        'lightSleepSeconds': int(secs * 0.50),
        'awakeSleepSeconds': int(secs * 0.08),
        'sleepStartTimestampLocal': f'{prev.isoformat()}T22:30:00',
        'sleepEndTimestampLocal': f'{day.isoformat()}T06:00:00',
        'avgSleepStress': 14,
        'avgHeartRate': 48,
        'averageRespirationValue': 14.0,
        'sleepNeed': {'actual': 480, 'baseline': 450,
                      'feedback': 'INCREASED', 'trainingFeedback': 'CHRONIC'},
    }}


READINESS = {
    'calendarDate': TODAY.isoformat(),
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTime': 720,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
}

ACTIVE_RUN_INJURY = {
    'date': (TODAY - timedelta(days=10)).isoformat(),
    'type': 'shin', 'body_region': 'shin',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running', 'jumping'],
    'safe_activities': ['cycling'],
}


class FakeGarminClient:
    """Call-counting fake for the Garmin methods the snapshot exercises."""

    def __init__(self, raw_activities=None, sleep_payloads=None,
                 readiness=None, hrv=None,
                 fail_activities=False, fail_readiness=False, fail_sleep=False):
        self.raw_activities = raw_activities or []
        self.sleep_payloads = sleep_payloads or {}
        self.readiness = readiness if readiness is not None else READINESS
        self.hrv = hrv
        self.fail_activities = fail_activities
        self.fail_readiness = fail_readiness
        self.fail_sleep = fail_sleep
        self.calls = defaultdict(int)
        self.activity_calls = []

    def total_calls(self):
        return sum(self.calls.values())

    def get_activities_by_date(self, start, end):
        self.calls['get_activities_by_date'] += 1
        self.activity_calls.append((start, end))
        if self.fail_activities:
            raise Exception('activities endpoint down')
        return [
            a for a in self.raw_activities
            if start <= a['startTimeLocal'][:10] <= end
        ]

    def get_training_readiness(self, d):
        self.calls['get_training_readiness'] += 1
        if self.fail_readiness:
            raise Exception('readiness endpoint down')
        return self.readiness

    def get_hrv_data(self, d):
        self.calls['get_hrv_data'] += 1
        return self.hrv

    def get_sleep_data(self, d):
        self.calls['get_sleep_data'] += 1
        if self.fail_sleep:
            raise Exception('sleep endpoint down')
        return self.sleep_payloads.get(d, {})


def _patch_garmin(monkeypatch, client):
    """Patch garmin_api_call everywhere the snapshot uses it."""
    fake_call = lambda fn: fn(client)
    monkeypatch.setattr(coaching_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(fitness_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(planning_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', lambda acts: acts)


def _seed_realistic_env(data_dir, history_days=35, seed_sleep_history=True,
                        ingest_marker=None):
    """A realistic athlete: multi-sport loads, current plan, injury,
    decisions, pending approval, persisted sleep nights."""
    daily_loads = {}
    for i in range(history_days):
        d = TODAY - timedelta(days=i)
        act_type = 'cycling' if i % 3 else 'strength_training'
        sport = 'cycling' if i % 3 else 'strength'
        daily_loads[d.isoformat()] = _v2_day(45.0 + (i % 5) * 10, d.isoformat(),
                                             act_type=act_type, sport=sport)

    sleep_history = []
    if seed_sleep_history:
        for i in range(1, 7):  # last night (today) intentionally NOT persisted
            d = (TODAY - timedelta(days=i)).isoformat()
            sleep_history.append({
                'date': d, 'bedtime': f'{d}T22:30:00', 'wake_time': f'{d}T06:00:00',
                'duration_hrs': 7.2, 'score': 78, 'deep_pct': 19, 'rem_pct': 21,
                'light_pct': 52, 'awake_mins': 30, 'avg_hr': 49,
                'respiration': 14.2, 'sleep_stress': 15,
            })

    _write(data_dir, 'fitness_history.json', {
        'schema_version': 2,
        'daily_loads': daily_loads,
        'snapshots': [],
        'sleep_history': sleep_history,
        'readiness_history': [],
        'last_updated': TODAY.isoformat(),
        'last_activity_ingest_date': (ingest_marker or TODAY).isoformat(),
    })

    _write(data_dir, 'athlete.json', {
        'personal': {'name': 'Test Athlete', 'age': 38, 'weight_kg': 78,
                     'max_hr': 185, 'resting_hr': 46},
        'injury_history': [ACTIVE_RUN_INJURY],
        'training_pillars': {
            'strength': {'target_type': 'sessions', 'target_sessions_per_week': 2,
                         'types': ['strength_training']},
            'mobility': {'target_type': 'minutes', 'target_mins_per_week': 60,
                         'types': ['yoga']},
        },
        'life_constraints': {}, 'preferences': {},
    })

    _write(data_dir, 'training_config.json', {
        'current_block': {'phase': 'build', 'weekly_volume_target_hrs': 8.0},
        'events': [
            {'name': 'Big Gravel Race', 'date': (TODAY + timedelta(days=60)).isoformat(),
             'type': 'gravel_race', 'priority': 'A'},
            {'name': 'Fun Trail Run', 'date': (TODAY + timedelta(days=90)).isoformat(),
             'type': 'trail_run', 'priority': 'C'},
        ],
    })

    days = {}
    for offset in range(-2, 5):  # plan spans today-2 .. today+4
        d = (TODAY + timedelta(days=offset)).isoformat()
        if offset == 2:
            days[d] = {'planned': {'type': 'rest'}}
        elif offset % 2:
            days[d] = {'planned': {'type': 'strength_training', 'duration_mins': 45,
                                   'purpose': 'Posterior chain maintenance'}}
        else:
            days[d] = {'planned': {'type': 'cycling', 'duration_mins': 90,
                                   'purpose': 'Z2 aerobic base'}}
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


def _default_client():
    return FakeGarminClient(
        raw_activities=[
            _raw_activity(TODAY, 'cycling'),
            _raw_activity(TODAY - timedelta(days=1), 'strength_training'),
            _raw_activity(TODAY - timedelta(days=2), 'cycling'),
        ],
        sleep_payloads={TODAY.isoformat(): _sleep_payload(TODAY)},
    )


async def _snapshot(mock_ctx, **kwargs):
    return json.loads(await get_coaching_snapshot(mock_ctx, **kwargs))


# ---------------------------------------------------------------------------
# Section resolution (pure)
# ---------------------------------------------------------------------------

class TestSectionResolution:
    def test_none_and_core_resolve_to_core_only(self):
        assert _resolve_snapshot_sections(None) == (set(), None)
        assert _resolve_snapshot_sections([]) == (set(), None)
        assert _resolve_snapshot_sections(['core']) == (set(), None)
        assert _resolve_snapshot_sections(['CORE']) == (set(), None)

    def test_full_expands_to_all_named(self):
        include, err = _resolve_snapshot_sections(['full'])
        assert err is None
        assert include == set(SNAPSHOT_NAMED_SECTIONS)

    def test_named_sections_pass_through(self):
        include, err = _resolve_snapshot_sections(['sleep', 'recovery'])
        assert err is None
        assert include == {'sleep', 'recovery'}

    def test_bare_string_tolerated(self):
        """LLM clients sometimes send 'full' instead of ['full'] — never
        iterate it as characters."""
        include, err = _resolve_snapshot_sections('full')
        assert err is None
        assert include == set(SNAPSHOT_NAMED_SECTIONS)
        include, err = _resolve_snapshot_sections('sleep')
        assert include == {'sleep'}

    def test_unknown_section_errors_with_vocabulary(self):
        include, err = _resolve_snapshot_sections(['bogus'])
        assert include is None
        assert 'bogus' in err
        for name in SNAPSHOT_NAMED_SECTIONS + ('core', 'full'):
            assert name in err


# ---------------------------------------------------------------------------
# CORE payload contract
# ---------------------------------------------------------------------------

CORE_MANDATED_KEYS = [
    'current_time_context', 'flags', 'week_grid', 'week_grid_today',
    'fitness_metrics', 'acwr_warnings', 'injuries', 'plan_adherence',
    'weekly_plan', 'planned_vs_actual', 'coaching_memory', 'sleep_gate',
    'data_quality',
]

HEAVY_SECTION_KEYS = [
    'sleep', 'activities_this_week', 'recovery', 'readiness_baselines',
    'strength', 'goal_progress', 'adaptation_patterns', 'activity_patterns',
    'compliance_diagnostics', 'sport_priorities', 'volume_data', 'trends',
    'intensity_distribution', 'compliance',
]


class TestCorePayload:
    async def test_core_contains_mandated_keys(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        assert 'error' not in result
        for key in CORE_MANDATED_KEYS:
            assert key in result, f"core payload missing mandated key '{key}'"
        assert result['sections']['included'] == ['core']
        # current_time_context is the FIRST key
        assert next(iter(result)) == 'current_time_context'

    async def test_core_fitness_metrics_carry_acwr_status_and_ewma_reference(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        fm = result['fitness_metrics']
        # Rolling 7d:28d primary + labeled legacy EWMA reference
        # (cutover 2026-06-10; the acwr_shadow key is retired)
        assert 'acwr_status' in fm and 'acwr_ewma' in fm
        assert 'acwr_shadow' not in fm
        assert set(fm['acwr_status']) == {'value', 'zone', 'safe'}
        assert set(fm['acwr_ewma']) == {'value', 'zone', 'safe', 'note'}
        assert 'load_hierarchy' in fm

    async def test_core_carries_injuries_and_memory(self, data_env, mock_ctx, monkeypatch):
        """Memory + injuries live in the DEFAULT payload — they must not be
        opt-in or they die by default."""
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        assert result['injuries'][0]['restricted_activities'] == ['running', 'jumping']
        memory = result['coaching_memory']
        assert len(memory['active_decisions']) == 5
        # Most recent decision first
        assert memory['active_decisions'][0]['id'] == 'd0'
        assert len(memory['pending_approvals']) == 1
        # d3..d6 are 9/12/15/18 days old -> 4 due review (scans ALL active).
        # Phase 3: decisions_due_review carries actual summaries, not a count.
        due = memory['decisions_due_review']
        assert len(due) == 4
        assert {'id', 'decision', 'review_date', 'status'} <= set(due[0])

    async def test_core_plan_is_today_and_tomorrow_only(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        plan = result['weekly_plan']
        assert plan['has_plan'] is True
        assert plan['today'] is not None
        assert plan['tomorrow'] is not None
        assert 'days' not in plan, "core must not carry the full plan days"

    async def test_core_sleep_gate_signal(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        gate = result['sleep_gate']
        assert gate['avg_hours'] is not None
        assert isinstance(gate['deficit'], bool)
        # Last night (today) was freshly fetched with score 82
        assert gate['last_night_score'] == 82
        assert gate['nights_analyzed'] == 7

    async def test_core_keeps_open_anomalies_without_details(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        # No strength activity yesterday -> a 'missing' anomaly exists
        client = FakeGarminClient(
            raw_activities=[_raw_activity(TODAY, 'cycling')],
            sleep_payloads={TODAY.isoformat(): _sleep_payload(TODAY)},
        )
        _patch_garmin(monkeypatch, client)

        result = await _snapshot(mock_ctx)

        pva = result['planned_vs_actual']
        assert pva.get('anomalies'), "open anomalies must be in the core payload"
        assert 'details' not in pva, "per-session details are plan-section only"

    async def test_core_excludes_heavy_sections(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx)

        for key in HEAVY_SECTION_KEYS:
            assert key not in result, f"core payload must not carry '{key}'"

    async def test_explicit_core_matches_default(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        default_result = await _snapshot(mock_ctx)
        core_result = await _snapshot(mock_ctx, sections=['core'])

        assert set(default_result.keys()) == set(core_result.keys())

    async def test_output_is_compact_json(self, data_env, mock_ctx, monkeypatch):
        """indent=2 dropped — pretty-printing only burns tokens."""
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        raw = await get_coaching_snapshot(mock_ctx)

        assert '\n' not in raw


# ---------------------------------------------------------------------------
# Named sections + full
# ---------------------------------------------------------------------------

class TestSections:
    async def test_full_is_superset_of_core(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        core = await _snapshot(mock_ctx)
        full = await _snapshot(mock_ctx, sections=['full'])

        assert set(core.keys()) <= set(full.keys())
        for key in HEAVY_SECTION_KEYS:
            assert key in full, f"full payload missing '{key}'"
        # Nested supersets too
        assert set(core['weekly_plan'].keys()) <= set(full['weekly_plan'].keys())
        assert 'days' in full['weekly_plan']
        assert set(core['coaching_memory'].keys()) <= set(full['coaching_memory'].keys())
        assert 'adaptation_patterns' in full['coaching_memory']
        # Full sleep detail present with per-night records
        assert full['sleep'].get('nights')
        assert sorted(full['sections']['included']) == sorted(
            ['core'] + list(SNAPSHOT_NAMED_SECTIONS))

    async def test_named_section_adds_only_requested(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx, sections=['sleep'])

        assert 'sleep' in result
        assert result['sleep'].get('avg_duration_hrs') is not None
        assert 'activities_this_week' not in result
        assert 'recovery' not in result
        assert result['sections']['included'] == ['core', 'sleep']

    async def test_plan_section_adds_days_details_compliance(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        result = await _snapshot(mock_ctx, sections=['plan'])

        assert len(result['weekly_plan']['days']) == 7
        assert 'details' in result['planned_vs_actual']
        assert 'compliance' in result

    async def test_unknown_section_returns_error(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = _default_client()
        _patch_garmin(monkeypatch, client)

        result = await _snapshot(mock_ctx, sections=['core', 'bogus_section'])

        assert set(result.keys()) == {'error'}
        assert 'bogus_section' in result['error']
        for name in SNAPSHOT_NAMED_SECTIONS:
            assert name in result['error']
        # Validation happens before any Garmin work
        assert client.total_calls() == 0


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE: write side effects run on EVERY call regardless of sections
# ---------------------------------------------------------------------------

class TestSideEffectsAlwaysRun:
    async def test_daily_loads_advance_after_core_only_call(
            self, data_env, mock_ctx, monkeypatch):
        """Regression guard for the Phase 0 silent pipeline death: a
        sections=['core'] call must still ingest trailing activities."""
        # History last ingested 5 days ago; today's ride exists only on Garmin
        _seed_realistic_env(data_env, history_days=35,
                            ingest_marker=TODAY - timedelta(days=5))
        on_disk_before = json.loads(
            (data_env / 'fitness_history.json').read_text(encoding='utf-8'))
        # Remove the recent days so the new ingest is observable
        for i in range(5):
            on_disk_before['daily_loads'].pop(
                (TODAY - timedelta(days=i)).isoformat(), None)
        _write(data_env, 'fitness_history.json', on_disk_before)
        assert TODAY.isoformat() not in on_disk_before['daily_loads']

        _patch_garmin(monkeypatch, _default_client())
        result = await _snapshot(mock_ctx, sections=['core'])
        assert 'error' not in result

        on_disk = json.loads(
            (data_env / 'fitness_history.json').read_text(encoding='utf-8'))
        assert TODAY.isoformat() in on_disk['daily_loads'], \
            "daily_loads did not advance after a core-only snapshot"
        assert on_disk['last_activity_ingest_date'] == TODAY.isoformat()

    async def test_sleep_and_readiness_persist_on_core_only_call(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        await _snapshot(mock_ctx, sections=['core'])

        on_disk = json.loads(
            (data_env / 'fitness_history.json').read_text(encoding='utf-8'))
        sleep_dates = {r['date'] for r in on_disk['sleep_history']}
        assert TODAY.isoformat() in sleep_dates, "last night's sleep not persisted"
        readiness_dates = {r['date'] for r in on_disk.get('readiness_history', [])}
        assert TODAY.isoformat() in readiness_dates, "readiness not persisted"


# ---------------------------------------------------------------------------
# Resilience: per-section failure degrades, never aborts
# ---------------------------------------------------------------------------

class TestResilience:
    async def test_activities_failure_degrades_to_data_quality(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, FakeGarminClient(
            fail_activities=True,
            sleep_payloads={TODAY.isoformat(): _sleep_payload(TODAY)},
        ))

        result = await _snapshot(mock_ctx)

        # Degraded, not dead: flag + error envelope + everything local intact
        assert 'activities endpoint down' in result['data_quality']['activities_unavailable']
        assert 'error' in result
        assert result['current_time_context']
        assert result['injuries']
        assert result['coaching_memory']['active_decisions']
        assert result['weekly_plan']['today'] is not None
        # week_grid rebuilt from persisted daily_loads — not all-REST
        non_rest = [d for d in result['week_grid'].values() if not d['is_rest']]
        assert non_rest, "week_grid must fall back to local history, not show REST"

    async def test_readiness_failure_degrades_without_error_envelope(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, FakeGarminClient(
            raw_activities=[_raw_activity(TODAY)],
            sleep_payloads={TODAY.isoformat(): _sleep_payload(TODAY)},
            fail_readiness=True,
        ))

        result = await _snapshot(mock_ctx, sections=['recovery'])

        assert 'error' not in result
        assert result['data_quality']['recovery'] == 'unavailable'
        assert result['recovery']['status'] == 'unavailable'
        # Rest of the snapshot intact
        assert result['week_grid']
        assert result['fitness_metrics']['acwr_status']

    async def test_sleep_failure_degrades_to_data_quality(
            self, data_env, mock_ctx, monkeypatch):
        # No persisted nights, sleep endpoint down -> no_data gate + flags
        _seed_realistic_env(data_env, seed_sleep_history=False)
        _patch_garmin(monkeypatch, FakeGarminClient(
            raw_activities=[_raw_activity(TODAY)],
            fail_sleep=True,
        ))

        result = await _snapshot(mock_ctx)

        assert 'error' not in result
        assert result['sleep_gate'] == {'status': 'no_data'}
        assert result['data_quality']['sleep'] == 'unavailable'
        assert 'sleep endpoint down' in result['data_quality']['sleep_fetch_error']

    async def test_auth_required_surfaces_message_with_local_data(
            self, data_env, mock_ctx, monkeypatch):
        """GarminAuthRequiredError keeps its actionable message in the error
        envelope ALONGSIDE plan, injuries, memory and time context."""
        _seed_realistic_env(data_env)

        def _auth_fail(fn):
            raise GarminAuthRequiredError()
        monkeypatch.setattr(coaching_mod, 'garmin_api_call', _auth_fail)
        monkeypatch.setattr(fitness_mod, 'garmin_api_call', _auth_fail)
        monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', lambda a: a)

        result = await _snapshot(mock_ctx)

        assert 'AUTH_REQUIRED' in result['error']
        assert 'garmin_login.py' in result['error']  # the remediation
        assert result['data_quality']['garmin_auth'] == 'required'
        # Locally derivable data still present
        assert result['current_time_context']
        assert result['weekly_plan']['today'] is not None
        assert result['injuries'][0]['type'] == 'shin'
        assert result['coaching_memory']['active_decisions']
        assert result['plan_adherence']


# ---------------------------------------------------------------------------
# Short-TTL Garmin fetch cache
# ---------------------------------------------------------------------------

class TestGarminFetchCache:
    async def test_back_to_back_calls_reuse_fetches(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = _default_client()
        _patch_garmin(monkeypatch, client)

        await _snapshot(mock_ctx)
        calls_after_first = client.total_calls()
        assert calls_after_first > 0

        await _snapshot(mock_ctx)
        assert client.total_calls() == calls_after_first, \
            "second back-to-back snapshot must reuse cached Garmin data"

    async def test_force_refresh_bypasses_cache(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = _default_client()
        _patch_garmin(monkeypatch, client)

        await _snapshot(mock_ctx)
        calls_after_first = client.total_calls()

        result = await _snapshot(mock_ctx, force_refresh=True)
        assert 'error' not in result
        assert client.total_calls() > calls_after_first

    async def test_ttl_expiry_refetches(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = _default_client()
        _patch_garmin(monkeypatch, client)

        await _snapshot(mock_ctx)
        calls_after_first = client.total_calls()

        # Age every cache entry past the TTL
        for key, (ts, value) in list(coaching_mod._garmin_fetch_cache.items()):
            coaching_mod._garmin_fetch_cache[key] = (
                ts - coaching_mod.GARMIN_CACHE_TTL_SECS - 1, value)

        await _snapshot(mock_ctx)
        assert client.total_calls() > calls_after_first

    async def test_failures_are_not_cached(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = FakeGarminClient(
            fail_activities=True,
            sleep_payloads={TODAY.isoformat(): _sleep_payload(TODAY)},
        )
        _patch_garmin(monkeypatch, client)

        await _snapshot(mock_ctx)
        client.fail_activities = False
        client.raw_activities = [_raw_activity(TODAY)]

        result = await _snapshot(mock_ctx)
        assert 'error' not in result
        assert 'activities_unavailable' not in result.get('data_quality', {})


# ---------------------------------------------------------------------------
# Efficiency: ranged sleep fetch + lazy HR-zone enrichment
# ---------------------------------------------------------------------------

class TestEfficiency:
    async def test_sleep_fetches_only_missing_nights(self, data_env, mock_ctx, monkeypatch):
        """6 of the last 7 nights are persisted — only last night is fetched."""
        _seed_realistic_env(data_env)  # sleep_history has today-1..today-6
        client = _default_client()
        _patch_garmin(monkeypatch, client)

        result = await _snapshot(mock_ctx, sections=['sleep'])

        assert client.calls['get_sleep_data'] == 1
        # Summary still covers all 7 nights (persisted + fresh)
        assert result['sleep']['days_analyzed'] == 7
        assert len(result['sleep']['nights']) == 7

    async def test_sleep_fetch_capped_at_seven_nights(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env, seed_sleep_history=False)
        payloads = {
            (TODAY - timedelta(days=i)).isoformat():
                _sleep_payload(TODAY - timedelta(days=i))
            for i in range(10)
        }
        client = FakeGarminClient(
            raw_activities=[_raw_activity(TODAY)], sleep_payloads=payloads)
        _patch_garmin(monkeypatch, client)

        await _snapshot(mock_ctx)

        assert client.calls['get_sleep_data'] == 7

    async def test_hr_zone_enrichment_only_with_activities_section(
            self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        client = _default_client()
        _patch_garmin(monkeypatch, client)
        enrich_calls = []

        def _recording_enrich(acts):
            enrich_calls.append(len(acts))
            return acts
        monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', _recording_enrich)

        await _snapshot(mock_ctx)  # core
        assert enrich_calls == [], "core must not pay for HR-zone enrichment"

        await _snapshot(mock_ctx, sections=['fitness'])
        assert enrich_calls == [], "fitness section alone must not enrich"

        await _snapshot(mock_ctx, sections=['activities'])
        assert len(enrich_calls) == 1


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

class TestTokenBudget:
    async def test_core_payload_stays_small(self, data_env, mock_ctx, monkeypatch):
        """Core targets ~2-3K tokens; guard at <12000 chars with realistic
        fixture data (35 days of loads, 7-day plan, injuries, memory)."""
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        raw = await get_coaching_snapshot(mock_ctx)

        assert 'error' not in json.loads(raw)
        assert len(raw) < 12000, (
            f"core payload is {len(raw)} chars — token budget blown")

    async def test_core_is_much_smaller_than_full(self, data_env, mock_ctx, monkeypatch):
        _seed_realistic_env(data_env)
        _patch_garmin(monkeypatch, _default_client())

        core_raw = await get_coaching_snapshot(mock_ctx)
        full_raw = await get_coaching_snapshot(mock_ctx, sections=['full'])

        assert len(core_raw) < len(full_raw) * 0.6


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_activities_from_history_synthesizes_dates(self):
        daily_loads = {
            '2026-06-08': {'total': 50, 'by_sport': {'cycling': 50},
                           'activities': [{'id': 1, 'type': 'cycling',
                                           'duration_mins': 60, 'load': 50}]},
            '2026-06-01': {'total': 30, 'by_sport': {'strength': 30},
                           'activities': [{'id': 2, 'type': 'strength_training',
                                           'duration_mins': 40, 'load': 30}]},
        }
        acts = _activities_from_history(daily_loads, '2026-06-05', '2026-06-10')
        assert len(acts) == 1
        assert acts[0]['date'] == '2026-06-08'

    def test_activities_from_history_tolerates_v1_floats(self):
        assert _activities_from_history({'2026-06-08': 50.0}, '2026-06-01', '2026-06-10') == []

    def test_merge_sleep_nights_fetched_wins_and_caps(self):
        history = [{'date': (TODAY - timedelta(days=i)).isoformat(), 'score': 70}
                   for i in range(10)]
        fetched = [{'date': TODAY.isoformat(), 'score': 90, 'deep_mins': 80}]
        merged = _merge_sleep_nights(history, fetched, TODAY)
        assert len(merged) == 7
        assert merged[0]['score'] == 90          # fetched record wins
        assert merged[0]['deep_mins'] == 80      # full detail preserved
        dates = [m['date'] for m in merged]
        assert dates == sorted(dates, reverse=True)

    def test_build_sleep_gate_no_data(self):
        assert _build_sleep_gate(None) == {'status': 'no_data'}
        assert _build_sleep_gate({'status': 'no_data'}) == {'status': 'no_data'}

    def test_build_sleep_gate_deficit_bool(self):
        gate = _build_sleep_gate({
            'status': 'deficit', 'acute_status': 'cautious',
            'avg_duration_hrs': 6.4, 'days_analyzed': 7,
            'nights': [{'score': 55, 'duration_hrs': 6.0}],
        })
        assert gate['deficit'] is True
        assert gate['avg_hours'] == 6.4
        assert gate['last_night_score'] == 55

    def test_count_decisions_due_review(self):
        decisions = [
            {'date': (TODAY - timedelta(days=20)).isoformat()},
            {'date': (TODAY - timedelta(days=8)).isoformat()},
            {'date': TODAY.isoformat()},
            {'date': 'garbage'},
            {},
        ]
        assert _count_decisions_due_review(decisions, TODAY) == 2
