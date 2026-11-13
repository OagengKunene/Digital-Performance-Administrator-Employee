"""Change A: efficiency and security hardening.

Each test pins one behaviour so a future change cannot quietly undo it.
"""
import time
from sqlalchemy import event, select
from pae import api
from pae.db import LoginSession
from pae.security import token_hash


def count_selects(client):
    """Count SELECT statements sent to the test database while a block runs."""
    engine = client.app.dependency_overrides[api.get_db]().__next__().get_bind()
    counter = {"n": 0}

    def before(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1
    event.listen(engine, "before_cursor_execute", before)
    return counter, lambda: event.remove(engine, "before_cursor_execute", before)


def test_marks_import_uses_constant_queries(client, teacher):
    # 4 learners x 50 topics = 200 rows. Before change A this ran ~200 extra SELECTs.
    rows = [f"L{l},Big test,Topic {t},10,20" for t in range(50) for l in range(1, 5)]
    csv = "learner_id,assessment,topic,earned,possible\n" + "\n".join(rows) + "\n"
    counter, stop = count_selects(client)
    try:
        assert client.post("/classes/1/imports/marks", headers=teacher, json={"csv": csv}).status_code == 200
    finally:
        stop()
    assert counter["n"] < 15, f"import ran {counter['n']} SELECTs; it must not grow with row count"


def test_attendance_import_uses_constant_queries(client, teacher):
    rows = [f"L{l},2026-10-{d:02d},present" for d in range(1, 29) for l in range(1, 5)]
    csv = "learner_id,day,status\n" + "\n".join(rows) + "\n"
    counter, stop = count_selects(client)
    try:
        assert client.post("/classes/1/imports/attendance", headers=teacher, json={"csv": csv}).status_code == 200
    finally:
        stop()
    assert counter["n"] < 15


def test_existing_records_still_block_duplicates_after_preload(client, teacher):
    marks = "learner_id,assessment,topic,earned,possible\nL1,Test 1,Algebra,5,20\n"  # L1 already has this mark
    assert client.post("/classes/1/imports/marks", headers=teacher, json={"csv": marks}).status_code == 422
    other_max = "learner_id,assessment,topic,earned,possible\nL4,Test 1,Algebra,5,30\n"  # max must stay 20
    assert client.post("/classes/1/imports/marks", headers=teacher, json={"csv": other_max}).status_code == 422
    att = "learner_id,day,status\nL1,2026-11-13,present\n"
    assert client.post("/classes/1/imports/attendance", headers=teacher, json={"csv": att}).status_code == 200
    assert client.post("/classes/1/imports/attendance", headers=teacher, json={"csv": att}).status_code == 422


def test_unknown_email_runs_a_full_password_check(client, monkeypatch):
    calls = []
    real = api.verify_password
    monkeypatch.setattr(api, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    r = client.post("/auth/login", json={"email": "nobody@example.test", "password": "whatever-password"})
    assert r.status_code == 401
    assert calls == [api.DUMMY_HASH], "unknown emails must cost the same as wrong passwords"


def test_expired_sessions_are_deleted(client, teacher):
    session = client.app.dependency_overrides[api.get_db]().__next__()
    session.add(LoginSession(token_hash=token_hash("old-token"), user_id=1, expires=time.time() - 10))
    session.commit()
    assert client.get("/classes", headers={"Authorization": "Bearer old-token"}).status_code == 401
    assert session.get(LoginSession, token_hash("old-token")) is None
    session.add(LoginSession(token_hash=token_hash("old-2"), user_id=1, expires=time.time() - 10))
    session.commit()
    assert client.post("/auth/login", json={"email": "teacher@example.test", "password": "a-long-test-password"}).status_code == 200
    session.expire_all()
    assert session.get(LoginSession, token_hash("old-2")) is None, "login sweeps expired sessions"


def test_rate_limit_memory_is_bounded(monkeypatch):
    api.attempts.clear()
    old = time.time() - api.LOGIN_WINDOW_SECONDS - 5
    for i in range(api.ATTEMPT_KEYS_SWEEP_AT + 50):
        api.attempts[("1.2.3.4", f"user{i}@x")].append(old)
    api.prune_attempts(time.time())
    assert len(api.attempts) == 0


def test_rate_limit_still_blocks_after_ten_attempts(client):
    for _ in range(api.LOGIN_MAX_ATTEMPTS):
        assert client.post("/auth/login", json={"email": "teacher@example.test", "password": "wrong-password-x"}).status_code == 401
    assert client.post("/auth/login", json={"email": "teacher@example.test", "password": "a-long-test-password"}).status_code == 429


def test_evidence_numbers_unchanged(client, teacher):
    d = client.get("/classes/1/evidence", headers=teacher).json()
    assert (d["mean_percentage"], d["marked_count"], d["missing_count"]) == (75, 3, 1)
    by_id = {l["learner_id"]: l for l in d["learners"]}
    assert by_id["L1"]["mean_percentage"] == 60 and by_id["L4"]["mean_percentage"] is None
