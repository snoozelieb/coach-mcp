"""MCP Prompt templates for coaching workflows.

Registers reusable prompt templates that MCP clients can invoke
for structured coaching interactions.
"""

from .mcp_app import mcp
from .planner import load_athlete
from .rules import load_training_config, get_upcoming_events
from fastmcp.prompts import PromptResult, Message
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


@mcp.prompt(
    name="weekly_planning",
    description="Structured prompt for building next week's training plan. "
    "Gathers athlete context, current phase, and constraints.",
)
def weekly_planning_prompt(notes: str = "") -> PromptResult:
    """Build next week's training plan with full coaching context."""
    athlete = load_athlete()
    training_config = load_training_config()

    today = date.today()
    next_monday = today + timedelta(days=(7 - today.weekday()))
    events = get_upcoming_events()

    # Build context summary
    personal = athlete.get('personal', {})
    pillars = athlete.get('training_pillars', {})
    constraints = athlete.get('life_constraints', {})
    injuries = [
        i for i in athlete.get('injury_history', [])
        if i.get('status') in ('active', 'improving')
    ]
    current_block = training_config.get('current_block', {})

    context = {
        'athlete_name': personal.get('name', 'Athlete'),
        'planning_week': next_monday.isoformat(),
        'current_phase': current_block.get('phase', 'unknown'),
        'pillars': pillars,
        'blocked_days': constraints.get('blocked_days', []),
        'preferred_times': constraints.get('preferred_times', {}),
        'active_injuries': injuries,
        'upcoming_events': events[:3],
        'notes': notes,
    }

    return PromptResult(
        messages=[
            Message(
                f"Plan the training week starting {next_monday.isoformat()} for "
                f"{context['athlete_name']}.\n\n"
                f"## Context\n"
                f"```json\n{json.dumps(context, indent=2)}\n```\n\n"
                f"## Instructions\n"
                f"1. Call get_coaching_snapshot() FIRST. Check `current_time_context` "
                f"at the top — ground every planning decision in the current date, "
                f"day of week, and time_period.\n"
                f"2. Call get_week_constraints() for structured planning constraints\n"
                f"3. Check the load hierarchy (overall ACWR > sport ACWR > sport CTL)\n"
                f"4. Build the plan respecting blocked days and injury restrictions\n"
                f"5. Each session needs a PURPOSE (why this session, this day)\n"
                f"6. Use update_weekly_plan(plan=...) to save — it takes a structured "
                f"plan dict (not a JSON string), and every non-rest session needs a "
                f"purpose string. The response lists `purpose_warnings` for sessions "
                f"missing one — fix them before pushing to Garmin.\n"
                f"7. Log significant decisions with log_coaching_decision()"
            )
        ],
        description="Weekly planning prompt with athlete context",
    )


@mcp.prompt(
    name="morning_brief",
    description="Daily coaching check-in. Reviews yesterday, today's plan, "
    "and readiness. Use at the start of each day.",
)
def morning_brief_prompt() -> PromptResult:
    """Generate a morning coaching brief for today."""
    return PromptResult(
        messages=[
            Message(
                "Generate a coaching brief grounded in the current time.\n\n"
                "## Your task\n"
                "1. Call get_coaching_snapshot() — the first key is "
                "`current_time_context`. Greet the athlete appropriate to "
                "the time_period ('Good morning' only if morning; otherwise "
                "match the period honestly).\n"
                "2. Check yesterday's planned vs actual — investigate any anomalies\n"
                "3. Review today's planned session and readiness/recovery. If "
                "time_period is evening or night and today's session is "
                "already done, shift the brief toward recovery and tomorrow.\n"
                "4. Check sleep data — is the athlete recovered enough for today's plan?\n"
                "5. Adapt today's session if needed (readiness gate)\n\n"
                "## Output format\n"
                "- **Time-appropriate greeting** referencing day and time_period\n"
                "- **Yesterday**: What happened vs what was planned (flag anomalies)\n"
                "- **Today's session**: What's planned and why (note if already completed)\n"
                "- **Readiness check**: Score, sleep, recovery — is today's plan appropriate?\n"
                "- **Adjustments**: Any modifications needed based on current state\n"
                "- **One thing to focus on**: The single most important thing for the rest of today"
            )
        ],
        description="Coaching brief grounded in current time",
    )


