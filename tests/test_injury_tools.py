"""Tests for tools/injury_tools.py — diagnose, research, and track injuries."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import date

import coach.planner as planner
from coach.tools.injury_tools import (
    diagnose_injury,
    research_injury,
    _is_relevant_content,
    _is_significant_redirect,
    _extract_clinical_info,
    _fetch_injury_research,
    _save_diagnosis_to_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def injury_dir(data_dir, monkeypatch):
    """Redirect planner.DATA_DIR to tmp_path and seed minimal athlete."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    # Also patch the injury_tools module's imports from planner
    import coach.tools.injury_tools as injury_mod
    monkeypatch.setattr(injury_mod, 'load_athlete', lambda: json.loads(
        (data_dir / 'athlete.json').read_text()
    ) if (data_dir / 'athlete.json').exists() else {})
    monkeypatch.setattr(injury_mod, 'save_json_file', lambda fn, data: (
        (data_dir / fn).write_text(json.dumps(data, indent=2))
    ))
    monkeypatch.setattr(injury_mod, 'load_json_file', lambda fn: json.loads(
        (data_dir / fn).read_text()
    ) if (data_dir / fn).exists() else {})
    # Seed minimal athlete
    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'Test', 'age': 30},
        'injury_history': [],
        'coaching_notes': '',
    }))
    return data_dir


CLINICAL_CONTENT = (
    "Shin splints, also known as medial tibial stress syndrome, is a common "
    "exercise-related condition. Symptoms include pain along the inner edge of "
    "the shinbone. Treatment includes rest, ice, and stretching. Rehabilitation "
    "involves gradual return to activity with strengthening exercises. The "
    "recovery timeline is typically 2-6 weeks with proper management. "
    "Risk factors include sudden increases in training volume, running on hard "
    "surfaces, and improper footwear. Complications may include stress fractures "
    "if left untreated. Presentation typically includes tenderness along the "
    "posteromedial border of the tibia. Diagnosis is primarily clinical based "
    "on history and examination findings. "
    "x" * 500  # Pad to meet minimum length
)


# ---------------------------------------------------------------------------
# Phase 1 tests
# ---------------------------------------------------------------------------

class TestDiagnosePhase1:
    def test_returns_questions_for_known_region(self):
        result = json.loads(diagnose_injury(location="shin"))
        assert result["phase"] == "assessment"
        assert result["body_region"] == "shin"
        assert len(result["questions"]) > 6  # default + shin-specific

    def test_normalizes_left_shin_to_shin(self):
        result = json.loads(diagnose_injury(location="left shin"))
        assert result["body_region"] == "shin"

    def test_normalizes_right_knee_to_knee(self):
        result = json.loads(diagnose_injury(location="Right Knee"))
        assert result["body_region"] == "knee"

    def test_normalizes_lower_back(self):
        result = json.loads(diagnose_injury(location="lower back"))
        assert result["body_region"] == "back"

    def test_unknown_region_gets_default_questions_only(self):
        result = json.loads(diagnose_injury(location="elbow"))
        assert result["body_region"] == "elbow"
        assert result["phase"] == "assessment"
        # Only default questions (no region-specific)
        default_count = len(result["questions"])
        shin_result = json.loads(diagnose_injury(location="shin"))
        assert default_count < len(shin_result["questions"])

    def test_returns_instructions(self):
        result = json.loads(diagnose_injury(location="ankle"))
        assert "instructions" in result


# ---------------------------------------------------------------------------
# Phase 2 tests
# ---------------------------------------------------------------------------

