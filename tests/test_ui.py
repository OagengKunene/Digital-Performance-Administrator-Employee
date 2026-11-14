from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def backend(client, monkeypatch):
    """Exercise the real HTTP/1.1 transport, including header validation."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            requests.append((self.path, self.headers.get("Authorization")))
            length = int(self.headers.get("Content-Length", 0))
            response = client.request(
                self.command, self.path,
                headers=dict(self.headers),
                content=self.rfile.read(length) if length else None,
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_PATCH = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    monkeypatch.setenv("PAE_API_URL", f"http://127.0.0.1:{server.server_port}")
    thread.start()
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def sign_in(email, password):
    page = AppTest.from_file(Path(__file__).resolve().parents[1] / "ui/app.py", default_timeout=20).run()
    assert not page.exception
    page.text_input[0].input(email)
    page.text_input[1].input(password)
    page.button[0].click().run()
    return page


def assert_authenticated_requests(requests):
    assert requests[0] == ("/auth/login", None)
    assert len(requests) > 1
    assert all(header and header.startswith("Bearer ") and header.removeprefix("Bearer ")
               for _, header in requests[1:])


def test_staff_sign_in_and_overview(backend):
    page = sign_in("teacher@example.test", "a-long-test-password")
    assert not page.exception
    assert page.metric[0].value == "75.0%"
    assert page.metric[1].value == "3 / 4"
    assert page.session_state["role"] == "teacher"
    assert len(page.sidebar.selectbox[0].options) == 1
    assert_authenticated_requests(backend)


def test_principal_sees_school_setup_and_both_classes(backend):
    page = sign_in("principal@example.test", "a-long-test-password")
    assert not page.exception
    assert page.session_state["role"] == "principal"
    assert page.sidebar.expander[0].label == "School setup"
    assert len(page.sidebar.selectbox[-1].options) == 2
    assert_authenticated_requests(backend)


def test_wrong_password_shows_error_without_transport_exception(backend):
    page = sign_in("principal@example.test", "wrong-password")
    assert not page.exception
    assert page.error
    assert "token" not in page.session_state or not page.session_state["token"]
    assert backend == [("/auth/login", None)]


def test_queue_returns_immediately_and_shows_saved_progress(backend, ai_config):
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Agent reports").run()
    next(button for button in page.button if button.label == "Run agent team").click().run()
    assert not page.exception
    assert any("queued" in item.value.lower() for item in page.success)
    assert any("queued" in item.label for item in page.expander)


def test_principal_can_enable_non_ai_monitoring(backend):
    page = sign_in("principal@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Autonomous monitoring").run()
    assert not page.exception
    page.checkbox[0].check()
    next(button for button in page.button if button.label == "Save monitoring policy").click().run()
    assert not page.exception
    assert page.success


def test_staff_receives_capture_report_in_new_inbox(backend, client, teacher):
    response = client.patch("/classes/1/marks/4", headers=teacher,
        json={"earned": 15, "reason": "Confirmed using the marked original script"})
    assert response.status_code == 200
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Reports inbox").run()
    assert not page.exception
    assert any("Performance evaluation" in item.label for item in page.expander)
    assert any("75.0" in frame.value.to_string() for frame in page.dataframe)


def test_staff_connects_an_automatic_capture_source(backend):
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Assessment sources").run()
    assert not page.exception
    next(button for button in page.button if button.label == "Connect source").click().run()
    assert not page.exception
    assert page.success
    assert any("School marks export" in item.label for item in page.expander)
    assert any("Source key" in item.label and item.proto.type == 1 for item in page.text_input)
    assert any("/ingestion/" in item.value for item in page.code)


def test_offline_engine_and_demonstration_from_the_interface(backend, monkeypatch):
    from pae.config import settings
    for name, value in {"google_api_key": "", "groq_api_key": "", "ai_provider": "offline"}.items():
        monkeypatch.setattr(settings(), name, value)
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Agent reports").run()
    engine = next(box for box in page.selectbox if box.label == "Engine")
    assert engine.value == "offline"
    next(box for box in page.checkbox if box.label.startswith("Demonstrate self-correction")).check().run()
    next(button for button in page.button if button.label == "Run agent team").click().run()
    assert not page.exception
    assert any("queued" in item.value.lower() for item in page.success)


def test_photo_demo_runs_sample_without_task_or_agent_button(backend, db_factory):
    import shutil
    from pae.script_photos import process_photo
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract required for real photo demo")
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Photo script demo").run()
    assert not page.exception
    assert not any(button.label == "Run agent team" for button in page.button)
    next(button for button in page.button if button.label == "Try a sample marked script").click().run()
    assert not page.exception
    for _ in range(5):
        if not process_photo(db_factory):
            break
    page.run()
    assert not page.exception
    assert any(metric.value == "62.5%" for metric in page.metric)
    assert any("Completed automatically" in message.value for message in page.success)
    assert any(button.label == "Download student report" for button in page.get("download_button"))


def test_photo_upload_starts_automatically_and_refresh_does_not_resubmit(backend, db_factory, monkeypatch):
    import shutil
    from types import SimpleNamespace
    from sqlalchemy import select, func
    from pae.db import ScriptPhoto
    from pae.script_photos import process_photo, sample_script
    import streamlit as st
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract required")
    image = sample_script("L1")
    uploaded = SimpleNamespace(name="student-script.png", getvalue=lambda: image)
    # AppTest has no file chooser driver. Inject only its uploaded-file result;
    # the page, HTTP upload, validation, actual OCR and report remain real.
    original = st.file_uploader
    monkeypatch.setattr(st, "file_uploader", lambda label, *args, **kwargs:
        uploaded if label.startswith("Drop a marked") else original(label, *args, **kwargs))
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Photo script demo").run()
    assert not page.exception
    page.run()
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(ScriptPhoto)) == 1
    for _ in range(5):
        if not process_photo(db_factory): break
    page.run()
    assert not page.exception
    assert any(metric.value == "62.5%" for metric in page.metric)


def test_complete_colleague_demo_shows_both_teams_and_correction_evidence(backend, db_factory):
    import asyncio
    import shutil
    from pae.script_photos import process_photo
    from pae.jobs import process_one
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract required for real demo")
    page = sign_in("teacher@example.test", "a-long-test-password")
    page.sidebar.radio[0].set_value("Agent activity").run()
    next(button for button in page.button if button.label == "Run the complete colleague demo").click().run()
    assert not page.exception
    assert any("Both teams queued" in item.value for item in page.success)
    for _ in range(12):
        photo = process_photo(db_factory)
        report = asyncio.run(process_one(db_factory))
        if not photo and not report:
            break
    page.run()
    assert not page.exception
    assert any("62.5%" in item.value for item in page.success)
    assert any("Controlled demonstration" in item.value for item in page.info)
    assert any("Correction evidence" in item.value for item in page.subheader)
    assert any("Rejected draft and replacement" in item.value for item in page.subheader)
    assert any(frame.value.astype(str).apply(lambda col: col.str.contains("Correction agent")).any().any()
               for frame in page.dataframe)
