---
name: strength-session-author
description: Compose a gym, rehab or prehab session as the `exercises` list the coach server renders onto the Garmin watch — Garmin exercise keys, sets with reps or timed holds, per-side cues, circuits, explicit loads and an explicit warm-up — then run preview_workout until the step tree matches the brief and save with update_weekly_plan. Use whenever a strength, gym, physio-protocol or rehab-circuit session is planned, reviewed or repaired.
---

# Authoring a strength or rehab session

The server renders exactly what the session says and defaults nothing: a
missing `sets`, an unknown key or a muscle-group word where a Garmin category
belongs is an error that names the allowed keys. Your job is the judgment —
which movements, how much, in what order, with which cue — written in the
contract so the watch shows it as you meant it.

## Before composing

1. `get_coaching_snapshot()` — current date, injuries (hard gate: nothing in a
   `restricted_activities` list, ever), `capacity.strength` (recent session
   lengths), the week grid.
2. `get_strength_baseline()` — the athlete's current loads per movement group.
   Loads are written explicitly as `weight_kg`; the renderer never picks one.
3. `list_exercises(search=...)` for Garmin exercise keys; `research_exercise`
   when a movement is new. A name that is not a Garmin key still renders, with
   the readable name in the step description, but then needs a `category`.
4. The physio's or athlete's own words for cues. They go into `notes`
   verbatim (the watch shows about 50 characters; put the safety word first:
   "No jump. Push off left").
5. Coaching memory: `adaptation_patterns` and the last `record_athlete_response`
   for this kind of session, and the snapshot's sleep and readiness signals.
   Size today's session from how the athlete responded last time, not from
   what the plan intended.

## Composition judgment

- Order: warm-up (`warmup_mins`, explicit), the main compound movement while
  fresh, accessories, rehab/prehab, core. Rehab-first when the protocol says so.
- Volume: anchor sets and reps on what the athlete has done (baseline,
  `sync_strength_session` history), progress one variable at a time
  (load, then reps, then sets). Never jump a new movement to working weight.
- Rest: `rest_secs` renders a timed rest step; omit it for a lap-button rest
  the athlete controls. Typical: 90–180 s heavy compound, 45–60 s accessory,
  30–45 s rehab. `rest_secs: 0` removes the rest step (flow between
  movements inside a circuit).
- Holds and carries are `duration_secs` per set, not reps. Per-side work is
  `each_side: true` (the cue says "each side"; reps stay per side).
- Supersets, Tabata rounds and rehab circuits are a circuit:
  `{"rounds": N, "exercises": [...], "rest_secs": S}` — the exercises run back
  to back N times; they carry reps or duration_secs, not sets.
- A rep range ("8–10") is `reps: 8` plus the range in `notes`; "to failure" is
  `reps: "open"` (the set ends with the lap button).
