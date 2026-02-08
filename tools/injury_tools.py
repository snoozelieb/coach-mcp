"""Injury tools - diagnose, research, and track injuries.

Uses orthobullets.com as primary clinical reference with Wikipedia fallback.
Diagnosis logic is delegated to the LLM — this module provides clinical context,
severity assessment, and research content for the LLM to reason about.
"""

from mcp_app import mcp
from web_utils import fetch_page_text, fetch_page_text_validated
from planner import load_json_file, save_json_file, load_athlete
from config import (INJURY_ASSESSMENT_QUESTIONS, INJURY_SEVERITY_LEVELS,
                    INJURY_STATUS_OPTIONS, ATHLETE_FILE,
                    ORTHOBULLETS_BASE_URL, ORTHOBULLETS_SPECIALTIES)
from datetime import date
import json
import re


# ---------------------------------------------------------------------------
# Search term mapping: body region + sublocation → orthobullets search slugs
# This is *information retrieval* (what article to fetch), not diagnosis.
# ---------------------------------------------------------------------------

_SEARCH_TERM_MAP = {
    "shin": {
        "anterior": ["anterior-tibialis-tendinitis", "shin-splints"],
        "front": ["anterior-tibialis-tendinitis", "shin-splints"],
        "medial": ["medial-tibial-stress-syndrome", "shin-splints"],
        "along the bone": ["medial-tibial-stress-syndrome", "tibial-stress-fracture"],
        "lateral": ["peroneal-tendinitis", "compartment-syndrome"],
        "_default": ["shin-splints", "tibial-stress-fracture"],
    },
    "knee": {
        "front": ["patellofemoral-pain-syndrome", "patellar-tendinitis"],
        "kneecap": ["patellofemoral-pain-syndrome", "chondromalacia-patella"],
        "medial": ["medial-collateral-ligament-mcl-injury", "medial-meniscus-tear"],
        "lateral": ["iliotibial-band-syndrome", "lateral-meniscus-tear"],
        "behind": ["bakers-cyst", "posterior-cruciate-ligament-pcl-injury"],
        "_default": ["knee-pain", "patellofemoral-pain-syndrome"],
    },
    "ankle": {
        "lateral": ["lateral-ankle-sprain", "peroneal-tendinitis"],
        "outer": ["lateral-ankle-sprain", "peroneal-tendinitis"],
        "medial": ["medial-ankle-sprain", "posterior-tibial-tendinitis"],
        "achilles": ["achilles-tendinitis", "achilles-tendon-rupture"],
        "back": ["achilles-tendinitis", "retrocalcaneal-bursitis"],
        "front": ["anterior-ankle-impingement"],
        "_default": ["ankle-sprain", "achilles-tendinitis"],
    },
    "back": {
        "lower": ["low-back-pain", "lumbar-disc-herniation"],
        "lumbar": ["low-back-pain", "lumbar-disc-herniation"],
        "mid": ["thoracic-back-pain"],
        "thoracic": ["thoracic-back-pain"],
        "upper": ["cervical-strain", "neck-pain"],
        "_default": ["low-back-pain", "lumbar-disc-herniation"],
    },
    "shoulder": {
        "front": ["biceps-tendinitis", "anterior-shoulder-instability"],
        "top": ["acromioclavicular-joint-injury", "shoulder-impingement"],
        "back": ["posterior-shoulder-instability", "infraspinatus-tear"],
        "side": ["rotator-cuff-tendinitis", "shoulder-impingement"],
        "_default": ["rotator-cuff-tendinitis", "shoulder-impingement"],
    },
    "hip": {
        "front": ["hip-flexor-strain", "femoroacetabular-impingement"],
        "groin": ["hip-flexor-strain", "adductor-strain"],
        "lateral": ["greater-trochanteric-bursitis", "gluteus-medius-tear"],
        "side": ["greater-trochanteric-bursitis", "iliotibial-band-syndrome"],
        "back": ["piriformis-syndrome", "hamstring-origin-tendinopathy"],
        "buttock": ["piriformis-syndrome", "hamstring-origin-tendinopathy"],
        "_default": ["hip-pain", "greater-trochanteric-bursitis"],
    },
    "foot": {
        "heel": ["plantar-fasciitis", "calcaneal-stress-fracture"],
        "bottom": ["plantar-fasciitis", "metatarsalgia"],
        "arch": ["plantar-fasciitis", "posterior-tibial-tendinitis"],
        "ball": ["metatarsalgia", "mortons-neuroma"],
        "toes": ["turf-toe", "mortons-neuroma"],
        "_default": ["plantar-fasciitis", "foot-pain"],
    },
    "calf": {
        "upper": ["gastrocnemius-strain", "calf-strain"],
        "mid": ["calf-strain", "soleus-strain"],
        "lower": ["achilles-tendinitis", "soleus-strain"],
        "near achilles": ["achilles-tendinitis"],
        "_default": ["calf-strain", "achilles-tendinitis"],
    },
}


