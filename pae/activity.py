"""Public execution summaries. Never capture model thoughts or hidden reasoning."""
from .db import utcnow

AGENTS = {
    "coordinator": "Coordinator", "photo_reader": "Photo reader", "validator": "Evidence validator",
    "correction_agent": "Correction agent", "performance_analyst": "Performance analyst",
    "intervention_planner": "Intervention planner", "reporting_agent": "Reporting agent",
    "quality_reviewer": "Quality reviewer", "delivery": "Report delivery",
}
PLANS = {
    "coordinator": "Inspect the saved evidence and choose the specialists required for this task.",
    "performance_analyst": "Inspect source metrics, topic coverage and evidence limitations.",
    "intervention_planner": "Select support actions justified by the recorded evidence.",
    "reporting_agent": "Build the report from checked evidence and address any reviewer feedback.",
    "quality_reviewer": "Compare the report with source evidence; approve it or request a specific repair.",
}


def activity(agent, phase, message, *, tool="", evidence=None, at=None):
    return {"type": "agent_activity", "agent": agent, "phase": phase, "message": message,
            "tool": tool, "evidence": evidence or {}, "at": at or utcnow()}


def public_activity(events):
    """Retain explicit new events; old history remains visible without invented plans."""
    return [{"agent": item.get("agent", "coordinator"), "phase": item["phase"],
             "message": item.get("message", ""), "tool": item.get("tool", ""),
             "evidence": item.get("evidence", {}), "at": item.get("at", "")}
            for item in events if item.get("type") == "agent_activity"]


def output_summary(stage, result):
    if stage == "coordinator":
        return {key: result.get(key) for key in ("rationale", "analyse_performance", "plan_support", "focus_topics")}
    if stage == "performance_analyst":
        return {"findings": result.get("findings", [])}
    if stage == "intervention_planner":
        return {"recommendations": result.get("recommendations", [])}
    if stage == "reporting_agent":
        return {key: result.get(key) for key in ("summary", "mean_percentage", "marked_count", "missing_count")}
    return {key: result.get(key) for key in ("approved", "issues", "checked_mean_percentage", "checked_marked_count", "checked_missing_count")}
