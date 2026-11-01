"""Targets are alerts (2026-10-30).

The watch sounds whenever the athlete is outside a step's target, in both
directions. So: easy and recovery render as ceilings above the athlete's
aerobic floor (resting HR + 30 % of heart-rate reserve), the other words and
hr_target stay bands, hr_max is an explicit ceiling, only an interval inherits
the session intensity, the warm-up and cool-down of a plain session carry no
target, and an HR band on a short timed step is a warning (HR lags).
"""
import json

import pytest

import coach.fitness as fitness_mod
import coach.workout_builder as wb
from coach.authoring import (
    AEROBIC_FLOOR_FALLBACK_BPM,
    HR_LAG_MIN_STEP_SECS,
    Phase,
    contract_document,
    validate_session,
)
from coach.workout_builder import (
    TARGET_HR,
    TARGET_NONE,
    TARGET_PACE,
    TARGET_POWER,
    build_cycling_workout,
    build_running_workout,
    build_workout_report,
    get_aerobic_floor_bpm,
    get_hr_target_for_intensity,
    get_pace_target_for_intensity,
)

HR_ZONES = {'z1_recovery': [119, 133], 'z2_aerobic': [134, 148], 'z3_tempo': [149, 163],
            'z4_threshold': [164, 179], 'z5_max': [180, 195]}
FLOOR = 88  # resting 42 + 30 % of (195 - 42)


