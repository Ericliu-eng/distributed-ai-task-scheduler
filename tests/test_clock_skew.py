import os
from datetime import datetime, timedelta, timezone

import pytest

from app import store as store_module
from app.store import SchedulerStore


POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set TEST_DATABASE_URL to run the PostgreSQL clock-skew test",
)
def test_slow_worker_clock_does_not_expire_a_fresh_lease(monkeypatch):
    """A worker whose clock runs 60 s behind must still get a full lease.

    If lease deadlines came from the worker's clock, a 15 s lease would already
    look 45 s expired to a monitor with a correct clock, and live work would be
    recovered and run twice.
    """
    store = SchedulerStore(POSTGRES_TEST_URL)
    store.init()
    store.reset()
    task, _ = store.create_task("skewed worker")

    real_utcnow = store_module.utcnow
    monkeypatch.setattr(store_module, "utcnow", lambda: real_utcnow() - timedelta(seconds=60))
    claimed = store.claim("slow-clock-worker", task["route_tier"], lease_seconds=15)
    assert store.renew_lease(task["id"], "slow-clock-worker", claimed["attempt"], 15)
    monkeypatch.setattr(store_module, "utcnow", real_utcnow)

    monitor_now = datetime.now(timezone.utc)
    assert store.requeue_stale_tasks(now=monitor_now) == []
    lease_until = datetime.fromisoformat(store.get_task(task["id"])["lease_until"])
    assert lease_until > monitor_now + timedelta(seconds=10)
    store.engine.dispose()
