"""Photograph capture and per-student reporting, including real local OCR."""
import asyncio
import copy
import io
import json
import shutil
import time

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, func

from pae.db import ScriptPhoto, Mark, Audit, User, ReportNotification
from pae.script_photos import process_photo, sample_script, check_reading, review_report
from pae.script_reader import ScriptReading, normalise_image, local_read, parse_transcript, vision_read


@pytest.fixture
def reading():
    return ScriptReading(learner_id="L1", assessment="Test photo", subject="Maths", total_earned=25, total_possible=40,
        questions=[{"question": "Q" + str(i), "topic": topic, "earned": score, "possible": 10, "evidence": f"Q{i} {score}/10"}
                   for i, topic, score in [(1, "Algebra", 8), (2, "Equations", 5), (3, "Geometry", 3), (4, "Data handling", 9)]]).model_dump()


def upload(client, teacher, **kwargs):
    response = client.post("/classes/1/script-photos", headers=teacher, params={"reader": "local", **kwargs}, content=sample_script("L1"))
    assert response.status_code == 202, response.text
    return response.json()["id"]


def detail(client, teacher, identifier):
    response = client.get("/classes/1/script-photos/" + identifier, headers=teacher)
    assert response.status_code == 200, response.text
    return response.json()


def drain(db_factory, reading):
    for _ in range(8):
        if not process_photo(db_factory, lambda *args: copy.deepcopy(reading)):
            return
    raise AssertionError("Photo workflow did not stop")


def test_upload_to_individual_report_and_teacher_principal_inboxes(client, teacher, principal, db_factory, reading):
    identifier = upload(client, teacher)
    assert detail(client, teacher, identifier)["status"] == "queued"
    assert process_photo(db_factory, lambda *args: reading)
    captured = detail(client, teacher, identifier)
    assert captured["status"] == "validating" and len(captured["extraction"]["questions"]) == 4
    drain(db_factory, reading)
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed"
    report = item["report"]
    assert (report["earned"], report["possible"], report["mean_percentage"]) == (25, 40, 62.5)
    assert report["learner_id"] == "L1" and report["roster_matched"] is True
    assert report["review"]["checked_percentage"] == 62.5
    assert any("Geometry" in x for x in report["support_areas"])
    assert any("Algebra" in x for x in report["strengths"])
    assert [e["stage"] for e in item["events"]] == ["queued", "reading", "validating", "reporting", "reviewing", "completed"]
    for headers in (teacher, principal):
        inbox = client.get("/notifications", headers=headers).json()
        assert len(inbox) == 1 and inbox[0]["kind"] == "script_evaluation"
        assert inbox[0]["payload"]["report"]["mean_percentage"] == 62.5
    assert client.get("/notifications", headers={"Authorization": "Bearer other-token"}).json() == []
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Mark)) == 4
        assert db.get(Mark, 1).earned == 12
        assert db.get(Mark, 4).earned is None


@pytest.mark.parametrize("problem", ["wrong_total", "duplicate", "unreadable", "over_maximum", "missing_maximum", "overlap", "empty"])
def test_uncertain_or_inconsistent_readings_block_publication(client, teacher, db_factory, reading, problem):
    if problem == "wrong_total": reading["total_earned"] = 35
    elif problem == "duplicate": reading["questions"].append(copy.deepcopy(reading["questions"][0]))
    elif problem == "unreadable": reading["questions"][0]["uncertain"] = True
    elif problem == "over_maximum": reading["questions"][0]["earned"] = 18
    elif problem == "missing_maximum": reading["questions"][0]["possible"] = None
    elif problem == "overlap": reading["questions"][1]["question"] = "Q1.1"
    else: reading = ScriptReading().model_dump()
    identifier = upload(client, teacher)
    drain(db_factory, reading)
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review" and item["issues"]
    assert client.get("/notifications", headers=teacher).json() == []