def _build_search_terms(body_region: str, location_specific: str) -> list[str]:
    """Build orthobullets slug search terms from body region + sublocation."""
    region_map = _SEARCH_TERM_MAP.get(body_region, {})
    location_lower = location_specific.lower() if location_specific else ""

    # Try to match sublocation keywords
    for key, slugs in region_map.items():
        if key == "_default":
            continue
        if key in location_lower:
            return slugs

    # Fallback to default for this region
    return region_map.get("_default", [f"{body_region}-pain"])


def _is_relevant_content(content: str, body_region: str, search_term: str) -> bool:
    """Validate fetched content is actually about the injury (not an unrelated redirect).

    Checks: minimum length, clinical indicators present, and at least one
    meaningful word from the search term appears in the content.
    """
    if len(content) < 500:
        return False

    content_lower = content.lower()

    # Must have clinical indicators
    clinical_indicators = ['treatment', 'diagnosis', 'symptoms', 'rehabilitation',
                           'causes', 'clinical', 'management', 'presentation',
                           'epidemiology', 'complications']
    if not any(ind in content_lower for ind in clinical_indicators):
        return False

    # At least one meaningful word from search term should appear
    # Split slug into words (e.g. "achilles-tendinitis" → ["achilles", "tendinitis"])
    term_words = [w for w in search_term.replace('-', ' ').split() if len(w) > 3]
    if term_words and not any(w in content_lower for w in term_words):
        return False

    return True


def _extract_clinical_info(content: str) -> dict:
    """Extract structured clinical findings from fetched text.

    Categorises sentences by keyword matching into treatment, rehabilitation,
    presentation, recovery_timeline, risk_factors, and complications.
    """
    sentences = re.split(r'[.!?]+', content)

    categories = {
        "treatment": ["treatment", "management", "therapy", "intervention", "conservative", "surgical"],
        "rehabilitation": ["rehabilitation", "exercise", "stretching", "strengthening", "physical therapy", "physiotherapy"],
        "presentation": ["presentation", "symptom", "sign", "examination", "clinical finding", "history"],
        "recovery_timeline": ["recovery", "healing", "duration", "weeks", "days", "months", "return to"],
        "risk_factors": ["risk factor", "etiology", "cause", "predispos", "mechanism"],
        "complications": ["complication", "prognosis", "recurrence", "chronic", "sequelae"],
    }

    results = {cat: [] for cat in categories}

    for sentence in sentences:
        sentence_clean = sentence.strip()
        sentence_lower = sentence_clean.lower()
        if len(sentence_clean) < 20 or len(sentence_clean) > 300:
            continue
        for cat, keywords in categories.items():
            if any(kw in sentence_lower for kw in keywords):
                results[cat].append(sentence_clean)

    # Trim to top findings per category
    return {cat: findings[:5] for cat, findings in results.items() if findings}


def _fetch_injury_research(body_region: str, clinical_picture: dict) -> dict:
    """Fetch orthobullets content relevant to body region + clinical picture.

    Tries orthobullets URLs first (slug across specialties), validates content
    is relevant (protects against redirects), falls back to Wikipedia.

    Returns dict with keys: source, content, url, clinical_info.
    """
    location_specific = clinical_picture.get("location_specific", "")
    search_terms = _build_search_terms(body_region, location_specific)

    # Try orthobullets first
    for slug in search_terms:
        for specialty in ORTHOBULLETS_SPECIALTIES:
            url = f"{ORTHOBULLETS_BASE_URL}/{specialty}/{slug}"
            try:
                content, final_url = fetch_page_text_validated(url)
                if _is_relevant_content(content, body_region, slug):
                    clinical_info = _extract_clinical_info(content)
                    return {
                        "source": "orthobullets",
                        "content": content[:3000],
                        "url": final_url,
                        "clinical_info": clinical_info,
                    }
            except Exception:
                continue

    # Fallback: Wikipedia
    wiki_terms = search_terms[0].replace('-', '_') if search_terms else body_region
    wiki_url = f"https://en.wikipedia.org/wiki/{wiki_terms}"
    try:
        content, final_url = fetch_page_text_validated(wiki_url)
        if _is_relevant_content(content, body_region, search_terms[0] if search_terms else body_region):
            clinical_info = _extract_clinical_info(content)
            return {
                "source": "wikipedia",
                "content": content[:3000],
                "url": final_url,
                "clinical_info": clinical_info,
            }
    except Exception:
        pass

    # Nothing worked
    return {"source": "none", "content": "", "url": "", "clinical_info": {}}


