"""Tests for tools/athlete_tools.py — profile updates, threshold pace, FTP, methodology."""
import json
import pytest

import planner
from tools.athlete_tools import (
    update_athlete,
    set_threshold_pace,
    set_ftp,
    update_methodology,
)


@pytest.fixture
def athlete_dir(data_dir, monkeypatch):
    """Redirect DATA_DIR and seed athlete.json."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    athlete = {
        'personal': {'name': 'Test Athlete', 'age': 30, 'max_hr': 190, 'weight_kg': 75},
        'injury_history': [],
        'life_constraints': {},
        'preferences': {},
    }
    (data_dir / 'athlete.json').write_text(json.dumps(athlete))
    return data_dir


@pytest.fixture
def methodology_dir(athlete_dir):
    """Also seed methodology.json."""
    methodology = {
        'pillars': {'strength_sessions_per_week': 2},
        'safety_constraints': {'max_consecutive_hard_days': 2},
        'race_templates': {
            'multi_day_mtb': {'description': 'MTB stage race', 'key_sessions': []},
        },
    }
    (athlete_dir / 'methodology.json').write_text(json.dumps(methodology))
    return athlete_dir


# ---------------------------------------------------------------------------
# update_athlete
# ---------------------------------------------------------------------------

class TestUpdateAthlete:
    def test_update_personal_preserves_other_fields(self, athlete_dir):
        result = json.loads(update_athlete('personal', '{"max_hr": 185}'))

        assert result['status'] == 'success'
        assert result['updated']['max_hr'] == 185
        athlete = json.loads((athlete_dir / 'athlete.json').read_text())
        assert athlete['personal']['name'] == 'Test Athlete'  # Preserved

    def test_training_pillars_gets_timestamp(self, athlete_dir):
        pillars_data = json.dumps({
            'based_on_persona': 'endurance_athlete',
            'pillars': [{'name': 'endurance', 'target_hours_per_week': 5}],
        })
        result = json.loads(update_athlete('training_pillars', pillars_data))

        assert result['status'] == 'success'
        assert 'last_updated' in result['updated']

    def test_invalid_section(self, athlete_dir):
        result = json.loads(update_athlete('nonexistent_section', '{}'))
        assert 'error' in result
        assert 'Unknown section' in result['error']

    def test_invalid_json(self, athlete_dir):
        result = json.loads(update_athlete('personal', '{bad json}'))
        assert 'error' in result
        assert 'Invalid JSON' in result['error']

    def test_add_injury_defaults_to_active(self, athlete_dir):
        injury_data = json.dumps({
            'date': '2026-01-15',
            'type': 'knee',
            'description': 'Runner knee pain',
        })
        result = json.loads(update_athlete('add_injury', injury_data))

        assert result['status'] == 'success'
        athlete = json.loads((athlete_dir / 'athlete.json').read_text())
        assert athlete['injury_history'][0]['status'] == 'active'

    def test_coaching_notes_must_be_string(self, athlete_dir):
        result = json.loads(update_athlete('coaching_notes', '{"not": "a string"}'))
        assert 'error' in result
        assert 'string' in result['error']


# ---------------------------------------------------------------------------
# set_threshold_pace
# ---------------------------------------------------------------------------

class TestSetThresholdPace:
    def test_direct_pace(self, athlete_dir):
        result = json.loads(set_threshold_pace(pace='5:30'))

        assert result['status'] == 'success'
        assert result['threshold_sec_per_km'] == 330  # 5*60 + 30

    def test_from_30min_time_trial(self, athlete_dir):
        result = json.loads(set_threshold_pace(time_trial_mins=30, time_trial_distance_km=6.0))

        assert result['status'] == 'success'
        # 30 min / 6 km = 300 sec/km, adjusted ×1.05 for ≤35-min TT = 315
        assert result['threshold_sec_per_km'] == 315

    def test_40min_tt_uses_lower_adjustment(self, athlete_dir):
        """A 40-min TT uses the ×1.02 factor (35 < mins ≤ 50), not ×1.05."""
        result = json.loads(set_threshold_pace(time_trial_mins=40, time_trial_distance_km=8.0))

        assert result['status'] == 'success'
        # 40 min / 8 km = 300 sec/km, adjusted ×1.02 = 306
        assert result['threshold_sec_per_km'] == 306

    def test_60min_tt_no_adjustment(self, athlete_dir):
        """A 60-min TT uses no adjustment (>50 min ≈ true threshold)."""
        result = json.loads(set_threshold_pace(time_trial_mins=60, time_trial_distance_km=12.0))

        assert result['status'] == 'success'
        # 60 min / 12 km = 300 sec/km, no adjustment
        assert result['threshold_sec_per_km'] == 300

    def test_zone_boundaries_are_correct(self, athlete_dir):
        """Verify actual pace zone math at threshold = 300 sec/km (5:00/km)."""
        result = json.loads(set_threshold_pace(pace='5:00'))

        zones = result['pace_zones']
        assert len(zones) == 5

        # Verify the pace was persisted to athlete.json
        athlete = json.loads((athlete_dir / 'athlete.json').read_text())
        raw_zones = athlete['personal']['pace_zones']
        threshold = 300

        # Verify ALL 5 zone boundaries against expected multipliers
        assert raw_zones['z1_recovery'] == [int(threshold * 1.25), int(threshold * 1.30)]
        assert raw_zones['z2_easy'] == [int(threshold * 1.15), int(threshold * 1.24)]
        assert raw_zones['z3_tempo'] == [int(threshold * 1.05), int(threshold * 1.14)]
        assert raw_zones['z4_threshold'] == [int(threshold * 0.96), int(threshold * 1.04)]
        assert raw_zones['z5_interval'] == [int(threshold * 0.85), int(threshold * 0.95)]

        # Zones must be ordered: z5 (fastest) < z4 < z3 < z2 < z1 (slowest)
        assert raw_zones['z5_interval'][1] < raw_zones['z4_threshold'][0]
        assert raw_zones['z4_threshold'][1] < raw_zones['z3_tempo'][0]
        assert raw_zones['z3_tempo'][1] < raw_zones['z2_easy'][0]
        assert raw_zones['z2_easy'][1] < raw_zones['z1_recovery'][0]


# ---------------------------------------------------------------------------
# set_ftp
# ---------------------------------------------------------------------------

class TestSetFtp:
    def test_direct_value(self, athlete_dir):
        result = json.loads(set_ftp(ftp_watts=250))

        assert result['status'] == 'success'
        assert result['ftp'] == 250

    def test_from_20min_test(self, athlete_dir):
        result = json.loads(set_ftp(test_avg_watts=265, test_duration_mins=20))

        assert result['status'] == 'success'
        assert result['ftp'] == int(265 * 0.95)  # 251

    def test_8min_test_uses_090_factor(self, athlete_dir):
        """An 8-min test (≤10 min) uses the ×0.90 adjustment factor."""
        result = json.loads(set_ftp(test_avg_watts=300, test_duration_mins=8))

        assert result['status'] == 'success'
        assert result['ftp'] == int(300 * 0.90)  # 270

    def test_30min_test_uses_098_factor(self, athlete_dir):
        """A 30-min test (25 < mins ≤ 40) uses the ×0.98 adjustment factor."""
        result = json.loads(set_ftp(test_avg_watts=260, test_duration_mins=30))

        assert result['status'] == 'success'
        assert result['ftp'] == int(260 * 0.98)  # 254

    def test_power_zone_math(self, athlete_dir):
        """Verify actual power zone boundaries at FTP=200."""
        result = json.loads(set_ftp(ftp_watts=200))

        # Check zone count
        zones = result['power_zones']
        assert len(zones) == 7

        # Verify from persisted file (raw integer boundaries)
        athlete = json.loads((athlete_dir / 'athlete.json').read_text())
        raw_zones = athlete['personal']['power_zones']

        # Verify ALL 7 zone boundaries against expected multipliers
        assert raw_zones['z1_recovery'] == [0, int(200 * 0.55)]             # 0-110
        assert raw_zones['z2_endurance'] == [int(200 * 0.56), int(200 * 0.75)]  # 112-150
        assert raw_zones['z3_tempo'] == [int(200 * 0.76), int(200 * 0.90)]      # 152-180
        assert raw_zones['z4_threshold'] == [int(200 * 0.91), int(200 * 1.05)]  # 182-210
        assert raw_zones['z5_vo2max'] == [int(200 * 1.06), int(200 * 1.20)]     # 212-240
        assert raw_zones['z6_anaerobic'] == [int(200 * 1.21), int(200 * 1.50)]  # 242-300
        assert raw_zones['z7_neuromuscular'] == [int(200 * 1.51), None]          # 302+


# ---------------------------------------------------------------------------
# update_methodology
# ---------------------------------------------------------------------------

class TestUpdateMethodology:
    def test_update_pillars(self, methodology_dir):
        result = json.loads(update_methodology('pillars', '{"strength_sessions_per_week": 3}'))

        assert result['status'] == 'success'
        assert result['updated']['strength_sessions_per_week'] == 3

    def test_add_race_template(self, methodology_dir):
        template = json.dumps({
            'name': 'gravel',
            'description': 'Gravel race',
            'key_sessions': [],
        })
        result = json.loads(update_methodology('add_race_template', template))

        assert result['status'] == 'success'
        methodology = json.loads((methodology_dir / 'methodology.json').read_text())
        assert 'gravel' in methodology['race_templates']

    def test_invalid_section(self, methodology_dir):
        result = json.loads(update_methodology('nonexistent', '{}'))
        assert 'error' in result

    def test_update_safety_constraints(self, methodology_dir):
        result = json.loads(update_methodology('safety_constraints', '{"max_consecutive_hard_days": 3}'))

        assert result['status'] == 'success'
        assert result['updated']['max_consecutive_hard_days'] == 3
