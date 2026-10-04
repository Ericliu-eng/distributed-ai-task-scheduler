from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from app.store import Base, SchedulerStore, Task, retry_delay_seconds


POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")


def make_store(tmp_path, *, base=2.0, cap=10.0):
    url = f"sqlite:///{(tmp_path / 'retry.db').as_posix()}"
    store = SchedulerStore(url, retry_base_seconds=base, retry_max_seconds=cap)
    store.init()
    return store


def test_backoff_is_exponential_capped_and_jittered_per_task():
    first = [retry_delay_seconds("task-a", attempt, 2, 10) for attempt in range(1, 6)]
    repeated = [retry_delay_seconds("task-a", attempt, 2, 10) for attempt in range(1, 6)]
    other = [retry_delay_seconds("task-b", attempt, 2, 10) for attempt in range(1, 6)]

    assert first == repeated
    assert first != other
    for attempt, delay in enumerate(first, start=1):
        ceiling = min(10, 2 * 2 ** (attempt - 1))
        assert ceiling * 0.5 <= delay <= ceiling
    assert [min(10, 2 * 2 ** (attempt - 1)) for attempt in range(1, 6)] == [2, 4, 8, 10, 10]


@pytest.mark.parametrize("attempt,base,cap", [(0, 1, 10), (1, 0, 10), (1, 1, 0)])
def test_backoff_rejects_invalid_settings(attempt, base, cap):
    with pytest.raises(ValueError):
        retry_delay_seconds("task", attempt, base, cap)


def assert_retry_eligibility(store):
    task, _ = store.create_task("retry after a provider outage")
    first = store.claim("worker-a", task["route_tier"])

    before = datetime.now(timezone.utc)
    assert store.fail_or_retry(task["id"], "worker-a", first["attempt"], "HTTP 503") == "queued"
    queued = store.get_task(task["id"])
    retry_at = datetime.fromisoformat(queued["next_attempt_at"]).replace(tzinfo=timezone.utc)
    delay = (retry_at - before).total_seconds()
    assert 1 <= delay <= 2.1
    assert store.claim("worker-b", task["route_tier"]) is None

    with store.Session.begin() as session:
        session.get(Task, task["id"]).next_attempt_at = datetime.now(timezone.utc) - timedelta(milliseconds=1)
    second = store.claim("worker-b", task["route_tier"])
    assert second["attempt"] == 2
    assert second["next_attempt_at"] is None

    retry_events = [event for event in store.events() if event["kind"] == "retry"]
    assert len(retry_events) == 1
    assert "retry in " in retry_events[0]["message"]


def test_retry_is_not_claimable_before_next_attempt_and_becomes_eligible_after(tmp_path):
    assert_retry_eligibility(make_store(tmp_path))


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set TEST_DATABASE_URL to run PostgreSQL retry eligibility",
)
def test_postgresql_retry_eligibility():
    schema = f"retry_test_{uuid.uuid4().hex}"
    store = SchedulerStore(POSTGRES_TEST_URL, retry_base_seconds=2, retry_max_seconds=10)
    try:
        with store.engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        store.engine = store.engine.execution_options(schema_translate_map={None: schema})
        store.Session.configure(bind=store.engine)
        Base.metadata.create_all(store.engine)
        assert_retry_eligibility(store)
    finally:
        with store.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        store.engine.dispose()


def test_terminal_failure_has_no_scheduled_retry(tmp_path):
    store = make_store(tmp_path)
    task, _ = store.create_task("fail after one attempt")
    with store.Session.begin() as session:
        session.get(Task, task["id"]).max_attempts = 1
    claim = store.claim("worker-a", task["route_tier"])

    assert store.fail_or_retry(task["id"], "worker-a", claim["attempt"], "fatal") == "failed"
    failed = store.get_task(task["id"])
    assert failed["next_attempt_at"] is None
    assert failed["finished_at"] is not None
