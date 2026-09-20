from app.store import SchedulerStore
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
    assert worker.run_once(delay=False) is True
    assert store.get_task(task["id"])["status"] == "succeeded"
    assert store.get_task(task["id"])["attempt"] == 2
