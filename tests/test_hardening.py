from datetime import datetime, timedelta, timezone
import threading

from fastapi import HTTPException, Response
import pytest
from sqlalchemy import inspect

from app import api
from app.store import SchedulerStore, Task, as_utc, task_dict
from bench import evaluate
from monitor.recovery import RecoveryMonitor
from worker.main import WorkerService


PACIFIC = timezone(timedelta(hours=-7))


def make_store():
    store = SchedulerStore("sqlite:///:memory:")
    store.init()
    return store


def test_timestamps_from_a_non_utc_session_are_normalized():
    # PostgreSQL returns TIMESTAMPTZ in the connection's TimeZone, not always UTC.
    local = datetime(2026, 10, 4, 10, 0, tzinfo=PACIFIC)
    assert as_utc(local) == datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)
    assert as_utc(datetime(2026, 10, 4, 17, 0)) == datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)

    task = Task(id="t", prompt="p", sla="standard", priority=5, route_tier="small",
                route_reason="r", difficulty_score=0, estimated_cost=0.003, status="succeeded",
                attempt=1, max_attempts=3, recovery_count=0,
                created_at=datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc),
                started_at=datetime(2026, 10, 4, 10, 0, 2, tzinfo=PACIFIC),
                finished_at=datetime(2026, 10, 4, 10, 0, 3, tzinfo=PACIFIC))
    serialized = task_dict(task)
    assert (serialized["queue_ms"], serialized["run_ms"]) == (2000, 1000)


def test_init_creates_claim_and_lease_indexes():
    store = make_store()
    names = {index["name"] for index in inspect(store.engine).get_indexes("tasks")}
    assert {"ix_tasks_claim", "ix_tasks_lease"} <= names
    store.init()  # idempotent on an existing database


def test_metrics_aggregate_in_sql():
    store = make_store()
    done, _ = store.create_task("finish me")
    retried, _ = store.create_task("retry me", fail_once=True)
    store.create_task("x" * 900)  # routed large, left queued

    first = store.claim("w", "small")
    store.finish(first["id"], "w", first["attempt"], "ok")
    second = store.claim("w", "small")
    store.fail_or_retry(second["id"], "w", second["attempt"], "503")
    with store.Session.begin() as session:
        session.get(Task, retried["id"]).next_attempt_at = datetime.now(timezone.utc)
    third = store.claim("w", "small")
    store.finish(third["id"], "w", third["attempt"], "ok")

    metrics = store.metrics()
    assert metrics["total"] == 3
    assert (metrics["succeeded"], metrics["queued"], metrics["failed"]) == (2, 1, 0)
    assert (metrics["small"], metrics["large"]) == (2, 1)
    assert metrics["retries"] == 1
    assert metrics["estimated_cost"] == pytest.approx(0.003 * 2 + 0.018)
    assert metrics["cost_saved_pct"] == pytest.approx((1 - 0.024 / 0.054) * 100, abs=0.1)
    assert metrics["avg_latency_ms"] >= 0
    assert done["id"] in {first["id"], third["id"]}


def test_empty_metrics():
    metrics = make_store().metrics()
    assert metrics["total"] == 0
    assert metrics["avg_latency_ms"] == 0
    assert metrics["cost_saved_pct"] == 0


class FlakyStore:
    """Raises on the first claims, then lets the loop run until stopped."""

    def __init__(self, failures: int):
        self.failures = failures
        self.claims = 0
        self.offline = threading.Event()

    def init(self): pass
    def heartbeat(self, *args, **kwargs): pass

    def claim(self, *args, **kwargs):
        self.claims += 1
        if self.claims <= self.failures:
            raise RuntimeError("database unavailable")
        return None

    def mark_worker_offline(self, worker_id):
        self.offline.set()


def test_worker_loop_survives_database_errors():
    store = FlakyStore(failures=2)
    worker = WorkerService(store, "w", "small", poll_interval=0.001)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    deadline = datetime.now() + timedelta(seconds=5)
    while store.claims < 4 and datetime.now() < deadline:
        threading.Event().wait(0.01)
    worker.stop()
    thread.join(timeout=5)
    assert store.claims >= 4
    assert store.offline.is_set()


def test_monitor_loop_survives_database_errors():
    calls = []

    class Store:
        def init(self): pass

        def requeue_stale_tasks(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database unavailable")
            return []

    monitor = RecoveryMonitor(Store(), interval=0.001)
    thread = threading.Thread(target=monitor.run_forever)
    thread.start()
    deadline = datetime.now() + timedelta(seconds=5)
    while len(calls) < 3 and datetime.now() < deadline:
        threading.Event().wait(0.01)
    monitor.stop()
    thread.join(timeout=5)
    assert len(calls) >= 3


def test_demo_endpoints_require_demo_mode(monkeypatch):
    monkeypatch.setattr(api, "store", make_store())
    monkeypatch.setattr(api, "DEMO_MODE", False)
    with pytest.raises(HTTPException) as error:
        api.reset_demo()
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        api.create_task(api.TaskCreate(prompt="fail please", fail_once=True), Response())
    created = api.create_task(api.TaskCreate(prompt="normal task"), Response())
    assert created["created"] is True
    assert api.health()["demo_mode"] is False

    monkeypatch.setattr(api, "DEMO_MODE", True)
    assert api.reset_demo()["seeded"] == 5


def test_code_grader_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(evaluate, "CODE_TIMEOUT_SECONDS", 1)
    case = {case.id: case for case in evaluate.load_cases()}["code-001"]
    assert evaluate.grade(case, "def solve(x):\n    while True:\n        pass") is False
    assert evaluate.grade(case, "import os\ndef solve(x):\n    return x + 1") is False
