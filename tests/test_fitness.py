"""
Tests for fitness.py — Garmin training load, migration, sleep, patterns.

Tests cover:
- Garmin activityTrainingLoad extraction
- Sport group mapping
- Schema v1 → v2 migration
- Sport-specific CTL/ATL/TSB/ACWR
- Sleep persistence and trends
- Activity pattern analysis
- Helper functions (_extract_total_loads, _extract_sport_loads)
"""
import json
import pytest
from datetime import date, timedelta
from unittest.mock import patch

from coach.fitness import (
    calculate_training_load,
    calculate_daily_load,
    calculate_fitness_metrics,
    calculate_ewma,
    calculate_intensity_distribution,
    migrate_fitness_history,
    load_fitness_history,
    save_fitness_history,
    update_fitness_history,
    _extract_total_loads,
    _extract_sport_loads,
    calculate_sport_fitness_metrics,
    get_sleep_trend,
    persist_sleep_data,
    analyze_activity_patterns,
    get_day_context,
    persist_readiness_data,
    calculate_readiness_baselines,
    derive_adaptation_thresholds,
    detect_bedtime_drift,
    backfill_fitness_history,
)
from conftest import FakeGarminClient, patch_garmin_everywhere


# ── Garmin Training Load ─────────────────────────────────────────

class TestGarminTrainingLoad:
    """Test Garmin activityTrainingLoad extraction."""

    def test_garmin_load_returned(self):
        """Activity with garmin_training_load → returns that value."""
        activity = {'garmin_training_load': 127.5}
        assert calculate_training_load(activity) == 127.5

    def test_garmin_load_zero_returns_zero(self):
        """garmin_training_load: 0 → returns 0.0."""
        activity = {'garmin_training_load': 0}
        assert calculate_training_load(activity) == 0.0

    def test_garmin_load_none_returns_zero(self):
        """garmin_training_load: None → returns 0.0."""
        activity = {'garmin_training_load': None}
        assert calculate_training_load(activity) == 0.0

    def test_garmin_load_missing_returns_zero(self):
        """No garmin_training_load field → returns 0.0."""
        activity = {'type': 'running', 'duration_mins': 45}
        assert calculate_training_load(activity) == 0.0

    def test_daily_load_sums_garmin_loads(self):
        """Daily load sums garmin_training_load across activities."""
        activities = [
            {'garmin_training_load': 85.3},
            {'garmin_training_load': 42.1},
        ]
        assert calculate_daily_load(activities) == 127.4


# ── Schema Migration ─────────────────────────────────────────────

class TestMigrateFitnessHistory:
    def test_migrates_v1_daily_loads(self):
        v1 = {
            'daily_loads': {'2026-02-01': 17.1, '2026-02-02': 5.5},
            'snapshots': [],
            'last_updated': '2026-02-02',
        }
        v2 = migrate_fitness_history(v1)

        assert v2['schema_version'] == 2
        assert v2['daily_loads']['2026-02-01']['total'] == 17.1
        assert v2['daily_loads']['2026-02-01']['by_sport'] == {}
        assert v2['daily_loads']['2026-02-01']['activities'] == []

    def test_migrates_v1_snapshots(self):
        v1 = {
            'daily_loads': {},
            'snapshots': [
                {'date': '2026-02-01', 'ctl': 21, 'atl': 15, 'tsb': 6, 'acwr': 0.7},
            ],
            'last_updated': '2026-02-01',
        }
        v2 = migrate_fitness_history(v1)

        snap = v2['snapshots'][0]
        assert snap['total']['ctl'] == 21
        assert snap['total']['atl'] == 15
        assert snap['total']['tsb'] == 6
        assert snap['total']['acwr'] == 0.7

    def test_already_v2_no_change(self):
        v2 = {
            'schema_version': 2,
            'daily_loads': {
                '2026-02-01': {'total': 10, 'by_sport': {'cycling': 10}, 'activities': []},
            },
            'snapshots': [{'date': '2026-02-01', 'total': {'ctl': 5, 'atl': 3, 'tsb': 2, 'acwr': 0.6}}],
            'sleep_history': [],
            'last_updated': '2026-02-01',
        }
        result = migrate_fitness_history(v2)
        assert result == v2

    def test_adds_sleep_history_if_missing(self):
        v1 = {
            'daily_loads': {},
            'snapshots': [],
            'last_updated': None,
        }
        v2 = migrate_fitness_history(v1)
        assert 'sleep_history' in v2
        assert v2['sleep_history'] == []

    def test_preserves_existing_v2_daily_loads(self):
        """If some entries are already v2 dicts, leave them alone."""
        mixed = {
            'daily_loads': {
                '2026-02-01': 17.1,  # v1
                '2026-02-02': {'total': 5.5, 'by_sport': {'cycling': 5.5}, 'activities': []},  # v2
            },
            'snapshots': [],
            'last_updated': '2026-02-02',
        }
        result = migrate_fitness_history(mixed)
        assert result['daily_loads']['2026-02-01']['total'] == 17.1
        assert result['daily_loads']['2026-02-02']['total'] == 5.5


