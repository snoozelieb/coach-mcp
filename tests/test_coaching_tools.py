"""Tests for tools/coaching_tools.py — helper functions (pure logic, no mocking needed)."""
import json
import pytest
from datetime import date, timedelta
from unittest.mock import patch

from coach.tools.coaching_tools import (
    get_coaching_snapshot,
    _compare_planned_actual,
    _parse_readiness_for_snapshot,
    _build_adaptation_patterns,
    _build_snapshot_flags,
    _build_compliance_diagnostics,
    _derive_sleep_trend_direction,
    _derive_hrv_trend,
    _derive_compliance_rate_pct,
    _analyze_sport_priorities,
    _build_week_grid,
    _summarize_plan_adherence_by_pillar,
    _compute_coaching_score,
)
from conftest import FakeGarminClient, patch_garmin_everywhere


# ---------------------------------------------------------------------------
# _compare_planned_actual
# ---------------------------------------------------------------------------

class TestComparePlannedActual:
    def test_no_plan_returns_status(self):
        result = _compare_planned_actual(None, [], date(2026, 1, 15))
        assert result['status'] == 'no_plan'

    def test_empty_plan_days_returns_no_plan(self):
        result = _compare_planned_actual({'days': {}}, [], date(2026, 1, 15))
        assert result['status'] == 'no_plan'

    def test_plan_with_only_rest_days(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'rest'}},
                '2026-01-14': {'planned': {}},
            }
        }
        result = _compare_planned_actual(plan, [], date(2026, 1, 15))
        assert result['sessions_planned'] == 0

    def test_rest_day_variant_not_counted(self):
        """rest_day, Rest, REST — all treated as rest, not as missing sessions."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'rest_day'}},
                '2026-01-14': {'planned': {'type': 'Rest'}},
            }
        }
        result = _compare_planned_actual(plan, [], date(2026, 1, 15))
        assert result['sessions_planned'] == 0
        assert result['sessions_missed'] == 0
        missing = [a for a in result['anomalies'] if a['flag'] == 'missing']
        assert len(missing) == 0

    def test_all_completed_matched_status(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
            {'date': '2026-01-14', 'type': 'strength_training', 'duration_mins': 55},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_planned'] == 2
        assert result['sessions_completed'] == 2
        assert result['sessions_missed'] == 0
        assert result['completion_rate'] == 100.0
        assert all(d['status'] == 'matched' for d in result['details'])
        # Matched entries are minimal (date + status only)
        for d in result['details']:
            assert set(d.keys()) == {'date', 'status'}

    def test_missed_sessions_anomaly(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['sessions_missed'] == 1
        assert result['completion_rate'] == 50.0
        # Missing session surfaced as anomaly
        missing_anomalies = [a for a in result['anomalies'] if a['flag'] == 'missing']
        assert len(missing_anomalies) == 1
        assert missing_anomalies[0]['planned_type'] == 'strength_training'

    def test_pending_future_sessions(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-16': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['sessions_pending'] == 1

    def test_duration_surplus_anomaly(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 75},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))
        surplus_anomalies = [a for a in result['anomalies'] if a['flag'] == 'duration_delta' and a['delta_pct'] > 0]
        assert len(surplus_anomalies) == 1
        assert surplus_anomalies[0]['delta_pct'] > 30

    def test_duration_gap_partial_status(self):
        """A session significantly shorter than planned gets 'partial' status and duration_delta anomaly."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 30},  # 67% short
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['details'][0]['status'] == 'partial'
        gap_anomalies = [a for a in result['anomalies'] if a['flag'] == 'duration_delta' and a['delta_pct'] < 0]
        assert len(gap_anomalies) == 1

    def test_type_mismatch_detected(self):
        """When planned type differs from actual type, surfaces type_mismatch anomaly."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'race', 'duration_mins': 240}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 84},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        type_anomalies = [a for a in result['anomalies'] if a['flag'] == 'type_mismatch']
        assert len(type_anomalies) == 1
        assert type_anomalies[0]['planned_type'] == 'race'
        assert type_anomalies[0]['actual_type'] == 'cycling'
        assert result['details'][0]['status'] == 'type_mismatch'

    def test_unplanned_activity_on_rest_day(self):
        """Activity on a rest day gets 'unplanned' anomaly."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'rest'}},
                '2026-01-14': {'planned': {'type': 'running', 'duration_mins': 45}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 60},
            {'date': '2026-01-14', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        unplanned = [a for a in result['anomalies'] if a['flag'] == 'unplanned']
        assert len(unplanned) == 1
        assert unplanned[0]['activity_type'] == 'cycling'

    def test_multi_activity_best_match_by_type(self):
        """When multiple activities exist, the one matching planned type is used."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        # Morning run + evening cycling — code finds the cycling match
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 30},
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 90},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        detail = result['details'][0]
        assert detail['actual_type'] == 'cycling'
        assert detail['status'] == 'matched'
        # All activities for the day are included
        assert 'all_activities' in detail
        assert len(detail['all_activities']) == 2

    def test_no_type_match_yields_missing_plus_unplanned(self):
        """A taxonomy-known planned type NEVER pairs with non-matching actuals
        (the June 2026 crosswire fix) — missing + unplanned instead."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'swimming', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 30},
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 90},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 0
        assert result['sessions_missed'] == 1
        missing = [a for a in result['anomalies'] if a['flag'] == 'missing']
        assert len(missing) == 1
        assert missing[0]['planned_type'] == 'swimming'
        unplanned = [a for a in result['anomalies'] if a['flag'] == 'unplanned']
        assert {a['activity_type'] for a in unplanned} == {'running', 'cycling'}
        assert not [a for a in result['anomalies'] if a['flag'] == 'type_mismatch']

    def test_single_matched_activity_minimal(self):
        """Single matched activity produces minimal detail (no all_activities, no type/duration)."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        detail = result['details'][0]
        assert detail == {'date': '2026-01-13', 'status': 'matched'}

    def test_invalid_date_key_skipped(self):
        """Non-ISO date keys in plan are silently skipped, not counted."""
        plan = {
            'days': {
                'Monday': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-14', 'type': 'strength_training', 'duration_mins': 55},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        # Only the valid ISO date should be processed
        assert result['sessions_planned'] == 1
        assert result['sessions_completed'] == 1

    def test_zero_planned_duration_no_anomaly(self):
        """When planned_duration is 0 (or missing), the duration comparison is skipped.
        This guards against a division-by-zero in the percentage calculation."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 0}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        duration_anomalies = [a for a in result['anomalies'] if a['flag'] == 'duration_delta']
        assert len(duration_anomalies) == 0

    def test_matched_entry_is_minimal(self):
        """Matched entries with small duration deltas are trimmed to {date, status} only."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 50}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        # 10% short, not anomalous — matched entry is minimal
        detail = result['details'][0]
        assert detail['status'] == 'matched'
        assert set(detail.keys()) == {'date', 'status'}


# ---------------------------------------------------------------------------
# _parse_readiness_for_snapshot
# ---------------------------------------------------------------------------

class TestParseReadinessForSnapshot:
    def test_empty_data(self):
        result = _parse_readiness_for_snapshot({})
        assert result['status'] == 'unavailable'

    def test_none_data(self):
        result = _parse_readiness_for_snapshot(None)
        assert result['status'] == 'unavailable'

    def test_returns_structured_data_no_recommendation(self):
        """Readiness returns structured data fields, no prose recommendation."""
        readiness = {
            'score': 85,
            'level': 'PRIME',
            'hrvStatus': 'BALANCED',
            'sleepScore': 90,
            'recoveryTime': 120,
        }
        result = _parse_readiness_for_snapshot(readiness)
        assert result['score'] == 85
        assert result['level'] == 'PRIME'
        assert result['hrv_status'] == 'BALANCED'
        assert 'recommendation' not in result  # No prose — LLM interprets


# ---------------------------------------------------------------------------
# _build_adaptation_patterns + derived field helpers
# ---------------------------------------------------------------------------

class TestBuildAdaptationPatterns:
    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_patterns_from_coaching_log(self, mock_log):
        mock_log.return_value = {
            'athlete_responses': [
                {'date': '2026-01-01', 'stimulus': 'test', 'response': 'positive', 'pattern': 'handles_volume_well'},
                {'date': '2026-01-02', 'stimulus': 'test', 'response': 'good', 'pattern': 'handles_volume_well'},
            ]
        }

        result = _build_adaptation_patterns()

        assert result['handles_volume_well'] is True
        assert result['total_responses'] == 2

    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_empty_coaching_log(self, mock_log):
        mock_log.return_value = {'athlete_responses': []}

        result = _build_adaptation_patterns()

        assert result['handles_volume_well'] is False
        assert result['total_responses'] == 0

    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_coaching_log_exception_handled(self, mock_log):
        mock_log.side_effect = Exception("File not found")

        result = _build_adaptation_patterns()

        assert result['handles_volume_well'] is None
        assert result['patterns_logged'] == 0

    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_tied_patterns_default_to_false(self, mock_log):
        """When competing patterns have equal counts (e.g., 2x handles_volume_well
        vs 2x struggles_with_volume), the comparison > returns False."""
        mock_log.return_value = {
            'athlete_responses': [
                {'pattern': 'handles_volume_well'},
                {'pattern': 'handles_volume_well'},
                {'pattern': 'struggles_with_volume'},
                {'pattern': 'struggles_with_volume'},
            ]
        }

        result = _build_adaptation_patterns()

        # Tie: 2 == 2, so 2 > 2 is False
        assert result['handles_volume_well'] is False

    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_quantified_included_when_sufficient_data(self, mock_log):
        """When enough numeric responses exist, quantified thresholds appear."""
        responses = [
            {'pattern': 'handles_volume_well', 'load_change_pct': 12,
             'compliance_result': True, 'injury_flag': False}
            for _ in range(10)
        ]
        mock_log.return_value = {'athlete_responses': responses}

        result = _build_adaptation_patterns()

        assert 'quantified' in result
        assert 'volume_tolerance' in result['quantified']
        assert result['quantified']['confidence'] in ('moderate', 'high')

    @patch('coach.tools.coaching_tools.load_coaching_log')
    def test_no_quantified_when_insufficient_data(self, mock_log):
        """Boolean flags still work without quantified data."""
        responses = [
            {'pattern': 'handles_volume_well', 'load_change_pct': 12}
            for _ in range(3)
        ]
        mock_log.return_value = {'athlete_responses': responses}

        result = _build_adaptation_patterns()

        assert 'quantified' not in result
        assert result['handles_volume_well'] is True


class TestDerivedFields:
    """Tests for derived fields relocated to root-level parents."""

    def test_sleep_trend_improving(self):
        assert _derive_sleep_trend_direction({'recent_trend': 0.5}) == 'improving'

    def test_sleep_trend_declining(self):
        assert _derive_sleep_trend_direction({'recent_trend': -0.5}) == 'declining'

    def test_sleep_trend_stable(self):
        assert _derive_sleep_trend_direction({'recent_trend': 0.1}) == 'stable'

    def test_sleep_trend_none_data(self):
        assert _derive_sleep_trend_direction(None) == 'stable'

    def test_hrv_trend_stable(self):
        assert _derive_hrv_trend({'hrv_status': 'BALANCED'}) == 'stable'
        assert _derive_hrv_trend({'hrv_status': 'GOOD'}) == 'stable'

    def test_hrv_trend_declining(self):
        assert _derive_hrv_trend({'hrv_status': 'LOW'}) == 'declining'
        assert _derive_hrv_trend({'hrv_status': 'POOR'}) == 'declining'

    def test_hrv_trend_unknown(self):
        assert _derive_hrv_trend({'hrv_status': ''}) == 'unknown'
        assert _derive_hrv_trend(None) == 'unknown'

    def test_compliance_rate_calculation(self):
        """Compliance rate is calculated from pillar counts."""
        compliance = {
            'overall_compliant': False,
            'strength': {'compliant': True},
            'mobility': {'compliant': False},
            'long_effort': {'compliant': True},
        }
        # 2 of 3 pillars met = 67%
        assert _derive_compliance_rate_pct(compliance) == 67.0

    def test_compliance_rate_all_met(self):
        compliance = {
            'strength': {'compliant': True},
            'mobility': {'compliant': True},
            'long_effort': {'compliant': True},
        }
        assert _derive_compliance_rate_pct(compliance) == 100.0

    def test_compliance_rate_no_pillars(self):
        assert _derive_compliance_rate_pct({}) is None


# ---------------------------------------------------------------------------
# _analyze_sport_priorities
# ---------------------------------------------------------------------------

class TestAnalyzeSportPriorities:
    def test_single_cycling_event(self):
        # Relative date so the test isn't date-dependent
        event_date = (date.today() + timedelta(days=45)).isoformat()
        events = [{'name': 'sani2c', 'date': event_date, 'priority': 'A', 'type': 'multi_day_mtb'}]
        result = _analyze_sport_priorities(events, {}, {}, date.today())

        assert 'cycling' in result['sports']
        assert result['sports']['cycling']['primary_focus'] is True
        assert result['has_multi_sport'] is False

    def test_multi_sport_volume_sums_to_100(self):
        # Relative dates so the test isn't date-dependent
        cycling_date = (date.today() + timedelta(days=60)).isoformat()
        running_date = (date.today() + timedelta(days=30)).isoformat()
        events = [
            {'name': 'sani2c', 'date': cycling_date, 'priority': 'A', 'type': 'multi_day_mtb'},
            {'name': '10k trail', 'date': running_date, 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {}, date.today())

        assert result['has_multi_sport'] is True
        total_pct = sum(s['volume_pct'] for s in result['sports'].values())
        assert abs(total_pct - 100.0) < 0.5

    def test_past_events_filtered(self):
        # Relative dates so the test isn't date-dependent
        past_date = (date.today() - timedelta(days=120)).isoformat()
        future_date = (date.today() + timedelta(days=45)).isoformat()
        events = [
            {'name': 'past_race', 'date': past_date, 'priority': 'A', 'type': 'road_cycling'},
            {'name': 'future_race', 'date': future_date, 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {}, date.today())

        assert 'cycling' not in result['sports']
        assert 'running' in result['sports']

    def test_no_events(self):
        result = _analyze_sport_priorities([], {}, {}, date.today())

        assert result['sports'] == {}
        assert result['has_multi_sport'] is False

    def test_closer_race_gets_higher_volume_pct(self):
        """A close B-race should get more volume than a far A-race when time_weight
        dominates. Verify the actual percentages, not just has_multi_sport."""
        close_date = (date.today() + timedelta(days=13)).isoformat()
        far_date = (date.today() + timedelta(days=200)).isoformat()
        events = [
            {'name': 'far_race', 'date': far_date, 'priority': 'A', 'type': 'multi_day_mtb'},
            {'name': 'close_race', 'date': close_date, 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {}, date.today())

        # close B-race (13 days away): priority_weight=3, time_weight=4 → score=12
        # far A-race (200 days away): priority_weight=4, time_weight=1 → score=4
        running = result['sports']['running']
        cycling = result['sports']['cycling']
        assert running['volume_pct'] > cycling['volume_pct']
        assert running['total_score'] == 12
        assert cycling['total_score'] == 4


# ---------------------------------------------------------------------------
# Snapshot coaching_memory + data_quality — driven through the REAL snapshot
#
# These used to re-implement the slicing/flag logic inside the test body and
# assert on their own copy (zero production coverage). They now seed the
# sandbox DATA_DIR, answer Garmin with the canonical FakeGarminClient, and
# assert on get_coaching_snapshot()'s actual output.
# ---------------------------------------------------------------------------

def _seed_snapshot_env(data_dir, *, athlete=None, training_config=None,
                       coaching_log=None, history_days=10):
    """Minimal-but-valid data files for driving the real snapshot."""
    today = date.today()
    daily_loads = {}
    for i in range(history_days):
        d_iso = (today - timedelta(days=i)).isoformat()
        daily_loads[d_iso] = {
            'total': 50.0,
            'by_sport': {'cycling': 50.0},
            'activities': [{'id': i, 'type': 'cycling', 'sport': 'cycling',
                            'duration_mins': 60, 'load': 50.0, 'avg_hr': 130,
                            'date': d_iso}],
        }
    files = {
        'fitness_history.json': {
            'schema_version': 2, 'daily_loads': daily_loads, 'snapshots': [],
            'sleep_history': [], 'readiness_history': [],
            'last_updated': today.isoformat(),
            'last_activity_ingest_date': today.isoformat(),
        },
        'athlete.json': athlete if athlete is not None else {
            'personal': {'name': 'Test Athlete', 'age': 36, 'weight_kg': 70,
                         'max_hr': 188, 'resting_hr': 52},
            'injury_history': [], 'life_constraints': {}, 'preferences': {},
        },
        'training_config.json': training_config if training_config is not None else {
            'current_block': {'phase': 'build'},
            'events': [{'name': 'Test Race',
                        'date': (today + timedelta(days=45)).isoformat(),
                        'type': 'gravel', 'priority': 'A'}],
        },
        'coaching_log.json': coaching_log if coaching_log is not None else {
            'decisions': [], 'athlete_responses': [], 'pending_approvals': [],
        },
    }
    for filename, payload in files.items():
        (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')
    return today


class TestSnapshotCoachingMemory:
    """coaching_memory in the real snapshot payload (production slicing)."""

    async def test_caps_at_five_decisions_newest_first(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        """Regression for the memory-recency bug: the snapshot must surface
        the five MOST RECENT active decisions, newest first — not the five
        oldest (the [:5]-on-append-order bug that starved coaching memory)."""
        today = date.today()
        coaching_log = {
            'decisions': [
                {'id': f'd{i}', 'date': (today - timedelta(days=i)).isoformat(),
                 'type': 'load_adjustment', 'decision': f'decision {i}',
                 'status': 'active'}
                for i in range(8)
            ],
            'athlete_responses': [
                {'date': (today - timedelta(days=i)).isoformat(),
                 'stimulus': f's{i}', 'response': 'good',
                 'pattern': 'handles_volume_well'}
                for i in range(5)
            ],
            'pending_approvals': [
                {'id': 'p1', 'change': 'increase volume 15%',
                 'expires': (today + timedelta(days=5)).isoformat()},
            ],
        }
        _seed_snapshot_env(sandbox_data_dir, coaching_log=coaching_log)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        memory = result['coaching_memory']
        assert [d['id'] for d in memory['active_decisions']] == [
            'd0', 'd1', 'd2', 'd3', 'd4']  # capped at 5, newest first
        assert len(memory['pending_approvals']) == 1
        assert memory['pending_approvals'][0]['id'] == 'p1'
        # Recent responses capped at 3, newest first
        assert [r['stimulus'] for r in memory['recent_responses']] == [
            's0', 's1', 's2']

    async def test_expired_pending_approvals_filtered(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        today = date.today()
        coaching_log = {
            'decisions': [], 'athlete_responses': [],
            'pending_approvals': [
                {'id': 'stale', 'change': 'old idea',
                 'expires': (today - timedelta(days=1)).isoformat()},
                {'id': 'live', 'change': 'current proposal',
                 'expires': (today + timedelta(days=3)).isoformat()},
            ],
        }
        _seed_snapshot_env(sandbox_data_dir, coaching_log=coaching_log)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        pending = result['coaching_memory']['pending_approvals']
        assert [p['id'] for p in pending] == ['live']

    async def test_empty_log_yields_empty_but_valid_memory(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        _seed_snapshot_env(sandbox_data_dir)  # default: empty coaching log
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        memory = result['coaching_memory']
        assert memory['active_decisions'] == []
        assert memory['pending_approvals'] == []
        assert memory['decisions_due_review'] == []
        assert memory['recent_responses'] == []
        # Learned patterns are memory-section only — not in the core payload
        assert 'adaptation_patterns' not in memory

    async def test_memory_section_adds_adaptation_patterns(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        today = date.today()
        coaching_log = {
            'decisions': [], 'pending_approvals': [],
            'athlete_responses': [
                {'date': today.isoformat(), 'stimulus': 'big week',
                 'response': 'good', 'pattern': 'handles_volume_well'},
            ],
        }
        _seed_snapshot_env(sandbox_data_dir, coaching_log=coaching_log)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(
            await get_coaching_snapshot(mock_ctx, sections=['memory']))

        patterns = result['coaching_memory']['adaptation_patterns']
        assert 'handles_volume_well' in patterns


class TestDataQuality:
    """data_quality flags from the real snapshot payload."""

    async def test_flags_missing_weight_and_age(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        athlete = {
            'personal': {'name': 'Test Athlete', 'age': None, 'weight_kg': None,
                         'max_hr': 188, 'resting_hr': 52},
            'injury_history': [], 'life_constraints': {}, 'preferences': {},
        }
        _seed_snapshot_env(sandbox_data_dir, athlete=athlete)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        dq = result['data_quality']
        assert dq.get('weight') == 'missing'
        assert dq.get('age') == 'missing'
        assert 'name' not in dq  # name IS present — must not be flagged

    async def test_no_flags_when_data_complete(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        """Healthy Garmin + complete profile + upcoming A-race: every quality
        check passes and data_quality is exactly {} (always-present key)."""
        _seed_snapshot_env(sandbox_data_dir)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient())

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        assert result['data_quality'] == {}

    async def test_flags_unavailable_recovery_and_sleep(
            self, sandbox_data_dir, mock_ctx, monkeypatch):
        """Readiness + sleep endpoints down (non-auth errors): the snapshot
        degrades per-section and flags both in data_quality instead of
        aborting."""
        _seed_snapshot_env(sandbox_data_dir)
        client = FakeGarminClient(overrides={
            'get_training_readiness': Exception('readiness endpoint down'),
            'get_sleep_data': Exception('sleep endpoint down'),
        })
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(await get_coaching_snapshot(mock_ctx))

        dq = result['data_quality']
        assert dq.get('recovery') == 'unavailable'
        assert dq.get('sleep') == 'unavailable'
        assert 'sleep endpoint down' in dq.get('sleep_fetch_error', '')
        # Degraded, not dead: the rest of the snapshot is still there
        assert 'error' not in result
        assert result['week_grid']


# ---------------------------------------------------------------------------
# Activity fetch window — mid-week plan start
# ---------------------------------------------------------------------------

class TestMidWeekPlanActivityFetch:
    """Verify that activities before plan start but within the calendar week are visible."""

    def test_compare_planned_actual_ignores_activities_outside_plan_dates(self):
        """Activities fetched from before the plan start don't cause false anomalies.

        Plan starts Saturday Feb 7. Activity on Wednesday Feb 4 is in the fetched
        range (for compliance) but should NOT appear as missing/unplanned in
        planned_vs_actual since Feb 4 is not a plan day.
        """
        plan = {
            'week_start': '2026-02-07',
            'week_end': '2026-02-13',
            'days': {
                '2026-02-07': {'planned': {'type': 'cycling', 'duration_mins': 120}},
                '2026-02-08': {'planned': {'type': 'rest'}},
                '2026-02-09': {'planned': {'type': 'running', 'duration_mins': 45}},
            }
        }
        # Activities include one from before plan start (Wed Feb 4)
        activities = [
            {'date': '2026-02-04', 'type': 'running', 'duration_mins': 40},
            {'date': '2026-02-07', 'type': 'cycling', 'duration_mins': 115},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 2, 8))

        # Feb 4 activity should not appear in anomalies or details at all
        all_dates = [d['date'] for d in result['details']]
        assert '2026-02-04' not in all_dates

        anomaly_dates = [a['date'] for a in result['anomalies']]
        assert '2026-02-04' not in anomaly_dates

        # Feb 7 cycling should be matched
        assert result['sessions_completed'] == 1
        # Feb 9 running is pending (today is Feb 8)
        assert result['sessions_pending'] == 1

    def test_compare_planned_actual_with_pre_plan_and_plan_activities(self):
        """Full fetch range passed to comparison — plan-date activities still match correctly."""
        plan = {
            'week_start': '2026-02-05',
            'week_end': '2026-02-11',
            'days': {
                '2026-02-05': {'planned': {'type': 'running', 'duration_mins': 50}},
                '2026-02-06': {'planned': {'type': 'strength_training', 'duration_mins': 45}},
                '2026-02-07': {'planned': {'type': 'rest'}},
            }
        }
        # Activities from full calendar week (Mon Feb 2 onward) + plan dates
        activities = [
            {'date': '2026-02-02', 'type': 'cycling', 'duration_mins': 60},
            {'date': '2026-02-03', 'type': 'yoga', 'duration_mins': 30},
            {'date': '2026-02-05', 'type': 'running', 'duration_mins': 48},
            {'date': '2026-02-06', 'type': 'strength_training', 'duration_mins': 50},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 2, 7))

        assert result['sessions_completed'] == 2
        assert result['sessions_missed'] == 0

        # Pre-plan activities (Feb 2, Feb 3) should not appear
        all_dates = [d['date'] for d in result['details']]
        assert '2026-02-02' not in all_dates
        assert '2026-02-03' not in all_dates

    def test_calendar_week_filter_for_compliance(self):
        """Activities before plan start but within calendar week are included for compliance.

        This tests the filtering logic used in get_coaching_snapshot:
        activities_this_week filters from monday_this_week, regardless of plan start.
        """
        monday_this_week = date(2026, 2, 2)  # Monday

        # All fetched activities (from min(plan_start, monday))
        all_fetched = [
            {'date': '2026-02-02', 'type': 'cycling', 'duration_mins': 60},
            {'date': '2026-02-03', 'type': 'yoga', 'duration_mins': 30},
            {'date': '2026-02-05', 'type': 'running', 'duration_mins': 48},
            {'date': '2026-02-07', 'type': 'strength_training', 'duration_mins': 50},
        ]

        # Calendar week filter (same logic as in get_coaching_snapshot)
        activities_this_week = [
            a for a in all_fetched
            if a.get('date') and a['date'] >= monday_this_week.isoformat()
        ]

        # All 4 activities are within the calendar week (Mon Feb 2 - Sun Feb 8)
        assert len(activities_this_week) == 4

    def test_calendar_week_filter_excludes_prior_week(self):
        """Activities from before the calendar week's Monday are excluded from compliance."""
        monday_this_week = date(2026, 2, 9)  # Monday Feb 9

        # Plan started Feb 5 (Thursday of previous week)
        all_fetched = [
            {'date': '2026-02-05', 'type': 'running', 'duration_mins': 40},
            {'date': '2026-02-06', 'type': 'cycling', 'duration_mins': 60},
            {'date': '2026-02-09', 'type': 'strength_training', 'duration_mins': 45},
            {'date': '2026-02-10', 'type': 'running', 'duration_mins': 50},
        ]

        activities_this_week = [
            a for a in all_fetched
            if a.get('date') and a['date'] >= monday_this_week.isoformat()
        ]

        # Only Feb 9 and Feb 10 should be in the calendar week
        assert len(activities_this_week) == 2
        assert activities_this_week[0]['date'] == '2026-02-09'
        assert activities_this_week[1]['date'] == '2026-02-10'

    def test_fetch_start_uses_earlier_of_plan_and_monday(self):
        """Fetch start date should be min(plan_start, monday_this_week)."""
        today = date(2026, 2, 8)  # Saturday
        monday_this_week = today - timedelta(days=today.weekday())  # Feb 2

        # Case 1: Plan starts after Monday — fetch from Monday
        plan_start_1 = date(2026, 2, 5)  # Thursday
        assert min(plan_start_1, monday_this_week) == monday_this_week

        # Case 2: Plan starts before Monday — fetch from plan start
        plan_start_2 = date(2026, 1, 30)  # Friday of previous week
        assert min(plan_start_2, monday_this_week) == plan_start_2

        # Case 3: Plan starts on Monday — both are equal
        plan_start_3 = date(2026, 2, 2)  # Monday
        assert min(plan_start_3, monday_this_week) == monday_this_week


