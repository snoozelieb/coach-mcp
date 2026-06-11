"""Coaching decision tools - log decisions, manage approvals, track athlete responses.

Also owns the Phase 3 memory lifecycles:
- Anomaly persistence (curiosity with memory): planned-vs-actual anomalies are
  registered once in coaching_log.json under 'anomalies' and carry an
  open -> asked -> resolved lifecycle via resolve_anomaly().
- Adaptation-pattern normalization against config.ADAPTATION_PATTERN_REGISTRY
  so recorded patterns aggregate by canonical key.
- Decision review lifecycle: active decisions past their review_date
  auto-transition to 'needs_review' whenever decisions are loaded.
- Tagged auto-proposal machinery (season lifecycle): ensure_tagged_proposal
  routes through propose_coaching_action and an event-tag registry so each
  real-world trigger (race passed, phase overdue) only ever generates ONE
  proposal — open, approved and rejected proposals all block re-creation.
"""

from ..mcp_app import mcp
from ..planner import load_coaching_log, save_coaching_log
from ..config import ADAPTATION_PATTERN_REGISTRY
from datetime import date, timedelta
import json
import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adaptation-pattern normalization (canonical registry in coach/config.py)
# ---------------------------------------------------------------------------

def normalize_adaptation_pattern(pattern) -> tuple[str | None, bool]:
    """Normalize a free-form pattern label against ADAPTATION_PATTERN_REGISTRY.

    Returns (canonical_or_normalized, recognized). Matching is case/space/
    punctuation tolerant; a UNIQUE substring match in either direction counts
    as recognized (fuzzy contains-match). Ambiguous or unknown labels return
    the normalized form with recognized=False — they are still storable, just
    flagged so the coach prefers a canonical key next time.
    """
    if pattern is None or not str(pattern).strip():
        return None, False
    norm = re.sub(r'[^a-z0-9]+', '_', str(pattern).strip().lower()).strip('_')
    if norm in ADAPTATION_PATTERN_REGISTRY:
        return norm, True
    matches = [k for k in ADAPTATION_PATTERN_REGISTRY if norm in k or k in norm]
    if len(matches) == 1:
        return matches[0], True
    return norm, False


# ---------------------------------------------------------------------------
# Anomaly persistence (curiosity with memory)
# ---------------------------------------------------------------------------

ANOMALY_ACTIVE_STATUSES = ('open', 'asked')
ANOMALY_RESOLVE_STATUSES = ('resolved', 'asked')
MAX_ANOMALY_ENTRIES = 200      # registry cap in coaching_log.json
MAX_SURFACED_ANOMALIES = 20    # cap on the open/asked view in the snapshot


def _slugify(value) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(value).strip().lower()).strip('_')


def anomaly_id_for(anomaly: dict) -> str:
    """Stable identity for a detected planned-vs-actual anomaly.

    Format '<date>:<type>:<slug>'. Built from the plan-side fields (stable
    across snapshot calls) plus the actual type, so the same real-world
    anomaly always maps to the same id — that key is what makes registration
    idempotent and lets resolved anomalies never re-register.
    """
    a_date = anomaly.get('date') or 'unknown'
    a_type = anomaly.get('flag') or anomaly.get('type') or 'unknown'
    parts = [
        anomaly.get('planned_type'),
        anomaly.get('actual_type') or anomaly.get('activity_type'),
        anomaly.get('planned_mins'),
    ]
    if a_type == 'unplanned':
        parts.append(anomaly.get('duration_mins'))
    slug = '_'.join(_slugify(p) for p in parts if p is not None)
    return f"{a_date}:{a_type}:{slug or a_type}"


def relative_day_phrase(date_iso, today: date) -> str | None:
    """'today' / 'yesterday' / 'N days ago' for an ISO date vs threaded today.

    Future dates yield 'tomorrow' / 'in N days'. None when unparseable.
    """
    try:
        delta = (today - date.fromisoformat(str(date_iso))).days
    except (TypeError, ValueError):
        return None
    if delta == 0:
        return 'today'
    if delta == 1:
        return 'yesterday'
    if delta == -1:
        return 'tomorrow'
    if delta > 1:
        return f'{delta} days ago'
    return f'in {-delta} days'


