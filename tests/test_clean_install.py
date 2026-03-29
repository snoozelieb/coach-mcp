"""
Tests for clean-install behavior — what happens when a brand-new user runs
MCP tools with no data files (empty DATA_DIR).

These tests verify:
1. File-based tools work from empty state (no crash, sensible defaults)
2. Garmin-dependent tools return structured errors (not stack traces)
3. Semantic correctness on empty state (no misleading data)
4. End-to-end first-user flow (onboarding sequence persists data)
"""
import json
import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_calc_mod
import coach.parsers as parsers_mod
import coach.tools.fitness_tools as fitness_mod
import coach.tools.strength_tools as strength_mod
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
from coach.tools.athlete_tools import (
    update_athlete,
    set_threshold_pace,
    set_ftp,
    get_methodology,
    update_methodology,
)
from coach.tools.strength_tools import get_strength_baseline, approve_progression
from coach.tools.fitness_tools import get_fitness_status, get_athlete
from coach.tools.planning_tools import get_weekly_plan, push_plan_to_garmin
from coach.tools.decision_tools import (
    log_coaching_decision,
    get_active_decisions,
    record_athlete_response,
    get_response_patterns,
)
from coach.tools.coaching_tools import (
    get_coaching_snapshot,
    get_coaching_score,
    get_compliance_report,
)
from coach.tools.fitness_tools import get_load_status
from coach.rules import check_weekly_compliance


# ---------------------------------------------------------------------------
# Fixture: simulate a clean install with empty DATA_DIR
# ---------------------------------------------------------------------------

@pytest.fixture
def empty_install(data_dir, monkeypatch):
    """Simulate a clean install — empty DATA_DIR, no files."""
    # Core I/O layer
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)

    # Modules that import DATA_DIR from config directly
    monkeypatch.setattr(fitness_mod, 'DATA_DIR', data_dir)
    monkeypatch.setattr(strength_mod, 'DATA_DIR', data_dir)
    monkeypatch.setattr(coaching_mod, 'DATA_DIR', data_dir)
    monkeypatch.setattr(planning_mod, 'DATA_DIR', data_dir)
    monkeypatch.setattr(rules, 'DATA_DIR', data_dir)
    monkeypatch.setattr(fitness_calc_mod, 'DATA_DIR', data_dir)
    monkeypatch.setattr(parsers_mod, 'DATA_DIR', data_dir)

    return data_dir


# ============================================================================
# Category 1: File-based tools that should work from empty state (no Garmin)
# ============================================================================

