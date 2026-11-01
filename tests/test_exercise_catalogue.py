"""The exercise catalogue after the 2026-10 "Weighted Side Plank" bug.

Garmin files 26 names under more than one category (SIDE_PLANK under PLANK
and SUSPENSION). The cache used to keep ONE category per name, last wins, so
SIDE_PLANK became a suspension exercise: list_exercises(category='PLANK') never
showed it, the coach reached for WEIGHTED_SIDE_PLANK, and the watch announced
a weight for a bodyweight hold. The cache now keeps every category, Garmin's
bodyweight flag and the loaded/bodyweight counterpart; the resolver, the
listing and the contract read them. Holds are timed by contract.
"""
import importlib.util
import json
from pathlib import Path

import pytest

import coach.tools.research_tools as research_mod
from coach.authoring import (
    is_timed_hold, normalise_exercise_name, primary_catalogue_category, validate_session,
)

ROOT = Path(__file__).resolve().parent.parent

# A slice of the real cache shape (scripts/fetch_exercises.py, schema 2)
CATALOGUE = {
    "exercises": {
        "PLANK": {"category": "PLANK", "categories": ["BANDED_EXERCISES", "PLANK", "SUSPENSION"],
                  "garmin_category": "PLANK", "muscles": ["abs"], "primary_muscles": ["abs"],
                  "secondary_muscles": [], "injury_prevention": [],
                  "is_bodyweight": True, "counterpart": "WEIGHTED_PLANK"},
        "SIDE_PLANK": {"category": "PLANK", "categories": ["PLANK", "SUSPENSION"],
                       "garmin_category": "PLANK", "muscles": ["obliques"], "primary_muscles": ["obliques"],
                       "secondary_muscles": [], "injury_prevention": ["back"],
                       "is_bodyweight": True, "counterpart": "WEIGHTED_SIDE_PLANK"},
        "WEIGHTED_SIDE_PLANK": {"category": "PLANK", "categories": ["PLANK"],
                                "garmin_category": "PLANK", "muscles": ["obliques"],
                                "primary_muscles": ["obliques"], "secondary_muscles": [],
                                "injury_prevention": [], "counterpart": "SIDE_PLANK"},
        "TRX_ROW": {"category": "SUSPENSION", "categories": ["SUSPENSION"], "garmin_category": "SUSPENSION",
                    "muscles": ["lats"], "primary_muscles": ["lats"], "secondary_muscles": [],
                    "injury_prevention": []},
        "GOBLET_SQUAT": {"category": "SQUAT", "categories": ["SQUAT"], "garmin_category": "SQUAT",
                         "muscles": ["quads"], "primary_muscles": ["quads"], "secondary_muscles": [],
                         "injury_prevention": []},
    },
    "categories": ["BANDED_EXERCISES", "PLANK", "SQUAT", "SUSPENSION"],
    "injury_mappings": {"back": ["SIDE_PLANK"]},
    "custom_exercises": [],
    "metadata": {"schema": 2, "exercise_count": 5},
}

LEGACY_CATALOGUE = {
    "exercises": {
        "PLANK": {"category": "SUSPENSION", "garmin_category": "SUSPENSION", "muscles": ["abs"],
                  "primary_muscles": ["abs"], "secondary_muscles": [], "injury_prevention": []},
        "SIDE_PLANK": {"category": "SUSPENSION", "garmin_category": "SUSPENSION", "muscles": ["obliques"],
                       "primary_muscles": ["obliques"], "secondary_muscles": [], "injury_prevention": []},
    },
    "categories": ["PLANK", "SUSPENSION"],
    "injury_mappings": {},
    "custom_exercises": [],
    "metadata": {"exercise_count": 2},
}


def _write_catalogue(data_dir, payload):
    (data_dir / 'exercises.json').write_text(json.dumps(payload), encoding='utf-8')


def _listing(**kwargs):
    return json.loads(research_mod.list_exercises(**kwargs))


# ---------------------------------------------------------------------------
# The hold rule: a hold is timed
# ---------------------------------------------------------------------------

