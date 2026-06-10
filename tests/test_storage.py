"""
Tests for coach/storage.py (single I/O choke point) and coach/schemas.py
(typed schema layer).

Covers:
- read/write round-trips (incl. utf-8 content survival)
- cross-process lock contention (two real subprocesses) + stale lock + timeout
- migration dry-run (--check) vs real (--migrate) with .v<N>.bak assertion
- every live-file SHAPE parsed via realistic fixtures (synthesized — NOT the
  live data files; live data is never touched by tests)
- validation failure still returns the raw data (flag, never block)
- planner/fitness delegation respects monkeypatched DATA_DIR
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import coach.planner as planner
import coach.fitness as fitness_mod
import coach.storage as storage
from coach.schemas import SCHEMA_MODELS
from coach.storage import (
    apply_migrations,
    check_data_files,
    file_lock,
    main as storage_main,
    migrate_data_files,
    pending_migrations,
    read_json,
    validate_data,
    write_json,
)

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Realistic fixtures mirroring the live file SHAPES (synthesized data)
# ---------------------------------------------------------------------------

ATHLETE_FIXTURE = {
    "training_pillars": {  # new nested-dict form
        "based_on_persona": "multi_sport",
        "customized": True,
        "last_updated": "2026-01-13",
        "pillars": [
            {
                "name": "endurance",
                "description": "Long aerobic efforts",
                "target_hours_per_week": 4,
                "target_type": "hours",
                "types": ["cycling", "running"],
                "priority": "primary",
            },
            {
                "name": "mobility",
                "description": "Injury prevention",
                "target_mins_per_week": 90,  # legacy spelling tolerated
                "target_type": "minutes",
                "types": ["yoga", "pilates"],
                "priority": "high",
            },
        ],
    },
    "personal": {
        "name": "Test Athlete",
        "age": 37,
        "max_hr": 193,
        "resting_hr": 42,
        "hr_zones": {"z1_recovery": [118, 132], "z5_max": [178, 193]},
        "ftp": 216,
        "threshold_pace_sec_per_km": None,
        "pace_zones": {"z1_recovery": None},
        "weight_kg": 85,
        "power_zones": {"z7_neuromuscular": [326, None]},
    },
    "life_constraints": {
        "recurring_commitments": [
            {"day": "Wednesday", "activity": "padel", "time": "afternoon",
             "duration_mins": 90, "priority": "high", "notes": "non-negotiable"},
        ],
        "preferred_training_times": ["morning", "early_evening"],
        "no_training_days": ["Sunday"],  # legacy key, no blocked_days
        "work_schedule": {"type": "flexible", "busy_periods": [], "notes": ""},
        "travel": [{"date": "2026-04-25", "end_date": "2026-04-26",
                    "type": "trip", "notes": "weekend away"}],
    },
    "injury_history": [
        {   # canonical record: type + restricted_activities
            "date": "2026-01-07",
            "type": "peroneal tendinopathy (suspected)",
            "description": "Left lateral ankle pain",
            "location": "left lateral ankle",
            "severity": "severe",
            "status": "improving",
            "cause": "Volume spike",
            "restricted_activities": ["running", "trail_running"],
            "safe_activities": ["cycling", "swimming"],
            "treatment": "Rest, ice",
            "rehab_protocol": {
                "frequency": "3x per week",
                "exercises": [
                    {"name": "Ankle eversion with band", "sets": 3, "reps": 15,
                     "notes": "Resistance band"},
                    {"name": "Single leg balance", "duration_secs": 30, "reps": 3},
                ],
                "ice_protocol": "Ice 15 mins",
                "progression": "Add standing balance",
            },
            "follow_up": "Reassess in 7-10 days",
            "progress_notes": [{"date": "2026-01-10", "note": "Feels better"}],
            "notes": "Partial clearance",
        },
        {   # legacy record: 'restrictions' free-text list, NO restricted_activities
            "date": "2026-03-18",
            "type": "shoulder",
            "description": "Right shoulder impingement",
            "status": "improving",
            "severity": "moderate",
            "restrictions": ["Avoid overhead pressing", "No swimming until cleared"],
            "progress_notes": [{"date": "2026-03-18", "note": "Physio confirmed"}],
        },
        {   # minimal diagnose_injury-shaped record with empty restriction lists
            "date": "2026-02-09",
            "body_region": "ankle",
            "type": "ankle",
            "location": "ankle",
            "severity": "moderate",
            "status": "resolved",
            "onset": "gradual",
            "pain_type": "Dull/aching",
            "location_specific": "Outer side (lateral)",
            "restricted_activities": [],
            "safe_activities": [],
        },
    ],
    "swimming": {"experience": "beginner", "comfortable_distance_m": 900,
                 "pace_per_100m_secs": 140, "strokes": ["freestyle"],
                 "pool_length_m": 25, "notes": "Technique focus"},
    "pilates": {"experience": "beginner", "focus_areas": ["core"]},
    "cycling_technique": {"equipment": {"indoor": "wattbike_atom"},
                          "technique_focus": {"status": "active_block"}},
    "preferences": {"likes": ["MTB"], "dislikes": ["treadmill"],
                    "gym_access": {"type": "commercial_gym"}},
    "coaching_notes": "Posterior chain rehab 3x/week — bands at home, machines at gym.",
    "strength_baseline": {
        "equivalence_groups": {"BENCH_PRESS": ["BARBELL_BENCH_PRESS", "DUMBBELL_BENCH_PRESS"]},
        "exercises": {
            "bench_press": {  # full-form entry
                "canonical_name": "BENCH_PRESS",
                "preferred_variation": "DUMBBELL_BENCH_PRESS",
                "current": {"weight_kg": 12.5, "reps": 12, "sets": 3,
                            "last_performed": "2026-01-18"},
                "history": [{"date": "2026-01-14", "weight_kg": 10.0, "reps": 12,
                             "sets": 3, "variation": "DUMBBELL_BENCH_PRESS"}],
                "progression": {"suggested_weight_kg": 12.5, "suggested_reps": 12,
                                "rationale": "Ready for +2.5kg", "status": "approved",
                                "approved_date": "2026-02-09"},
            },
            "pull_up": {  # flat-form entry (heterogeneous!)
                "current_weight_kg": 42.5,
                "current_reps": 12,
                "current_sets": 3,
                "preferred_variation": "SEATED_CABLE_ROW",
                "notes": "Swapped due to shoulder",
            },
            "core": {"canonical_name": "CORE", "preferred_variation": "PLANK",
                     "current": {"weight_kg": None, "reps": 17, "sets": 3,
                                 "last_performed": "2026-01-18"},
                     "history": [], "progression": None},
        },
        "last_synced": "2026-02-08",
    },
    "baseline_last_refreshed": "2026-02-09",
}

WEEKLY_PLAN_FIXTURE = {
    "days": {
        "2026-06-10": {  # list-of-sessions form with notes
            "planned": [
                {"type": "mobility", "name": "Morning mobility", "duration_mins": 30,
                 "intensity": "easy", "purpose": "Mobility pillar — done.",
                 "description": "34 min mobility."},
                {"type": "padel", "name": "Wednesday padel", "duration_mins": 90,
                 "intensity": "moderate", "purpose": "Fun pillar", "description": "Social."},
            ],
            "notes": "Readiness 66.",
        },
        "2026-06-11": {  # session with exercises list
            "planned": [
                {"type": "strength", "name": "Rehab set", "duration_mins": 10,
                 "intensity": "easy", "purpose": "Bio rehab 1/3",
                 "exercises": [
                     {"name": "CALF_RAISE", "sets": 3, "reps": 20, "rest_secs": 60,
                      "notes": "Slow controlled."},
                 ]},
            ],
        },
        "2026-06-15": {  # session with nested structure (repeat/steps)
            "planned": [
                {"type": "running", "name": "L1 TEST", "duration_mins": 24,
                 "intensity": "easy", "purpose": "Gated walk/run restart",
                 "description": "Flat, soft surface.",
                 "structure": [
                     {"phase": "warmup", "distance_m": 300, "intensity": "easy",
                      "notes": "Walk"},
                     {"phase": "repeat", "iterations": 4, "steps": [
                         {"phase": "interval", "duration_mins": 4, "intensity": "easy"},
                         {"phase": "rest", "duration_mins": 2, "notes": "Walk"},
                     ]},
                     {"phase": "cooldown", "distance_m": 300, "intensity": "easy"},
                 ]},
            ],
        },
        "2026-06-16": {  # legacy dict-form planned + audit fields
            "day_name": "Tuesday",
            "planned": {"type": "strength", "description": "Full-body",
                        "duration_mins": 40, "intensity": "moderate",
                        "purpose": "Strength pillar 2/2"},
            "actual": None,
            "status": "pending",
            "notes": "",
        },
        "2026-06-17": {"planned": None, "status": "pending"},  # rest day
    },
    "rationale": "Reset week.",
    "pushed_workout_ids": [1596313856, 1596313832],
    "week_start": "2026-06-10",
    "week_end": "2026-06-17",
    "last_updated": "2026-06-10",
}

TRAINING_CONFIG_FIXTURE = {
    # stored state
    "events": [
        {"date": "2026-11-28", "name": "UTCT", "priority": "A", "type": "trail_ultra",
         "distance_km": 35, "target_time": None, "url": "https://example.com",
         "notes": "Distance TBC"},
    ],
    "current_block": {"phase": "base", "start_date": "2026-06-08",
                      "end_date": "2026-07-05", "weekly_volume_target_hrs": 6.5,
                      "focus": ["aerobic base"], "notes": "Post-race reset"},
    "periodization": {"current_phase": "base", "phase_start": "2026-06-08",
                      "target_transition": "2026-08-01", "a_race_date": None,
                      "phases": {"base": {"focus": "volume"}, "build": {}, "peak": {},
                                 "taper": {}}},
    "goals": [
        {"id": "goal_1", "name": "Finish UTCT", "type": "race", "priority": "A",
         "target_date": "2026-11-28", "description": "Trail ultra",
         "success_criteria": "Finish healthy", "key_metrics": [],
         "linked_event": "UTCT", "status": "active", "achieved_date": "",
         "outcome": ""},
    ],
    # derived-view pollution that historically leaked into the live file —
    # must be tolerated, never destroyed:
    "thresholds": {"hard_hr_avg": 150, "hard_hr_max": 175,
                   "long_effort_min_mins": 60, "volume_compliance_percent": 80},
    "race_analysis": {"elevation_significance_m": 1000, "high_altitude_m": 1500},
    "weekly_structure": {"preferred_long_day": "Saturday",
                         "preferred_strength_days": ["Tuesday", "Friday"],
                         "rest_after_game": True, "max_consecutive_hard": 2},
    "constraints": {"max_consecutive_hard_days": 2,
                    "mandatory_rest_after_race_days": 2,
                    "max_weekly_volume_increase_percent": 15},
    "race_requirements": {"trail_ultra": {"description": "x", "key_sessions": [],
                                          "phase_guidance": {}}},
    "personas": {"_description": "x", "multi_sport": {"description": "x",
                 "suggested_pillars": [], "typical_weekly_hours": "6-10",
                 "key_focus": "balance"}},
    "pillars": {"endurance_hours_per_week": 4, "strength_sessions_per_week": 2},
    "athlete_pillars": {"based_on_persona": "multi_sport", "customized": True,
                        "last_updated": "2026-01-13", "pillars": []},
    "pillars_source": "athlete.json",
}

FITNESS_HISTORY_FIXTURE = {
    "schema_version": 2,
    "daily_loads": {
        "2026-06-08": {
            "total": 422.6,
            "by_sport": {"cycling": 422.6},
            "activities": [
                {"id": 21799535700, "type": "cycling", "sport": "cycling",
                 "duration_mins": 83.8, "load": 136.3, "avg_hr": 141.0,
                 "norm_power": None},
            ],
        },
        "2026-06-09": {"total": 0.0, "by_sport": {}, "activities": []},
    },
    "snapshots": [
        {"date": "2026-06-08",
         "total": {"ctl": 80.9, "atl": 80.0, "tsb": 0.9, "acwr": 0.99},
         "cycling": {"ctl": 60.5, "atl": 79.5, "tsb": -19.0, "acwr": 1.31},
         "running": {"ctl": 2.3, "atl": 0.1, "tsb": 2.2, "acwr": 0.04},
         "strength": {"ctl": 1.2, "atl": 0.3, "tsb": 0.9, "acwr": 0.25}},
    ],
    "sleep_history": [
        {"date": "2026-06-07", "bedtime": 1779316195000,  # epoch-ms form
         "wake_time": 1779345655000, "duration_hrs": 7.9, "score": 90,
         "deep_pct": 15.0, "rem_pct": 25.0, "light_pct": 60.0,
         "awake_mins": 19.0, "avg_hr": 46.0, "respiration": 13.0,
         "sleep_stress": 11.0},
        {"date": "2026-06-08", "bedtime": "2026-06-08T22:31:00",  # ISO form
         "wake_time": "2026-06-09T06:12:00", "duration_hrs": 7.7, "score": 84,
         "deep_mins": 70, "deep_pct": 15.2, "rem_mins": 115, "rem_pct": 24.9,
         "light_mins": 277, "light_pct": 59.9, "awake_mins": 12.0,
         "avg_hr": 44.0, "respiration": 12.8, "sleep_stress": 9.0},
    ],
    "readiness_history": [
        {"date": "2026-06-08", "score": 40, "level": "LOW",
         "hrv_status": "BALANCED", "body_battery": None},
    ],
    "last_updated": "2026-06-09",
    "last_activity_ingest_date": "2026-06-09",
}

COACHING_LOG_FIXTURE = {
    "decisions": [
        {"id": "dec_20260610_1", "date": "2026-06-10", "type": "load_adjustment",
         "decision": "Hold volume flat", "rationale": "ACWR 0.91, base reset",
         "status": "active", "outcome": "", "review_date": "2026-06-17",
         "status_updated": "2026-06-10"},
    ],
    "pending_approvals": [
        {"id": "prop_1", "action_type": "phase_transition",
         "proposal": "base -> build", "rationale": "4 weeks of base done",
         "impact": "major", "expires": "2026-06-20", "status": "pending"},
    ],
    "athlete_responses": [
        {"date": "2026-06-09", "stimulus": "Long Z2 ride 135min",
         "response": "Felt strong next day", "pattern": "handles_volume_well"},
    ],
    "metadata": {"created": "2026-01-01", "last_updated": "2026-06-10",
                 "version": "1.0"},
}

METHODOLOGY_FIXTURE = {
    "default_pillar_templates": {"_note": "defaults", "strength_sessions_per_week": 2,
                                 "mobility_minutes_per_week": 90,
                                 "long_effort_per_week": 1,
                                 "long_effort_min_duration_mins": 60},
    "personas": {"_description": "x", "multi_sport": {"description": "x",
                 "suggested_pillars": [], "typical_weekly_hours": "6-10",
                 "key_focus": "balance"}},
    "safety_constraints": {"max_consecutive_hard_days": 2,
                           "mandatory_rest_after_race_days": 2,
                           "max_weekly_volume_increase_percent": 15},
    "race_templates": {
        "multi_day_mtb": {
            "description": "3-day stage race",
            "key_sessions": [{"type": "long_ride", "priority": "critical"}],
            "phase_guidance": {"base": "Volume focus."},
        },
    },
    "activity_classification": {"strength_types": ["strength_training"],
                                "mobility_types": ["yoga", "pilates"],
                                "cardio_types": ["cycling", "running"],
                                "high_intensity_types": ["intervals"]},
    "recovery_protocols": {"pre_sleep_stretching": {"description": "x",
                           "principles": [], "by_activity": {}, "ask_athlete": []}},
    "training_protocols": {"cycling_technique": {"description": "x",
                           "drill_library": {}}},
    "pillars": {"strength_sessions_per_week": 2, "strength_program": "lean_and_mean_6wk"},
    "strength_programs": {"lean_and_mean_6wk": {"name": "Lean", "duration_weeks": 6,
                          "structure": "x", "schedule": {}, "workouts": {}}},
    "session_guidelines": {"_description": "x",
                           "long_ride": {"base": {"duration_range": [120, 180]}}},
}

ATHLETE_BASELINE_FIXTURE = {
    "last_refreshed": "2026-02-09",
    "baseline": {
        "avg_weekly_volume_hrs": 6.2,
        "max_weekly_volume_hrs": 11.4,
        "activity_distribution": {"running": 37, "cycling": 65, "strength_training": 22},
        "typical_week": {"running": 1.2, "cycling": 3.4, "strength_training": 1.1},
        "total_activities": 180,
        "weeks_analyzed": 26,
    },
    "personal_records": [
        {"record_type": "fastest_5k", "value": 1320.0, "value_formatted": 22.0,
         "unit": None, "date": "2025-06-15", "activity_id": 11111111111},
    ],
    "garmin_profile": {
        "full_name": "Test Athlete", "display_name": "test", "weight_kg": 85.1,
        "weight_date": "2026-02-08", "birth_date": "1989-01-01", "age": 37,
        "max_hr": None,
        "hr_zones": {"z1_recovery": [118, 132], "max_hr": 193, "lthr": 172},
    },
}

EXERCISE_LIBRARY_FIXTURE = {
    "dumbbell bench press": {  # researched full-form entry
        "setup": "Lie on bench", "execution": "Press up",
        "key_cues": ["wrists stacked", "feet planted"],
        "common_mistakes": ["flaring elbows"],
        "video_url": "https://example.com/v", "garmin_note": "Press; cues...",
    },
    "banded monster walk": {  # newer research_exercise shape
        "exercise": "banded monster walk",
        "form_cues": {"note": "x", "suggested_searches": []},
        "garmin_note": "x", "video_url": "",
        "modifications": {"easier": "x", "harder": "y",
                          "equipment_alternatives": "z"},
        "sources": [], "researched_date": "2026-03-01",
    },
}

LIVE_SHAPE_FIXTURES = {
    'athlete.json': ATHLETE_FIXTURE,
    'weekly_plan.json': WEEKLY_PLAN_FIXTURE,
    'training_config.json': TRAINING_CONFIG_FIXTURE,
    'fitness_history.json': FITNESS_HISTORY_FIXTURE,
    'coaching_log.json': COACHING_LOG_FIXTURE,
    'methodology.json': METHODOLOGY_FIXTURE,
    'athlete_baseline.json': ATHLETE_BASELINE_FIXTURE,
    'exercise_library.json': EXERCISE_LIBRARY_FIXTURE,
}


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------

class TestRoundTrips:
    def test_write_then_read_returns_equal_data(self, data_dir):
        payload = {"days": {"2026-06-10": {"planned": None}}, "rationale": "x"}
        write_json('weekly_plan.json', payload, data_dir=data_dir)
        loaded = read_json('weekly_plan.json', data_dir=data_dir)
        # Migration adds canonical keys but original content is intact
        assert loaded['days'] == payload['days']
        assert loaded['rationale'] == 'x'

    def test_missing_file_returns_empty_dict(self, data_dir):
        assert read_json('nonexistent.json', data_dir=data_dir) == {}

    def test_unknown_filename_no_validation_no_migration(self, data_dir):
        payload = {"archived_days": [{"date": "2026-06-01"}]}
        write_json('plan_history.json', payload, data_dir=data_dir)
        loaded = read_json('plan_history.json', data_dir=data_dir)
        assert loaded == payload
        assert 'schema_version' not in loaded

    def test_utf8_content_survives_round_trip(self, data_dir):
        payload = {
            "metadata": {"note": "em—dash, café, snel — fiets 🚴, 中文"},
            "decisions": [],
        }
        write_json('coaching_log.json', payload, data_dir=data_dir)
        loaded = read_json('coaching_log.json', data_dir=data_dir)
        assert loaded['metadata']['note'] == payload['metadata']['note']

    def test_file_on_disk_is_ascii_safe_for_legacy_readers(self, data_dir):
        """Output stays ASCII-escaped so modules that still open() files
        without an explicit encoding (cp1252 on Windows) can't mis-decode."""
        write_json('coaching_log.json', {"metadata": {"note": "café — ok"}},
                   data_dir=data_dir)
        raw = (data_dir / 'coaching_log.json').read_bytes()
        assert max(raw) < 0x80  # pure ASCII bytes

    def test_reads_raw_utf8_files_written_by_hand(self, data_dir):
        """Files containing raw (unescaped) UTF-8 must read correctly — this
        was the cp1252 bug on Windows."""
        path = data_dir / 'coaching_log.json'
        path.write_text(json.dumps({"metadata": {"note": "café — 🚴"}},
                                   ensure_ascii=False), encoding='utf-8')
        loaded = read_json('coaching_log.json', data_dir=data_dir)
        assert loaded['metadata']['note'] == "café — 🚴"

    def test_atomic_write_leaves_no_temp_files(self, data_dir):
        write_json('coaching_log.json', {"decisions": []}, data_dir=data_dir)
        leftovers = [p for p in data_dir.iterdir()
                     if p.suffix == '.tmp' or p.name.endswith('.lock')]
        assert leftovers == []


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------