class TestDiagnosePhase2:
    MILD_ANSWERS = json.dumps({
        "onset": "Gradual (over days/weeks)",
        "pain_type": "Dull/aching",
        "timing": "Only during activity",
        "swelling": "No",
        "history": "No",
        "recent_changes": "Increased volume",
        "location_specific": "Front (anterior)",
        "aggravating": "Running",
    })

    MODERATE_ANSWERS = json.dumps({
        "onset": "Gradual (over days/weeks)",
        "pain_type": "Dull/aching",
        "timing": "During and after activity",
        "swelling": "Slight swelling",
        "history": "No",
        "recent_changes": "Increased volume",
        "location_specific": "Inner side (medial)",
        "aggravating": "Running",
    })

    SEVERE_ANSWERS = json.dumps({
        "onset": "Sudden (during activity)",
        "pain_type": "Sharp/stabbing",
        "timing": "At rest too",
        "swelling": "Yes, significant",
        "history": "Yes",
        "recent_changes": "No changes",
        "location_specific": "Front (anterior)",
        "aggravating": "Walking",
    })

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_returns_clinical_picture(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["phase"] == "diagnosis"
        assert result["clinical_picture"]["onset"] == "gradual"
        assert result["clinical_picture"]["body_region"] == "shin"
        assert "possible_conditions" not in result

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_mild_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["severity_assessment"] == "mild"

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_moderate_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MODERATE_ANSWERS))
        assert result["severity_assessment"] == "moderate"

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_severe_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.SEVERE_ANSWERS))
        assert result["severity_assessment"] == "severe"

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_red_flag_detection(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.SEVERE_ANSWERS))
        assert any("Severe" in rf for rf in result["red_flags"])

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_research_context_included(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {
            "source": "physio-pedia",
            "content": "some clinical content",
            "url": "https://www.physio-pedia.com/Shin_Splints",
            "clinical_info": {"treatment": ["Rest and ice"]},
        }
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["research_context"]["source"] == "physio-pedia"
        assert result["research_context"]["url"] != ""
        assert result["research_context"]["research_status"] == "ok"

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_saves_to_injury_history(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["saved_to_profile"] is True

        # Verify athlete.json was updated
        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        assert len(athlete['injury_history']) == 1
        assert athlete['injury_history'][0]['body_region'] == 'shin'

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_saves_to_coaching_notes(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        diagnose_injury("shin", self.MILD_ANSWERS)

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        assert "Injury: shin" in athlete['coaching_notes']
        assert "severity=mild" in athlete['coaching_notes']

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_deduplicates_same_day_same_region(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        # Diagnose twice on same day
        diagnose_injury("shin", self.MILD_ANSWERS)
        diagnose_injury("shin", self.SEVERE_ANSWERS)

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        shin_entries = [e for e in athlete['injury_history'] if e['body_region'] == 'shin']
        assert len(shin_entries) == 1
        # Should have updated severity to severe
        assert shin_entries[0]['severity'] == 'severe'

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_fetch_failure_returns_diagnosis_with_empty_research(self, mock_fetch, injury_dir):
        mock_fetch.side_effect = Exception("Network error")
        # Should NOT crash — returns error from outer except
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert "error" in result or result["phase"] == "diagnosis"

    def test_invalid_json_answers_returns_error(self):
        result = json.loads(diagnose_injury("shin", "not valid json"))
        assert "error" in result

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_candidate_conditions_present(self, mock_fetch, injury_dir):
        """Phase 2 response includes candidate_conditions derived from search terms."""
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert "candidate_conditions" in result
        assert len(result["candidate_conditions"]) > 0
        # For "Front (anterior)" shin → should include anterior tibialis tendinitis
        conditions_lower = [c.lower() for c in result["candidate_conditions"]]
        assert any("anterior" in c or "shin" in c for c in conditions_lower)

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_needs_research_signal_when_no_content(self, mock_fetch, injury_dir):
        """When _fetch_injury_research returns empty dict, response has needs_research signal."""
        mock_fetch.return_value = {}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["research_context"]["research_status"] == "needs_research"
        assert "suggested_sources" in result["research_context"]
        assert len(result["research_context"]["suggested_sources"]) > 0

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_coaching_notes_not_duplicated(self, mock_fetch, injury_dir):
        """Calling diagnose twice on same day/region does NOT duplicate coaching_notes."""
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        diagnose_injury("shin", self.MILD_ANSWERS)
        diagnose_injury("shin", self.SEVERE_ANSWERS)

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        notes = athlete['coaching_notes']
        # Only one injury line for shin on today's date
        today = date.today().isoformat()
        injury_lines = [l for l in notes.split('\n') if f"[{today}]" in l and "(shin)" in l]
        assert len(injury_lines) == 1
        # Should reflect latest severity
        assert "severity=severe" in injury_lines[0]


# ---------------------------------------------------------------------------
# Helper tests: _is_relevant_content
# ---------------------------------------------------------------------------

class TestIsRelevantContent:
    def test_rejects_short_content(self):
        assert _is_relevant_content("short", "shin", "Shin_Splints") is False

    def test_rejects_no_clinical_indicators(self):
        content = "a" * 600  # Long enough but no clinical words
        assert _is_relevant_content(content, "shin", "Shin_Splints") is False

    def test_rejects_unrelated_content(self):
        content = "This is about treatment of plants and gardening " + "x" * 500
        assert _is_relevant_content(content, "shin", "Shin_Splints") is False

    def test_accepts_relevant_content(self):
        assert _is_relevant_content(CLINICAL_CONTENT, "shin", "Shin_Splints") is True

    def test_accepts_when_no_meaningful_term_words(self):
        # Search term with only short words (<=3 chars) — term check is skipped
        content = "Treatment and rehabilitation of the ACL " + "x" * 500
        assert _is_relevant_content(content, "knee", "ACL") is True


# ---------------------------------------------------------------------------
# Helper tests: _is_significant_redirect
# ---------------------------------------------------------------------------

class TestIsSignificantRedirect:
    def test_same_url_not_redirect(self):
        assert _is_significant_redirect(
            "https://www.physio-pedia.com/Shin_Splints",
            "https://www.physio-pedia.com/Shin_Splints"
        ) is False

    def test_trailing_slash_not_redirect(self):
        assert _is_significant_redirect(
            "https://www.physio-pedia.com/Shin_Splints",
            "https://www.physio-pedia.com/Shin_Splints/"
        ) is False

    def test_different_path_is_redirect(self):
        assert _is_significant_redirect(
            "https://www.physio-pedia.com/Shin_Splints",
            "https://www.physio-pedia.com/Search?query=shin"
        ) is True

    def test_empty_final_url_not_redirect(self):
        assert _is_significant_redirect(
            "https://www.physio-pedia.com/Shin_Splints", ""
        ) is False

    def test_redirect_to_home_page(self):
        assert _is_significant_redirect(
            "https://www.physio-pedia.com/Nonexistent_Article",
            "https://www.physio-pedia.com/"
        ) is True


# ---------------------------------------------------------------------------
# Helper tests: _extract_clinical_info
# ---------------------------------------------------------------------------

class TestExtractClinicalInfo:
    def test_extracts_treatment(self):
        info = _extract_clinical_info(CLINICAL_CONTENT)
        assert "treatment" in info
        assert len(info["treatment"]) > 0

    def test_extracts_rehabilitation(self):
        info = _extract_clinical_info(CLINICAL_CONTENT)
        assert "rehabilitation" in info

    def test_extracts_recovery_timeline(self):
        info = _extract_clinical_info(CLINICAL_CONTENT)
        assert "recovery_timeline" in info

    def test_extracts_risk_factors(self):
        info = _extract_clinical_info(CLINICAL_CONTENT)
        assert "risk_factors" in info

    def test_skips_short_sentences(self):
        info = _extract_clinical_info("Treatment. x. Recovery in 2 weeks is possible with physiotherapy.")
        # "Treatment" and "x" are too short (<20 chars), so filtered out.
        # The longer sentence may match multiple categories — that's fine.
        # Verify short fragments aren't in any category.
        all_findings = [s for v in info.values() for s in v]
        assert not any(f.strip() == "Treatment" for f in all_findings)
        assert not any(f.strip() == "x" for f in all_findings)

    def test_empty_content_returns_empty(self):
        info = _extract_clinical_info("")
        assert info == {}


# ---------------------------------------------------------------------------
# _fetch_injury_research integration tests
# ---------------------------------------------------------------------------

class TestFetchInjuryResearch:
    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_tries_physiopedia_before_wikipedia(self, mock_fetch):
        """Physio-pedia is tried first; Wikipedia is fallback."""
        calls = []
        def track_calls(url):
            calls.append(url)
            if "physio-pedia.com" in url:
                return (CLINICAL_CONTENT, url)
            return ("irrelevant", url)

        mock_fetch.side_effect = track_calls
        result = _fetch_injury_research("shin", {"location_specific": ""})
        assert result["source"] == "physio-pedia"
        assert "physio-pedia.com" in calls[0]

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_falls_back_to_wikipedia(self, mock_fetch):
        """When physio-pedia fails, Wikipedia is tried."""
        def fallback(url):
            if "physio-pedia.com" in url:
                raise Exception("404")
            return (CLINICAL_CONTENT, url)

        mock_fetch.side_effect = fallback
        result = _fetch_injury_research("shin", {"location_specific": ""})
        assert result["source"] == "wikipedia"

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_irrelevant_content_skipped(self, mock_fetch):
        """Content that fails relevance check is skipped."""
        mock_fetch.return_value = ("short", "https://www.physio-pedia.com/Shin_Splints")
        result = _fetch_injury_research("shin", {"location_specific": ""})
        assert result == {}

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_complete_failure_returns_empty_dict(self, mock_fetch):
        """When all sources fail, returns empty dict."""
        mock_fetch.side_effect = Exception("Network error")
        result = _fetch_injury_research("shin", {"location_specific": ""})
        assert result == {}

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_redirect_rejected(self, mock_fetch):
        """Content from a significant redirect is rejected."""
        mock_fetch.return_value = (
            CLINICAL_CONTENT,
            "https://www.physio-pedia.com/Search?query=shin"  # Redirected!
        )
        result = _fetch_injury_research("shin", {"location_specific": ""})
        # Both physio-pedia attempts and wikipedia attempt all redirect
        # → should return empty dict
        assert result == {}


# ---------------------------------------------------------------------------
# Schema contract tests: _save_diagnosis_to_profile
# ---------------------------------------------------------------------------

class TestSaveDiagnosisSchema:
    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_saved_entry_has_downstream_fields(self, mock_fetch, injury_dir):
        """Saved injury entry has type, restricted_activities, safe_activities for downstream consumers."""
        mock_fetch.return_value = {}
        diagnose_injury("shin", TestDiagnosePhase2.MILD_ANSWERS)

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        entry = athlete['injury_history'][0]

        # Schema contract: fields downstream consumers expect
        assert 'type' in entry
        assert entry['type'] == 'shin'  # = body_region
        assert 'restricted_activities' in entry
        assert isinstance(entry['restricted_activities'], list)
        assert 'safe_activities' in entry
        assert isinstance(entry['safe_activities'], list)
        assert 'status' in entry
        assert entry['status'] == 'active'
        assert 'body_region' in entry

    @patch('coach.tools.injury_tools._fetch_injury_research')
    def test_saved_entry_consumable_by_planning_context(self, mock_fetch, injury_dir):
        """Verify the saved entry has fields that build_planning_context reads."""
        mock_fetch.return_value = {}
        diagnose_injury("knee", json.dumps({
            "onset": "Gradual",
            "pain_type": "Dull/aching",
            "timing": "Only during activity",
            "swelling": "No",
            "history": "No",
            "recent_changes": "No changes",
            "location_specific": "Front (kneecap)",
            "aggravating": "Stairs (down)",
        }))

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        entry = athlete['injury_history'][0]

        # Simulate what planner.py does
        active_injuries = [e for e in athlete['injury_history'] if e.get('status', 'active') == 'active']
        assert len(active_injuries) == 1

        # Simulate restricted/safe collection (planner.py:332-344)
        all_restricted = set()
        all_safe = set()
        for injury in active_injuries:
            all_restricted.update(injury.get('restricted_activities', []))
            all_safe.update(injury.get('safe_activities', []))
        # Should not crash — returns empty sets for new injury
        assert isinstance(all_restricted, set)
        assert isinstance(all_safe, set)

        # Simulate what strength_tools.py does (string-match on type)
        injury_type = entry.get('type', '').lower()
        assert 'knee' in injury_type


# ---------------------------------------------------------------------------
# research_injury tests
# ---------------------------------------------------------------------------

class TestResearchInjury:
    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_tries_physiopedia_before_wikipedia(self, mock_fetch):
        # Return relevant content on first call (physio-pedia)
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://www.physio-pedia.com/Shin_Splints")
        result = json.loads(research_injury("shin splints"))

        # Should have used physio-pedia URL
        assert any("physio-pedia" in s for s in result["sources"])
        assert "researched_info" in result

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_no_hardcoded_severity_guidance(self, mock_fetch):
        mock_fetch.side_effect = lambda url: (CLINICAL_CONTENT, url)
        result = json.loads(research_injury("shin splints", severity="moderate"))
        assert "severity_context" not in result

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_no_hardcoded_activity_guidance(self, mock_fetch):
        mock_fetch.side_effect = lambda url: (CLINICAL_CONTENT, url)
        result = json.loads(research_injury("shin splints"))
        assert "activity_guidance" not in result

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_uses_provided_url_first(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://custom.com/injury")
        result = json.loads(research_injury(
            "shin splints", url="https://custom.com/injury"
        ))
        assert "https://custom.com/injury" in result["sources"]

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_handles_all_fetch_failures(self, mock_fetch):
        mock_fetch.side_effect = Exception("Network error")
        result = json.loads(research_injury("shin splints"))
        assert "suggested_searches" in result["researched_info"]
        assert "physio-pedia.com" in result["researched_info"]["recommended_sources"]

    def test_invalid_severity_returns_error(self):
        result = json.loads(research_injury("shin splints", severity="extreme"))
        assert "error" in result
        assert "Invalid severity" in result["error"]
        assert "extreme" in result["error"]
        assert "mild" in result["error"]
        assert "moderate" in result["error"]
        assert "severe" in result["error"]

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_structured_extraction_in_researched_info(self, mock_fetch):
        # Return same URL to avoid redirect rejection
        mock_fetch.side_effect = lambda url: (CLINICAL_CONTENT, url)
        result = json.loads(research_injury("shin splints"))
        info = result["researched_info"]
        # Should have structured categories from _extract_clinical_info
        assert any(k in info for k in ["treatment", "rehabilitation", "recovery_timeline"])

    @patch('coach.tools.injury_tools.fetch_page_text_validated')
    def test_redirect_rejected_in_research(self, mock_fetch):
        """Significant redirects are rejected in research_injury too."""
        mock_fetch.return_value = (
            CLINICAL_CONTENT,
            "https://www.physio-pedia.com/Search?query=shin"
        )
        result = json.loads(research_injury("shin splints"))
        # No sources should be added since all redirected
        assert len(result["sources"]) == 0
        assert "suggested_searches" in result["researched_info"]