class TestHoldsAreTimed:
    @pytest.mark.parametrize("name", [
        "PLANK", "SIDE_PLANK", "WEIGHTED_SIDE_PLANK", "_45_DEGREE_PLANK", "COPENHAGEN_PLANK",
        "HEX_DUMBBELL_HOLD", "_90_DEGREE_STATIC_HOLD", "FARMERS_CARRY", "OVERHEAD_CARRY",
        "L_SIT", "HANGING_L_SIT", "WALL_SIT", "DEAD_HANG", "THREE_WAY_PLANK",
        "Side plank left", "Plank 30s", "Wall sit hold", "side plank R",
    ])
    def test_static_holds_are_recognised(self, name):
        assert is_timed_hold(name) is True

    @pytest.mark.parametrize("name", [
        "PLANK_JACKS", "ROLLING_SIDE_PLANK", "INCHWORM_TO_SIDE_PLANK", "PLYOMETRIC_SIDE_PLANK",
        "SIDE_KICK_PLANK", "SPIDERMAN_PLANK", "TURKISH_GET_UP_TO_SIDE_PLANK", "INCHING_ELBOW_PLANK",
        "SWISS_BALL_PLANK_LEG_LIFT_AND_HOLD", "DUMBBELL_BICEPS_CURL_WITH_STATIC_HOLD",
        "PLANK_TO_PUSH_UP", "SIDE_PLANK_WITH_LEG_LIFT", "KETTLEBELL_SIT_UP", "HANGING_LEG_RAISE",
        "GOBLET_SQUAT", "DEAD_BUG", "",
    ])
    def test_dynamic_movements_stay_on_reps(self, name):
        assert is_timed_hold(name) is False

    def test_the_live_catalogue_split_is_the_documented_one(self):
        """Against the owner's cached Garmin catalogue (skipped without it):
        every static *_PLANK key is a hold, every moving one is not."""
        path = ROOT / "data" / "exercises.json"
        if not path.exists():
            pytest.skip("no cached catalogue")
        names = [n for n in json.loads(path.read_text(encoding='utf-8'))["exercises"] if n.endswith("PLANK")]
        if len(names) < 40:
            pytest.skip("catalogue slice, not the full cache")
        holds = {n for n in names if is_timed_hold(n)}
        assert {"PLANK", "SIDE_PLANK", "WEIGHTED_SIDE_PLANK", "REVERSE_PLANK", "_45_DEGREE_PLANK"} <= holds
        assert not holds & {"ROLLING_SIDE_PLANK", "PLYOMETRIC_SIDE_PLANK", "INCHWORM_TO_SIDE_PLANK",
                            "SIDE_KICK_PLANK", "SPIDERMAN_PLANK", "INCHING_ELBOW_PLANK"}

    def test_a_rep_count_on_a_hold_is_a_teaching_error(self):
        report = validate_session({"type": "strength", "exercises": [
            {"name": "PLANK", "category": "PLANK", "sets": 3, "reps": 10}]}, CATALOGUE["exercises"])
        (message,) = [p["message"] for p in report.errors]
        assert "hold is timed" in message and "duration_secs" in message and "reps 10" in message
        assert "exercises[0]" in report.errors[0]["field"]

    def test_timed_and_lap_button_holds_pass(self):
        for spec in ({"name": "PLANK", "sets": 3, "duration_secs": 45},
                     {"name": "PLANK", "sets": 3, "reps": "open", "notes": "hold as long as you can"}):
            report = validate_session({"type": "strength", "exercises": [spec]}, CATALOGUE["exercises"])
            assert report.errors == [], spec

    def test_the_rule_reaches_circuits_and_off_catalogue_names(self):
        report = validate_session({"type": "strength", "exercises": [
            {"rounds": 2, "exercises": [{"name": "Side plank left", "category": "PLANK", "reps": 8}]}]},
            CATALOGUE["exercises"])
        (message,) = [p["message"] for p in report.errors]
        assert "SIDE_PLANK_LEFT" in message and "duration_secs" in message

    def test_pilates_rehab_lane_is_untouched(self):
        """The lenient rehab shape (type pilates/rehab) is not the strength contract."""
        report = validate_session({"type": "pilates", "duration_mins": 15, "exercises": [
            {"name": "Side plank", "sets": 2, "reps": 8}]}, CATALOGUE["exercises"])
        assert report.errors == []


