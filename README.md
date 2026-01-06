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

### 4. Add Your Races

Edit `data/training_config.json`:

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
| `data/athlete.json` | Your personal info, life constraints, preferences | Yes |
| `data/athlete_baseline.json` | Garmin-derived training capacity | Auto-generated |
| `data/methodology.json` | Training pillars and safety rules | Rarely |
| `data/training_config.json` | Your race calendar and current phase | Yes |
| `data/weekly_plan.json` | Current 7-day rolling plan | Via tools |

## MCP Tools

### Data Tools
- `get_athlete()` - Your full profile
- `get_daily_metrics()` - Today's RHR, Body Battery, Sleep
- `get_activities_range(start, end)` - Activity history
- `get_training_readiness(date)` - Recovery score and HRV
- `refresh_athlete_baseline()` - Regenerate from 6 months of Garmin data

### Planning Tools
- `get_planning_context()` - Full context for LLM planning
- `get_weekly_plan()` - Current 7-day plan
- `update_weekly_plan(plan_json)` - Save updated plan
- `get_compliance_report(days)` - Pillar compliance status

### Race Management
- `list_races()` - View all races with days until
- `add_race(...)` - Add new race
- `update_race(...)` - Modify race details
- `remove_race(name)` - Delete race
- `research_race(name)` - Fetch race info from URL

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
