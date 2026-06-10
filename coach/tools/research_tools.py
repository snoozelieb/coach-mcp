"""Research tools - exercise library lookup, exercise research, sport research."""

from ..mcp_app import mcp
from ..web_utils import fetch_page_text
from ..planner import load_json_file, save_json_file
from ..config import DATA_DIR, EXERCISE_LIBRARY_FILE, PAGE_TEXT_MAX_CHARS
from datetime import date
import json
import logging
import re

logger = logging.getLogger(__name__)


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': True})
def research_exercise(exercise_name: str, save_to_library: bool = True) -> str:
    """
    Research proper form, cues, and common mistakes for an exercise.

    Fetches information from fitness resources and optionally saves to the
    exercise library for use in Garmin workout notes.

    Args:
        exercise_name: Name of the exercise (e.g., "Romanian deadlift", "Bulgarian split squat")
        save_to_library: Whether to cache the form cues for future workouts (default True)

    Returns:
        JSON with form cues, setup instructions, common mistakes, and modifications.

    Usage:
        research_exercise("Romanian deadlift")
        research_exercise("hip thrust", save_to_library=True)
    """


    try:
        # Normalize exercise name
        exercise_normalized = exercise_name.strip().lower()
        exercise_url_name = exercise_name.replace(' ', '_').replace('-', '_')

        result = {
            "exercise": exercise_name,
            "form_cues": {},
            "sources": [],
            "cached": False,
        }

        # Check if already in library
        library_path = DATA_DIR / EXERCISE_LIBRARY_FILE
        library = {}
        if library_path.exists():
            with open(library_path) as f:
                library = json.load(f)

            if exercise_normalized in library:
                cached = library[exercise_normalized]
                cached["cached"] = True
                cached["note"] = "Retrieved from exercise library. Use research_exercise with a new name to research different exercises."
                return json.dumps(cached, indent=2)

        # Primary source: muscleandstrength.com has excellent form guides with videos
        # URL format: muscleandstrength.com/exercises/exercise-name.html
        exercise_url_slug = exercise_name.lower().replace(' ', '-').replace('_', '-')

        # Search sources for exercise info - prioritize muscleandstrength for video content
        search_sources = [
            {
                "name": "Muscle & Strength",
                "url": f"https://www.muscleandstrength.com/exercises/{exercise_url_slug}.html",
                "type": "form",
                "has_video": True
            },
            {
                "name": "ExRx",
                "url": f"https://exrx.net/WeightExercises/search?q={exercise_url_name}",
                "type": "form",
                "has_video": False
            },
        ]

        # Track video URL for successful source
        video_url = None

        # Try to fetch content
        fetched_content = None
        for source in search_sources:
            try:
                content = fetch_page_text(source["url"])

                # Check for exercise-related content
                content_lower = content.lower()
                exercise_indicators = ['muscle', 'exercise', 'movement', 'form', 'position', 'repetition', 'set']
                has_exercise_content = any(ind in content_lower for ind in exercise_indicators)

                if len(content) > 300 and has_exercise_content:
                    fetched_content = content
                    result["sources"].append(source["url"])
                    # Capture video URL if source has videos
                    if source.get("has_video"):
                        video_url = source["url"]
                    break
            except Exception:
                continue

        # Extract relevant information
        if fetched_content:
            sentences = re.split(r'[.!?]+', fetched_content)

            # Keywords for different aspects
            setup_keywords = ['position', 'stance', 'grip', 'feet', 'hands', 'setup', 'starting']
            execution_keywords = ['lower', 'raise', 'push', 'pull', 'extend', 'flex', 'drive', 'squeeze', 'contract']
            cue_keywords = ['keep', 'maintain', 'avoid', 'ensure', 'focus', 'engage', 'brace']
            mistake_keywords = ['avoid', 'don\'t', 'never', 'mistake', 'wrong', 'error', 'common']
            muscle_keywords = ['muscle', 'target', 'work', 'engage', 'activate']

            setup_findings = []
            execution_findings = []
            cue_findings = []
            mistake_findings = []
            muscle_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 15 and len(sentence_clean) < 200:
                    if any(kw in sentence_lower for kw in setup_keywords):
                        setup_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in execution_keywords):
                        execution_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in cue_keywords):
                        cue_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in mistake_keywords):
                        mistake_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in muscle_keywords):
                        muscle_findings.append(sentence_clean)

            result["form_cues"] = {
                "setup": setup_findings[:3] if setup_findings else ["Set up with proper stance and alignment"],
                "execution": execution_findings[:4] if execution_findings else ["Control the movement through full range of motion"],
                "key_cues": cue_findings[:4] if cue_findings else ["Focus on mind-muscle connection"],
                "common_mistakes": mistake_findings[:3] if mistake_findings else ["Avoid using momentum or excessive weight"],
                "muscles_worked": muscle_findings[:2] if muscle_findings else [],
            }

            # Generate a concise note for Garmin workouts
            garmin_note_parts = []
            if setup_findings:
                garmin_note_parts.append(setup_findings[0][:80])
            if cue_findings:
                garmin_note_parts.append(cue_findings[0][:80])

            # Build garmin note with video link
            note_text = ". ".join(garmin_note_parts) if garmin_note_parts else f"Perform {exercise_name} with controlled form"

            # Add video URL to note (shortened domain for Garmin display)
            if video_url:
                result["video_url"] = video_url
                # Shorten URL for Garmin note display
                short_url = video_url.replace("https://www.", "").replace("https://", "")
                result["garmin_note"] = f"{note_text[:180]}. Video: {short_url}"[:250]
            else:
                result["garmin_note"] = note_text[:250]

        else:
            # Couldn't fetch - provide fallback video URL and guidance
            fallback_video = f"https://www.muscleandstrength.com/exercises/{exercise_url_slug}.html"
            result["form_cues"] = {
                "note": f"Unable to fetch form guide for '{exercise_name}'.",
                "suggested_searches": [
                    f"{exercise_name} form guide",
                    f"{exercise_name} how to",
                    f"{exercise_name} technique",
                ],
            }
            result["video_url"] = fallback_video
            result["garmin_note"] = f"Perform {exercise_name} with controlled form. Video: muscleandstrength.com/exercises/{exercise_url_slug}"[:250]

        # Add modifications guidance
        result["modifications"] = {
            "easier": "Reduce weight, decrease range of motion, or use assisted variation",
            "harder": "Add weight, slow tempo, add pause at bottom, or use unilateral variation",
            "equipment_alternatives": "Check gym equipment available or ask for substitution"
        }

        # Save to library if requested
        if save_to_library and result.get("form_cues"):
            library[exercise_normalized] = {
                "exercise": exercise_name,
                "form_cues": result["form_cues"],
                "garmin_note": result.get("garmin_note", ""),
                "video_url": result.get("video_url", ""),
                "modifications": result["modifications"],
                "sources": result["sources"],
                "researched_date": date.today().isoformat(),
            }

            # Ensure data directory exists
            DATA_DIR.mkdir(exist_ok=True)
            with open(library_path, 'w') as f:
                json.dump(library, f, indent=2)

            result["cached"] = True
            result["cache_note"] = "Saved to exercise library. Will be included in Garmin workout notes."

        return json.dumps(result, indent=2)

    except Exception as e:
        logger.exception("research_exercise failed")
        return json.dumps({"error": str(e)})


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def list_exercises(
    category: str = None,
    muscle: str = None,
    injury_prevention: str = None,
    search: str = None,
    limit: int = 50
) -> str:
    """
    Browse the exercise library with optional filters.

    Args:
        category: Filter by Garmin category (e.g., "DEADLIFT", "SQUAT", "PLANK")
        muscle: Filter by muscle group (e.g., "hamstrings", "glutes", "core")
        injury_prevention: Filter exercises for injury prevention (e.g., "hamstring", "knee", "ankle")
        search: Text search in exercise names
        limit: Max results to return (default 50)

    Returns:
        JSON with matching exercises and available categories/muscles.
    """
    try:
        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run scripts/fetch_exercises.py first.",
                "hint": "python scripts/fetch_exercises.py"
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})
        categories = library.get("categories", [])
        injury_mappings = library.get("injury_mappings", {})

        # Build result
        result = {
            "filters_applied": {},
            "matches": [],
            "total_in_library": len(exercises),
        }

        # Apply filters
        matches = []
        for name, data in exercises.items():
            include = True

            # Category filter
            if category:
                if category.upper() != data.get("garmin_category", "").upper():
                    include = False

            # Muscle filter
            if muscle and include:
                muscles = data.get("muscles", [])
                if not any(muscle.lower() in m.lower() for m in muscles):
                    include = False

            # Injury prevention filter
            if injury_prevention and include:
                prevention = data.get("injury_prevention", [])
                if not any(injury_prevention.lower() in p.lower() for p in prevention):
                    include = False

            # Text search
            if search and include:
                if search.lower() not in name.lower():
                    include = False

            if include:
                matches.append({
                    "name": name,
                    "category": data.get("garmin_category"),
                    "primary_muscles": data.get("primary_muscles", []),
                    "secondary_muscles": data.get("secondary_muscles", []),
                    "injury_prevention": data.get("injury_prevention", []),
                })

        # Apply limit
        result["matches"] = matches[:limit]
        result["match_count"] = len(matches)

        # Record applied filters
        if category:
            result["filters_applied"]["category"] = category
        if muscle:
            result["filters_applied"]["muscle"] = muscle
        if injury_prevention:
            result["filters_applied"]["injury_prevention"] = injury_prevention
        if search:
            result["filters_applied"]["search"] = search

        # Include available options if no filters
        if not any([category, muscle, injury_prevention, search]):
            result["available_categories"] = categories[:20]
            result["categories_note"] = f"{len(categories)} total categories"
            result["injury_prevention_types"] = list(injury_mappings.keys())

        return json.dumps(result, indent=2)

    except Exception as e:
        logger.exception("list_exercises failed")
        return json.dumps({"error": str(e)})


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
def research_sport(sport_name: str, url: str = None) -> str:
    """
    Research training principles and methodology for a specific sport.

    Use this when onboarding an athlete in an unfamiliar sport. Fetches
    information about training approaches, periodization, common injuries,
    and key performance metrics.

    Args:
        sport_name: Name of the sport (e.g., "rock climbing", "CrossFit", "rowing")
        url: Optional direct URL to a training resource for this sport

    Returns:
        JSON with training principles, typical periodization, common injuries,
        and key metrics for this sport.

    Usage:
        research_sport("rock climbing")
        research_sport("CrossFit", url="https://example.com/crossfit-training")
    """
    try:
        research_result = {
            "sport": sport_name,
            "researched_info": {},
            "sources": [],
            "training_implications": [],
        }

        # Format sport name for Wikipedia URL
        sport_url_name = sport_name.replace(' ', '_').title()

        # Build list of URLs to try
        if url:
            search_sources = [{"name": "Provided URL", "url": url, "type": "direct"}]
        else:
            search_sources = []

        # Add Wikipedia as primary source
        search_sources.extend([
            {
                "name": "Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{sport_url_name}",
                "type": "general"
            },
            # Try with "_training" suffix for training-specific articles
            {
                "name": "Wikipedia Training",
                "url": f"https://en.wikipedia.org/wiki/{sport_url_name}_training",
                "type": "training"
            },
        ])

        # Try to fetch from sources
        fetched_content = None
        for source in search_sources:
            try:
                content = fetch_page_text(source["url"])

                # Check if we got meaningful sport/training content
                content_lower = content.lower()
                sport_indicators = ['training', 'competition', 'athlete', 'technique', 'performance', 'exercise', 'strength', 'endurance']
                has_sport_content = any(ind in content_lower for ind in sport_indicators)

                if len(content) > 500 and has_sport_content:
                    fetched_content = content
                    research_result["sources"].append(source["url"])
                    break
            except Exception:
                continue

        # Extract relevant information from fetched content
        if fetched_content:
            content_lower = fetched_content.lower()
            sentences = re.split(r'[.!?]+', fetched_content)

            # Keywords for different aspects of sport training
            training_keywords = ["training", "workout", "practice", "conditioning", "preparation"]
            periodization_keywords = ["season", "off-season", "peak", "competition", "periodization", "cycle", "phase"]
            injury_keywords = ["injury", "injuries", "strain", "overuse", "prevention", "risk"]
            strength_keywords = ["strength", "power", "muscle", "resistance", "weight"]
            endurance_keywords = ["endurance", "aerobic", "cardio", "stamina", "cardiovascular"]
            technique_keywords = ["technique", "skill", "form", "mechanics", "coordination"]

            training_findings = []
            periodization_findings = []
            injury_findings = []
            physical_demands = []
            technique_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 30:  # Skip very short fragments
                    if any(kw in sentence_lower for kw in training_keywords):
                        training_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in periodization_keywords):
                        periodization_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in injury_keywords):
                        injury_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in strength_keywords + endurance_keywords):
                        physical_demands.append(sentence_clean)
                    if any(kw in sentence_lower for kw in technique_keywords):
                        technique_findings.append(sentence_clean)

            research_result["researched_info"] = {
                "training_approaches": training_findings[:5] if training_findings else [f"Research specific training protocols for {sport_name}"],
                "periodization": periodization_findings[:3] if periodization_findings else ["Periodization varies by competition schedule"],
                "common_injuries": injury_findings[:4] if injury_findings else [f"Research common {sport_name} injuries for prevention planning"],
                "physical_demands": physical_demands[:4] if physical_demands else ["Assess physical demands through athlete interview"],
                "technique_notes": technique_findings[:3] if technique_findings else ["Technical development is sport-specific"],
            }

            # Generate training implications for the coach
            implications = []
            if any("endurance" in s.lower() for s in physical_demands):
                implications.append("Include aerobic base building in training")
            if any("strength" in s.lower() or "power" in s.lower() for s in physical_demands):
                implications.append("Strength training is important for this sport")
            if any("technique" in s.lower() or "skill" in s.lower() for s in technique_findings):
                implications.append("Allocate time for sport-specific skill work")
            if injury_findings:
                implications.append("Plan injury prevention work based on common injury patterns")

            research_result["training_implications"] = implications if implications else [
                f"Gather more specific information about {sport_name} training requirements from the athlete"
            ]

            research_result["content_preview"] = fetched_content[:1500]

        else:
            # Couldn't fetch - provide guidance
            research_result["researched_info"] = {
                "note": f"Unable to fetch research for '{sport_name}'. Gather info via athlete interview:",
                "questions_to_ask": [
                    f"What does a typical {sport_name} training week look like?",
                    "What are the main physical demands of the sport?",
                    "What injuries are common in this sport?",
                    "What does your competition schedule look like?",
                    "What does peak performance look like vs off-season?"
                ]
            }

        return json.dumps(research_result, indent=2)

    except Exception as e:
        logger.exception("research_sport failed")
        return json.dumps({"error": str(e)})