# ---------------------------------------------------------------------------
# Garmin keys
# ---------------------------------------------------------------------------

class TestGarminKeys:
    @pytest.mark.parametrize("raw, key", [
        ("45 degree plank", "_45_DEGREE_PLANK"), ("_45_DEGREE_PLANK", "_45_DEGREE_PLANK"),
        ("90 degree static hold", "_90_DEGREE_STATIC_HOLD"), ("Goblet squat", "GOBLET_SQUAT"),
    ])
    def test_keys_that_start_with_a_digit_keep_garmins_underscore(self, raw, key):
        assert normalise_exercise_name(raw) == key

    @pytest.mark.parametrize("name, categories, primary", [
        ("SIDE_PLANK", ["PLANK", "SUSPENSION"], "PLANK"),
        ("PLANK", ["BANDED_EXERCISES", "PLANK", "SUSPENSION"], "PLANK"),
        ("ROW", ["BANDED_EXERCISES", "ROW", "SANDBAG", "SLED", "SUSPENSION"], "ROW"),
        ("ZERCHER_SQUAT", ["SANDBAG", "SQUAT"], "SQUAT"),
        ("FACE_PULL", ["ROW", "SUSPENSION"], "ROW"),                  # the movement category
        ("MOUNTAIN_CLIMBER", ["PLANK", "SUSPENSION"], "PLANK"),
        ("JUMPING_JACKS", ["BATTLE_ROPE", "CARDIO"], "CARDIO"),
        ("CHEST_PRESS", ["SLED", "SUSPENSION"], "SUSPENSION"),        # the only workout-API category
        ("GLUTE_BRIDGE", ["BANDED_EXERCISES", "SUSPENSION"], "BANDED_EXERCISES"),
        ("SPRINT", ["RUN", "SANDBAG"], "RUN"),
        ("TRX_ROW", ["SUSPENSION"], "SUSPENSION"),
        ("ANYTHING", [], ""),
    ])
    def test_primary_category_rule(self, name, categories, primary):
        assert primary_catalogue_category(name, categories) == primary

    def test_every_multi_category_name_in_the_cache_has_a_valid_primary(self):
        path = ROOT / "data" / "exercises.json"
        if not path.exists():
            pytest.skip("no cached catalogue")
        exercises = json.loads(path.read_text(encoding='utf-8'))["exercises"]
        multi = {n: e for n, e in exercises.items() if len(e.get("categories") or []) > 1}
        if not multi:
            pytest.skip("legacy catalogue")
        for name, entry in multi.items():
            assert entry["category"] == primary_catalogue_category(name, entry["categories"]), name
            assert entry["category"] in entry["categories"], name
        assert exercises["SIDE_PLANK"]["category"] == "PLANK"
        assert exercises["SIDE_PLANK"]["is_bodyweight"] is True
        assert exercises["WEIGHTED_SIDE_PLANK"]["counterpart"] == "SIDE_PLANK"


# ---------------------------------------------------------------------------
# The contract reads the bodyweight/loaded pairing and the real step text
# ---------------------------------------------------------------------------