@mcp.prompt(
    name="injury_assessment",
    description="Structured injury assessment workflow. Guides through "
    "diagnosis, severity, and training modifications.",
)
def injury_assessment_prompt(body_region: str, description: str = "") -> PromptResult:
    """Assess an injury and determine training modifications."""
    return PromptResult(
        messages=[
            Message(
                f"The athlete is reporting an issue with their {body_region}.\n"
                f"{'Description: ' + description if description else ''}\n\n"
                f"## Assessment workflow\n"
                f"0. Call get_coaching_snapshot() first and check "
                f"`current_time_context` — a niggle reported at 5am is different "
                f"from one reported mid-training.\n"
                f"1. Ask clarifying questions about:\n"
                f"   - When it started (acute vs chronic)\n"
                f"   - Pain level (1-10) and type (sharp, dull, burning)\n"
                f"   - What makes it worse/better\n"
                f"   - Impact on daily activities\n"
                f"2. Call diagnose_injury() with the body region and symptoms\n"
                f"3. Call research_injury() for clinical evidence\n"
                f"4. Determine severity and training modifications\n"
                f"5. Update injury status with update_injury_status()\n"
                f"6. Modify the weekly plan if needed\n\n"
                f"## Key principles\n"
                f"- If in doubt about severity, be CONSERVATIVE\n"
                f"- Never prescribe through pain that could worsen injury\n"
                f"- Suggest professional evaluation for anything beyond mild\n"
                f"- Log the decision with log_coaching_decision()"
            )
        ],
        description="Injury assessment workflow",
    )


@mcp.prompt(
    name="week_review",
    description="End-of-week review and adaptation. Analyzes compliance, "
    "load response, and plans adjustments for next week.",
)
def week_review_prompt() -> PromptResult:
    """Review the training week and plan adaptations."""
    return PromptResult(
        messages=[
            Message(
                "## End-of-week coaching review\n\n"
                "1. Call get_coaching_snapshot() for current state. Verify "
                "`current_time_context` — confirm it's actually end-of-week "
                "(day_of_week Sunday/Monday, or the athlete's review day).\n"
                "2. Call get_compliance_report() for detailed pillar analysis\n"
                "3. Call query_metrics(kind='intensity') for zone analysis (or "
                "request snapshot sections=['fitness', 'activities'])\n\n"
                "## Review checklist\n"
                "- **Compliance**: What was hit, what was missed, and WHY?\n"
                "- **Load response**: How did the athlete handle this week's load?\n"
                "- **Sleep & recovery**: Trends over the week\n"
                "- **Key sessions**: Were the important ones completed with quality?\n"
                "- **Anomalies**: Anything unusual that needs discussion?\n\n"
                "## Adaptation decisions\n"
                "- Record athlete response: call record_athlete_response() with load/compliance data\n"
                "- Should volume change next week? Check adaptation_patterns\n"
                "- Any pillar adjustments needed?\n"
                "- Log decisions with log_coaching_decision()\n\n"
                "## Output\n"
                "- Summary of the week (what went well, what didn't)\n"
                "- Specific adaptation recommendations for next week\n"
                "- One thing to celebrate, one thing to improve"
            )
        ],
        description="End-of-week review and adaptation prompt",
    )