def _anomaly_summary(anomaly: dict, today: date | None = None) -> str:
    """One-line human-readable summary, anchored to the anomaly's date.

    Embeds the absolute date AND a relative phrase computed from the
    threaded today — "2026-06-10 (yesterday): Planned padel (90min) has no
    matching activity" — so a coach deep in a long conversation can never
    mistake which day an anomaly belongs to.
    """
    a_type = anomaly.get('flag', 'unknown')
    if a_type == 'missing':
        base = (f"Planned {anomaly.get('planned_type')} "
                f"({anomaly.get('planned_mins')}min) has no matching activity")
    elif a_type == 'type_mismatch':
        base = (f"Planned {anomaly.get('planned_type')}, "
                f"actual was {anomaly.get('actual_type')}")
    elif a_type == 'duration_delta':
        base = (f"{anomaly.get('planned_mins')}min planned vs "
                f"{anomaly.get('actual_mins')}min actual "
                f"({anomaly.get('delta_pct')}%)")
    elif a_type == 'unplanned':
        base = (f"Unplanned {anomaly.get('activity_type')} "
                f"({anomaly.get('duration_mins')}min")
        if anomaly.get('load') is not None:
            base += f", load {anomaly.get('load')}"
        base += ")"
        if anomaly.get('on_rest_day'):
            base += " on a rest day"
    else:
        base = a_type
    a_date = anomaly.get('date')
    if not a_date:
        return base
    phrase = relative_day_phrase(a_date, today) if today is not None else None
    if phrase:
        return f"{a_date} ({phrase}): {base}"
    return f"{a_date}: {base}"


def register_detected_anomalies(detected: list | None,
                                today: date | None = None) -> list:
    """Persist freshly detected anomalies; return the open/asked view.

    Curiosity with memory: each detected anomaly is registered ONCE
    (idempotent by id) as status 'open' under coaching_log.json['anomalies'].
    resolve_anomaly() moves entries to 'asked' (keeps surfacing, with the
    athlete's partial explanation attached) or 'resolved' (stops surfacing —
    and because the id stays in the registry, it can never re-register).

    `today` (threaded from the snapshot boundary — clock discipline) drives
    the today-is-pending guard: a 'missing' anomaly dated today or later is
    NEVER registered (the day isn't over), and any OPEN 'missing' registry
    entry dated today or later is dropped — those were written by the
    pre-fix comparison and legitimately re-register tomorrow if the session
    genuinely never happened. Summaries and the surfaced 'days_ago' fields
    are anchored to the same today.

    The returned list is every open/asked registry entry, newest date first:
    lifecycle fields (id, status, summary, athlete_explanation) merged with
    the fresh detection detail when the anomaly is still being detected.

    The date.today() here is a write-time created/updated stamp on new
    registry entries (and the fallback when `today` is not threaded) —
    allowlisted in tests/test_clock_discipline.py.
    """
    log = load_coaching_log()
    registry = log.get('anomalies', [])
    stamp_iso = date.today().isoformat()
    if today is None:
        today = date.fromisoformat(stamp_iso)
    today_iso = today.isoformat()
    changed = False

    # Today is PENDING, not missed: drop wrongly-registered open
    # missing-anomalies dated today (or later). Removing the id lets the
    # anomaly re-register tomorrow if the session truly never happened.
    kept = [
        entry for entry in registry
        if not (entry.get('type') == 'missing'
                and entry.get('status') == 'open'
                and (entry.get('date') or '') >= today_iso)
    ]
    if len(kept) != len(registry):
        registry = kept
        changed = True

    by_id = {entry.get('id') for entry in registry}

    fresh_by_id = {}
    for anomaly in detected or []:
        if (anomaly.get('flag') == 'missing'
                and (anomaly.get('date') or '') >= today_iso):
            continue  # today's sessions are pending — never register as missed
        aid = anomaly_id_for(anomaly)
        fresh_by_id[aid] = anomaly
        if aid in by_id:
            continue  # already registered (open, asked OR resolved) — never duplicate
        registry.append({
            'id': aid,
            'date': anomaly.get('date'),
            'type': anomaly.get('flag', 'unknown'),
            'summary': _anomaly_summary(anomaly, today),
            'status': 'open',
            'athlete_explanation': None,
            'created': stamp_iso,
            'updated': stamp_iso,
        })
        by_id.add(aid)
        changed = True

    if len(registry) > MAX_ANOMALY_ENTRIES:
        registry = registry[-MAX_ANOMALY_ENTRIES:]
        changed = True

    if changed:
        log['anomalies'] = registry
        save_coaching_log(log)

    surfaced = []
    for entry in registry:
        if entry.get('status') not in ANOMALY_ACTIVE_STATUSES:
            continue
        view = {k: v for k, v in entry.items() if v is not None}
        fresh = fresh_by_id.get(entry.get('id'))
        if fresh:
            for key, value in fresh.items():
                view.setdefault(key, value)
            # Keep the relative phrase current — a summary written when the
            # anomaly was 'yesterday' must not still say 'yesterday' later.
            view['summary'] = _anomaly_summary(fresh, today)
        try:
            view['days_ago'] = (today - date.fromisoformat(
                view.get('date') or '')).days
        except (TypeError, ValueError):
            pass
        surfaced.append(view)
    surfaced.sort(key=lambda a: a.get('date') or '', reverse=True)
    return surfaced[:MAX_SURFACED_ANOMALIES]


