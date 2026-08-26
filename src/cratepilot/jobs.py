from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from .storage import Store


class JobRunner:
    def __init__(self, store: Store) -> None:
        self.store = store

    def submit(self, kind: str, operation: Callable[[], dict[str, Any]]) -> str:
        job_id = uuid.uuid4().hex
        self.store.update_job(job_id, kind, "queued", 0.0, "Queued")

        def run() -> None:
            self.store.update_job(job_id, kind, "running", 0.1, "Working")
            try:
                result = operation()
            except Exception as exc:  # surfaced through the local job API
                self.store.update_job(job_id, kind, "failed", 1.0, str(exc))
            else:
                self.store.update_job(job_id, kind, "complete", 1.0, "Complete", result)

        threading.Thread(target=run, name=f"cratepilot-{kind}-{job_id[:6]}", daemon=True).start()
        return job_id