@mcp.prompt(
    name="onboarding",
    description="New athlete onboarding conversation. Discovers goals, "
    "constraints, and history to prescribe a training approach.",
)
def onboarding_prompt() -> PromptResult:
    """Guide through new athlete onboarding."""
    return PromptResult(
        messages=[
            Message(
                "## New athlete onboarding\n\n"
                "You are onboarding a new athlete. Your job is to UNDERSTAND them "
                "deeply, then PRESCRIBE their training approach with authority.\n\n"
                "### Phase 0: Ground the conversation\n"
                "Read the `coach://context/now` resource (or call "
                "get_coaching_snapshot() and check `current_time_context`) so "
                "your greeting, scheduling suggestions, and first-session "
                "timing match the current day and time_period.\n\n"
                "### Phase 1: Understand\n"
                "Ask about (one area at a time, conversationally):\n"
                "- Sports and training background\n"
                "- Current fitness level and recent training\n"
                "- Goals — what does success look like?\n"
                "- Events or races they're targeting\n"
                "- Injuries, limitations, health concerns\n"
                "- Life constraints (work schedule, family, travel)\n"
                "- Available training hours per week (be realistic)\n"
                "- Equipment and facilities access\n\n"
                "### Phase 2: Prescribe\n"
                "Based on what you learned:\n"
                "1. Call refresh_athlete_baseline() to pull Garmin data\n"
                "2. Tell them what their training pillars will be and WHY\n"
                "3. Use update_athlete() to save their profile\n"
                "4. If they have races, use races(action='add', ...) to register them\n"
                "5. Build their first weekly plan\n\n"
                "### Key principle\n"
                "Don't offer a menu of options. You are the expert — "
                "TELL them what they need based on evidence and experience."
            )
        ],
        description="New athlete onboarding prompt",
    )


@mcp.prompt(
    name="race_program",
    description="Build a multi-week race program anchored to the athlete's "
    "demonstrated capacity — never to a textbook template. Saves via "
    "training_program so the server can gate it.",
)
def race_program_prompt(race_name: str = "", notes: str = "") -> PromptResult:
    """Build a capacity-anchored multi-week program for a target race."""
    return PromptResult(
        messages=[
            Message(
                f"Build a multi-week training program"
                f"{' for ' + race_name if race_name else ''} with the "
                f"athlete.{' Notes: ' + notes if notes else ''}\n\n"
                f"## Ground it first (VERIFY BEFORE PRESCRIBING)\n"
                f"1. get_coaching_snapshot() — check current_time_context, "
                f"injuries (hard gate: nothing in restricted_activities, "
                f"ever), fitness_metrics, and especially `capacity`: the "
                f"athlete's demonstrated per-sport recent sessions.\n"
                f"2. query_metrics(kind='sport_history', sport=..., days=90) "
                f"— the actual session list (durations, distances, paces). "
                f"THIS is the program's anchor: week 1 starts at the "
                f"demonstrated longest recent session, never at a textbook "
                f"floor. Older history: get_activities_range.\n"
                f"3. races(action='list') — confirm the target event exists "
                f"(add it if not); note priority and weeks until race day.\n\n"
                f"## Design the program\n"
                f"4. Ladder the key session ~10-15%/week from the "
                f"demonstrated anchor; place recovery weeks and a taper; "
                f"respect the load hierarchy (overall ACWR > sport ACWR > "
                f"sport CTL) and every active injury restriction. If week 1 "
                f"must sit BELOW demonstrated capacity (rehab, bone-load "
                f"protocol), that is a deliberate regression — say why, and "
                f"record it in capacity_anchor.rationale_if_below.\n"
                f"5. Discuss the shape with the athlete — fixed weekly slots, "
                f"life constraints, review checkpoints (physio, test weeks).\n\n"
                f"## Save and execute\n"
                f"6. training_program(action='set', program=...) — include "
                f"the REQUIRED capacity_anchor (sport, "
                f"demonstrated_longest_session_mins from step 2, source). "
                f"The server verifies the anchor against history and rejects "
                f"claims the data contradicts.\n"
                f"7. Derive week 1 into the rolling plan via "
                f"update_weekly_plan (purpose per session; address every "
                f"grounding_warnings entry), then push_plan_to_garmin().\n"
                f"8. log_coaching_decision() for the program decision; set "
                f"review checkpoints as decisions with review_date."
            )
        ],
        description="Capacity-anchored race program builder",
    )