# ---------------------------------------------------------------------------
# _compare_planned_actual — anomaly context enrichment
# ---------------------------------------------------------------------------

class TestAnomalyContextEnrichment:
    """Tests for context enrichment when daily_loads/sleep_history are provided."""

    def test_anomaly_gets_sleep_context(self):
        """Missing session anomaly should include sleep data for that day."""
        plan = {
            'days': {
                '2026-03-14': {'planned': {'type': 'running', 'duration_mins': 60}},
            }
        }
        sleep_history = [
            {'date': '2026-03-14', 'score': 45, 'duration_hrs': 4.8},
        ]
        result = _compare_planned_actual(
            plan, [], date(2026, 3, 15),
            daily_loads={}, sleep_history=sleep_history,
        )
        assert len(result['anomalies']) == 1
        ctx = result['anomalies'][0].get('context', {})
        assert ctx['sleep_hours'] == 4.8
        assert ctx['sleep_score'] == 45

    def test_anomaly_gets_prior_day_load(self):
        """Anomaly should include prior day's load."""
        plan = {
            'days': {
                '2026-03-14': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        daily_loads = {
            '2026-03-13': {'total': 120.0, 'by_sport': {}, 'activities': []},
        }
        result = _compare_planned_actual(
            plan, [], date(2026, 3, 15),
            daily_loads=daily_loads, sleep_history=[],
        )
        ctx = result['anomalies'][0].get('context', {})
        assert ctx['prior_day_load'] == 120.0

    def test_no_context_without_data(self):
        """Without daily_loads/sleep_history, anomalies have no context key."""
        plan = {
            'days': {
                '2026-03-14': {'planned': {'type': 'running', 'duration_mins': 60}},
            }
        }
        result = _compare_planned_actual(plan, [], date(2026, 3, 15))
        assert 'context' not in result['anomalies'][0]

    def test_backward_compatible_without_new_params(self):
        """Existing call signature still works."""
        plan = {
            'days': {
                '2026-03-14': {'planned': {'type': 'running', 'duration_mins': 60}},
            }
        }
        result = _compare_planned_actual(plan, [], date(2026, 3, 15))
        assert result['sessions_missed'] == 1


# ---------------------------------------------------------------------------
# _build_snapshot_flags
# ---------------------------------------------------------------------------

class TestBuildSnapshotFlags:
    def test_empty_snapshot_returns_empty(self):
        flags = _build_snapshot_flags({}, date.today())
        assert flags == {}

    def test_acwr_warning_flagged(self):
        snapshot = {'acwr_warnings': [{'level': 'overall', 'zone': 'elevated'}]}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['acwr_warning'] is True

    def test_injuries_counted(self):
        snapshot = {'injuries': [{'name': 'knee'}, {'name': 'ankle'}]}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['active_injuries'] == 2

    def test_anomalies_counted(self):
        snapshot = {
            'planned_vs_actual': {
                'anomalies': [
                    {'flag': 'missing'},
                    {'flag': 'type_mismatch'},
                    {'flag': 'duration_delta'},
                ],
            },
        }
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['anomaly_count'] == 3

    def test_sleep_deficit_from_flag(self):
        snapshot = {'sleep': {'deficit_flag': True}}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['sleep_deficit'] is True

    def test_sleep_deficit_from_trend(self):
        snapshot = {'sleep': {'trend_direction': 'declining'}}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['sleep_deficit'] is True

    def test_pending_approvals(self):
        snapshot = {'coaching_memory': {'pending_approvals': [{'id': 1}]}}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['pending_approvals'] == 1

    def test_compliance_below_70(self):
        snapshot = {'compliance': {'compliance_rate_pct': 50.0}}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['compliance_below_70'] is True

    def test_compliance_above_70_not_flagged(self):
        snapshot = {'compliance': {'compliance_rate_pct': 85.0}}
        flags = _build_snapshot_flags(snapshot, date.today())
        assert 'compliance_below_70' not in flags

    def test_decisions_due_for_review(self):
        old_date = (date.today() - timedelta(days=10)).isoformat()
        snapshot = {
            'coaching_memory': {
                'active_decisions': [{'date': old_date}],
                'pending_approvals': [],
            }
        }
        flags = _build_snapshot_flags(snapshot, date.today())
        assert flags['decisions_due_for_review'] == 1

    def test_recent_decisions_not_flagged(self):
        recent_date = date.today().isoformat()
        snapshot = {
            'coaching_memory': {
                'active_decisions': [{'date': recent_date}],
                'pending_approvals': [],
            }
        }
        flags = _build_snapshot_flags(snapshot, date.today())
        assert 'decisions_due_for_review' not in flags


# ---------------------------------------------------------------------------
# _build_compliance_diagnostics
# ---------------------------------------------------------------------------

class TestBuildComplianceDiagnostics:
    PILLARS = {
        'strength': {
            'target_type': 'sessions',
            'target_sessions_per_week': 2,
            'types': ['strength_training'],
        },
        'endurance': {
            'target_type': 'hours',
            'target_hours_per_week': 4,
            'types': ['cycling', 'running'],
        },
    }

    def test_no_data(self):
        result = _build_compliance_diagnostics([], {})
        assert result['status'] == 'no_data'

    def test_all_met(self):
        week = [
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'cycling', 'duration_mins': 120},
            {'type': 'running', 'duration_mins': 120},
        ]
        weekly_4wk = [week] * 4
        result = _build_compliance_diagnostics(weekly_4wk, self.PILLARS)
        assert result['per_pillar']['strength']['met_weeks'] == 4
        assert result['per_pillar']['strength']['chronic_miss'] is False
        assert result['per_pillar']['endurance']['met_weeks'] == 4

    def test_chronic_miss_detected(self):
        good_week = [
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'cycling', 'duration_mins': 240},
        ]
        bad_week = [
            {'type': 'cycling', 'duration_mins': 240},
        ]
        weekly_4wk = [bad_week, bad_week, bad_week, good_week]
        result = _build_compliance_diagnostics(weekly_4wk, self.PILLARS)
        assert result['per_pillar']['strength']['met_weeks'] == 1
        assert result['per_pillar']['strength']['chronic_miss'] is True
        assert result['lowest_compliance_pillar'] == 'strength'

    def test_minutes_target_type(self):
        pillars = {
            'mobility': {
                'target_type': 'minutes',
                'target_minutes_per_week': 90,
                'types': ['yoga', 'stretching'],
            },
        }
        week_met = [{'type': 'yoga', 'duration_mins': 60}, {'type': 'stretching', 'duration_mins': 40}]
        week_missed = [{'type': 'yoga', 'duration_mins': 30}]
        weekly_4wk = [week_met, week_missed, week_met, week_missed]
        result = _build_compliance_diagnostics(weekly_4wk, pillars)
        assert result['per_pillar']['mobility']['met_weeks'] == 2

    def test_empty_weeks(self):
        weekly_4wk = [[], [], [], []]
        result = _build_compliance_diagnostics(weekly_4wk, self.PILLARS)
        assert result['per_pillar']['strength']['met_weeks'] == 0
        assert result['per_pillar']['strength']['chronic_miss'] is True

    def test_accepts_new_wrapper_format_via_normalizer(self):
        """Regression: snapshot passes the new wrapper-format training_pillars after
        normalisation through pillars_as_name_dict. Iterating the raw wrapper used
        to crash with `'str' object has no attribute 'get'` because keys like
        'based_on_persona' map to string values."""
        from coach.rules import pillars_as_name_dict
        wrapper = {
            'based_on_persona': 'multi_sport',
            'customized': True,
            'last_updated': '2026-01-13',
            'pillars': [
                {'name': 'strength', 'target_type': 'sessions',
                 'target_sessions_per_week': 2, 'types': ['strength_training']},
                {'name': 'endurance', 'target_type': 'hours',
                 'target_hours_per_week': 4, 'types': ['cycling', 'running']},
            ],
        }
        week = [
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'cycling', 'duration_mins': 240},
        ]
        weekly_4wk = [week] * 4
        normalized = pillars_as_name_dict(wrapper)
        result = _build_compliance_diagnostics(weekly_4wk, normalized)
        assert result['per_pillar']['strength']['met_weeks'] == 4
        assert result['per_pillar']['endurance']['met_weeks'] == 4


