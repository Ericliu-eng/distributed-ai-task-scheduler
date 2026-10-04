from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, text

from app.store import SchedulerStore, Task
from monitor.recovery import RecoveryMonitor


def make_store():
    store = SchedulerStore("sqlite:///:memory:")
    store.init()
    return store


def test_claim_creates_and_owner_renews_lease():
    store = make_store()
    task, _ = store.create_task("lease protected task")
    claimed = store.claim("worker-a", "small", lease_seconds=10)
    assert claimed["id"] == task["id"]
    first_lease = datetime.fromisoformat(claimed["lease_until"])

    assert store.renew_lease(task["id"], "wrong-worker", claimed["attempt"], 20) is False
    assert store.renew_lease(task["id"], "worker-a", claimed["attempt"] + 1, 20) is False
    assert store.renew_lease(task["id"], "worker-a", claimed["attempt"], 20) is True

    renewed = datetime.fromisoformat(store.get_task(task["id"])["lease_until"])
    assert renewed > first_lease


def test_expired_task_is_requeued_and_stale_write_is_fenced():
    store = make_store()
    task, _ = store.create_task("recover this task")
    old_claim = store.claim("worker-old", "small", lease_seconds=10)

    monitor = RecoveryMonitor(store)
    with store.Session.begin() as session:
        row = session.get(Task, task["id"])
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    recovered = monitor.run_once()
    assert recovered == [task["id"]]
    queued = store.get_task(task["id"])
    assert queued["status"] == "queued"
    assert queued["worker_id"] is None
    assert queued["lease_until"] is None
    assert queued["recovery_count"] == 1

    new_claim = store.claim("worker-new", "small", lease_seconds=10)
    assert new_claim["attempt"] == 2
    assert store.finish(task["id"], "worker-new", 2, "new result") is True
    assert store.finish(task["id"], "worker-old", old_claim["attempt"], "stale result") is False
    completed = store.get_task(task["id"])
    assert completed["result"] == "new result"
    assert completed["recovery_count"] == 1


def test_startup_migrates_existing_mvp_database(tmp_path):
    path = tmp_path / "old-mvp.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    engine.dispose()

    store = SchedulerStore(f"sqlite:///{path}")
    store.init()
    columns = {column["name"] for column in inspect(store.engine).get_columns("tasks")}
    assert {"lease_until", "recovery_count", "next_attempt_at"}.issubset(columns)
