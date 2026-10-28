"""The authoring eval's cases stay honest without spending money.

scripts/authoring_eval.py --run has a model author every brief and measures
how often the rendering matches. CI cannot pay for that, but it can prove
the measuring stick: every case's golden session renders under the eval
athlete and satisfies the case's own checks (so a check can be met under the
contract and the cases drift with it), every brief carries what the
workout-author agent demands before it will author, and the file holds no
personal data.
"""
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

# Loaded at collection so the autouse sandbox fixture patches their DATA_DIR
# before the harness renders anything (a module imported later in a test
# would resolve the live data dir).
import coach.config  # noqa: F401
import coach.workout_builder  # noqa: F401
from coach.schemas import WeeklyPlan

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "evals" / "authoring"
SCRIPT = ROOT / "scripts" / "authoring_eval.py"
SEED_FILES = ("athlete.json", "training_config.json", "exercises.json")

# The agent's brief contract (.claude/agents/workout-author.md): purpose, the
# shape and targets, restrictions verbatim, demonstrated capacity, athlete
# facts. Every brief names each heading so the agent cannot stall on a gap
# the case did not intend.
BRIEF_HEADINGS = ("PURPOSE", "SESSION", "RESTRICTIONS", "CAPACITY", "ATHLETE FACTS")
PRIVATE_WORDS = ("keryn", "schoon", "suikerbossie")


def _load_module():
    spec = importlib.util.spec_from_file_location("authoring_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_module()


@pytest.fixture(scope="module")
def cases(harness):
    return harness.load_cases()


@pytest.fixture
def eval_athlete(sandbox_data_dir):
    """Seed the sandbox with the eval's synthetic athlete, config and the
    committed slice of the Garmin catalogue."""
    for name in SEED_FILES:
        shutil.copyfile(EVAL_DIR / name, sandbox_data_dir / name)
    return sandbox_data_dir


def test_cases_file_is_well_formed(cases):
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert 15 <= len(cases) <= 25, "the review asked for 15-20 briefs"
    sports = {c["sport"] for c in cases}
    assert {"running", "cycling", "strength", "mobility"} <= sports
    for case in cases:
        assert re.fullmatch(r"[a-z0-9_]+", case["id"]), case["id"]
        assert case["brief"].strip(), case["id"]
        if case.get("expect_missing_brief"):
            assert "golden" not in case, f"{case['id']}: a missing_brief case has nothing to render"
        else:
            assert isinstance(case.get("golden"), dict), f"{case['id']}: golden session required"
            assert case.get("checks"), f"{case['id']}: checks required"


def test_briefs_carry_the_agent_contract(cases):
    for case in cases:
        if case.get("expect_missing_brief"):
            continue
        for heading in BRIEF_HEADINGS:
            assert heading in case["brief"], f"{case['id']}: brief lacks {heading}"


def test_no_personal_data_in_eval_files(cases):
    text = json.dumps(cases).lower() + (EVAL_DIR / "athlete.json").read_text(encoding="utf-8").lower()
    for word in PRIVATE_WORDS:
        assert word not in text, f"private word {word!r} in the eval files"


def test_goldens_satisfy_their_own_checks(harness, cases, eval_athlete):
    failures = {}
    for case in cases:
        if case.get("expect_missing_brief"):
            continue
        result = harness.evaluate_case(case, case["golden"], None)
        if not result["pass"]:
            failures[case["id"]] = (result["failures"], result["report"]["steps"] if result["report"] else None)
    assert not failures, json.dumps(failures, indent=1)


def test_goldens_are_valid_plan_sessions(cases):
    """What the eval calls correct must also be what update_weekly_plan accepts."""
    for case in cases:
        if case.get("expect_missing_brief"):
            continue
        plan = {"days": {"2030-01-06": {"planned": [case["golden"]]}}}
        WeeklyPlan.model_validate(plan)


def test_checks_fail_on_the_wrong_rendering(harness, cases, eval_athlete):
    """A check set that passes everything measures nothing: every case must
    reject at least one neighbouring golden."""
    goldens = [(c["id"], c["golden"]) for c in cases if not c.get("expect_missing_brief")]
    for case in cases:
        if case.get("expect_missing_brief"):
            continue
        rejected = 0
        for other_id, other in goldens:
            if other_id == case["id"]:
                continue
            if harness.apply_checks(harness.render(other), case["checks"]):
                rejected += 1
        assert rejected == len(goldens) - 1, (
            f"{case['id']}: its checks accept another case's rendering — too loose")


def test_extract_answer_reads_structured_output_or_a_fenced_block(harness):
    session = {"type": "yoga", "duration_mins": 30, "purpose": "x"}
    structured = {"structured_output": {"session": session, "missing_brief": None, "notes": ""}, "result": ""}
    assert harness.extract_answer(structured)["session"] == session
    prose = {"result": "Here it is.\n\n```json\n" + json.dumps({"session": session, "missing_brief": None,
                                                                 "notes": "none"}) + "\n```\n"}
    assert harness.extract_answer(prose)["session"] == session
    pasted = {"result": "**Session**\n```json\n" + json.dumps(session) + "\n```\nPreview steps: ..."}
    assert harness.extract_answer(pasted)["session"] == session
    refused = {"result": "missing_brief: the load for the goblet squat was not given."}
    assert harness.extract_answer(refused)["session"] is None
    assert harness.extract_answer(refused)["missing_brief"]
    assert harness.extract_answer({"result": "I could not do this."}) == {}


def test_apply_checks_semantics(harness):
    report = {"ok": True, "workout_type": "running", "estimated_mins": 42,
              "steps": ["1. Warm-up 10:00", "2. 6 x", "   2.1. Interval 3:00 @ pace 4:35/km to 4:25/km",
                        "   2.2. Recovery 1:30", "3. Cool-down 5:00"],
              "warnings": [{"field": "x", "message": "library cue used"}]}
    assert harness.apply_checks(report, {"ok": True, "workout_type": "running",
                                         "estimated_mins": [40, 45], "steps_match": ["^2[.] 6 x$"]}) == []
    assert harness.apply_checks(report, {"steps_match": [{"pattern": "Recovery", "min": 2}]})
    assert harness.apply_checks(report, {"steps_absent": ["Cool-down"]})
    assert harness.apply_checks(report, {"max_warnings": 0})
    assert harness.apply_checks(report, {"warnings_absent": ["library cue"]})
    assert harness.apply_checks(report, {"estimated_mins": [50, 60]})
