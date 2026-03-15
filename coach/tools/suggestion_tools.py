from ..mcp_app import mcp
from ..planner import (
    save_suggestion,
    get_pending_suggestions as get_suggestions,
    approve_suggestion as approve_sug,
    reject_suggestion as reject_sug,
)
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool()
def propose_suggestion(
    suggestion_type: str,
    description: str,
    rationale: str,
    proposed_change: str = None
) -> str:
    """
    Propose a training configuration change for user approval.

    Use this when the LLM identifies patterns that warrant pillar adjustments,
    new constraints, or other configuration changes.

    Args:
        suggestion_type: Category (e.g., 'pillar_adjustment', 'add_constraint',
                        'volume_change', 'event_timing')
        description: Short description of the suggestion
        rationale: Why this change is recommended (evidence-based)
        proposed_change: Specific change to make (e.g., 'strength_sessions: 2 -> 3')

    Returns confirmation with suggestion ID.
    """
    try:
        suggestion = {
            'type': suggestion_type,
            'description': description,
            'rationale': rationale,
            'proposed_change': proposed_change,
        }
        suggestion_id = save_suggestion(suggestion)
        return json.dumps({
            'status': 'pending',
            'suggestion_id': suggestion_id,
            'message': 'Suggestion saved. Awaiting user approval.'
        })
    except Exception as e:
        logger.exception("propose_suggestion failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_pending_suggestions() -> str:
    """
    Lists all pending suggestions awaiting user decision.

    Returns array of suggestions with id, type, description, and rationale.
    """
    try:
        pending = get_suggestions()
        return json.dumps(pending, indent=2)
    except Exception as e:
        logger.exception("list_pending_suggestions failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def approve_suggestion(suggestion_id: str) -> str:
    """
    Approve a pending suggestion.

    Args:
        suggestion_id: The ID of the suggestion to approve

    Returns the approved suggestion details or error if not found.
    """
    try:
        result = approve_sug(suggestion_id)
        if result:
            return json.dumps({
                'status': 'approved',
                'suggestion': result
            })
        else:
            return json.dumps({'error': f'Suggestion {suggestion_id} not found'})
    except Exception as e:
        logger.exception("approve_suggestion failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def reject_suggestion(suggestion_id: str, reason: str = None) -> str:
    """
    Reject a pending suggestion.

    Args:
        suggestion_id: The ID of the suggestion to reject
        reason: Optional reason for rejection

    Returns the rejected suggestion details or error if not found.
    """
    try:
        result = reject_sug(suggestion_id, reason)
        if result:
            return json.dumps({
                'status': 'rejected',
                'suggestion': result
            })
        else:
            return json.dumps({'error': f'Suggestion {suggestion_id} not found'})
    except Exception as e:
        logger.exception("reject_suggestion failed")
        return json.dumps({'error': str(e)})
