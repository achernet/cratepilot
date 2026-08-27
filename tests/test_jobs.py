import time
from pathlib import Path

from cratepilot.jobs import JobRunner
from cratepilot.storage import Store


def test_job_progress_logs_and_cooperative_cancellation(tmp_path: Path):
    store = Store(tmp_path / "jobs.db")
    runner = JobRunner(store)

    def operation(context):
        for index in range(100):
            context.report(index / 100, f"Step {index}")
            time.sleep(0.005)
        return {"unexpected": True}

    job_id = runner.submit("planning", operation)
    deadline = time.monotonic() + 2
    while store.job(job_id)["progress"] < 0.05 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runner.cancel(job_id)
    while store.job(job_id)["status"] not in {"cancelled", "failed"} and time.monotonic() < deadline:
        time.sleep(0.005)
    job = store.job(job_id)
    assert job["status"] == "cancelled"
    assert any("Cancellation requested" in entry["message"] for entry in job["logs"])
