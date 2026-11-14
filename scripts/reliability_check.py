"""Linux process-level recovery check; fictional data and loopback SMTP only.

Run with the installed project environment. No school DB or provider key is used.
The crash scenario waits for the real 61-second lease instead of editing it.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
HEADER = "learner_id,assessment,topic,earned,possible\n"
FIRST = HEADER + "G101-004,Sample Test 1,Algebra,15,20\n"
SECOND = HEADER + "G101-001,Sample Test 2,Algebra,8,20\n"


def wait_for(label, check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {label}")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process, abrupt=False):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL if abrupt else signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def certificate(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "smtp.crt", directory / "smtp.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


class MailHandler(socketserver.StreamRequestHandler):
    """Minimal TLS-only SMTP sink. Stores messages locally; never relays mail."""
    def handle(self):
        self.connection.settimeout(10)
        self.wfile.write(b"220 localhost reliability sink\r\n")
        while line := self.rfile.readline(8192):
            command = line.split(b" ", 1)[0].strip().upper()
            if command in {b"EHLO", b"HELO"}:
                self.wfile.write(b"250 localhost\r\n")
            elif command in {b"MAIL", b"RCPT", b"RSET"}:
                self.wfile.write(b"250 OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                data = []
                while chunk := self.rfile.readline(65536):
                    if chunk == b".\r\n":
                        break
                    data.append(chunk)
                self.server.messages.append(b"".join(data))
                self.wfile.write(b"250 Accepted locally\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"502 Unsupported\r\n")


class MailSink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, cert, key):
        self.messages = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        super().__init__(("127.0.0.1", port), MailHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        try:
            return self.context.wrap_socket(connection, server_side=True), address
        except Exception:
            connection.close()
            raise


async def fault_worker(marker):
    # Only this test subprocess stalls a deterministic provider stage. The
    # production worker has no fault-injection environment variables or hooks.
    from pae import offline_engine, worker
    original = offline_engine.offline_stage

    async def stalled(stage, *args, **kwargs):
        if stage == "performance_analyst":
            marker.write_text("unfinished provider request")
            await asyncio.sleep(3600)
        return await original(stage, *args, **kwargs)

    offline_engine.offline_stage = stalled
    await worker.main()


def exercise():
    checks = []

    def passed(name):
        checks.append(name)
        print("PASS: " + name, flush=True)

    with tempfile.TemporaryDirectory(prefix="pae-reliability-") as folder:
        directory = Path(folder)
        database = directory / "fictional.db"
        cert, key = certificate(directory)
        api_port, smtp_port = free_port(), free_port()
        env = dict(os.environ, DATABASE_URL="sqlite:///" + str(database),
            PYTHONPATH=str(ROOT), GOOGLE_API_KEY="", GROQ_API_KEY="", AI_PROVIDER="gemini",
            AI_FALLBACK_PROVIDER="", AI_TIMEOUT_SECONDS="1", WORKER_POLL_SECONDS="0.1",
            INGESTION_INBOX_ROOT=str(directory / "inbox"), INGESTION_SETTLE_SECONDS="0",
            EMAIL_DELIVERY_ENABLED="true", SMTP_HOST="localhost", SMTP_PORT=str(smtp_port),
            SMTP_SECURITY="ssl", SMTP_FROM="reports@fictional-school.co.za", SMTP_USERNAME="",
            SMTP_PASSWORD="", SSL_CERT_FILE=str(cert))
        subprocess.run([sys.executable, "-m", "pae.manage", "bootstrap", "--sample"],
            cwd=directory, env=env, check=True, stdout=subprocess.DEVNULL)
        credentials = dict(line.split() for line in
            (directory / ".local/bootstrap-login.txt").read_text().splitlines())
        # These addresses are delivered solely to the local non-relaying sink.
        with sqlite3.connect(database) as db:
            db.execute("UPDATE users SET email=? WHERE email=?",
                ("teacher@fictional-school.co.za", "teacher@example.test"))
        processes, mail = [], None
        with (directory / "services.log").open("w+") as log:
            def launch(args, provider="gemini"):
                process = subprocess.Popen([sys.executable, *args], cwd=directory,
                    env={**env, "AI_PROVIDER": provider}, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                processes.append(process)
                return process

            def query(sql, params=()):
                with sqlite3.connect(database) as db:
                    return db.execute(sql, params).fetchall()

            api = launch(["-m", "uvicorn", "pae.api:app", "--host", "127.0.0.1", "--port", str(api_port)])
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=3, trust_env=False) as client:
                    def healthy():
                        assert api.poll() is None, "API process exited"
                        try:
                            return client.get("/health").status_code == 200
                        except httpx.TransportError:
                            return False
                    wait_for("API startup", healthy)

                    def login(email, password):
                        r = client.post("/auth/login", json={"email": email, "password": password})
                        r.raise_for_status()
                        return {"Authorization": "Bearer " + r.json()["access_token"]}

                    principal = login("principal@example.test", credentials["principal@example.test"])
                    teacher = login("teacher@fictional-school.co.za", credentials["teacher@example.test"])
                    other = login("other.teacher@example.test", credentials["other.teacher@example.test"])

                    def get(path, headers=teacher):
                        response = client.get(path, headers=headers)
                        response.raise_for_status()
                        return response.json()

                    cid = get("/classes")[0]["id"]
                    prefix = f"/classes/{cid}"
                    response = client.put("/notification-preferences", headers=teacher,
                        json={"email_enabled": True})
                    response.raise_for_status()
                    response = client.post(prefix + "/sources", headers=teacher,
                        json={"name": "Fictional reliability export", "kind": "marks"})
                    response.raise_for_status()
                    source = response.json()

                    def upload(content):
                        response = client.post(source["upload_endpoint"], content=content,
                            headers={"X-Ingestion-Key": source["ingestion_key"], "Content-Type": "text/csv"})
                        response.raise_for_status()
                        return response.json()

                    # Queue a valid row followed by an impossible score: no
                    # partial mark, capture or report may be committed.
                    upload(FIRST + "G101-001,Sample Test 2,Algebra,99,20\n")
                    worker = launch(["-m", "pae.worker"])
                    wait_for("invalid import rejection", lambda: any(
                        b["status"] == "requires_review" for b in get(prefix + "/ingestion-batches")))
                    assert get(prefix + "/evidence")["marked_count"] == 3
                    assert get(prefix + "/captures") == []
                    assert any(m["kind"] == "source_attention" for m in get("/notifications"))
                    passed("Invalid multi-row import rolls back and delivers an exception notice")

                    # Stop the worker, queue input through the actual HTTP
                    # endpoint, then restart it without any inference keys.
                    stop(worker)
                    upload(FIRST)
                    worker = launch(["-m", "pae.worker"])
                    wait_for("keyless capture", lambda: any(
                        c["evaluation_status"] == "awaiting_provider" for c in get(prefix + "/captures")))
                    for headers in (teacher, principal):
                        reports = [m for m in get("/notifications", headers) if m["kind"] == "capture_evaluation"]
                        assert len(reports) == 1
                        report = reports[0]["payload"]["report"]
                        assert (report["mean_percentage"], report["marked_count"], report["missing_count"]) == (75, 4, 0)
                    assert get("/notifications", other) == []
                    passed("Queued capture survives restart; teacher/principal receive exact metrics without AI")

                    wait_for("SMTP outage recorded", lambda: query(
                        "SELECT id FROM email_deliveries WHERE status='retrying' AND attempts=1"))
                    # Initial SMTP attempts fail on a closed loopback port.
                    # Start the TLS sink and let the real 60-second retry run.
                    mail = MailSink(smtp_port, cert, key)
                    threading.Thread(target=mail.serve_forever, daemon=True).start()
                    stop(worker)
                    worker = launch(["-m", "pae.worker"], provider="offline")
                    wait_for("automatic offline evaluation", lambda: any(
                        m["kind"] == "agent_evaluation" for m in get("/notifications")))
                    passed("Provider-unavailable capture completes automatically after provider configuration restart")

                    stop(worker)
                    upload(SECOND)
                    marker = directory / "stage-entered.txt"
                    worker = launch([str(Path(__file__).resolve()), "--fault-worker", str(marker)], provider="offline")
                    wait_for("unfinished provider stage", marker.exists)
                    stop(worker, abrupt=True)
                    stalled = query("SELECT id, state FROM agent_jobs WHERE status='running'")
                    assert len(stalled) == 1
                    identifier, state = stalled[0][0], json.loads(stalled[0][1])
                    assert "coordinator" in state["outputs"] and "performance_analyst" not in state["outputs"]
                    assert state["request_attempts"] == 2
                    passed("SIGKILL preserves completed stage and the unfinished request's attempt count")

                    worker = launch(["-m", "pae.worker"], provider="offline")
                    print("Waiting for real worker lease expiry and SMTP retry (about one minute)...", flush=True)
                    wait_for("expired-lease report recovery", lambda: len([
                        m for m in get("/notifications") if m["kind"] == "agent_evaluation"]) == 2, timeout=90)
                    events = json.loads(query("SELECT events FROM agent_runs WHERE id=?", (identifier,))[0][0])
                    completed = Counter(e["agent"] for e in events if e["type"] == "stage_completed")
                    assert completed["coordinator"] == 1 and completed["quality_reviewer"] == 1
                    for headers in (teacher, principal):
                        reports = [m for m in get("/notifications", headers) if m["kind"] == "agent_evaluation"]
                        assert sorted(m["payload"]["report"]["mean_percentage"] for m in reports) == [68, 75]
                        assert len({m["id"] for m in reports}) == 2
                    assert upload(SECOND)["status"] == "completed"
                    time.sleep(0.5)
                    assert len(get(prefix + "/captures")) == 2
                    assert get("/notifications", other) == []
                    passed("Actual lease expiry resumes only unfinished work with correct snapshots and no duplicate inbox reports")

                    wait_for("TLS SMTP retry delivery", lambda: query(
                        "SELECT COUNT(*) FROM email_deliveries WHERE status!='sent'")[0][0] == 0, timeout=30)
                    count = query("SELECT COUNT(*) FROM email_deliveries")[0][0]
                    assert len(mail.messages) == count
                    from email import policy
                    from email.parser import BytesParser
                    ids = [BytesParser(policy=policy.default).parsebytes(m)["Message-ID"] for m in mail.messages]
                    assert len(set(ids)) == count
                    assert query("SELECT COUNT(*) FROM email_deliveries WHERE attempts=2")[0][0] >= 1
                    passed("Real certificate-verified TLS SMTP recovers from connection failure across restart")
                    assert all(p.poll() is None for p in (api, worker))
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-5000:], file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    stop(process)
                if mail:
                    mail.shutdown()
                    mail.server_close()
    return {"status": "passed", "checks": checks,
        "scope": "Isolated fictional SQLite database, real HTTP/API/worker processes, local verified TLS SMTP sink, deterministic offline inference. No live model or school mail server."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help="Optional JSON results path")
    args = parser.parse_args()
    if args.fault_worker:
        asyncio.run(fault_worker(args.fault_worker))
    else:
        results = exercise()
        if args.output:
            args.output.write_text(json.dumps(results, indent=2) + "\n")
