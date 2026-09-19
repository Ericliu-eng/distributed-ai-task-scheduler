from app.store import SchedulerStore

def make_store():
    store = SchedulerStore("sqlite:///:memory:")
    store.init()
    return store

def test_idempotency_returns_same_task():
    store = make_store()
    first, created_first = store.create_task("a valid prompt", idempotency_key="same")
    second, created_second = store.create_task("a different prompt", idempotency_key="same")
    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]

def test_priority_and_fencing():
    store = make_store()
    low, _ = store.create_task("low priority task", priority=1)
    high, _ = store.create_task("high priority task", priority=10)
    claimed = store.claim("worker-small-01", "small")
    assert claimed["id"] == high["id"]
    assert store.finish(claimed["id"], "wrong-worker", claimed["attempt"], "stale") is False
    assert store.finish(claimed["id"], "worker-small-01", claimed["attempt"], "done") is True
    assert store.get_task(low["id"])["status"] == "queued"

def test_retry_then_success():
    store = make_store()
    task, _ = store.create_task("retry this task", fail_once=True)
    claim1 = store.claim("worker-small-01", "small")
    assert store.fail_or_retry(task["id"], "worker-small-01", claim1["attempt"], "503") == "queued"
    claim2 = store.claim("worker-small-01", "small")
    assert claim2["attempt"] == 2
    assert store.finish(task["id"], "worker-small-01", 2, "ok") is True
