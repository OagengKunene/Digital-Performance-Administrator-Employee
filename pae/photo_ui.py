"""Live Streamlit view of the durable marked-script photo workflow."""
import hashlib
import json

import pandas as pd
import streamlit as st


def report_markdown(report):
    lines = ["# Student marked-script report", "", report["summary"], "",
        f"Learner: {report['learner_name']} ({report['learner_id'] or 'ID not read'})",
        f"Assessment: {report['assessment']}", f"Subject: {report['subject']}",
        f"Result: {report['earned']:g}/{report['possible']:g} ({report['mean_percentage']:g}%)", "",
        "## Question results", ""]
    for row in report["questions"]:
        lines.append(f"- {row['question']} — {row['topic']}: {row['earned']:g}/{row['possible']:g} ({row['percentage']:g}%)")
    for title, field in [("Strengths on this script", "strengths"), ("Support areas", "support_areas"),
                         ("Recommended follow-up", "recommendations"), ("Scope and limitations", "limitations")]:
        lines += ["", "## " + title, ""]
        lines += ["- " + text for text in report[field]] or ["No specific finding from the available topic evidence."]
    review = report.get("review", {})
    lines += ["", "## Report review", "", review.get("scope", "Review pending"),
        "Status: " + review.get("status", "pending")]
    return "\n".join(lines) + "\n"


