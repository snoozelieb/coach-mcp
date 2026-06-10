"""Race tools - manage races/events and research race details.

Phase 2 consolidation: the old list_races / add_race / update_race /
research_race tools are now actions on the single `races` tool. Their
implementations live on as the private `_list_races` / `_add_race` /
`_update_race` / `_research_race` functions below (bodies moved, not
rewritten). `remove_race` stays a standalone tool — destructive operations
keep their own registration so destructiveHint applies per-tool.
"""

from typing import Literal

from ..mcp_app import mcp
from ..web_utils import fetch_page_text
from ..planner import save_json_file
from ..rules import load_training_config
from ..taxonomy import race_sport_for
from ..config import (VALID_PRIORITIES, ELEVATION_SIGNIFICANCE_THRESHOLD,
                    HIGH_ALTITUDE_THRESHOLD)
from datetime import date, timedelta
import json
import logging
import re
import requests

logger = logging.getLogger(__name__)


def _research_race(name: str = None, url: str = None) -> dict:
    """Research a race/event to gather training-relevant context.

    Fetches information like course profile, elevation, difficulty,
    typical conditions, and training recommendations.
    """
    try:
        # Load config for race lookup and thresholds
        config = load_training_config()

        # Get race analysis thresholds (with config.py fallbacks)
        race_analysis = config.get('race_analysis', {})
        elevation_threshold = race_analysis.get('elevation_significance_m', ELEVATION_SIGNIFICANCE_THRESHOLD)
        altitude_threshold = race_analysis.get('high_altitude_m', HIGH_ALTITUDE_THRESHOLD)

        # Get URL from race config if name provided
        if name and not url:
            events = config.get('events', [])
            name_lower = name.lower()
            for event in events:
                if name_lower in event.get('name', '').lower():
                    url = event.get('url')
                    if not url:
                        return {
                            'error': f"Race '{event['name']}' has no URL. Provide one or add it with races(action='update')."
                        }
                    break
            else:
                return {'error': f"No race found matching '{name}'"}

        if not url:
            return {'error': 'Provide either a race name or URL'}

        # Fetch the page and extract text
        page_text = fetch_page_text(url)

        # Build research summary
        # Look for key patterns in the text
        text_lower = page_text.lower()

        research = {
            'url': url,
            'raw_content_preview': page_text[:500] + '...',
            'detected_info': {},
            'training_relevance': []
        }

        # Detect distance
        distance_match = re.search(r'(\d+)\s*km', text_lower)
        if distance_match:
            research['detected_info']['distance_km'] = int(distance_match.group(1))

        # Detect elevation
        elevation_match = re.search(r'(\d[\d,]*)\s*m.*(?:elevation|climb|ascent)', text_lower)
        if elevation_match:
            elev = elevation_match.group(1).replace(',', '')
            research['detected_info']['elevation_m'] = int(elev)
            if int(elev) > elevation_threshold:
                research['training_relevance'].append('Significant climbing - include hill training')

        # Detect duration hints
        if 'stage' in text_lower or 'multi-day' in text_lower or 'day 1' in text_lower:
            research['detected_info']['multi_day'] = True
            research['training_relevance'].append('Multi-day event - build back-to-back endurance')

        # Detect terrain type
        if 'technical' in text_lower or 'singletrack' in text_lower:
            research['detected_info']['technical_terrain'] = True
            research['training_relevance'].append('Technical terrain - practice bike handling skills')

        if 'gravel' in text_lower:
            research['detected_info']['surface'] = 'gravel'
        elif 'road' in text_lower and 'off-road' not in text_lower:
            research['detected_info']['surface'] = 'road'
        elif 'trail' in text_lower or 'mountain' in text_lower:
            research['detected_info']['surface'] = 'trail/mtb'

        # Detect altitude
        altitude_match = re.search(r'(\d[\d,]*)\s*m.*(?:altitude|above sea level)', text_lower)
        if altitude_match:
            alt = int(altitude_match.group(1).replace(',', ''))
            if alt > altitude_threshold:
                research['detected_info']['high_altitude'] = True
                research['training_relevance'].append(f'High altitude ({alt}m) - consider acclimatization')

        # Add general note
        research['note'] = 'Review raw_content_preview for additional context. Use this info to adjust training focus.'

        return research

    except requests.RequestException as e:
        logger.exception("races(action='research') failed")
        return {'error': f'Failed to fetch URL: {str(e)}'}
    except Exception as e:
        logger.exception("races(action='research') failed")
        return {'error': str(e)}


