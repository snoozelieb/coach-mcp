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


class TestRefreshAthleteBaseline:
    @patch('tools.fitness_tools.garmin_api_call')
    def test_creates_baseline_file_with_correct_structure(self, mock_api_call, tmp_path, monkeypatch):
        """The full pipeline: API → parse → calculate → write file."""
        import tools.fitness_tools as fitness_mod
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)

        mock_client = Mock()
        mock_client.get_activities_by_date.return_value = [SAMPLE_RUNNING_ACTIVITY]
        mock_client.get_personal_record.return_value = SAMPLE_PR_DATA
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
