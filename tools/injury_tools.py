"""Injury tools - diagnose, research, and track injuries."""

from mcp_app import mcp
from web_utils import fetch_page_text
from planner import load_json_file, save_json_file, load_athlete
from config import (INJURY_ASSESSMENT_QUESTIONS, INJURY_SEVERITY_LEVELS,
                    INJURY_STATUS_OPTIONS, ATHLETE_FILE)
from datetime import date
import json
import re


@mcp.tool()
def diagnose_injury(location: str, answers: str = None) -> str:
    """
    Clinical assessment tool for sports injuries. Uses a two-phase approach.

    Phase 1 - Get assessment questions:
        Call with just location to receive clinical questions to ask the athlete.
        Example: diagnose_injury(location="shin")

    Phase 2 - Get diagnosis:
        Call with location + answers (JSON string) to receive diagnosis.
        Example: diagnose_injury(location="shin", answers='{"onset": "Gradual", ...}')

    Args:
        location: Body part (shin, knee, ankle, back, shoulder, hip, foot, calf)
        answers: JSON string of answers to assessment questions (optional)

    Returns:
        Phase 1: JSON with clinical assessment questions
        Phase 2: JSON with possible conditions, severity, and recommendations
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

        # Phase 2: Analyze answers and provide diagnosis
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

        # Build possible conditions based on location and symptoms
        possible_conditions = _get_possible_conditions(body_region, clinical_picture, answers_dict)

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

        return json.dumps({
            "location": location,
            "phase": "diagnosis",
            "clinical_picture": clinical_picture,
            "possible_conditions": possible_conditions,
            "severity_assessment": severity,
            "severity_reasoning": severity_reasoning,
            "red_flags": red_flags if red_flags else ["None identified based on assessment"],
            "recommended_action": recommended_action,
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


def _get_possible_conditions(body_region: str, clinical: dict, answers: dict) -> list:
    """Helper to determine possible conditions based on body region and symptoms."""

    conditions = []

    if body_region == "shin":
        if "anterior" in clinical.get("location_specific", "").lower() or "front" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Anterior Tibialis Tendinitis",
                "likelihood": "high" if clinical["onset"] == "gradual" else "medium",
                "description": "Inflammation of the anterior tibialis tendon from overuse",
                "why_matches": [f for f in [
                    "Front of shin" if "anterior" in clinical.get("location_specific", "").lower() else None,
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Volume increase" if "increase" in answers.get("recent_changes", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Sudden volume increase", "Tight calves", "Hill running"],
            })
        if "medial" in clinical.get("location_specific", "").lower() or "along the bone" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Shin Splints (MTSS)",
                "likelihood": "high" if clinical["onset"] == "gradual" else "medium",
                "description": "Medial tibial stress syndrome - inflammation along the shin bone",
                "why_matches": [f for f in [
                    "Medial/bone location",
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Running aggravates" if "running" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Hard surfaces", "Poor footwear", "Flat feet"],
            })
        if clinical["swelling"] and clinical["onset"] == "gradual":
            conditions.append({
                "name": "Stress Fracture (tibial)",
                "likelihood": "low",
                "description": "Micro-fracture of the tibia from repetitive stress",
                "why_matches": ["Gradual onset with swelling - needs professional evaluation"],
                "typical_causes": ["Overtraining", "Rapid volume increase", "Poor bone density"],
                "warning": "If pain is localized to one spot and severe, seek imaging"
            })

    elif body_region == "knee":
        if "front" in clinical.get("location_specific", "").lower() or "kneecap" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Patellofemoral Pain Syndrome (Runner's Knee)",
                "likelihood": "high",
                "description": "Pain around or behind the kneecap",
                "why_matches": [f for f in [
                    "Front/kneecap location",
                    "Worse with stairs" if "stairs" in answers.get("aggravating", "").lower() else None,
                    "Worse with squatting" if "squat" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Muscle imbalance", "Overuse", "Poor tracking"],
            })
        if "lateral" in clinical.get("location_specific", "").lower() or "outer" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "IT Band Syndrome",
                "likelihood": "high" if "running" in answers.get("aggravating", "").lower() else "medium",
                "description": "Inflammation where IT band crosses the knee",
                "why_matches": [f for f in [
                    "Lateral/outer location",
                    "Running aggravates" if "running" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Running", "Cycling", "Weak hip abductors"],
            })

    elif body_region == "ankle":
        if "lateral" in clinical.get("location_specific", "").lower() or "outer" in clinical.get("location_specific", "").lower():
            if clinical["onset"] == "acute":
                conditions.append({
                    "name": "Lateral Ankle Sprain",
                    "likelihood": "high",
                    "description": "Stretching or tearing of lateral ankle ligaments",
                    "why_matches": ["Sudden onset", "Outer ankle location"],
                    "typical_causes": ["Rolling ankle inward", "Uneven surface", "Landing awkwardly"],
                })
            else:
                conditions.append({
                    "name": "Peroneal Tendinitis",
                    "likelihood": "high",
                    "description": "Inflammation of tendons on outer ankle",
                    "why_matches": ["Gradual onset", "Lateral location"],
                    "typical_causes": ["Overuse", "Running on uneven surfaces"],
                })
        if "achilles" in clinical.get("location_specific", "").lower() or "back" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Achilles Tendinitis/Tendinopathy",
                "likelihood": "high",
                "description": "Inflammation or degeneration of the Achilles tendon",
                "why_matches": [f for f in [
                    "Posterior/Achilles location",
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Worse with pushing off" if "push" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Tight calves", "Hill running", "Sudden volume increase"],
            })

    elif body_region == "foot":
        if "heel" in clinical.get("location_specific", "").lower() and "bottom" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Plantar Fasciitis",
                "likelihood": "high" if "first steps" in answers.get("aggravating", "").lower() else "medium",
                "description": "Inflammation of the plantar fascia under the foot",
                "why_matches": [f for f in [
                    "Heel/bottom location",
                    "Worse with first steps in morning" if "morning" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Tight calves", "Poor arch support", "Sudden volume increase"],
            })

    elif body_region == "calf":
        if clinical["onset"] == "acute":
            conditions.append({
                "name": "Calf Muscle Strain",
                "likelihood": "high",
                "description": "Tear or strain of gastrocnemius or soleus muscle",
                "why_matches": ["Sudden onset", "Calf location"],
                "typical_causes": ["Explosive movement", "Sprinting", "Jumping", "Fatigue"],
            })
        else:
            conditions.append({
                "name": "Calf Muscle Tightness/Overuse",
                "likelihood": "high",
                "description": "Muscle fatigue and tightness from overuse",
                "why_matches": ["Gradual onset"],
                "typical_causes": ["Overtraining", "Inadequate stretching", "Volume increase"],
            })

    # If no specific conditions matched, add general options
    if not conditions:
        conditions.append({
            "name": "Soft Tissue Injury (unspecified)",
            "likelihood": "medium",
            "description": f"Injury to {body_region} area - may be muscular, tendon, or ligament",
            "why_matches": ["Location and symptoms suggest soft tissue involvement"],
            "typical_causes": ["Overuse", "Trauma", "Muscle imbalance"],
        })

    return conditions


@mcp.tool()
def research_injury(injury_type: str, severity: str = "moderate", url: str = None) -> str:
    """
    Research treatment protocols and recovery timelines for a specific injury.

    Fetches information from provided URL or tries common medical sources.
    Each injury is researched uniquely rather than using static protocols.

    Args:
        injury_type: Name of the injury (e.g., "anterior tibialis tendinitis")
        severity: mild, moderate, or severe (provides context for research)
        url: Optional direct URL to a resource about this injury

    Returns:
        JSON with researched information including treatment approaches,
        recovery expectations, and sources.

    Usage patterns:
        1. Direct URL: research_injury("shin splints", url="https://en.wikipedia.org/wiki/Shin_splints")
        2. Auto-search: research_injury("shin splints") - tries Wikipedia and other sources
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

        # Format injury name for URL
        injury_url_name = injury_type.replace(' ', '_')

        # Build list of URLs to try
        if url:
            # Use provided URL first
            search_sources = [{"name": "Provided URL", "url": url, "type": "direct"}]
        else:
            # Try common medical sources
            search_sources = []

        # Always add fallback sources
        search_sources.extend([
            # Wikipedia - usually accessible and has good medical content
            {
                "name": "Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{injury_url_name}",
                "type": "clinical"
            },
        ])

        # Try to fetch from sports medicine sources
        fetched_content = None
        for source in search_sources:
            try:
                content = fetch_page_text(source["url"])

                # Check if we got meaningful clinical content (not just a 404 or search page)
                content_lower = content.lower()
                clinical_indicators = ['treatment', 'diagnosis', 'symptoms', 'rehabilitation', 'causes', 'clinical', 'management']
                has_clinical_content = any(ind in content_lower for ind in clinical_indicators)

                if len(content) > 500 and has_clinical_content:
                    fetched_content = content
                    research_result["sources"].append(source["url"])
                    break
            except Exception:
                continue

        # Extract relevant information from fetched content
        if fetched_content:
            content_lower = fetched_content.lower()

            # Look for treatment-related content
            treatment_keywords = ["treatment", "management", "therapy", "intervention"]
            rehab_keywords = ["rehabilitation", "exercise", "stretching", "strengthening"]
            timeline_keywords = ["recovery", "healing", "duration", "weeks", "days"]

            # Extract sentences containing relevant keywords
            sentences = re.split(r'[.!?]+', fetched_content)
            treatment_findings = []
            rehab_findings = []
            timeline_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 20:  # Skip very short fragments
                    if any(kw in sentence_lower for kw in treatment_keywords):
                        treatment_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in rehab_keywords):
                        rehab_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in timeline_keywords):
                        timeline_findings.append(sentence_clean)

            research_result["researched_info"] = {
                "treatment_approaches": treatment_findings[:5] if treatment_findings else ["Research specific treatment protocols with your physiotherapist"],
                "rehabilitation": rehab_findings[:5] if rehab_findings else ["Gradual return to activity under professional guidance"],
                "recovery_insights": timeline_findings[:3] if timeline_findings else ["Recovery time varies based on severity and individual factors"],
            }

            # Try to extract specific recommendations
            research_result["raw_findings"] = {
                "content_preview": fetched_content[:1000],
                "note": "Review the content above for detailed information specific to this injury"
            }

        else:
            # Couldn't fetch - provide guidance on what to research
            research_result["researched_info"] = {
                "note": f"Unable to fetch current research for '{injury_type}'. Recommend searching:",
                "suggested_searches": [
                    f"{injury_type} treatment protocol",
                    f"{injury_type} rehabilitation exercises",
                    f"{injury_type} recovery timeline athlete",
                    f"{injury_type} return to sport criteria",
                ],
                "recommended_sources": [
                    "physio-pedia.com",
                    "sportsinjuryclinic.net",
                    "Your local sports physiotherapist",
                ]
            }

        # Add severity-based general guidance
        severity_guidance = {
            "mild": {
                "general_approach": "Often manageable with relative rest and self-care",
                "typical_timeframe": "Usually improves within 1-3 weeks with appropriate management",
                "professional_advice": "See a physio if no improvement after 7-10 days",
            },
            "moderate": {
                "general_approach": "May require modified activities and structured rehabilitation",
                "typical_timeframe": "Expect 3-6 weeks for significant improvement",
                "professional_advice": "Professional assessment recommended for proper diagnosis and treatment plan",
            },
            "severe": {
                "general_approach": "Requires professional evaluation and structured treatment",
                "typical_timeframe": "Recovery often takes 6-12+ weeks",
                "professional_advice": "See a healthcare professional promptly - may need imaging or specialist referral",
            },
        }

        research_result["severity_context"] = severity_guidance.get(severity, severity_guidance["moderate"])

        # Activity guidance based on injury location/type
        injury_lower = injury_type.lower()
        if any(term in injury_lower for term in ["shin", "tibialis", "calf", "achilles", "plantar", "foot", "ankle"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["cycling", "swimming", "upper body strength", "core work"],
                "likely_restricted": ["running", "jumping", "high-impact activities"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["knee", "patella", "it band", "meniscus"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["swimming", "upper body strength", "non-weight-bearing activities"],
                "likely_restricted": ["running", "squatting", "stairs", "cycling (depends on injury)"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["shoulder", "rotator"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["lower body activities", "walking", "cycling", "core work"],
                "likely_restricted": ["overhead movements", "swimming (depends)", "pushing/pulling"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["back", "spine", "disc"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["walking", "swimming", "gentle movement"],
                "likely_restricted": ["heavy lifting", "high-impact", "prolonged sitting"],
                "note": "Back injuries vary significantly - professional assessment essential"
            }
        else:
            research_result["activity_guidance"] = {
                "general": "Avoid activities that aggravate symptoms",
                "cross_training": "Usually possible to maintain fitness with alternative activities",
                "note": "Confirm specific restrictions with your physiotherapist"
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