def _list_races(today: date) -> dict:
    """List all configured races/events with priority and days until.

    today is required — the races boundary resolves it (clock discipline).
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        result = []
        for event in events:
            event_copy = event.copy()
            try:
                event_date = date.fromisoformat(event.get('date', ''))
                event_copy['days_until'] = (event_date - today).days
            except ValueError:
                event_copy['days_until'] = None
            result.append(event_copy)

        # Sort by date
        result.sort(key=lambda e: e.get('date', ''))

        return {'races': result, 'count': len(result)}

    except Exception as e:
        logger.exception("races(action='list') failed")
        return {'error': str(e)}


def _add_race(
    name: str,
    race_date: str,
    priority: str,
    race_type: str = None,
    distance_km: float = None,
    duration_days: int = 1,
    target_time: str = None,
    url: str = None,
    notes: str = None
) -> dict:
    """Add a new race/event to the training calendar."""
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Validate priority
        if priority.upper() not in VALID_PRIORITIES:
            return {'error': 'Priority must be A, B, or C'}

        # Check for duplicate priority (only 1 race per priority allowed)
        existing_with_priority = [e for e in events if e.get('priority') == priority.upper()]
        if existing_with_priority:
            existing_name = existing_with_priority[0].get('name', 'Unknown')
            return {
                'error': f"Priority {priority.upper()} already assigned to '{existing_name}'. "
                         f"Only one race per priority allowed. Update or remove the existing race first."
            }

        # Build new event
        new_event = {
            'date': race_date,
            'name': name,
            'priority': priority.upper(),
        }

        if race_type:
            new_event['type'] = race_type
        if distance_km:
            new_event['distance_km'] = distance_km
        if duration_days > 1:
            new_event['duration_days'] = duration_days
            # Calculate end date
            start = date.fromisoformat(race_date)
            end = start + timedelta(days=duration_days - 1)
            new_event['end_date'] = end.isoformat()
        if target_time:
            new_event['target_time'] = target_time
        if url:
            new_event['url'] = url
        if notes:
            new_event['notes'] = notes

        events.append(new_event)

        # Sort by date
        events.sort(key=lambda e: e.get('date', ''))

        # Save back
        config['events'] = events
        save_json_file('training_config.json', config)

        response = {
            'status': 'success',
            'message': f"Added {priority.upper()}-race: {name} on {race_date}",
            'event': new_event,
        }
        # Surface the taxonomy-derived sport so unrecognized race types are
        # visible immediately (unknown types fall back to overall CTL in
        # sport-specific fitness lookups).
        if race_type:
            sport = race_sport_for(race_type)
            response['sport'] = sport
            if sport is None:
                response['warning'] = (
                    f"Race type '{race_type}' is not a recognized race type — "
                    "sport-specific fitness tracking will fall back to overall CTL. "
                    "Known types include: multi_day_mtb, road_cycling, mtb, trail_ultra, "
                    "marathon, half_marathon, 10k, 5k, triathlon, swimming, tournament."
                )
        return response

    except Exception as e:
        logger.exception("races(action='add') failed")
        return {'error': str(e)}


def _update_race(
    name: str,
    new_date: str = None,
    new_priority: str = None,
    new_name: str = None,
    target_time: str = None,
    distance_km: float = None,
    notes: str = None,
    url: str = None
) -> dict:
    """Update any field of an existing race/event."""
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Find matching event
        name_lower = name.lower()
        for event in events:
            if name_lower in event.get('name', '').lower():
                changes = []

                if new_date:
                    event['date'] = new_date
                    changes.append(f"date -> {new_date}")
                    # Update end_date if multi-day
                    if event.get('duration_days', 1) > 1:
                        start = date.fromisoformat(new_date)
                        end = start + timedelta(days=event['duration_days'] - 1)
                        event['end_date'] = end.isoformat()

                if new_priority:
                    if new_priority.upper() not in VALID_PRIORITIES:
                        return {'error': 'Priority must be A, B, or C'}
                    # Check for duplicate priority (only 1 race per priority allowed)
                    # Exclude current event from check
                    other_events = [e for e in events if e.get('name') != event.get('name')]
                    existing_with_priority = [e for e in other_events if e.get('priority') == new_priority.upper()]
                    if existing_with_priority:
                        existing_name = existing_with_priority[0].get('name', 'Unknown')
                        return {
                            'error': f"Priority {new_priority.upper()} already assigned to '{existing_name}'. "
                                     f"Only one race per priority allowed."
                        }
                    event['priority'] = new_priority.upper()
                    changes.append(f"priority -> {new_priority.upper()}")

                if new_name:
                    event['name'] = new_name
                    changes.append(f"name -> {new_name}")

                if target_time:
                    event['target_time'] = target_time
                    changes.append(f"target_time -> {target_time}")

                if distance_km:
                    event['distance_km'] = distance_km
                    changes.append(f"distance -> {distance_km}km")

                if notes:
                    event['notes'] = notes
                    changes.append("notes updated")

                if url:
                    event['url'] = url
                    changes.append("url updated")

                if not changes:
                    return {'error': 'No updates provided'}

                # Re-sort by date
                events.sort(key=lambda e: e.get('date', ''))

                # Save back
                config['events'] = events
                save_json_file('training_config.json', config)

                return {
                    'status': 'success',
                    'message': f"Updated {event['name']}: {', '.join(changes)}",
                    'event': event
                }

        return {'error': f"No event found matching '{name}'"}

    except Exception as e:
        logger.exception("races(action='update') failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': False, 'openWorldHint': True})
def races(
    action: Literal['list', 'add', 'update', 'research'],
    name: str = None,
    race_date: str = None,
    priority: str = None,
    race_type: str = None,
    distance_km: float = None,
    duration_days: int = 1,
    target_time: str = None,
    url: str = None,
    notes: str = None,
    new_date: str = None,
    new_priority: str = None,
    new_name: str = None,
) -> dict:
    """
    Manage races/events on the training calendar (replaces the old
    list_races / add_race / update_race / research_race tools).

    When to use each action:
    - 'list': see all configured races with priority (A/B/C), type, and
      days_until. Use before planning a block or answering "what's next?".
    - 'add': register a new race. Requires name, race_date (YYYY-MM-DD),
      and priority (A = goal race, B = important, C = training race; only
      one race per priority). Optional: race_type, distance_km,
      duration_days (>1 for stage races), target_time, url, notes.
      NOTE: 'add' appends an event — calling it twice adds duplicates.
    - 'update': change fields on an existing race. Requires name
      (case-insensitive partial match). Optional: new_date, new_priority,
      new_name, target_time, distance_km, notes, url.
    - 'research': fetch the race's web page (open web) and extract
      training-relevant context — distance, elevation, terrain, altitude.
      Provide name (uses the configured race's URL) or a direct url.

    Deleting a race stays on the standalone remove_race tool (destructive).

    Returns a dict per action:
    - list: {'races': [...], 'count': N} sorted by date with days_until
    - add/update: {'status', 'message', 'event'} (+ 'sport'/'warning' on add)
    - research: {'url', 'detected_info', 'training_relevance', ...}
    """
    try:
        if action == 'list':
            return _list_races(date.today())  # tool boundary resolution

        if action == 'add':
            missing = [arg for arg, val in (('name', name),
                                            ('race_date', race_date),
                                            ('priority', priority)) if not val]
            if missing:
                return {'error': f"action='add' requires: {', '.join(missing)}"}
            return _add_race(name, race_date, priority, race_type, distance_km,
                             duration_days, target_time, url, notes)

        if action == 'update':
            if not name:
                return {'error': "action='update' requires: name"}
            return _update_race(name, new_date, new_priority, new_name,
                                target_time, distance_km, notes, url)

        if action == 'research':
            return _research_race(name, url)

        return {'error': f"Unknown action '{action}'. "
                         "Valid actions: list, add, update, research"}

    except Exception as e:
        logger.exception("races(action=%s) failed", action)
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True,
                       'idempotentHint': True, 'openWorldHint': False})
def remove_race(name: str) -> str:
    """
    Remove a race/event from the training calendar.

    Destructive — the event is deleted from training_config.json. Kept as a
    standalone tool (not a `races` action) so clients see destructiveHint.

    Args:
        name: Name of the event to remove (case-insensitive partial match)

    Returns confirmation or error if not found.
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Find matching event
        name_lower = name.lower()
        matching = [e for e in events if name_lower in e.get('name', '').lower()]

        if not matching:
            return json.dumps({'error': f"No event found matching '{name}'"})

        if len(matching) > 1:
            names = [e.get('name') for e in matching]
            return json.dumps({
                'error': f"Multiple matches found: {names}. Be more specific."
            })

        removed = matching[0]
        events.remove(removed)

        # Save back
        config['events'] = events
        save_json_file('training_config.json', config)

        return json.dumps({
            'status': 'success',
            'message': f"Removed: {removed.get('name')}",
            'removed_event': removed
        }, indent=2)

    except Exception as e:
        logger.exception("remove_race failed")
        return json.dumps({'error': str(e)})
