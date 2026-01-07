# The Enforcer - AI Training Coach

An MCP server that connects to Garmin Connect and provides AI-driven training coaching with enforced training pillars.

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

### 3. Set Up Your Profile

Edit `data/athlete.json` with your info:

```json
{
  "personal": {
    "max_hr": 184,
    "hr_zones": {...}
  },
  "life_constraints": {
    "recurring_commitments": [
      {"day": "Wednesday", "activity": "padel", "time": "afternoon"}
    ]
  },
  "preferences": {
    "likes": ["outdoor rides", "MTB"],
    "dislikes": ["treadmill"]
  }
}
```

Or use the `update_athlete()` tool to set up via the MCP interface.

### 4. Add Your Races

Use the race management tools or edit `data/training_config.json`:

```json
{
  "events": [
    {
      "date": "2026-05-07",
      "name": "My A Race",
      "priority": "A",
      "type": "multi_day_mtb"
    }
  ],
  "current_block": {
    "phase": "base",
    "weekly_volume_target_hrs": 8.0
  }
}
```

### 5. Run the Server

```bash
python server.py
```

Or run the morning audit:

```bash
python daily_loop.py        # Standalone mode
python daily_loop.py --llm  # With LLM coaching
```

## Data Files

| File | Purpose | Edit? |
|------|---------|-------|
| `data/athlete.json` | Your personal info, life constraints, preferences | Yes (or via tool) |
| `data/athlete_baseline.json` | Garmin-derived training capacity | Auto-generated |
| `data/methodology.json` | Training pillars and safety rules | Rarely |
| `data/training_config.json` | Your race calendar and current phase | Yes (or via tools) |
| `data/weekly_plan.json` | Current 7-day rolling plan | Via tools |

## MCP Tools (22 total)

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

## The Enforcer Persona

The AI coach is direct and evidence-based:
- No sugar-coating - tells you what you need to hear
- Always cites the data behind recommendations
- Supportive but firm about pillars
- Every message guides your next action

## License

MIT
