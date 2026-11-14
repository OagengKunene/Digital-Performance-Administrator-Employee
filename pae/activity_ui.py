"""Shared live plans/actions/checks/corrections view for every execution path."""
import json
import streamlit as st
from .activity import AGENTS


def render_activity(events, *, work_plan=None, expected_agents=None, status=None):
    st.subheader("Plan · Act · Check · Correct")
    st.caption("Saved plans, tool actions, observations and verification results from actual execution.")
    if not events and status in {"completed", "verified_metrics", "failed", "requires_review"}:
        st.info("This historical execution has no recorded step summaries. Its saved result remains available; start new work to see live activity.")
        return
    cards = st.columns(4)
    for column, title, phases in zip(cards, ("Plans", "Actions", "Checks", "Corrections"),
                                     ({"plan"}, {"act"}, {"check"}, {"correct"})):
        column.metric(title, sum(row["phase"] in phases for row in events))
    if work_plan:
        with st.expander("Coordinator's work plan", expanded=True):
            if work_plan.get("rationale"):
                st.write(work_plan["rationale"])
            if work_plan.get("goal"):
                st.write(work_plan["goal"])
            for index, step in enumerate(work_plan.get("steps", []), 1):
                st.write(f"{index}. {step}")
            if work_plan.get("repair_policy"):
                st.caption(work_plan["repair_policy"])
    latest = {row["agent"]: row for row in events}
    agents = list(dict.fromkeys([*(expected_agents or []), *latest]))
    if agents:
        rows = []
        for name in agents:
            last = latest.get(name, {})
            skipped = (name == "performance_analyst" and work_plan and work_plan.get("analyse_performance") is False) or (
                name == "intervention_planner" and work_plan and work_plan.get("plan_support") is False) or (
                name == "correction_agent" and work_plan and work_plan.get("correction_required") is False)
            rows.append({"Agent": AGENTS.get(name, name), "Latest step": "skipped by coordinator" if skipped else last.get("phase", "waiting"),
                "Action / result": "Not required by the current evidence plan." if skipped else last.get("message", "Waiting for delegation"),
                "Tool": last.get("tool", "")})
        st.dataframe(rows, hide_index=True, width="stretch")
    if not events:
        st.info("Waiting for the worker to begin. New execution summaries appear here automatically.")
        return
    st.dataframe([{"Time": row.get("at", ""), "Agent": AGENTS.get(row["agent"], row["agent"]),
                   "Step": row["phase"], "Activity": row["message"], "Tool": row.get("tool", "")}
                  for row in events], hide_index=True, width="stretch")
    with st.expander("Evidence behind the latest actions and checks"):
        for row in events[-8:]:
            if row.get("evidence"):
                st.write(AGENTS.get(row["agent"], row["agent"]) + " · " + row["phase"])
                st.json(row["evidence"])


def render_photo_correction(item):
    if item.get("demonstrate_repair"):
        st.info("Controlled demonstration: the first transcription deliberately contains one error. The validator must catch it; correction re-reads the actual photo before another check.")
    work = item.get("report", {}).get("workflow", {})
    readings = work.get("readings", [])
    if len(readings) > 1:
        st.subheader("Correction evidence")
        history = []
        for record in readings:
            data = record.get("extraction", {})
            rows = data.get("questions", [])
            known = bool(rows) and all(row.get("earned") is not None for row in rows)
            history.append({"Attempt": str(record.get("attempt", "staff")), "Strategy": record.get("strategy", record.get("source", "")),
                "Transcribed sum": sum(row["earned"] for row in rows) if known else None,
                "Written total": data.get("total_earned"),
                "Check result": "; ".join(record.get("validation_issues", [])) or "No validation issues recorded"})
        st.dataframe(history, hide_index=True, width="stretch")
        before = {q["question"]: q for q in readings[0].get("extraction", {}).get("questions", [])}
        after = {q["question"]: q for q in readings[-1].get("extraction", {}).get("questions", [])}
        difference = [{"Question": key, "Initial reading": before.get(key, {}).get("earned"),
            "Latest reading": after.get(key, {}).get("earned"),
            "Changed": before.get(key, {}).get("earned") != after.get(key, {}).get("earned")}
            for key in dict.fromkeys([*before, *after])]
        if difference:
            st.dataframe(difference, hide_index=True, width="stretch")


