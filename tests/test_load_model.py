"""Golden-value tests for the load model (Phase 1.5 — shadow then cutover).

Pins BOTH ACWR models against hand-computed values so any future change to
either is intentional:

- EWMA model (CURRENT decision model): k = 2/(N+1). The pinned numbers
  encode today's behavior — if anyone changes the decay constant (e.g. to
  the TrainingPeaks k = 1/N convention) these tests MUST fail, because
  cutover requires recomputing historical snapshots and recalibrating every
  consumer constant in the same commit.
- Classic rolling model (SHADOW): acute = 7-day mean daily load, chronic =
  28-day mean (coupled windows). Constant load => exactly 1.0; a known
  spike week => a hand-derivable ratio.

Also covers: snapshot upsert-by-date dedup (same-day re-runs must replace,
not append), get_fitness_trend day-span math (was dividing by snapshot
count), zone classification boundaries, the snapshot/tool exposure of
acwr_shadow, and the read-only shadow report script.
"""
import json
from datetime import date, timedelta

import pytest

import coach.fitness as fitness
import coach.planner as planner
import coach.rules as rules
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod
import coach.tools.fitness_tools as fitness_tools_mod

from coach.fitness import (
    ACWR_ROLLING_ACUTE_DAYS,
    ACWR_ROLLING_CHRONIC_DAYS,
    calculate_ewma,
    calculate_fitness_metrics,
    calculate_rolling_acwr,
    classify_acwr_zone,
    get_fitness_trend,
    update_fitness_history,
)
from coach.tools.coaching_tools import get_coaching_snapshot
from coach.tools.fitness_tools import query_metrics

from scripts.acwr_shadow_report import (
    build_shadow_report,
    format_report,
    load_history,
)

TODAY = date.today()
AS_OF = date(2026, 6, 1)  # fixed date for golden-value determinism

SHADOW_NOTE = 'shadow model — comparison period running until cutover'


def _constant_loads(load: float, days: int, end: date) -> dict[str, float]:
    """Flat daily_loads: `days` consecutive days at `load`, ending at `end`."""
    return {
        (end - timedelta(days=i)).isoformat(): load
        for i in range(days)
    }


def _spike_loads(base: float, spike: float, end: date) -> dict[str, float]:
    """28 days at `base` followed by a 7-day spike at `spike`, ending at `end`."""
    loads = {}
    for i in range(35):
        d = (end - timedelta(days=i)).isoformat()
        loads[d] = spike if i < 7 else base
    return loads


def _v2_day(load: float, sport: str = 'cycling') -> dict:
    return {
        'total': load,
        'by_sport': {sport: load},
        'activities': [{'id': 1, 'type': sport, 'sport': sport,
                        'duration_mins': 60, 'load': load}],
    }


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


# ---------------------------------------------------------------------------
# Rolling (shadow) model — golden values
# ---------------------------------------------------------------------------