def _save_diagnosis_to_profile(body_region: str, location: str,
                               severity: str, clinical_picture: dict) -> bool:
    """Save diagnosed injury to athlete.json (injury_history + coaching_notes).

    Deduplicates: if same body_region + same date already exists, updates it.
    Returns True on success, False on error.
    """
    try:
        athlete = load_athlete()
        # Remove baseline data before saving (load_athlete merges them in)
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        today = date.today().isoformat()

        # --- injury_history ---
        injury_history = athlete.get('injury_history', [])

        # Check for existing entry (same region + same date)
        existing = None
        for entry in injury_history:
            if entry.get('body_region') == body_region and entry.get('date') == today:
                existing = entry
                break

        injury_entry = {
            "date": today,
            "body_region": body_region,
            "location": location,
            "severity": severity,
            "status": "active",
            "onset": clinical_picture.get("onset", "unknown"),
            "pain_type": clinical_picture.get("pain_type", "unknown"),
            "location_specific": clinical_picture.get("location_specific", ""),
        }

        if existing:
            existing.update(injury_entry)
        else:
            injury_history.append(injury_entry)
            athlete['injury_history'] = injury_history

        # --- coaching_notes ---
        summary = f"[{today}] Injury: {location} ({body_region}), severity={severity}, onset={clinical_picture.get('onset', 'unknown')}"
        existing_notes = athlete.get('coaching_notes', '')
        if existing_notes:
            athlete['coaching_notes'] = f"{existing_notes}\n{summary}"
        else:
            athlete['coaching_notes'] = summary

        save_json_file(ATHLETE_FILE, athlete)
        return True
    except Exception:
        return False