# ---------------------------------------------------------------------------
# Tagged auto-proposals (season lifecycle)
# ---------------------------------------------------------------------------
# The snapshot auto-generates season-lifecycle proposals (race passed without
# a debrief, phase transition overdue). They go through the SAME approval
# machinery as any other proposal — the athlete approve_proposal /
# reject_proposal — but carry an event_tag so re-running the detection can
# never duplicate one. approve_proposal drops the proposal payload when it
# mints the decision, so creation is also recorded in an auto_proposal_tags
# registry that outlives the proposal itself (approved/expired included).

AUTO_PROPOSAL_TAGS_KEY = 'auto_proposal_tags'


def find_tagged_proposal(log: dict, event_tag: str) -> str | None:
    """Has a proposal carrying this event_tag ever been created?

    Returns where the tag was found — 'pending' (open proposal), 'rejected'
    (athlete said no), or 'recorded' (tag registry: covers approved and
    expired proposals whose payload no longer carries the tag) — or None.
    """
    for p in log.get('pending_approvals') or []:
        if isinstance(p, dict) and p.get('event_tag') == event_tag:
            return 'pending'
    for p in log.get('rejected_proposals') or []:
        if isinstance(p, dict) and p.get('event_tag') == event_tag:
            return 'rejected'
    if event_tag in (log.get(AUTO_PROPOSAL_TAGS_KEY) or {}):
        return 'recorded'
    return None