# ── Helper Functions ─────────────────────────────────────────────

class TestExtractTotalLoads:
    def test_extracts_from_v2(self):
        daily_loads = {
            '2026-02-01': {'total': 17.1, 'by_sport': {'cycling': 12}, 'activities': []},
            '2026-02-02': {'total': 5.5, 'by_sport': {'strength': 5.5}, 'activities': []},
        }
        flat = _extract_total_loads(daily_loads)
        assert flat == {'2026-02-01': 17.1, '2026-02-02': 5.5}

    def test_handles_v1_fallback(self):
        daily_loads = {'2026-02-01': 10.0}
        flat = _extract_total_loads(daily_loads)
        assert flat == {'2026-02-01': 10.0}


class TestExtractSportLoads:
    def test_extracts_cycling(self):
        daily_loads = {
            '2026-02-01': {'total': 17.1, 'by_sport': {'cycling': 12.3, 'strength': 4.8}, 'activities': []},
            '2026-02-02': {'total': 5.5, 'by_sport': {'running': 5.5}, 'activities': []},
        }
        cycling = _extract_sport_loads(daily_loads, 'cycling')
        assert cycling == {'2026-02-01': 12.3, '2026-02-02': 0.0}

    def test_v1_returns_zeros(self):
        daily_loads = {'2026-02-01': 10.0}
        running = _extract_sport_loads(daily_loads, 'running')
        assert running == {'2026-02-01': 0.0}


# ── Sport-Specific Fitness Metrics ───────────────────────────────

class TestSportSpecificFitnessMetrics:
    def _make_daily_loads(self, days=50, cycling_daily=8.0, running_daily=3.0):
        """Build v2 daily_loads for testing."""
        today = date.today()
        loads = {}
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            loads[d] = {
                'total': cycling_daily + running_daily,
                'by_sport': {'cycling': cycling_daily, 'running': running_daily},
                'activities': [],
            }
        return loads

    def test_cycling_ctl_independent_of_running(self):
        loads = self._make_daily_loads(50, cycling_daily=10.0, running_daily=0.0)
        cycling = calculate_sport_fitness_metrics(loads, 'cycling', date.today())
        running = calculate_sport_fitness_metrics(loads, 'running', date.today())

        assert cycling['ctl'] > 0
        assert running['ctl'] == 0.0

    def test_running_ctl_independent_of_cycling(self):
        loads = self._make_daily_loads(50, cycling_daily=0.0, running_daily=5.0)
        cycling = calculate_sport_fitness_metrics(loads, 'cycling', date.today())
        running = calculate_sport_fitness_metrics(loads, 'running', date.today())

        assert cycling['ctl'] == 0.0
        assert running['ctl'] > 0

    def test_sport_acwr_calculated(self):
        loads = self._make_daily_loads(50, cycling_daily=10.0, running_daily=5.0)
        cycling = calculate_sport_fitness_metrics(loads, 'cycling', date.today())
        assert 'acwr' in cycling
        assert cycling['acwr'] > 0

    def test_zero_chronic_load_returns_high_acwr(self):
        """Zero chronic + some acute = dangerous ACWR."""
        today = date.today()
        loads = {}
        # No activity for 40+ days, then sudden spike
        for i in range(50):
            d = (today - timedelta(days=i)).isoformat()
            if i < 3:  # Last 3 days: spike
                loads[d] = {'total': 20, 'by_sport': {'running': 20}, 'activities': []}
            else:
                loads[d] = {'total': 0, 'by_sport': {}, 'activities': []}

        running = calculate_sport_fitness_metrics(loads, 'running', date.today())
        # ACWR should be high (acute load present but low chronic)
        assert running['acwr'] > 1.3

    def test_empty_loads_returns_zero_ctl(self):
        loads = {}
        m = calculate_sport_fitness_metrics(loads, 'cycling', date.today())
        assert m['ctl'] == 0.0


# ── Update Fitness History (v2 format) ───────────────────────────

