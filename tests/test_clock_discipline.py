"""Clock discipline lint (Phase 4.3) — ban naked wall-clock reads in coach/.

The date-rot bug class: helpers that call date.today()/datetime.now()
internally make behavior depend on when the code runs. Tests seeded with
hardcoded dates silently rot past those internal filters (this bit the suite
four times before Phase 4). The fix is clock injection: resolve the clock
ONCE at a tool boundary (@mcp.tool / @mcp.prompt / @mcp.resource) and thread
`today: date` (or `now: datetime`) through every helper.

This module is the enforcement: an AST scan over coach/**/*.py that FAILS
when date.today() / datetime.now() / datetime.utcnow() / datetime.today()
appears outside the explicit ALLOWLIST below. Every allowlist entry is a
(file, top-level function) pair with a justification. Entries that stop
matching any call fail too — the list can only shrink truthfully.

Limits (documented, accepted): the scan matches calls on the bare names
`date` and `datetime`; aliased imports (e.g. `from datetime import date as
d`) would evade it. Nobody in coach/ does that — and a reviewer adding an
alias to dodge a lint has bigger problems.
"""
import ast
from pathlib import Path

COACH_DIR = Path(__file__).resolve().parent.parent / 'coach'

# (module attribute pairs that read the wall clock)
BANNED_CALLS = {
    ('date', 'today'),
    ('datetime', 'now'),
    ('datetime', 'utcnow'),
    ('datetime', 'today'),
}

# ---------------------------------------------------------------------------
# ALLOWLIST — (posix path relative to repo root, top-level function name)
#
# Three legitimate categories:
#   [boundary]  @mcp.tool / @mcp.prompt function bodies: the ONE place a
#               default "today" is resolved before threading into helpers.
#   [stamp]     write-time audit stamps at the storage layer (created /
#               updated / last_updated fields) — wall-clock metadata, not
#               date logic; nothing filters or branches on them.
#   [bare-call] helpers invoked with no arguments from modules outside the
#               clock-discipline refactor's ownership (resources.py,
#               prompts.py, scripts/daily_loop.py) — they keep an internal
#               default-resolution line and accept injection for tests.
# ---------------------------------------------------------------------------
ALLOWLIST = {
    # [stamp] last_updated audit stamp on every fitness_history save
    ('coach/fitness.py', 'save_fitness_history'),
    # [bare-call] canonical clock reader (now= injection seam); called bare
    # by the coach://context/now resource (coach/resources.py) and
    # interactive_check_in — it IS the boundary that turns wall-clock into
    # coaching context
    ('coach/parsers.py', 'build_current_time_context'),
    # [stamp] metadata.last_updated audit stamp on every coaching_log save
    ('coach/planner.py', 'save_coaching_log'),
    # [bare-call] storage boundary; scripts/daily_loop.py calls it bare —
    # resolves today once, threads it into _prune_and_archive_plan_days
    ('coach/planner.py', 'save_weekly_plan'),
    # [boundary] @mcp.prompt — weekly planning prompt resolves its own today
    ('coach/prompts.py', 'weekly_planning_prompt'),
    # [bare-call] the weekly_planning @mcp.prompt calls this bare; tool
    # callers thread today= explicitly
    ('coach/rules.py', 'get_upcoming_events'),
    # [boundary] @mcp.tool bodies
    ('coach/tools/athlete_tools.py', 'update_athlete'),
    ('coach/tools/athlete_tools.py', 'analyze_ftp_test'),
    ('coach/tools/coaching_tools.py', 'get_compliance_report'),
    ('coach/tools/coaching_tools.py', 'get_coaching_score'),
    ('coach/tools/coaching_tools.py', 'get_coaching_snapshot'),
    ('coach/tools/data_tools.py', 'get_activities_range'),
    ('coach/tools/decision_tools.py', 'log_coaching_decision'),
    ('coach/tools/decision_tools.py', 'get_active_decisions'),
    ('coach/tools/decision_tools.py', 'update_decision_status'),
    ('coach/tools/decision_tools.py', 'propose_coaching_action'),
    ('coach/tools/decision_tools.py', 'list_pending_approvals'),
    ('coach/tools/decision_tools.py', 'approve_proposal'),
    ('coach/tools/decision_tools.py', 'reject_proposal'),
    ('coach/tools/decision_tools.py', 'record_athlete_response'),
    ('coach/tools/decision_tools.py', 'resolve_anomaly'),
    # [stamp] created/updated stamps on new anomaly-registry entries, also
    # the fallback default for `today` when not threaded (snapshot threads it)
    ('coach/tools/decision_tools.py', 'register_detected_anomalies'),
    # [stamp] 'created' stamp on the auto_proposal_tags registry entry
    # (expiry math lives in propose_coaching_action, a boundary)
    ('coach/tools/decision_tools.py', 'ensure_tagged_proposal'),
    # [boundary] @mcp.tool bodies
    ('coach/tools/fitness_tools.py', 'refresh_athlete_baseline'),
    ('coach/tools/fitness_tools.py', 'refresh_fitness_history'),
    ('coach/tools/fitness_tools.py', 'backfill_history'),
    ('coach/tools/fitness_tools.py', 'query_metrics'),
    ('coach/tools/injury_tools.py', 'update_injury_status'),
    # [stamp] write-time diagnosis date on the injury record (and its
    # same-day dedup key); called only from the diagnose_injury tool
    ('coach/tools/injury_tools.py', '_save_diagnosis_to_profile'),
    # [boundary] @mcp.tool bodies
    ('coach/tools/interactive_tools.py', 'generate_smart_brief'),
    ('coach/tools/planning_tools.py', 'get_periodization_status'),
    ('coach/tools/planning_tools.py', 'get_weekly_prescription'),
    ('coach/tools/planning_tools.py', 'update_phase'),
    ('coach/tools/planning_tools.py', 'get_weekly_plan'),
    ('coach/tools/planning_tools.py', 'update_weekly_plan'),
    ('coach/tools/planning_tools.py', 'get_week_constraints'),
    ('coach/tools/race_tools.py', 'races'),
    ('coach/tools/research_tools.py', 'research_exercise'),
    ('coach/tools/strength_tools.py', 'sync_strength_session'),
    ('coach/tools/strength_tools.py', 'approve_progression'),
    ('coach/tools/strength_tools.py', 'generate_strength_workout'),
}