# ---------------------------------------------------------------------------
# _build_week_grid
# ---------------------------------------------------------------------------

class TestBuildWeekGrid:
    TODAY = date(2026, 4, 18)  # Saturday

    def test_returns_7_days_ending_today(self):
        result = _build_week_grid([], self.TODAY)
        keys = list(result.keys())
        assert len(keys) == 7
        assert keys[0] == '2026-04-12'  # today - 6
        assert keys[-1] == '2026-04-18'  # today

    def test_empty_activities_all_rest_days(self):
        result = _build_week_grid([], self.TODAY)
        for day_data in result.values():
            assert day_data['is_rest'] is True
            assert day_data['activity_count'] == 0
            assert day_data['types_summary'] == 'REST'
            assert day_data['types'] == []

    def test_day_of_week_accurate(self):
        result = _build_week_grid([], self.TODAY)
        assert result['2026-04-18']['day_of_week'] == 'Saturday'
        assert result['2026-04-12']['day_of_week'] == 'Sunday'

    def test_single_activity_recorded(self):
        acts = [{'date': '2026-04-18', 'type': 'cycling', 'duration_mins': 60.0}]
        result = _build_week_grid(acts, self.TODAY)
        assert result['2026-04-18']['is_rest'] is False
        assert result['2026-04-18']['activity_count'] == 1
        assert result['2026-04-18']['types_summary'] == 'cycling'
        assert result['2026-04-18']['total_duration_mins'] == 60.0

    def test_multiple_activities_grouped(self):
        acts = [
            {'date': '2026-04-18', 'type': 'cycling', 'duration_mins': 60.0},
            {'date': '2026-04-18', 'type': 'strength_training', 'duration_mins': 45.0},
            {'date': '2026-04-18', 'type': 'cycling', 'duration_mins': 15.0},
        ]
        result = _build_week_grid(acts, self.TODAY)
        assert result['2026-04-18']['activity_count'] == 3
        assert result['2026-04-18']['types_summary'] == 'cycling+strength_training'
        assert result['2026-04-18']['total_duration_mins'] == 120.0

    def test_is_today_only_for_today(self):
        result = _build_week_grid([], self.TODAY)
        is_today_days = [d for d, v in result.items() if v['is_today']]
        assert is_today_days == ['2026-04-18']

    def test_total_load_from_dict_schema(self):
        daily_loads = {'2026-04-18': {'total': 85.5, 'activities': []}}
        result = _build_week_grid([], self.TODAY, daily_loads=daily_loads)
        assert result['2026-04-18']['total_load'] == 85.5

    def test_total_load_from_scalar_legacy(self):
        daily_loads = {'2026-04-18': 42.7}
        result = _build_week_grid([], self.TODAY, daily_loads=daily_loads)
        assert result['2026-04-18']['total_load'] == 42.7

    def test_total_load_none_when_missing(self):
        result = _build_week_grid([], self.TODAY, daily_loads={})
        assert result['2026-04-18']['total_load'] is None

    def test_activities_outside_window_ignored(self):
        # Activity 10 days ago should not appear in 7-day window
        acts = [{'date': '2026-04-07', 'type': 'running', 'duration_mins': 30.0}]
        result = _build_week_grid(acts, self.TODAY)
        # No day in the window should have activities
        for v in result.values():
            assert v['activity_count'] == 0


