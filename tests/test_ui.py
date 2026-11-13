from urllib.parse import urlsplit
from pathlib import Path
import httpx
from streamlit.testing.v1 import AppTest


def test_staff_sign_in_and_overview(client, monkeypatch):
    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("trust_env", None)
        return client.request(method, urlsplit(url).path, **kwargs)
    monkeypatch.setattr(httpx, "request", request)
    page = AppTest.from_file(Path(__file__).resolve().parents[1] / "ui/app.py", default_timeout=20).run()
    assert not page.exception
    page.text_input[0].input("teacher@example.test")
    page.text_input[1].input("a-long-test-password")
    page.button[0].click().run()
    assert not page.exception
    assert page.metric[0].value == "75.0%"
    assert page.metric[1].value == "3 / 4"
    assert page.session_state["role"] == "teacher"
    assert len(page.sidebar.selectbox[0].options) == 1


def test_principal_sees_school_setup_and_both_classes(client, monkeypatch):
    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("trust_env", None)
        return client.request(method, urlsplit(url).path, **kwargs)
    monkeypatch.setattr(httpx, "request", request)
    page = AppTest.from_file(Path(__file__).resolve().parents[1] / "ui/app.py", default_timeout=20).run()
    page.text_input[0].input("principal@example.test")
    page.text_input[1].input("a-long-test-password")
    page.button[0].click().run()
    assert not page.exception
    assert page.session_state["role"] == "principal"
    assert page.sidebar.expander[0].label == "School setup"
    assert len(page.sidebar.selectbox[-1].options) == 2