def render_photo_demo(api, cid):
    prefix = f"/classes/{cid}/script-photos"
    st.subheader("Photo to student performance report")
    st.write("Upload a photo of an already marked test. Reading, demo mark capture, report review and inbox delivery start automatically.")
    st.caption("Demo capture: official class marks are unchanged. Local OCR reads clear printed score summaries; configured vision readers can attempt handwriting.")
    runtime = api("GET", "/script-photo-readers")
    available = runtime["available"]
    labels = {"auto": "Automatic — configured vision reader, otherwise local OCR", "local": "Local OCR — no API key",
              "groq": "Groq vision — sends the photo to your configured provider",
              "gemini": "Gemini vision — sends the photo to your configured provider"}
    with st.expander("Photo reader", expanded=False):
        reader = st.selectbox("Read photo with", ["auto"] + [r for r in ("local", "groq", "gemini") if available[r]],
                              format_func=lambda value: labels[value])
        st.caption("Include the learner ID, awarded marks, maximum marks and total in a clear, upright photo. Only visible scores are transcribed; answers are not re-marked.")
    resolved = runtime["automatic_reader"] if reader == "auto" else reader
    if resolved == "local":
        st.caption("Reading engine: local OCR. The photo stays on this application server.")
    elif resolved:
        st.caption("Reading engine: " + resolved.capitalize() + " vision. The photo is sent for reading through your configured " + resolved.capitalize() + " account.")
    else:
        st.warning("The automatic photo reader is unavailable. Run setup or select a configured vision reader.")
    left, right = st.columns([3, 2])
    with left:
        uploaded = st.file_uploader("Drop a marked test photo — processing starts on upload", type=["jpg", "jpeg", "png", "webp"],
                                    key=f"script-photo-upload-{cid}")
        if uploaded:
            raw = uploaded.getvalue()
            if len(raw) > 6_000_000:
                st.error("Use a photo of at most 6 MB.")
                st.stop()
            fingerprint = str(cid) + ":" + reader + ":" + hashlib.sha256(raw).hexdigest()
            if st.session_state.get("last-photo-upload") != fingerprint:
                item = api("POST", prefix, params={"filename": uploaded.name, "reader": reader}, content=raw)
                st.session_state["photo-choice-" + str(cid)] = item["id"]
                st.session_state["last-photo-upload"] = fingerprint
    with right:
        st.write("Try the complete demo")
        st.caption("A fictional marked page is rendered and passed through real local OCR. No pre-filled extraction or report is used.")
        if st.button("Try a sample marked script", disabled=not available["local"]):
            item = api("POST", prefix + "/sample")
            st.session_state["photo-choice-" + str(cid)] = item["id"]
        if not available["local"]:
            st.info("Run scripts/setup.py in Codespaces to install local OCR, or select a configured vision reader for your own photo.")
    submissions = api("GET", prefix)
    if not submissions:
        st.info("Your live capture and student report will appear here after you upload a photo.")
        return
    choices = [s["id"] for s in submissions]
    lookup = {s["id"]: s for s in submissions}
    state_key = "photo-choice-" + str(cid)
    if st.session_state.get(state_key) not in choices:
        st.session_state[state_key] = choices[0]
    identifier = st.selectbox("Photo history", choices, key=state_key,
        format_func=lambda value: lookup[value]["filename"] + " · " + lookup[value]["created_at"])

    @st.fragment(run_every="1s")
    def live_capture():
        item = api("GET", prefix + "/" + identifier)
        status = item["status"]
        steps = {"queued": (0.05, "Photo received"), "reading": (0.20, "Reading awarded marks"),
            "validating": (0.45, "Checking scores and totals"), "reporting": (0.65, "Preparing the student report"),
            "reviewing": (0.85, "Reviewing the report"), "completed": (1.0, "Report reviewed and delivered")}
        progress, label = steps.get(status, (0.45, "Photo needs attention"))
        st.progress(progress, text=label)
        if status == "completed":
            st.success("Completed automatically. The teacher and principal can find this demo report in Reports inbox.")
        elif status in {"requires_review", "failed", "authorisation_blocked"}:
            st.warning("Processing paused. Check the source photo or the transcription below.")
        else:
            st.info("Live processing continues in the background. You can leave this page and return.")
        for issue in item["issues"]:
            st.warning(issue)
        image_key = "script-photo-image-" + identifier
        if image_key not in st.session_state:
            st.session_state[image_key] = api("GET", prefix + "/" + identifier + "/image", binary=True)
        photo, marks = st.columns([2, 3])
        extraction = item.get("extraction", {})
        with photo:
            st.image(st.session_state[image_key], caption="Uploaded marked page", width="stretch")
        with marks:
            st.subheader("Live mark capture")
            st.caption("Reader: " + item["reader"] + " · Photo attempts: " + str(item["attempts"]))
            if extraction:
                st.write("Learner:", extraction.get("learner_name") or extraction.get("learner_id") or "Identity not readable")
                st.write("Assessment:", extraction.get("assessment", "Marked test"))
                rows = extraction.get("questions", [])
                if rows:
                    st.dataframe([{key: row.get(key) for key in ["question", "topic", "earned", "possible", "uncertain"]}
                                  for row in rows], hide_index=True, width="stretch")
                else:
                    st.write("Written total:", extraction.get("total_earned"), "/", extraction.get("total_possible"))
                st.caption("A blank or uncertain score pauses validation; it is never silently counted as zero.")
            else:
                st.write("Waiting for the photo reader. Detected scores appear here as soon as reading completes.")
        if status == "completed":
            report = item["report"]
            st.subheader("Student performance review")
            cards = st.columns(3)
            cards[0].metric("Captured score", f"{report['earned']:g} / {report['possible']:g}")
            cards[1].metric("Script percentage", f"{report['mean_percentage']:g}%")
            cards[2].metric("Overall support threshold", "Below" if report["support_review"] else "At or above")
            st.write(report["summary"])
            st.caption(f"Descriptive strengths threshold: 70%. Support-review threshold for this class: {report['support_threshold']:g}%.")
            if report["topics"]:
                chart = pd.DataFrame(report["topics"]).set_index("topic")
                st.bar_chart(chart[["percentage"]], horizontal=True, color="#3366a1")
            for title, field in [("Strengths on this script", "strengths"), ("Areas needing support", "support_areas"),
                                 ("Recommended follow-up", "recommendations")]:
                st.write("**" + title + "**")
                if report[field]:
                    for text in report[field]:
                        st.write("• " + text)
                else:
                    st.write("No specific finding from the available topic evidence.")
            with st.expander("Report checks and limitations", expanded=True):
                review = report["review"]
                st.write("Arithmetic review:", review["status"], "· Checked percentage:", review["checked_percentage"])
                for limitation in report["limitations"]:
                    st.caption(limitation)
            st.download_button("Download student report", report_markdown(report), file_name="student-script-report.md", mime="text/markdown")
            st.download_button("Download capture and review evidence", json.dumps(item, indent=2), file_name="script-capture.json", mime="application/json")
        elif status in {"requires_review", "failed"}:
            with st.expander("Correct an unclear transcription", expanded=False):
                st.caption("Only needed for an exception. Check the paper, correct the reading and explain the evidence; validation and reporting then resume automatically.")
                with st.form("correct-photo-" + identifier):
                    corrected = st.data_editor(pd.DataFrame(extraction.get("questions", []), columns=[
                        "question", "topic", "earned", "possible", "evidence", "uncertain"]), num_rows="dynamic", hide_index=True)
                    earned = st.text_input("Written total earned (blank if not shown)", value="" if extraction.get("total_earned") is None else str(extraction["total_earned"]))
                    possible = st.text_input("Written total possible (blank if not shown)", value="" if extraction.get("total_possible") is None else str(extraction["total_possible"]))
                    reason = st.text_input("Evidence for the correction", placeholder="Checked Q3 against the original marked script")
                    if st.form_submit_button("Save corrected reading and resume"):
                        try:
                            reading = {**extraction, "questions": corrected.astype(object).where(pd.notnull(corrected), None).to_dict("records"),
                                "total_earned": float(earned) if earned.strip() else None,
                                "total_possible": float(possible) if possible.strip() else None, "issues": []}
                        except ValueError:
                            st.error("Enter numeric totals or leave them blank.")
                        else:
                            api("POST", prefix + "/" + identifier + "/correction", json={"reading": reading, "reason": reason})
                            st.rerun(scope="fragment")
        with st.expander("Live activity and source transcription"):
            st.dataframe(item["events"], hide_index=True)
            if extraction.get("transcript"):
                st.text(extraction["transcript"])

    live_capture()
