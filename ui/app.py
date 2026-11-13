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
    response = httpx.request(method, API + path, headers=headers, timeout=110, trust_env=use_proxy, **kwargs)
    if response.status_code >= 400:
        if response.status_code == 401:
            st.session_state.pop("token", None)
        detail = response.json().get("detail", "Request failed")
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
    view = st.sidebar.radio("Workspace", ["Overview", "Import records", "Learner support", "Agent reports", "Audit history"])
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
    elif view == "Import records":
        if st.session_state.get("role") == "principal":
            roster = st.file_uploader("Learner roster CSV (learner_id,name)", type=["csv"], key="roster")
            if roster and st.button("Import roster"):
                result = api("POST", f"/classes/{cid}/learners", json={"csv": roster.getvalue().decode("utf-8-sig")})
                st.success(f"Added {result['inserted']} learners")
        kind = st.selectbox("Record type", ["marks", "attendance"])
        st.write("Marks columns: learner_id,assessment,topic,earned,possible")
        st.write("Attendance columns: learner_id,day,status")
        st.caption("Blank earned values remain missing. Duplicate uploads are rejected atomically.")
        st.dataframe(api("GET", f"/classes/{cid}/learners"), hide_index=True)
        file = st.file_uploader("CSV file", type=["csv"])
        if file and st.button("Validate and import"):
            result = api("POST", f"/classes/{cid}/imports/{kind}", json={"csv": file.getvalue().decode("utf-8-sig")})
            st.success(f"Imported {result['inserted']} records")
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
        st.subheader("Live ADK workflow")
        st.caption("Requires GOOGLE_API_KEY. Verified metrics do not imply that every recommendation is educationally validated; staff review remains necessary.")
        goal = st.text_area("Task", value="Review this class's performance and prepare a teacher report with evidence-based support recommendations.")
        if st.button("Run agent team"):
            with st.spinner("Agents are analysing, drafting and reviewing..."):
                result = api("POST", f"/classes/{cid}/runs", json={"goal": goal})
            st.json(result)
        for run in api("GET", f"/classes/{cid}/runs"):
            with st.expander(run["at"] + " · " + run["status"]):
                st.json(run["result"])
                st.dataframe(run["events"], hide_index=True)
                st.download_button("Download run", json.dumps(run, indent=2), file_name=run["id"] + ".json", key=run["id"])
    else:
        st.dataframe(api("GET", f"/classes/{cid}/audit"), hide_index=True)
except (httpx.ConnectError, httpx.TimeoutException):
    st.error("The backend is unavailable or timed out. Start it with python scripts/start.py and check the terminal.")
