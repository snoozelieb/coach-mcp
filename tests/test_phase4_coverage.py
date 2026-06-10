"""Phase 4.5 coverage-gap tests: previously untested tools, in risk order.

Covers analyze_ftp_test (FTP protocol parsing + estimate), refresh_fitness_
history (the backfill path), get_methodology / get_onboarding_guide (read
surfaces), and the sandbox guard itself. All Garmin traffic goes through the
canonical FakeGarminClient; all file I/O lands in the autouse sandbox
DATA_DIR (see conftest.sandbox_data_dir).
"""
import json
from datetime import date

import coach.config as config
import coach.fitness as fitness_mod
import coach.planner as planner

from coach.tools.athlete_tools import analyze_ftp_test, get_methodology
from coach.tools.fitness_tools import refresh_fitness_history, get_onboarding_guide
from conftest import (
    LIVE_DATA_DIR,
    FakeGarminClient,
    make_garmin_activity,
    patch_garmin_everywhere,
)

TODAY = date.today()


# ---------------------------------------------------------------------------
# Sandbox guard self-checks (Phase 4.4)
# ---------------------------------------------------------------------------

class TestSandboxGuard:
    def test_data_dir_redirected_away_from_live(self, sandbox_data_dir):
        """Every coach module that binds DATA_DIR points at the sandbox."""
        for mod in (config, planner, fitness_mod):
            assert mod.DATA_DIR == sandbox_data_dir
            assert mod.DATA_DIR != LIVE_DATA_DIR

    def test_unpatched_writes_land_in_sandbox(self, sandbox_data_dir):
        """A test that 'forgets' to patch DATA_DIR still cannot touch live
        data — the default write path resolves to the sandbox."""
        planner.save_json_file('athlete.json', {'personal': {'name': 'X'}})

        assert (sandbox_data_dir / 'athlete.json').exists()
        assert not json.loads(
            (sandbox_data_dir / 'athlete.json').read_text(encoding='utf-8')
        ).get('injury_history')


# ---------------------------------------------------------------------------
# analyze_ftp_test
# ---------------------------------------------------------------------------

def _lap(intensity, mins, avg, mx, mn):
    return {'intensityType': intensity, 'duration': mins * 60,
            'averagePower': avg, 'maxPower': mx, 'minPower': mn,
            'normalizedPower': avg, 'averageHR': 165, 'maxHR': 182,
            'averageBikeCadence': 92}


def _ftp_splits(activity_id, test_min_power=235):
    """Standard FTP protocol: warmup, blowout, recovery, 2x10' test, cooldown."""
    return {'activityId': activity_id, 'lapDTOs': [
        _lap('WARMUP', 10, 150, 180, 120),
        _lap('ACTIVE', 5, 320, 380, 250),                 # blowout
        _lap('RECOVERY', 5, 110, 130, 90),
        _lap('ACTIVE', 10, 250, 265, test_min_power),     # test half 1
        _lap('ACTIVE', 10, 250, 262, test_min_power + 3), # test half 2
        _lap('COOLDOWN', 8, 120, 140, 100),
    ]}


def _ftp_client(splits=_ftp_splits):
    return FakeGarminClient(
        activities=[
            make_garmin_activity(
                TODAY, 'cycling', name='FTP Test 20min',
                duration_secs=48 * 60, avgPower=210, maxPower=380,
                normPower=225),
        ],
        overrides={'get_activity_splits': splits},
    )


class TestAnalyzeFtpTest:
    def test_clean_test_full_analysis(self, monkeypatch):
        patch_garmin_everywhere(monkeypatch, _ftp_client())

        result = json.loads(analyze_ftp_test())

        assert result['status'] == 'success'
        assert result['test_name'] == 'FTP Test 20min'

        analysis = result['test_analysis']
        # The two post-recovery ACTIVE laps form the 20-minute test portion
        assert analysis['test_duration_mins'] == 20
        assert analysis['test_avg_power'] == 250
        assert analysis['test_completed'] is True
        assert analysis['completion_pct'] == 100

        pacing = analysis['pacing']
        assert pacing['surge_detected'] is False
        assert pacing['crash_detected'] is False
        assert 'Excellent pacing' in pacing['pacing_verdict']

        # Blowout = first ACTIVE lap before the recovery lap
        assert analysis['blowout_phase']['avg_power'] == 320
        assert analysis['blowout_phase']['effective'] is True

        # 250W x 0.95 = 237 FTP, clean completion -> high confidence
        estimate = result['ftp_estimate']
        assert estimate['method'] == '20min_test'
        assert estimate['adjustment_factor'] == 0.95
        assert estimate['estimated_ftp'] == 237
        assert estimate['confidence'] == 'high'

        rec = result['coach_recommendation']
        assert rec['suggested_ftp'] == 237
        assert rec['retest_in_weeks'] == 8

    def test_crash_lowers_confidence_and_ftp(self, monkeypatch):
        """Power collapsing below 100W mid-test: low confidence, extra
        conservative suggestion, sooner retest."""
        patch_garmin_everywhere(
            monkeypatch,
            _ftp_client(lambda aid: _ftp_splits(aid, test_min_power=60)))

        result = json.loads(analyze_ftp_test())

        analysis = result['test_analysis']
        assert analysis['pacing']['crash_detected'] is True
        assert result['ftp_estimate']['confidence'] == 'low'
        estimated = result['ftp_estimate']['estimated_ftp']
        rec = result['coach_recommendation']
        assert rec['suggested_ftp'] == int(estimated * 0.95)
        assert rec['retest_in_weeks'] == 4
        assert 'conservative' in rec['rationale'].lower()

    def test_garmin_20min_power_preferred(self, monkeypatch):
        """When Garmin already computed max20MinPower, that wins over the
        lap-derived average."""
        client = FakeGarminClient(
            activities=[
                make_garmin_activity(
                    TODAY, 'cycling', name='Threshold test',
                    duration_secs=48 * 60, avgPower=210, max20MinPower=260),
            ],
            overrides={'get_activity_splits': _ftp_splits},
        )
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(analyze_ftp_test())

        estimate = result['ftp_estimate']
        assert estimate['method'] == '20min_garmin'
        assert estimate['raw_power'] == 260
        assert estimate['estimated_ftp'] == int(260 * 0.95)

    def test_no_ftp_test_found(self, monkeypatch):
        client = FakeGarminClient(activities=[
            make_garmin_activity(TODAY, 'running', name='Morning Run'),
        ])
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(analyze_ftp_test())

        assert result['status'] == 'not_found'
        assert 'No FTP tests found' in result['error']

    def test_explicit_activity_id_not_in_window(self, monkeypatch):
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(analyze_ftp_test('999'))

        assert result['status'] == 'not_found'