_LOCK_CHILD_SCRIPT = """
import json, sys, time
from pathlib import Path
from coach.storage import file_lock

target = Path(sys.argv[1])
out = Path(sys.argv[2])
hold = float(sys.argv[3])
with file_lock(target, timeout=30):
    start = time.time()
    time.sleep(hold)
    end = time.time()
out.write_text(json.dumps([start, end]), encoding='utf-8')
"""


class TestFileLock:
    def test_two_processes_never_hold_lock_simultaneously(self, tmp_path):
        """Real cross-process contention: two subprocesses hold the same lock
        and record their hold windows — the windows must not overlap."""
        script = tmp_path / 'lock_child.py'
        script.write_text(_LOCK_CHILD_SCRIPT, encoding='utf-8')
        target = tmp_path / 'shared.json'
        env = {**os.environ, 'PYTHONPATH': str(REPO_ROOT)}

        procs = []
        outputs = []
        for i in range(2):
            out = tmp_path / f'window_{i}.json'
            outputs.append(out)
            procs.append(subprocess.Popen(
                [sys.executable, str(script), str(target), str(out), '0.6'],
                env=env, cwd=str(REPO_ROOT),
            ))
        for p in procs:
            assert p.wait(timeout=60) == 0

        windows = [json.loads(o.read_text(encoding='utf-8')) for o in outputs]
        (s1, e1), (s2, e2) = windows
        overlap = min(e1, e2) - max(s1, s2)
        assert overlap <= 0.05, f"lock windows overlapped by {overlap:.3f}s"
        # Lock released afterwards
        assert not (tmp_path / 'shared.json.lock').exists()

    def test_lock_released_on_exception(self, tmp_path):
        target = tmp_path / 'f.json'
        with pytest.raises(RuntimeError):
            with file_lock(target):
                raise RuntimeError('boom')
        assert not (tmp_path / 'f.json.lock').exists()
        # Reacquirable immediately
        with file_lock(target, timeout=1):
            pass

    def test_fresh_lock_times_out(self, tmp_path):
        target = tmp_path / 'f.json'
        lock = tmp_path / 'f.json.lock'
        lock.write_text('pid=999999', encoding='utf-8')  # held, fresh mtime
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.3, stale_after=30):
                pass
        assert time.monotonic() - start < 5

    def test_stale_lock_is_broken(self, tmp_path):
        target = tmp_path / 'f.json'
        lock = tmp_path / 'f.json.lock'
        lock.write_text('pid=999999', encoding='utf-8')
        stale_time = time.time() - 60  # older than the 30s stale window
        os.utime(lock, (stale_time, stale_time))
        with file_lock(target, timeout=2):  # must break the stale lock
            assert lock.exists()  # we now hold it
        assert not lock.exists()