def ensure_tagged_proposal(event_tag: str, action_type: str, proposal: str,
                           rationale: str, impact: str = 'major',
                           expires_days: int = 14) -> dict:
    """Create ONE pending approval tagged event_tag — idempotent forever.

    Routes through propose_coaching_action (the canonical approval flow),
    then stamps the proposal with the tag and records it in the
    auto_proposal_tags registry. A tag that already exists anywhere
    (open, approved, rejected, expired) blocks re-creation, so detection
    can run on every snapshot without nagging.

    The date.today() here is a write-time 'created' stamp on the tag
    registry entry, not date logic (expiry math lives in
    propose_coaching_action, a tool boundary) — allowlisted in
    tests/test_clock_discipline.py.

    Returns {'created': bool, 'event_tag': ..., 'proposal_id'|'existing': ...}.
    """
    log = load_coaching_log()
    existing = find_tagged_proposal(log, event_tag)
    if existing:
        return {'created': False, 'existing': existing, 'event_tag': event_tag}

    result = json.loads(propose_coaching_action(
        action_type=action_type, proposal=proposal, rationale=rationale,
        impact=impact, expires_days=expires_days))
    if 'error' in result:
        return {'created': False, 'error': result['error'],
                'event_tag': event_tag}

    proposal_id = result.get('proposal_id')
    log = load_coaching_log()
    for p in log.get('pending_approvals', []):
        if isinstance(p, dict) and p.get('id') == proposal_id:
            p['event_tag'] = event_tag
            p['auto_generated'] = True
            break
    log.setdefault(AUTO_PROPOSAL_TAGS_KEY, {})[event_tag] = {
        'proposal_id': proposal_id,
        'action_type': action_type,
        'created': date.today().isoformat(),
    }
    save_coaching_log(log)
    logger.info("Auto-created %s proposal %s (tag=%s)",
                action_type, proposal_id, event_tag)
    return {'created': True, 'proposal_id': proposal_id,
            'event_tag': event_tag}


# ---------------------------------------------------------------------------
# Decision review lifecycle
# ---------------------------------------------------------------------------

def auto_transition_due_decisions(today: date) -> tuple[dict, list]:
    """Flip active decisions past their review_date to 'needs_review'.

    Runs whenever decisions are loaded (get_active_decisions, the snapshot's
    coaching_memory). Each transition is persisted and logged once — the
    status flip itself makes re-runs naturally idempotent. Decisions without
    a review_date are left active (legacy entries; the due-review summary
    still surfaces them by age).

    today is required — resolve date.today() at the tool boundary and
    thread it through (clock discipline).

    Returns (coaching_log, transitioned_decisions).
    """
    log = load_coaching_log()
    transitioned = []
    for d in log.get('decisions', []):
        if d.get('status') != 'active':
            continue
        review_date = d.get('review_date')
        if not review_date:
            continue
        try:
            overdue = date.fromisoformat(review_date) < today
        except (ValueError, TypeError):
            continue
        if overdue:
            d['status'] = 'needs_review'
            d['needs_review_since'] = today.isoformat()
            transitioned.append(d)
    if transitioned:
        save_coaching_log(log)
        logger.info(
            "Auto-transitioned %d overdue decision(s) to needs_review: %s",
            len(transitioned), [d.get('id') for d in transitioned])
    return log, transitioned