# ---------------------------------------------------------------------------
# _summarize_plan_adherence_by_pillar
# ---------------------------------------------------------------------------

class TestPlanAdherenceByPillar:
    TODAY = date(2026, 4, 18)  # Saturday

    def _plan(self, days_dict):
        return {'days': days_dict}

    def test_no_plan_returns_zeros(self):
        result = _summarize_plan_adherence_by_pillar({}, [], self.TODAY)
        assert result['strength']['planned'] == 0
        assert result['strength']['skipped_dates'] == []
        assert result['strength']['deficit'] == 0

    def test_planned_and_completed_strength(self):
        plan = self._plan({
            '2026-04-13': {'planned': {'type': 'strength_training', 'duration_mins': 45}},
            '2026-04-15': {'planned': {'type': 'strength_training', 'duration_mins': 45}},
        })
        acts = [
            {'date': '2026-04-13', 'type': 'strength_training', 'duration_mins': 50},
            {'date': '2026-04-15', 'type': 'strength_training', 'duration_mins': 42},
        ]
        result = _summarize_plan_adherence_by_pillar(plan, acts, self.TODAY)
        assert result['strength']['planned'] == 2
        assert result['strength']['completed'] == 2
        assert result['strength']['skipped_dates'] == []

    def test_skipped_days_reported(self):
        plan = self._plan({
            '2026-04-13': {'planned': {'type': 'strength_training', 'duration_mins': 45}},  # Mon
            '2026-04-15': {'planned': {'type': 'strength_training', 'duration_mins': 45}},  # Wed
            '2026-04-17': {'planned': {'type': 'strength_training', 'duration_mins': 45}},  # Fri
        })
        acts = [
            {'date': '2026-04-17', 'type': 'strength_training', 'duration_mins': 45},
        ]
        result = _summarize_plan_adherence_by_pillar(plan, acts, self.TODAY)
        assert result['strength']['planned'] == 3
        assert result['strength']['completed'] == 1
        assert result['strength']['skipped_dates'] == ['2026-04-13', '2026-04-15']
        assert result['strength']['deficit'] == 2

    def test_future_sessions_are_pending_not_skipped(self):
        plan = self._plan({
            '2026-04-20': {'planned': {'type': 'strength_training', 'duration_mins': 45}},  # future
        })
        result = _summarize_plan_adherence_by_pillar(plan, [], self.TODAY)
        assert result['strength']['planned'] == 1
        assert result['strength']['skipped_dates'] == []
        assert result['strength']['pending_dates'] == ['2026-04-20']

    def test_rest_day_not_counted(self):
        plan = self._plan({
            '2026-04-13': {'planned': {'type': 'rest', 'duration_mins': 0}},
            '2026-04-14': {'planned': {'type': 'rest_day'}},
        })
        result = _summarize_plan_adherence_by_pillar(plan, [], self.TODAY)
        assert result['strength']['planned'] == 0
        assert result['mobility']['planned'] == 0

    def test_mobility_and_long_effort_tracked(self):
        plan = self._plan({
            '2026-04-13': {'planned': {'type': 'yoga', 'duration_mins': 45}},  # mobility
            '2026-04-15': {'planned': {'type': 'cycling', 'duration_mins': 150}},  # long_effort
        })
        result = _summarize_plan_adherence_by_pillar(plan, [], self.TODAY)
        assert result['mobility']['planned'] == 1
        assert result['mobility']['skipped_dates'] == ['2026-04-13']
        assert result['long_effort']['planned'] == 1
        assert result['long_effort']['skipped_dates'] == ['2026-04-15']


