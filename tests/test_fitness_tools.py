"""Tests for tools/fitness_tools.py — refresh_athlete_baseline pipeline.

Only tests the baseline refresh (composes API calls + parsing + file write).
The thin-wrapper tools (get_training_readiness, get_fitness_status) are
covered by test_parsers.py and test_fitness.py respectively.
"""
import json
from unittest.mock import Mock, patch

from conftest import (
    SAMPLE_RUNNING_ACTIVITY,
    SAMPLE_PR_DATA,
)

# Garmin profile mock data
SAMPLE_FULL_NAME = {'firstName': 'John', 'lastName': 'Doe', 'displayName': 'johndoe'}
SAMPLE_USER_PROFILE = {'userData': {'birthDate': '1990-06-15', 'displayName': 'johndoe', 'maxHeartRate': 192}}
SAMPLE_BODY_COMP = {'totalAverage': {'weight': 75000}}


def _make_garmin_mock():
    """Create a mock Garmin client with profile API methods."""
    mock_client = Mock()
    mock_client.get_activities_by_date.return_value = [SAMPLE_RUNNING_ACTIVITY]
    mock_client.get_personal_record.return_value = SAMPLE_PR_DATA
    mock_client.get_full_name.return_value = SAMPLE_FULL_NAME
    mock_client.get_user_profile.return_value = SAMPLE_USER_PROFILE
    mock_client.get_body_composition.return_value = SAMPLE_BODY_COMP
    return mock_client


class TestRefreshAthleteBaseline:
    @patch('tools.fitness_tools.garmin_api_call')
    def test_creates_baseline_file_with_correct_structure(self, mock_api_call, tmp_path, monkeypatch):
        """The full pipeline: API → parse → calculate → write file."""
        import tools.fitness_tools as fitness_mod
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)

        mock_client = _make_garmin_mock()
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        result = json.loads(fitness_mod.refresh_athlete_baseline())

        assert result['status'] == 'success'
        assert result['activities_analyzed'] == 1
        assert result['personal_records_count'] == 3

        # Verify file was actually written with correct schema
        profile_path = tmp_path / 'athlete_baseline.json'
        assert profile_path.exists()

        profile = json.loads(profile_path.read_text())
        assert 'baseline' in profile
        assert 'personal_records' in profile
        assert 'last_refreshed' in profile
        assert profile['baseline']['total_activities'] == 1

    @patch('tools.fitness_tools.garmin_api_call')
    def test_api_error_returns_error_json(self, mock_api_call):
        from tools.fitness_tools import refresh_athlete_baseline

        mock_api_call.side_effect = Exception("Auth failed")
        result = json.loads(refresh_athlete_baseline())

        assert 'error' in result
        assert 'Auth failed' in result['error']

    @patch('tools.fitness_tools.garmin_api_call')
    def test_garmin_profile_saved_to_baseline(self, mock_api_call, tmp_path, monkeypatch):
        """Garmin profile (name, weight, age) saved under garmin_profile key."""
        import tools.fitness_tools as fitness_mod
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)

        mock_client = _make_garmin_mock()
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        result = json.loads(fitness_mod.refresh_athlete_baseline())

        assert result['status'] == 'success'
        assert result['garmin_profile']['full_name'] == 'John Doe'
        assert result['garmin_profile']['weight_kg'] == 75.0
        assert result['garmin_profile']['age'] >= 35

        # Check file too
        profile = json.loads((tmp_path / 'athlete_baseline.json').read_text())
        assert 'garmin_profile' in profile
        assert profile['garmin_profile']['weight_kg'] == 75.0

    @patch('tools.fitness_tools.garmin_api_call')
    def test_garmin_profile_failure_non_fatal(self, mock_api_call, tmp_path, monkeypatch):
        """If profile APIs fail, baseline still succeeds with empty garmin_profile."""
        import tools.fitness_tools as fitness_mod
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)

        call_count = [0]
        def side_effect(fn, *a, **kw):
            call_count[0] += 1
            mock = Mock()
            mock.get_activities_by_date.return_value = [SAMPLE_RUNNING_ACTIVITY]
            mock.get_personal_record.return_value = SAMPLE_PR_DATA
            # Profile calls raise
            mock.get_full_name.side_effect = Exception("API error")
            mock.get_user_profile.side_effect = Exception("API error")
            mock.get_body_composition.side_effect = Exception("API error")
            return fn(mock, *a, **kw)

        mock_api_call.side_effect = side_effect

        result = json.loads(fitness_mod.refresh_athlete_baseline())
        assert result['status'] == 'success'
        # garmin_profile should be empty dict (failure was caught)
        assert result.get('garmin_profile') == {} or result.get('garmin_profile', {}).get('full_name') is None


class TestAutoPopulateAthlete:
    @patch('tools.fitness_tools.garmin_api_call')
    def test_fills_none_fields_from_garmin(self, mock_api_call, tmp_path, monkeypatch):
        """Auto-populates None fields in athlete.json from Garmin profile."""
        import tools.fitness_tools as fitness_mod
        import planner
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)

        # Create athlete.json with None values
        athlete = {
            'personal': {'name': None, 'weight_kg': None, 'age': None, 'max_hr': 190}
        }
        (tmp_path / 'athlete.json').write_text(json.dumps(athlete))

        mock_client = _make_garmin_mock()
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        fitness_mod.refresh_athlete_baseline()

        # Verify athlete.json was updated
        updated = json.loads((tmp_path / 'athlete.json').read_text())
        assert updated['personal']['name'] == 'John Doe'
        assert updated['personal']['weight_kg'] == 75.0
        assert updated['personal']['age'] >= 35
        # Manually set value should NOT be overwritten
        assert updated['personal']['max_hr'] == 190

    @patch('tools.fitness_tools.garmin_api_call')
    def test_does_not_overwrite_manual_values(self, mock_api_call, tmp_path, monkeypatch):
        """Never overwrites manually set values in athlete.json."""
        import tools.fitness_tools as fitness_mod
        import planner
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)

        # Create athlete.json with manually set values
        athlete = {
            'personal': {'name': 'Manual Name', 'weight_kg': 80.0, 'age': 40}
        }
        (tmp_path / 'athlete.json').write_text(json.dumps(athlete))

        mock_client = _make_garmin_mock()
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        fitness_mod.refresh_athlete_baseline()

        # Verify manually set values are preserved
        updated = json.loads((tmp_path / 'athlete.json').read_text())
        assert updated['personal']['name'] == 'Manual Name'
        assert updated['personal']['weight_kg'] == 80.0
        assert updated['personal']['age'] == 40

    @patch('tools.fitness_tools.garmin_api_call')
    def test_fills_max_hr_from_garmin(self, mock_api_call, tmp_path, monkeypatch):
        """Auto-populates max_hr when None in athlete.json."""
        import tools.fitness_tools as fitness_mod
        import planner
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)

        athlete = {
            'personal': {'name': 'Test', 'max_hr': None}
        }
        (tmp_path / 'athlete.json').write_text(json.dumps(athlete))

        mock_client = _make_garmin_mock()
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        fitness_mod.refresh_athlete_baseline()

        updated = json.loads((tmp_path / 'athlete.json').read_text())
        assert updated['personal']['max_hr'] == 192