class TestRollingAcwrGoldenValues:
    def test_window_constants_are_classic_research_windows(self):
        assert ACWR_ROLLING_ACUTE_DAYS == 7
        assert ACWR_ROLLING_CHRONIC_DAYS == 28

    def test_constant_load_is_exactly_one(self):
        """Constant daily load => acute mean == chronic mean => exactly 1.0."""
        loads = _constant_loads(50.0, 35, AS_OF)
        m = calculate_fitness_metrics(loads, AS_OF)
        assert m['acwr_rolling'] == 1.0
        assert m['acwr_rolling_status'] == {
            'value': 1.0, 'zone': 'optimal', 'safe': True,
        }

    def test_spike_week_known_ratio(self):
        """28d @ 50 then 7d @ 100:
        acute = 100, chronic = (21*50 + 7*100)/28 = 62.5 => 1.6 exactly."""
        loads = _spike_loads(50.0, 100.0, AS_OF)
        m = calculate_fitness_metrics(loads, AS_OF)
        assert m['acwr_rolling'] == 1.6
        assert m['acwr_rolling_status']['zone'] == 'danger'
        assert m['acwr_rolling_status']['safe'] is False

    def test_direct_function_spike_series(self):
        loads_list = [50.0] * 28 + [100.0] * 7
        assert calculate_rolling_acwr(loads_list) == 1.6

    def test_direct_function_constant_series(self):
        assert calculate_rolling_acwr([50.0] * 35) == 1.0

    def test_rounds_to_two_decimals(self):
        # acute = 50, chronic = (21*40 + 7*50)/28 = 42.5 => 1.17647... => 1.18
        loads_list = [40.0] * 21 + [50.0] * 7
        assert calculate_rolling_acwr(loads_list) == 1.18

    def test_empty_series_returns_neutral(self):
        assert calculate_rolling_acwr([]) == 1.0

    def test_all_zero_loads_returns_neutral(self):
        assert calculate_rolling_acwr([0.0] * 35) == 1.0

    def test_leading_zeros_outside_chronic_window_ignored(self):
        """Days before the 28-day chronic window must not dilute the ratio."""
        loads_list = [0.0] * 15 + [50.0] * 28
        assert calculate_rolling_acwr(loads_list) == 1.0

    def test_existing_keys_unchanged(self):
        """Adding the shadow model must not touch existing return keys."""
        m = calculate_fitness_metrics(_constant_loads(50.0, 35, AS_OF), AS_OF)
        for key in ('ctl', 'atl', 'tsb', 'acwr', 'acwr_status', 'acwr_risk',
                    'days_analyzed', 'days_with_data', 'data_sufficient',
                    'as_of_date'):
            assert key in m, f'missing pre-existing key {key}'


# ---------------------------------------------------------------------------
# EWMA (current) model — golden values pinning the existing constants
# ---------------------------------------------------------------------------

class TestEwmaGoldenValuesPinned:
    def test_two_value_series_pins_k(self):
        """k = 2/(7+1) = 0.25: ewma = 20*0.25 + 10*0.75 = 12.5.
        (TrainingPeaks k = 1/7 would give ~11.4 — a change must fail here.)"""
        assert calculate_ewma([10, 20], 7) == 12.5

    def test_step_series_pins_decay_rate(self):
        """[0]*7 then [100]*7 with N=7: 100*(1 - 0.75^7) = 86.65... => 86.7.
        (k = 1/N would give 66.0 — any decay-constant change must fail here.)"""
        assert calculate_ewma([0] * 7 + [100] * 7, 7) == 86.7

    def test_constant_series_equals_constant(self):
        assert calculate_ewma([42.0] * 30, 42) == 42.0

    def test_full_metrics_constant_load_golden(self):
        """35 days @ 50/day (49-day window zero-padded at the front):
        CTL 40.6, ATL 50.0, TSB -9.4, EWMA-ACWR 1.23 ('optimal').

        NOTE the audit finding made visible: perfectly constant load reads
        as EWMA-ACWR 1.23 while the rolling model reads exactly 1.0. This
        divergence is WHY the shadow comparison exists.
        """
        m = calculate_fitness_metrics(_constant_loads(50.0, 35, AS_OF), AS_OF)
        assert m['ctl'] == 40.6
        assert m['atl'] == 50.0
        assert m['tsb'] == -9.4
        assert m['acwr'] == 1.23
        assert m['acwr_status'] == 'optimal'
        assert m['acwr_rolling'] == 1.0

    def test_full_metrics_spike_golden(self):
        """Spike week: EWMA reads 1.71 (danger); rolling reads 1.6 (danger).
        Both flag the spike — magnitudes differ between models."""
        m = calculate_fitness_metrics(_spike_loads(50.0, 100.0, AS_OF), AS_OF)
        assert m['acwr'] == 1.71
        assert m['acwr_status'] == 'danger'
        assert m['acwr_rolling'] == 1.6


# ---------------------------------------------------------------------------
# Zone classification boundaries (research thresholds 0.8 / 1.3 / 1.5)
# ---------------------------------------------------------------------------

