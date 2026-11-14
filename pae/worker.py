"""Run durable jobs and policy-driven monitoring independently of API requests."""
import argparse
import asyncio
import logging
import signal
import time
import uuid
from sqlalchemy import inspect
from .config import settings
from .db import SessionLocal, engine, WorkerHeartbeat
from .jobs import process_one, LeaseLost
from .monitoring import check_due

logger = logging.getLogger(__name__)


def control_cycle(worker_id):
    from .ingestion import scan_sources, process_batch
    from .captures import queue_pending, recover_due
    from .notifications import process_email
    with SessionLocal() as db:
        beat = db.get(WorkerHeartbeat, worker_id)
        if not beat:
            db.add(WorkerHeartbeat(id=worker_id, last_seen=time.time()))
        else:
            beat.last_seen = time.time()
        db.commit()
    for action in [scan_sources, process_batch, queue_pending, recover_due, process_email, check_due]:
        try:
            action(SessionLocal)
        except Exception as error:
            logger.error("Control action %s failed error_type=%s", action.__name__, type(error).__name__)


async def serve(once=False):
    worker_id = str(uuid.uuid4())
    if once:
        control_cycle(worker_id)
        await process_one(SessionLocal)
        return

    async def controls():
        while True:
            try:
                await asyncio.to_thread(control_cycle, worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Heartbeat/database setup can fail before action-level guards.
                # Keep both loops alive and try again on the next control cycle.
                logger.error("Control cycle failed error_type=%s", type(error).__name__)
            await asyncio.sleep(settings().worker_poll_seconds)

    async def evaluations():
        while True:
            try:
                await process_one(SessionLocal)
            except LeaseLost:
                logger.warning("A job lease changed; discarded work from the old owner.")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Evaluation cycle failed error_type=%s", type(error).__name__)
            await asyncio.sleep(settings().worker_poll_seconds)

    # Ingestion and factual delivery keep running while a model request waits.
    async with asyncio.TaskGroup() as group:
        group.create_task(controls())
        group.create_task(evaluations())


async def main(once=False):
    if not inspect(engine).has_table("capture_events"):
        raise SystemExit("Run .venv/bin/python scripts/setup.py to install dependencies and initialise new tables.")
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in [signal.SIGTERM, signal.SIGINT]:
        try:
            loop.add_signal_handler(signum, task.cancel)
        except NotImplementedError:
            pass  # Windows relies on normal process shutdown and lease expiry.
    try:
        await serve(once)
    except asyncio.CancelledError:
        logger.info("Worker stopped; completed stages remain saved.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
