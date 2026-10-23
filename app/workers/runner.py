"""The background worker: periodic jobs in one asyncio loop.

Used by `python -m app.workers.run` (production: its own process) and, when RUN_WORKER_IN_APP is
true (default only in development), inside the API process. Every job catches and logs its own
errors, so one failing job never stops the others.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.modules.notifications import dispatcher, jobs

logger = logging.getLogger("app.worker")


@dataclass
class Job:
    name: str
    every_seconds: float
    run: Callable[[], Awaitable[Any]]
    next_at: float = 0.0  # monotonic; 0 = run at start


def default_jobs() -> list[Job]:
    return [
        Job("dispatch", 30, dispatcher.dispatch_due),
        Job("recover_stuck", 5 * 60, dispatcher.recover_stuck),
        Job("cart_reminders", 15 * 60, jobs.cart_reminders),
        Job("cleanup", 24 * 60 * 60, jobs.cleanup),
    ]


class Worker:
    def __init__(self, job_list: list[Job] | None = None) -> None:
        self.jobs = job_list if job_list is not None else default_jobs()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Graceful: the running job finishes, then the loop ends."""
        self._stop.set()

    async def run_job(self, job: Job) -> None:
        try:
            await job.run()
        except Exception:
            logger.exception("Job %s failed", job.name)

    async def run(self) -> None:
        logger.info("Worker started: %s", ", ".join(f"{j.name}/{j.every_seconds:g}s" for j in self.jobs))
        while not self._stop.is_set():
            now = time.monotonic()
            for job in self.jobs:
                if self._stop.is_set():
                    break
                if now >= job.next_at:
                    job.next_at = now + job.every_seconds
                    await self.run_job(job)
            wait = max(0.5, min(j.next_at for j in self.jobs) - time.monotonic()) if self.jobs else 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except TimeoutError:
                pass
        logger.info("Worker stopped")
