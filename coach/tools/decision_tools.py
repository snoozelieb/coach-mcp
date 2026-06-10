"""Coaching decision tools - log decisions, manage approvals, track athlete responses."""

from ..mcp_app import mcp
from ..planner import load_coaching_log, save_coaching_log
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': False, 'openWorldHint': False})
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


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
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


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
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


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': False, 'openWorldHint': False})
def propose_coaching_action(
    action_type: str,
    proposal: str,
    rationale: str,
    impact: str = "minor",
    expires_days: int = 7,
    proposed_change: str = None,
) -> str:
    """
    Propose a coaching change that requires the athlete's approval.

    Single canonical proposal workflow — replaces the former
    propose_major_change / propose_suggestion split. Use this any time the
    coach wants to change training state in a way the athlete should sign off on
    (phase transition, volume swing, pillar adjustment, rule change, etc.).
    For tactical tweaks that do NOT need approval, use log_coaching_decision
    directly.

    Args:
        action_type: Category key (e.g. 'phase_transition', 'volume_change',
                     'pillar_adjustment', 'goal_rebalance', 'add_constraint',
                     'skip_session', 'add_race')
        proposal: Short statement of what's being proposed
        rationale: Why — cite data/evidence
        impact: 'minor' (default) or 'major' — major flags phase changes, large
                volume swings, goal rebalancing. Surfaces to the athlete with
                extra emphasis.
        expires_days: Days until the proposal auto-expires (default 7)
        proposed_change: Optional specific config change
                         (e.g. 'strength_sessions: 2 -> 3')

    Returns:
        Proposal ID for the athlete to approve or reject.
    """
    try:
        log = load_coaching_log()

        if 'pending_approvals' not in log:
            log['pending_approvals'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        proposal_count = len(log['pending_approvals'])
        proposal_id = f"p_{date.today().strftime('%Y%m%d')}_{proposal_count + 1:03d}"

        new_proposal = {
            'id': proposal_id,
            'proposed_date': date.today().isoformat(),
            'action_type': action_type,
            'proposal': proposal,
            'rationale': rationale,
            'impact': impact,
            'expires': (date.today() + timedelta(days=expires_days)).isoformat(),
        }
        if proposed_change:
            new_proposal['proposed_change'] = proposed_change

        log['pending_approvals'].append(new_proposal)
        save_coaching_log(log)

        return json.dumps({
            'status': 'proposed',
            'proposal_id': proposal_id,
            'message': f'Proposal awaiting approval: {proposal}',
            'expires': new_proposal['expires'],
            'action_required': 'Athlete must approve_proposal or reject_proposal',
        }, indent=2)

    except Exception as e:
        logger.exception("propose_coaching_action failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
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
            'instructions': 'Use approve_proposal(id) or reject_proposal(id, reason) to act on proposals',
        }, indent=2)

    except Exception as e:
        logger.exception("list_pending_approvals failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def approve_proposal(proposal_id: str) -> str:
    """
    Approve a pending coaching proposal — the change becomes an active decision.

    Args:
        proposal_id: ID of the proposal to approve

    Returns:
        Confirmation and the new active decision.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])
        decisions = log.get('decisions', [])

        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        decision_count = len([d for d in decisions if d.get('date') == date.today().isoformat()])
        decision_id = f"d_{date.today().strftime('%Y%m%d')}_{decision_count + 1:03d}"

        new_decision = {
            'id': decision_id,
            'date': date.today().isoformat(),
            'type': found.get('action_type') or found.get('type'),
            'decision': found['proposal'],
            'rationale': found['rationale'],
            'status': 'active',
            'outcome': None,
            'review_date': (date.today() + timedelta(days=14)).isoformat(),
            'approved_from': proposal_id,
        }
        if found.get('proposed_change'):
            new_decision['proposed_change'] = found['proposed_change']

        decisions.append(new_decision)
        log['pending_approvals'] = pending
        log['decisions'] = decisions
        save_coaching_log(log)

        return json.dumps({
            'status': 'approved',
            'proposal_id': proposal_id,
            'decision_id': decision_id,
            'message': f'Approved: {found["proposal"]}',
            'now_active': True,
        }, indent=2)

    except Exception as e:
        logger.exception("approve_proposal failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def reject_proposal(proposal_id: str, reason: str = None) -> str:
    """
    Reject a pending coaching proposal.

    Args:
        proposal_id: ID of the proposal to reject
        reason: Optional reason for rejection (helps the coach learn)

    Returns:
        Confirmation of rejection.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])

        if 'rejected_proposals' not in log:
            log['rejected_proposals'] = []

        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        found['rejected_date'] = date.today().isoformat()
        found['rejection_reason'] = reason
        log['rejected_proposals'].append(found)
        log['pending_approvals'] = pending
        save_coaching_log(log)

        return json.dumps({
            'status': 'rejected',
            'proposal_id': proposal_id,
            'reason': reason,
            'message': f'Rejected: {found["proposal"]}',
        }, indent=2)

    except Exception as e:
        logger.exception("reject_proposal failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': False, 'openWorldHint': False})
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


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
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
