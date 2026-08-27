from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .storage import Store

LOGGER = logging.getLogger(__name__)


class JobCancelled(RuntimeError):
    pass


@dataclass
class JobContext:
    job_id: str
    kind: str
    store: Store
    cancelled: threading.Event

    def report(self, progress: float, message: str, *, level: str = "info") -> None:
        progress = max(0.0, min(0.99, progress))
        self.store.update_job(self.job_id, self.kind, "running", progress, message)
        self.store.append_job_log(self.job_id, level, message)
        getattr(LOGGER, level if level in {"debug", "info", "warning", "error"} else "info")(
            "%s job %s: %s", self.kind, self.job_id, message
        )
        self.check_cancelled()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled("Cancelled by user.")


class JobRunner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._cancellations: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, operation: Callable[[JobContext], dict[str, Any]]) -> str:
        job_id = uuid.uuid4().hex
        cancellation = threading.Event()
        with self._lock:
            self._cancellations[job_id] = cancellation
        self.store.update_job(job_id, kind, "queued", 0.0, "Queued")
        self.store.append_job_log(job_id, "info", f"Queued {kind} task.")

        def run() -> None:
            context = JobContext(job_id, kind, self.store, cancellation)
            self.store.update_job(job_id, kind, "running", 0.01, "Starting")
            self.store.append_job_log(job_id, "info", f"Started {kind} task.")
            LOGGER.info("Started %s job %s", kind, job_id)
            try:
                result = operation(context)
                context.check_cancelled()
            except JobCancelled as exc:
                self.store.append_job_log(job_id, "warning", str(exc))
                self.store.update_job(job_id, kind, "cancelled", 1.0, str(exc))
                LOGGER.info("Cancelled %s job %s", kind, job_id)
            except Exception as exc:  # surfaced through the local job API
                self.store.append_job_log(job_id, "error", str(exc))
                self.store.update_job(job_id, kind, "failed", 1.0, str(exc))
                LOGGER.exception("%s job %s failed", kind, job_id)
            else:
                self.store.append_job_log(job_id, "info", "Complete")
                self.store.update_job(job_id, kind, "complete", 1.0, "Complete", result)
                LOGGER.info("Completed %s job %s", kind, job_id)
            finally:
                with self._lock:
                    self._cancellations.pop(job_id, None)

        threading.Thread(target=run, name=f"cratepilot-{kind}-{job_id[:6]}", daemon=True).start()
        return job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            cancellation = self._cancellations.get(job_id)
        if cancellation is None:
            return False
        cancellation.set()
        current = self.store.job(job_id)
        if current:
            self.store.append_job_log(job_id, "warning", "Cancellation requested; stopping at the next safe checkpoint.")
            self.store.update_job(
                job_id, str(current["kind"]), "cancelling", float(current["progress"]),
                "Cancellation requested; stopping safely…",
            )
        return True