# ---------------------------------------------------------------------------
# refresh_fitness_history
# ---------------------------------------------------------------------------

class TestRefreshFitnessHistory:
    def test_backfills_history_and_returns_metrics(
            self, sandbox_data_dir, monkeypatch):
        client = FakeGarminClient()  # 3 default activities ending today
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(refresh_fitness_history(days=30))

        assert result['status'] == 'success'
        assert result['activities_processed'] == 3
        assert result['days_with_load'] == 3
        assert set(result['current_metrics']) == {
            'ctl', 'atl', 'tsb', 'acwr', 'acwr_status'}

        # Persisted into the sandboxed fitness_history.json
        history = json.loads(
            (sandbox_data_dir / 'fitness_history.json').read_text(
                encoding='utf-8'))
        assert len(history['daily_loads']) == 3
        assert TODAY.isoformat() in history['daily_loads']
        today_entry = history['daily_loads'][TODAY.isoformat()]
        assert today_entry['total'] > 0
        assert today_entry['activities']

    def test_no_activities(self, monkeypatch):
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(refresh_fitness_history(days=30))

        assert result['status'] == 'no_activities'

    def test_garmin_failure_is_clean_error(self, monkeypatch):
        client = FakeGarminClient(
            overrides={'get_activities_by_date': Exception('api down')})
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(refresh_fitness_history(days=30))

        assert result['error'] == 'api down'


# ---------------------------------------------------------------------------
# get_methodology / get_onboarding_guide
# ---------------------------------------------------------------------------

METHODOLOGY = {
    'pillars': {'strength_sessions_per_week': 2,
                'mobility_minutes_per_week': 90},
    'safety_constraints': {'max_consecutive_hard_days': 2,
                           'rest_days_after_race': 2},
    'race_templates': {'gravel': {'description': 'long steady efforts'}},
    'personas': {
        '_comment': 'internal note — must not surface as a persona',
        'endurance_athlete': {
            'description': 'Aerobic engine first',
            'typical_weekly_hours': '6-10',
            'key_focus': 'volume',
            'suggested_pillars': ['endurance', 'strength'],
        },
        'multi_sport': {
            'description': 'Several sports in parallel',
            'key_focus': 'balance',
        },
    },
}


class TestGetMethodology:
    def test_returns_methodology_verbatim(self, sandbox_data_dir):
        (sandbox_data_dir / 'methodology.json').write_text(
            json.dumps(METHODOLOGY))

        result = json.loads(get_methodology())

        assert result['pillars'] == METHODOLOGY['pillars']
        assert result['safety_constraints'] == METHODOLOGY['safety_constraints']
        assert 'gravel' in result['race_templates']

    def test_missing_file_returns_empty_not_error(self, sandbox_data_dir):
        result = json.loads(get_methodology())

        assert result == {}


class TestGetOnboardingGuide:
    def test_personas_and_steps(self, sandbox_data_dir):
        (sandbox_data_dir / 'methodology.json').write_text(
            json.dumps(METHODOLOGY))

        result = json.loads(get_onboarding_guide())

        personas = {p['id']: p for p in result['available_personas']}
        # Underscore-prefixed keys are internal, not personas
        assert set(personas) == {'endurance_athlete', 'multi_sport'}
        assert personas['endurance_athlete']['typical_weekly_hours'] == '6-10'
        assert personas['endurance_athlete']['suggested_pillars'] == [
            'endurance', 'strength']
        # Sparse personas get safe defaults
        assert personas['multi_sport']['typical_weekly_hours'] == 'varies'

        steps = result['onboarding_steps']
        assert [s['step'] for s in steps] == [1, 2, 3, 4, 5]
        # The guide ends with PRESCRIBE + save, matching the coaching model
        assert steps[3]['name'] == 'PRESCRIBE the plan'
        assert 'TELL them' in steps[3]['instruction']
        assert 'update_athlete' in steps[4]['instruction']
        assert result['update_example']['section'] == 'training_pillars'

    def test_no_methodology_still_returns_guide(self, sandbox_data_dir):
        result = json.loads(get_onboarding_guide())

        assert result['available_personas'] == []
        assert len(result['onboarding_steps']) == 5