def test_report_metrics_are_rechecked_before_delivery(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    for _ in range(3):
        assert process_photo(db_factory, lambda *args: reading)
    with db_factory() as db:
        item = db.get(ScriptPhoto, identifier)
        assert item.status == "reviewing"
        item.report = {**item.report, "mean_percentage": 99}
        db.commit()
    assert process_photo(db_factory)
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review" and item["report"]["review"]["status"] == "blocked"
    assert client.get("/notifications", headers=teacher).json() == []


def test_duplicate_uploads_and_worker_replays_do_not_duplicate_delivery(client, teacher, db_factory, reading):
    first = upload(client, teacher, filename="first.png")
    assert upload(client, teacher, filename="repeated.png") == first
    drain(db_factory, reading)
    assert upload(client, teacher) == first
    assert not process_photo(db_factory)
    assert len(client.get("/notifications", headers=teacher).json()) == 1


def test_photo_image_results_and_corrections_are_class_scoped(client, teacher, principal, db_factory, reading):
    identifier = upload(client, teacher)
    for token in ("other-token", "outsider-token"):
        headers = {"Authorization": "Bearer " + token}
        for suffix in ("", "/image"):
            assert client.get("/classes/1/script-photos/" + identifier + suffix, headers=headers).status_code == 404
        assert client.post("/classes/1/script-photos/" + identifier + "/correction", headers=headers,
            json={"reading": reading, "reason": "Checked the original paper"}).status_code == 404
    assert client.get("/classes/2/script-photos/" + identifier, headers=principal).status_code == 404
    response = client.get("/classes/1/script-photos/" + identifier + "/image", headers=teacher)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "image" not in detail(client, teacher, identifier)


def test_staff_correction_resumes_automatically_and_preserves_original(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    uncertain = copy.deepcopy(reading)
    uncertain["questions"][0]["uncertain"] = True
    drain(db_factory, uncertain)
    result = client.post("/classes/1/script-photos/" + identifier + "/correction", headers=teacher,
        json={"reading": reading, "reason": "Checked Q1 against the original marked paper"})
    assert result.status_code == 200, result.text
    drain(db_factory, reading)
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed"
    assert item["original_extraction"]["questions"][0]["uncertain"] is True
    with db_factory() as db:
        audit = db.scalar(select(Audit).where(Audit.action == "script_photo_transcription_corrected"))
        assert audit.detail["reason"].startswith("Checked Q1")
    assert client.post("/classes/1/script-photos/" + identifier + "/correction", headers=teacher,
        json={"reading": reading, "reason": "Checked original paper"}).status_code == 409


def test_expired_photo_lease_recovers_without_repeating_saved_ocr(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    assert process_photo(db_factory, lambda *args: reading)
    with db_factory() as db:
        item = db.get(ScriptPhoto, identifier)
        item.lease_owner, item.lease_until = "dead-worker", time.time() - 1
        db.commit()
    drain(db_factory, reading)
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed" and item["attempts"] == 1


def test_replaced_worker_cannot_publish_or_save_its_old_ocr(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    def stolen(*args):
        with db_factory() as db:
            item = db.get(ScriptPhoto, identifier)
            item.lease_owner, item.lease_until = "new-worker", time.time() + 60
            db.commit()
        return reading
    assert process_photo(db_factory, stolen)
    item = detail(client, teacher, identifier)
    assert not item["extraction"] and item["status"] == "reading"
    assert client.get("/notifications", headers=teacher).json() == []


def test_provider_outage_retries_without_exposing_error_details(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    def unavailable(*args):
        raise httpx.HTTPStatusError("private provider key and student content", request=httpx.Request("POST", "https://provider.invalid"),
            response=httpx.Response(503))
    assert process_photo(db_factory, unavailable)
    first = detail(client, teacher, identifier)
    assert first["status"] == "reading" and "private provider" not in json.dumps(first)
    with db_factory() as db:
        db.get(ScriptPhoto, identifier).next_at = 0
        db.commit()
    drain(db_factory, reading)
    assert detail(client, teacher, identifier)["attempts"] == 2
    assert detail(client, teacher, identifier)["status"] == "completed"


def test_revoked_staff_access_blocks_photo_delivery(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    for _ in range(3): process_photo(db_factory, lambda *args: reading)
    with db_factory() as db:
        db.get(User, 1).school = "No longer authorised"
        db.commit()
    assert process_photo(db_factory)
    with db_factory() as db:
        assert db.get(ScriptPhoto, identifier).status == "authorisation_blocked"
        assert db.scalar(select(func.count()).select_from(ReportNotification)) == 0


def test_total_only_script_has_no_invented_topic_findings(client, teacher, db_factory):
    reading = ScriptReading(total_earned=14, total_possible=20).model_dump()
    identifier = upload(client, teacher)
    drain(db_factory, reading)
    report = detail(client, teacher, identifier)["report"]
    assert report["mean_percentage"] == 70 and report["roster_matched"] is False
    assert report["strengths"] == report["support_areas"] == []
    assert any("Only an overall total" in text for text in report["limitations"])


@pytest.mark.parametrize("raw", [b"", b"not an image", b"x" * 6_000_001])
def test_invalid_and_oversized_files_are_rejected(client, teacher, raw):
    assert client.post("/classes/1/script-photos", headers=teacher, content=raw).status_code in {413, 422}


def test_oversized_dimensions_are_rejected_without_storing_image():
    image = Image.new("RGB", (4100, 4100), "white")
    output = io.BytesIO()
    image.save(output, "PNG")
    with pytest.raises(ValueError, match="megapixels"):
        normalise_image(output.getvalue())


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_supported_phone_image_formats_decode_and_strip_metadata(format):
    output = io.BytesIO()
    Image.new("RGB", (100, 60), "white").save(output, format=format)
    normal = normalise_image(output.getvalue())
    with Image.open(io.BytesIO(normal)) as image:
        assert image.format == "JPEG" and image.size == (100, 60)
        assert not image.getexif()


def test_phone_exif_orientation_is_applied_before_ocr():
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (100, 60), "white").save(output, format="JPEG", exif=exif)
    with Image.open(io.BytesIO(normalise_image(output.getvalue()))) as image:
        assert image.size == (60, 100) and not image.getexif()


def test_negative_and_zero_maxima_are_not_misread_as_positive():
    reading = parse_transcript("Q1 Algebra -2/10\nQ2 Geometry 3/0\nTOTAL: 1/10")
    assert reading["issues"] and not reading["questions"]


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Install Tesseract for real photo OCR")
@pytest.mark.parametrize("source", ["sample", "upload"])
def test_real_rendered_photo_ocr_and_complete_pipeline(client, teacher, principal, db_factory, source):
    response = client.post("/classes/1/script-photos/sample", headers=teacher) if source == "sample" else client.post(
        "/classes/1/script-photos?reader=local", content=sample_script("L1"), headers=teacher)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    for _ in range(5):
        if not process_photo(db_factory): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed", item
    assert item["report"]["mean_percentage"] == 62.5
    assert item["report"]["review"]["status"] == "passed"


def test_groq_vision_uses_image_payload_and_validates_response(monkeypatch, ai_config, reading):
    import pae.script_reader as reader
    monkeypatch.setattr(ai_config, "groq_api_key", "private-fake-key")
    class Gate:
        async def wait(self): return 0
    monkeypatch.setattr(reader, "model_pacer", lambda *args, **kwargs: Gate())
    seen = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            seen.append(kwargs)
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": json.dumps(reading)}}]})
    monkeypatch.setattr(reader.httpx, "AsyncClient", Client)
    result = asyncio.run(vision_read(b"image-bytes", "groq"))
    assert result == reading
    assert seen[0]["json"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert seen[0]["json"]["model"] == ai_config.groq_vision_model
    assert "private-fake-key" not in json.dumps(result)


def test_gemini_vision_uses_actual_image_part_and_schema(monkeypatch, ai_config, reading):
    from types import SimpleNamespace
    import google.genai
    import pae.script_reader as reader
    monkeypatch.setattr(ai_config, "google_api_key", "private-fake-key")
    class Gate:
        async def wait(self): return 0
    monkeypatch.setattr(reader, "model_pacer", lambda *args, **kwargs: Gate())
    calls = []
    class Models:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=json.dumps(reading))
    class AsyncClient:
        models = Models()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["http_options"].retry_options.attempts == 1
            self.aio = AsyncClient()
    monkeypatch.setattr(google.genai, "Client", Client)
    assert asyncio.run(vision_read(b"image-bytes", "gemini")) == reading
    assert calls[0]["contents"][1].inline_data.data == b"image-bytes"
    assert calls[0]["config"].response_schema is ScriptReading


def test_equivalent_question_labels_are_duplicate_records(reading):
    reading["questions"][1]["question"] = "Question 01"
    assert any("duplicate" in issue for issue in check_reading(ScriptReading.model_validate(reading)))


def test_reader_auto_selection_respects_offline_setting(monkeypatch, ai_config):
    from pae.script_reader import choose_reader
    monkeypatch.setattr(ai_config, "google_api_key", "")
    monkeypatch.setattr(ai_config, "groq_api_key", "configured")
    assert choose_reader() == "groq"
    monkeypatch.setattr(ai_config, "ai_provider", "offline")
    assert choose_reader() == "local"
