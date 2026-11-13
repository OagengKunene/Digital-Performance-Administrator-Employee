import pytest
from pae.agents import ReviewedReport, review_numbers, SCHEMAS


def upload(client, teacher, csv, kind="marks"):
    return client.post(f"/classes/1/imports/{kind}", headers=teacher, json={"csv": csv})


def test_authentication_required(client):
    assert client.get("/classes").status_code == 401


def test_teacher_scope_and_cross_school_boundary(client, teacher, principal):
    assert [c["id"] for c in client.get("/classes", headers=teacher).json()] == [1]
    assert len(client.get("/classes", headers=principal).json()) == 2
    for path in ["evidence", "marks", "audit", "runs", "learners"]:
        assert client.get(f"/classes/2/{path}", headers=teacher).status_code == 404
        assert client.get(f"/classes/3/{path}", headers=principal).status_code == 404


def test_missing_marks_do_not_become_zero(client, teacher):
    r = client.get("/classes/1/evidence", headers=teacher).json()
    assert (r["mean_percentage"], r["marked_count"], r["missing_count"]) == (75, 3, 1)


def test_genuine_zero_is_included(client, teacher):
    csv = "learner_id,assessment,topic,earned,possible\nL4,Test 2,Geometry,0,20\n"
    assert upload(client, teacher, csv).status_code == 200
    data = client.get("/classes/1/evidence", headers=teacher).json()
    assert (data["mean_percentage"], data["marked_count"]) == (56.25, 4)


@pytest.mark.parametrize("bad", ["21", "-1", "NaN", "inf"])
def test_invalid_batch_is_atomic(client, teacher, bad):
    csv = f"learner_id,assessment,topic,earned,possible\nL1,Test 2,Geometry,10,20\nL2,Test 2,Geometry,{bad},20\n"
    assert upload(client, teacher, csv).status_code == 422
    assert len(client.get("/classes/1/marks", headers=teacher).json()) == 4


def test_duplicate_upload_rejected(client, teacher):
    csv = "learner_id,assessment,topic,earned,possible\nL1,New test,Geometry,10,20\n"
    assert upload(client, teacher, csv).status_code == 200
    assert upload(client, teacher, csv).status_code == 422


def test_duplicate_within_upload_rejected(client, teacher):
    csv = "learner_id,assessment,topic,earned,possible\nL1,New,Geometry,10,20\nL1,New,Geometry,10,20\n"
    assert upload(client, teacher, csv).status_code == 422


def test_inconsistent_maximum_rejected(client, teacher):
    csv = "learner_id,assessment,topic,earned,possible\nL1,New,Geometry,10,20\nL2,New,Geometry,10,30\n"
    assert upload(client, teacher, csv).status_code == 422


def test_unknown_learner_rejected(client, teacher):
    assert upload(client, teacher, "learner_id,assessment,topic,earned,possible\nUNKNOWN,New,Geometry,10,20\n").status_code == 422


def test_attendance_unknown_is_not_absence(client, teacher):
    csv = "learner_id,day,status\nL1,2026-11-13,present\nL2,2026-11-13,unknown\n"
    assert upload(client, teacher, csv, "attendance").status_code == 200
    data = client.get("/classes/1/evidence", headers=teacher).json()
    assert data["attendance_recorded_status_counts"] == {"present": 1, "unknown": 1}


def test_mark_correction_has_before_after_audit(client, teacher):
    r = client.patch("/classes/1/marks/1", headers=teacher, json={"earned": 13, "reason": "Rechecked source script"})
    assert r.status_code == 200
    log = next(row for row in client.get("/classes/1/audit", headers=teacher).json()
               if row["action"] == "correct_mark")
    assert log["detail"]["before"] == 12 and log["detail"]["after"] == 13


def test_intervention_completion_requires_evidence(client, teacher):
    r = client.post("/classes/1/interventions", headers=teacher, json={"learner_id": "L1", "topic": "Algebra", "activity": "Complete revision worksheet", "due": "2026-11-20"})
    assert r.status_code == 200
    iid = r.json()["id"]
    assert client.patch(f"/classes/1/interventions/{iid}", headers=teacher, json={"status": "completed", "follow_up": ""}).status_code == 422
    assert client.patch(f"/classes/1/interventions/{iid}", headers=teacher, json={"status": "completed", "follow_up": "Worksheet reviewed; reassessment scheduled"}).status_code == 200


def test_teacher_cannot_create_staff(client, teacher):
    assert client.post("/staff", headers=teacher, json={"email": "new@test.school", "password": "long-password-example", "role": "principal"}).status_code == 403


def test_principal_can_onboard_real_records(client, principal):
    user = client.post("/staff", headers=principal, json={"email": "new@test.school", "password": "long-password-example", "role": "teacher"})
    assert user.status_code == 200
    c = client.post("/classes", headers=principal, json={"name": "11B", "subject": "Science", "teacher_id": user.json()["id"]})
    assert c.status_code == 200
    r = client.post(f"/classes/{c.json()['id']}/learners", headers=principal, json={"csv": "learner_id,name\nNEW-001,Example learner\n"})
    assert r.status_code == 200


def test_live_agents_require_a_key(client, teacher, monkeypatch):
    from pae.config import settings
    monkeypatch.setattr(settings(), "google_api_key", "")
    assert client.post("/classes/1/runs", headers=teacher, json={"goal": "Review the class performance"}).status_code == 503


def test_numeric_reviewer_detects_and_accepts_repair():
    source = {"mean_percentage": 75.0, "marked_count": 3, "missing_count": 1}
    report = ReviewedReport(summary="Class evidence summary", mean_percentage=56.25, marked_count=4, missing_count=0, recommendations=[])
    assert len(review_numbers(report, source)) == 3
    corrected = report.model_copy(update=source)
    assert review_numbers(corrected, source) == []


def test_actual_adk_team_constructs_without_network():
    assert set(SCHEMAS) == {"coordinator", "performance_analyst", "intervention_planner",
                            "reporting_agent", "quality_reviewer"}


def test_login_and_revocation(client):
    login = client.post("/auth/login", json={"email": "teacher@example.test", "password": "a-long-test-password"})
    assert login.status_code == 200
    auth = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get("/classes", headers=auth).status_code == 200
    assert client.post("/auth/logout", headers=auth).status_code == 200
    assert client.get("/classes", headers=auth).status_code == 401
