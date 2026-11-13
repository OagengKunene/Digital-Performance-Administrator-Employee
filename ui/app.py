import json
import os
from urllib.parse import urlsplit
from datetime import date
import httpx
import streamlit as st

st.set_page_config(page_title="Performance Administrator", layout="wide")
API = os.environ.get("PAE_API_URL", "http://127.0.0.1:8000")
st.title("Performance Administrator")
st.caption("Assessment evidence, learner support and reviewed agent reports")


def api(method, path, **kwargs):
    use_proxy = urlsplit(API).hostname not in {"127.0.0.1", "localhost"}
    headers = {}
    token = st.session_state.get("token")
    if token:
        headers["Authorization"] = "Bearer " + token
    timeout = httpx.Timeout(30, connect=10)
    try:
        response = httpx.request(method, API + path, headers=headers, timeout=timeout, trust_env=use_proxy, **kwargs)
    except httpx.TransportError:
        st.error("The application server is unavailable. Keep scripts/start.py running and refresh this page.")
        st.stop()
    if response.status_code >= 400:
        if response.status_code == 401:
            st.session_state.pop("token", None)
        try:
            detail = response.json().get("detail", "Request failed")
        except ValueError:
            detail = f"Application request failed (HTTP {response.status_code}). Check the API terminal."
        st.error(str(detail))
        st.stop()
    return response.json()


