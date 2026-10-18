from datetime import datetime, timezone

from app.store import SchedulerStore, Task
from worker.main import WorkerService


def make_store():
    store = SchedulerStore("sqlite:///:memory:")
    store.init()
    return store


def test_independent_worker_processes_the_matching_tier():
    store = make_store()
    task, _ = store.create_task("explain database indexes", priority=7)
    worker = WorkerService(store, "worker-small-test", "small")
    assert worker.run_once(delay=False) is True
    completed = store.get_task(task["id"])
    assert completed["status"] == "succeeded"
    assert completed["worker_id"] == "worker-small-test"
    assert completed["attempt"] == 1


def test_worker_retries_transient_failure():
    store = make_store()
    task, _ = store.create_task("transient failure", fail_once=True)
    worker = WorkerService(store, "worker-small-test", "small")
    assert worker.run_once(delay=False) is True
    assert store.get_task(task["id"])["status"] == "queued"
    with store.Session.begin() as session:
        session.get(Task, task["id"]).next_attempt_at = datetime.now(timezone.utc)
    assert worker.run_once(delay=False) is True
    assert store.get_task(task["id"])["status"] == "succeeded"
    assert store.get_task(task["id"])["attempt"] == 2


def count_transactions(store):
    """Count database transactions the store begins from now on."""
    from sqlalchemy import event

    began = []
    event.listen(store.engine, "begin", lambda connection: began.append(1))
    return began


def test_one_task_costs_two_transactions():
    """Claim and write-back carry the heartbeats; nothing else touches the database."""
    store = make_store()
    store.create_task("first task")
    store.create_task("second task")
    worker = WorkerService(store, "worker-small-test", "small", renew_interval=60)
    assert worker.run_once(delay=False) is True  # the first loop also writes an idle heartbeat

    began = count_transactions(store)
    assert worker.run_once(delay=False) is True
    assert len(began) == 2


def test_claim_and_write_back_keep_worker_state_current():
    store = make_store()
    task, _ = store.create_task("track worker state")
    store.heartbeat("worker-small-test", "small", "idle")

    claimed = store.claim("worker-small-test", "small")
    worker = store.list_workers()[0]
    assert (worker["status"], worker["task_id"]) == ("busy", task["id"])
    assert claimed["fail_once"] is False

    assert store.finish(task["id"], "worker-small-test", claimed["attempt"], "ok")
    worker = store.list_workers()[0]
    assert (worker["status"], worker["task_id"]) == ("idle", None)


def test_fenced_write_back_still_marks_worker_idle():
    store = make_store()
    task, _ = store.create_task("stale owner")
    claimed = store.claim("worker-small-test", "small")
    assert not store.finish(task["id"], "worker-small-test", claimed["attempt"] + 1, "late")
    worker = store.list_workers()[0]
    assert (worker["status"], worker["task_id"]) == ("idle", None)


def test_claim_registers_an_unknown_worker():
    store = make_store()
    task, _ = store.create_task("first contact")
    store.claim("worker-new", "small")
    assert [(w["id"], w["tier"], w["status"]) for w in store.list_workers()] == [
        ("worker-new", "small", "busy")
    ]
