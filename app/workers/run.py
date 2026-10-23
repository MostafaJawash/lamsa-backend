"""Run the background worker: python -m app.workers.run

Stops gracefully on SIGTERM / SIGINT (Ctrl+C): the running job finishes first.
"""

import asyncio
import logging
import signal

from app.db.session import engine
from app.modules.notifications import handlers
from app.workers.runner import Worker


async def main() -> None:
    handlers.register()  # jobs publish no events today, but keep the worker able to
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except (NotImplementedError, RuntimeError):  # Windows: no loop signal handlers
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(worker.stop))
    try:
        await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