@mcp.prompt(
    name="author_session",
    description="Author ONE Garmin-ready plan session from a brief: the sport "
    "procedure (runs, rides, strength/rehab, mobility), the session contract, "
    "and the preview loop — preview_workout until the rendering matches the "
    "brief, then update_weekly_plan.",
)
def author_session_prompt(sport: str = "", brief: str = "") -> PromptResult:
    """The authoring procedure the sport skills carry, for clients without skills."""
    sport_word = (sport or "").strip().lower()
    sport_notes = {
        "running": (
            "RUNS: a plain easy/long run is duration_mins + intensity (the renderer "
            "draws warm-up, main, cool-down). Anything with reps — intervals, "
            "run/walk ladders, tempo blocks, hills, strides — is a 'structure' list: "
            "warmup, repeat {iterations, steps}, cooldown; each phase ONE end "
            "condition (duration_secs/duration_mins/distance_m or \"open\") and ONE "
            "target (intensity word, hr_target, pace_sec_per_km). Pace targets need "
            "threshold pace on the profile (set_threshold_pace); without it an "
            "intensity word resolves to HR. Return-to-run work is a ladder of short "
            "run/walk repeats sized from capacity.running and the last recorded response, "
            "advanced only when the current level was completed pain-free (the "
            "running-session-author skill holds the ladder and the one progression rule), "
            "every step cued in notes."),
        "cycling": (
            "RIDES: an outdoor endurance ride is ONE block from duration_mins + "
            "intensity (HR). Structured work (tempo climbs, sweet spot, threshold, "
            "VO2, cadence drills) is a 'structure' list; indoors (type wattbike / "
            "indoor_cycling) targets are power: power_pct [lo, hi] of FTP (set_ftp), "
            "power_watts, or an intensity word that resolves to the power zone; "
            "outdoors hr_target or an intensity word. Repeats nest; 30 s efforts "
            "are duration_secs; a self-paced warm-up is duration_mins \"open\". FTP "
            "tests are type ftp_test (the protocol is built in)."),
        "strength": (
            "STRENGTH / REHAB: every movement is an 'exercises' entry: name (Garmin "
            "key from list_exercises; spaces are normalised), sets, reps or "
            "duration_secs (holds), each_side, rest_secs (timed rest; lap button "
            "when omitted), weight_kg (explicit only — look at get_strength_baseline "
            "and write the number; nothing is guessed — the save warns when the "
            "baseline records a load the plan omits), notes (the physio's cue, first "
            "50 characters show; without notes the researched library cue is used and "
            "flagged). Off-catalogue names need a category; the watch then shows the "
            "category plus the readable name — prefer that over a catalogue key that "
            "names the movement the athlete must avoid (a JUMP key for a no-jump "
            "athlete). Supersets and Tabata are circuits {rounds, exercises, "
            "rest_secs}. State warmup_mins before loaded compounds (the save warns "
            "when it is missing). Sets written in the description without an "
            "exercises list are an error."),
        "mobility": (
            "MOBILITY: yoga, stretching and mobility are ONE timed block (type yoga / "
            "stretching, duration_mins, notes listing the flow). Rehab tracked as "
            "pilates (type rehab) takes exercises with reps, duration_secs or "
            "neither (lap button) and notes."),
    }
    sport_text = sport_notes.get(sport_word) or "\n".join(sport_notes.values())
    return PromptResult(
        messages=[
            Message(
                f"Author one plan session{' (' + sport_word + ')' if sport_word else ''}."
                f"{' Brief: ' + brief if brief else ''}\n\n"
                f"## Procedure\n"
                f"1. get_coaching_snapshot() first — current_time_context, injuries "
                f"(hard gate), capacity for this sport (VERIFY BEFORE PRESCRIBING: "
                f"query_metrics(kind='sport_history') for the session list).\n"
                f"2. Compose the session as a dict: type, purpose, duration_mins, "
                f"intensity, name (40 chars), description, and structure / exercises "
                f"per the contract. Say what you want; nothing is defaulted.\n"
                f"3. preview_workout(session) — read the 'steps' tree (what the watch "
                f"shows), fix every error (the message names the allowed keys) and "
                f"weigh every warning. preview_workout() alone returns the contract.\n"
                f"4. Repeat 2-3 until the rendering matches the brief, then save with "
                f"update_weekly_plan(plan=...) and push with push_plan_to_garmin() "
                f"(preview, show the athlete the steps, confirm).\n\n"
                f"## Sport notes\n{sport_text}"
            )
        ],
        description="Session authoring: contract + preview loop",
    )
