"""Offline provider: the same five stages, executed by deterministic rules.

Why it exists
- A report must be producible with no API key, no network and no quota, so
  the workflow can be demonstrated, tested and relied on as a fallback.
- It plugs into the existing durable job system as provider "offline". Jobs,
  checkpoints, schema validation, numeric checks, the review/repair loop,
  snapshot fencing and audit all run exactly as for Gemini or Groq.

Contract
- offline_stage() has the same signature and return shape as agents.run_stage.
- Every output validates against the stage schema in agents.SCHEMAS.
- Text is rule-based, not model reasoning. Runs record provider "offline".

Self-correction demonstration
- With demonstrate_repair=True, the first reporting draft omits the
  missing-marks coverage statement. The offline reviewer rejects it with an
  explicit issue, the job's existing repair loop sends it back, and the
  repaired draft is approved. The flag is stored on the job and its events.
"""
from datetime import date
from .agents import SCHEMAS
from .grounding import action_catalog
from .providers import OFFLINE_MODEL


def _pct(value):
    return "no marked records" if value is None else f"{value:g}%"


def _coverage_line(data):
    return (f"Coverage: {data['marked_count']} of {data['expected_marks']} expected topic records are marked; "
            f"{data['missing_count']} missing. Missing marks are excluded, not counted as zero.")


def _weakest(data, limit=3):
    scored = [t for t in data.get("topics", []) if t.get("mean_percentage") is not None]
    return sorted(scored, key=lambda t: (t["mean_percentage"], t["topic"]))[:limit]


def _support_learners(data):
    return sorted((l for l in data.get("learners", []) if l.get("support_review")),
                  key=lambda l: (l["mean_percentage"], l["learner_id"]))


def _open_interventions(data):
    return [i for i in data.get("interventions", []) if i.get("status") != "completed"]


# ---------- stages ----------

def coordinator(data, goal, outputs):
    has_marks = data["marked_count"] > 0
    needs_support = bool(_support_learners(data) or data["missing_count"] or _open_interventions(data))
    focus = [t["topic"] for t in _weakest(data)]
    reasons = []
    reasons.append("analyse recorded performance" if has_marks else "skip analysis: no marked records yet")
    reasons.append("plan support" if needs_support else "skip support planning: no learner below threshold, missing marks or open support")
    return {"rationale": "Goal reviewed against the authorised snapshot. Plan: " + "; ".join(reasons) + ".",
            "analyse_performance": has_marks, "plan_support": needs_support, "focus_topics": focus}


def performance_analyst(data, goal, outputs):
    findings = [f"Marked-record average {_pct(data['mean_percentage'])} across {data['marked_count']} marked records "
                f"for {data['learner_count']} learners.",
                f"{data['missing_count']} of {data['expected_marks']} expected topic records are missing; they are not treated as zero."]
    for topic in _weakest(data):
        findings.append(f"{topic['topic']}: {_pct(topic['mean_percentage'])} over {topic['marked']} marked records.")
    below = _support_learners(data)
    if below:
        findings.append(f"{len(below)} learner(s) below the {data['support_threshold']:g}% support threshold: "
                        + ", ".join(l["learner_id"] for l in below[:8]) + ("…" if len(below) > 8 else "") + ".")
    today = date.today().isoformat()
    for item in sorted(_open_interventions(data), key=lambda i: i["due"])[:3]:
        overdue = "OVERDUE, " if item["due"] < today else ""
        findings.append(f"Open support #{item['id']} for {item['learner_id']} ({item['topic']}): "
                        f"{overdue}due {item['due']}, status {item['status']}.")
    counts = data.get("attendance_recorded_status_counts") or {}
    if counts:
        findings.append("Recorded attendance statuses: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                        + ". No cause of absence is inferred.")
    return {"findings": findings[:12]}


def intervention_planner(data, goal, outputs):
    """Select from the approved action catalog only, verbatim (grounding rule)."""
    return {"recommendations": action_catalog(data)[:10]}


def reporting_agent(data, goal, outputs, demonstrate_repair=False):
    first_draft = "review_feedback" not in outputs
    hide_coverage = demonstrate_repair and first_draft and data["missing_count"] > 0
    lines = [f"{data['class_name']} ({data['subject']}): marked-record average {_pct(data['mean_percentage'])}."]
    if not hide_coverage:
        lines.append(_coverage_line(data))
    findings = outputs.get("performance_analyst", {}).get("findings", [])
    lines.extend(findings[2:5])
    lines.append("This descriptive average is not an official term or promotion mark.")
    recs = outputs.get("intervention_planner", {}).get("recommendations", [])
    return {"summary": " ".join(lines)[:6000], "mean_percentage": data["mean_percentage"],
            "marked_count": data["marked_count"], "missing_count": data["missing_count"],
            "recommendations": recs[:10]}


def quality_reviewer(data, goal, outputs):
    report = outputs["reporting_agent"]
    issues = []
    for field in ["mean_percentage", "marked_count", "missing_count"]:
        if report[field] != data[field]:
            issues.append(f"{field} does not match the source records.")
    if data["missing_count"] and f"{data['marked_count']} of {data['expected_marks']}" not in report["summary"]:
        issues.append("The summary does not state how many expected records are missing; add the coverage statement.")
    planned = set(outputs.get("intervention_planner", {}).get("recommendations", []))
    if any(rec not in planned for rec in report["recommendations"]):
        issues.append("A recommendation is not backed by the intervention planner's evidence.")
    return {"approved": not issues, "issues": issues[:10],
            "checked_mean_percentage": data["mean_percentage"],
            "checked_marked_count": data["marked_count"], "checked_missing_count": data["missing_count"],
            "checked_assessment_count": data.get("assessment_count")}


STAGES = {"coordinator": coordinator, "performance_analyst": performance_analyst,
          "intervention_planner": intervention_planner, "reporting_agent": reporting_agent,
          "quality_reviewer": quality_reviewer}


async def offline_stage(stage, data, goal, outputs, provider, model, request_id,
                        on_request=None, demonstrate_repair=False):
    """Drop-in replacement for agents.run_stage. No network, no key, no pacing."""
    if on_request:
        await on_request({"type": "model_request", "agent": stage, "provider": "offline",
                          "model": OFFLINE_MODEL, "waited_seconds": 0})
    if stage == "reporting_agent":
        result = reporting_agent(data, goal, outputs, demonstrate_repair)
    else:
        result = STAGES[stage](data, goal, outputs)
    return SCHEMAS[stage].model_validate(result).model_dump()
