"""Source-derived action choices and checks for known unsupported report claims."""
import re


def action_catalog(data):
    actions = []
    missing = data.get("missing_count", 0)
    if missing:
        actions.append(f"Verify the original scripts and mark register for the {missing} missing topic record(s); capture results only when supported by the source.")
    flagged = sum(bool(row.get("support_review")) for row in data.get("learners", []))
    if flagged:
        threshold = data.get("support_threshold", 50)
        actions.append(f"Review targeted support for the {flagged} learner(s) whose recorded mean is below the configured {threshold:g}% threshold.")
    counts = data.get("attendance_recorded_status_counts", {})
    if not sum(counts.values()):
        actions.append("Obtain the attendance register before evaluating attendance.")
    elif counts.get("absent", 0):
        actions.append(f"Review the {counts['absent']} supplied absence record(s) with the teacher; establish the context before drawing conclusions.")
    for topic in data.get("topics", [])[:3]:
        actions.append(f"Offer optional practice on {topic['topic']}; use subsequent marked work to decide whether individual support is needed.")
    actions.append("Review subsequent verified assessment records and update the support plan when the evidence changes.")
    return actions


def grounding_issues(stage, result, data):
    issues = []
    if stage in {"intervention_planner", "reporting_agent"}:
        selected = result.get("recommendations", [])
        allowed = action_catalog(data)
        if any(item not in allowed for item in selected):
            issues.append("Select recommendations verbatim from the supplied approved_action_catalog. Do not infer that missing marks require a make-up assessment.")
        if data.get("missing_count", 0) and allowed[0] not in selected:
            issues.append("Include the source-supported missing-record verification action.")
        if len(selected) != len(set(selected)):
            issues.append("Remove duplicate recommendations.")
    if stage == "coordinator" and not any(row.get("support_review") for row in data.get("learners", [])):
        for focus in result.get("focus_topics", []):
            text = focus.lower()
            if ("threshold" in text and ("breach" in text or "below" in text)
                    and not text.startswith(("no ", "none ", "not "))):
                issues.append("No learner is flagged below the support threshold. Do not claim a threshold breach.")
    if stage == "reporting_agent":
        count, distinct = data.get("marked_count"), data.get("assessment_count")
        if count and distinct is not None and count != distinct:
            text = result.get("summary", "").lower()
            patterns = [rf"\b{count}\s+(?:assessments|tests)\s+(?:marked|completed)",
                        rf"\b{count}\s+marked\s+(?:assessments|tests)\b"]
            if any(re.search(pattern, text) for pattern in patterns):
                issues.append("marked_count counts learner-assessment-topic records; assessment_count counts distinct assessments. State these units explicitly.")
    return issues

