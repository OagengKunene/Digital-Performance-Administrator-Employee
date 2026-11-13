"""Linux/container smoke check using isolated fictional data and no AI credentials."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import httpx

root = Path(__file__).resolve().parents[1]


def main():
    for port in [8000, 8501]:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise SystemExit(f"Port {port} is in use. Stop this project's services before the smoke check.")
    with tempfile.TemporaryDirectory(prefix="pae-smoke-") as folder:
        directory = Path(folder)
        env = dict(os.environ, DATABASE_URL="sqlite:///" + str(directory / "school.db"),
                   GOOGLE_API_KEY="", GROQ_API_KEY="", AI_PROVIDER="gemini",
                   AI_FALLBACK_PROVIDER="", WORKER_POLL_SECONDS="0.1",
                   INGESTION_INBOX_ROOT=str(directory / "source-inboxes"), INGESTION_SETTLE_SECONDS="0")
        subprocess.run([sys.executable, "-m", "pae.manage", "bootstrap", "--sample"],
                       cwd=directory, env=env, check=True, stdout=subprocess.DEVNULL)
        with (directory / "services.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, str(root / "scripts/start.py")],
                env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                with httpx.Client(timeout=3, trust_env=False) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            assert client.get("http://127.0.0.1:8000/health").status_code == 200
                            assert client.get("http://127.0.0.1:8501/_stcore/health").status_code == 200
                            break
                        except (httpx.TransportError, AssertionError):
                            if process.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("Services did not become healthy")
                            time.sleep(0.2)
                    credentials = (directory / ".local/bootstrap-login.txt").read_text().splitlines()
                    principal = next(line.split() for line in credentials
                                     if line.startswith("principal@example.test"))
                    response = client.post("http://127.0.0.1:8000/auth/login",
                        json={"email": principal[0], "password": principal[1]})
                    response.raise_for_status()
                    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
                    classes = client.get("http://127.0.0.1:8000/classes", headers=headers).json()
                    cid = classes[0]["id"]
                    prefix = f"http://127.0.0.1:8000/classes/{cid}"
                    policy = client.put(prefix + "/monitoring", headers=headers,
                        json={"enabled": True, "use_ai": False, "interval_minutes": 15})
                    policy.raise_for_status()
                    deadline = time.monotonic() + 15
                    while True:
                        items = client.get(prefix + "/follow-ups", headers=headers).json()
                        if any(item["issue_key"] == "missing_marks" for item in items):
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("The worker did not create the autonomous follow-up")
                        time.sleep(0.2)
                    baseline = client.get(prefix + "/statistics", headers=headers).json()
                    assert baseline["mean_percentage"] == 75
                    assert baseline["missing_count"] == 1
                    runtime = client.get("http://127.0.0.1:8000/agent-runtime", headers=headers).json()
                    assert runtime["worker_last_seen"] is not None
                    assert not runtime["configured"]
                    source = client.post(prefix + "/sources", headers=headers,
                        json={"name": "Automatic school export", "kind": "marks"})
                    source.raise_for_status()
                    connected = source.json()
                    folder = Path(connected["inbox_folder"])
                    folder.mkdir(parents=True, exist_ok=True)
                    # Act as the school's scheduled export, without any report prompt.
                    (folder / "export.csv").write_text(
                        "learner_id,assessment,topic,earned,possible\nG101-004,Sample Test 1,Algebra,15,20\n")
                    deadline = time.monotonic() + 15
                    while True:
                        batches = client.get(prefix + "/ingestion-batches", headers=headers).json()
                        if any(batch["status"] == "completed" for batch in batches):
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("Source file was not captured automatically: " + str(batches))
                        time.sleep(0.2)
                    messages = client.get("http://127.0.0.1:8000/notifications", headers=headers).json()
                    report = next(m["payload"]["report"] for m in messages if m["kind"] == "capture_evaluation")
                    assert report["marked_count"] == 4 and report["missing_count"] == 0
                    assert report["mean_percentage"] == 75 and report["assessment_count"] == 1
                    teacher = next(line.split() for line in credentials if line.startswith("teacher@example.test"))
                    response = client.post("http://127.0.0.1:8000/auth/login",
                        json={"email": teacher[0], "password": teacher[1]})
                    response.raise_for_status()
                    teacher_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
                    delivered = client.get("http://127.0.0.1:8000/notifications", headers=teacher_headers).json()
                    assert any(m["kind"] == "capture_evaluation" for m in delivered)
                    print("PASS: all three services, watched CSV capture, verified evaluation, automatic teacher/principal inbox delivery, no AI key or report prompt.")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-5000:])
                raise
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