class TestCounterparts:
    def test_weighted_key_without_a_load_is_an_error_naming_the_bodyweight_key(self):
        report = validate_session({"type": "strength", "exercises": [
            {"name": "WEIGHTED_SIDE_PLANK", "sets": 3, "duration_secs": 30, "each_side": True}]},
            CATALOGUE["exercises"])
        (message,) = [p["message"] for p in report.errors]
        assert "SIDE_PLANK" in message and "weight_kg" in message and "Weighted Side Plank" in message

    def test_weighted_key_with_a_load_passes(self):
        report = validate_session({"type": "strength", "exercises": [
            {"name": "WEIGHTED_SIDE_PLANK", "sets": 3, "duration_secs": 30, "weight_kg": 10}]},
            CATALOGUE["exercises"])
        assert report.errors == [] and report.warnings == []

    def test_bodyweight_key_with_a_load_is_a_hint(self):
        report = validate_session({"type": "strength", "exercises": [
            {"name": "SIDE_PLANK", "sets": 3, "duration_secs": 30, "weight_kg": 5}]},
            CATALOGUE["exercises"])
        assert report.errors == []
        (message,) = [w["message"] for w in report.warnings]
        assert "WEIGHTED_SIDE_PLANK" in message

    def test_the_step_text_the_watch_cuts_is_measured_not_the_notes_alone(self):
        """An off-catalogue name puts the readable name and the side before the
        cue: that whole text is what the watch cuts at about 50 characters."""
        cue = "Slow eccentric, full range"  # 26 characters: fine on a catalogue key
        on_key = validate_session({"type": "strength", "warmup_mins": 5, "exercises": [
            {"name": "GOBLET_SQUAT", "sets": 3, "reps": 8, "notes": cue}]}, CATALOGUE["exercises"])
        assert on_key.warnings == []
        off_key = validate_session({"type": "strength", "exercises": [
            {"name": "Banded ankle eversion", "category": "BANDED_EXERCISES", "sets": 2, "reps": 20,
             "each_side": True, "notes": cue}]}, CATALOGUE["exercises"])
        messages = [w["message"] for w in off_key.warnings]
        assert any("Banded Ankle Eversion right: Slow eccentric, full range" in m
                   and "55 characters" in m for m in messages), messages


# ---------------------------------------------------------------------------
# list_exercises reads every category, the flags and a readable search
# ---------------------------------------------------------------------------

class TestListExercises:
    def test_category_filter_finds_a_name_under_every_category_it_is_filed_in(self, sandbox_data_dir):
        _write_catalogue(sandbox_data_dir, CATALOGUE)
        under_plank = {m["name"] for m in _listing(category="plank")["matches"]}
        assert under_plank == {"PLANK", "SIDE_PLANK", "WEIGHTED_SIDE_PLANK"}
        under_suspension = {m["name"] for m in _listing(category="SUSPENSION")["matches"]}
        assert under_suspension == {"PLANK", "SIDE_PLANK", "TRX_ROW"}

    def test_matches_carry_the_flags_the_coach_needs(self, sandbox_data_dir):
        _write_catalogue(sandbox_data_dir, CATALOGUE)
        by_name = {m["name"]: m for m in _listing(category="PLANK")["matches"]}
        assert by_name["SIDE_PLANK"]["is_bodyweight"] is True
        assert by_name["SIDE_PLANK"]["counterpart"] == "WEIGHTED_SIDE_PLANK"
        assert by_name["SIDE_PLANK"]["categories"] == ["PLANK", "SUSPENSION"]
        assert by_name["SIDE_PLANK"]["category"] == "PLANK"
        assert "is_bodyweight" not in by_name["WEIGHTED_SIDE_PLANK"]
        assert "categories" not in by_name["WEIGHTED_SIDE_PLANK"]  # one category: nothing to add
        assert "catalogue_note" not in _listing(category="PLANK")

    @pytest.mark.parametrize("search", ["side plank", "SIDE_PLANK", "plank side", "Side-plank"])
    def test_search_reads_spaces_and_word_order(self, sandbox_data_dir, search):
        _write_catalogue(sandbox_data_dir, CATALOGUE)
        names = {m["name"] for m in _listing(search=search)["matches"]}
        assert names == {"SIDE_PLANK", "WEIGHTED_SIDE_PLANK"}

    def test_legacy_cache_is_named_so_the_coach_refreshes_it(self, sandbox_data_dir):
        _write_catalogue(sandbox_data_dir, LEGACY_CATALOGUE)
        result = _listing(category="PLANK")
        assert result["matches"] == []                     # the old filing hides SIDE_PLANK
        assert "fetch_exercises" in result["catalogue_note"]


# ---------------------------------------------------------------------------
# scripts/fetch_exercises.py builds the schema-2 cache
# ---------------------------------------------------------------------------

