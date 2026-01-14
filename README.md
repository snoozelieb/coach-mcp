# AI Training Coach

An MCP server that connects to Garmin Connect and provides AI-driven training coaching with structured training pillars.

## Features

- **Garmin Integration**: Fetches activities, recovery metrics, and personal records
- **Training Pillars**: Enforces Strength (2x/week), Mobility (90min/week), Long Effort (1x/week)
- **Race Periodization**: Plans training around A/B/C/D priority races
- **Rolling 7-Day Plans**: Maintains and updates weekly training plans
- **Compliance Tracking**: Monitors pillar adherence and safety rules
- **LLM Coaching**: Daily briefs with personalized coaching feedback

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file:

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-xxx  # Optional, for LLM features
```

### 3. Run Setup Wizard

```bash
python setup_wizard.py
```

This creates your personal data files:
- `data/athlete.json` - Your profile, HR zones, preferences
- `data/training_config.json` - Training phase and race calendar
- `data/weekly_plan.json` - Your rolling 7-day plan
- `data/coaching_log.json` - LLM coaching memory (decisions, patterns)

You can also set up manually or via the MCP tools (`update_athlete()`, `add_race()`).

### 4. Run the Server

```bash
python server.py
```

Or run the morning audit:

```bash
python daily_loop.py        # Standalone mode
python daily_loop.py --llm  # With LLM coaching
```

## MCP Client

This server is designed to work with MCP (Model Context Protocol) clients.

**Recommended: Claude Code (CLI)**

```bash
# Add to your Claude Code MCP settings
claude mcp add coach-mcp python server.py
```

This has been tested and works well with [Claude Code](https://claude.ai/code). Other MCP clients (Claude Desktop, custom implementations) may work but have not been tested.

## Data Files

| File | Purpose | Git Status |
|------|---------|------------|
| `data/methodology.json` | Training pillars and safety rules | Shared (committed) |
| `data/exercises.json` | Garmin exercise reference data | Shared (committed) |
| `data/athlete.json` | Your personal info, preferences | Personal (gitignored) |
| `data/athlete_baseline.json` | Garmin-derived training capacity | Personal (gitignored) |
| `data/training_config.json` | Your race calendar and current phase | Personal (gitignored) |
| `data/weekly_plan.json` | Current 7-day rolling plan | Personal (gitignored) |
| `data/coaching_log.json` | LLM coaching memory (decisions, patterns) | Personal (gitignored) |
| `data/exercise_library.json` | Cached exercise form cues for Garmin notes | Personal (gitignored) |

Personal data files are created by `setup_wizard.py` and stay on your machine.

## MCP Tools

### Garmin Data Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_daily_metrics()` | Today's RHR, Body Battery, Sleep | JSON object |
| `get_activities_range(start, end)` | Activity history between dates | JSON array |
| `get_training_readiness(date)` | Recovery score, HRV, acute load | JSON object |
| `get_personal_records()` | All personal bests from Garmin | JSON array |
| `refresh_athlete_baseline()` | Regenerate baseline from 6 months data | JSON summary |
| `get_compliance_report(days)` | Pillar compliance for period | JSON object |

### Athlete Profile Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_athlete()` | Full athlete profile with preferences | JSON object |
| `update_athlete(section, data)` | Update profile section | Confirmation |

**update_athlete sections:**
- `personal` - max_hr, resting_hr, hr_zones, ftp, weight_kg
- `life_constraints` - recurring_commitments, preferred_training_times
- `preferences` - likes, dislikes, equipment
- `coaching_notes` - free-form notes
- `add_commitment` - add a recurring commitment
- `add_injury` - add to injury history

### Methodology Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_methodology()` | View pillars, constraints, race templates | JSON object |
| `update_methodology(section, data)` | Update training methodology | Confirmation |

**update_methodology sections:**
- `pillars` - strength_sessions_per_week, mobility_minutes_per_week, etc.
- `safety_constraints` - max_consecutive_hard_days, rest_after_race, etc.
- `add_race_template` - add new race type template
- `update_race_template` - modify existing race template