class TestFileBasedToolsEmptyState:
    """Tools that do local file I/O should work on a clean install."""

    def test_update_athlete_creates_file_from_scratch(self, empty_install):
        """update_athlete should create athlete.json from nothing."""
        result = json.loads(update_athlete('personal', '{"name": "New User"}'))

        assert result['status'] == 'success'
        assert result['updated']['name'] == 'New User'

        # Verify the file was actually created on disk
        athlete_file = empty_install / 'athlete.json'
        assert athlete_file.exists()
        saved = json.loads(athlete_file.read_text())
        assert saved['personal']['name'] == 'New User'

    def test_set_threshold_pace_empty_athlete(self, empty_install):
        """set_threshold_pace should create athlete.json with pace zones."""
        result = json.loads(set_threshold_pace(pace='5:00'))

        assert result['status'] == 'success'
        assert result['threshold_sec_per_km'] == 300  # 5:00 = 300 sec
        assert 'pace_zones' in result
        assert len(result['pace_zones']) == 5  # z1 through z5

        # File persisted
        athlete_file = empty_install / 'athlete.json'
        assert athlete_file.exists()

    def test_set_ftp_empty_athlete(self, empty_install):
        """set_ftp should create athlete.json with power zones."""
        result = json.loads(set_ftp(ftp_watts=200))

        assert result['status'] == 'success'
        assert result['ftp'] == 200
        assert 'power_zones' in result
        assert len(result['power_zones']) == 7  # 7-zone model

        # File persisted
        athlete_file = empty_install / 'athlete.json'
        assert athlete_file.exists()

    def test_get_strength_baseline_empty(self, empty_install):
        """get_strength_baseline should return zero-state, not crash."""
        result = json.loads(get_strength_baseline())

        assert 'exercises' in result
        assert result['total_exercises_tracked'] == 0
        assert result['pending_progressions'] == []

    def test_get_fitness_status_no_history(self, empty_install):
        """get_fitness_status should report no_data with actionable message."""
        result = json.loads(get_fitness_status())

        assert result['status'] == 'no_data'
        assert 'refresh_fitness_history' in result.get('message', '').lower() or \
               'refresh_fitness_history' in result.get('action', '').lower()

    def test_get_weekly_plan_no_plan(self, empty_install):
        """get_weekly_plan should return a valid JSON structure."""
        result = json.loads(get_weekly_plan())

        # Should be a valid dict (either empty template or {})
        assert isinstance(result, dict)
        # Should NOT have an error key
        assert 'error' not in result

    def test_log_decision_creates_coaching_log(self, empty_install):
        """log_coaching_decision should create coaching_log.json from scratch."""
        result = json.loads(log_coaching_decision(
            decision_type='load_adjustment',
            decision='Reduce volume by 10%',
            rationale='Athlete reported fatigue',
        ))

        assert result['status'] == 'logged'
        assert 'decision_id' in result

        # File created
        log_file = empty_install / 'coaching_log.json'
        assert log_file.exists()
        saved = json.loads(log_file.read_text())
        assert len(saved['decisions']) == 1

    def test_get_active_decisions_empty(self, empty_install):
        """get_active_decisions should return count: 0 on empty state."""
        result = json.loads(get_active_decisions())

        assert result['count'] == 0
        assert result['active_decisions'] == []

    def test_record_response_creates_log(self, empty_install):
        """record_athlete_response should create coaching_log.json."""
        result = json.loads(record_athlete_response(
            stimulus='Long ride 2hrs Z2',
            response='Felt strong next day, readiness 75',
            pattern='handles_volume_well',
        ))

        assert result['status'] == 'recorded'

        # File created
        log_file = empty_install / 'coaching_log.json'
        assert log_file.exists()

    def test_get_response_patterns_empty(self, empty_install):
        """get_response_patterns should return pattern_count: 0."""
        result = json.loads(get_response_patterns())

        assert result['pattern_count'] == 0
        assert result['patterns'] == {}

    def test_get_athlete_empty(self, empty_install):
        """get_athlete should return valid JSON (empty profile) not crash."""
        result = json.loads(get_athlete())

        assert isinstance(result, dict)
        # No error key
        assert 'error' not in result

    def test_get_methodology_empty(self, empty_install):
        """get_methodology should return {} on empty state, not crash."""
        result = json.loads(get_methodology())

        assert isinstance(result, dict)
        # Empty dict is fine — no methodology configured yet
        assert 'error' not in result


# ============================================================================
# Category 2: Garmin-dependent tools with mocked API failure
# ============================================================================

class TestGarminDependentToolsNoCredentials:
    """Tools that call Garmin API should return structured errors, not stack traces."""

    @pytest.fixture(autouse=True)
    def mock_garmin_failure(self, monkeypatch):
        """Mock garmin_api_call to simulate no credentials."""
        def _fail(*args, **kwargs):
            raise Exception("No Garmin credentials configured")

        # Patch in every module that imports garmin_api_call
        monkeypatch.setattr('coach.tools.coaching_tools.garmin_api_call', _fail)
        monkeypatch.setattr('coach.tools.fitness_tools.garmin_api_call', _fail)
        monkeypatch.setattr('coach.tools.planning_tools.garmin_api_call', _fail)

    @pytest.mark.asyncio
    async def test_coaching_snapshot_no_garmin(self, empty_install, mock_ctx):
        """get_coaching_snapshot should return structured error, not crash."""
        result = json.loads(await get_coaching_snapshot(mock_ctx))

        # Should have an error key with a readable message
        assert 'error' in result
        assert isinstance(result['error'], str)
        assert len(result['error']) > 0

    def test_coaching_score_no_garmin(self, empty_install):
        """get_coaching_score should return structured error, not crash."""
        result = json.loads(get_coaching_score())

        # Should either have an error key or degrade gracefully
        assert isinstance(result, dict)
        # If it has an error, it should be a string message
        if 'error' in result:
            assert isinstance(result['error'], str)

    def test_compliance_report_no_garmin(self, empty_install):
        """get_compliance_report should return structured error, not crash."""
        result = json.loads(get_compliance_report())

        assert 'error' in result
        assert isinstance(result['error'], str)

    def test_load_status_no_garmin(self, empty_install):
        """get_load_status should return structured error, not crash."""
        result = json.loads(get_load_status())

        assert 'error' in result
        assert isinstance(result['error'], str)