- Injury or return-to-training: the restriction words in the cue ("Toe
  down, slow"; "No overhead"), unilateral variants, slower tempo in `notes`,
  lighter explicit loads. When Garmin's nearest catalogue key names the very
  thing the athlete must avoid (SIDE_LUNGE_JUMP_OFF for a no-jump side lunge),
  do not use that key: write an off-catalogue readable name with the category
  (`"name": "Side lunge no jump", "category": "LUNGE"`) so the wrist shows
  "Side Lunge No Jump", and keep the restriction first in the cue. The save
  rejects an exercise whose key or category carries a restricted movement
  word from an active injury (`injury_gate`).
- `duration_mins` is the planned length; the preview's `estimated_mins` is a
  check that volume and rests fit it.

## Writing it in the contract

| You want | Write |
|---|---|
| 3 sets of 10 goblet squats at 24 kg, 90 s rest | `{"name": "GOBLET_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 24, "rest_secs": 90}` |
| 3 × 45 s plank | `{"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 45}` |
| 2 × 20 banded eversions per side (not a Garmin key) | `{"name": "BANDED_ANKLE_EVERSION", "category": "BANDED_EXERCISES", "sets": 2, "reps": 20, "each_side": true}` |
| Superset push-ups and dead bugs, 3 rounds | `{"rounds": 3, "rest_secs": 60, "exercises": [{"name": "PUSH_UP", "category": "PUSH_UP", "reps": 10}, {"name": "DEAD_BUG", "category": "HIP_STABILITY", "reps": 8, "each_side": true}]}` |
| A 5-minute warm-up block first | session-level `"warmup_mins": 5` |
| The physio's cue | `"notes": "Physio: slow eccentric, stop at pain 3/10"` |

Category words are Garmin's 35: BANDED_EXERCISES, BATTLE_ROPE, BENCH_PRESS,
CALF_RAISE, CARDIO, CARRY, CHOP, CORE, CRUNCH, CURL, DEADLIFT, FLYE, HIP_RAISE,
HIP_STABILITY, HIP_SWING, HYPEREXTENSION, LATERAL_RAISE, LEG_CURL, LEG_RAISE,
LUNGE, OLYMPIC_LIFT, PLANK, PLYO, PULL_UP, PUSH_UP, ROW, SHOULDER_PRESS,
SHOULDER_STABILITY, SHRUG, SIT_UP, SQUAT, SUSPENSION, TOTAL_BODY,
TRICEPS_EXTENSION, WARM_UP. "Glutes" or "core stability" are rejected with
suggestions. `preview_workout()` with no session returns the full contract.

## Worked example — rehab-led lower body, 40 minutes

```json
{
  "type": "strength",
  "name": "Physio lower body A",
  "description": "Physio protocol + posterior chain",
  "purpose": "Ankle stability under load before the return-to-run ladder restarts",
  "duration_mins": 40,
  "intensity": "easy",
  "warmup_mins": 5,
  "exercises": [
    {"name": "SINGLE_LEG_STANDING_CALF_RAISE", "category": "CALF_RAISE", "sets": 2, "reps": 20,
     "each_side": true, "notes": "Slow eccentric, full range, hold the rail lightly"},
    {"name": "DUMBBELL_BULGARIAN_SPLIT_SQUAT", "category": "SQUAT", "sets": 3, "reps": 8,
     "each_side": true, "weight_kg": 10, "rest_secs": 90, "notes": "6-8 each leg, knee tracks over the toe"},
    {"name": "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL", "category": "DEADLIFT", "sets": 3, "reps": 10,
     "each_side": true, "weight_kg": 8, "rest_secs": 60, "notes": "Unstable surface, toe down, slow"},
    {"name": "Side lunge no jump", "category": "LUNGE", "sets": 2, "reps": 8, "each_side": true,
     "notes": "No jump. Push off the left foot, finish tall"},
    {"rounds": 3, "rest_secs": 60, "name": "Core finisher", "exercises": [
      {"name": "DEAD_BUG", "category": "HIP_STABILITY", "reps": 10, "notes": "Slow, low back flat"},
      {"name": "COPENHAGEN_PLANK", "category": "PLANK", "duration_secs": 20, "each_side": true}
    ]}
  ]
}
```

## The loop, then the save

1. `preview_workout(session)` — read `steps` as the athlete will see them on the
   wrist. Fix every entry in `errors` (the message names the allowed keys).
   Decide on every `warnings` entry: an off-catalogue name means the watch shows
   the category plus your readable name; a cue over 50 characters is cut at a
   word boundary.
2. Compare the tree with the brief: every movement present, sets and reps as
   intended, holds timed, per-side where it matters, loads only where you stated
   them, cues carrying the safety words.
3. `update_weekly_plan(plan=...)` — `authoring_error` means step 1 was
   skipped. Then `push_plan_to_garmin()` and show the athlete the `steps` from
   the preview before confirming.

Checklist before saving: every exercise has `sets` and `reps` or
`duration_secs`; every off-catalogue name has a `category`; every cue is in
`notes`, safety word first (without notes the researched library cue goes on
the watch and the preview flags it); every loaded movement states the
`weight_kg` you looked up (the save warns when the baseline records a load
you omitted); `warmup_mins` is set before loaded compounds (the save warns
when it is missing — nothing is inserted); sets or reps in `description`
without an `exercises` list are an error; `estimated_mins` is within reason
of `duration_mins` (the preview warns past 40 %).
