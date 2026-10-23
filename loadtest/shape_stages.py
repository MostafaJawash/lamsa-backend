"""Fixed stages for the browsing/shopping mix: 50, 200, 500, 1000 concurrent users.

STAGE_SECONDS per stage (environment variable LOAD_STAGE_SECONDS, default 90).
"""

import os

from locust import LoadTestShape

STAGES = (50, 200, 500, 1000)
STAGE_SECONDS = int(os.environ.get("LOAD_STAGE_SECONDS", "90"))


class StagesShape(LoadTestShape):
    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        stage = int(elapsed // STAGE_SECONDS)
        if stage >= len(STAGES):
            return None
        users = STAGES[stage]
        return users, max(20.0, users / 10)  # spawn rate: reach each stage within ~10 s