### Planning Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_planning_context()` | Full context for LLM planning | JSON object |
| `get_weekly_plan()` | Current 7-day rolling plan | JSON object |
| `update_weekly_plan(plan_json)` | Save new/updated plan | Confirmation |
| `push_plan_to_garmin()` | Push workouts to Garmin calendar | JSON summary |

### Performance Testing Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `set_ftp(ftp_watts, test_avg_watts, test_duration_mins)` | Set cycling FTP from test | Power zones |
| `set_threshold_pace(pace, time_trial_mins, time_trial_distance_km)` | Set running threshold | Pace zones |

### Race Management Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `list_races()` | View all races with days until | JSON array |
| `add_race(name, date, priority, ...)` | Add new A/B/C/D race | Confirmation |
| `update_race(name, ...)` | Update date, priority, notes, URL | Confirmation |
| `remove_race(name)` | Delete race by name | Confirmation |
| `research_race(name or url)` | Fetch race info from website | JSON with course details |

### Suggestion Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `propose_suggestion(type, desc, rationale, change)` | LLM proposes config change | Suggestion ID |
| `list_pending_suggestions()` | View pending suggestions | JSON array |
| `approve_suggestion(id)` | User approves suggestion | Confirmation |
| `reject_suggestion(id, reason)` | User rejects with reason | Confirmation |

### Injury Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `diagnose_injury(location, answers)` | Clinical assessment (two-phase) | Questions or diagnosis |
| `research_injury(injury_type, severity, url)` | Web research for treatment & recovery | JSON with researched info |
| `update_injury_status(date, status, notes)` | Track injury progress | Confirmation |

**diagnose_injury two-phase flow:**
1. Call with just `location` to get clinical assessment questions
2. Call with `location` + `answers` (JSON) to get diagnosis with possible conditions

**research_injury usage:**
- Auto-search: `research_injury("shin splints")` - searches Wikipedia/medical sources
- Direct URL: `research_injury("tendinitis", url="https://...")` - fetches specific resource

### Exercise Research Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `research_exercise(exercise_name, save_to_library)` | Form cues, setup, mistakes, video URL | JSON + cached to library |
| `list_exercises(category, muscle, search)` | Browse exercise database | JSON with matches |

**research_exercise workflow:**
- Primary source: muscleandstrength.com (has video guides)
- Returns: setup instructions, key cues, common mistakes, modifications, **video_url**
- Saves to `data/exercise_library.json` by default
- Form cues + video links appear as notes on Garmin workouts when pushed via `push_plan_to_garmin()`

**Example:**
```python
research_exercise("Romanian deadlift")  # Learn proper form
# → Form cues + video URL cached, will appear on Garmin during strength workouts
# → garmin_note: "Hip hinge, chest up. Video: muscleandstrength.com/exercises/romanian-deadlift"
```

### Strength Sync Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `sync_strength_session(activity_id)` | Pull completed exercise data from Garmin | JSON with synced data |
| `get_strength_baseline(exercise)` | View current strength baselines | JSON with weights, reps |
| `approve_progression(exercise)` | Approve suggested weight increase | Confirmation |
| `set_exercise_preference(group, variation)` | Set preferred exercise variation | Confirmation |

**Strength sync workflow:**
1. Complete gym session with Garmin watch
2. Call `sync_strength_session()` (or auto-syncs via `get_coaching_snapshot()`)
3. Baselines updated with actual weights used
4. If target reps completed, progression suggested (+2.5kg)
5. Call `approve_progression("bench_press")` to accept
6. Next workout uses new weight automatically

## Training Methodology

### Pillars (defined in `methodology.json`)
- **Strength**: 2 sessions per week
- **Mobility**: 90 minutes per week (yoga, pilates, stretching)
- **Long Effort**: 1 session of 60+ minutes cardio

### Safety Rules
- Max 2 consecutive hard days
- Mandatory rest after races
- Max 10% weekly volume increase

### Race Templates
Pre-configured training guidance for:
- `multi_day_mtb` - Stage races like sani2c
- `trail_ultra` - Ultra-distance trail running
- `road_cycling` - Road cycling events
- `tournament` - Multi-game tournaments (Ultimate, Padel)

## Running Tests

```bash
python -m pytest test_server.py test_rules.py test_planner.py -v
```

## License

MIT