# ---------------------------------------------------------------------------
# Migrations: registry, dry-run vs real, .bak discipline
# ---------------------------------------------------------------------------

class TestMigrations:
    def _write_raw(self, data_dir, filename, data):
        (data_dir / filename).write_text(json.dumps(data, indent=2),
                                         encoding='utf-8')

    def test_weekly_plan_derives_week_bounds(self):
        plan = {"days": {"2026-06-12": {"planned": None},
                         "2026-06-10": {"planned": None}}}
        migrated, applied = apply_migrations('weekly_plan.json', plan)
        assert applied == [(0, 1)]
        assert migrated['week_start'] == '2026-06-10'
        assert migrated['week_end'] == '2026-06-12'
        assert migrated['schema_version'] == 1

    def test_weekly_plan_keeps_existing_week_bounds(self):
        plan = {"days": {"2026-06-12": {}}, "week_start": "2026-06-08",
                "week_end": "2026-06-14"}
        migrated, _ = apply_migrations('weekly_plan.json', plan)
        assert migrated['week_start'] == '2026-06-08'
        assert migrated['week_end'] == '2026-06-14'

    def test_athlete_mirrors_no_training_days_to_blocked_days(self):
        athlete = {"life_constraints": {"no_training_days": ["Sunday"]}}
        migrated, applied = apply_migrations('athlete.json', athlete)
        assert applied == [(0, 1)]
        # BOTH keys present after migration — nothing renamed or deleted
        assert migrated['life_constraints']['no_training_days'] == ['Sunday']
        assert migrated['life_constraints']['blocked_days'] == ['Sunday']

    def test_athlete_mirrors_blocked_days_back(self):
        athlete = {"life_constraints": {"blocked_days": ["Wednesday"]}}
        migrated, _ = apply_migrations('athlete.json', athlete)
        assert migrated['life_constraints']['no_training_days'] == ['Wednesday']
        assert migrated['life_constraints']['blocked_days'] == ['Wednesday']

    def test_migrations_never_downgrade(self):
        data = {"schema_version": 5, "days": {}}
        migrated, applied = apply_migrations('weekly_plan.json', data)
        assert applied == []
        assert migrated['schema_version'] == 5

    def test_fitness_history_v1_floats_migrated_to_v2(self):
        legacy = {"daily_loads": {"2026-02-02": 17.1}, "snapshots": []}
        migrated, applied = apply_migrations('fitness_history.json', legacy)
        assert applied == [(0, 2)]
        assert migrated['daily_loads']['2026-02-02']['total'] == 17.1
        assert migrated['schema_version'] == 2

    def test_exercise_library_is_never_stamped(self, data_dir):
        write_json('exercise_library.json', dict(EXERCISE_LIBRARY_FIXTURE),
                   data_dir=data_dir)
        loaded = read_json('exercise_library.json', data_dir=data_dir)
        assert 'schema_version' not in loaded
        assert set(loaded) == set(EXERCISE_LIBRARY_FIXTURE)

    # -- dry-run vs real ----------------------------------------------------

    def test_check_is_a_pure_dry_run(self, data_dir):
        plan = {"days": {"2026-06-10": {"planned": None}}}
        self._write_raw(data_dir, 'weekly_plan.json', plan)
        before = (data_dir / 'weekly_plan.json').read_bytes()

        report = check_data_files(data_dir)

        assert report['weekly_plan.json']['pending_migrations'] == [(0, 1)]
        assert report['weekly_plan.json']['validation_problems'] == []
        # Nothing written: same bytes, no .bak, no extra files
        assert (data_dir / 'weekly_plan.json').read_bytes() == before
        assert list(data_dir.glob('*.bak')) == []

    def test_cli_check_exit_zero_and_no_writes(self, data_dir, capsys):
        self._write_raw(data_dir, 'weekly_plan.json',
                        {"days": {"2026-06-10": {"planned": None}}})
        before = (data_dir / 'weekly_plan.json').read_bytes()

        rc = storage_main(['--check', '--data-dir', str(data_dir)])

        assert rc == 0
        assert (data_dir / 'weekly_plan.json').read_bytes() == before
        out = capsys.readouterr().out
        assert 'weekly_plan.json' in out
        assert 'dry-run' in out

    def test_real_migration_writes_bak_then_upgrades(self, data_dir):
        plan = {"days": {"2026-06-12": {"planned": None},
                         "2026-06-10": {"planned": None}},
                "rationale": "athlete-authored text — must survive"}
        self._write_raw(data_dir, 'weekly_plan.json', plan)
        original_bytes = (data_dir / 'weekly_plan.json').read_bytes()

        report = migrate_data_files(data_dir)

        entry = report['weekly_plan.json']
        assert entry['migrated'] is True
        assert entry['applied'] == [(0, 1)]
        # .v<N>.bak holds the EXACT pre-migration bytes
        bak = data_dir / 'weekly_plan.json.v0.bak'
        assert entry['backup'] == bak.name
        assert bak.exists()
        assert bak.read_bytes() == original_bytes
        # Upgraded file: canonical keys added, athlete content intact
        upgraded = json.loads((data_dir / 'weekly_plan.json').read_text(encoding='utf-8'))
        assert upgraded['schema_version'] == 1
        assert upgraded['week_start'] == '2026-06-10'
        assert upgraded['week_end'] == '2026-06-12'
        assert upgraded['rationale'] == plan['rationale']
        assert upgraded['days'] == plan['days']

    def test_migrate_is_idempotent_and_bak_is_one_time(self, data_dir):
        self._write_raw(data_dir, 'weekly_plan.json',
                        {"days": {"2026-06-10": {"planned": None}}})
        migrate_data_files(data_dir)
        bak = data_dir / 'weekly_plan.json.v0.bak'
        bak_bytes = bak.read_bytes()
        file_bytes = (data_dir / 'weekly_plan.json').read_bytes()

        report = migrate_data_files(data_dir)  # second run: no-op

        assert report['weekly_plan.json']['migrated'] is False
        assert bak.read_bytes() == bak_bytes  # backup NOT overwritten
        assert (data_dir / 'weekly_plan.json').read_bytes() == file_bytes

    def test_normal_save_over_v0_file_also_takes_bak(self, data_dir):
        """The first migrating WRITE through the normal save path must also
        leave a .v0.bak of the pre-upgrade file."""
        original = {"life_constraints": {"no_training_days": ["Sunday"]}}
        self._write_raw(data_dir, 'athlete.json', original)
        original_bytes = (data_dir / 'athlete.json').read_bytes()

        updated = {"life_constraints": {"no_training_days": ["Sunday"]},
                   "personal": {"name": "Test"}}
        write_json('athlete.json', updated, data_dir=data_dir)

        bak = data_dir / 'athlete.json.v0.bak'
        assert bak.exists()
        assert bak.read_bytes() == original_bytes
        saved = json.loads((data_dir / 'athlete.json').read_text(encoding='utf-8'))
        assert saved['schema_version'] == 1
        assert saved['life_constraints']['blocked_days'] == ['Sunday']
        assert saved['life_constraints']['no_training_days'] == ['Sunday']

    def test_read_migrates_in_memory_but_never_writes(self, data_dir):
        self._write_raw(data_dir, 'weekly_plan.json',
                        {"days": {"2026-06-10": {"planned": None}}})
        before = (data_dir / 'weekly_plan.json').read_bytes()

        loaded = read_json('weekly_plan.json', data_dir=data_dir)

        # Caller sees canonical shape...
        assert loaded['schema_version'] == 1
        assert loaded['week_start'] == '2026-06-10'
        # ...but plain reads never touch disk
        assert (data_dir / 'weekly_plan.json').read_bytes() == before
        assert list(data_dir.glob('*.bak')) == []


