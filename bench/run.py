from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import tempfile
import time
import uuid

from sqlalchemy import func, select, update

from app.router import TIER_COST
from app.store import Event, SchedulerStore, Task
from worker.main import WorkerService


def percentile(values: list[float], percent: float) -> float:
    """Return a linearly interpolated percentile without external dependencies."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
    }


def run_benchmark(database_url: str, task_count: int = 200,
                  worker_count: int = 8) -> dict:
    if task_count < 1:
        raise ValueError("task_count must be at least 1")
    if worker_count < 2:
        raise ValueError("worker_count must be at least 2")

    setup_store = SchedulerStore(database_url)
    setup_store.init()
    setup_store.reset()
    run_id = uuid.uuid4().hex[:12]

    tier_task_ids: dict[str, list[str]] = {"small": [], "large": []}
    for index in range(task_count):
        prompt = f"benchmark request {index}"
        if index % 2:
            prompt += " " + ("multi-step capacity analysis " * 32)
        task, _ = setup_store.create_task(
            prompt,
            priority=(index % 10) + 1,
            idempotency_key=f"benchmark-{run_id}-{index}",
        )
        tier_task_ids["small" if index % 2 == 0 else "large"].append(task["id"])

    # Queue throughput should use every worker evenly. Router quality and cost
    # are evaluated separately, so this synthetic workload pins a 50/50 tier mix.
    with setup_store.Session.begin() as session:
        for tier, task_ids in tier_task_ids.items():
            for offset in range(0, len(task_ids), 500):
                session.execute(
                    update(Task).where(Task.id.in_(task_ids[offset:offset + 500])).values(
                        route_tier=tier,
                        route_reason="Benchmark workload: forced balanced tier distribution",
                        estimated_cost=TIER_COST[tier],
                    )
                )

    worker_specs = [
        (f"benchmark-worker-{index}", "small" if index % 2 == 0 else "large")
        for index in range(worker_count)
    ]

    def consume(worker_id: str, tier: str) -> int:
        worker_store = SchedulerStore(database_url)
        try:
            service = WorkerService(
                worker_store,
                worker_id=worker_id,
                tier=tier,
                lease_seconds=30,
                renew_interval=5,
            )
            completed = 0
            while service.run_once(delay=False):
                completed += 1
            return completed
        finally:
            worker_store.engine.dispose()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(consume, *spec) for spec in worker_specs]
        completed_by_workers = [future.result() for future in futures]
    duration_seconds = time.perf_counter() - started

    tasks = setup_store.list_tasks(limit=task_count + 10)
    # Every claim writes an event in the same transaction, so a task claimed more
    # than once (or retried) would show more than one claim event.
    with setup_store.Session() as session:
        claims_per_task = session.execute(
            select(func.count()).where(Event.kind == "claimed").group_by(Event.task_id)
        ).scalars().all()
    duplicate_claims = sum(count - 1 for count in claims_per_task if count > 1)
    succeeded = [task for task in tasks if task["status"] == "succeeded"]
    queue_latencies = [float(task["queue_ms"]) for task in succeeded]
    total_latencies = [
        float(task["queue_ms"] + task["run_ms"])
        for task in succeeded
        if task["queue_ms"] is not None and task["run_ms"] is not None
    ]
    result = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "database": setup_store.engine.dialect.name,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tasks_submitted": task_count,
        "tasks_succeeded": len(succeeded),
        "workers": worker_count,
        "worker_mode": "threads in one process, each with its own database connection pool",
        "duplicate_claims": duplicate_claims,
        "tasks_with_single_attempt": sum(task["attempt"] == 1 for task in tasks),
        "execution_mode": "mock inference without provider delay",
        "workload": "burst submission with forced 50/50 tier distribution",
        "worker_completions": completed_by_workers,
        "duration_seconds": round(duration_seconds, 4),
        "throughput_tasks_per_second": round(
            len(succeeded) / duration_seconds if duration_seconds else 0, 2
        ),
        "queue_latency_ms": summarize(queue_latencies),
        "total_latency_ms": summarize(total_latencies),
        "routing": {
            "small": sum(task["route_tier"] == "small" for task in tasks),
            "large": sum(task["route_tier"] == "large" for task in tasks),
        },
    }
    setup_store.engine.dispose()
    if len(succeeded) != task_count:
        raise RuntimeError(
            f"benchmark incomplete: {len(succeeded)}/{task_count} tasks succeeded"
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark scheduler queue performance")
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--allow-reset", action="store_true",
                        help="allow deleting existing rows in an explicitly configured database")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.getLogger("scheduler-worker").setLevel(logging.WARNING)
    if args.database_url and not args.allow_reset:
        raise SystemExit(
            "An explicit benchmark database is reset before each run. "
            "Pass --allow-reset only after verifying it is a disposable database."
        )

    if args.database_url:
        result = run_benchmark(args.database_url, args.tasks, args.workers)
    else:
        with tempfile.TemporaryDirectory(prefix="scheduler-benchmark-") as directory:
            database_url = f"sqlite:///{Path(directory, 'benchmark.db').as_posix()}"
            result = run_benchmark(database_url, args.tasks, args.workers)

    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
