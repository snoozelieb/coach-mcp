---
name: strength-session-author
description: Compose a gym, rehab or prehab session as the `exercises` list the coach server renders onto the Garmin watch — Garmin exercise keys, sets with reps or timed holds, Left and Right steps for per-side work, circuits, explicit loads, an explicit warm-up and the station each movement is done at, ordered by station so the athlete never leaves a bench and comes back — then run preview_workout until the step tree and the station walk match the brief and save with update_weekly_plan. Use whenever a strength, gym, physio-protocol or rehab-circuit session is planned, reviewed or repaired.
---

# Authoring a strength or rehab session

The server renders exactly what the session says and defaults nothing: a
missing `sets`, an unknown key or a muscle-group word where a Garmin category
belongs is an error that names the allowed keys. Your job is the judgment —
which movements, how much, in what order, at which station, with which cue —
written in the contract so the watch shows it as you meant it.

## Before composing

1. `get_coaching_snapshot()` — current date, injuries (hard gate: nothing in a
   `restricted_activities` list, ever), `capacity.strength` (recent session
   lengths), the week grid.
2. `get_athlete()` — `preferences.gym_access` and `preferences.equipment`: a
   commercial gym (shared stations: finish one before you leave it) or a home
   gym (one bench, no contention). The station labels you write come from the
   brief and from here, never from a guess: when you do not know where a
   movement will be done, leave `station` out and say so when you hand the
   session back.
3. `get_strength_baseline()` — the athlete's current loads per movement group.
   Loads are written explicitly as `weight_kg`; the renderer never picks one.
4. `list_exercises(search=...)` for Garmin exercise keys; `research_exercise`
   when a movement is new. The listing shows every category a key is filed
   under, `is_bodyweight` and its `counterpart`: SIDE_PLANK is the bodyweight
   hold, WEIGHTED_SIDE_PLANK its loaded twin (an error without `weight_kg`). A
   name that is not a Garmin key still renders, with the readable name in the
   step description, but then needs a `category`. `key_words` lists the
   equipment words the key spells (CABLE, INCLINE, DUMBBELL): words of the
   key, not Garmin data — DUMBBELL and BAND travel — a hint for the station
   label, never a decision.
5. The physio's or athlete's own words for cues. They go into `notes`
   verbatim (the watch shows about 50 characters; put the safety word first:
   "No jump. Push off left").
6. Coaching memory: `adaptation_patterns` and the last `record_athlete_response`
   for this kind of session, and the snapshot's sleep and readiness signals.
   Size today's session from how the athlete responded last time, not from
   what the plan intended.

## Composition judgment

- Order by station, not by muscle. The athlete walks the gym the way the
  watch lists the steps, and each set is one step, so a wrong order costs two
  lap presses per set to skip. Write the `station` on every movement
  (`"incline bench"`, `"cable"`, `"mat"`; one label per spot, reused
  verbatim) and order the session so the walk visits each station once:
  1. A practitioner's stated order wins (the physio's or biokineticist's
     program sequence). A movement that needs nothing — a dead bug, a band
     pull-apart — is done at the station it sits between, so an alternating
     shoulder-core program keeps its order without a walk: give it that
     station.
  2. The main lift's station comes first, while the athlete is fresh
     (`warmup_mins` before it; ramp-up sets are separate lower-load entries at
     the same station).
  3. The remaining stations follow by contention: rack and platform, bench
     and dumbbells, machines and cables, pull-up bar, bands, mat. Core and mat
     work last — nothing is lost by waiting for a mat.
  4. Inside a station, finish one load before changing it: ramp up on a bar
     (add plates), keep one dumbbell pair together; heaviest first is the
     tie-break only.
  5. At home, contention collapses: one bench may be revisited, bands and the
     mat may come first. Say so in the session notes when a revisit is
     deliberate — the preview still warns.
- Volume: anchor sets and reps on what the athlete has done (baseline,
  `sync_strength_session` history), progress one variable at a time
  (load, then reps, then sets). Never jump a new movement to working weight.
- Rest: `rest_secs` renders a timed rest step; omit it for a lap-button rest
  the athlete controls. Typical: 90–180 s heavy compound, 45–60 s accessory,
  30–45 s rehab. `rest_secs: 0` removes the rest step (flow between
  movements inside a circuit).
- Holds and carries are `duration_secs` per set, never reps: the save rejects
  a rep count on a PLANK, HOLD, HANG, CARRY, L_SIT or WALL_SIT name (reps
  `"open"` is the lap-button hold). Per-side work is `each_side: true`: the
  watch gets a Left step and a Right step per set or round, each with the
  stated reps or time, so `estimated_mins` counts both sides.