class TestUpdateFitnessHistoryV2:
    def test_stores_per_sport_breakdown(self, tmp_path, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        activities = [
            {'date': '2026-02-01', 'type': 'cycling', 'duration_mins': 60, 'garmin_training_load': 95.0},
            {'date': '2026-02-01', 'type': 'strength_training', 'duration_mins': 30, 'garmin_training_load': 35.0},
        ]
        history = update_fitness_history(activities, date(2026, 2, 2))

        day = history['daily_loads']['2026-02-01']
        assert 'total' in day
        assert 'by_sport' in day
        assert 'cycling' in day['by_sport']
        assert 'strength' in day['by_sport']
        assert day['total'] == day['by_sport']['cycling'] + day['by_sport']['strength']

    def test_stores_activity_details(self, tmp_path, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        activities = [
            {'date': '2026-02-01', 'activity_id': 123, 'type': 'running', 'duration_mins': 45, 'garmin_training_load': 72.0},
        ]
        history = update_fitness_history(activities, date(2026, 2, 2))

        day = history['daily_loads']['2026-02-01']
        assert len(day['activities']) == 1
        assert day['activities'][0]['id'] == 123
        assert day['activities'][0]['sport'] == 'running'

    def test_generates_per_sport_snapshots(self, tmp_path, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        # Build enough history for meaningful CTL
        activities = []
        for i in range(50):
            d = (date.today() - timedelta(days=i)).isoformat()
            activities.append({'date': d, 'type': 'cycling', 'duration_mins': 60, 'garmin_training_load': 85.0})
            activities.append({'date': d, 'type': 'running', 'duration_mins': 30, 'garmin_training_load': 55.0})

        history = update_fitness_history(activities, date.today())
        snap = history['snapshots'][-1]

        assert 'total' in snap
        assert 'cycling' in snap
        assert 'running' in snap
        assert snap['cycling']['ctl'] > 0
        assert snap['running']['ctl'] > 0


# ── Sleep Persistence ────────────────────────────────────────────

class TestPersistSleepData:
    def test_adds_new_records(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        history = {'sleep_history': []}
        records = [
            {'date': two_days_ago, 'duration_hrs': 7.2, 'score': 82, 'deep_pct': 22, 'rem_pct': 25, 'avg_hr': 55},
            {'date': yesterday, 'duration_hrs': 6.8, 'score': 75, 'deep_pct': 18, 'rem_pct': 22, 'avg_hr': 57},
        ]
        result = persist_sleep_data(records, history, today=date.today())
        assert len(result['sleep_history']) == 2
        assert result['sleep_history'][0]['date'] == two_days_ago

    def test_deduplicates_by_date(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        history = {
            'sleep_history': [
                {'date': two_days_ago, 'duration_hrs': 7.2, 'score': 82},
            ],
        }
        records = [
            {'date': two_days_ago, 'duration_hrs': 7.5, 'score': 85},  # Duplicate date
            {'date': yesterday, 'duration_hrs': 6.8, 'score': 75},
        ]
        result = persist_sleep_data(records, history, today=date.today())
        assert len(result['sleep_history']) == 2
        # Original record preserved (not overwritten)
        dup = next(r for r in result['sleep_history'] if r['date'] == two_days_ago)
        assert dup['score'] == 82

    def test_default_retention_keeps_full_history(self):
        # Retention defaults to None (keep everything) — old nights are the
        # training diary, never silently pruned.
        history = {
            'sleep_history': [
                {'date': '2025-12-01', 'duration_hrs': 7.0, 'score': 70},
            ],
        }
        records = [
            {'date': date.today().isoformat(), 'duration_hrs': 7.5, 'score': 85},
        ]
        result = persist_sleep_data(records, history, today=date.today())
        dates = [r['date'] for r in result['sleep_history']]
        assert '2025-12-01' in dates and len(dates) == 2

    def test_configured_retention_window_applies(self, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'SLEEP_HISTORY_RETENTION_DAYS', 30)
        old = (date.today() - timedelta(days=40)).isoformat()
        history = {'sleep_history': [{'date': old, 'duration_hrs': 7.0}]}
        records = [{'date': date.today().isoformat(), 'duration_hrs': 7.5}]
        result = persist_sleep_data(records, history, today=date.today())
        dates = [r['date'] for r in result['sleep_history']]
        assert old not in dates and len(dates) == 1

    def test_sorts_by_date(self):
        history = {'sleep_history': []}
        records = [
            {'date': '2026-02-06', 'duration_hrs': 7.0},
            {'date': '2026-02-04', 'duration_hrs': 7.5},
            {'date': '2026-02-05', 'duration_hrs': 6.8},
        ]
        result = persist_sleep_data(records, history, today=date(2026, 2, 7))
        dates = [r['date'] for r in result['sleep_history']]
        assert dates == sorted(dates)
        assert len(dates) == 3


# ── Sleep Trend ──────────────────────────────────────────────────

class TestGetSleepTrend:
    def _make_sleep_history(self, days=14, base_hrs=7.0, trend=0.0):
        """Generate sleep records with optional trend."""
        records = []
        for i in range(days):
            d = (date.today() - timedelta(days=i)).isoformat()
            hrs = base_hrs + trend * (days - i) / days
            records.append({
                'date': d,
                'duration_hrs': round(hrs, 1),
                'score': 75,
            })
        return records

    def test_returns_avg_duration(self):
        history = {'sleep_history': self._make_sleep_history(14, base_hrs=7.5)}
        result = get_sleep_trend(history, days=30, today=date.today())
        assert result['avg_duration'] == 7.5

    def test_detects_improving_trend(self):
        # get_sleep_trend filters by cutoff then compares first half (older) vs second half (newer)
        # Records are sorted by date ascending after filtering
        # So: first half = older dates, second half = newer dates
        # Improving = second half avg > first half avg
        records = []
        for i in range(20):
            d = (date.today() - timedelta(days=i)).isoformat()
            # i=0 is today (most recent), i=19 is oldest
            # We want recent (low i) to have higher duration
            hrs = 7.5 if i < 10 else 6.5
            records.append({'date': d, 'duration_hrs': hrs, 'score': 75})
        history = {'sleep_history': records}
        result = get_sleep_trend(history, days=30, today=date.today())
        # After sort ascending: oldest first → first half is 6.5hrs, second half is 7.5hrs
        assert result['direction'] == 'improving'

    def test_detects_declining_trend(self):
        records = []
        for i in range(20):
            d = (date.today() - timedelta(days=i)).isoformat()
            # Recent (low i) = worse, older (high i) = better
            hrs = 6.0 if i < 10 else 7.5
            records.append({'date': d, 'duration_hrs': hrs, 'score': 75})
        history = {'sleep_history': records}
        result = get_sleep_trend(history, days=30, today=date.today())
        # After sort ascending: first half (older) = 7.5hrs, second half (newer) = 6.0hrs
        assert result['direction'] == 'declining'

    def test_empty_history(self):
        result = get_sleep_trend({'sleep_history': []}, today=date.today())
        assert result['status'] == 'no_data'

    def test_counts_deficit_weeks(self):
        # All records at 6.5hrs → every week is a deficit
        records = self._make_sleep_history(14, base_hrs=6.5)
        history = {'sleep_history': records}
        result = get_sleep_trend(history, days=30, today=date.today())
        assert result['weeks_in_deficit'] > 0


# ── Activity Pattern Analysis ────────────────────────────────────

class TestAnalyzeActivityPatterns:
    def _make_loads(self, today=None):
        if today is None:
            today = date.today()
        loads = {}
        # Cycling 3x/week for 4 weeks, running stopped 20 days ago
        for week in range(4):
            for day_offset in [0, 2, 4]:  # Mon, Wed, Fri
                d = (today - timedelta(days=week * 7 + day_offset)).isoformat()
                loads[d] = {
                    'total': 10,
                    'by_sport': {'cycling': 10},
                    'activities': [
                        {'type': 'cycling', 'sport': 'cycling', 'duration_mins': 60, 'load': 10},
                    ],
                }
        # Add one running session 20 days ago
        run_date = (today - timedelta(days=20)).isoformat()
        if run_date in loads:
            loads[run_date]['activities'].append(
                {'type': 'running', 'sport': 'running', 'duration_mins': 30, 'load': 5}
            )
            loads[run_date]['by_sport']['running'] = 5
            loads[run_date]['total'] += 5
        else:
            loads[run_date] = {
                'total': 5,
                'by_sport': {'running': 5},
                'activities': [
                    {'type': 'running', 'sport': 'running', 'duration_mins': 30, 'load': 5},
                ],
            }
        return loads

    def test_tracks_last_activity_by_sport(self):
        today = date.today()
        loads = self._make_loads(today)
        result = analyze_activity_patterns(loads, today)

        assert 'cycling' in result['last_activity_by_sport']
        assert result['last_activity_by_sport']['cycling']['days_ago'] <= 7

    def test_detects_long_absence(self):
        today = date.today()
        loads = self._make_loads(today)
        result = analyze_activity_patterns(loads, today)

        # Running had last session 20 days ago
        assert 'running' in result['last_activity_by_sport']
        assert result['last_activity_by_sport']['running']['days_ago'] == 20
        # Should generate alert
        assert any('running' in a.lower() for a in result['alerts'])

    def test_sessions_per_week_structure(self):
        today = date.today()
        loads = self._make_loads(today)
        result = analyze_activity_patterns(loads, today)

        assert 'cycling' in result['sessions_per_week_4wk']
        assert len(result['sessions_per_week_4wk']['cycling']) == 4

    def test_empty_loads(self):
        result = analyze_activity_patterns({}, date.today())
        assert result['last_activity_by_sport'] == {}
        assert result['alerts'] != []  # Should have "no activity" alerts

    def test_detects_strength_not_present(self):
        """Strength missing generates no alert (it's optional in pattern)."""
        today = date.today()
        loads = self._make_loads(today)
        result = analyze_activity_patterns(loads, today)
        # Strength not present → no alert (strength is not flagged as missing like running)
        # But cycling and running patterns should still be detected
        assert 'sessions_per_week_4wk' in result


# ── Load Fitness History (auto-migration) ────────────────────────

class TestLoadFitnessHistory:
    def test_auto_migrates_v1(self, tmp_path, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        v1 = {
            'daily_loads': {'2026-02-01': 17.1},
            'snapshots': [{'date': '2026-02-01', 'ctl': 21, 'atl': 15, 'tsb': 6, 'acwr': 0.7}],
            'last_updated': '2026-02-01',
        }
        with open(tmp_path / 'fitness_history.json', 'w') as f:
            json.dump(v1, f)

        result = load_fitness_history()
        assert result['schema_version'] == 2
        assert result['daily_loads']['2026-02-01']['total'] == 17.1

    def test_returns_fresh_v2_when_no_file(self, tmp_path, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        result = load_fitness_history()
        assert result['schema_version'] == 2
        assert result['daily_loads'] == {}
        assert result['sleep_history'] == []


# ── Intensity Distribution with Zone Data ─────────────────────

HR_ZONES = {
    'z1_recovery': [0, 120],
    'z2_aerobic': [120, 140],
    'z3_tempo': [140, 155],
    'z4_threshold': [155, 170],
    'z5_max': [170, 200],
}


class TestIntensityDistributionWithZoneData:
    """Tests for calculate_intensity_distribution() with hr_time_in_zones."""

    def test_uses_zone_data_when_present(self):
        """Activity with hr_time_in_zones should use actual zone data, not avg HR."""
        activities = [{
            'duration_mins': 120,
            'avg_hr': 135,  # Would classify as Z2 (all low) by avg HR
            'type': 'cycling',
            'hr_time_in_zones': {
                'z1': 30, 'z2': 40, 'z3': 20, 'z4': 20, 'z5': 10,
            },
        }]
        result = calculate_intensity_distribution(activities, HR_ZONES)
        zones = result['time_in_zones_mins']
        assert zones['low'] == 70   # z1 + z2
        assert zones['moderate'] == 20   # z3
        assert zones['high'] == 30  # z4 + z5
        assert result['data_source']['zone_data'] == 1
        assert result['data_source']['avg_hr'] == 0

    def test_fallback_to_avg_hr_without_zone_data(self):
        """Without hr_time_in_zones, falls back to avg HR classification."""
        activities = [{
            'duration_mins': 60,
            'avg_hr': 135,
            'type': 'cycling',
        }]
        result = calculate_intensity_distribution(activities, HR_ZONES)
        assert result['time_in_zones_mins']['low'] == 60
        assert result['data_source']['avg_hr'] == 1
        assert result['data_source']['zone_data'] == 0

    def test_mixed_activities(self):
        """Some activities with zone data, some without."""
        activities = [
            {
                'duration_mins': 60,
                'avg_hr': 135,
                'type': 'cycling',
                'hr_time_in_zones': {'z1': 10, 'z2': 20, 'z3': 15, 'z4': 10, 'z5': 5},
            },
            {
                'duration_mins': 30,
                'avg_hr': 160,
                'type': 'running',
                # No hr_time_in_zones — falls back to avg HR
            },
        ]
        result = calculate_intensity_distribution(activities, HR_ZONES)
        assert result['data_source']['zone_data'] == 1
        assert result['data_source']['avg_hr'] == 1
        # Zone data activity: low=30, mod=15, high=15
        # Avg HR activity: 160 > z3_upper (155) → high=30
        assert result['time_in_zones_mins']['low'] == 30
        assert result['time_in_zones_mins']['moderate'] == 15
        assert result['time_in_zones_mins']['high'] == 45

    def test_below_z1_time_counted_as_low(self):
        """Time not accounted for in zones (below Z1 floor) = low intensity."""
        activities = [{
            'duration_mins': 100,
            'avg_hr': 120,
            'type': 'cycling',
            'hr_time_in_zones': {'z1': 30, 'z2': 20, 'z3': 10},
            # Only 60 mins accounted, 40 mins unaccounted → added to low
        }]
        result = calculate_intensity_distribution(activities, HR_ZONES)
        assert result['time_in_zones_mins']['low'] == 90  # 30 + 20 + 40 unaccounted
        assert result['time_in_zones_mins']['moderate'] == 10

    def test_data_source_counts(self):
        """data_source should count activities per classification method."""
        activities = [
            {'duration_mins': 60, 'avg_hr': 130, 'type': 'cycling',
             'hr_time_in_zones': {'z1': 30, 'z2': 30}},
            {'duration_mins': 30, 'avg_hr': 150, 'type': 'running'},
            {'duration_mins': 20, 'avg_hr': 0, 'type': 'yoga'},
        ]
        result = calculate_intensity_distribution(activities, HR_ZONES)
        assert result['data_source'] == {
            'zone_data': 1,
            'avg_hr': 1,
            'type_estimate': 1,
        }

    def test_empty_activities(self):
        """Empty activities list returns data_source with all zeros."""
        result = calculate_intensity_distribution([], HR_ZONES)
        assert result.get('data_source') is None or result.get('zone_distribution') == {}


# ── Day Context ───────────────────────────────────────────────

class TestGetDayContext:
    """Tests for get_day_context() — surrounding context for anomaly enrichment."""

    def test_returns_sleep_data(self):
        sleep_history = [
            {'date': '2026-03-14', 'score': 82, 'duration_hrs': 7.2},
        ]
        ctx = get_day_context('2026-03-14', {}, sleep_history)
        assert ctx['sleep_score'] == 82
        assert ctx['sleep_hours'] == 7.2

    def test_returns_prior_day_load(self):
        daily_loads = {
            '2026-03-13': {'total': 95.3, 'by_sport': {}, 'activities': []},
        }
        ctx = get_day_context('2026-03-14', daily_loads, [])
        assert ctx['prior_day_load'] == 95.3

    def test_prior_day_hard_flag(self):
        daily_loads = {
            '2026-03-13': {
                'total': 85.0,
                'by_sport': {'running': 85.0},
                'activities': [{'sport': 'running', 'load': 85.0}],
            },
        }
        ctx = get_day_context('2026-03-14', daily_loads, [])
        assert ctx.get('prior_day_hard') is True

    def test_no_data_returns_empty(self):
        ctx = get_day_context('2026-03-14', {}, [])
        assert ctx == {}

    def test_missing_sleep_fields_excluded(self):
        sleep_history = [
            {'date': '2026-03-14', 'score': None, 'duration_hrs': 6.5},
        ]
        ctx = get_day_context('2026-03-14', {}, sleep_history)
        assert 'sleep_score' not in ctx
        assert ctx['sleep_hours'] == 6.5

    def test_v1_daily_loads_numeric(self):
        """Legacy v1 format where daily_loads values are plain numbers."""
        daily_loads = {'2026-03-13': 42.5}
        ctx = get_day_context('2026-03-14', daily_loads, [])
        assert ctx['prior_day_load'] == 42.5

    def test_invalid_date_returns_empty(self):
        ctx = get_day_context('not-a-date', {}, [])
        assert ctx == {}


# ── Persist Readiness Data ────────────────────────────────────

class TestPersistReadinessData:
    def test_adds_new_record(self):
        rec_date = (date.today() - timedelta(days=1)).isoformat()
        history = {'readiness_history': []}
        rec = {'date': rec_date, 'score': 72, 'level': 'MODERATE', 'hrv_status': 'BALANCED', 'body_battery': 55}
        result = persist_readiness_data(rec, history, today=date.today())
        assert len(result['readiness_history']) == 1
        assert result['readiness_history'][0]['score'] == 72

    def test_deduplicates_by_date(self):
        rec_date = (date.today() - timedelta(days=1)).isoformat()
        history = {
            'readiness_history': [
                {'date': rec_date, 'score': 72},
            ],
        }
        rec = {'date': rec_date, 'score': 75}
        result = persist_readiness_data(rec, history, today=date.today())
        assert len(result['readiness_history']) == 1
        assert result['readiness_history'][0]['score'] == 72  # Original preserved

    def test_default_retention_keeps_old_records(self):
        old_date = (date.today() - timedelta(days=65)).isoformat()
        recent_date = date.today().isoformat()
        history = {
            'readiness_history': [
                {'date': old_date, 'score': 60},
            ],
        }
        rec = {'date': recent_date, 'score': 72}
        result = persist_readiness_data(rec, history, today=date.today())
        assert [r['date'] for r in result['readiness_history']] == [old_date, recent_date]

    def test_missing_date_ignored(self):
        history = {'readiness_history': []}
        rec = {'score': 72}
        result = persist_readiness_data(rec, history, today=date.today())
        assert len(result['readiness_history']) == 0

    def test_creates_readiness_history_key(self):
        history = {}
        rec = {'date': '2026-03-14', 'score': 72}
        result = persist_readiness_data(rec, history, today=date.today())
        assert 'readiness_history' in result


# ── Calculate Readiness Baselines ─────────────────────────────

class TestCalculateReadinessBaselines:
    def test_sufficient_data(self):
        today = date.today()
        sleep = [
            {'date': (today - timedelta(days=i)).isoformat(), 'duration_hrs': 7.0 + i * 0.1, 'score': 75 + i}
            for i in range(10)
        ]
        readiness = [
            {'date': (today - timedelta(days=i)).isoformat(), 'score': 70 + i}
            for i in range(10)
        ]
        result = calculate_readiness_baselines(sleep, readiness, today)
        assert result['status'] == 'sufficient'
        assert 'sleep_duration_14d_avg' in result
        assert 'sleep_score_14d_avg' in result
        assert 'readiness_14d_avg' in result

    def test_insufficient_data(self):
        result = calculate_readiness_baselines([], [], date.today())
        assert result['status'] == 'insufficient_data'

    def test_partial_sleep_only(self):
        today = date.today()
        sleep = [
            {'date': (today - timedelta(days=i)).isoformat(), 'duration_hrs': 7.0, 'score': 80}
            for i in range(5)
        ]
        result = calculate_readiness_baselines(sleep, [], today)
        assert 'sleep_duration_14d_avg' in result
        assert 'readiness_14d_avg' not in result

    def test_30d_averages_differ_from_14d(self):
        today = date.today()
        # 14d: high scores, older 16d: low scores
        sleep = []
        for i in range(30):
            score = 90 if i < 14 else 60
            sleep.append({
                'date': (today - timedelta(days=i)).isoformat(),
                'duration_hrs': 7.5, 'score': score,
            })
        result = calculate_readiness_baselines(sleep, [], today)
        assert result['sleep_score_14d_avg'] > result['sleep_score_30d_avg']


# ── Derive Adaptation Thresholds ──────────────────────────────

class TestDeriveAdaptationThresholds:
    def test_empty_responses(self):
        result = derive_adaptation_thresholds([])
        assert result['status'] == 'accumulating'
        assert result['data_points'] == 0

    def test_insufficient_numeric_data(self):
        responses = [
            {'stimulus': 'ride', 'response': 'good', 'load_change_pct': 10}
            for _ in range(5)
        ]
        result = derive_adaptation_thresholds(responses)
        assert result['status'] == 'accumulating'
        assert result['data_points'] == 5

    def test_responses_without_numeric_fields(self):
        """Responses without load_change_pct are ignored."""
        responses = [
            {'stimulus': 'ride', 'response': 'good'}
            for _ in range(20)
        ]
        result = derive_adaptation_thresholds(responses)
        assert result['status'] == 'accumulating'
        assert result['data_points'] == 0

    def test_sufficient_data_quantified(self):
        responses = [
            {'load_change_pct': 12, 'compliance_result': True, 'injury_flag': False}
            for _ in range(10)
        ]
        result = derive_adaptation_thresholds(responses)
        assert result['status'] == 'quantified'
        assert 'volume_tolerance' in result
        assert result['confidence'] == 'moderate'

    def test_high_confidence_with_many_points(self):
        responses = [
            {'load_change_pct': 15, 'compliance_result': True}
            for _ in range(25)
        ]
        result = derive_adaptation_thresholds(responses)
        assert result['confidence'] == 'high'

    def test_safe_max_from_successful_bucket(self):
        """Conservative bucket with good success should yield safe_max."""
        responses = [
            {'load_change_pct': 5, 'compliance_result': True, 'injury_flag': False}
            for _ in range(8)
        ]
        result = derive_adaptation_thresholds(responses)
        assert result['status'] == 'quantified'
        assert 'safe_load_increase_max_pct' in result

    def test_injury_reduces_success_rate(self):
        """Bucket with injuries should have lower success rate."""
        responses = [
            {'load_change_pct': 25, 'compliance_result': True, 'injury_flag': True}
            for _ in range(8)
        ]
        result = derive_adaptation_thresholds(responses)
        aggressive = result['volume_tolerance'].get('aggressive', {})
        assert aggressive.get('success_rate', 1.0) == 0.0

    def test_mixed_buckets(self):
        responses = []
        # 4 conservative, 4 standard
        for _ in range(4):
            responses.append({'load_change_pct': 5, 'compliance_result': True})
        for _ in range(4):
            responses.append({'load_change_pct': 15, 'compliance_result': True})
        result = derive_adaptation_thresholds(responses)
        assert result['status'] == 'quantified'
        assert 'conservative' in result['volume_tolerance']
        assert 'standard' in result['volume_tolerance']


# ── Bedtime Drift Detection ──────────────────────────────────────

class TestDetectBedtimeDrift:
    def _nights(self, bedtimes_with_dates):
        """Helper: build sleep_history records with given bedtimes."""
        return [
            {'date': d, 'bedtime': bt, 'duration_hrs': 7.5, 'score': 80}
            for d, bt in bedtimes_with_dates
        ]

    def test_insufficient_data_returns_unknown(self):
        nights = self._nights([
            ('2026-04-10', '2026-04-10T22:00:00'),
            ('2026-04-11', '2026-04-11T22:15:00'),
        ])
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'insufficient_data'
        assert result['direction'] == 'unknown'

    def test_empty_list_returns_unknown(self):
        assert detect_bedtime_drift([])['status'] == 'insufficient_data'

    def test_stable_bedtime(self):
        nights = self._nights([
            (f'2026-04-{d:02d}', f'2026-04-{d:02d}T22:00:00')
            for d in range(1, 15)
        ])
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'ok'
        assert result['direction'] == 'stable'
        assert abs(result['drift_mins_per_wk']) < 5

    def test_drifting_later(self):
        # Bedtime goes from 22:00 to 23:30 over 14 nights → strong drift later
        nights = []
        for i, d in enumerate(range(1, 15)):
            hr = 22 + (i * 6) // 60  # gradually later
            mn = (i * 6) % 60
            nights.append({
                'date': f'2026-04-{d:02d}',
                'bedtime': f'2026-04-{d:02d}T{hr:02d}:{mn:02d}:00',
                'duration_hrs': 7.0, 'score': 75,
            })
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'ok'
        assert result['direction'] == 'later'
        assert result['drift_mins_per_wk'] > 15

    def test_drifting_earlier(self):
        # Bedtime goes from 23:30 to 22:00 over 14 nights → drifting earlier
        nights = []
        for i, d in enumerate(range(1, 15)):
            total_mins = (23 * 60 + 30) - i * 6  # start at 23:30, 6 min earlier/night
            hr = total_mins // 60
            mn = total_mins % 60
            nights.append({
                'date': f'2026-04-{d:02d}',
                'bedtime': f'2026-04-{d:02d}T{hr:02d}:{mn:02d}:00',
                'duration_hrs': 8.0, 'score': 85,
            })
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'ok'
        assert result['direction'] == 'earlier'
        assert result['drift_mins_per_wk'] < -15

    def test_handles_midnight_crossing(self):
        # Bedtimes: 23:30 → 00:30 should register drift later (not -23h earlier)
        nights = self._nights([
            ('2026-04-01', '2026-04-01T23:30:00'),
            ('2026-04-02', '2026-04-02T23:35:00'),
            ('2026-04-03', '2026-04-03T23:40:00'),
            ('2026-04-04', '2026-04-04T23:45:00'),
            ('2026-04-05', '2026-04-05T23:55:00'),
            ('2026-04-06', '2026-04-07T00:05:00'),  # past midnight
            ('2026-04-07', '2026-04-08T00:15:00'),
            ('2026-04-08', '2026-04-09T00:25:00'),
        ])
        result = detect_bedtime_drift(nights)
        assert result['status'] == 'ok'
        # Drifting later, not earlier by ~23 hours
        assert result['drift_mins_per_wk'] > 0
        assert result['drift_mins_per_wk'] < 200  # sanity

    def test_returns_current_avg_bedtime_string(self):
        nights = self._nights([
            (f'2026-04-{d:02d}', f'2026-04-{d:02d}T22:00:00')
            for d in range(1, 15)
        ])
        result = detect_bedtime_drift(nights)
        assert result['current_avg_bedtime'].startswith('22:')
        assert ':' in result['current_avg_bedtime']


# ── Training-diary backfill ──────────────────────────────────────

class TestBackfillFitnessHistory:
    def _seed(self, days=35, load=50.0):
        today = date.today()
        daily = {}
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            daily[d] = {'total': load, 'by_sport': {'cycling': load},
                        'activities': []}
        history = {'schema_version': 2, 'daily_loads': daily,
                   'snapshots': [], 'sleep_history': [],
                   'readiness_history': []}
        save_fitness_history(history)
        return today

    def test_dry_run_reports_without_writing(self, monkeypatch):
        today = self._seed()
        client = FakeGarminClient()
        patch_garmin_everywhere(monkeypatch, client)
        since = today - timedelta(days=9)
        report = backfill_fitness_history(since, today, today=today, apply=False)
        assert report['dry_run'] is True
        assert report['missing']['snapshots'] == 10
        assert report['missing']['sleep'] == 10
        assert report['missing']['readiness'] == 10
        # Dry-run never hits Garmin and never writes
        assert client.call_counts.get('get_sleep_data', 0) == 0
        assert client.call_counts.get('get_training_readiness', 0) == 0
        assert load_fitness_history()['snapshots'] == []

    def test_apply_fills_and_is_idempotent(self, monkeypatch):
        import coach.fitness as fitness
        monkeypatch.setattr(fitness, 'BACKFILL_THROTTLE_SECS', 0)
        today = self._seed()
        client = FakeGarminClient()
        patch_garmin_everywhere(monkeypatch, client)
        since = today - timedelta(days=4)
        report = backfill_fitness_history(since, today, today=today, apply=True)
        assert report['added']['snapshots'] == 5
        assert report['added']['sleep'] + report['unavailable']['sleep'] == 5
        assert report['added']['readiness'] + report['unavailable']['readiness'] == 5
        assert report['auth_error'] is None
        assert 'data-backups' in report['backup']
        saved = load_fitness_history()
        assert len(saved['snapshots']) == 5
        assert [s['date'] for s in saved['snapshots']] == sorted(
            s['date'] for s in saved['snapshots'])
        assert saved['snapshots'][0]['total']['ctl'] > 0
        # Add-only idempotency: a second apply adds nothing new
        again = backfill_fitness_history(since, today, today=today, apply=True)
        assert again['added']['snapshots'] == 0
        assert again['added']['sleep'] == 0

    def test_tool_validates_dates(self):
        from coach.tools.fitness_tools import backfill_history
        assert 'error' in backfill_history(since='not-a-date')
        far = (date.today() - timedelta(days=500)).isoformat()
        assert 'Range too large' in backfill_history(since=far)['error']
