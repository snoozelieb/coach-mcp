"""Coaching decision tools - log decisions, manage approvals, track athlete responses."""

from mcp_app import mcp
from planner import load_coaching_log, save_coaching_log
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool()
def log_coaching_decision(
    decision_type: str,
    decision: str,
    rationale: str,
    review_days: int = 7
) -> str:
    """
    Log a coaching decision for persistence across sessions.

    Use this to record significant coaching decisions that should influence
    future planning. Examples: volume adjustments, exercise modifications,
    phase-related changes.

    Args:
        decision_type: Category of decision (load_adjustment, exercise_selection,
                       intensity_change, recovery_protocol, injury_accommodation)
        decision: What was decided
        rationale: Why this decision was made (cite data)
        review_days: Days until this decision should be reviewed (default 7)

    Returns:
        Confirmation with the decision ID.
    """
    try:
        log = load_coaching_log()

        # Ensure structure exists
        if 'decisions' not in log:
            log['decisions'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        # Generate ID
        decision_count = len([d for d in log['decisions'] if d['date'] == date.today().isoformat()])
        decision_id = f"d_{date.today().strftime('%Y%m%d')}_{decision_count + 1:03d}"

        new_decision = {
            'id': decision_id,
            'date': date.today().isoformat(),
            'type': decision_type,
            'decision': decision,
            'rationale': rationale,
            'status': 'active',
            'outcome': None,
            'review_date': (date.today() + timedelta(days=review_days)).isoformat()
        }

        log['decisions'].append(new_decision)
        save_coaching_log(log)

        return json.dumps({
            'status': 'logged',
            'decision_id': decision_id,
            'message': f'Decision logged: {decision}',
            'review_date': new_decision['review_date']
        }, indent=2)

    except Exception as e:
        logger.exception("log_coaching_decision failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_active_decisions() -> str:
    """
    Get all active coaching decisions.

    Returns decisions that are currently influencing training plans.
    Use this at the start of planning to maintain continuity.

    Returns:
        List of active decisions with their rationale and review dates.
    """
    try:
        log = load_coaching_log()
        decisions = log.get('decisions', [])

        # Filter for active decisions
        active = [d for d in decisions if d.get('status') == 'active']

        # Also get decisions due for review
        today = date.today()
        due_for_review = []
        for d in active:
            review_date = d.get('review_date')
            if review_date:
                try:
                    review = date.fromisoformat(review_date)
                    if review <= today:
                        due_for_review.append(d['id'])
                except ValueError:
                    pass

        return json.dumps({
            'active_decisions': active,
            'count': len(active),
            'due_for_review': due_for_review,
            'note': 'These decisions should influence current planning'
        }, indent=2)

    except Exception as e:
        logger.exception("get_active_decisions failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_decision_status(
    decision_id: str,
    new_status: str,
    outcome: str = None
) -> str:
    """
    Update the status of a coaching decision.

    Args:
        decision_id: ID of the decision to update
        new_status: New status (active, completed, superseded, cancelled)
        outcome: Optional outcome note (what happened as a result)

    Returns:
        Confirmation of the update.
    """
    try:
        log = load_coaching_log()
        decisions = log.get('decisions', [])

        valid_statuses = ['active', 'completed', 'superseded', 'cancelled']
        if new_status not in valid_statuses:
            return json.dumps({'error': f'Invalid status. Must be one of: {valid_statuses}'})

        for d in decisions:
            if d.get('id') == decision_id:
                d['status'] = new_status
                if outcome:
                    d['outcome'] = outcome
                d['status_updated'] = date.today().isoformat()

                save_coaching_log(log)
                return json.dumps({
                    'status': 'updated',
                    'decision_id': decision_id,
                    'new_status': new_status,
                    'outcome': outcome
                }, indent=2)

        return json.dumps({'error': f'Decision {decision_id} not found'})

    except Exception as e:
        logger.exception("update_decision_status failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def propose_major_change(
    change_type: str,
    proposal: str,
    rationale: str,
    impact: str = "high"
) -> str:
    """
    Propose a major coaching change that requires user approval.

    Use this for significant changes like phase transitions, large volume
    adjustments, or goal rebalancing. The user must approve before these
    become active.

    Args:
        change_type: Type of change (phase_transition, volume_change_major,
                     goal_rebalance, skip_session, add_race)
        proposal: What change is being proposed
        rationale: Why this change is recommended (cite data)
        impact: Impact level (high, medium)

    Returns:
        Proposal ID for user to approve/reject.
    """
    try:
        log = load_coaching_log()

        if 'pending_approvals' not in log:
            log['pending_approvals'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        # Generate ID
        proposal_count = len(log['pending_approvals'])
        proposal_id = f"p_{date.today().strftime('%Y%m%d')}_{proposal_count + 1:03d}"

        new_proposal = {
            'id': proposal_id,
            'proposed_date': date.today().isoformat(),
            'type': change_type,
            'proposal': proposal,
            'rationale': rationale,
            'impact': impact,
            'expires': (date.today() + timedelta(days=3)).isoformat()
        }

        log['pending_approvals'].append(new_proposal)
        save_coaching_log(log)

        return json.dumps({
            'status': 'proposed',
            'proposal_id': proposal_id,
            'message': f'Proposal awaiting approval: {proposal}',
            'expires': new_proposal['expires'],
            'action_required': 'User must approve or reject this change'
        }, indent=2)

    except Exception as e:
        logger.exception("propose_major_change failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_pending_approvals() -> str:
    """
    List all pending coaching change proposals.

    Returns:
        List of proposals awaiting user approval.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])

        # Filter out expired proposals
        today = date.today()
        active_pending = []
        expired = []
        for p in pending:
            expires = p.get('expires')
            if expires:
                try:
                    exp_date = date.fromisoformat(expires)
                    if exp_date < today:
                        expired.append(p['id'])
                        continue
                except ValueError:
                    pass
            active_pending.append(p)

        return json.dumps({
            'pending_approvals': active_pending,
            'count': len(active_pending),
            'expired': expired,
            'instructions': 'Use approve_coaching_change(id) or reject_coaching_change(id, reason) to act on proposals'
        }, indent=2)

    except Exception as e:
        logger.exception("list_pending_approvals failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def approve_coaching_change(proposal_id: str) -> str:
    """
    Approve a pending coaching change proposal.

    The approved change becomes an active decision.

    Args:
        proposal_id: ID of the proposal to approve

    Returns:
        Confirmation and the new active decision.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])
        decisions = log.get('decisions', [])

        # Find the proposal
        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        # Convert to active decision
        decision_count = len([d for d in decisions if d['date'] == date.today().isoformat()])
        decision_id = f"d_{date.today().strftime('%Y%m%d')}_{decision_count + 1:03d}"

        new_decision = {
            'id': decision_id,
            'date': date.today().isoformat(),
            'type': found['type'],
            'decision': found['proposal'],
            'rationale': found['rationale'],
            'status': 'active',
            'outcome': None,
            'review_date': (date.today() + timedelta(days=14)).isoformat(),
            'approved_from': proposal_id
        }

        decisions.append(new_decision)
        log['pending_approvals'] = pending
        log['decisions'] = decisions
        save_coaching_log(log)

        return json.dumps({
            'status': 'approved',
            'proposal_id': proposal_id,
            'decision_id': decision_id,
            'message': f'Approved: {found["proposal"]}',
            'now_active': True
        }, indent=2)

    except Exception as e:
        logger.exception("approve_coaching_change failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def reject_coaching_change(proposal_id: str, reason: str = None) -> str:
    """
    Reject a pending coaching change proposal.

    Args:
        proposal_id: ID of the proposal to reject
        reason: Optional reason for rejection (helps LLM learn)

    Returns:
        Confirmation of rejection.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])

        if 'rejected_proposals' not in log:
            log['rejected_proposals'] = []

        # Find and remove the proposal
        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        # Archive to rejected
        found['rejected_date'] = date.today().isoformat()
        found['rejection_reason'] = reason
        log['rejected_proposals'].append(found)
        log['pending_approvals'] = pending
        save_coaching_log(log)

        return json.dumps({
            'status': 'rejected',
            'proposal_id': proposal_id,
            'reason': reason,
            'message': f'Rejected: {found["proposal"]}'
        }, indent=2)

    except Exception as e:
        logger.exception("reject_coaching_change failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def record_athlete_response(
    stimulus: str,
    response: str,
    pattern: str = None,
    load_change_pct: float = None,
    compliance_result: bool = None,
    readiness_delta: float = None,
    injury_flag: bool = None,
    session_purpose_achieved: bool = None
) -> str:
    """
    Record how the athlete responded to a training stimulus.

    Use this to track adaptation patterns that inform future planning.
    Include numeric fields when available — they enable quantified
    adaptation thresholds over time.

    Args:
        stimulus: What training was done (e.g., "Long ride 2.5hrs Z2")
        response: How athlete responded (e.g., "Training Readiness 72 next day")
        pattern: Optional pattern identified (e.g., "Responds well to long Z2")
        load_change_pct: Week-over-week load change % when this stimulus occurred
        compliance_result: Did the athlete complete the prescribed session?
        readiness_delta: Change in readiness score (next day - day before)
        injury_flag: Did this stimulus trigger injury/pain?
        session_purpose_achieved: Was the session's intended purpose met?

    Returns:
        Confirmation of recorded response.
    """
    try:
        log = load_coaching_log()

        if 'athlete_responses' not in log:
            log['athlete_responses'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        new_response = {
            'date': date.today().isoformat(),
            'stimulus': stimulus,
            'response': response
        }
        if pattern:
            new_response['pattern'] = pattern

        # Numeric fields for quantified adaptation (optional)
        if load_change_pct is not None:
            new_response['load_change_pct'] = load_change_pct
        if compliance_result is not None:
            new_response['compliance_result'] = compliance_result
        if readiness_delta is not None:
            new_response['readiness_delta'] = readiness_delta
        if injury_flag is not None:
            new_response['injury_flag'] = injury_flag
        if session_purpose_achieved is not None:
            new_response['session_purpose_achieved'] = session_purpose_achieved

        log['athlete_responses'].append(new_response)

        # Keep only last 200 responses (supports long-term pattern analysis)
        log['athlete_responses'] = log['athlete_responses'][-200:]

        save_coaching_log(log)

        return json.dumps({
            'status': 'recorded',
            'message': f'Response recorded: {response}',
            'pattern': pattern
        }, indent=2)

    except Exception as e:
        logger.exception("record_athlete_response failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_response_patterns() -> str:
    """
    Get identified athlete response patterns.

    Returns patterns from recorded responses to inform planning.

    Returns:
        List of patterns and recent responses.
    """
    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Extract patterns
        patterns = {}
        for r in responses:
            pattern = r.get('pattern')
            if pattern:
                if pattern not in patterns:
                    patterns[pattern] = {'count': 0, 'last_seen': r['date']}
                patterns[pattern]['count'] += 1
                if r['date'] > patterns[pattern]['last_seen']:
                    patterns[pattern]['last_seen'] = r['date']

        # Get recent responses (last 10)
        recent = responses[-10:] if responses else []

        return json.dumps({
            'patterns': patterns,
            'pattern_count': len(patterns),
            'recent_responses': recent,
            'note': 'Use these patterns to inform training decisions'
        }, indent=2)

    except Exception as e:
        logger.exception("get_response_patterns failed")
        return json.dumps({'error': str(e)})
