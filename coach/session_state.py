"""Per-process coaching session state: the SNAPSHOT FIRST mandate in code.

Mandate 1 was prompt-only. This module records when get_coaching_snapshot
last completed in this server process; prescriptive writes (plan, program,
phase, push, injury status transitions) refuse until a snapshot from TODAY,
younger than SNAPSHOT_MAX_AGE_MINUTES, has run. The snapshot is what lands
current_time_context, injuries, capacity, anomalies and memory in the
model's context; the gate guarantees it was delivered, not that it was read.

One stdio server process serves one MCP client, so the record is per client
session. Under HTTP transport it is per process (sessions share it).

Environment, read at call time:
- COACH_SNAPSHOT_GATE=0 disables the gate.
- COACH_SNAPSHOT_MAX_AGE_MIN overrides the maximum age (default 120).
- COACH_SETUP_GATE=0 disables the setup-mode check (athlete.json must exist).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

SNAPSHOT_MAX_AGE_MINUTES = 120.0

# Tools that refuse without a fresh snapshot (pinned by tests/test_snapshot_gate.py)
GATED_TOOLS = (
    'update_weekly_plan',
    'training_program',        # action != 'get'
    'push_plan_to_garmin',
    'update_phase',
    'update_injury_status',    # only status transitions; notes/severity stay open
)

_lock = threading.Lock()
_last: dict[str, Any] | None = None


def gate_enabled() -> bool:
    raw = os.environ.get('COACH_SNAPSHOT_GATE', '1').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


def max_age_minutes() -> float:
    raw = os.environ.get('COACH_SNAPSHOT_MAX_AGE_MIN', '').strip()
    try:
        return float(raw) if raw else SNAPSHOT_MAX_AGE_MINUTES
    except ValueError:
        return SNAPSHOT_MAX_AGE_MINUTES


def mark_snapshot_taken(today: date, *, degraded: bool = False) -> None:
    """Record a completed snapshot (success path only — never on an error return)."""
    global _last
    with _lock:
        _last = {'date': today.isoformat(), 'monotonic': time.monotonic(),
                 'degraded': degraded}


def snapshot_state() -> dict[str, Any] | None:
    with _lock:
        if _last is None:
            return None
        return {
            'date': _last['date'],
            'age_minutes': round((time.monotonic() - _last['monotonic']) / 60, 1),
            'degraded': _last['degraded'],
        }


def reset() -> None:
    global _last
    with _lock:
        _last = None


def setup_gate_enabled() -> bool:
    raw = os.environ.get('COACH_SETUP_GATE', '1').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


def setup_required(tool_name: str) -> dict[str, Any] | None:
    """Setup mode for installed copies: without an athlete profile the coach
    cannot plan for an athlete it does not know. Existence check only, so a
    minimal profile created in chat (update_athlete) is enough."""
    if not setup_gate_enabled():
        return None
    from . import config  # call-time: DATA_DIR can be re-pointed (tests, COACH_DATA_DIR)
    profile = Path(config.DATA_DIR) / config.ATHLETE_FILE
    if profile.exists():
        return None
    return {
        'error': 'setup_required',
        'message': (f"{tool_name} refused: no athlete profile at {profile} — the "
                    "coach cannot plan for an athlete it does not know."),
        'hint': ("Run `garmin-coach-setup` (or `python scripts/setup_wizard.py` from "
                 "a checkout), or set COACH_DATA_DIR to the folder that holds "
                 "athlete.json. In chat: get_onboarding_guide() then update_athlete()."),
        'data_dir': str(config.DATA_DIR),
    }


def require_snapshot(tool_name: str, today: date) -> dict[str, Any] | None:
    """None when the mandate is satisfied; otherwise the error dict to return.

    Also enforces setup mode first: prescriptive writes need an athlete
    profile before they need a snapshot."""
    setup_error = setup_required(tool_name)
    if setup_error:
        return setup_error
    if not gate_enabled():
        return None
    state = snapshot_state()
    hint = 'Call get_coaching_snapshot() first, then retry this call.'
    if state is None:
        return {
            'error': 'snapshot_required',
            'message': (f"{tool_name} refused: no coaching snapshot has run in this "
                        "session (SNAPSHOT FIRST mandate)."),
            'hint': hint,
        }
    limit = max_age_minutes()
    if state['date'] != today.isoformat() or state['age_minutes'] > limit:
        return {
            'error': 'snapshot_stale',
            'message': (f"{tool_name} refused: the last snapshot in this session is from "
                        f"{state['date']}, {state['age_minutes']:.0f} min ago; a "
                        f"prescriptive write needs one from today under {limit:.0f} "
                        "min old."),
            'hint': hint,
            'last_snapshot': state,
        }
    return None