RAW_GARMIN = {"categories": {
    "PLANK": {"exercises": {
        "SIDE_PLANK": {"primaryMuscles": ["OBLIQUES"], "secondaryMuscles": [],
                       "isBodyWeight": True, "counterpart": "WEIGHTED_SIDE_PLANK"},
        "WEIGHTED_SIDE_PLANK": {"primaryMuscles": ["OBLIQUES"], "secondaryMuscles": ["SHOULDERS"],
                                "counterpart": "SIDE_PLANK"}}},
    "SUSPENSION": {"exercises": {
        "SIDE_PLANK": {"primaryMuscles": ["OBLIQUES"], "secondaryMuscles": ["SHOULDERS"]},
        "TRX_ROW": {"primaryMuscles": ["LATS"], "secondaryMuscles": []}}},
    "ROW": {"exercises": {"FACE_PULL": {"primaryMuscles": ["REAR_DELTS"], "secondaryMuscles": []}}},
}}

EXISTING_CACHE = {
    "exercises": {
        "HIP_THRUST": {"category": "HIP_RAISE", "garmin_category": "HIP_RAISE", "custom": True,
                       "muscles": ["glutes"], "primary_muscles": ["glutes"], "secondary_muscles": [],
                       "injury_prevention": []},
        "SIDE_PLANK": {"category": "SUSPENSION", "garmin_category": "SUSPENSION"},
    },
    "custom_exercises": ["HIP_THRUST"],
}


@pytest.fixture(scope="module")
def fetch_script():
    spec = importlib.util.spec_from_file_location("fetch_exercises", ROOT / "scripts" / "fetch_exercises.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestFetchScript:
    def test_every_category_is_kept_and_the_primary_named(self, fetch_script):
        library = fetch_script.process_garmin_exercises(RAW_GARMIN)
        side_plank = library["exercises"]["SIDE_PLANK"]
        assert side_plank["categories"] == ["PLANK", "SUSPENSION"]
        assert side_plank["category"] == side_plank["garmin_category"] == "PLANK"
        assert side_plank["is_bodyweight"] is True
        assert side_plank["counterpart"] == "WEIGHTED_SIDE_PLANK"
        assert side_plank["muscles"] == ["obliques", "shoulders"]      # merged across categories
        assert "is_bodyweight" not in library["exercises"]["WEIGHTED_SIDE_PLANK"]
        assert library["exercises"]["TRX_ROW"]["categories"] == ["SUSPENSION"]
        assert library["exercises"]["FACE_PULL"]["category"] == "ROW"

    def test_custom_entries_survive_a_refresh_and_garmin_wins_on_a_clash(self, fetch_script):
        library = fetch_script.process_garmin_exercises(RAW_GARMIN, EXISTING_CACHE)
        assert library["custom_exercises"] == ["HIP_THRUST"]
        hip_thrust = library["exercises"]["HIP_THRUST"]
        assert hip_thrust["custom"] is True and hip_thrust["categories"] == ["HIP_RAISE"]
        assert library["exercises"]["SIDE_PLANK"]["categories"] == ["PLANK", "SUSPENSION"]  # not the legacy entry

    def test_metadata_describes_the_cache(self, fetch_script):
        meta = fetch_script.process_garmin_exercises(RAW_GARMIN, EXISTING_CACHE)["metadata"]
        assert meta["schema"] == 2
        assert meta["exercise_count"] == 5 and meta["custom_count"] == 1
        assert meta["multi_category_count"] == 1
        assert meta["bodyweight_count"] == 1 and meta["counterpart_count"] == 2
        assert meta["category_count"] == 3

    def test_a_refresh_backs_the_old_file_up_first(self, fetch_script, tmp_path):
        target = tmp_path / "exercises.json"
        target.write_text("{}", encoding="utf-8")
        backup = fetch_script.backup_existing(target)
        assert backup.parent == tmp_path / "backups" and backup.read_text(encoding="utf-8") == "{}"
        assert fetch_script.backup_existing(tmp_path / "missing.json") is None

    def test_the_eval_slice_is_schema_two(self):
        slice_ = json.loads((ROOT / "evals" / "authoring" / "exercises.json").read_text(encoding="utf-8"))
        assert all("categories" in e for e in slice_["exercises"].values())
        assert slice_["exercises"]["SIDE_PLANK"]["categories"] == ["PLANK", "SUSPENSION"]
        assert slice_["exercises"]["SIDE_PLANK"]["is_bodyweight"] is True
