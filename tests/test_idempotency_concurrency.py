import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.store import Event, SchedulerStore, Task


POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")


class SynchronizedSubmitStore(SchedulerStore):
    """Make every submitter observe the missing key before any may insert."""

    def __init__(self, database_url, barrier):
        super().__init__(database_url)
        self.barrier = barrier

    def _depths(self, session):
        depths = super()._depths(session)
        self.barrier.wait(timeout=10)
        return depths


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'idempotency.db').as_posix()}"
    store = SchedulerStore(url)
    store.init()
    try:
        yield url, store
    finally:
        store.engine.dispose()


def concurrent_submissions(url, key):
    barrier = threading.Barrier(4)
    stores = [SynchronizedSubmitStore(url, barrier) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            futures = [
                executor.submit(
                    store.create_task,
                    f"Concurrent submission {index}",
                    idempotency_key=key,
                )
                for index, store in enumerate(stores)
            ]
            return [future.result(timeout=30) for future in futures]
    finally:
        for store in stores:
            store.engine.dispose()


def assert_committed_task(response, persisted):
    """Defaults must be usable in the POST response, before a later GET."""
    assert response["status"] == "queued"
    assert response["attempt"] == 0
    assert response["recovery_count"] == 0
    assert response["max_attempts"] == 3
    assert response["created_at"] is not None
    # SQLite drops timezone metadata on loading a committed timestamp.
    response_time = datetime.fromisoformat(response["created_at"]).replace(tzinfo=timezone.utc)
    persisted_time = datetime.fromisoformat(persisted["created_at"]).replace(tzinfo=timezone.utc)
    assert response_time == persisted_time
    assert {key: value for key, value in response.items() if key != "created_at"} == {
        key: value for key, value in persisted.items() if key != "created_at"
    }


def assert_one_submission(store, key, responses):
    assert sorted(created for _, created in responses) == [False, False, False, True]
    ids = {task["id"] for task, _ in responses}
    assert len(ids) == 1
    task_id = ids.pop()
    persisted = store.get_task(task_id)
    assert persisted is not None
    for task, _ in responses:
        assert_committed_task(task, persisted)
    with store.Session() as session:
        tasks = session.scalars(select(Task).where(Task.idempotency_key == key)).all()
        events = session.scalars(select(Event).where(Event.task_id == task_id)).all()
        assert [task.id for task in tasks] == [task_id]
        assert [event.kind for event in events] == ["routed"]
    repeated, created = store.create_task("Later request with the same key", idempotency_key=key)
    assert created is False
    assert_committed_task(repeated, persisted)


@pytest.mark.parametrize("key", ["same-request", ""], ids=["nonempty-key", "empty-key"])
def test_concurrent_idempotent_submissions_return_one_committed_task(database, key):
    url, store = database
    assert_one_submission(store, key, concurrent_submissions(url, key))


def test_submissions_without_an_idempotency_key_remain_distinct(database):
    url, store = database
    responses = concurrent_submissions(url, None)
    assert all(created for _, created in responses)
    ids = {task["id"] for task, _ in responses}
    assert len(ids) == 4
    for task, _ in responses:
        assert_committed_task(task, store.get_task(task["id"]))
    with store.Session() as session:
        events = session.scalars(select(Event).where(Event.task_id.in_(ids))).all()
        assert len(events) == 4
        assert all(event.kind == "routed" for event in events)


def test_unrelated_integrity_error_is_not_treated_as_idempotency(database, monkeypatch):
    _, store = database
    existing, _ = store.create_task("Existing task without a key")
    # Force a primary-key collision instead of an idempotency-key collision.
    monkeypatch.setattr("app.store.uuid.uuid4", lambda: uuid.UUID(existing["id"]))
    with pytest.raises(IntegrityError):
        store.create_task("Must not be accepted", idempotency_key="unrelated-key")
    assert [task["id"] for task in store.list_tasks()] == [existing["id"]]
    assert [event["kind"] for event in store.events()] == ["routed"]


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set TEST_DATABASE_URL to run the PostgreSQL idempotency race",
)
def test_postgresql_concurrent_idempotent_submissions():
    store = SchedulerStore(POSTGRES_TEST_URL)
    store.init()
    key = f"idempotency-race-{uuid.uuid4()}"
    try:
        assert_one_submission(store, key, concurrent_submissions(POSTGRES_TEST_URL, key))
    finally:
        # Clean up only this case's data, even when a submission assertion fails.
        with store.Session.begin() as session:
            ids = session.scalars(select(Task.id).where(Task.idempotency_key == key)).all()
            if ids:
                session.execute(delete(Event).where(Event.task_id.in_(ids)))
                session.execute(delete(Task).where(Task.id.in_(ids)))
        store.engine.dispose()
