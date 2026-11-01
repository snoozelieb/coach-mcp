"""Gym order by station: the athlete walks the gym the way the watch lists
the steps, so a session states where each movement is done and the preview
prints the walk and warns when it returns to a station it left.

The station is the author's statement (Garmin's catalogue carries no
equipment); the renderer never reorders, never derives a station from a
key, and never sends the label to the watch. methodology.strength
.station_order (off | warn | error) is the athlete's enforcement level.
"""
import copy
import json
from datetime import date, timedelta

import pytest

import coach.fitness as fitness_mod
import coach.planner as planner
import coach.tools.research_tools as research_mod
import coach.workout_builder as wb
from coach.authoring import (
    STATION_MAX_CHARS,
    STATION_ORDER_DEFAULT,
    contract_document,
    key_words,
    normalise_station,
    station_walk,
    station_walk_line,
    validate_exercises,
    validate_session,
)
from coach.tools.planning_tools import preview_workout, push_plan_to_garmin, update_weekly_plan
from coach.workout_builder import STATION_RULE_DEFAULTS, build_workout_report, station_rules

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()

CATALOGUE = {
    "INCLINE_DUMBBELL_BENCH_PRESS": {"category": "BENCH_PRESS", "categories": ["BENCH_PRESS"]},
    "SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW": {"category": "ROW", "categories": ["ROW"]},
    "WEIGHTED_INCLINE_Y_RAISE": {"category": "SHOULDER_STABILITY", "categories": ["SHOULDER_STABILITY"]},
    "FACE_PULL_WITH_EXTERNAL_ROTATION": {"category": "ROW", "categories": ["ROW"]},
    "PULL_APART": {"category": "BANDED_EXERCISES", "categories": ["BANDED_EXERCISES"]},
    "DEAD_BUG": {"category": "HIP_STABILITY", "categories": ["HIP_STABILITY"]},
    "SIDE_PLANK": {"category": "PLANK", "categories": ["PLANK", "SUSPENSION"], "is_bodyweight": True},
    "HANGING_KNEE_RAISE": {"category": "LEG_RAISE", "categories": ["LEG_RAISE"], "is_bodyweight": True},
    "GOBLET_SQUAT": {"category": "SQUAT", "categories": ["SQUAT"]},
    "PLANK": {"category": "PLANK", "categories": ["PLANK"], "is_bodyweight": True},
}


def _ex(name, sets=3, reps=10, station=None, **extra):
    spec = {"name": name, "sets": sets, "reps": reps, **extra}
    if station is not None:
        spec["station"] = station
    return spec


def _upper(order="muscle"):
    """The athlete's real upper-body session (2026-10-30): in the order the
    watch dictated, or ordered by station."""
    muscle_order = [
        _ex("SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW", weight_kg=12.5, each_side=True, station="incline bench"),
        _ex("FACE_PULL_WITH_EXTERNAL_ROTATION", reps=15, station="cable"),
        _ex("WEIGHTED_INCLINE_Y_RAISE", weight_kg=1, station="incline bench"),
        _ex("PULL_APART", reps=12, station="cable"),
        _ex("INCLINE_DUMBBELL_BENCH_PRESS", sets=2, weight_kg=10, station="incline bench"),
        _ex("DEAD_BUG", station="mat"),
        {"rounds": 3, "rest_secs": 30, "station": "mat", "exercises": [
            {"name": "SIDE_PLANK", "duration_secs": 30, "each_side": True}]},
        _ex("HANGING_KNEE_RAISE", station="pull-up bar"),
    ]
    exercises = muscle_order if order == "muscle" else [muscle_order[i] for i in (4, 0, 2, 1, 3, 5, 6, 7)]
    return {"type": "strength", "name": "Upper body", "purpose": "posture", "duration_mins": 40,
            "warmup_mins": 5, "exercises": copy.deepcopy(exercises)}


def _messages(report):
    return [w["message"] for w in report.warnings]


# ---------------------------------------------------------------------------
# The contract key
# ---------------------------------------------------------------------------