# ---------------------------------------------------------------------------
# Schema validation: every live-file SHAPE parses; failures flag, never block
# ---------------------------------------------------------------------------

class TestSchemaShapes:
    @pytest.mark.parametrize('filename', sorted(LIVE_SHAPE_FIXTURES))
    def test_live_shape_fixture_validates_cleanly(self, filename):
        problems = validate_data(filename, LIVE_SHAPE_FIXTURES[filename])
        assert problems == [], f"{filename}: {problems}"

    @staticmethod
    def _assert_content_preserved(original, loaded, path=''):
        """Every original key/value must survive. Migrations may ADD canonical
        keys (e.g. the blocked_days mirror) but never alter or remove content."""
        if isinstance(original, dict):
            assert isinstance(loaded, dict), f"type changed at {path}"
            for key, value in original.items():
                assert key in loaded, f"key removed at {path}.{key}"
                TestSchemaShapes._assert_content_preserved(
                    value, loaded[key], f"{path}.{key}")
        else:
            assert loaded == original, f"value altered at {path}"

    @pytest.mark.parametrize('filename', sorted(LIVE_SHAPE_FIXTURES))
    def test_live_shape_fixture_round_trips_through_storage(self, filename, data_dir):
        original = json.loads(json.dumps(LIVE_SHAPE_FIXTURES[filename]))
        write_json(filename, original, data_dir=data_dir)
        loaded = read_json(filename, data_dir=data_dir)
        self._assert_content_preserved(LIVE_SHAPE_FIXTURES[filename], loaded, filename)

    def test_athlete_legacy_list_pillars_validates(self):
        athlete = {"training_pillars": [
            {"name": "strength", "target_type": "sessions",
             "target_sessions_per_week": 2, "types": ["strength_training"]},
        ]}
        assert validate_data('athlete.json', athlete) == []

    def test_athlete_name_keyed_dict_pillars_validates(self):
        athlete = {"training_pillars": {
            "strength": {"target_type": "sessions", "target_sessions_per_week": 2,
                         "types": ["strength_training"]},
        }}
        assert validate_data('athlete.json', athlete) == []

    def test_weekly_plan_dict_and_list_planned_both_validate(self):
        plan = {"days": {
            "2026-06-10": {"planned": {"type": "running", "duration_mins": 30}},
            "2026-06-11": {"planned": [{"type": "strength"}, {"type": "yoga"}]},
            "2026-06-12": {"planned": None},
        }}
        assert validate_data('weekly_plan.json', plan) == []

    def test_weekly_plan_non_iso_day_key_is_flagged(self):
        plan = {"days": {"next tuesday": {"planned": None}}}
        problems = validate_data('weekly_plan.json', plan)
        assert problems  # flagged...
        assert any('ISO' in p or 'iso' in p for p in problems)

    def test_empty_dicts_validate_for_all_object_schemas(self):
        for filename in SCHEMA_MODELS:
            if filename == 'exercise_library.json':
                continue  # root mapping — {} is also valid for it, see below
            assert validate_data(filename, {}) == [], filename
        assert validate_data('exercise_library.json', {}) == []

    def test_validation_failure_still_returns_raw_data(self, data_dir, caplog):
        """Validation FLAGS (warning naming the file) but the read returns
        the raw data untouched — reads are never blocked."""
        bad = {"personal": {"name": "Test", "age": "thirty-seven"},
               "custom_section": {"kept": True}}
        (data_dir / 'athlete.json').write_text(json.dumps(bad), encoding='utf-8')

        with caplog.at_level('WARNING', logger='coach.storage'):
            loaded = read_json('athlete.json', data_dir=data_dir)

        assert loaded['personal']['age'] == 'thirty-seven'  # raw data intact
        assert loaded['custom_section'] == {'kept': True}
        # The warning names the offending file
        assert any('athlete.json' in rec.getMessage() for rec in caplog.records)

    def test_validation_failure_does_not_block_writes(self, data_dir):
        bad = {"days": "this should be a dict"}
        write_json('weekly_plan.json', bad, data_dir=data_dir)
        assert (data_dir / 'weekly_plan.json').exists()
        loaded = json.loads((data_dir / 'weekly_plan.json').read_text(encoding='utf-8'))
        assert loaded['days'] == 'this should be a dict'

    def test_cli_check_reports_validation_problems_nonzero_exit(self, data_dir, capsys):
        (data_dir / 'athlete.json').write_text(
            json.dumps({"personal": {"age": "not-a-number"}}), encoding='utf-8')
        rc = storage_main(['--check', '--data-dir', str(data_dir)])
        assert rc == 1
        assert 'athlete.json' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Delegation: planner + fitness now route through storage
