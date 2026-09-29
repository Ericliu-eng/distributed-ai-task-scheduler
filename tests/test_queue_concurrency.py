import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.store import SchedulerStore


POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set TEST_DATABASE_URL to run the PostgreSQL concurrency test",
)
def test_eight_workers_claim_one_hundred_tasks_without_duplicates():
    """Exercise real SKIP LOCKED claims through independent DB connections."""
    setup_store = SchedulerStore(POSTGRES_TEST_URL)
    setup_store.init()
    setup_store.reset()

    created_ids = {
        setup_store.create_task(
            f"concurrency workload {index}",
            priority=(index % 10) + 1,
            idempotency_key=f"concurrency-{index}",
        )[0]["id"]
        for index in range(100)
    }

    claimed_ids: list[str] = []
    claimed_ids_lock = threading.Lock()

    def consume(worker_number: int, tier: str) -> None:
        store = SchedulerStore(POSTGRES_TEST_URL)
        worker_id = f"concurrency-{tier}-{worker_number}"
        while True:
            task = store.claim(worker_id, tier, lease_seconds=30)
            if task is None:
                return
            with claimed_ids_lock:
                claimed_ids.append(task["id"])
            assert store.finish(
                task["id"], worker_id, task["attempt"], f"completed by {worker_id}"
            )

    workers = [
        (number, tier)
        for tier in ("small", "large")
        for number in range(4)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(consume, number, tier) for number, tier in workers]
        for future in futures:
            future.result()

    final_tasks = setup_store.list_tasks(limit=200)
    assert len(claimed_ids) == 100
    assert len(set(claimed_ids)) == 100
    assert set(claimed_ids) == created_ids
    assert all(task["status"] == "succeeded" for task in final_tasks)
    assert all(task["attempt"] == 1 for task in final_tasks)

