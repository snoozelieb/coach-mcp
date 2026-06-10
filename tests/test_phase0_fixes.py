"""Regression tests for the Phase 0 stabilization fixes (roadmap items 1-10).

1.  v2 daily_loads flattened before calculate_fitness_metrics in planning tools
    (ACWR volume adjustment revives) + explicit fitness_metrics_unavailable flag
2.  `from tools.coaching_tools` -> `from .coaching_tools` (chronic-miss
    diagnostics actually reach get_week_constraints)
3.  normalize_injury tolerates both old ('name'/'restrictions') and real
    ('type'/'restricted_activities') record shapes
4.  pillar_target_minutes accepts both target-key spellings
5.  activity ingestion driven by last_activity_ingest_date + idempotent
    trailing 3-day re-ingest on every snapshot
6.  workout_builder power/pace zone loaders read the real athlete file path
7.  bedtime drift handles epoch-ms ints / None without crashing; sleep parse
    normalizes epoch-ms to ISO
8.  coaching memory surfaces the MOST RECENT decisions/responses
9.  plan lifecycle: week bounds derived, stale days pruned + archived,
    expired plan flagged (plan_stale / plan_expired / days_uncoached)
10. injury write-gate in update_weekly_plan and push_plan_to_garmin
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod

from coach.rules import normalize_injury, pillar_target_minutes, convert_athlete_pillars_to_legacy
from coach.parsers import epoch_ms_to_local_iso
from coach.fitness import detect_bedtime_drift, get_sleep_summary
from coach.planner import get_week_constraints as planner_week_constraints
from coach.workout_builder import get_athlete_power_zones, get_athlete_running_zones
from coach.tools.coaching_tools import get_coaching_snapshot, _build_compliance_diagnostics
from coach.tools.planning_tools import (
    get_periodization_status,
    get_weekly_prescription,
    get_week_constraints,
    update_weekly_plan,
    push_plan_to_garmin,
    _injury_gate_violations,
    _activity_matches_restriction,
)

TODAY = date.today()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def data_env(data_dir, monkeypatch):
    """Redirect DATA_DIR in every module that does file I/O to a tmp dir."""
    for mod in (planner, rules, fitness_mod, parsers_mod, workout_builder,
                coaching_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


def _v2_day(load, day_iso, act_type='cycling'):
    return {
        'total': load,
        'by_sport': {'cycling': load},
        'activities': [{
            'id': 1, 'type': act_type, 'sport': 'cycling',
            'duration_mins': 60, 'load': load, 'date': day_iso,
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


class FakeGarminClient:
    """Minimal fake for the Garmin client methods the snapshot exercises."""

    def __init__(self, raw_activities=None, sleep_payload=None):
        self.raw_activities = raw_activities or []
        self.sleep_payload = sleep_payload or {}
        self.activity_calls = []

    def get_activities_by_date(self, start, end):
        self.activity_calls.append((start, end))
        return [
            a for a in self.raw_activities
            if start <= a['startTimeLocal'][:10] <= end
        ]

    def get_training_readiness(self, d):
        return []

    def get_hrv_data(self, d):
        return None

    def get_sleep_data(self, d):
        return self.sleep_payload


def _patch_garmin(monkeypatch, client):
    """Patch garmin_api_call everywhere the snapshot uses it."""
    fake_call = lambda fn: fn(client)
    monkeypatch.setattr(coaching_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(fitness_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(planning_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', lambda acts: acts)


# ---------------------------------------------------------------------------
# Item 1: v2 daily_loads -> fitness metrics in planning tools
# ---------------------------------------------------------------------------

class TestPlanningToolsFitnessMetrics:
    def _seed_history(self, data_dir, spike=False):
        daily_loads = {}
        for i in range(47):
            ds = (TODAY - timedelta(days=i)).isoformat()
            load = 100.0 if (spike and i < 7) else 10.0
            daily_loads[ds] = _v2_day(load, ds)
        _write(data_dir, 'fitness_history.json', {
            'schema_version': 2,
            'daily_loads': daily_loads,
            'snapshots': [],
            'sleep_history': [],
            'last_updated': TODAY.isoformat(),
        })

    def test_periodization_status_computes_metrics_from_v2_loads(self, data_env):
        """v2 dict daily_loads must be flattened — fitness_status present."""
        self._seed_history(data_env)
        _write(data_env, 'training_config.json', {'current_block': {'phase': 'base'}})

        result = json.loads(get_periodization_status())

        assert 'error' not in result
        assert 'fitness_status' in result, "ACWR metrics silently dropped (v2 dict fed raw)"
        assert result['fitness_status']['ctl'] > 0
        assert 'fitness_metrics_unavailable' not in result

    def test_periodization_status_flags_unavailable_metrics(self, data_env):
        """A genuinely broken fitness history is flagged, not silently swallowed."""
        (data_env / 'fitness_history.json').write_text('{not valid json', encoding='utf-8')
        _write(data_env, 'training_config.json', {'current_block': {'phase': 'base'}})

        result = json.loads(get_periodization_status())

        assert 'error' not in result
        assert result.get('fitness_metrics_unavailable') is True
        assert 'fitness_status' not in result

    def test_prescription_acwr_volume_adjustment_fires(self, data_env, monkeypatch):
        """A recent load spike must reduce prescribed volume (ACWR > 1.3)."""
        self._seed_history(data_env, spike=True)
        _write(data_env, 'training_config.json', {
            'current_block': {'phase': 'base', 'weekly_volume_target_hrs': 6.0},
        })
        _write(data_env, 'athlete.json', {'personal': {}})
        _patch_garmin(monkeypatch, FakeGarminClient())

        result = json.loads(get_weekly_prescription())

        assert 'error' not in result
        assert result['volume']['adjustment'] == 0.85
        assert result['volume']['adjustment_reason'] == 'Reduced due to elevated ACWR'
        assert 'current_fitness' in result

    def test_prescription_flags_unavailable_metrics(self, data_env, monkeypatch):
        (data_env / 'fitness_history.json').write_text('{not valid json', encoding='utf-8')
        _write(data_env, 'training_config.json', {
            'current_block': {'phase': 'base', 'weekly_volume_target_hrs': 6.0},
        })
        _write(data_env, 'athlete.json', {'personal': {}})
        _patch_garmin(monkeypatch, FakeGarminClient())

        result = json.loads(get_weekly_prescription())

        assert 'error' not in result
        assert result.get('fitness_metrics_unavailable') is True
        assert result['volume']['adjustment'] == 1.0


# ---------------------------------------------------------------------------
# Item 2: relative import fix — diagnostics reach get_week_constraints
# ---------------------------------------------------------------------------

class TestWeekConstraintsImportFix:
    def test_chronic_misses_surface_in_constraints_tool(self, data_env):
        """The broken 'from tools.coaching_tools' import silently dropped
        compliance diagnostics; chronic misses must now surface."""
        _write(data_env, 'athlete.json', {
            'personal': {},
            'training_pillars': {
                'strength': {
                    'target_type': 'sessions',
                    'target_sessions_per_week': 2,
                    'types': ['strength_training'],
                },
            },
        })
        daily_loads = {}
        for i in range(28):
            ds = (TODAY - timedelta(days=i)).isoformat()
            daily_loads[ds] = _v2_day(20.0, ds, act_type='cycling')
        _write(data_env, 'fitness_history.json', {
            'schema_version': 2,
            'daily_loads': daily_loads,
            'snapshots': [],
            'sleep_history': [],
            'last_updated': TODAY.isoformat(),
        })

        result = json.loads(get_week_constraints())

        assert 'error' not in result
        assert 'chronic_misses' in result, "compliance diagnostics never loaded (broken import)"
        assert 'strength' in result['chronic_misses']


# ---------------------------------------------------------------------------
# Item 3: injury key normalizer
# ---------------------------------------------------------------------------

class TestNormalizeInjury:
    def test_real_record_shape(self):
        record = {
            'date': '2026-06-01', 'body_region': 'shin', 'type': 'shin',
            'status': 'active', 'severity': 'moderate',
            'restricted_activities': ['running', 'jumping'],
            'safe_activities': ['cycling'],
        }
        norm = normalize_injury(record)
        assert norm['type'] == 'shin'
        assert norm['name'] == 'shin'
        assert norm['status'] == 'active'
        assert norm['restricted_activities'] == ['running', 'jumping']
        assert norm['safe_activities'] == ['cycling']

    def test_old_record_shape(self):
        record = {'name': 'knee', 'status': 'active', 'restrictions': ['no running']}
        norm = normalize_injury(record)
        assert norm['type'] == 'knee'
        assert norm['name'] == 'knee'
        assert norm['restricted_activities'] == ['no running']

    def test_degenerate_inputs(self):
        assert normalize_injury(None)['type'] == 'unknown'
        assert normalize_injury({})['restricted_activities'] == []
        assert normalize_injury({'restricted_activities': 'not-a-list'})['restricted_activities'] == []

    def test_week_constraints_reads_real_injury_keys(self):
        """get_week_constraints previously read 'name'/'restrictions' which
        real records never have — restrictions came back empty."""
        injuries = [{
            'date': '2026-06-01', 'type': 'shin', 'body_region': 'shin',
            'status': 'active', 'severity': 'moderate',
            'restricted_activities': ['running', 'jumping'],
            'safe_activities': ['cycling'],
        }]
        result = planner_week_constraints(
            athlete={}, training_config={}, methodology={}, injuries=injuries,
        )
        entry = result['injury_restrictions'][0]
        assert entry['name'] == 'shin'
        assert entry['restricted_activities'] == ['running', 'jumping']
        # Legacy alias kept populated too
        assert entry['restrictions'] == ['running', 'jumping']

    def test_week_constraints_still_accepts_old_shape(self):
        injuries = [
            {'name': 'knee', 'status': 'active', 'severity': 'moderate',
             'restrictions': ['no running']},
            {'name': 'old_ankle', 'status': 'resolved'},
        ]
        result = planner_week_constraints(
            athlete={}, training_config={}, methodology={}, injuries=injuries,
        )
        assert len(result['injury_restrictions']) == 1
        assert result['injury_restrictions'][0]['name'] == 'knee'
        assert result['injury_restrictions'][0]['restricted_activities'] == ['no running']


# ---------------------------------------------------------------------------
# Item 4: pillar target key normalizer
# ---------------------------------------------------------------------------

class TestPillarTargetMinutes:
    def test_accepts_both_spellings(self):
        assert pillar_target_minutes({'target_mins_per_week': 90}) == 90
        assert pillar_target_minutes({'target_minutes_per_week': 90}) == 90
        assert pillar_target_minutes({'target_mins_per_week': 60,
                                      'target_minutes_per_week': 90}) == 60
        assert pillar_target_minutes({}) == 0
        assert pillar_target_minutes(None) == 0

    def test_convert_legacy_handles_both_spellings(self):
        for key in ('target_mins_per_week', 'target_minutes_per_week'):
            pillars = {'pillars': [
                {'name': 'mobility', 'target_type': 'minutes', key: 90},
            ]}
            legacy = convert_athlete_pillars_to_legacy(pillars)
            assert legacy['mobility_minutes_per_week'] == 90, f"{key} not read"

    def test_compliance_diagnostics_reads_live_key(self, data_env):
        """Live data uses 'target_mins_per_week' — a met pillar must not be
        scored as a chronic miss because the target read as 0."""
        pillars = {'mobility': {
            'target_type': 'minutes',
            'target_mins_per_week': 60,
            'types': ['yoga'],
        }}
        week = [{'type': 'yoga', 'duration_mins': 70}]
        result = _build_compliance_diagnostics([week, week, week, week], pillars)
        assert result['per_pillar']['mobility']['met_weeks'] == 4
        assert result['per_pillar']['mobility']['chronic_miss'] is False

    def test_planner_constraints_reads_live_key(self):
        athlete = {'training_pillars': {
            'mobility': {
                'target_type': 'minutes',
                'target_mins_per_week': 90,
                'types': ['yoga'],
            },
        }}
        result = planner_week_constraints(athlete=athlete, training_config={}, methodology={})
        assert result['pillar_requirements']['mobility']['min_mins'] == 90


# ---------------------------------------------------------------------------
# Item 5 + 8: snapshot ingestion + coaching memory recency
# ---------------------------------------------------------------------------

def _seed_snapshot_env(data_dir):
    """Fitness history whose ingestion froze 18 days ago but whose
    last_updated is today (the exact condition that killed ingestion)."""
    daily_loads = {}
    for i in range(18, 25):
        ds = (TODAY - timedelta(days=i)).isoformat()
        daily_loads[ds] = _v2_day(40.0, ds)
    _write(data_dir, 'fitness_history.json', {
        'schema_version': 2,
        'daily_loads': daily_loads,
        'snapshots': [],
        'sleep_history': [],
        'readiness_history': [],
        'last_updated': TODAY.isoformat(),  # bumped by sleep/readiness saves
    })
    _write(data_dir, 'athlete.json', {'personal': {'name': 'T', 'age': 30, 'weight_kg': 70}})
    _write(data_dir, 'training_config.json', {'current_block': {'phase': 'base'}})

    decisions = [
        {'id': f'd{i}', 'date': (TODAY - timedelta(days=8 - i)).isoformat(),
         'type': 'load_adjustment', 'decision': f'decision {i}',
         'status': 'active'}
        for i in range(1, 8)  # d1 oldest (8-1=7 days ago) ... d7 newest (1 day ago)
    ]
    responses = [
        {'date': (TODAY - timedelta(days=7 - i)).isoformat(),
         'stimulus': f'stimulus {i}', 'response': 'good',
         'pattern': 'handles_volume_well'}
        for i in range(1, 7)  # oldest first
    ]
    _write(data_dir, 'coaching_log.json', {
        'decisions': decisions,
        'athlete_responses': responses,
        'pending_approvals': [],
    })


class TestSnapshotIngestion:
    async def test_ingestion_resumes_despite_fresh_last_updated(
            self, data_env, mock_ctx, monkeypatch):
        client = FakeGarminClient(raw_activities=[
            _raw_activity(TODAY),
            _raw_activity(TODAY - timedelta(days=2)),
        ])
        _seed_snapshot_env(data_env)
        _patch_garmin(monkeypatch, client)

        result = json.loads(await get_coaching_snapshot(mock_ctx))
        assert 'error' not in result

        # First Garmin activity call is the ingest — it must extend back to
        # the last actually-ingested day (18 days ago), not be skipped.
        ingest_start, ingest_end = client.activity_calls[0]
        assert ingest_start == (TODAY - timedelta(days=18)).isoformat()
        assert ingest_end == TODAY.isoformat()

        on_disk = json.loads((data_env / 'fitness_history.json').read_text())
        assert TODAY.isoformat() in on_disk['daily_loads'], "today's activity not ingested"
        assert on_disk['last_activity_ingest_date'] == TODAY.isoformat()
        # Ingestion is fresh — no stale flag
        assert result.get('data_quality', {}).get('fitness_history') != 'stale'

    async def test_trailing_3_day_reingest_always_runs(
            self, data_env, mock_ctx, monkeypatch):
        client = FakeGarminClient(raw_activities=[_raw_activity(TODAY)])
        _seed_snapshot_env(data_env)
        _patch_garmin(monkeypatch, client)

        await get_coaching_snapshot(mock_ctx)  # sets last_activity_ingest_date
        calls_after_first = len(client.activity_calls)
        await get_coaching_snapshot(mock_ctx)  # fully fresh — must STILL re-ingest

        second_ingest_start, _ = client.activity_calls[calls_after_first]
        assert second_ingest_start == (TODAY - timedelta(days=3)).isoformat()

    async def test_coaching_memory_is_most_recent_first(
            self, data_env, mock_ctx, monkeypatch):
        client = FakeGarminClient(raw_activities=[_raw_activity(TODAY)])
        _seed_snapshot_env(data_env)
        _patch_garmin(monkeypatch, client)

        result = json.loads(await get_coaching_snapshot(mock_ctx))
        memory = result['coaching_memory']

        decisions = memory['active_decisions']
        assert len(decisions) == 5
        # Newest decision (d7, 1 day ago) first — NOT the oldest five
        assert decisions[0]['id'] == 'd7'
        assert decisions[0]['date'] == (TODAY - timedelta(days=1)).isoformat()
        returned_ids = [d['id'] for d in decisions]
        assert returned_ids == ['d7', 'd6', 'd5', 'd4', 'd3']

        responses = memory['recent_responses']
        assert len(responses) == 3
        dates = [r['date'] for r in responses]
        assert dates[0] == (TODAY - timedelta(days=1)).isoformat()
        assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Item 6: workout_builder zone paths
# ---------------------------------------------------------------------------

class TestZoneLoadingPaths:
    def test_power_zones_loaded_from_data_dir(self, data_env):
        zones = {'z2_endurance': [120, 160], 'z4_threshold': [200, 230]}
        _write(data_env, 'athlete.json', {'personal': {'power_zones': zones}})

        assert get_athlete_power_zones() == zones

    def test_running_zones_calculated_from_threshold_pace(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {'threshold_pace_sec_per_km': 300, 'pace_zones': None},
        })

        zones = get_athlete_running_zones()
        assert zones, "pace zones lost — wrong athlete.json path"
        assert zones['z4_threshold'] == [288, 312]  # 300 * [0.96, 1.04]

    def test_missing_athlete_file_returns_empty(self, data_env):
        assert get_athlete_power_zones() == {}
        assert get_athlete_running_zones() == {}


# ---------------------------------------------------------------------------
# Item 7: epoch-ms bedtime handling
# ---------------------------------------------------------------------------

def _local_epoch_ms(d, hour, minute=0):
    """Garmin '...Local' convention: epoch-ms whose UTC reading is local time."""
    return int(datetime(d.year, d.month, d.day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


class TestEpochMsBedtimes:
    def test_epoch_ms_to_local_iso(self):
        d = TODAY - timedelta(days=1)
        ms = _local_epoch_ms(d, 22, 30)
        assert epoch_ms_to_local_iso(ms) == f'{d.isoformat()}T22:30:00'
        assert epoch_ms_to_local_iso(None) is None
        assert epoch_ms_to_local_iso('2026-06-01T22:00:00') == '2026-06-01T22:00:00'
        assert epoch_ms_to_local_iso(object()) is None

    def test_bedtime_drift_survives_epoch_ms_ints(self):
        """Epoch-ms bedtimes previously raised AttributeError (.split on int)
        inside the snapshot path — must now compute normally."""
        nights = []
        for i in range(10):
            d = TODAY - timedelta(days=10 - i)
            nights.append({
                'date': d.isoformat(),
                'bedtime': _local_epoch_ms(d, 22),
            })
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'ok'
        assert result['direction'] == 'stable'
        assert result['current_avg_bedtime'] == '22:00'

    def test_bedtime_drift_survives_garbage(self):
        nights = []
        for i in range(10):
            d = TODAY - timedelta(days=10 - i)
            nights.append({'date': d.isoformat(),
                           'bedtime': None if i % 2 else ['nonsense']})
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'insufficient_data'

    def test_get_sleep_summary_normalizes_epoch_ms(self, monkeypatch):
        bed_ms = _local_epoch_ms(TODAY - timedelta(days=1), 22, 15)
        wake_ms = _local_epoch_ms(TODAY, 6, 0)
        payload = {'dailySleepDTO': {
            'sleepTimeSeconds': 7 * 3600,
            'sleepScores': {'overall': {'value': 80, 'qualifierKey': 'GOOD'}},
            'deepSleepSeconds': 5400,
            'remSleepSeconds': 5400,
            'lightSleepSeconds': 14400,
            'awakeSleepSeconds': 600,
            'sleepStartTimestampLocal': bed_ms,
            'sleepEndTimestampLocal': wake_ms,
            'avgSleepStress': 12,
            'avgHeartRate': 50,
        }}
        client = FakeGarminClient(sleep_payload=payload)
        monkeypatch.setattr(fitness_mod, 'garmin_api_call', lambda fn: fn(client))

        summary = get_sleep_summary(TODAY, days=2)

        night = summary['nights'][0]
        assert isinstance(night['bedtime'], str)
        assert night['bedtime'].endswith('T22:15:00')
        assert isinstance(night['wake_time'], str)
        # The normalized string must be drift-computable
        assert fitness_mod.detect_bedtime_drift(
            [dict(night, date=(TODAY - timedelta(days=i)).isoformat()) for i in range(10)]
        )['status'] == 'ok'


# ---------------------------------------------------------------------------
# Item 9a: plan lifecycle — week bounds + pruning + archive
# ---------------------------------------------------------------------------

class TestPlanLifecycle:
    def test_week_bounds_derived_and_stale_days_archived(self, data_env):
        old_day = (TODAY - timedelta(days=12)).isoformat()
        d0, d1 = TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat()
        plan = {'days': {
            old_day: {'planned': {'type': 'cycling', 'duration_mins': 60}},
            d0: {'planned': {'type': 'cycling', 'duration_mins': 60}},
            d1: {'planned': {'type': 'strength', 'duration_mins': 45}},
        }}

        result = json.loads(update_weekly_plan(json.dumps(plan)))
        assert result['status'] == 'success'

        saved = json.loads((data_env / 'weekly_plan.json').read_text())
        assert set(saved['days'].keys()) == {d0, d1}, "stale day not pruned"
        assert saved['week_start'] == d0
        assert saved['week_end'] == d1

        archive = json.loads((data_env / 'plan_history.json').read_text())
        archived_dates = [e['date'] for e in archive['archived_days']]
        assert old_day in archived_dates

    def test_existing_week_bounds_preserved(self, data_env):
        d0, d1 = TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat()
        plan = {
            'week_start': d0, 'week_end': (TODAY + timedelta(days=6)).isoformat(),
            'days': {d0: {'planned': None}, d1: {'planned': None}},
        }
        result = json.loads(update_weekly_plan(json.dumps(plan)))
        assert result['status'] == 'success'
        saved = json.loads((data_env / 'weekly_plan.json').read_text())
        assert saved['week_end'] == (TODAY + timedelta(days=6)).isoformat()


# ---------------------------------------------------------------------------
# Item 9b: expired plan signalled loudly in the snapshot
# ---------------------------------------------------------------------------

class TestExpiredPlanSignal:
    async def test_expired_plan_sets_flags_and_skips_anomalies(
            self, data_env, mock_ctx, monkeypatch):
        _seed_snapshot_env(data_env)
        days = {}
        for offset in (12, 11, 10):
            ds = (TODAY - timedelta(days=offset)).isoformat()
            days[ds] = {'planned': {'type': 'cycling', 'duration_mins': 60}}
        _write(data_env, 'weekly_plan.json', {
            'week_start': (TODAY - timedelta(days=12)).isoformat(),
            'week_end': (TODAY - timedelta(days=10)).isoformat(),
            'days': days,
        })
        client = FakeGarminClient(raw_activities=[_raw_activity(TODAY)])
        _patch_garmin(monkeypatch, client)

        result = json.loads(await get_coaching_snapshot(mock_ctx))
        assert 'error' not in result

        pva = result['planned_vs_actual']
        assert pva['status'] == 'plan_expired'
        assert pva['days_since_expiry'] == 10
        assert 'anomalies' not in pva, "false-anomaly flood not suppressed"

        assert result['data_quality']['plan_stale'] is True
        assert result['data_quality']['plan_days_since_expiry'] == 10

        assert result['flags']['plan_expired'] is True
        assert result['flags']['days_uncoached'] == 10

    async def test_current_plan_not_flagged(self, data_env, mock_ctx, monkeypatch):
        _seed_snapshot_env(data_env)
        ds = TODAY.isoformat()
        _write(data_env, 'weekly_plan.json', {
            'week_start': ds, 'week_end': ds,
            'days': {ds: {'planned': {'type': 'cycling', 'duration_mins': 60}}},
        })
        client = FakeGarminClient(raw_activities=[_raw_activity(TODAY)])
        _patch_garmin(monkeypatch, client)

        result = json.loads(await get_coaching_snapshot(mock_ctx))
        assert 'error' not in result
        assert result['planned_vs_actual'].get('status') != 'plan_expired'
        assert 'plan_stale' not in result.get('data_quality', {})
        assert 'plan_expired' not in result.get('flags', {})


# ---------------------------------------------------------------------------
# Item 10: injury write-gate
# ---------------------------------------------------------------------------

ACTIVE_RUN_INJURY = {
    'date': '2026-06-01', 'type': 'shin', 'body_region': 'shin',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running'],
    'safe_activities': ['cycling'],
}


class TestInjuryWriteGate:
    def test_restriction_alias_matching(self):
        assert _activity_matches_restriction('trail_running', 'run')
        assert _activity_matches_restriction('running', 'running')
        assert _activity_matches_restriction('run', 'running')
        assert not _activity_matches_restriction('strength', 'running')
        assert not _activity_matches_restriction('', 'running')

    def test_violations_include_nested_sessions(self):
        days = {
            TODAY.isoformat(): {'planned': {
                'type': 'double_session',
                'sessions': [
                    {'type': 'running', 'duration_mins': 30},
                    {'type': 'strength', 'duration_mins': 30},
                ],
            }},
        }
        violations = _injury_gate_violations(days, [ACTIVE_RUN_INJURY])
        assert len(violations) == 1
        assert violations[0]['session_type'] == 'running'

    def test_rest_days_never_violate(self):
        days = {TODAY.isoformat(): {'planned': {'type': 'rest'}}}
        assert _injury_gate_violations(days, [ACTIVE_RUN_INJURY]) == []

    def test_resolved_injury_does_not_gate(self):
        resolved = dict(ACTIVE_RUN_INJURY, status='resolved')
        days = {TODAY.isoformat(): {'planned': {'type': 'running', 'duration_mins': 30}}}
        assert _injury_gate_violations(days, [resolved]) == []

    def test_update_weekly_plan_rejects_restricted_session(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        plan = {'days': {tomorrow: {
            'planned': {'type': 'trail_running', 'duration_mins': 45},
        }}}

        result = json.loads(update_weekly_plan(json.dumps(plan)))

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['date'] == tomorrow
        assert result['violations'][0]['session_type'] == 'trail_running'
        assert not (data_env / 'weekly_plan.json').exists(), "plan saved despite gate"

    def test_update_weekly_plan_override_saves_with_warning(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        plan = {'days': {tomorrow: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}}

        result = json.loads(update_weekly_plan(json.dumps(plan), override_injury_gate=True))

        assert result['status'] == 'success'
        assert result['injury_gate']['injury_gate_overridden'] is True
        assert (data_env / 'weekly_plan.json').exists()

    def test_update_weekly_plan_safe_sessions_pass(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        plan = {'days': {tomorrow: {
            'planned': {'type': 'cycling', 'duration_mins': 60},
        }}}

        result = json.loads(update_weekly_plan(json.dumps(plan)))
        assert result['status'] == 'success'
        assert 'injury_gate' not in result

    def test_push_plan_blocked_by_gate(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        _write(data_env, 'weekly_plan.json', {'days': {tomorrow: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}})

        result = json.loads(push_plan_to_garmin())

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['date'] == tomorrow

    def test_push_plan_override_bypasses_gate(self, data_env, monkeypatch):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        _write(data_env, 'weekly_plan.json', {'days': {tomorrow: {
            'planned': {'type': 'running', 'duration_mins': 30,
                        'description': 'Easy run'},
        }}})

        def _fail(*args, **kwargs):
            raise Exception("Garmin unavailable")
        monkeypatch.setattr(planning_mod, 'garmin_api_call', _fail)

        result = json.loads(push_plan_to_garmin(override_injury_gate=True))

        # Gate bypassed: we got past it to the (failing) upload stage
        assert result.get('error') != 'injury_gate'
        assert result['injury_gate']['injury_gate_overridden'] is True
        assert len(result['errors']) == 1
