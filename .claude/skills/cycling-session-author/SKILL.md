---
name: cycling-session-author
description: Compose a cycling session the coach server renders onto the Garmin watch — outdoor rides from duration and intensity with HR targets, Wattbike or trainer sessions as a `structure` of warm-up, repeat blocks and cool-down with power_pct, power_watts, cadence or intensity-word targets, and FTP tests — then preview_workout until the step tree matches the brief and save with update_weekly_plan. Use whenever a ride, indoor session or FTP test is planned, reviewed or repaired.
---

# Authoring a cycling session

The server renders exactly what the session says and defaults nothing: a
repeat block renders as a repeat, 30 seconds as 30 seconds, half a minute as
30 seconds; a phase without an end condition or with two numeric targets is an
error that names the allowed keys. Your job is the judgment — the shape of the
ride for this athlete today — written in the contract.

## Before composing

1. `get_coaching_snapshot()` — current date, injuries (hard gate), the ride
   capacity (`capacity.cycling`: longest and typical recent rides), ACWR and
   the week grid.
2. FTP on the profile (`set_ftp`) for `power_pct`; power zones for intensity
   words indoors. Without them an indoor intensity word falls back to HR and
   `power_pct` is an error — write `power_watts` instead.
3. Indoor or outdoor decides the target family: types `wattbike`,
   `indoor_cycling`, `trainer`, `ftp_test` (or `"indoor": true`) resolve
   intensity words to power zones; everything else to HR zones.
4. Coaching memory: `adaptation_patterns` and the last `record_athlete_response`
   for this kind of session, and the snapshot's sleep and readiness signals.
   Size today's session from how the athlete responded last time, not from
   what the plan intended.

## Composition judgment

- An outdoor endurance ride is `duration_mins` + `intensity` (easy for Z2):
  ONE block the whole ride, HR target, no warm-up noise — the athlete warms up
  riding. Use structure outdoors only for deliberate work (tempo climbs, a
  threshold block) and give those phases `hr_target` or an intensity word.
- Indoors every session is `structure`: `warmup`, work as repeat blocks,
  `cooldown`. Each phase has ONE end condition (`duration_mins` with
  fractions, `duration_secs`, `distance_m`, or `"open"` for a self-paced
  lap-button step) and at most one numeric target: `power_pct` [low, high] as
  % of FTP (a single number means ±5 %), `power_watts` [low, high], `cadence`
  [low, high] rpm, `hr_target`; an `intensity` word may label the phase or be
  its target. A phase without a target inherits the session intensity.
- Intensity words render to the athlete's own `power_zones` on the profile;
  those zones win over any textbook band. When you write an explicit
  `power_pct` band, the usual FTP fractions are: recovery 45–55 %, endurance
  56–75 %, tempo 76–87 %, sweet spot 88–94 %, threshold 95–105 %, VO2
  106–120 % — adjust them to what the last comparable session showed the
  athlete can hold. Cadence drills (single-leg, spin-ups) take a `cadence`
  target with the power cue in `notes`; Garmin shows one target per step.
- Micro-intervals (30/30, 40/20) are repeat blocks with `duration_secs`
  phases. Sweet-spot blocks and threshold repeats take their rep count and
  durations from `capacity.cycling` and the last comparable session in
  `sport_history`; progress one lever at a time (reps, then duration, then
  band) and only after the recorded response to the previous one was good.
- An FTP test is `"type": "ftp_test"`: the protocol (warm-up, blowout, 20-minute
  test, cool-down) is built in; afterwards `analyze_ftp_test` and `set_ftp`.
- Session `name` is the Garmin workout name; the renderer appends "(Indoor)" or
  "(Outdoor)" when the description does not already carry it.

## Worked example — Wattbike threshold 4 × 4

```json
{
  "type": "wattbike",
  "name": "Threshold 4x4",
  "description": "4 x 4 min at threshold, 4 min recoveries",
  "purpose": "Raise sustainable power for the climbs; FTP block week 2 of 4",
  "duration_mins": 55,
  "intensity": "threshold",
  "structure": [
    {"phase": "warmup", "duration_mins": 12, "intensity": "easy", "notes": "Build cadence to 90"},
    {"phase": "interval", "duration_secs": 30, "cadence": [100, 110], "notes": "Spin-up, light gear"},
    {"phase": "recovery", "duration_mins": 2.5, "intensity": "recovery"},
    {"phase": "repeat", "iterations": 4, "steps": [
      {"phase": "interval", "duration_mins": 4, "power_pct": [98, 104], "notes": "Steady, seated"},
      {"phase": "recovery", "duration_mins": 4, "power_pct": [50, 58]}
    ]},
    {"phase": "cooldown", "duration_mins": "open", "intensity": "recovery", "notes": "Spin down, lap to end"}
  ]
}
```

## Worked example — outdoor Z2 with two tempo climbs

```json
{
  "type": "cycling",
  "name": "Z2 + 2 tempo climbs",
  "purpose": "Aerobic base with a controlled tempo stimulus on the climb repeats",
  "duration_mins": 120,
  "intensity": "easy",
  "structure": [
    {"phase": "warmup", "duration_mins": 30, "intensity": "easy"},
    {"phase": "repeat", "iterations": 2, "steps": [
      {"phase": "interval", "duration_mins": 12, "hr_target": [149, 163], "notes": "Tempo climb, seated"},
      {"phase": "recovery", "duration_mins": "open", "intensity": "easy", "notes": "Descend, lap at the bottom"}
    ]},
    {"phase": "cooldown", "duration_mins": "open", "intensity": "easy", "notes": "Z2 home"}
  ]
}
```

## The loop, then the save

1. `preview_workout(session)` — read `steps`: every repeat with its count,
   watts and cadence as you meant them, `"open"` only where the athlete
   presses lap. Fix every `errors` entry (missing FTP, two targets, an unknown
   key); weigh every `warnings` entry.
2. `update_weekly_plan(plan=...)`, then `push_plan_to_garmin()` and show the
   athlete the `steps` before confirming.

Checklist: indoor type or flag set when power is wanted; FTP on the profile
before `power_pct`; one end condition and one numeric target per phase;
recoveries as `recovery` phases; `name` set; planned `duration_mins` close to
the preview's `estimated_mins` (open steps count as zero).
