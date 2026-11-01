"""The target rules are the athlete's, not the code's.

methodology.targets and personal.aerobic_floor_pct_hrr override the code
defaults; the aerobic floor is checked against the athlete's own recorded
walking HR; get_athlete shows the floor beside that evidence.
"""
import json
from datetime import date, timedelta

import pytest

import coach.fitness as fitness_mod
import coach.planner as planner
import coach.tools.fitness_tools as fitness_tools
import coach.workout_builder as wb
from coach.fitness import walking_hr_evidence
from coach.workout_builder import (
    TARGET_RULE_DEFAULTS,
    build_workout_report,
    get_aerobic_floor_bpm,
    get_hr_target_for_intensity,
    get_pace_target_for_intensity,
    target_rules,
)

HR_ZONES = {'z1_recovery': [119, 133], 'z2_aerobic': [134, 148], 'z3_tempo': [149, 163],
            'z4_threshold': [164, 179], 'z5_max': [180, 195]}
TODAY = date.today()


@pytest.fixture
def env(data_dir, monkeypatch):
    for mod in (wb, fitness_mod, planner):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _profile(data_dir, **personal):
    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'T', 'resting_hr': 42, 'max_hr': 195, 'hr_zones': HR_ZONES, **personal},
        'injury_history': [], 'life_constraints': {}, 'preferences': {}}), encoding='utf-8')


def _methodology(data_dir, **targets):
    (data_dir / 'methodology.json').write_text(json.dumps({'targets': targets}), encoding='utf-8')


def _walks(*walks):
    """daily_loads with one walking activity per (days_ago, avg_hr, duration_mins)."""
    loads = {}
    for days_ago, hr, mins in walks:
        day = (TODAY - timedelta(days=days_ago)).isoformat()
        loads.setdefault(day, {'total': 0, 'by_sport': {}, 'activities': []})['activities'].append(
            {'type': 'walking', 'sport': 'walking', 'avg_hr': hr, 'duration_mins': mins, 'load': 10})
    return loads


def _history(data_dir, daily_loads):
    (data_dir / 'fitness_history.json').write_text(json.dumps({
        'daily_loads': daily_loads, 'snapshots': [], 'sleep_history': [], 'readiness_history': []}),
        encoding='utf-8')


class TestMethodologyTargets:
    def test_defaults_without_a_methodology_block(self, env):
        _profile(env)
        assert target_rules() == TARGET_RULE_DEFAULTS
        assert get_aerobic_floor_bpm() == 88

    def test_methodology_share_moves_the_floor(self, env):
        _profile(env)
        _methodology(env, aerobic_floor_pct_hrr=40)
        assert get_aerobic_floor_bpm() == 103
        assert get_hr_target_for_intensity('recovery') == (103, 133)

    def test_personal_share_wins_over_methodology(self, env):
        _profile(env, aerobic_floor_pct_hrr=20)
        _methodology(env, aerobic_floor_pct_hrr=40)
        assert get_aerobic_floor_bpm() == 73   # 42 + 20 % of 153

    def test_ceiling_words_are_the_athletes(self, env):
        _profile(env)
        _methodology(env, ceiling_intensities=['recovery'])
        assert get_hr_target_for_intensity('easy') == (134, 148)      # a band again
        assert get_hr_target_for_intensity('recovery') == (88, 133)   # still a ceiling

    def test_lag_threshold_and_pace_factor(self, env):
        _profile(env, threshold_pace_sec_per_km=300)
        _methodology(env, hr_lag_min_step_secs=60, pace_floor_factor=1.5)
        report = build_workout_report({'type': 'running', 'duration_mins': 10, 'intensity': 'tempo', 'structure': [
            {'phase': 'interval', 'duration_secs': 90, 'hr_target': [150, 165]}]}, '2026-01-01')
        assert not any('HR lags' in w['message'] for w in report['warnings'])
        slow, _fast = get_pace_target_for_intensity('easy')
        assert slow == pytest.approx(1000 / 450)

    def test_bad_values_are_ignored(self, env):
        _profile(env)
        _methodology(env, aerobic_floor_pct_hrr=True, hr_lag_min_step_secs=-5, pace_floor_factor='x',
                     ceiling_intensities=['easy', 'gentle'])
        assert target_rules() == TARGET_RULE_DEFAULTS


class TestWalkingEvidence:
    def test_median_of_long_enough_walks_in_the_window(self):
        loads = _walks((2, 100, 25), (10, 95, 30), (30, 91, 23), (40, 129, 9), (200, 70, 60))
        evidence = walking_hr_evidence(loads)
        assert evidence['median_bpm'] == 95 and evidence['count'] == 3   # 9-min walk and 200-day-old walk excluded
        assert evidence['window_days'] == 120 and evidence['latest_date'] == (TODAY - timedelta(days=2)).isoformat()

    def test_no_walks_means_no_median(self):
        assert walking_hr_evidence({})['median_bpm'] is None
        assert walking_hr_evidence({'not-a-date': {}})['median_bpm'] is None

    def test_preview_warns_when_the_floor_sits_above_the_athletes_walks(self, env):
        _profile(env, aerobic_floor_pct_hrr=60)   # 42 + 60 % of 153 = 134
        _history(env, _walks((2, 100, 25), (10, 95, 30), (30, 91, 23)))
        report = build_workout_report({'type': 'running', 'duration_mins': 30, 'intensity': 'easy', 'structure': [
            {'phase': 'recovery', 'duration_mins': 2, 'intensity': 'recovery', 'notes': 'Walk'}]}, '2026-01-01')
        assert report['ok']
        assert report['steps'][0].startswith('1. Recovery 2:00 @ HR 133-133 bpm')   # the ceiling collapsed
        messages = [w['message'] for w in report['warnings']]
        assert any('walking HR' in m and 'median 95 bpm' in m for m in messages), messages
        assert any('has no room' in m for m in messages), messages

    def test_no_warning_when_the_floor_is_below_the_walks(self, env):
        _profile(env)
        _history(env, _walks((2, 100, 25), (10, 95, 30), (30, 91, 23)))
        report = build_workout_report({'type': 'running', 'duration_mins': 30, 'intensity': 'easy', 'structure': [
            {'phase': 'recovery', 'duration_mins': 2, 'intensity': 'recovery', 'notes': 'Walk'}]}, '2026-01-01')
        assert not any('walking HR' in w['message'] for w in report['warnings'])


class TestGetAthleteDerived:
    def test_derived_block_shows_floor_and_walking_evidence(self, env):
        _profile(env)
        _history(env, _walks((2, 100, 25), (10, 95, 30), (30, 91, 23)))
        athlete = json.loads(fitness_tools.get_athlete())
        derived = athlete['derived']
        assert derived['aerobic_floor_bpm'] == 88
        assert derived['aerobic_floor_pct_hrr'] == 30
        assert derived['walking_hr_median_bpm'] == 95 and derived['walking_sessions_counted'] == 3
        assert derived['target_rules'] == TARGET_RULE_DEFAULTS
        assert 'aerobic_floor_pct_hrr' in derived['note']
