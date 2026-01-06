"""
Shared configuration and constants for coach-mcp.

Centralizes paths, thresholds, and magic numbers to avoid duplication
and make the codebase easier to maintain.
"""
from pathlib import Path

# Paths
DATA_DIR = Path(__file__).parent / "data"
TOKEN_DIR = str(Path(__file__).parent / ".garth")  # String for garth compatibility

# Data file names
ATHLETE_FILE = "athlete.json"
ATHLETE_BASELINE_FILE = "athlete_baseline.json"
TRAINING_CONFIG_FILE = "training_config.json"
METHODOLOGY_FILE = "methodology.json"
WEEKLY_PLAN_FILE = "weekly_plan.json"
SUGGESTIONS_FILE = "suggestions.json"

# Activity classification thresholds
LONG_EFFORT_MIN_MINS = 60
HARD_HR_AVG_THRESHOLD = 150
HARD_HR_MAX_THRESHOLD = 175

# Compliance thresholds
VOLUME_COMPLIANCE_MIN_PERCENT = 80

# API timeouts (seconds)
HTTP_TIMEOUT_SECONDS = 15

# Data retention (days)
PROFILE_HISTORY_DAYS = 180
RECENT_ACTIVITY_DAYS = 35  # 5 weeks to view current week within 4-week block context

# Web scraping limits
PAGE_TEXT_MAX_CHARS = 8000
ELEVATION_SIGNIFICANCE_THRESHOLD = 1000
HIGH_ALTITUDE_THRESHOLD = 1500

# Valid race priorities
VALID_PRIORITIES = ['A', 'B', 'C', 'D']
