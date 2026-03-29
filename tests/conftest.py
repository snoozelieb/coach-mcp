"""Shared pytest fixtures and sample data for coach-mcp test suite."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Sample data constants (importable by any test file via: from conftest import X)
# ---------------------------------------------------------------------------

SAMPLE_RUNNING_ACTIVITY = {
    'activityId': 12345678901,
    'activityName': 'Morning Run',
    'startTimeLocal': '2025-12-01T06:30:00.0',
    'activityType': {
        'typeId': 1,
        'typeKey': 'running',
        'parentTypeId': 17,
    },
    'duration': 2700,
    'distance': 8000,
    'averageHR': 145,
    'maxHR': 168,
    'calories': 520,
}

SAMPLE_STRENGTH_ACTIVITY = {
    'activityId': 12345678902,
    'activityName': 'Strength Training',
    'startTimeLocal': '2025-12-02T17:00:00.0',
    'activityType': {
        'typeId': 13,
        'typeKey': 'strength_training',
        'parentTypeId': 29,
    },
    'duration': 3600,
    'distance': None,
    'averageHR': 110,
    'maxHR': 135,
    'calories': 380,
}

SAMPLE_PR_DATA = {
    'personalRecords': [
        {
            'prTypeLabelKey': 'pr_running_fastest_5k_time',
            'value': 1320,
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-06-15T08:30:00.0',
            'activityId': 11111111111,
        },
        {
            'prTypeLabelKey': 'pr_running_fastest_10k_time',
            'value': 2820,
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-09-22T07:00:00.0',
            'activityId': 22222222222,
        },
        {
            'prTypeLabelKey': 'pr_running_longest_distance',
            'value': 21100,
            'unitKey': 'meter',
            'prStartTimeGmtFormatted': '2025-10-10T06:00:00.0',
            'activityId': 33333333333,
        },
    ]
}

SAMPLE_TRAINING_READINESS = {
    'calendarDate': '2025-12-01',
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
    'acuteLoad': 450.5,
    'feedbackPhrase': 'Your body is well recovered and ready for a hard workout.',
}

SAMPLE_PARSED_ACTIVITIES = [
    {'date': '2025-11-25', 'type': 'running', 'duration_mins': 45.0},
    {'date': '2025-11-26', 'type': 'strength_training', 'duration_mins': 60.0},
    {'date': '2025-11-28', 'type': 'running', 'duration_mins': 30.0},
    {'date': '2025-12-01', 'type': 'running', 'duration_mins': 60.0},
    {'date': '2025-12-02', 'type': 'cycling', 'duration_mins': 90.0},
    {'date': '2025-12-03', 'type': 'strength_training', 'duration_mins': 45.0},
    {'date': '2025-12-05', 'type': 'running', 'duration_mins': 75.0},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _MockContext:
    """Lightweight mock for fastmcp.Context used in direct tool calls."""
    def __init__(self):
        self.report_progress = AsyncMock()
        self.info = AsyncMock()
        self.debug = AsyncMock()
        self.warning = AsyncMock()
        self.error = AsyncMock()
        self.log = AsyncMock()


@pytest.fixture
def mock_ctx():
    """Provide a mock fastmcp Context for async tool tests."""
    return _MockContext()


@pytest.fixture(scope="session")
def garmin_fixtures():
    """Load real Garmin API fixture data (session-scoped, loaded once).

    test_fixtures.json lives in the project root (gitignored).
    """
    fixtures_path = Path(__file__).parent.parent / "test_fixtures.json"
    with open(fixtures_path) as f:
        return json.load(f)


@pytest.fixture
def data_dir(tmp_path):
    """Provide a temp directory for tool tests that do file I/O."""
    return tmp_path


@pytest.fixture
def sample_athlete():
    """Minimal athlete profile for tool tests."""
    return {
        'personal': {
            'name': 'Test Athlete',
            'age': 30,
            'max_hr': 190,
            'resting_hr': 45,
            'weight_kg': 75,
        },
        'injury_history': [],
        'life_constraints': {},
        'preferences': {},
    }