# ---------------------------------------------------------------------------

class TestDelegation:
    def test_planner_save_load_respect_monkeypatched_data_dir(self, data_dir, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
        planner.save_json_file('coaching_log.json', {'decisions': [], 'metadata': {}})
        assert (data_dir / 'coaching_log.json').exists()
        loaded = planner.load_json_file('coaching_log.json')
        assert loaded['decisions'] == []
        assert loaded['schema_version'] == 1  # stamped on write

    def test_planner_load_missing_file_returns_empty(self, data_dir, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
        assert planner.load_json_file('athlete.json') == {}

    def test_planner_save_is_utf8_safe(self, data_dir, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
        planner.save_json_file('athlete.json',
                               {'coaching_notes': 'volgehou — café 🚴'})
        loaded = planner.load_json_file('athlete.json')
        assert loaded['coaching_notes'] == 'volgehou — café 🚴'

    def test_save_fitness_history_routes_through_storage(self, data_dir, monkeypatch):
        monkeypatch.setattr(fitness_mod, 'DATA_DIR', data_dir)
        history = {'schema_version': 2, 'daily_loads': {}, 'snapshots': [],
                   'sleep_history': []}
        fitness_mod.save_fitness_history(history)
        path = data_dir / 'fitness_history.json'
        assert path.exists()
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['schema_version'] == 2  # lineage respected, not downgraded
        assert saved['last_updated']  # stamped by save_fitness_history
        # No stray temp/lock files
        assert [p for p in data_dir.iterdir() if p.suffix == '.tmp'] == []

    def test_save_weekly_plan_end_to_end_with_storage(self, data_dir, monkeypatch):
        from datetime import date
        monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
        today = date.today().isoformat()
        planner.save_weekly_plan({'days': {today: {'planned': None}}})
        saved = json.loads((data_dir / 'weekly_plan.json').read_text(encoding='utf-8'))
        assert saved['week_start'] == today
        assert saved['schema_version'] == 1