class TestStationKey:
    def test_label_is_normalised_not_interpreted(self):
        items, problems = validate_exercises([_ex("GOBLET_SQUAT", station="  Incline   BENCH ")])
        assert problems == []
        assert items[0].station == "incline bench"
        assert normalise_station("") is None and normalise_station(None) is None

    def test_long_or_non_text_labels_are_teaching_errors(self):
        _items, problems = validate_exercises([
            _ex("GOBLET_SQUAT", station="the mat in the corner next to the cable stack"),
            _ex("PLANK", reps=None, duration_secs=30, station=42)])
        messages = [p["message"] for p in problems]
        assert any(f"up to {STATION_MAX_CHARS}" in m for m in messages), messages
        assert any("short text label" in m and "42" in m for m in messages), messages
        assert [p["field"] for p in problems] == ["exercises[0].station", "exercises[1].station"]

    def test_station_is_an_allowed_key_on_exercises_circuits_and_members(self):
        items, problems = validate_exercises([
            {"rounds": 2, "station": "mat", "exercises": [
                {"name": "DEAD_BUG", "reps": 10},
                {"name": "PLANK", "duration_secs": 30, "station": "floor"}]}])
        assert problems == []
        circuit = items[0]
        assert circuit.station == "mat"
        assert [m.station for m in circuit.exercises] == [None, "floor"]

    def test_unknown_key_error_lists_station_among_the_allowed_keys(self):
        _items, problems = validate_exercises([_ex("GOBLET_SQUAT", spot="bench")])
        assert "station" in problems[0]["message"] and "unknown key 'spot'" in problems[0]["message"]

    def test_contract_document_teaches_the_key(self):
        text = contract_document("strength")
        assert "station" in text and "Stations:" in text and "station_order" in text


# ---------------------------------------------------------------------------
# The walk (pure)
# ---------------------------------------------------------------------------

class TestStationWalk:
    def test_consecutive_movements_at_one_station_form_a_block(self):
        items, _ = validate_exercises(_upper("station")["exercises"])
        walk = station_walk(items)
        assert [b["station"] for b in walk["blocks"]] == ["incline bench", "cable", "mat", "pull-up bar"]
        assert walk["blocks"][0]["exercises"] == ["INCLINE_DUMBBELL_BENCH_PRESS 10 kg",
                                                  "SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW 12.5 kg",
                                                  "WEIGHTED_INCLINE_Y_RAISE 1 kg"]
        assert walk["blocks"][2]["paths"] == ["exercises[5]", "exercises[6].exercises[0]"]  # the circuit's station covers its member
        assert walk["revisits"] == [] and walk["circuits_spanning"] == [] and walk["unstated"] == []
        assert walk["stated"] == 8
        assert station_walk_line(walk) == "incline bench → cable → mat → pull-up bar"

    def test_a_return_to_a_station_names_what_came_between(self):
        items, _ = validate_exercises(_upper("muscle")["exercises"])
        walk = station_walk(items)
        assert [(r["path"], r["station"], r["between"]) for r in walk["revisits"]] == [
            ("exercises[2]", "incline bench", ["cable"]),
            ("exercises[3]", "cable", ["incline bench"]),
            ("exercises[4]", "incline bench", ["cable"])]
        assert station_walk_line(walk).startswith("incline bench → cable → incline bench (again) → cable (again)")

    def test_unstated_movements_are_transparent_and_listed(self):
        items, _ = validate_exercises([
            _ex("INCLINE_DUMBBELL_BENCH_PRESS", station="incline bench"),
            _ex("DEAD_BUG"),
            _ex("WEIGHTED_INCLINE_Y_RAISE", weight_kg=1, station="incline bench"),
            _ex("HANGING_KNEE_RAISE")])
        walk = station_walk(items)
        assert len(walk["blocks"]) == 1 and walk["revisits"] == []   # the dead bug neither ends nor starts a block
        assert walk["unstated"] == ["exercises[1]", "exercises[3]"]
        assert station_walk_line(walk) == "incline bench; 2 movements with no station"

    def test_a_circuit_whose_members_state_different_stations_spans(self):
        items, _ = validate_exercises([{"rounds": 3, "exercises": [
            {"name": "INCLINE_DUMBBELL_BENCH_PRESS", "reps": 10, "station": "incline bench"},
            {"name": "FACE_PULL_WITH_EXTERNAL_ROTATION", "reps": 15, "station": "cable"},
            {"name": "DEAD_BUG", "reps": 10}]}])
        walk = station_walk(items)
        assert walk["circuits_spanning"] == [{"path": "exercises[0]", "stations": ["incline bench", "cable"]}]
        assert walk["unstated"] == ["exercises[0].exercises[2]"]

    def test_member_station_overrides_the_circuit_station(self):
        items, _ = validate_exercises([{"rounds": 2, "station": "mat", "exercises": [
            {"name": "DEAD_BUG", "reps": 10}, {"name": "PLANK", "duration_secs": 30, "station": "bench"}]}])
        walk = station_walk(items)
        assert [b["station"] for b in walk["blocks"]] == ["mat", "bench"]
        assert walk["circuits_spanning"][0]["stations"] == ["mat", "bench"]