class TestAcwrZoneBoundaries:
    @pytest.mark.parametrize('value,expected_zone', [
        (0.0, 'low'),
        (0.79, 'low'),
        (0.8, 'optimal'),    # inclusive lower bound of sweet spot
        (1.0, 'optimal'),
        (1.3, 'optimal'),    # inclusive upper bound of sweet spot
        (1.31, 'elevated'),
        (1.5, 'elevated'),   # inclusive upper bound of elevated
        (1.51, 'danger'),
        (2.0, 'danger'),
    ])
    def test_zone_boundaries(self, value, expected_zone):
        zone, risk = classify_acwr_zone(value)
        assert zone == expected_zone
        assert isinstance(risk, str) and risk

    def test_safe_flag_semantics(self):
        """low/optimal are 'safe'; elevated/danger are not."""
        for value, expected_safe in [(0.5, True), (1.0, True),
                                     (1.4, False), (1.7, False)]:
            zone, _ = classify_acwr_zone(value)
            assert (zone in ('optimal', 'low')) is expected_safe


# ---------------------------------------------------------------------------
# Snapshot upsert-by-date (same-day re-runs must not append duplicates)
# ---------------------------------------------------------------------------

class TestSnapshotUpsertByDate:
    def test_same_day_rerun_replaces_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        today_iso = TODAY.isoformat()
        acts = [{'date': today_iso, 'type': 'cycling', 'duration_mins': 60,
                 'garmin_training_load': 50.0}]

        update_fitness_history(acts, TODAY)
        h2 = update_fitness_history(acts, TODAY)  # same-day re-run (snapshot path)

        todays = [s for s in h2['snapshots'] if s['date'] == today_iso]
        assert len(todays) == 1

    def test_replacement_carries_latest_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        today_iso = TODAY.isoformat()

        h1 = update_fitness_history([
            {'date': today_iso, 'type': 'cycling', 'duration_mins': 60,
             'garmin_training_load': 50.0},
        ], TODAY)
        first_atl = h1['snapshots'][-1]['total']['atl']

        h2 = update_fitness_history([
            {'date': today_iso, 'type': 'cycling', 'duration_mins': 90,
             'garmin_training_load': 120.0},
        ], TODAY)
        todays = [s for s in h2['snapshots'] if s['date'] == today_iso]
        assert len(todays) == 1
        assert todays[0]['total']['atl'] > first_atl  # latest write wins

    def test_snapshots_sorted_and_unique_after_upsert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        today_iso = TODAY.isoformat()
        # Seed an out-of-order history that already contains a stale
        # same-date snapshot (the pre-fix append bug shape)
        _write(tmp_path, 'fitness_history.json', {
            'schema_version': 2,
            'daily_loads': {},
            'snapshots': [
                {'date': today_iso,
                 'total': {'ctl': 99, 'atl': 99, 'tsb': 0, 'acwr': 1.0}},
                {'date': (TODAY - timedelta(days=3)).isoformat(),
                 'total': {'ctl': 10, 'atl': 12, 'tsb': -2, 'acwr': 1.1}},
            ],
            'sleep_history': [],
            'last_updated': today_iso,
        })

        h = update_fitness_history([
            {'date': today_iso, 'type': 'cycling', 'duration_mins': 60,
             'garmin_training_load': 40.0},
        ], TODAY)

        dates = [s['date'] for s in h['snapshots']]
        assert dates == sorted(dates)
        assert len(dates) == len(set(dates))
        # Stale same-date snapshot replaced, not kept
        todays = [s for s in h['snapshots'] if s['date'] == today_iso]
        assert todays[0]['total']['ctl'] != 99


# ---------------------------------------------------------------------------
# get_fitness_trend — day-span math (was dividing by snapshot COUNT)
# ---------------------------------------------------------------------------

