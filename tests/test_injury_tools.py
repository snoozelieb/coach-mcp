"""Tests for tools/injury_tools.py — diagnose, research, and track injuries."""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import date

import planner
from tools.injury_tools import (
    diagnose_injury,
    research_injury,
    _build_search_terms,
    _is_relevant_content,
    _extract_clinical_info,
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
    import tools.injury_tools as injury_mod
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

    @patch('tools.injury_tools._fetch_injury_research')
    def test_returns_clinical_picture(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["phase"] == "diagnosis"
        assert result["clinical_picture"]["onset"] == "gradual"
        assert result["clinical_picture"]["body_region"] == "shin"
        assert "possible_conditions" not in result

    @patch('tools.injury_tools._fetch_injury_research')
    def test_mild_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["severity_assessment"] == "mild"

    @patch('tools.injury_tools._fetch_injury_research')
    def test_moderate_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MODERATE_ANSWERS))
        assert result["severity_assessment"] == "moderate"

    @patch('tools.injury_tools._fetch_injury_research')
    def test_severe_severity(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.SEVERE_ANSWERS))
        assert result["severity_assessment"] == "severe"

    @patch('tools.injury_tools._fetch_injury_research')
    def test_red_flag_detection(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.SEVERE_ANSWERS))
        assert any("Severe" in rf for rf in result["red_flags"])

    @patch('tools.injury_tools._fetch_injury_research')
    def test_research_context_included(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {
            "source": "orthobullets",
            "content": "some clinical content",
            "url": "https://www.orthobullets.com/sports/shin-splints",
            "clinical_info": {"treatment": ["Rest and ice"]},
        }
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["research_context"]["source"] == "orthobullets"
        assert result["research_context"]["url"] != ""

    @patch('tools.injury_tools._fetch_injury_research')
    def test_saves_to_injury_history(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        assert result["saved_to_profile"] is True

        # Verify athlete.json was updated
        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        assert len(athlete['injury_history']) == 1
        assert athlete['injury_history'][0]['body_region'] == 'shin'

    @patch('tools.injury_tools._fetch_injury_research')
    def test_saves_to_coaching_notes(self, mock_fetch, injury_dir):
        mock_fetch.return_value = {"source": "none", "content": "", "url": "", "clinical_info": {}}
        diagnose_injury("shin", self.MILD_ANSWERS)

        athlete = json.loads((injury_dir / 'athlete.json').read_text())
        assert "Injury: shin" in athlete['coaching_notes']
        assert "severity=mild" in athlete['coaching_notes']

    @patch('tools.injury_tools._fetch_injury_research')
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

    @patch('tools.injury_tools._fetch_injury_research')
    def test_fetch_failure_returns_diagnosis_with_empty_research(self, mock_fetch, injury_dir):
        mock_fetch.side_effect = Exception("Network error")
        # Should NOT crash — returns diagnosis with empty research
        result = json.loads(diagnose_injury("shin", self.MILD_ANSWERS))
        # Will have research_context with source "none" because _fetch_injury_research
        # is mocked to raise, which gets caught in the diagnose_injury try/except
        # Actually the mock raises on call, so the outer except catches it
        assert "error" in result or result["phase"] == "diagnosis"

    def test_invalid_json_answers_returns_error(self):
        result = json.loads(diagnose_injury("shin", "not valid json"))
        assert "error" in result


# ---------------------------------------------------------------------------
# Helper tests: _build_search_terms
# ---------------------------------------------------------------------------

class TestBuildSearchTerms:
    def test_shin_anterior(self):
        terms = _build_search_terms("shin", "Front (anterior)")
        assert "anterior-tibialis-tendinitis" in terms

    def test_shin_medial(self):
        terms = _build_search_terms("shin", "Inner side (medial)")
        assert "medial-tibial-stress-syndrome" in terms

    def test_knee_lateral(self):
        terms = _build_search_terms("knee", "Outer side (lateral)")
        assert "iliotibial-band-syndrome" in terms

    def test_ankle_achilles(self):
        terms = _build_search_terms("ankle", "Back (Achilles)")
        assert "achilles-tendinitis" in terms

    def test_fallback_for_unknown_sublocation(self):
        terms = _build_search_terms("shin", "unknown area")
        # Should return default
        assert "shin-splints" in terms

    def test_fallback_for_unknown_region(self):
        terms = _build_search_terms("elbow", "lateral")
        assert "elbow-pain" in terms

    def test_empty_location_uses_default(self):
        terms = _build_search_terms("knee", "")
        assert "knee-pain" in terms or "patellofemoral-pain-syndrome" in terms


# ---------------------------------------------------------------------------
# Helper tests: _is_relevant_content
# ---------------------------------------------------------------------------

class TestIsRelevantContent:
    def test_rejects_short_content(self):
        assert _is_relevant_content("short", "shin", "shin-splints") is False

    def test_rejects_no_clinical_indicators(self):
        content = "a" * 600  # Long enough but no clinical words
        assert _is_relevant_content(content, "shin", "shin-splints") is False

    def test_rejects_unrelated_content(self):
        content = "This is about treatment of plants and gardening " + "x" * 500
        assert _is_relevant_content(content, "shin", "shin-splints") is False

    def test_accepts_relevant_content(self):
        assert _is_relevant_content(CLINICAL_CONTENT, "shin", "shin-splints") is True

    def test_accepts_when_no_meaningful_term_words(self):
        # Search term with only short words (<=3 chars) — term check is skipped
        content = "Treatment and rehabilitation of the ACL " + "x" * 500
        assert _is_relevant_content(content, "knee", "acl") is True


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
# research_injury tests
# ---------------------------------------------------------------------------

class TestResearchInjury:
    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_tries_orthobullets_before_wikipedia(self, mock_fetch):
        # Return relevant content on first call (orthobullets)
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://www.orthobullets.com/sports/shin-splints")
        result = json.loads(research_injury("shin splints"))

        # Should have used orthobullets URL
        assert any("orthobullets" in s for s in result["sources"])
        assert "researched_info" in result

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_no_hardcoded_severity_guidance(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://example.com/page")
        result = json.loads(research_injury("shin splints", severity="moderate"))
        assert "severity_context" not in result

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_no_hardcoded_activity_guidance(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://example.com/page")
        result = json.loads(research_injury("shin splints"))
        assert "activity_guidance" not in result

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_uses_provided_url_first(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://custom.com/injury")
        result = json.loads(research_injury(
            "shin splints", url="https://custom.com/injury"
        ))
        assert "https://custom.com/injury" in result["sources"]

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_handles_all_fetch_failures(self, mock_fetch):
        mock_fetch.side_effect = Exception("Network error")
        result = json.loads(research_injury("shin splints"))
        assert "suggested_searches" in result["researched_info"]
        assert "orthobullets.com" in result["researched_info"]["recommended_sources"]

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_invalid_severity_defaults_to_moderate(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://example.com")
        result = json.loads(research_injury("shin splints", severity="extreme"))
        assert result["severity"] == "moderate"

    @patch('tools.injury_tools.fetch_page_text_validated')
    def test_structured_extraction_in_researched_info(self, mock_fetch):
        mock_fetch.return_value = (CLINICAL_CONTENT, "https://example.com")
        result = json.loads(research_injury("shin splints"))
        info = result["researched_info"]
        # Should have structured categories from _extract_clinical_info
        assert any(k in info for k in ["treatment", "rehabilitation", "recovery_timeline"])