def render_photo_team(item):
    st.write("**Photo team status:** " + item["status"])
    st.caption("Execution: deterministic coordinator/checking/report tools · Photo reader: " + item["reader"])
    render_photo_correction(item)
    render_activity(item.get("activity", []), work_plan=item.get("work_plan"), status=item["status"], expected_agents=[
        "coordinator", "photo_reader", "validator", "correction_agent", "performance_analyst",
        "intervention_planner", "reporting_agent", "quality_reviewer", "delivery"])
    if item["status"] == "completed":
        st.success(item["report"]["summary"])
        st.write("Reviewed percentage:", item["report"]["review"]["checked_percentage"])
    elif item.get("issues"):
        for issue in item["issues"]:
            st.warning(issue)


def render_class_team(run):
    progress = run.get("progress", {})
    st.write("**Class team status:** " + run["status"])
    st.caption("Execution engine: " + progress.get("provider", "historical"))
    if progress.get("demonstrate_repair"):
        st.caption("Controlled report demonstration: when records are missing, the initial draft deliberately omits the coverage statement so the reviewer can reject and repair it.")
    rejected = [row["evidence"] for row in run.get("activity", []) if row.get("evidence", {}).get("rejected_report")]
    if rejected:
        st.subheader("Rejected draft and replacement")
        before, after = st.columns(2)
        before.write("**Rejected draft**")
        before.write(rejected[-1]["rejected_report"].get("summary", ""))
        for issue in rejected[-1].get("issues", []):
            before.warning(issue)
        current = run["result"] if run["status"] == "verified_metrics" else progress.get("completed_outputs", {}).get("reporting_agent", {})
        after.write("**Latest draft**")
        after.write(current.get("summary", "Waiting for the reporting agent's replacement draft."))
    render_activity(run.get("activity", []), work_plan=progress.get("completed_outputs", {}).get("coordinator"), status=run["status"],
        expected_agents=["coordinator", "performance_analyst", "intervention_planner", "reporting_agent", "quality_reviewer"])
    if run["status"] == "verified_metrics":
        st.success(run["result"]["summary"])


def render_agent_workspace(api, cid):
    st.subheader("Live colleague activity")
    st.write("Watch the agents plan work, act on evidence, check outputs and correct rejected work before delivery.")
    runtime = api("GET", "/script-photo-readers")
    choices = [name for name in ("local", "groq", "gemini") if runtime["available"].get(name)]
    if choices:
        reader = st.selectbox("Demo photo reader", choices,
            format_func=lambda value: "Local OCR — no API key" if value == "local" else value.capitalize() + " vision — configured account")
        st.caption("The full demo uses a fictional marked photo and a clearly labelled seeded transcription error. The class team uses offline rules on the current class evidence.")
        if st.button("Run the complete colleague demo"):
            result = api("POST", f"/classes/{cid}/colleague-demo", params={"reader": reader})
            st.session_state["colleague-demo-" + str(cid)] = {"photo": result["photo"]["id"], "run": result["run"]["id"]}
            st.success("Both teams queued. The activity below refreshes automatically.")
            if result["class_report_reused"]:
                st.info("A class report was already active; its existing work is shown alongside the new photo demonstration.")
    else:
        st.info("Install the local photo reader with scripts/setup.py, or configure a vision reader. Existing class agent activity is still available below.")

    @st.fragment(run_every="1s")
    def live():
        selected = st.session_state.get("colleague-demo-" + str(cid))
        if selected:
            photo, report = st.tabs(["Photo agent team", "Class agent team"])
            with photo:
                render_photo_team(api("GET", f"/classes/{cid}/script-photos/" + selected["photo"]))
            with report:
                render_class_team(api("GET", f"/classes/{cid}/runs/" + selected["run"]))
        jobs = api("GET", f"/classes/{cid}/agent-activity")
        with st.expander("All saved agent work", expanded=not bool(selected)):
            if not jobs:
                st.info("Uploads, automatic captures and manual investigations will appear here.")
                return
            lookup = {job["kind"] + ":" + job["id"]: job for job in jobs}
            selected_id = st.selectbox("Saved execution", list(lookup), key="activity-job-" + str(cid),
                format_func=lambda key: lookup[key]["label"] + " · " + lookup[key]["at"])
            choice = lookup[selected_id]
            path = "script-photos" if choice["kind"] == "script_photo" else "runs"
            item = api("GET", f"/classes/{cid}/{path}/" + choice["id"])
            if choice["kind"] == "script_photo":
                render_photo_team(item)
            else:
                render_class_team(item)
            st.download_button("Download execution evidence", json.dumps(item, indent=2), file_name="agent-execution.json", mime="application/json")
    live()
