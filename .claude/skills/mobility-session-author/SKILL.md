---
name: mobility-session-author
description: Compose a yoga, stretching, mobility or physio rehab session the coach server renders onto the Garmin watch — a single timed block with the flow in notes for yoga and stretching, or a reps-and-holds `exercises` list tracked as Pilates for rehab routines — then preview_workout and save with update_weekly_plan. Use whenever a mobility, stretching, yoga or rehab-only session is planned, reviewed or repaired.
---

# Authoring a mobility or rehab session

Garmin has no pose-by-pose yoga workouts, so a mobility session is one timed
block that carries the flow as its description. A physio routine the athlete
tracks as Pilates can be itemised: each movement a repeat group of sets with
reps, a timed hold or a lap-button step. `preview_workout(session)` shows what
the watch gets; `preview_workout()` returns the contract.

## Judgment

- Type decides the Garmin sport: `yoga`, `mobility`, `stretching`, `foam_rolling`
  render as a yoga block; `rehab` and `pilates` render as Pilates and accept
  `exercises`.
- A mobility block is `duration_mins` plus `notes` naming the flow (the first
  50 characters show on the step; the full text is the workout description).
  Put the purpose-critical hold first: "Couch stretch 2 min/side, 90/90, pigeon".
- Rehab as Pilates: each `exercises` entry is `name` plus `sets` and `reps`
  (reps-based), or `duration_secs` (hold), or neither (lap button — the
  athlete decides), with the cue in `notes`. Names are free text here; the
  readable name goes on the step.
- Rehab that belongs to a gym session (banded eversions after the squats)
  goes into that strength session's `exercises` instead — see
  strength-session-author — so sets and reps reach the watch with Garmin keys.
- Keep mobility inside the injury gate like everything else: a restricted
  movement pattern stays out, and the cue says what to avoid.

## Worked example — evening mobility

```json
{
  "type": "yoga",
  "name": "Hip mobility 30",
  "purpose": "Hip flexor length for the bike position; sleep-friendly downshift",
  "duration_mins": 30,
  "intensity": "recovery",
  "notes": "Couch stretch 2 min/side, 90/90 x 10, pigeon 2 min/side, box breathing 5 min"
}
```

## Worked example — ankle rehab tracked as Pilates

```json
{
  "type": "rehab",
  "name": "Ankle rehab",
  "purpose": "Peroneal strength and single-leg balance, physio set 3 of the week",
  "duration_mins": 15,
  "intensity": "easy",
  "exercises": [
    {"name": "Banded ankle eversion", "sets": 2, "reps": 20, "notes": "Slow, each side"},
    {"name": "Single leg balance on BOSU", "sets": 3, "duration_secs": 30, "notes": "Toe down, NOT on the rim"},
    {"name": "Heel raises off step", "sets": 2, "reps": 15}
  ]
}
```

Checklist: the flow or protocol is in `notes` where the watch shows it; holds
are `duration_secs`; the session has a `purpose`; `preview_workout` shows the
step tree you expect before `update_weekly_plan`.