- Name what the wrist will show. A catalogue key that names a different
  movement (PISTOL_SQUAT for a bench sit-to-stand, SIDE_LUNGE_JUMP_OFF for a
  lunge without a jump) is wrong on the watch: use the key for the movement
  itself, or an off-catalogue readable name with its `category`. Off-catalogue
  text is the readable name plus the cue, cut at about 50 characters (the
  preview warns), so keep both short.
- Supersets, Tabata rounds and rehab circuits are a circuit:
  `{"rounds": N, "exercises": [...], "rest_secs": S}` — the exercises run back
  to back N times; they carry reps or duration_secs, not sets. A circuit stays
  on one station or on equipment that travels (bodyweight, band, mat): one
  whose rounds hop between a bench and a cable is impractical in a shared gym,
  so write those movements as straight sets at their stations. A circuit-level
  `station` covers every member that states none.
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
| 3 sets of 10 goblet squats at 24 kg, 90 s rest, at the dumbbell rack | `{"name": "GOBLET_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 24, "rest_secs": 90, "station": "dumbbells"}` |
| 3 × 45 s plank on the mat | `{"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 45, "station": "mat"}` |
| 2 × 20 banded eversions per side (not a Garmin key) | `{"name": "BANDED_ANKLE_EVERSION", "category": "BANDED_EXERCISES", "sets": 2, "reps": 20, "each_side": true, "station": "mat"}` |
| Superset push-ups and dead bugs, 3 rounds, on the mat | `{"rounds": 3, "rest_secs": 60, "station": "mat", "exercises": [{"name": "PUSH_UP", "category": "PUSH_UP", "reps": 10}, {"name": "DEAD_BUG", "category": "HIP_STABILITY", "reps": 8, "each_side": true}]}` |
| A 5-minute warm-up block first | session-level `"warmup_mins": 5` |
| The physio's cue | `"notes": "Physio: slow eccentric, stop at pain 3/10"` |
| Where it is done | `"station": "incline bench"` — one label per spot, reused verbatim; a circuit-level `"station"` covers its members |

Category words are Garmin's 35: BANDED_EXERCISES, BATTLE_ROPE, BENCH_PRESS,
CALF_RAISE, CARDIO, CARRY, CHOP, CORE, CRUNCH, CURL, DEADLIFT, FLYE, HIP_RAISE,
HIP_STABILITY, HIP_SWING, HYPEREXTENSION, LATERAL_RAISE, LEG_CURL, LEG_RAISE,
LUNGE, OLYMPIC_LIFT, PLANK, PLYO, PULL_UP, PUSH_UP, ROW, SHOULDER_PRESS,
SHOULDER_STABILITY, SHRUG, SIT_UP, SQUAT, SUSPENSION, TOTAL_BODY,
TRICEPS_EXTENSION, WARM_UP. "Glutes" or "core stability" are rejected with
suggestions. `preview_workout()` with no session returns the full contract.

## Worked example — rehab-led lower body, 40 minutes

