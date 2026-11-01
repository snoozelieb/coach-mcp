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
3. Targets are alerts: the watch sounds whenever the athlete is outside the
   target, in both directions. `easy` and `recovery` render as ceilings — the
   alert fires only above the zone top; the low bound is the athlete's aerobic
   floor (resting HR + 30 % of heart-rate reserve, so a brisk walk stays inside
   it and standing still drops out). `tempo`, `threshold`, `vo2`, `max_effort`
   and `hr_target` are bands the athlete must hit; `hr_max` is an explicit
   ceiling. HR lags 1–2 minutes: give an HR band only to blocks of 5 minutes or
   more. Shorter reps take `pace_sec_per_km` (needs `threshold_pace_sec_per_km`
   on the profile, `set_threshold_pace`), `hr_max`, or a cue in `notes` with
   `"intensity": "none"`. The preview warns about an HR band on a step under
   3 minutes.
4. Coaching memory: `adaptation_patterns` and the last `record_athlete_response`
   for this kind of session, and the snapshot's sleep and readiness signals.
   Size today's session from how the athlete responded last time, not from
   what the plan intended.
5. Weather: rain and wind do not move a run. Daylight does — check the
   training window in `snapshot.weather_outlook` and say so when the athlete
   hesitates.

## Composition judgment

- A plain run is `duration_mins` + `intensity` (easy, recovery, tempo,
  threshold, vo2, max_effort; z1–z5 accepted): the renderer draws an untargeted
  warm-up and cool-down of 10 % each (up to 5 minutes) around the main block,
  which carries the intensity. Use this for easy and long runs; anything with
  reps needs `structure`.
- Structured work is a list of phases and repeat blocks: `warmup`, then
  `{"phase": "repeat", "iterations": N, "steps": [work, recovery]}`, then
  `cooldown`. Each phase has ONE end condition (`duration_secs`,
  `duration_mins`, `distance_m`, or `"open"` for a lap-button step) and at most
  one numeric target (`pace_sec_per_km` [slow, fast], `hr_target` [low, high],
  `cadence`, `hr_max`); an `intensity` word can label the phase or be its
  target. An `interval` phase with no target inherits the session intensity;
  warm-ups, cool-downs, recoveries and rests render untargeted unless they
  state one.
- Return to running is a ladder of run/walk repeats (a level is one shape,
  e.g. R4/W2 × 4), every step cued (`"notes": "Run 4 min, land soft"`),
  recoveries as `recovery` phases so the watch separates them. Run reps take
  `easy` (a ceiling: the alert says "slow down", never "speed up"). A walk
  step with no target is silent; `"intensity": "recovery"` on it gives a
  keep-walking band from the aerobic floor to the top of z1 — the alert fires
  when the athlete stops, not while they walk. A warm-up with drills stays
  untargeted. The one
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
  "interval", "duration_secs": 20, "intensity": "none", "notes":
  "Stride, tall, relaxed"}, {"phase": "recovery", "duration_secs": 60}]}` after
  an easy run's main block. A 20-second effort has no steady state: an HR band
  there only beeps, so the cue is the target.
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
    {"phase": "warmup", "duration_secs": 180, "notes": "300m walk + 4 heel + 4 toe steps"},
    {"phase": "repeat", "iterations": 4, "steps": [
      {"phase": "interval", "duration_secs": 240, "intensity": "easy", "notes": "Run 4 min, land soft"},
      {"phase": "recovery", "duration_secs": 120, "intensity": "recovery", "notes": "Walk 2 min"}
    ]},
    {"phase": "cooldown", "duration_secs": 180, "intensity": "recovery", "notes": "200-300m walk"}
  ]
}
```

On the watch: the warm-up is silent, each run alerts only above the top of
z2, each walk and the cool-down alert only when the athlete stops (below the
aerobic floor) or breaks into a run (above the top of z1).

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
   min/km, HR as bpm; a ceiling shows as aerobic floor to zone top). Fix every
   `errors` entry; weigh every `warnings` entry (an untargeted interval, an HR
   band on a step under 3 minutes, a cue over 50 characters, a session
   intensity outside the vocabulary).
2. `update_weekly_plan(plan=...)`, then `push_plan_to_garmin()` and show the
   athlete the `steps` before confirming.

Checklist: one end condition per phase; `"open"` only where the athlete should
press lap; intensity words from the vocabulary (`preview_workout()` lists it);
recoveries as `recovery` phases; no HR band on a step under 3 minutes; `name` set; the ladder level or rep scheme
visible in `notes` on the watch.
