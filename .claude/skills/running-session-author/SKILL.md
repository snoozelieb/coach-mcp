---
name: running-session-author
description: Compose a running session the coach server renders onto the Garmin watch — plain easy and long runs from duration and intensity, or a `structure` of warm-up, repeat blocks and cool-down for intervals, run/walk ladders, tempo blocks, hills and strides, with pace, HR or intensity-word targets — then preview_workout until the step tree matches the brief and save with update_weekly_plan. Use whenever a run or a return-to-run session is planned, reviewed or repaired.
---

# Authoring a running session

The server renders exactly what the session says and defaults nothing: a phase
without an end condition, an intensity word outside the vocabulary or a
mistyped key is an error that names the allowed keys. Your job is the judgment
— the shape of the session for this athlete today — written in the contract.

## Before composing

1. `get_coaching_snapshot()` — current date, injuries (hard gate), the run
   capacity (`capacity.running`: longest and typical recent runs), the week
   grid and plan adherence.
2. `query_metrics(kind='sport_history', sport='running')` when the capacity
   numbers need the session list behind them (VERIFY BEFORE PRESCRIBING).
3. Targets: pace targets need `threshold_pace_sec_per_km` on the profile
   (`set_threshold_pace`). Without it an intensity word resolves to the HR
   zone, which is honest but lags on short reps; for 1-minute efforts prefer a
   cue in `notes` or an explicit `pace_sec_per_km`.
4. Coaching memory: `adaptation_patterns` and the last `record_athlete_response`
   for this kind of session, and the snapshot's sleep and readiness signals.
   Size today's session from how the athlete responded last time, not from
   what the plan intended.

## Composition judgment

- A plain run is `duration_mins` + `intensity` (easy, recovery, tempo,
  threshold, vo2, max_effort; z1–z5 accepted): the renderer draws a warm-up and
  cool-down of 10 % each (up to 5 minutes) around the main block. Use this for
  easy and long runs; anything with reps needs `structure`.
- Structured work is a list of phases and repeat blocks: `warmup`, then
  `{"phase": "repeat", "iterations": N, "steps": [work, recovery]}`, then
  `cooldown`. Each phase has ONE end condition (`duration_secs`,
  `duration_mins`, `distance_m`, or `"open"` for a lap-button step) and at most
  one numeric target (`pace_sec_per_km` [slow, fast], `hr_target` [low, high],
  `cadence`); an `intensity` word can label the phase or be its target. A
  phase with no target inherits the session intensity.
- Return to running is a ladder of run/walk repeats (a level is one shape,
  e.g. R4/W2 × 4), every step cued (`"notes": "Run 4 min, land soft"`),
  recoveries as `recovery` phases so the watch separates them. The one
  progression rule: advance a level only when the current level was completed
  pain-free AND the recorded response to it was good (`record_athlete_response`,
  `adaptation_patterns`) — never on the calendar, and never past demonstrated
  capacity. Which level the athlete is on comes from the physio's clearance
  and the last completed session, not from this skill.
- Interval shapes (tempo blocks with easy floats, VO2 reps with equal
  recoveries, hills by `distance_m` or `duration_secs` with the jog back as a
  `recovery` `"open"` lap-button step when the descent length varies) take
  their rep count and durations from `capacity.running` and the last
  comparable session in `sport_history`. The shape is the coach's; the numbers
  are the athlete's. A number you cannot trace to the history is a template.
- Strides: `{"phase": "repeat", "iterations": 6, "steps": [{"phase":
  "interval", "duration_secs": 20, "intensity": "max_effort", "notes":
  "Stride, tall, relaxed"}, {"phase": "recovery", "duration_secs": 60}]}` after
  an easy run's main block.
- Session `name` is the Garmin workout name (40 characters); keep audit notes
  out of `description`.

## Worked example — Level 2 run/walk ladder

```json
{
  "type": "running",
  "name": "L2 R4/W2 x 4",
  "description": "Return-to-run ladder, level 2",
  "purpose": "Tendon reload at the level the physio cleared; pain-free before level 3",
  "duration_mins": 30,
  "intensity": "easy",
  "structure": [
    {"phase": "warmup", "duration_secs": 180, "intensity": "recovery", "notes": "300m walk + 4 heel + 4 toe steps"},
    {"phase": "repeat", "iterations": 4, "steps": [
      {"phase": "interval", "duration_secs": 240, "intensity": "easy", "notes": "Run 4 min, land soft"},
      {"phase": "recovery", "duration_secs": 120, "intensity": "recovery", "notes": "Walk 2 min"}
    ]},
    {"phase": "cooldown", "duration_secs": 180, "intensity": "recovery", "notes": "200-300m walk"}
  ]
}
```

## Worked example — threshold with a distance finisher

```json
{
  "type": "running",
  "name": "2x10 threshold + 1 km",
  "purpose": "Lactate threshold stimulus two weeks out from the 10 km",
  "duration_mins": 50,
  "intensity": "easy",
  "structure": [
    {"phase": "warmup", "duration_mins": 12, "intensity": "easy"},
    {"phase": "repeat", "iterations": 2, "steps": [
      {"phase": "interval", "duration_mins": 10, "intensity": "threshold", "notes": "Controlled hard"},
      {"phase": "recovery", "duration_mins": 3, "intensity": "recovery"}
    ]},
    {"phase": "interval", "distance_m": 1000, "pace_sec_per_km": [270, 260], "notes": "Finish strong"},
    {"phase": "cooldown", "duration_mins": "open", "intensity": "recovery", "notes": "Jog home, lap to end"}
  ]
}
```

## The loop, then the save

1. `preview_workout(session)` — read `steps`: every rep block present with its
   count, each phase with the end condition and target you meant (pace shows as
   min/km, HR as bpm). Fix every `errors` entry; weigh every `warnings` entry
   (an untargeted interval, a cue over 50 characters, a session intensity
   outside the vocabulary).
2. `update_weekly_plan(plan=...)`, then `push_plan_to_garmin()` and show the
   athlete the `steps` before confirming.

Checklist: one end condition per phase; `"open"` only where the athlete should
press lap; intensity words from the vocabulary (`preview_workout()` lists it);
recoveries as `recovery` phases; `name` set; the ladder level or rep scheme
visible in `notes` on the watch.
