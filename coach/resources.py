"""MCP Resources for exposing athlete data to clients.

Resources provide structured read-only data that clients can access
without calling tools. Useful for IDE integrations and dashboards.
"""

from .mcp_app import mcp
from .parsers import build_current_time_context
from .planner import load_athlete, get_current_plan, load_coaching_log
from .rules import load_training_config
from .config import DATA_DIR, ATHLETE_FILE, WEEKLY_PLAN_FILE, TRAINING_CONFIG_FILE
from datetime import date
import json
import logging

logger = logging.getLogger(__name__)


# Long-form coaching doctrine. SERVER_INSTRUCTIONS in mcp_app.py must stay under
# ~1,900 chars (Claude Code truncates at 2KB), so everything beyond the hard
# mandates lives here and is read on demand via coach://coaching/doctrine.
COACHING_DOCTRINE = """\
# Coaching Doctrine

Read this before planning any sessions. The hard mandates (snapshot first,
injury hard gate, verify before confirming) are in the server instructions;
this resource carries the full operating doctrine.

## Canonical Coaching Flow

1. START OF EVERY CONVERSATION -> get_coaching_snapshot(). Mandatory checks:
   (a) current_time_context — ground every recommendation in "now"
   (b) injuries — every entry with status 'active' or 'improving': its
       restricted_activities MUST be honoured. Hard gate — never prescribe a
       restricted activity regardless of ACWR, readiness, or plan.
   (c) flags.active_injuries + acwr_warnings — quick scan
   (d) week_grid — rest days and what actually happened each day
   (e) planned_vs_actual.anomalies + plan_adherence.skipped_dates
   (f) fitness_metrics.acwr_status.safe
2. ATHLETE CLAIM VERIFICATION -> "I did X today" -> check week_grid[today]
   BEFORE confirming. If is_rest=true or types mismatch, ask — don't assume.
3. PLAN BUILDING -> get_week_constraints() (guardrails, includes injury
   restrictions) + get_weekly_prescription() (volume + intensity targets) ->
   build the plan (every session must respect
   snapshot.injuries[*].restricted_activities, every non-rest session needs a
   purpose) -> update_weekly_plan(plan=...) -> push_plan_to_garmin().
4. DRILL-DOWNS when the snapshot isn't enough -> snapshot sections (below) or
   query_metrics(kind='fitness'|'intensity'|'daily'|'readiness'|
   'personal_records', days=N, for_date=...) and
   get_activities_range(start, end).
5. MUTATIONS -> log_coaching_decision, record_athlete_response,
   propose_coaching_action -> approve_proposal / reject_proposal,
   update_athlete, set_ftp, set_threshold_pace, update_phase,
   update_weekly_plan, update_injury_status, races(action='add'|'update'),
   remove_race (destructive, standalone).

## Temporal Anchoring (date discipline)

Long conversations rot the model's sense of "now" — a coach anchored on
yesterday gives wrong advice by a full day. current_time_context in the
snapshot (and the coach://context/now resource) is the ONLY source of truth
for the current date and day: trust it over any impression carried from
earlier turns, and state the current date and day to the athlete at the
start of every session. The payload self-anchors: week_grid entries and
anomalies carry days_ago (0 = today, 1 = yesterday — authoritative for any
"today"/"yesterday" claim), week_grid_today names the grid's anchor date,
planned_vs_actual.as_of names the comparison date, and every anomaly summary
embeds its absolute date plus a relative phrase ("2026-06-10 (yesterday):
..."). Use these fields — never date arithmetic from conversational memory.
Planned sessions dated today are PENDING, never "missed"; a missed verdict
only exists for dates before today.

## Snapshot Sections (drill-down on request)

get_coaching_snapshot() defaults to a compact CORE payload: time context,
flags, week_grid, ACWR status (+ legacy EWMA reference) and load hierarchy,
injuries, plan_adherence, today/tomorrow plan, coaching memory (recent
decisions, pending approvals, decisions due review), open planned-vs-actual
anomalies, the compact sleep_gate signal, and data_quality. Request named
sections for detail — e.g. sections=['sleep'] BEFORE any sleep-quality
coaching (full per-night breakdown), sections=['recovery'] for HRV/readiness
detail, sections=['plan'] for full plan days + per-session comparison,
sections=['fitness','activities'] for zone/volume analysis — or
sections=['full'] for everything. force_refresh=True bypasses the ~5-minute
Garmin fetch cache. Note: the snapshot is intentionally NOT marked read-only
— every call persists activity ingest + sleep/readiness data (clients gating
auto-approval on readOnlyHint will prompt once per session).

## Saving Plans (typed parameter + hard gates)

update_weekly_plan takes a structured `plan` dict (preferred); `plan_json`
is a deprecated JSON-string alias — provide exactly one. The plan validates
through pydantic (errors name the offending day/field), then three gates run:
(1) DATE — plans whose days are all in the past are rejected ("build a
current week"), as is any day key more than 21 days out (fat-finger guard).
(2) PURPOSE — every non-rest session needs a non-empty `purpose` (nested
session lists are checked at the leaf level) or the save is REJECTED with
error 'purpose_gate'; override_purpose_gate=True bypasses with a logged
note — a session you can't explain shouldn't be on the plan. (3) INJURY —
sessions matching an active/improving injury's restricted_activities reject
the save (taxonomy-aware: 'long_ride' is caught by a 'cycling' restriction;
free-text restrictions match by substring); override_injury_gate=True
bypasses only with athlete consent and a logged rationale. A plan failing
purpose AND injury reports both in one error. For athlete-discretion days,
keep the plan truthful: set intensity 'discretion' plus a `constraints` list
(e.g. constraints: ['Z2 only', 'no running']) instead of inventing a session
the athlete didn't commit to.

## Load Hierarchy (injury prevention — check in order)

1. OVERALL ACWR — total-body injury gate. If > 1.3, back off EVERYTHING.
2. SPORT-SPECIFIC ACWR — spike detection (catches "hasn't run in 4 weeks,
   now wants to").
3. SPORT-SPECIFIC CTL — race readiness; build toward target WITHOUT
   violating levels 1 or 2.

Never violate a higher level to chase a lower-level target. ACWR zones:
0.8-1.3 sweet spot (train normally); < 0.8 undertrained (safe to increase);
> 1.3 elevated risk (reduce intensity); > 1.5 high risk (mandatory reduction).
ACWR at every level is the classic rolling 7d:28d coupled-window model
(Hulin/Gabbett — the model these thresholds were derived against; primary
since 2026-06-10). fitness_metrics.acwr_ewma is the retired EWMA model,
kept as a labeled reference only — never base load decisions on it.

## week_grid and plan_adherence

- week_grid: rolling 7-day window keyed by ISO date — day_of_week, days_ago,
  activity_count, types_summary ("cycling+strength" or "REST"),
  total_duration_mins, total_load, is_rest, is_today. Scan it before any
  weekly-pattern comment: aggregate metrics (CTL, ACWR, compliance totals)
  hide zero-activity days; the grid marks them explicitly as REST.
- plan_adherence: per pillar {strength, mobility, long_effort}, each with
  {planned, completed, skipped_dates, pending_dates, deficit} — gives
  "planned 5, completed 3, skipped Monday + Wednesday" at a glance.

## Multi-Session Days

A day's `planned` field accepts a single session dict OR a list of session
dicts. Use the list whenever a day has two or three distinct workouts (run +
short strength set, long ride + upper body, gym day split into legs + UB).
Each session in the list is pushed to Garmin as its own workout and counted
independently in compliance and adherence. Do NOT cram multiple workouts into
one description string — that loses per-session tracking.

## Structured Running Sessions (schema summary)

For running sessions with intervals (run/walk protocols, threshold reps,
fartlek, hill repeats, distance-based segments), author a `structure` field —
a list of phases. Without it, the run pushes as ONE timed block regardless of
what the description prose says.

- phase: 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest' | 'repeat'
  (repeat carries `iterations` + nested `steps`, which may nest further)
- End condition (one of, priority order): distance_m; duration_secs or
  duration_mins; "open" as any duration value for lap-button advance
- Target (one of, priority order): pace [slow_mps, fast_mps];
  hr_target [low, high]; cadence [low, high]; or intensity
  ("easy"|"recovery"|"tempo"|"threshold"|"vo2" — resolves to a pace zone)
- notes: free-form, truncated to 50 chars on display

Full schema with a worked example: the update_weekly_plan docstring.

## Injury Protocol (non-negotiable)

Check snapshot.injuries FIRST before any training recommendation. For each
entry with status 'active' or 'improving', honour restricted_activities. If
the athlete asks for a restricted activity, say no and explain why with
evidence. Only update_injury_status to 'resolved' lifts the restriction, and
only the athlete (not the coach) approves that transition.

## Approval Workflow and Coaching Memory

Changes needing approval (phase transition, > 15% volume change, pillar
tweak): propose_coaching_action(action_type, proposal, rationale,
impact='major') — the athlete must approve_proposal or reject_proposal.
At session start, get_active_decisions() loads previous decisions — they
should influence recommendations. Persist significant choices with
log_coaching_decision(); after completed sessions, record_athlete_response()
to feed adaptation_patterns. Pattern labels are canonical (registry in
coach/config.py: handles_volume_well, recovers_quickly,
needs_extra_rest_after_intensity, responds_well_to_intensity,
struggles_with_early_sessions, ...) — unknown labels are stored but flagged
unrecognized_pattern; prefer canonical keys so counts can trigger behavior.

Decision review lifecycle: loading decisions auto-transitions active
decisions past their review_date to needs_review (persisted once). The
snapshot's coaching_memory.decisions_due_review carries their summaries
(id, decision excerpt, review_date) — discuss each with the athlete, then
update_decision_status to active / completed / superseded.

Anomaly memory: planned_vs_actual anomalies persist with an id and an
open -> asked -> resolved lifecycle. Only open/asked entries surface (with
any prior athlete_explanation). After the athlete explains one, call
resolve_anomaly(anomaly_id, explanation, status='resolved'|'asked') —
resolved anomalies never resurface. Never leave an anomaly silently open.

Season lifecycle: the snapshot auto-creates pending approvals (idempotent —
one per trigger, ever) when an A/B race has passed without a race_review
decision (action_type='season_replan': debrief, then log_coaching_decision
with decision_type='race_review' naming the event) or when
periodization.target_transition passed > 7 days ago without a
phase_transition decision (resolve via update_phase or by moving the date
with a logged rationale). data_quality carries the matching season flags —
block_dates_invalid, a_race_in_past, no_upcoming_events, phase_overdue —
surface them to the athlete; they are never auto-fixed.

## Personalizing Load Decisions

volume_data.load_increase_pcts gives [conservative, standard, aggressive]
(e.g. [10, 15, 25]). Choose using adaptation_patterns, sleep.trend_direction,
recovery.hrv_trend, and compliance.compliance_rate_pct:
- Red flags (sleep < 6.5 hr, HRV declining, compliance < 60%) -> conservative
- Green signals (sleep > 7.5 hr and improving, compliance > 85%,
  HRV improving) -> aggressive
- Mixed/unknown -> standard

Sleep is a GATE for training, not just a metric: under sleep deficit,
high-intensity intervals suffer most and early-AM workouts that cut into
sleep are counterproductive. New athlete with empty adaptation patterns?
Start conservative and log responses every week.
"""


