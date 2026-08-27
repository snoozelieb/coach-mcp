---
name: capture-fixtures
description: Refresh the real Garmin API test fixtures (test_fixtures.json) — an OWNER-RUN-ONLY step agents and CI must never execute. Use when Garmin response shapes changed, fixture-driven tests look stale, or the user asks to recapture fixtures.
---

# Refreshing Garmin test fixtures

## The rule that matters most

`python scripts/capture_fixtures.py` hits the live Garmin API with the owner's
session and writes personal health data. **Agents and CI must NEVER run it.**
Ask the user to run it themselves — suggest the `!` prefix so the output lands
in the session:

```
! python scripts/capture_fixtures.py
```

## How the two-layer fixture system works

- `tests/fixtures/garmin_sample.json` — **committed, fully synthetic** values
  with real response *shapes*. Clean checkouts run every fixture-driven test
  from this alone.
- `test_fixtures.json` (repo root) — the real capture, **gitignored** (even
  redacted it contains sleep/HRV/weight/load data). The `garmin_fixtures`
  fixture in `tests/conftest.py` loads the sample first and overlays the real
  capture where present, so real-shape regressions surface on the owner's
  machine while clean checkouts stay green.

The capture script redacts obvious PII before writing (names/emails →
"REDACTED", profile/device ids → 0, GPS coordinates removed).

## When to refresh

- A garminconnect upgrade or Garmin API change alters response shapes
  (`tests/test_garmin_contract.py` failing is the usual tell).
- A new endpoint was added to the codebase — also add it to the capture list
  in `scripts/capture_fixtures.py` and a synthetic shape to
  `garmin_sample.json` + FakeGarminClient.

## Prerequisites & verification

- Valid Garmin session (`.garth/` tokens or credentials in `.env`); if auth
  fails: `python scripts/garmin_login.py` (also owner-run — interactive MFA).
- Afterwards: confirm `git status` does NOT list `test_fixtures.json` (it must
  stay gitignored), then `python -m pytest -q` — real shapes now overlay the
  synthetic ones.