try:
    if not st.session_state.get("token"):
        st.subheader("Staff sign in")
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in"):
                response = api("POST", "/auth/login", json={"email": email, "password": password})
                st.session_state.token = response["access_token"]
                st.session_state.role = response["role"]
                st.rerun()
        st.info("For evaluation accounts, run the documented bootstrap command and open .local/bootstrap-login.txt in your private Codespace.")
        st.stop()
    st.sidebar.write("Signed in as " + st.session_state.get("role", "staff"))
    if st.sidebar.button("Sign out"):
        api("POST", "/auth/logout")
        st.session_state.clear()
        st.rerun()
    if st.session_state.get("role") == "principal":
        with st.sidebar.expander("School setup"):
            st.caption("Create staff and classes here. Import learner rosters in the workspace.")
            with st.form("new-staff"):
                staff_email = st.text_input("Staff email")
                staff_password = st.text_input("Initial staff password", type="password")
                if st.form_submit_button("Create teacher"):
                    api("POST", "/staff", json={"email": staff_email, "password": staff_password, "role": "teacher"})
                    st.success("Teacher created")
            teachers = [u for u in api("GET", "/staff") if u["role"] == "teacher"]
            if teachers:
                with st.form("new-class"):
                    class_name = st.text_input("Class name")
                    subject = st.text_input("Subject")
                    teacher = st.selectbox("Teacher", teachers, format_func=lambda u: u["email"])
                    threshold = st.number_input("Support review threshold (%)", min_value=0.0, max_value=100.0, value=50.0)
                    if st.form_submit_button("Create class"):
                        api("POST", "/classes", json={"name": class_name, "subject": subject, "teacher_id": teacher["id"], "support_threshold": threshold})
                        st.rerun()
    classes = api("GET", "/classes")
    if not classes:
        st.info("No classes assigned to this account.")
        st.stop()
    selected = st.sidebar.selectbox("Class", classes, format_func=lambda c: c["name"] + " · " + c["subject"])
    cid = selected["id"]
    data = api("GET", f"/classes/{cid}/evidence")
    view = st.sidebar.radio("Workspace", ["Overview", "Reports inbox", "Assessment sources", "Import records", "Learner support", "Agent reports", "Autonomous monitoring", "Audit history"])
    if view == "Overview":
        st.subheader(selected["name"] + " · " + selected["subject"])
        cols = st.columns(3)
        cols[0].metric("Marked-record average", "No marks" if data["mean_percentage"] is None else str(data["mean_percentage"]) + "%")
        cols[1].metric("Marked / expected topic records", f"{data['marked_count']} / {data['expected_marks']}")
        cols[2].metric("Missing topic marks", data["missing_count"])
        st.dataframe(data["topics"], width="stretch", hide_index=True)
        st.subheader("Learner evidence")
        st.dataframe(data["learners"], width="stretch", hide_index=True)
        st.write("Recorded attendance statuses", data["attendance_recorded_status_counts"])
        with st.expander("Metric definitions"):
            st.json(data["definitions"])
        st.download_button("Download evidence JSON", json.dumps(data, indent=2), file_name=f"class-{cid}-evidence.json", mime="application/json")
    elif view == "Reports inbox":
        st.subheader("Automatically delivered reports")
        st.caption("Assigned teachers and school principals receive capture reports here automatically. Data reports arrive independently of AI availability.")
        preference = api("GET", "/notification-preferences")
        with st.expander("Email delivery settings"):
            st.write("Registered staff address:", preference["registered_email"])
            st.write("Email service configured:", preference["smtp_configured"])
            if preference["usable_address"]:
                with st.form("email-delivery"):
                    enabled = st.checkbox("Also email future reports", value=preference["email_enabled"])
                    if st.form_submit_button("Save email preference"):
                        api("PUT", "/notification-preferences", json={"email_enabled": enabled})
                        st.success("Preference saved for future reports")
            else:
                st.info("The fictional sample address receives in-app reports. Create a staff account with its real address for email delivery.")
        @st.fragment(run_every="5s")
        def inbox():
            messages = api("GET", "/notifications")
            if not messages:
                st.info("Reports appear automatically after the next accepted marks or attendance capture.")
            for message in messages:
                with st.expander(message["created_at"] + " · " + message["title"], expanded=False):
                    st.caption("In-app: delivered · Email: " + message["email_status"])
                    if message["email_last_error"]:
                        st.caption(message["email_last_error"])
                    payload = message["payload"]
                    report = payload.get("report", {})
                    st.write(report.get("summary", ""))
                    st.caption(payload.get("scope", ""))
                    metrics = {key: report[key] for key in ["mean_percentage", "assessment_count", "marked_count", "missing_count"] if key in report}
                    if metrics:
                        st.dataframe([metrics], hide_index=True)
                    for action in report.get("recommendations", report.get("actions", [])):
                        st.write("• " + action)
                    if report.get("review"):
                        st.caption("Agent review completed; source metrics checked. Staff can inspect or override the report.")
                    st.download_button("Download report", json.dumps(payload, indent=2),
                        file_name="report-" + message["id"] + ".json", key="download-" + message["id"])
                    if not message["read_at"] and st.button("Mark read", key="read-" + message["id"]):
                        api("POST", "/notifications/" + message["id"] + "/read")
                        st.rerun(scope="fragment")
        inbox()
    elif view == "Assessment sources":
        st.subheader("Automatic assessment capture")
        st.caption("Connect an existing marks/attendance CSV export once. New source records are validated, captured, evaluated and routed without a Run button. Scripts and handwritten work require a separate marking integration.")
        with st.expander("Connect a source"):
            with st.form("new-source"):
                name = st.text_input("Source name", value="School marks export")
                kind = st.selectbox("Source record type", ["marks", "attendance"])
                corrections = st.checkbox("Allow this source to correct already captured values", value=False)
                if st.form_submit_button("Connect source"):
                    result = api("POST", f"/classes/{cid}/sources", json={"name": name, "kind": kind, "allow_corrections": corrections})
                    st.session_state["new_ingestion_key"] = {"id": result["id"], "key": result.pop("ingestion_key")}
                    st.success("Source connected. Configure its export destination once below.")
        sources = api("GET", f"/classes/{cid}/sources")
        for source in sources:
            with st.expander(source["name"] + " · " + source["kind"], expanded=True):
                st.write("Server inbox directory (write completed CSV files here):")
                st.code(source["inbox_folder"], language=None)
                st.write("Or configure the export system to POST UTF-8 CSV to:")
                st.code(API + source["upload_endpoint"], language=None)
                st.caption("Authenticate the export with the X-Ingestion-Key header. Use the deployed API address when the school system runs elsewhere.")
                key = st.session_state.get("new_ingestion_key", {})
                if key.get("id") == source["id"]:
                    st.text_input("Source key — save privately; shown only in this session", value=key["key"], type="password", key="key-" + source["id"])
                if st.button("Rotate source key", key="rotate-" + source["id"]):
                    result = api("POST", f"/classes/{cid}/sources/{source['id']}/rotate-key")
                    st.session_state["new_ingestion_key"] = {"id": source["id"], "key": result["ingestion_key"]}
                    st.rerun()
                with st.form("configure-source-" + source["id"]):
                    enabled = st.checkbox("Source enabled", value=source["enabled"])
                    corrections = st.checkbox("Permit audited corrections from this source", value=source["allow_corrections"])
                    if st.form_submit_button("Save source settings"):
                        api("PUT", f"/classes/{cid}/sources/{source['id']}", json={"enabled": enabled, "allow_corrections": corrections})
                        st.rerun()
        @st.fragment(run_every="5s")
        def source_progress():
            st.subheader("Source processing")
            batches = api("GET", f"/classes/{cid}/ingestion-batches")
            for batch in batches:
                with st.expander(batch["filename"] + " · " + batch["status"]):
                    st.json(batch["result"])
            st.subheader("Automatic evaluations")
            st.dataframe(api("GET", f"/classes/{cid}/captures"), hide_index=True)
        source_progress()
    elif view == "Import records":
        if st.session_state.get("role") == "principal":
            roster = st.file_uploader("Learner roster CSV (learner_id,name)", type=["csv"], key="roster")
            if roster and st.button("Import roster"):
                result = api("POST", f"/classes/{cid}/learners", json={"csv": roster.getvalue().decode("utf-8-sig")})
                st.success(f"Added {result['inserted']} learners")
        kind = st.selectbox("Record type", ["marks", "attendance"])
        st.write("Marks columns: learner_id,assessment,topic,earned,possible")
        st.write("Attendance columns: learner_id,day,status")
        st.caption("Every accepted change automatically delivers a data report and queues the agent evaluation. No task prompt is needed. Blank earned values remain missing.")
        st.dataframe(api("GET", f"/classes/{cid}/learners"), hide_index=True)
        file = st.file_uploader("CSV file", type=["csv"])
        if file and st.button("Validate and import"):
            result = api("POST", f"/classes/{cid}/imports/{kind}", json={"csv": file.getvalue().decode("utf-8-sig")})
            st.success(f"Imported {result['inserted']} records. The capture report was delivered to staff inboxes; agent evaluation follows automatically.")
        with st.expander("Correct an existing mark"):
            records = api("GET", f"/classes/{cid}/marks")
            if records:
                with st.form("correct-mark"):
                    mark = st.selectbox("Record", records, format_func=lambda m: f"{m['learner_id']} / {m['assessment']} / {m['topic']} / {m['earned']}")
                    blank = st.checkbox("Mark remains missing", value=mark["earned"] is None)
                    score = st.number_input("Correct score", min_value=0.0, max_value=float(mark["possible"]), value=float(mark["earned"] or 0))
                    reason = st.text_input("Reason (at least 10 characters)")
                    if st.form_submit_button("Save audited correction"):
                        api("PATCH", f"/classes/{cid}/marks/{mark['id']}", json={"earned": None if blank else score, "reason": reason})
                        st.success("Correction saved with an audit record")
    elif view == "Learner support":
        st.subheader("Assigned interventions")
        st.dataframe(data["interventions"], hide_index=True)
        learners = api("GET", f"/classes/{cid}/learners")
        with st.form("support"):
            learner = st.selectbox("Learner", learners, format_func=lambda l: l["name"])
            topic = st.text_input("Topic")
            activity = st.text_area("Support activity")
            due = st.date_input("Due date", value=date.today())
            if st.form_submit_button("Assign support"):
                api("POST", f"/classes/{cid}/interventions", json={"learner_id": learner["id"], "topic": topic, "activity": activity, "due": due.isoformat()})
                st.rerun()
        if data["interventions"]:
            with st.form("follow-up"):
                item = st.selectbox("Update intervention", data["interventions"], format_func=lambda i: f"{i['id']}: {i['topic']} ({i['learner_id']})")
                status = st.selectbox("Status", ["assigned", "in_progress", "completed", "requires_review"])
                follow = st.text_area("Completion evidence or follow-up outcome")
                if st.form_submit_button("Save follow-up"):
                    api("PATCH", f"/classes/{cid}/interventions/{item['id']}", json={"status": status, "follow_up": follow})
                    st.rerun()
    elif view == "Agent reports":
        st.subheader("Digital employee reports")
        runtime = api("GET", "/agent-runtime")
        st.caption("The coordinator plans work and delegates to ADK specialists. Completed stages are saved. Numerical verification does not certify every narrative claim; staff review remains necessary.")
        st.write("Selected provider:", runtime["provider"], "· Model:", runtime["model"])
        if not runtime["configured"]:
            st.warning("Add the selected provider's API key through Codespaces secrets, then restart the Codespace.")
        if runtime["worker_last_seen"] is None:
            st.info("No worker heartbeat yet. Start all services with python scripts/start.py.")
        goal = st.text_area("Task", value="Review this class's performance and prepare a teacher report with evidence-based support recommendations.")
        if st.button("Run agent team", disabled=not runtime["configured"]):
            queued = api("POST", f"/classes/{cid}/runs", json={"goal": goal})
            st.success("Report queued. You can leave this page; the worker saves progress.")
        st.download_button("Download statistical report (no AI interpretation)",
            json.dumps(api("GET", f"/classes/{cid}/statistics"), indent=2),
            file_name=f"class-{cid}-statistics.json", mime="application/json")

        @st.fragment(run_every=3)
        def show_runs():
            runs = api("GET", f"/classes/{cid}/runs")
            if not runs:
                st.info("No reports yet.")
            for run in runs:
                progress = run.get("progress", {})
                active = run["status"] in {"queued", "running", "retrying"}
                with st.expander(run["at"] + " · " + run["status"], expanded=active):
                    st.write("Status:", run["status"])
                    if progress:
                        st.write("Current stage:", progress["active_stage"] or "finished")
                        st.write("Saved stages:", ", ".join(progress["completed_stages"]) or "none")
                        st.write("Dispatched attempts:", progress["request_attempts"], "/", progress["request_budget"])
                        if run["status"] == "retrying":
                            st.info("Only the failed stage is waiting for its next attempt.")
                    st.json(run["result"])
                    if progress.get("completed_outputs"):
                        with st.expander("Saved intermediate work — drafts for staff review"):
                            st.json(progress["completed_outputs"])
                    if progress.get("resumable"):
                        choices = [name for name, available in runtime["available_providers"].items() if available]
                        if choices:
                            provider = st.selectbox("Provider for unfinished stage", choices,
                                key="resume-provider-" + run["id"])
                            if st.button("Resume from saved progress", key="resume-" + run["id"]):
                                api("POST", f"/classes/{cid}/runs/{run['id']}/resume", json={"provider": provider})
                                st.rerun()
                    if active and st.button("Cancel further processing", key="cancel-" + run["id"]):
                        api("POST", f"/classes/{cid}/runs/{run['id']}/cancel")
                        st.rerun()
                    if run["events"]:
                        with st.expander("Activity"):
                            st.dataframe(run["events"], hide_index=True)
                    st.download_button("Download run", json.dumps(run, indent=2),
                        file_name=run["id"] + ".json", key=run["id"])
        show_runs()
    elif view == "Autonomous monitoring":
        st.subheader("Autonomous monitoring and follow-up")
        st.caption("While the worker is running, enabled policies check records on schedule and after configured record changes. Follow-ups are assigned in this application; no email or SMS delivery is implied.")
        policy = api("GET", f"/classes/{cid}/monitoring")
        if st.session_state.get("role") == "principal":
            with st.form("monitor-policy"):
                enabled = st.checkbox("Enable autonomous record monitoring", value=policy["enabled"])
                use_ai = st.checkbox("Automatically initiate AI investigations when evidence changes", value=policy["use_ai"])
                on_change = st.checkbox("Check after record changes", value=policy["on_record_change"])
                minutes = st.number_input("Scheduled check interval (minutes)", min_value=15, max_value=10080, value=policy["interval_minutes"])
                daily = st.number_input("Maximum automatic AI reports for this class per UTC day", min_value=1, max_value=5, value=policy["daily_ai_limit"])
                st.caption("Record checks and in-app follow-ups work without a model key. Automatic AI reports consume provider allowance and also obey the installation-wide daily cap.")
                if st.form_submit_button("Save monitoring policy"):
                    api("PUT", f"/classes/{cid}/monitoring", json={"enabled": enabled,
                        "use_ai": use_ai, "on_record_change": on_change,
                        "interval_minutes": minutes, "daily_ai_limit": daily})
                    st.success("Monitoring policy saved. The worker will apply it.")
        else:
            st.write("Monitoring enabled:", policy["enabled"])
            st.write("Automatic AI investigations:", policy["use_ai"])

        @st.fragment(run_every=5)
        def show_followups():
            items = api("GET", f"/classes/{cid}/follow-ups")
            st.subheader("Assigned follow-ups")
            if not items:
                st.info("No follow-ups recorded. Enabled monitoring creates them when evidence needs attention.")
            for item in items:
                label = item["issue_key"] + " · " + item["status"]
                if item["escalated_to_principal"]:
                    label += " · escalated to principal"
                with st.expander(label):
                    st.json(item["detail"])
                    if item["outcome"]:
                        st.write(item["outcome"])
                    with st.form("follow-up-" + str(item["id"])):
                        status = st.selectbox("Outcome status", ["acknowledged", "resolved"])
                        outcome = st.text_area("Action taken or resolution evidence (at least 10 characters)")
                        if st.form_submit_button("Save outcome"):
                            api("PATCH", f"/classes/{cid}/follow-ups/{item['id']}",
                                json={"status": status, "outcome": outcome})
                            st.rerun()
        show_followups()
    else:
        st.dataframe(api("GET", f"/classes/{cid}/audit"), hide_index=True)
except (httpx.ConnectError, httpx.TimeoutException):
    st.error("The backend is unavailable or timed out. Start it with python scripts/start.py and check the terminal.")