# ---------------------------------------------------------------------------
# _compute_coaching_score
# ---------------------------------------------------------------------------

class TestComputeCoachingScore:
    TODAY = date(2026, 4, 19)

    def _empty_history(self):
        return {'daily_loads': {}, 'snapshots': []}

    def test_returns_expected_shape_on_empty_data(self):
        result = _compute_coaching_score(
            fitness_history=self._empty_history(),
            athlete={'injury_history': []},
            training_config={},
            coaching_log={},
            today=self.TODAY,
        )
        assert 'overall_score' in result
        assert result['trend'] == 'stable'
        assert set(result['components'].keys()) == {
            'progress', 'health', 'achievability', 'adaptation'
        }
        for comp in result['components'].values():
            assert 'score' in comp and 'weight' in comp and 'data' in comp

    def test_active_injury_penalises_health(self):
        result = _compute_coaching_score(
            fitness_history=self._empty_history(),
            athlete={
                'injury_history': [
                    {'status': 'active', 'type': 'knee strain',
                     'restricted_activities': ['running']},
                ],
            },
            training_config={},
            coaching_log={},
            today=self.TODAY,
        )
        assert result['components']['health']['data']['injuries_active'] == 1
        assert result['components']['health']['data']['restricted_activities'] == ['running']
        assert result['components']['health']['score'] <= 70  # 90 - 20

    def test_adaptation_score_from_log_richness(self):
        responses_5 = [{'response': 'positive', 'pattern': f'p{i}'} for i in range(5)]
        result = _compute_coaching_score(
            fitness_history=self._empty_history(),
            athlete={'injury_history': []},
            training_config={},
            coaching_log={'athlete_responses': responses_5},
            today=self.TODAY,
        )
        assert result['components']['adaptation']['score'] == 65
        assert result['components']['adaptation']['data']['patterns_identified'] == 5
        assert result['components']['adaptation']['data']['positive_responses'] == 5

    def test_achievability_from_daily_loads_activities(self):
        # Seed one strength session per week for the last 4 weeks
        daily_loads = {}
        for week in range(4):
            d = (self.TODAY - timedelta(days=week * 7 + 1)).isoformat()
            daily_loads[d] = {
                'total': 40.0,
                'by_sport': {'strength': 40.0},
                'activities': [
                    {'date': d, 'type': 'strength_training', 'duration_mins': 45},
                ],
            }
        result = _compute_coaching_score(
            fitness_history={'daily_loads': daily_loads, 'snapshots': []},
            athlete={'injury_history': []},
            training_config={},
            coaching_log={},
            today=self.TODAY,
        )
        # Compliance reconstructed from daily_loads — compliance_rate is set
        assert result['components']['achievability']['data']['compliance_rate'] is not None

    def test_pure_no_garmin_no_disk(self, monkeypatch):
        """Guard against regression: no garmin_api_call invocation inside the helper."""
        from coach.tools import coaching_tools
        called = []
        monkeypatch.setattr(
            coaching_tools, 'garmin_api_call',
            lambda *a, **kw: called.append((a, kw)) or [],
        )
        _compute_coaching_score(
            fitness_history=self._empty_history(),
            athlete={'injury_history': []},
            training_config={},
            coaching_log={},
            today=self.TODAY,
        )
        assert called == []
