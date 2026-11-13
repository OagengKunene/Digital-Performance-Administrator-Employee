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