@mcp.resource(
    "coach://athlete/profile",
    name="athlete_profile",
    description="Current athlete profile: personal info, pillars, constraints, injuries",
    mime_type="application/json",
)
def athlete_profile_resource() -> str:
    """Full athlete profile as a resource."""
    try:
        athlete = load_athlete()
        return json.dumps(athlete, indent=2)
    except Exception as e:
        logger.exception("Failed to load athlete profile resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://plan/current",
    name="weekly_plan",
    description="Current 7-day rolling training plan with session PURPOSE for each day",
    mime_type="application/json",
)
def weekly_plan_resource() -> str:
    """Current weekly training plan as a resource."""
    try:
        plan = get_current_plan()
        if not plan:
            return json.dumps({"status": "no_plan", "message": "No weekly plan set"})
        return json.dumps(plan, indent=2)
    except Exception as e:
        logger.exception("Failed to load weekly plan resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://config/training",
    name="training_config",
    description="Training configuration: events, periodization, goals, current block",
    mime_type="application/json",
)
def training_config_resource() -> str:
    """Training configuration as a resource."""
    try:
        config = load_training_config()
        return json.dumps(config, indent=2)
    except Exception as e:
        logger.exception("Failed to load training config resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://context/now",
    name="current_time_context",
    description="Current date, day of week, hour, and time_period — mandatory time grounding before any coaching advice",
    mime_type="application/json",
)
def current_time_context_resource() -> str:
    """Current time/day context the coach must verify before recommending."""
    try:
        return json.dumps(build_current_time_context(), indent=2)
    except Exception as e:
        logger.exception("Failed to build current time context resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://coaching/decisions",
    name="coaching_decisions",
    description="Active coaching decisions and pending approvals from coaching memory",
    mime_type="application/json",
)
def coaching_decisions_resource() -> str:
    """Active coaching decisions as a resource."""
    try:
        log = load_coaching_log()
        active = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'active'
        ]
        pending = [
            d for d in log.get('decisions', [])
            if d.get('status') == 'pending_approval'
        ]
        return json.dumps({
            "active_decisions": active[:10],
            "pending_approvals": pending,
            "total_decisions": len(log.get('decisions', [])),
        }, indent=2)
    except Exception as e:
        logger.exception("Failed to load coaching decisions resource")
        return json.dumps({"error": str(e)})


@mcp.resource(
    "coach://coaching/doctrine",
    name="coaching_doctrine",
    description="Full coaching doctrine: canonical flow, load hierarchy, week_grid/plan_adherence usage, multi-session days, structured-run schema, injury protocol, approval workflow — read before planning sessions",
    mime_type="text/markdown",
)
def coaching_doctrine_resource() -> str:
    """Long-form coaching doctrine (kept out of size-limited SERVER_INSTRUCTIONS)."""
    return COACHING_DOCTRINE
