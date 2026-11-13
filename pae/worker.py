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


async def serve(once=False):
    worker_id = str(uuid.uuid4())
    while True:
        with SessionLocal() as db:
            beat = db.get(WorkerHeartbeat, worker_id)
            if not beat:
                db.add(WorkerHeartbeat(id=worker_id, last_seen=time.time()))
            else:
                beat.last_seen = time.time()
            db.commit()
        try:
            check_due(SessionLocal)
            await process_one(SessionLocal)
        except LeaseLost:
            logger.warning("A job lease changed; discarded work from the old owner.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # No exception body or traceback: provider errors may contain data.
            logger.error("Worker cycle failed error_type=%s", type(error).__name__)
        if once:
            return
        await asyncio.sleep(settings().worker_poll_seconds)


async def main(once=False):
    if not inspect(engine).has_table("agent_jobs"):
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