def _profile(data_dir, monkeypatch, **personal):
    for mod in (wb, fitness_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    (data_dir / 'athlete.json').write_text(json.dumps({'personal': personal}), encoding='utf-8')
    return data_dir


def _run_steps(session):
    return build_running_workout(session, '2026-01-01').workoutSegments[0].workoutSteps


def _lag_warnings(report):
    return [w for w in report['warnings'] if 'HR lags' in w['message']]


class TestAerobicFloor:
    def test_floor_is_resting_plus_30_pct_of_reserve(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        assert get_aerobic_floor_bpm() == FLOOR

    def test_profile_share_overrides_the_default(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, aerobic_floor_pct_hrr=40)
        assert get_aerobic_floor_bpm() == 103

    def test_max_hr_falls_back_to_the_top_of_z5(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, hr_zones=HR_ZONES)
        assert get_aerobic_floor_bpm() == FLOOR

    def test_no_resting_hr_means_no_floor(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, max_hr=195, hr_zones=HR_ZONES)
        assert get_aerobic_floor_bpm() is None


class TestCeilingWords:
    def test_easy_and_recovery_are_ceilings_above_the_floor(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        assert get_hr_target_for_intensity('recovery') == (FLOOR, 133)
        assert get_hr_target_for_intensity('easy') == (FLOOR, 148)

    def test_the_other_words_stay_bands(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        assert get_hr_target_for_intensity('tempo') == (149, 163)
        assert get_hr_target_for_intensity('threshold') == (164, 179)
        assert get_hr_target_for_intensity('vo2') == (180, 195)

    def test_without_a_profile_floor_the_fallback_is_used_and_flagged(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, max_hr=195, hr_zones=HR_ZONES)
        assert get_hr_target_for_intensity('easy') == (AEROBIC_FLOOR_FALLBACK_BPM, 148)
        report = build_workout_report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy'}, '2026-01-01')
        assert report['ok']
        assert any('resting_hr' in w['message'] for w in report['warnings'])

    def test_the_flag_is_silent_when_the_profile_has_a_floor(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        report = build_workout_report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy'}, '2026-01-01')
        assert not any('resting_hr' in w['message'] for w in report['warnings'])

    def test_pace_ceiling_slow_bound_is_twice_the_threshold_pace(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, threshold_pace_sec_per_km=300)
        slow, fast = get_pace_target_for_intensity('easy')
        assert slow == pytest.approx(1000 / 600)  # 10:00/km: slower than this is a walk
        assert fast == pytest.approx(1000 / 345)  # top of z2_easy (115 % of threshold)

    def test_pace_band_words_are_unchanged(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, threshold_pace_sec_per_km=300)
        slow, fast = get_pace_target_for_intensity('tempo')
        # the zone table truncates: int(300 * 1.14) is 341, int(300 * 1.05) is 315
        assert (slow, fast) == pytest.approx((1000 / int(300 * 1.14), 1000 / int(300 * 1.05)))


class TestOnlyAnIntervalInherits:
    def test_untargeted_warmup_walk_and_cooldown_stay_untargeted(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        warmup, repeat, cooldown = _run_steps({
            'type': 'running', 'duration_mins': 44, 'intensity': 'easy', 'structure': [
                {'phase': 'warmup', 'duration_mins': 5, 'notes': 'Walk'},
                {'phase': 'repeat', 'iterations': 3, 'steps': [
                    {'phase': 'interval', 'duration_mins': 10},
                    {'phase': 'recovery', 'duration_mins': 2, 'notes': 'Walk'}]},
                {'phase': 'cooldown', 'duration_mins': 3}]})
        run, walk = repeat.workoutSteps
        assert warmup.targetType == TARGET_NONE
        assert walk.targetType == TARGET_NONE
        assert cooldown.targetType == TARGET_NONE
        assert run.targetType == TARGET_HR  # the interval inherits easy, as a ceiling
        assert (run.targetValueOne, run.targetValueTwo) == (FLOOR, 148)

    def test_a_walk_that_states_recovery_gets_the_keep_walking_band(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        (walk,) = _run_steps({'type': 'running', 'duration_mins': 2, 'intensity': 'easy', 'structure': [
            {'phase': 'recovery', 'duration_mins': 2, 'intensity': 'recovery', 'notes': 'Walk'}]})
        assert walk.targetType == TARGET_HR
        assert (walk.targetValueOne, walk.targetValueTwo) == (FLOOR, 133)

    def test_a_rest_never_inherits_a_band(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        work, rest = _run_steps({'type': 'running', 'duration_mins': 12, 'intensity': 'tempo', 'structure': [
            {'phase': 'interval', 'duration_mins': 10},
            {'phase': 'rest', 'duration_mins': 2}]})
        assert (work.targetValueOne, work.targetValueTwo) == (149, 163)
        assert rest.targetType == TARGET_NONE


class TestHrMax:
    def test_hr_max_is_a_ceiling_above_the_floor(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        (step,) = _run_steps({'type': 'running', 'duration_mins': 10, 'structure': [
            {'phase': 'interval', 'duration_mins': 10, 'hr_max': 145}]})
        assert step.targetType == TARGET_HR
        assert (step.targetValueOne, step.targetValueTwo) == (FLOOR, 145)

    def test_hr_max_with_hr_target_is_two_targets(self):
        report = validate_session({'type': 'running', 'duration_mins': 10, 'structure': [
            {'phase': 'interval', 'duration_mins': 10, 'hr_max': 145, 'hr_target': [120, 145]}]})
        assert not report.ok
        assert 'ONE target' in report.errors[0]['message']

    def test_hr_max_is_plausibility_checked(self):
        with pytest.raises(ValueError):
            Phase(phase='interval', duration_mins=10, hr_max=20)


class TestHrLagWarning:
    def _report(self, structure, intensity='easy'):
        return build_workout_report({'type': 'running', 'duration_mins': 30, 'intensity': intensity,
                                     'structure': structure}, '2026-01-01')

    def test_an_hr_band_on_a_short_step_warns(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        report = self._report([{'phase': 'interval', 'duration_secs': 60, 'hr_target': [150, 165]}])
        assert report['ok']
        assert len(_lag_warnings(report)) == 1
        assert 'hr_max' in _lag_warnings(report)[0]['message']

    def test_an_inherited_band_on_a_short_interval_warns(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        report = self._report([{'phase': 'interval', 'duration_secs': 90}], intensity='threshold')
        assert len(_lag_warnings(report)) == 1

    def test_strides_on_an_hr_only_profile_warn(self, data_dir, monkeypatch):
        """The old skill example: 20 s max_effort = a z5 band the heart never reaches."""
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        report = self._report([{'phase': 'repeat', 'iterations': 6, 'steps': [
            {'phase': 'interval', 'duration_secs': 20, 'intensity': 'max_effort'},
            {'phase': 'recovery', 'duration_secs': 60}]}])
        assert len(_lag_warnings(report)) == 1

    def test_ceilings_pace_long_and_distance_steps_do_not_warn(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        report = self._report([
            {'phase': 'interval', 'duration_secs': 60, 'intensity': 'easy'},       # ceiling
            {'phase': 'interval', 'duration_secs': 60, 'hr_max': 150},             # ceiling
            {'phase': 'interval', 'duration_secs': 20, 'pace_sec_per_km': [240, 230]},  # pace
            {'phase': 'interval', 'duration_secs': HR_LAG_MIN_STEP_SECS, 'hr_target': [150, 165]},
            {'phase': 'interval', 'distance_m': 400, 'hr_target': [150, 165]},     # not timed
            {'phase': 'interval', 'duration_secs': 20, 'intensity': 'none', 'notes': 'Stride'},
        ])
        assert report['ok']
        assert _lag_warnings(report) == []


class TestPlainSessions:
    def test_plain_run_warmup_and_cooldown_carry_no_target(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        steps = _run_steps({'type': 'running', 'duration_mins': 40, 'intensity': 'easy'})
        assert [s.targetType for s in steps] == [TARGET_NONE, TARGET_HR, TARGET_NONE]
        assert (steps[1].targetValueOne, steps[1].targetValueTwo) == (FLOOR, 148)

    def test_plain_tempo_run_main_block_is_a_band(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        steps = _run_steps({'type': 'running', 'duration_mins': 40, 'intensity': 'tempo'})
        assert (steps[1].targetValueOne, steps[1].targetValueTwo) == (149, 163)

    def test_plain_run_with_a_threshold_pace_keeps_pace_targets(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES,
                 threshold_pace_sec_per_km=300)
        steps = _run_steps({'type': 'running', 'duration_mins': 40, 'intensity': 'easy'})
        assert [s.targetType for s in steps] == [TARGET_NONE, TARGET_PACE, TARGET_NONE]

    def test_plain_outdoor_ride_is_a_ceiling_with_an_under_cue(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        workout = build_cycling_workout({'type': 'cycling', 'duration_mins': 90, 'intensity': 'easy'}, '2026-01-01')
        (step,) = workout.workoutSegments[0].workoutSteps
        assert (step.targetValueOne, step.targetValueTwo) == (FLOOR, 148)
        assert step.description == 'HR under 148 bpm'

    def test_plain_outdoor_tempo_ride_keeps_the_band_cue(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
        workout = build_cycling_workout({'type': 'cycling', 'duration_mins': 60, 'intensity': 'tempo'}, '2026-01-01')
        (step,) = workout.workoutSegments[0].workoutSteps
        assert step.description == 'HR 149-163 bpm'

    def test_plain_indoor_ride_warmup_and_cooldown_carry_no_target(self, data_dir, monkeypatch):
        _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES,
                 power_zones={'z1_recovery': [0, 118], 'z2_endurance': [120, 162]})
        workout = build_cycling_workout({'type': 'indoor_cycling', 'duration_mins': 60, 'intensity': 'easy'}, '2026-01-01')
        steps = workout.workoutSegments[0].workoutSteps
        assert [s.targetType for s in steps] == [TARGET_NONE, TARGET_POWER, TARGET_NONE]
        assert (steps[1].targetValueOne, steps[1].targetValueTwo) == (120, 162)  # power stays a band


def test_the_run_walk_that_beeped_as_the_watch_now_sees_it(data_dir, monkeypatch):
    """The 2026-10-29 session: 'HR too low' on every walk, and on the first
    minute of every run. Now the warm-up is silent, the runs alert only above
    145, the walks only when the athlete stops."""
    _profile(data_dir, monkeypatch, resting_hr=42, max_hr=195, hr_zones=HR_ZONES)
    report = build_workout_report({
        'type': 'running', 'name': 'Run/walk 3x(10 run/2 walk)', 'duration_mins': 44, 'intensity': 'easy',
        'structure': [
            {'phase': 'warmup', 'duration_mins': 5, 'notes': 'Walk'},
            {'phase': 'repeat', 'iterations': 3, 'steps': [
                {'phase': 'interval', 'duration_mins': 10, 'hr_max': 145, 'notes': 'Run easy, HR under 145'},
                {'phase': 'recovery', 'duration_mins': 2, 'intensity': 'recovery', 'notes': 'Walk'}]},
            {'phase': 'cooldown', 'duration_mins': 3, 'intensity': 'recovery', 'notes': 'Walk'}]}, '2026-10-30')
    assert report['ok'], report['errors']
    assert report['steps'] == [
        '1. Warm-up 5:00 — "Walk"',
        '2. 3 x',
        '   2.1. Interval 10:00 @ HR 88-145 bpm — "Run easy, HR under 145"',
        '   2.2. Recovery 2:00 @ HR 88-133 bpm — "Walk"',
        '3. Cool-down 3:00 @ HR 88-133 bpm — "Walk"',
    ]
    assert _lag_warnings(report) == []


def test_contract_document_states_the_alert_rules():
    text = contract_document()
    for phrase in ('ceiling', 'aerobic floor', 'hr_max', 'inherits the session intensity', 'HR lags'):
        assert phrase in text, phrase