class TestFitnessTrendDayMath:
    def _seed(self, data_dir, snapshots):
        _write(data_dir, 'fitness_history.json', {
            'schema_version': 2,
            'daily_loads': {},
            'snapshots': snapshots,
            'sleep_history': [],
            'last_updated': TODAY.isoformat(),
        })

    def test_projection_uses_day_span_not_snapshot_count(self, tmp_path, monkeypatch):
        """2 snapshots 20 days apart, CTL 30 -> 50: daily change must be
        20/20 = 1.0/day => projected 30d = 80.0. The old bug divided by the
        snapshot count (2) => 10/day => projected 350."""
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        self._seed(tmp_path, [
            {'date': (TODAY - timedelta(days=20)).isoformat(),
             'total': {'ctl': 30, 'atl': 30, 'tsb': 0, 'acwr': 1.0}},
            {'date': TODAY.isoformat(),
             'total': {'ctl': 50, 'atl': 55, 'tsb': -5, 'acwr': 1.1}},
        ])

        trend = get_fitness_trend(days=28, today=TODAY)

        assert trend['trend'] == 'building'
        assert trend['ctl_change'] == 20
        assert trend['projected_ctl_30_days'] == 80.0

    def test_duplicate_same_date_snapshots_deduped(self, tmp_path, monkeypatch):
        """Legacy same-date duplicates: keep the LAST write per date."""
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        d10 = (TODAY - timedelta(days=10)).isoformat()
        self._seed(tmp_path, [
            {'date': d10, 'total': {'ctl': 40, 'atl': 40, 'tsb': 0, 'acwr': 1.0}},
            {'date': d10, 'total': {'ctl': 45, 'atl': 45, 'tsb': 0, 'acwr': 1.0}},
            {'date': TODAY.isoformat(),
             'total': {'ctl': 55, 'atl': 50, 'tsb': 5, 'acwr': 0.9}},
        ])

        trend = get_fitness_trend(days=28, today=TODAY)

        assert trend['data_points'] == 2          # duplicates collapsed
        assert trend['ctl_start'] == 45           # last write for d10 wins
        assert trend['ctl_change'] == 10
        assert trend['projected_ctl_30_days'] == 85.0  # 55 + (10/10)*30

    def test_all_same_date_collapses_to_insufficient(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        self._seed(tmp_path, [
            {'date': TODAY.isoformat(), 'total': {'ctl': 40, 'atl': 40, 'tsb': 0, 'acwr': 1.0}},
            {'date': TODAY.isoformat(), 'total': {'ctl': 42, 'atl': 42, 'tsb': 0, 'acwr': 1.0}},
        ])

        trend = get_fitness_trend(days=28, today=TODAY)

        assert trend['trend'] == 'unknown'
        assert trend['data_points'] == 1


# ---------------------------------------------------------------------------
# Shadow exposure — query_metrics(kind='fitness') tool + coaching snapshot
# ---------------------------------------------------------------------------

class TestShadowExposure:
    def _seed_history(self, data_dir, days=35, load=50.0):
        daily_loads = {
            (TODAY - timedelta(days=i)).isoformat(): _v2_day(load)
            for i in range(days)
        }
        _write(data_dir, 'fitness_history.json', {
            'schema_version': 2,
            'daily_loads': daily_loads,
            'snapshots': [],
            'sleep_history': [],
            'readiness_history': [],
            'last_updated': TODAY.isoformat(),
            'last_activity_ingest_date': TODAY.isoformat(),
        })

    def test_fitness_metrics_exposes_shadow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)
        self._seed_history(tmp_path)

        result = query_metrics(kind='fitness')

        assert 'error' not in result
        overall = result['metrics']['overall']
        # EWMA model untouched (golden: constant 50 => 1.23)
        assert overall['acwr'] == 1.23
        shadow = overall['acwr_shadow']
        assert shadow['value'] == 1.0  # constant load => rolling exactly 1.0
        assert shadow['zone'] == 'optimal'
        assert shadow['safe'] is True
        assert shadow['note'] == SHADOW_NOTE

    async def test_snapshot_exposes_acwr_shadow_next_to_acwr_status(
            self, tmp_path, monkeypatch, mock_ctx):
        for mod in (planner, rules, fitness, parsers_mod, workout_builder,
                    coaching_mod, planning_mod, strength_mod, fitness_tools_mod):
            monkeypatch.setattr(mod, 'DATA_DIR', tmp_path)

        self._seed_history(tmp_path)
        _write(tmp_path, 'athlete.json',
               {'personal': {'name': 'T', 'age': 30, 'weight_kg': 70}})
        _write(tmp_path, 'training_config.json', {'current_block': {'phase': 'base'}})
        _write(tmp_path, 'coaching_log.json',
               {'decisions': [], 'athlete_responses': [], 'pending_approvals': []})

        class FakeClient:
            def get_activities_by_date(self, start, end):
                return []

            def get_training_readiness(self, d):
                return []

            def get_hrv_data(self, d):
                return None

            def get_sleep_data(self, d):
                return {}

        fake_call = lambda fn: fn(FakeClient())
        monkeypatch.setattr(coaching_mod, 'garmin_api_call', fake_call)
        monkeypatch.setattr(fitness, 'garmin_api_call', fake_call)
        monkeypatch.setattr(planning_mod, 'garmin_api_call', fake_call)
        monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', lambda acts: acts)

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        assert 'error' not in result
        fm = result['fitness_metrics']
        # Both models side by side
        assert 'acwr_status' in fm
        assert 'acwr_shadow' in fm
        shadow = fm['acwr_shadow']
        assert shadow['note'] == SHADOW_NOTE
        assert shadow['value'] == fm['overall']['acwr_rolling']
        assert shadow['zone'] == fm['overall']['acwr_rolling_status']['zone']
        # Decisions still keyed off the EWMA status until cutover
        assert fm['acwr_status']['value'] == fm['overall']['acwr']


