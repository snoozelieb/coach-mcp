"""Tests for SERVER_INSTRUCTIONS size and the coaching doctrine resource.

Claude Code truncates MCP server instructions at ~2KB. The hard mandates must
fit under that limit; long-form doctrine lives in coach://coaching/doctrine.
"""
import coach.resources  # noqa: F401 — triggers resource registration on mcp

from coach.mcp_app import SERVER_INSTRUCTIONS, mcp
from coach.resources import COACHING_DOCTRINE, coaching_doctrine_resource

DOCTRINE_URI = "coach://coaching/doctrine"


# ---------------------------------------------------------------------------
# SERVER_INSTRUCTIONS size budget
# ---------------------------------------------------------------------------

class TestServerInstructions:
    def test_under_2kb_truncation_limit(self):
        """Claude Code drops everything past ~2KB — instructions must fit."""
        assert len(SERVER_INSTRUCTIONS) < 2000, (
            f"SERVER_INSTRUCTIONS is {len(SERVER_INSTRUCTIONS)} chars; "
            "Claude Code truncates at 2KB and silently drops the tail"
        )

    def test_hard_mandates_present(self):
        """The three hard mandates must survive in the compressed text."""
        assert "get_coaching_snapshot" in SERVER_INSTRUCTIONS
        assert "current_time_context" in SERVER_INSTRUCTIONS
        assert "restricted_activities" in SERVER_INSTRUCTIONS
        assert "week_grid" in SERVER_INSTRUCTIONS

    def test_points_to_doctrine_resource(self):
        assert DOCTRINE_URI in SERVER_INSTRUCTIONS

    def test_attached_to_mcp_instance(self):
        assert mcp.instructions == SERVER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Doctrine resource
# ---------------------------------------------------------------------------

class TestDoctrineResource:
    async def test_doctrine_resource_registered(self):
        resources = await mcp.list_resources()
        uris = [str(r.uri) for r in resources]
        assert DOCTRINE_URI in uris

    async def test_doctrine_readable_via_mcp(self):
        result = await mcp.read_resource(DOCTRINE_URI)
        contents = result.contents
        assert len(contents) > 0
        text = contents[0].content
        assert isinstance(text, str) and text.strip()
        assert text == COACHING_DOCTRINE

    def test_doctrine_non_empty_with_key_phrases(self):
        text = coaching_doctrine_resource()
        assert text.strip()
        for phrase in ("injury", "snapshot", "ACWR"):
            assert phrase in text, f"doctrine missing key phrase: {phrase!r}"

    def test_doctrine_covers_required_sections(self):
        """Long-form content moved out of SERVER_INSTRUCTIONS must all be here."""
        text = COACHING_DOCTRINE
        # Canonical flow
        assert "get_week_constraints" in text
        assert "get_weekly_prescription" in text
        assert "push_plan_to_garmin" in text
        # Load hierarchy
        assert "OVERALL ACWR" in text
        assert "SPORT-SPECIFIC CTL" in text
        # week_grid / plan_adherence
        assert "week_grid" in text
        assert "plan_adherence" in text
        # Multi-session days
        assert "Multi-Session Days" in text
        assert "single session dict OR" in text
        # Structured-run schema
        assert "structure" in text
        assert "repeat" in text
        assert "duration_secs" in text
        # Injury protocol
        assert "restricted_activities" in text
        assert "update_injury_status" in text
        # Approval workflow
        assert "propose_coaching_action" in text
        assert "approve_proposal" in text