def summarize_decisions_due_review(decisions: list | None,
                                   today: date,
                                   review_after_days: int = 7) -> list:
    """Compact summaries (id, decision excerpt, review_date, status) of
    decisions due a review conversation.

    Includes: status 'needs_review' (auto-transitioned past review_date),
    active decisions whose review_date is today, and — legacy heuristic for
    entries without a review_date — active decisions logged more than
    review_after_days ago.
    """
    due = []
    for d in decisions or []:
        status = d.get('status')
        include = False
        if status == 'needs_review':
            include = True
        elif status == 'active':
            review_date = d.get('review_date')
            if review_date:
                try:
                    include = date.fromisoformat(review_date) <= today
                except (ValueError, TypeError):
                    include = False
            else:
                d_date = d.get('date')
                try:
                    include = bool(d_date) and (
                        today - date.fromisoformat(d_date)).days > review_after_days
                except (ValueError, TypeError):
                    include = False
        if include:
            due.append({
                'id': d.get('id'),
                'decision': (d.get('decision') or '')[:120],
                'review_date': d.get('review_date'),
                'status': status,
            })
    due.sort(key=lambda s: s.get('review_date') or '9999-12-31')
    return due


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


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def get_active_decisions() -> str:
    """
    Get all active coaching decisions (and those overdue for review).

    Returns decisions that are currently influencing training plans.
    Use this at the start of planning to maintain continuity.

    Loading decisions also runs the review lifecycle: any active decision
    whose review_date has passed auto-transitions to 'needs_review'
    (persisted once). Discuss those with the athlete, then
    update_decision_status() them to active, completed or superseded.

    Returns:
        Active decisions, needs_review decisions, and ids due for review.
    """
    try:
        today = date.today()
        log, transitioned = auto_transition_due_decisions(today)
        decisions = log.get('decisions', [])

        active = [d for d in decisions if d.get('status') == 'active']
        needs_review = [d for d in decisions if d.get('status') == 'needs_review']

        due_for_review = [d['id'] for d in needs_review if d.get('id')]
        for d in active:
            review_date = d.get('review_date')
            if review_date:
                try:
                    if date.fromisoformat(review_date) <= today:
                        due_for_review.append(d['id'])
                except ValueError:
                    pass

        result = {
            'active_decisions': active,
            'count': len(active),
            'needs_review': needs_review,
            'due_for_review': due_for_review,
            'note': ('These decisions should influence current planning. '
                     'needs_review entries are past their review_date — '
                     'discuss with the athlete, then update_decision_status '
                     'to active, completed or superseded.'),
        }
        if transitioned:
            result['auto_transitioned_to_needs_review'] = [
                d.get('id') for d in transitioned]
        return json.dumps(result, indent=2)

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

    Resolves needs_review decisions too: after discussing an overdue decision
    with the athlete, move it back to 'active' (still applies), or to
    'completed' / 'superseded'.

    Args:
        decision_id: ID of the decision to update
        new_status: New status (active, completed, superseded, cancelled,
                    needs_review)
        outcome: Optional outcome note (what happened as a result)

    Returns:
        Confirmation of the update.
    """
    try:
        log = load_coaching_log()
        decisions = log.get('decisions', [])

        valid_statuses = ['active', 'completed', 'superseded', 'cancelled',
                          'needs_review']
        if new_status not in valid_statuses:
            return json.dumps({'error': f'Invalid status. Must be one of: {valid_statuses}'})

        for d in decisions:
            if d.get('id') == decision_id:
                d['status'] = new_status
                if outcome:
                    d['outcome'] = outcome
                d['status_updated'] = date.today().isoformat()

                # Reactivating a decision whose review_date already passed
                # would auto-transition it straight back to needs_review on
                # the next load — roll the review window forward instead.
                if new_status == 'active':
                    review_date = d.get('review_date')
                    try:
                        overdue = (review_date is not None and
                                   date.fromisoformat(review_date) <= date.today())
                    except (ValueError, TypeError):
                        overdue = True
                    if overdue:
                        d['review_date'] = (
                            date.today() + timedelta(days=7)).isoformat()
                    d.pop('needs_review_since', None)

                save_coaching_log(log)
                return json.dumps({
                    'status': 'updated',
                    'decision_id': decision_id,
                    'new_status': new_status,
                    'review_date': d.get('review_date'),
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

    The pattern is normalized against the canonical registry
    (config.ADAPTATION_PATTERN_REGISTRY: handles_volume_well,
    recovers_quickly, needs_extra_rest_after_intensity,
    responds_well_to_intensity, struggles_with_early_sessions, ...) so
    counts aggregate by canonical key. Unknown patterns are still stored but
    flagged 'unrecognized_pattern' in the response with the registry list.

    Args:
        stimulus: What training was done (e.g., "Long ride 2.5hrs Z2")
        response: How athlete responded (e.g., "Training Readiness 72 next day")
        pattern: Optional pattern identified (e.g., "handles_volume_well")
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
        stored_pattern, recognized = (None, True)
        if pattern:
            stored_pattern, recognized = normalize_adaptation_pattern(pattern)
            new_response['pattern'] = stored_pattern or pattern

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

        result = {
            'status': 'recorded',
            'message': f'Response recorded: {response}',
            'pattern': new_response.get('pattern')
        }
        if pattern and not recognized:
            result['unrecognized_pattern'] = True
            result['known_patterns'] = sorted(ADAPTATION_PATTERN_REGISTRY)
            result['note'] = ('Pattern stored as given but is not a canonical '
                              'registry key — prefer a canonical key so the '
                              'pattern can trigger coaching behavior.')
        return json.dumps(result, indent=2)

    except Exception as e:
        logger.exception("record_athlete_response failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_response_patterns() -> str:
    """
    Get identified athlete response patterns.

    Returns patterns from recorded responses to inform planning. Stored
    pattern labels are normalized against the canonical registry
    (config.ADAPTATION_PATTERN_REGISTRY) so counts aggregate by canonical
    key; non-canonical labels are counted under their normalized form and
    marked recognized=false.

    Returns:
        Pattern counts by canonical key, the registry, and recent responses.
    """
    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Aggregate by canonical pattern key
        patterns = {}
        for r in responses:
            raw = r.get('pattern')
            if not raw:
                continue
            canonical, recognized = normalize_adaptation_pattern(raw)
            entry = patterns.setdefault(canonical, {
                'count': 0,
                'last_seen': r.get('date', ''),
                'recognized': recognized,
            })
            entry['count'] += 1
            if r.get('date', '') > entry['last_seen']:
                entry['last_seen'] = r['date']

        # Get recent responses (last 10)
        recent = responses[-10:] if responses else []

        return json.dumps({
            'patterns': patterns,
            'pattern_count': len(patterns),
            'canonical_registry': ADAPTATION_PATTERN_REGISTRY,
            'recent_responses': recent,
            'note': 'Use these patterns to inform training decisions'
        }, indent=2)

    except Exception as e:
        logger.exception("get_response_patterns failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def resolve_anomaly(anomaly_id: str, explanation: str,
                    status: str = 'resolved') -> str:
    """
    Record the athlete's explanation for a planned-vs-actual anomaly.

    Anomalies surfaced by get_coaching_snapshot() carry persistent ids
    ('<date>:<type>:<slug>') and live in coaching memory until explained.
    After asking the athlete about one (Curiosity Protocol), persist their
    answer here:
    - status='resolved' (default): question answered — the anomaly stops
      surfacing and never re-registers.
    - status='asked': question raised but not fully answered — the anomaly
      keeps surfacing WITH the partial explanation attached.

    Args:
        anomaly_id: id from snapshot planned_vs_actual.anomalies[*].id
        explanation: The athlete's explanation (their words, or a summary)
        status: 'resolved' (default) or 'asked'

    Returns:
        Confirmation with the updated anomaly entry.
    """
    try:
        if status not in ANOMALY_RESOLVE_STATUSES:
            return json.dumps({
                'error': (f"Invalid status '{status}'. "
                          f"Must be one of: {list(ANOMALY_RESOLVE_STATUSES)}")
            })
        if not explanation or not str(explanation).strip():
            return json.dumps({'error': 'explanation is required — record what the athlete said'})

        log = load_coaching_log()
        registry = log.get('anomalies', [])
        entry = next((a for a in registry if a.get('id') == anomaly_id), None)
        if entry is None:
            open_ids = [a.get('id') for a in registry
                        if a.get('status') in ANOMALY_ACTIVE_STATUSES]
            return json.dumps({
                'error': f'Anomaly {anomaly_id} not found',
                'open_anomaly_ids': open_ids,
            })

        entry['status'] = status
        entry['athlete_explanation'] = str(explanation).strip()
        entry['updated'] = date.today().isoformat()
        save_coaching_log(log)

        return json.dumps({
            'status': 'updated',
            'anomaly': entry,
            'message': (f"Anomaly {status}: {entry.get('summary')}"),
        }, indent=2)

    except Exception as e:
        logger.exception("resolve_anomaly failed")
        return json.dumps({'error': str(e)})
