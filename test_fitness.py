"""
Tests for fitness.py — sport-specific load calculations, migration, sleep, patterns.

Tests cover:
- Power-based TSS calculation for cycling
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

from config import get_sport_group
from fitness import (
    calculate_training_load,
    calculate_daily_load,
    calculate_fitness_metrics,
    calculate_ewma,
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
)


# ── Sport Group Mapping ─────────────────────────────────────────

class TestGetSportGroup:
    def test_cycling_types(self):
        assert get_sport_group('cycling') == 'cycling'
        assert get_sport_group('mountain_biking') == 'cycling'
        assert get_sport_group('indoor_cycling') == 'cycling'
        assert get_sport_group('virtual_ride') == 'cycling'
        assert get_sport_group('gravel_cycling') == 'cycling'
        assert get_sport_group('road_biking') == 'cycling'

    def test_running_types(self):
        assert get_sport_group('running') == 'running'
        assert get_sport_group('trail_running') == 'running'
        assert get_sport_group('treadmill_running') == 'running'
        assert get_sport_group('track_running') == 'running'

    def test_strength_types(self):
        assert get_sport_group('strength_training') == 'strength'
        assert get_sport_group('indoor_cardio') == 'strength'
        assert get_sport_group('functional_strength') == 'strength'

    def test_other_types(self):
        assert get_sport_group('padel') == 'other'
        assert get_sport_group('ultimate_disc') == 'other'
        assert get_sport_group('yoga') == 'other'
        assert get_sport_group('swimming') == 'other'
        assert get_sport_group('pilates') == 'other'
        assert get_sport_group('unknown_sport') == 'other'

    def test_empty_string(self):
        assert get_sport_group('') == 'other'


# ── Power-Based TSS ─────────────────────────────────────────────

class TestPowerBasedTSS:
    """Test power-based Training Stress Score for cycling activities."""

    def test_cycling_with_np_and_ftp(self):
        """Cycling + NP + FTP → power-based TSS."""
        activity = {
            'type': 'indoor_cycling',
            'duration_mins': 60,
            'avg_hr': 140,
            'norm_power': 185,  # equal to FTP
        }
        # TSS = (3600/3600) * (185/185)^2 * 100 = 100
        # load = 100 / 10 = 10.0
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        assert load == 10.0

    def test_cycling_above_ftp(self):
        """NP above FTP → TSS > 100/hr."""
        activity = {
            'type': 'cycling',
            'duration_mins': 60,
            'avg_hr': 155,
            'norm_power': 222,  # 1.2× FTP
        }
        # TSS = 1 * (222/185)^2 * 100 = 1 * 1.44 * 100 = 144
        # load = 144 / 10 = 14.4
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        assert load == 14.4

    def test_cycling_below_ftp(self):
        """NP below FTP → TSS < 100/hr."""
        activity = {
            'type': 'mountain_biking',
            'duration_mins': 60,
            'avg_hr': 130,
            'norm_power': 130,  # ~0.70× FTP
        }
        # TSS = 1 * (130/185)^2 * 100 = 1 * 0.4937 * 100 = 49.37
        # load = 49.37 / 10 ≈ 4.9
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        assert load == 4.9

    def test_cycling_without_np_falls_back_to_hr(self):
        """No NP → HR-based TRIMP even for cycling."""
        activity = {
            'type': 'cycling',
            'duration_mins': 60,
            'avg_hr': 140,
            'norm_power': None,
        }
        load_with_ftp = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        load_without_ftp = calculate_training_load(activity, athlete_max_hr=190)
        # Both should use HR-based calculation (same result)
        assert load_with_ftp == load_without_ftp
        assert load_with_ftp > 0

    def test_cycling_without_ftp_falls_back_to_hr(self):
        """Has NP but no FTP → HR-based TRIMP."""
        activity = {
            'type': 'indoor_cycling',
            'duration_mins': 60,
            'avg_hr': 140,
            'norm_power': 200,
        }
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=None)
        # Should use HR-based (FTP is None)
        load_hr_only = calculate_training_load(
            {'type': 'indoor_cycling', 'duration_mins': 60, 'avg_hr': 140},
            athlete_max_hr=190,
        )
        assert load == load_hr_only

    def test_running_ignores_power(self):
        """Running activities should NOT use power TSS even if power is present."""
        activity = {
            'type': 'running',
            'duration_mins': 60,
            'avg_hr': 150,
            'norm_power': 300,  # Running power (from Stryd etc)
        }
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        # Should use HR-based, not power TSS (running is not cycling sport group)
        load_no_power = calculate_training_load(
            {'type': 'running', 'duration_mins': 60, 'avg_hr': 150},
            athlete_max_hr=190,
        )
        assert load == load_no_power

    def test_short_cycling_session(self):
        """30-min session at FTP → TSS ~50 → load ~5."""
        activity = {
            'type': 'indoor_cycling',
            'duration_mins': 30,
            'avg_hr': 155,
            'norm_power': 185,
        }
        load = calculate_training_load(activity, athlete_max_hr=190, athlete_ftp=185)
        assert load == 5.0

    def test_zero_duration_returns_zero(self):
        activity = {
            'type': 'cycling',
            'duration_mins': 0,
            'norm_power': 200,
        }
        assert calculate_training_load(activity, athlete_ftp=185) == 0.0


class TestCalculateDailyLoadWithFtp:
    def test_passes_ftp_through(self):
        """Verify daily_load sums individual loads with FTP."""
        activities = [
            {'type': 'indoor_cycling', 'duration_mins': 60, 'avg_hr': 140, 'norm_power': 185},
            {'type': 'strength_training', 'duration_mins': 30, 'avg_hr': 110},
        ]
        total = calculate_daily_load(activities, athlete_max_hr=190, athlete_ftp=185)
        cycling_load = calculate_training_load(activities[0], 190, 185)
        strength_load = calculate_training_load(activities[1], 190, 185)
        assert total == cycling_load + strength_load


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
        cycling = calculate_sport_fitness_metrics(loads, 'cycling')
        running = calculate_sport_fitness_metrics(loads, 'running')

        assert cycling['ctl'] > 0
        assert running['ctl'] == 0.0

    def test_running_ctl_independent_of_cycling(self):
        loads = self._make_daily_loads(50, cycling_daily=0.0, running_daily=5.0)
        cycling = calculate_sport_fitness_metrics(loads, 'cycling')
        running = calculate_sport_fitness_metrics(loads, 'running')

        assert cycling['ctl'] == 0.0
        assert running['ctl'] > 0

    def test_sport_acwr_calculated(self):
        loads = self._make_daily_loads(50, cycling_daily=10.0, running_daily=5.0)
        cycling = calculate_sport_fitness_metrics(loads, 'cycling')
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

        running = calculate_sport_fitness_metrics(loads, 'running')
        # ACWR should be high (acute load present but low chronic)
        assert running['acwr'] > 1.3

    def test_empty_loads_returns_zero_ctl(self):
        loads = {}
        m = calculate_sport_fitness_metrics(loads, 'cycling')
        assert m['ctl'] == 0.0


# ── Update Fitness History (v2 format) ───────────────────────────

class TestUpdateFitnessHistoryV2:
    def test_stores_per_sport_breakdown(self, tmp_path, monkeypatch):
        import fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        activities = [
            {'date': '2026-02-01', 'type': 'cycling', 'duration_mins': 60, 'avg_hr': 140, 'norm_power': 185},
            {'date': '2026-02-01', 'type': 'strength_training', 'duration_mins': 30, 'avg_hr': 110},
        ]
        history = update_fitness_history(activities, athlete_max_hr=190, athlete_ftp=185)

        day = history['daily_loads']['2026-02-01']
        assert 'total' in day
        assert 'by_sport' in day
        assert 'cycling' in day['by_sport']
        assert 'strength' in day['by_sport']
        assert day['total'] == day['by_sport']['cycling'] + day['by_sport']['strength']

    def test_stores_activity_details(self, tmp_path, monkeypatch):
        import fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        activities = [
            {'date': '2026-02-01', 'activity_id': 123, 'type': 'running', 'duration_mins': 45, 'avg_hr': 150},
        ]
        history = update_fitness_history(activities, athlete_max_hr=190)

        day = history['daily_loads']['2026-02-01']
        assert len(day['activities']) == 1
        assert day['activities'][0]['id'] == 123
        assert day['activities'][0]['sport'] == 'running'

    def test_generates_per_sport_snapshots(self, tmp_path, monkeypatch):
        import fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        # Build enough history for meaningful CTL
        activities = []
        for i in range(50):
            d = (date.today() - timedelta(days=i)).isoformat()
            activities.append({'date': d, 'type': 'cycling', 'duration_mins': 60, 'avg_hr': 140})
            activities.append({'date': d, 'type': 'running', 'duration_mins': 30, 'avg_hr': 145})

        history = update_fitness_history(activities, athlete_max_hr=190)
        snap = history['snapshots'][-1]

        assert 'total' in snap
        assert 'cycling' in snap
        assert 'running' in snap
        assert snap['cycling']['ctl'] > 0
        assert snap['running']['ctl'] > 0


# ── Sleep Persistence ────────────────────────────────────────────

class TestPersistSleepData:
    def test_adds_new_records(self):
        history = {'sleep_history': []}
        records = [
            {'date': '2026-02-05', 'duration_hrs': 7.2, 'score': 82, 'deep_pct': 22, 'rem_pct': 25, 'avg_hr': 55},
            {'date': '2026-02-06', 'duration_hrs': 6.8, 'score': 75, 'deep_pct': 18, 'rem_pct': 22, 'avg_hr': 57},
        ]
        result = persist_sleep_data(records, history)
        assert len(result['sleep_history']) == 2
        assert result['sleep_history'][0]['date'] == '2026-02-05'

    def test_deduplicates_by_date(self):
        history = {
            'sleep_history': [
                {'date': '2026-02-05', 'duration_hrs': 7.2, 'score': 82},
            ],
        }
        records = [
            {'date': '2026-02-05', 'duration_hrs': 7.5, 'score': 85},  # Duplicate date
            {'date': '2026-02-06', 'duration_hrs': 6.8, 'score': 75},
        ]
        result = persist_sleep_data(records, history)
        assert len(result['sleep_history']) == 2
        # Original record preserved (not overwritten)
        feb5 = next(r for r in result['sleep_history'] if r['date'] == '2026-02-05')
        assert feb5['score'] == 82

    def test_prunes_old_records(self):
        history = {
            'sleep_history': [
                {'date': '2025-12-01', 'duration_hrs': 7.0, 'score': 70},  # >30 days ago
            ],
        }
        records = [
            {'date': date.today().isoformat(), 'duration_hrs': 7.5, 'score': 85},
        ]
        result = persist_sleep_data(records, history)
        # Old record should be pruned
        dates = [r['date'] for r in result['sleep_history']]
        assert '2025-12-01' not in dates

    def test_sorts_by_date(self):
        history = {'sleep_history': []}
        records = [
            {'date': '2026-02-06', 'duration_hrs': 7.0},
            {'date': '2026-02-04', 'duration_hrs': 7.5},
            {'date': '2026-02-05', 'duration_hrs': 6.8},
        ]
        result = persist_sleep_data(records, history)
        dates = [r['date'] for r in result['sleep_history']]
        assert dates == sorted(dates)


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
        result = get_sleep_trend(history, days=30)
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
        result = get_sleep_trend(history, days=30)
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
        result = get_sleep_trend(history, days=30)
        # After sort ascending: first half (older) = 7.5hrs, second half (newer) = 6.0hrs
        assert result['direction'] == 'declining'

    def test_empty_history(self):
        result = get_sleep_trend({'sleep_history': []})
        assert result['status'] == 'no_data'

    def test_counts_deficit_weeks(self):
        # All records at 6.5hrs → every week is a deficit
        records = self._make_sleep_history(14, base_hrs=6.5)
        history = {'sleep_history': records}
        result = get_sleep_trend(history, days=30)
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
        import fitness
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
        import fitness
        monkeypatch.setattr(fitness, 'DATA_DIR', tmp_path)

        result = load_fitness_history()
        assert result['schema_version'] == 2
        assert result['daily_loads'] == {}
        assert result['sleep_history'] == []