def _scan_file(path: Path) -> list[tuple[str, int]]:
    """Return [(top_level_function_or_<module>, lineno)] for banned calls."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    toplevel = [
        (node.name, node.lineno, node.end_lineno)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        if not (isinstance(base, ast.Name)
                and (base.id, node.func.attr) in BANNED_CALLS):
            continue
        fn = next(
            (name for name, start, end in toplevel
             if start <= node.lineno <= end),
            '<module>',
        )
        hits.append((fn, node.lineno))
    return hits


def _scan_coach_tree() -> dict[tuple[str, str], list[int]]:
    """Map (relpath, function) -> banned-call line numbers across coach/."""
    repo_root = COACH_DIR.parent
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(COACH_DIR.rglob('*.py')):
        rel = path.relative_to(repo_root).as_posix()
        for fn, lineno in _scan_file(path):
            found.setdefault((rel, fn), []).append(lineno)
    return found


class TestClockDiscipline:
    def test_coach_dir_exists(self):
        assert COACH_DIR.is_dir(), f'coach/ not found at {COACH_DIR}'

    def test_no_naked_clock_reads_outside_allowlist(self):
        """The lint: every wall-clock read must be an allowlisted boundary."""
        found = _scan_coach_tree()
        violations = {
            key: lines for key, lines in found.items()
            if key not in ALLOWLIST
        }
        assert not violations, (
            'Naked wall-clock read(s) outside the tool-boundary allowlist.\n'
            'Resolve date.today()/datetime.now() ONCE at the @mcp.tool '
            'boundary and thread `today: date` through the helper instead '
            '(see module docstring):\n' + '\n'.join(
                f'  {file}::{fn} (lines {lines})'
                for (file, fn), lines in sorted(violations.items())
            )
        )

    def test_allowlist_has_no_stale_entries(self):
        """Entries whose clock read was removed must leave the allowlist —
        the list can only shrink truthfully, never accumulate dead grants."""
        found = _scan_coach_tree()
        stale = ALLOWLIST - set(found)
        assert not stale, (
            'Stale allowlist entries (no banned call there anymore) — '
            'remove them:\n' + '\n'.join(
                f'  {file}::{fn}' for file, fn in sorted(stale)
            )
        )

    def test_module_level_clock_reads_banned(self):
        """A module-level date.today() (e.g. a constant) is the worst rot:
        frozen at import time. Nothing in coach/ may do it, ever."""
        found = _scan_coach_tree()
        module_level = [key for key in found if key[1] == '<module>']
        assert not module_level, (
            f'Module-level wall-clock reads found: {sorted(module_level)}'
        )