# ---------------------------------------------------------------------------
# The report: warnings at the athlete's level, the walk ahead of the steps
# ---------------------------------------------------------------------------

class TestStationWarnings:
    def test_revisits_warn_by_default_with_both_stations_named(self):
        report = validate_session(_upper("muscle"), CATALOGUE)
        hits = [w for w in report.warnings if "returns to" in w["message"]]
        assert [h["field"] for h in hits] == ["exercises[2]", "exercises[3]", "exercises[4]"]
        assert "returns to 'incline bench' after 'cable'" in hits[0]["message"]
        assert "finish a station and the weights carried there" in hits[0]["message"]
        assert report.ok

    def test_station_order_visits_each_spot_once_without_a_warning(self):
        report = validate_session(_upper("station"), CATALOGUE)
        assert not any("returns to" in m or "no station" in m for m in _messages(report)), _messages(report)

    def test_a_spanning_circuit_warns(self):
        report = validate_session({"type": "strength", "exercises": [{"rounds": 3, "exercises": [
            {"name": "INCLINE_DUMBBELL_BENCH_PRESS", "reps": 10, "station": "incline bench"},
            {"name": "FACE_PULL_WITH_EXTERNAL_ROTATION", "reps": 15, "station": "cable"}]}]}, CATALOGUE)
        (message,) = [m for m in _messages(report) if "rounds change spot" in m]
        assert "'incline bench', 'cable'" in message and "straight sets" in message

    def test_unstated_stations_warn_once_listing_the_movements(self):
        session = _upper("station")
        for spec in session["exercises"][5:]:
            spec.pop("station", None)
        report = validate_session(session, CATALOGUE)
        (message,) = [m for m in _messages(report) if "no station on" in m]
        assert "exercises[5], exercises[6].exercises[0], exercises[7]" in message
        assert "one label per spot" in message

    def test_a_single_movement_has_no_walk_to_check(self):
        report = validate_session({"type": "strength", "exercises": [_ex("GOBLET_SQUAT")]}, CATALOGUE)
        assert not any("no station" in m for m in _messages(report))

    def test_error_level_rejects_and_off_level_only_records_the_walk(self):
        session = _upper("muscle")
        strict = validate_session(session, CATALOGUE, station_order="error")
        assert not strict.ok
        assert [e["field"] for e in strict.errors if "returns to" in e["message"]] == ["exercises[2]", "exercises[3]", "exercises[4]"]
        quiet = validate_session(session, CATALOGUE, station_order="off")
        assert quiet.ok and not any("returns to" in m for m in _messages(quiet))
        assert quiet.station_walk["revisits"]   # the walk is still a fact of the plan
        assert validate_session(session, CATALOGUE, station_order="nonsense").ok   # unknown level: the default (warn)


# ---------------------------------------------------------------------------
# The preview, the save, the push, the listing (sandboxed data dir)
# ---------------------------------------------------------------------------

@pytest.fixture
def env(data_dir, monkeypatch):
    for mod in (wb, fitness_mod, planner, research_mod):
        monkeypatch.setattr(mod, "DATA_DIR", data_dir)
    (data_dir / "exercises.json").write_text(json.dumps({"exercises": CATALOGUE, "categories": []}), encoding="utf-8")
    (data_dir / "athlete.json").write_text(json.dumps({
        "personal": {"name": "T", "resting_hr": 42, "max_hr": 195}, "injury_history": [],
        "life_constraints": {}, "preferences": {}}), encoding="utf-8")
    return data_dir


def _set_level(data_dir, level):
    (data_dir / "methodology.json").write_text(json.dumps({"strength": {"station_order": level}}), encoding="utf-8")