Rehab first because the protocol says so; then one walk: rail, bench, BOSU,
floor, mat.

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
     "each_side": true, "station": "rail", "notes": "Slow eccentric, full range, hold the rail lightly"},
    {"name": "DUMBBELL_BULGARIAN_SPLIT_SQUAT", "category": "SQUAT", "sets": 3, "reps": 8,
     "each_side": true, "weight_kg": 10, "rest_secs": 90, "station": "bench", "notes": "6-8 each leg, knee tracks over the toe"},
    {"name": "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL", "category": "DEADLIFT", "sets": 3, "reps": 10,
     "each_side": true, "weight_kg": 8, "rest_secs": 60, "station": "bosu", "notes": "Unstable surface, toe down, slow"},
    {"name": "Side lunge no jump", "category": "LUNGE", "sets": 2, "reps": 8, "each_side": true,
     "station": "floor", "notes": "No jump. Push off the left foot, finish tall"},
    {"rounds": 3, "rest_secs": 60, "name": "Core finisher", "station": "mat", "exercises": [
      {"name": "DEAD_BUG", "category": "HIP_STABILITY", "reps": 10, "notes": "Slow, low back flat"},
      {"name": "COPENHAGEN_PLANK", "category": "PLANK", "duration_secs": 20, "each_side": true}
    ]}
  ]
}
```

## Worked example — upper body in a shared gym, ordered by station

The brief, in muscle order: bench-supported row 12.5 kg, face pull, incline Y
raise 1 kg, band pull-apart, incline dumbbell press 10 kg for 2 sets, dead
bug, side planks, hanging knee raise. Written in that order the athlete
returns to the incline bench twice with three dumbbell pairs. Written by
station the walk is incline bench, cable, mat, pull-up bar — the press first
as the main lift, the band work done at the cable stack, the mat last:

```json
{
  "type": "strength",
  "name": "Upper body posture",
  "description": "Posture: incline bench block, cable, core",
  "purpose": "Posterior-chain strength for the forward hunch; the press stays light",
  "duration_mins": 40,
  "intensity": "easy",
  "warmup_mins": 5,
  "exercises": [
    {"name": "INCLINE_DUMBBELL_BENCH_PRESS", "category": "BENCH_PRESS", "sets": 2, "reps": 10,
     "weight_kg": 10, "rest_secs": 90, "station": "incline bench", "notes": "2 sets only, no heavy overhead"},
    {"name": "SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW", "category": "ROW", "sets": 3, "reps": 10,
     "each_side": true, "weight_kg": 12.5, "rest_secs": 60, "station": "incline bench", "notes": "Chest on the bench, pull to the hip"},
    {"name": "WEIGHTED_INCLINE_Y_RAISE", "category": "SHOULDER_STABILITY", "sets": 3, "reps": 10,
     "weight_kg": 1, "rest_secs": 45, "station": "incline bench", "notes": "Chest on the bench, Y then T, below 90 deg"},
    {"name": "FACE_PULL_WITH_EXTERNAL_ROTATION", "category": "ROW", "sets": 3, "reps": 15,
     "rest_secs": 45, "station": "cable", "notes": "Cable at chest height, pull to the face"},
    {"name": "PULL_APART", "category": "BANDED_EXERCISES", "sets": 3, "reps": 12,
     "rest_secs": 30, "station": "cable", "notes": "Band, at the cable stack, slow"},
    {"name": "DEAD_BUG", "category": "HIP_STABILITY", "sets": 3, "reps": 10, "station": "mat",
     "notes": "Slow, low back flat"},
    {"rounds": 3, "rest_secs": 30, "station": "mat", "exercises": [
      {"name": "SIDE_PLANK", "category": "PLANK", "duration_secs": 30, "each_side": true, "notes": "Straight line, hips up"}
    ]},
    {"name": "HANGING_KNEE_RAISE", "category": "LEG_RAISE", "sets": 3, "reps": 10, "station": "pull-up bar",
     "notes": "No swinging"}
  ]
}
```

## The loop, then the save

1. `preview_workout(session)` — read `steps` as the athlete will see them on the
   wrist. The first line of a gym session is the station walk (`Stations:
   incline bench → cable → mat → pull-up bar`); `station_walk` lists the
   movements and loads per station. Fix every entry in `errors` (the message
   names the allowed keys). Decide on every `warnings` entry: an off-catalogue
   name means the watch shows the category plus your readable name; a cue
   over 50 characters is cut at a word boundary; "returns to" means the walk
   goes back to a station it left — reorder, or state the station the movement
   is really done at; "a circuit whose rounds change spot" means straight sets
   instead; "no station on" means the walk cannot be checked there.
2. Compare the tree with the brief: every movement present, sets and reps as
   intended, holds timed, a Left and a Right step where `each_side` is set,
   loads only where you stated them, cues carrying the safety words, each
   station visited once.
3. `update_weekly_plan(plan=...)` — `authoring_error` means step 1 was
   skipped (or the athlete set `methodology.strength.station_order` to
   `error`, so a revisit rejects the save). Then `push_plan_to_garmin()` and
   show the athlete the `steps` from the preview, station walk first, before
   confirming.

Checklist before saving: every exercise has `sets` and `reps` or
`duration_secs`; every off-catalogue name has a `category`; every cue is in
`notes`, safety word first (without notes the researched library cue goes on
the watch and the preview flags it); every loaded movement states the
`weight_kg` you looked up (the save warns when the baseline records a load
you omitted); every movement states its `station` and the `Stations:` line
visits each spot once (a practitioner's order that forces a revisit is the
one exception — say so in the session notes); `warmup_mins` is set before
loaded compounds (the save warns when it is missing — nothing is inserted);
sets or reps in `description` without an `exercises` list are an error;
`estimated_mins` is within reason of `duration_mins` (the preview warns past
40 %).