@mcp.tool()
def diagnose_injury(location: str, answers: str = None) -> str:
    """
    Clinical assessment tool for sports injuries. Uses a two-phase approach.

    Phase 1 - Get assessment questions:
        Call with just location to receive clinical questions to ask the athlete.
        Example: diagnose_injury(location="shin")

    Phase 2 - Get clinical context for LLM diagnosis:
        Call with location + answers (JSON string) to receive severity assessment,
        research context from orthobullets/Wikipedia, and save to athlete profile.
        The LLM performs differential diagnosis using the returned research context.
        Example: diagnose_injury(location="shin", answers='{"onset": "Gradual", ...}')

    Args:
        location: Body part (shin, knee, ankle, back, shoulder, hip, foot, calf)
        answers: JSON string of answers to assessment questions (optional)

    Returns:
        Phase 1: JSON with clinical assessment questions
        Phase 2: JSON with clinical picture, severity, research context, and recommendations
    """
    try:
        location_lower = location.lower().strip()

        # Map common terms to our body regions
        location_map = {
            "left shin": "shin", "right shin": "shin",
            "left knee": "knee", "right knee": "knee",
            "left ankle": "ankle", "right ankle": "ankle",
            "lower back": "back", "upper back": "back",
            "left shoulder": "shoulder", "right shoulder": "shoulder",
            "left hip": "hip", "right hip": "hip",
            "left foot": "foot", "right foot": "foot",
            "left calf": "calf", "right calf": "calf",
        }

        # Extract body region
        body_region = location_map.get(location_lower, location_lower)
        for key in location_map:
            if key in location_lower:
                body_region = location_map[key]
                break

        # Phase 1: Return assessment questions
        if answers is None:
            # Get default questions + region-specific questions
            questions = INJURY_ASSESSMENT_QUESTIONS.get("default", []).copy()
            region_questions = INJURY_ASSESSMENT_QUESTIONS.get(body_region, [])
            questions.extend(region_questions)

            return json.dumps({
                "location": location,
                "body_region": body_region,
                "phase": "assessment",
                "questions": questions,
                "instructions": "Ask these questions to gather clinical information, then call again with answers."
            }, indent=2)

        # Phase 2: Analyze answers and provide clinical context
        try:
            answers_dict = json.loads(answers)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid answers JSON: {str(e)}"})

        # Build clinical picture from answers
        clinical_picture = {
            "location": location,
            "body_region": body_region,
            "onset": "acute" if "sudden" in answers_dict.get("onset", "").lower() else "gradual",
            "pain_type": answers_dict.get("pain_type", "unknown"),
            "timing": answers_dict.get("timing", "unknown"),
            "swelling": "yes" in answers_dict.get("swelling", "").lower(),
            "location_specific": answers_dict.get("location_specific", ""),
            "aggravating_factors": answers_dict.get("aggravating", ""),
            "history": "yes" in answers_dict.get("history", "").lower(),
            "recent_changes": answers_dict.get("recent_changes", ""),
        }

        # Determine severity based on answers
        severity = "mild"
        severity_reasoning = []

        if "rest" in answers_dict.get("timing", "").lower() or "constant" in answers_dict.get("timing", "").lower():
            severity = "severe"
            severity_reasoning.append("Pain at rest or constant pain")
        elif "during and after" in answers_dict.get("timing", "").lower():
            severity = "moderate"
            severity_reasoning.append("Pain persists after activity")

        if "significant" in answers_dict.get("swelling", "").lower():
            severity = "severe"
            severity_reasoning.append("Significant swelling present")
        elif "slight" in answers_dict.get("swelling", "").lower():
            if severity == "mild":
                severity = "moderate"
            severity_reasoning.append("Some swelling present")

        if not severity_reasoning:
            severity_reasoning.append("Pain only during activity, no swelling")

        # Fetch research context (orthobullets → Wikipedia fallback)
        research_context = _fetch_injury_research(body_region, clinical_picture)

        # Determine red flags
        red_flags = []
        if severity == "severe":
            red_flags.append("Severe symptoms present - consider professional evaluation")
        if "radiation" in answers_dict and "past" in answers_dict.get("radiation", "").lower():
            red_flags.append("Radiating pain may indicate nerve involvement")
        if answers_dict.get("weight_bearing") == "No, too painful":
            red_flags.append("Unable to bear weight - may need imaging")
        if answers_dict.get("weakness") and "significant" in answers_dict.get("weakness", "").lower():
            red_flags.append("Significant weakness - may indicate tear or nerve issue")

        # Build recommendations
        if severity == "severe":
            recommended_action = "Rest immediately. Ice if swelling. See a healthcare professional within 24-48 hours."
        elif severity == "moderate":
            recommended_action = "Rest from aggravating activities. Ice 15-20 mins 3x/day. See physio if no improvement in 7-10 days."
        else:
            recommended_action = "Reduce activity intensity. Ice after exercise. Monitor for worsening symptoms."

        # Save to athlete profile
        saved = _save_diagnosis_to_profile(body_region, location, severity, clinical_picture)

        return json.dumps({
            "location": location,
            "phase": "diagnosis",
            "clinical_picture": clinical_picture,
            "severity_assessment": severity,
            "severity_reasoning": severity_reasoning,
            "red_flags": red_flags if red_flags else ["None identified based on assessment"],
            "recommended_action": recommended_action,
            "research_context": {
                "source": research_context.get("source", "none"),
                "content": research_context.get("content", ""),
                "url": research_context.get("url", ""),
                "clinical_info": research_context.get("clinical_info", {}),
            },
            "saved_to_profile": saved,
            "red_flags_to_watch": [
                "Pain becoming severe or constant",
                "Swelling increases",
                "Unable to bear weight",
                "Numbness or tingling",
                "No improvement after 7-10 days rest"
            ],
            "disclaimer": "This is informational only, not medical advice. Consult a healthcare professional for diagnosis and treatment."
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def research_injury(injury_type: str, severity: str = "moderate", url: str = None) -> str:
    """
    Research treatment protocols and recovery timelines for a specific injury.

    Fetches information from orthobullets.com (primary) or Wikipedia (fallback),
    or a directly provided URL. Each injury is researched uniquely rather than
    using static protocols.

    Args:
        injury_type: Name of the injury (e.g., "anterior tibialis tendinitis")
        severity: mild, moderate, or severe (provides context for research)
        url: Optional direct URL to a resource about this injury

    Returns:
        JSON with researched information including treatment approaches,
        recovery expectations, and sources.

    Usage patterns:
        1. Direct URL: research_injury("shin splints", url="https://orthobullets.com/...")
        2. Auto-search: research_injury("shin splints") - tries orthobullets then Wikipedia
    """
    try:
        if severity.lower() not in INJURY_SEVERITY_LEVELS:
            severity = "moderate"

        research_result = {
            "injury": injury_type,
            "severity": severity,
            "researched_info": {},
            "sources": [],
            "raw_findings": [],
        }

        # Format injury name for URL slugs
        injury_slug = injury_type.lower().replace(' ', '-').replace('_', '-')
        injury_wiki = injury_type.replace(' ', '_')

        # Build list of URLs to try
        sources_to_try = []

        if url:
            sources_to_try.append({"name": "Provided URL", "url": url})

        # Orthobullets: try slug across specialties
        for specialty in ORTHOBULLETS_SPECIALTIES:
            sources_to_try.append({
                "name": "Orthobullets",
                "url": f"{ORTHOBULLETS_BASE_URL}/{specialty}/{injury_slug}",
            })

        # Wikipedia fallback
        sources_to_try.append({
            "name": "Wikipedia",
            "url": f"https://en.wikipedia.org/wiki/{injury_wiki}",
        })

        # Try to fetch from sources
        fetched_content = None
        for source in sources_to_try:
            try:
                content, final_url = fetch_page_text_validated(source["url"])
                if _is_relevant_content(content, "", injury_slug):
                    fetched_content = content
                    research_result["sources"].append(final_url)
                    break
            except Exception:
                continue

        # Extract relevant information from fetched content
        if fetched_content:
            clinical_info = _extract_clinical_info(fetched_content)
            research_result["researched_info"] = clinical_info if clinical_info else {
                "note": "Content fetched but no structured findings extracted"
            }

            research_result["raw_findings"] = {
                "content_preview": fetched_content[:1000],
                "note": "Review the content above for detailed information specific to this injury"
            }
        else:
            research_result["researched_info"] = {
                "note": f"Unable to fetch current research for '{injury_type}'. Recommend searching:",
                "suggested_searches": [
                    f"{injury_type} treatment protocol",
                    f"{injury_type} rehabilitation exercises",
                    f"{injury_type} recovery timeline athlete",
                    f"{injury_type} return to sport criteria",
                ],
                "recommended_sources": [
                    "orthobullets.com",
                    "physio-pedia.com",
                    "Your local sports physiotherapist",
                ]
            }

        research_result["disclaimer"] = "This is researched information, not medical advice. Each injury is unique - consult a healthcare professional for diagnosis and personalized treatment."

        return json.dumps(research_result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_injury_status(
    injury_date: str,
    new_status: str = None,
    notes: str = None,
    severity: str = None
) -> str:
    """
    Update an existing injury's status and add progress notes.

    Args:
        injury_date: Date of the injury to update (YYYY-MM-DD)
        new_status: New status (active, improving, resolved)
        notes: Progress note to add
        severity: Updated severity (mild, moderate, severe)

    Returns:
        Confirmation with updated injury details.
    """
    try:
        athlete = load_athlete()
        # Remove baseline data before saving
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        injury_history = athlete.get('injury_history', [])

        # Find the injury by date
        found_injury = None
        for injury in injury_history:
            if injury.get('date') == injury_date:
                found_injury = injury
                break

        if not found_injury:
            return json.dumps({
                "error": f"No injury found for date {injury_date}",
                "existing_injuries": [i.get('date') for i in injury_history]
            })

        changes = []

        if new_status:
            if new_status.lower() not in INJURY_STATUS_OPTIONS:
                return json.dumps({
                    "error": f"Invalid status. Must be one of: {INJURY_STATUS_OPTIONS}"
                })
            found_injury['status'] = new_status.lower()
            changes.append(f"status -> {new_status.lower()}")

        if severity:
            if severity.lower() not in INJURY_SEVERITY_LEVELS:
                return json.dumps({
                    "error": f"Invalid severity. Must be one of: {INJURY_SEVERITY_LEVELS}"
                })
            found_injury['severity'] = severity.lower()
            changes.append(f"severity -> {severity.lower()}")

        if notes:
            # Add to progress notes
            if 'progress_notes' not in found_injury:
                found_injury['progress_notes'] = []
            found_injury['progress_notes'].append({
                "date": date.today().isoformat(),
                "note": notes
            })
            changes.append("progress note added")

        if not changes:
            return json.dumps({"error": "No updates provided"})

        # Save back
        save_json_file(ATHLETE_FILE, athlete)

        return json.dumps({
            "status": "success",
            "message": f"Updated injury from {injury_date}: {', '.join(changes)}",
            "injury": found_injury
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})
