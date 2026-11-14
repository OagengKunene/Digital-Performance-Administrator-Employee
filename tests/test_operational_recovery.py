"""Operational regressions: control-loop survival and unattended backlog recovery."""
import asyncio
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from pae.db import AgentJob, AgentRun
from pae.jobs import process_one
from pae.captures import queue_pending, recover_due
from test_capture_automation import correct


def test_worker_survives_temporary_heartbeat_database_failure(monkeypatch, ai_config, caplog):
    from pae import worker

    calls = []
    resumed = threading.Event()

    def controls(identifier):
        calls.append(identifier)
        if len(calls) == 1:
            # Heartbeat persistence happens before the individual action guards.
            raise OperationalError("private SQL", {}, RuntimeError("private credentials"))
        resumed.set()

    async def evaluations(factory):
        await asyncio.sleep(0.01)
        return False

    monkeypatch.setattr(worker, "control_cycle", controls)
    monkeypatch.setattr(worker, "process_one", evaluations)
    monkeypatch.setattr(ai_config, "worker_poll_seconds", 0.01)

    async def exercise():
        task = asyncio.create_task(worker.serve())
        try:
            # Poll on the event loop: the control cycle executes in a thread.
            for _ in range(100):
                if task.done():
                    await task
                if resumed.is_set():
                    break
                await asyncio.sleep(0.01)
            assert resumed.is_set(), "Background control processing never recovered"
        finally:
            task.cancel()
            if not task.done():
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())
    assert len(calls) >= 2
    assert "OperationalError" in caplog.text
    assert "private SQL" not in caplog.text and "private credentials" not in caplog.text


@pytest.mark.parametrize("blocker", ["cooldown", "exhausted", "nonrecoverable", "unconfigured_provider"])
def test_old_failed_captures_cannot_starve_a_due_recovery(
        client, teacher, db_factory, ai_config, blocker):
    async def unavailable(*args, **kwargs):
        error = RuntimeError("provider unavailable")
        error.status_code = 429
        raise error

    identifiers = []
    for score in range(21):
        correct(client, teacher, db_factory, score)
        assert queue_pending(db_factory)
        assert asyncio.run(process_one(db_factory, unavailable))
        with db_factory() as db:
            job = db.scalar(select(AgentJob).where(AgentJob.next_at > 0).order_by(AgentJob.updated_at.desc()))
            assert job.status == "failed"
            identifiers.append(job.id)
            if score < 20:
                if blocker == "cooldown":
                    job.next_at = time.time() + 3600
                elif blocker == "exhausted":
                    job.state = {**job.state, "request_attempts": job.state["request_budget"]}
                elif blocker == "unconfigured_provider":
                    job.provider = "groq"
                else:
                    run = db.get(AgentRun, job.id)
                    run.result = {**run.result, "error_code": "stage_output_invalid"}
            else:
                job.next_at = 0
            db.commit()

    assert recover_due(db_factory), "Twenty older failures hid a due capture indefinitely"
    with db_factory() as db:
        assert db.get(AgentJob, identifiers[-1]).status == "queued"
        assert all(db.get(AgentJob, identifier).status == "failed" for identifier in identifiers[:-1])