# ---------------------------------------------------------------------------
# Shadow report script (read-only)
# ---------------------------------------------------------------------------

class TestShadowReportScript:
    def _history(self, days=35, load=50.0, end=AS_OF):
        daily_loads = {
            (end - timedelta(days=i)).isoformat(): _v2_day(load)
            for i in range(days)
        }
        return {
            'schema_version': 2,
            'daily_loads': daily_loads,
            'snapshots': [],
            'sleep_history': [],
            'last_updated': end.isoformat(),
        }

    def test_report_structure_and_goldens(self):
        report = build_shadow_report(self._history(), days=14, as_of=AS_OF)

        assert report['status'] == 'ok'
        assert report['days_compared'] == 14
        last = report['rows'][-1]
        assert last['date'] == AS_OF.isoformat()
        assert last['rolling_acwr'] == 1.0   # constant load
        assert last['ewma_acwr'] == 1.23     # pinned EWMA golden
        stats = report['stats']
        for key in ('mean_abs_diff', 'max_divergence', 'days_in_different_zones',
                    'zone_mismatch_pct', 'divergence_level',
                    'ewma_unsafe_rolling_safe_days', 'rolling_unsafe_ewma_safe_days'):
            assert key in stats
        assert stats['mean_abs_diff'] > 0  # the two models DO diverge

    def test_report_window_clipped_to_first_load(self):
        """Days before any recorded load are excluded, not reported as zeros."""
        report = build_shadow_report(self._history(days=10), days=90, as_of=AS_OF)
        assert report['days_compared'] == 10
        assert report['window_start'] == (AS_OF - timedelta(days=9)).isoformat()

    def test_no_data_history(self):
        assert build_shadow_report({'daily_loads': {}})['status'] == 'no_data'

    def test_v1_flat_loads_supported(self):
        history = {
            'daily_loads': _constant_loads(50.0, 35, AS_OF),  # v1 flat floats
            'snapshots': [],
        }
        report = build_shadow_report(history, days=7, as_of=AS_OF)
        assert report['status'] == 'ok'
        assert report['rows'][-1]['rolling_acwr'] == 1.0

    def test_format_report_renders(self):
        report = build_shadow_report(self._history(), days=14, as_of=AS_OF)
        text = format_report(report)
        assert 'DIVERGENCE STATS' in text
        assert 'RECOMMENDATION' in text
        assert 'READ-ONLY' in text
        assert AS_OF.isoformat() in text

    def test_format_report_no_data(self):
        text = format_report({'status': 'no_data', 'note': 'empty'})
        assert 'No report' in text

    def test_script_writes_nothing(self, tmp_path):
        """End to end on a tmp copy: file bytes untouched, no new files."""
        history_path = tmp_path / 'fitness_history.json'
        history_path.write_text(json.dumps(self._history()), encoding='utf-8')
        before_bytes = history_path.read_bytes()
        before_listing = sorted(p.name for p in tmp_path.iterdir())

        history = load_history(history_path)
        report = build_shadow_report(history, days=30, as_of=AS_OF)
        format_report(report)

        assert history_path.read_bytes() == before_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == before_listing
