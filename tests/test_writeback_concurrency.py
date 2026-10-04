"""Force ownership changes inside the writeback race, using separate connections."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.schema import CreateSchema, DropSchema

from app.store import Base, Event, SchedulerStore, Task


@pytest.fixture(params=["sqlite", "postgresql"])
def stores(request, tmp_path):
    """Use a fresh SQLite file or a unique schema in the explicit test database."""
    schema = None
    if request.param == "postgresql":
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("set TEST_DATABASE_URL to run PostgreSQL writeback races")
        schema = f"writeback_test_{uuid.uuid4().hex}"
    else:
        url = f"sqlite:///{(tmp_path / 'writeback.db').as_posix()}"

    instances = [SchedulerStore(url) for _ in range(3)]
    try:
        if schema:
            # Avoid resetting or claiming tasks from other tests in this database.
            with instances[0].engine.begin() as connection:
                connection.execute(CreateSchema(schema))
            for store in instances:
                store.engine = store.engine.execution_options(
                    schema_translate_map={None: schema}
                )
                store.Session.configure(bind=store.engine)
            Base.metadata.create_all(instances[0].engine)
        else:
            instances[0].init()
        yield instances
    finally:
        try:
            if schema:
                with instances[0].engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            for store in instances:
                store.engine.dispose()


def claimed_task(store, *, final_attempt=False, expired=False):
    task, _ = store.create_task("writeback concurrency test")
    if final_attempt:
        with store.Session.begin() as session:
            session.execute(
                update(Task).where(Task.id == task["id"]).values(max_attempts=1)
            )
    claimed = store.claim(
        "worker-original", task["route_tier"], lease_seconds=-1 if expired else 60
    )
    assert claimed is not None
    return claimed


def task_events(store, task_id):
    with store.Session() as session:
        return [
            (row.kind, row.message)
            for row in session.scalars(
                select(Event).where(Event.task_id == task_id).order_by(Event.id)
            )
        ]


def intercept_first_write(store, callback):
    """Pause before a statement acquires a write lock, after any ownership reads.

    The old ORM implementation flushed its event before its task update. Catching
    the first mutation works for it and for the atomic UPDATE implementation.
    """
    intercepted = []

    def before_execute(connection, cursor, statement, parameters, context, many):
        if not intercepted and (context.isupdate or context.isinsert):
            intercepted.append(True)
            callback()

    event.listen(store.engine, "before_cursor_execute", before_execute)
    return before_execute, intercepted


@pytest.mark.parametrize("operation", ["finish", "fail"])
@pytest.mark.parametrize("reuse_worker_id", [False, True])
def test_recovery_between_read_and_write_fences_old_attempt(
    stores, operation, reuse_worker_id
):
    stale, recovery, observer = stores
    old = claimed_task(stale, expired=True)
    replacement_worker = old["worker_id"] if reuse_worker_id else "worker-replacement"
    replacement_state = []
    replacement_events = []

    def recover_and_claim():
        assert recovery.requeue_stale_tasks() == [old["id"]]
        replacement = recovery.claim(replacement_worker, old["route_tier"])
        assert replacement["id"] == old["id"]
        assert replacement["attempt"] == old["attempt"] + 1
        replacement_state.append(observer.get_task(old["id"]))
        replacement_events.extend(task_events(observer, old["id"]))

    hook, intercepted = intercept_first_write(stale, recover_and_claim)
    try:
        if operation == "finish":
            outcome = stale.finish(
                old["id"], old["worker_id"], old["attempt"], "stale result"
            )
            assert outcome is False
        else:
            outcome = stale.fail_or_retry(
                old["id"], old["worker_id"], old["attempt"], "stale failure"
            )
            assert outcome == "fenced"
    finally:
        event.remove(stale.engine, "before_cursor_execute", hook)

    assert intercepted == [True]
    assert observer.get_task(old["id"]) == replacement_state[0]
    assert task_events(observer, old["id"]) == replacement_events
    assert replacement_state[0]["status"] == "running"
    assert replacement_state[0]["worker_id"] == replacement_worker
    assert replacement_state[0]["result"] is None
    assert replacement_state[0]["error"] is None


@pytest.mark.parametrize("final_attempt", [False, True])
def test_competing_success_and_failure_have_exactly_one_winner(stores, final_attempt):
    success_store, failure_store, observer = stores
    claimed = claimed_task(observer, final_attempt=final_attempt)
    ready_to_write = threading.Barrier(2, timeout=10)
    hooks = [
        (store, *intercept_first_write(store, ready_to_write.wait))
        for store in (success_store, failure_store)
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            success = executor.submit(
                success_store.finish,
                claimed["id"], claimed["worker_id"], claimed["attempt"], "winner result",
            )
            failure = executor.submit(
                failure_store.fail_or_retry,
                claimed["id"], claimed["worker_id"], claimed["attempt"], "winner error",
            )
            success_result = success.result(timeout=30)
            failure_result = failure.result(timeout=30)
    finally:
        for store, hook, _ in hooks:
            event.remove(store.engine, "before_cursor_execute", hook)

    assert all(intercepted == [True] for _, _, intercepted in hooks)
    assert int(success_result) + int(failure_result != "fenced") == 1
    current = observer.get_task(claimed["id"])
    writeback_events = [
        kind for kind, _ in task_events(observer, claimed["id"])
        if kind in {"succeeded", "retry", "failed"}
    ]
    assert current["attempt"] == claimed["attempt"]
    assert current["lease_until"] is None
    if success_result:
        assert failure_result == "fenced"
        assert current["status"] == "succeeded"
        assert current["result"] == "winner result"
        assert current["error"] is None
        assert current["worker_id"] == claimed["worker_id"]
        assert current["finished_at"] is not None
        assert writeback_events == ["succeeded"]
    else:
        expected = "failed" if final_attempt else "queued"
        assert failure_result == expected
        assert current["status"] == expected
        assert current["result"] is None
        assert current["error"] == "winner error"
        assert current["worker_id"] is None
        assert (current["finished_at"] is not None) == final_attempt
        assert writeback_events == ["failed" if final_attempt else "retry"]


@pytest.mark.parametrize("operation", ["finish", "retry", "exhausted"])
def test_event_insert_failure_rolls_back_writeback(stores, operation):
    writer, _, observer = stores
    claimed = claimed_task(writer, final_attempt=operation == "exhausted")
    before = observer.get_task(claimed["id"])
    events_before = task_events(observer, claimed["id"])

    def reject_event(connection, cursor, statement, parameters, context, many):
        if context.isinsert and context.compiled.statement.table.name == "events":
            raise RuntimeError("injected event persistence failure")

    event.listen(writer.engine, "before_cursor_execute", reject_event)
    try:
        with pytest.raises(RuntimeError, match="injected event persistence failure"):
            if operation == "finish":
                writer.finish(claimed["id"], claimed["worker_id"], claimed["attempt"], "ok")
            else:
                writer.fail_or_retry(
                    claimed["id"], claimed["worker_id"], claimed["attempt"], "unavailable"
                )
    finally:
        event.remove(writer.engine, "before_cursor_execute", reject_event)

    assert observer.get_task(claimed["id"]) == before
    assert task_events(observer, claimed["id"]) == events_before


@pytest.mark.parametrize("operation", ["finish", "retry", "exhausted"])
def test_expiration_without_recovery_keeps_current_attempt_authorized(stores, operation):
    writer, _, observer = stores
    claimed = claimed_task(writer, final_attempt=operation == "exhausted", expired=True)
    if operation == "finish":
        assert writer.finish(
            claimed["id"], claimed["worker_id"], claimed["attempt"], "still owned"
        ) is True
        expected = "succeeded"
    else:
        expected = "failed" if operation == "exhausted" else "queued"
        assert writer.fail_or_retry(
            claimed["id"], claimed["worker_id"], claimed["attempt"], "still owned"
        ) == expected
    current = observer.get_task(claimed["id"])
    assert current["status"] == expected
    assert current["lease_until"] is None
    assert (current["finished_at"] is not None) == (expected != "queued")
    events_before_duplicate = task_events(observer, claimed["id"])

    # A duplicate report must not rewrite the accepted outcome or add an event.
    assert writer.finish(
        claimed["id"], claimed["worker_id"], claimed["attempt"], "duplicate result"
    ) is False
    assert writer.fail_or_retry(
        claimed["id"], claimed["worker_id"], claimed["attempt"], "duplicate failure"
    ) == "fenced"
    assert observer.get_task(claimed["id"]) == current
    assert task_events(observer, claimed["id"]) == events_before_duplicate