# ============================================================================
# Category 3: Semantic correctness on empty state
# ============================================================================

class TestSemanticCorrectnessEmptyState:
    """Verify tools produce semantically correct results, not misleading data."""

    def test_compliance_with_no_pillars(self, empty_install):
        """check_weekly_compliance with empty activities and no config."""
        result = check_weekly_compliance([])

        assert isinstance(result, dict)
        assert 'overall_compliant' in result
        # With no pillars configured (all targets default to 0), all pillars
        # are vacuously met: 0 >= 0 is True for strength, mobility, long_effort.
        # This is technically correct but may be misleading — document it.
        assert result['strength']['required'] == 0
        assert result['strength']['compliant'] is True
        assert result['mobility']['required'] == 0
        assert result['mobility']['compliant'] is True
        assert result['long_effort']['required'] == 0
        assert result['long_effort']['compliant'] is True

    def test_push_plan_no_plan_exists(self, empty_install, monkeypatch):
        """push_plan_to_garmin should report clear error when no plan exists."""
        # Mock garmin_api_call since push_plan calls it
        def _fail(*args, **kwargs):
            raise Exception("No Garmin credentials")
        monkeypatch.setattr('coach.tools.planning_tools.garmin_api_call', _fail)

        result = json.loads(push_plan_to_garmin())

        assert 'error' in result
        # Should mention missing plan (not a Garmin error)
        assert 'plan' in result['error'].lower()

    def test_approve_progression_no_baseline(self, empty_install):
        """approve_progression should return error about exercise not found."""
        result = json.loads(approve_progression('bench_press'))

        assert result['status'] == 'error'
        assert 'not found' in result['message'].lower() or \
               'no pending' in result['message'].lower()


# ============================================================================
# Category 4: End-to-end first-user flow
# ============================================================================

class TestFirstUserFlow:
    """Simulate a new user going through the onboarding sequence."""

    def test_onboarding_sequence(self, empty_install):
        """Full onboarding: update_athlete → set_threshold_pace → set_ftp → log_decision."""
        # Step 1: Create athlete profile
        r1 = json.loads(update_athlete('personal', '{"name": "Jane", "age": 28, "weight_kg": 62}'))
        assert r1['status'] == 'success'

        # Step 2: Set running threshold
        r2 = json.loads(set_threshold_pace(pace='5:30'))
        assert r2['status'] == 'success'
        assert r2['threshold_sec_per_km'] == 330

        # Step 3: Set cycling FTP
        r3 = json.loads(set_ftp(ftp_watts=180))
        assert r3['status'] == 'success'
        assert r3['ftp'] == 180

        # Step 4: Log a coaching decision
        r4 = json.loads(log_coaching_decision(
            decision_type='exercise_selection',
            decision='Start with upper body focus due to knee history',
            rationale='Athlete reported previous knee issues',
        ))
        assert r4['status'] == 'logged'

        # Verify everything persisted to the right files
        athlete_file = empty_install / 'athlete.json'
        coaching_file = empty_install / 'coaching_log.json'
        assert athlete_file.exists()
        assert coaching_file.exists()

        athlete = json.loads(athlete_file.read_text())
        assert athlete['personal']['name'] == 'Jane'
        assert athlete['personal']['threshold_pace_sec_per_km'] == 330
        assert athlete['personal']['ftp'] == 180
        assert 'pace_zones' in athlete['personal']
        assert 'power_zones' in athlete['personal']

        coaching = json.loads(coaching_file.read_text())
        assert len(coaching['decisions']) == 1

    def test_update_methodology_creates_file(self, empty_install):
        """update_methodology should create methodology.json from empty state."""
        result = json.loads(update_methodology(
            'pillars',
            '{"strength_sessions_per_week": 2, "mobility_minutes_per_week": 60}',
        ))

        assert result['status'] == 'success'
        assert result['updated']['strength_sessions_per_week'] == 2

        # File created
        methodology_file = empty_install / 'methodology.json'
        assert methodology_file.exists()
        saved = json.loads(methodology_file.read_text())
        assert saved['pillars']['strength_sessions_per_week'] == 2
        assert saved['pillars']['mobility_minutes_per_week'] == 60