class TestPreviewAndRules:
    def test_rules_default_then_read_methodology_and_ignore_nonsense(self, env):
        assert station_rules() == STATION_RULE_DEFAULTS == {"station_order": STATION_ORDER_DEFAULT}
        _set_level(env, "ERROR ")
        assert station_rules() == {"station_order": "error"}
        _set_level(env, "loud")
        assert station_rules() == {"station_order": "warn"}

    def test_the_walk_heads_the_steps_and_never_reaches_the_watch(self, env):
        report = build_workout_report(_upper("station"), TOMORROW)
        assert report["ok"]
        assert report["steps"][0] == "Stations: incline bench → cable → mat → pull-up bar"
        assert report["steps"][1] == "1. [Warm Up] 5:00"
        assert report["station_walk"] == [
            "incline bench: INCLINE_DUMBBELL_BENCH_PRESS 10 kg, SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW 12.5 kg, "
            "WEIGHTED_INCLINE_Y_RAISE 1 kg",
            "cable: FACE_PULL_WITH_EXTERNAL_ROTATION, PULL_APART",
            "mat: DEAD_BUG, SIDE_PLANK",
            "pull-up bar: HANGING_KNEE_RAISE"]
        assert "station" not in json.dumps(report["workout"])

    def test_no_stations_stated_means_no_walk_line(self, env):
        session = _upper("station")
        for spec in session["exercises"]:
            spec.pop("station", None)
        report = build_workout_report(session, TOMORROW)
        assert report["ok"] and report["steps"][0] == "1. [Warm Up] 5:00" and "station_walk" not in report
        assert any("no station on" in w["message"] for w in report["warnings"])

    def test_preview_tool_carries_the_walk(self, env):
        result = preview_workout(_upper("muscle"))
        assert result["steps"][0].startswith("Stations: incline bench → cable → incline bench (again)")
        assert len(result["station_walk"]) == 7
        assert sum("returns to" in w["message"] for w in result["warnings"]) == 3

    def test_error_level_rejects_the_save_and_warn_level_saves_with_the_warning(self, env):
        _set_level(env, "error")
        plan = {"days": {TOMORROW: {"planned": _upper("muscle")}}}
        rejected = update_weekly_plan(plan=plan)
        assert rejected["error"] == "authoring_error"
        assert sum("returns to" in p["message"] for p in rejected["problems"]) == 3
        _set_level(env, "warn")
        saved = update_weekly_plan(plan=plan)
        assert saved["status"] == "success"
        assert sum("returns to" in w["message"] for w in saved["authoring_warnings"]) == 3

    def test_push_preview_shows_the_walk_inside_the_consent_step(self, env, monkeypatch):
        monkeypatch.setattr("coach.tools.planning_tools.garmin_api_call", lambda fn: [])
        assert update_weekly_plan(plan={"days": {TOMORROW: {"planned": _upper("muscle")}}})["status"] == "success"
        preview = push_plan_to_garmin()
        assert preview["status"] == "preview", preview
        (session,) = preview["sessions"]
        assert session["steps"][0].startswith("Stations: incline bench → cable → incline bench (again)")
        assert sum("returns to" in w["message"] for w in session["warnings"]) == 3

    def test_listing_shows_the_words_a_key_spells(self, env):
        matches = {m["name"]: m for m in json.loads(research_mod.list_exercises(limit=50))["matches"]}
        assert matches["INCLINE_DUMBBELL_BENCH_PRESS"]["key_words"] == ["INCLINE", "DUMBBELL", "BENCH"]
        assert matches["HANGING_KNEE_RAISE"]["key_words"] == ["HANGING"]
        assert "key_words" not in matches["DEAD_BUG"]


class TestKeyWords:
    def test_words_of_the_key_in_key_order(self):
        assert key_words("CABLE_FACE_PULL") == ["CABLE"]
        assert key_words("WEIGHTED_SWISS_BALL_Y_RAISE") == ["SWISS_BALL"]
        assert key_words("band pull apart") == ["BAND"]
        assert key_words("LAT_PULLDOWN") == [] and key_words("") == []
        assert key_words("BARBELL_BENCH_PRESS") == ["BARBELL", "BENCH"]   # BAR is not read inside BARBELL
