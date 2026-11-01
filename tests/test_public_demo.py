from datetime import datetime, timedelta, timezone
import os

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from app import api
from app.limits import RateLimiter
from app.store import Event, SchedulerStore, Task, Workflow
from monitor.recovery import RecoveryMonitor


def request_from(host):
    return Request({"type": "http", "client": (host, 50000), "headers": []})


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_rate_limiter_allows_a_burst_then_refills_per_client():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, clock=clock)
    assert [limiter.acquire("a") for _ in range(3)] == [0, 0, 0]
    assert limiter.acquire("a") == pytest.approx(20.0)  # one token every 20 s
    assert limiter.acquire("b") == 0  # clients are limited independently
    clock.now = 20.0
    assert limiter.acquire("a") == 0


def test_rate_limiter_forgets_the_oldest_clients_past_its_bound():
    limiter = RateLimiter(per_minute=1, max_clients=2, clock=FakeClock())
    for client in ("a", "b", "c"):
        limiter.acquire(client)
    assert list(limiter._buckets) == ["b", "c"]


@pytest.fixture
def public_api(monkeypatch):
    monkeypatch.setattr(api, "store", SchedulerStore("sqlite:///:memory:"))
    api.store.init()
    monkeypatch.setattr(api, "DEMO_MODE", True)
    return api


def test_submissions_are_rate_limited_per_client(public_api, monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMITER", RateLimiter(per_minute=2))
    api.guard_submission(request_from("10.0.0.1"))
    api.guard_submission(request_from("10.0.0.1"))
    with pytest.raises(HTTPException) as error:
        api.guard_submission(request_from("10.0.0.1"))
    assert error.value.status_code == 429 and int(error.value.headers["Retry-After"]) >= 1
    api.guard_submission(request_from("10.0.0.2"))


def test_submissions_are_refused_when_the_queue_is_full(public_api, monkeypatch):
    monkeypatch.setattr(api, "MAX_ACTIVE_TASKS", 2)
    api.store.create_task("first queued task")
    api.guard_submission(request_from("10.0.0.1"))
    api.store.create_task("second queued task")
    with pytest.raises(HTTPException) as error:
        api.guard_submission(request_from("10.0.0.1"))
    assert error.value.status_code == 503


def test_guards_are_attached_to_every_endpoint_that_creates_work():
    guarded = {route.path for route in api.app.routes
               if getattr(route, "methods", None) and "POST" in route.methods
               and any(dep.call is api.guard_submission for dep in route.dependant.dependencies)}
    assert guarded == {"/tasks", "/workflows", "/demo/workflow", "/demo/reset"}


def test_shared_deployments_can_disable_the_destructive_reset(public_api, monkeypatch):
    monkeypatch.setattr(api, "ALLOW_DEMO_RESET", False)
    with pytest.raises(HTTPException) as error:
        api.reset_demo()
    assert error.value.status_code == 403
    assert api.health()["demo_reset"] is False
    # Pipeline demos stay available.
    assert api.demo_workflow(scenario="retry")["status"] == "running"


def age(store, model, column, minutes):
    """Move a timestamp column back in time for every row of ``model``."""
    with store.Session.begin() as session:
        for row in session.query(model):
            if getattr(row, column) is not None:
                setattr(row, column, datetime.now(timezone.utc) - timedelta(minutes=minutes))


@pytest.mark.parametrize("url", ["sqlite:///:memory:"] + (
    [os.environ["TEST_DATABASE_URL"]] if os.getenv("TEST_DATABASE_URL") else []))
def test_purge_removes_only_old_finished_work(url):
    store = SchedulerStore(url)
    store.init()
    store.reset()
    old_task, _ = store.create_task("finished long ago")
    claimed = store.claim("w", "small")
    store.finish(old_task["id"], "w", claimed["attempt"], "ok")
    old_flow = store.create_workflow("old", [{"key": "only", "prompt": "single node"}])
    claimed = store.claim("w", "small")
    store.finish(claimed["id"], "w", claimed["attempt"], "ok")
    for model, column in ((Task, "finished_at"), (Workflow, "finished_at"), (Event, "created_at")):
        age(store, model, column, 90)

    fresh, _ = store.create_task("still queued")
    running = store.create_workflow("running", [{"key": "a", "prompt": "first node"},
                                                {"key": "b", "prompt": "second node", "depends_on": ["a"]}])
    removed = store.purge_finished(older_than_minutes=60)

    assert removed["workflows"] == 1 and removed["tasks"] == 2
    assert store.get_task(old_task["id"]) is None and store.get_workflow(old_flow["id"]) is None
    assert store.get_task(fresh["id"])["status"] == "queued"
    assert store.get_workflow(running["id"])["status"] == "running"


def test_monitor_purges_on_a_schedule_only_when_retention_is_set():
    calls = []

    class Store:
        def requeue_stale_tasks(self):
            return []

        def purge_finished(self, minutes):
            calls.append(minutes)
            return {"workflows": 0, "tasks": 0, "events": 0}

    RecoveryMonitor(Store()).run_once()
    assert calls == []
    monitor = RecoveryMonitor(Store(), retention_minutes=60, purge_every=3600)
    monitor.run_once()
    monitor.run_once()  # within purge_every: no second purge
    assert calls == [60]
