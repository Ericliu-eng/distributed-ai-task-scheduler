import os
from pathlib import Path
import subprocess
import sys
import time

from app.store import SchedulerStore
from monitor.recovery import RecoveryMonitor
from worker.main import WorkerService


def test_task_recovers_after_claiming_process_exits(tmp_path, monkeypatch):
    database_path = tmp_path / "crash-recovery.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    store = SchedulerStore()
    store.init()
    created, _ = store.create_task(
        prompt="recover this task after the first worker crashes",
        priority=1,
    )

    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    claim_script = (
        "from app.store import SchedulerStore; "
        "store = SchedulerStore(); "
        "store.init(); "
        "assert store.claim('crashed-worker', 'small', lease_seconds=0.2)"
    )

    subprocess.run(
        [sys.executable, "-c", claim_script],
        cwd=repository_root,
        env=environment,
        check=True,
    )

    time.sleep(0.3)
    recovered = RecoveryMonitor(store=store).run_once()
    assert recovered == [created["id"]]

    replacement = WorkerService(
        worker_id="replacement-worker",
        tier="small",
        store=store,
        lease_seconds=2,
        renew_interval=0.1,
    )
    assert replacement.run_once() is True

    task = store.get_task(created["id"])
    assert task is not None
    assert task["status"] == "succeeded"
    assert task["attempt"] == 2
    assert task["recovery_count"] == 1
    assert task["worker_id"] == "replacement-worker"
    assert task["result"]

    event_types = [
        event["kind"]
        for event in store.events()
        if event["task_id"] == created["id"]
    ]
    assert "recovered" in event_types
    assert event_types[0] == "succeeded"
