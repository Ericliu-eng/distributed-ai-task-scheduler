from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
import json
import logging
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import uuid

from sqlalchemy import func, select, update

from app.router import TIER_COST
from app.store import Event, SchedulerStore, Task, as_utc
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


def simulated_execute(work_ms: float, prompt: str, tier: str, delay: bool = True) -> str:
    """Stand-in for a provider call that takes a fixed amount of time."""
    if work_ms > 0:
        time.sleep(work_ms / 1000)
    return f"{tier} result for: {prompt[:24]}"


def consume(database_url: str, worker_id: str, tier: str, work_ms: float,
            start_at: float) -> tuple[int, float]:
    """Drain one tier until it is empty; return (completed, wall-clock end time).

    Top-level so it can run in a separate process. Every worker waits for the
    same wall-clock ``start_at`` so process start-up is not timed.
    """
    logging.getLogger("scheduler-worker").setLevel(logging.WARNING)
    worker_store = SchedulerStore(database_url)
    try:
        service = WorkerService(
            worker_store,
            worker_id=worker_id,
            tier=tier,
            lease_seconds=30,
            renew_interval=5,
            execute_fn=partial(simulated_execute, work_ms),
        )
        # Open the connection pool before the clock starts.
        worker_store.heartbeat(worker_id, tier, "idle")
        time.sleep(max(0.0, start_at - time.time()))
        completed = 0
        while service.run_once(delay=False):
            completed += 1
        return completed, time.time()
    finally:
        worker_store.engine.dispose()


WORKER_MODES = {
    "threads": "threads in one process, each with its own database connection pool",
    "processes": "one operating-system process per worker, each with its own connection pool",
}


def run_benchmark(database_url: str, task_count: int = 200,
                  worker_count: int = 8, work_ms: float = 0.0,
                  worker_mode: str = "threads") -> dict:
    if task_count < 1:
        raise ValueError("task_count must be at least 1")
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")
    if work_ms < 0:
        raise ValueError("work_ms must not be negative")
    if worker_mode not in WORKER_MODES:
        raise ValueError(f"worker_mode must be one of {sorted(WORKER_MODES)}")

    setup_store = SchedulerStore(database_url)
    setup_store.init()
    setup_store.reset()
    run_id = uuid.uuid4().hex[:12]

    # Workers alternate tiers; a single worker serves one tier with every task.
    worker_specs = [
        (f"benchmark-worker-{index}", "small" if index % 2 == 0 else "large")
        for index in range(worker_count)
    ]
    tiers = sorted({tier for _, tier in worker_specs}, reverse=True)

    tier_task_ids: dict[str, list[str]] = {tier: [] for tier in tiers}
    for index in range(task_count):
        prompt = f"benchmark request {index}"
        if index % 2:
            prompt += " " + ("multi-step capacity analysis " * 32)
        task, _ = setup_store.create_task(
            prompt,
            priority=(index % 10) + 1,
            idempotency_key=f"benchmark-{run_id}-{index}",
        )
        tier_task_ids[tiers[index % len(tiers)]].append(task["id"])

    # Queue throughput should use every worker evenly. Router quality and cost
    # are evaluated separately, so this synthetic workload pins the tier mix.
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

    # Processes need time to start and import; every worker waits for start_at.
    start_at = time.time() + (5.0 if worker_mode == "processes" else 0.5)
    pool = ProcessPoolExecutor if worker_mode == "processes" else ThreadPoolExecutor
    with pool(max_workers=worker_count) as executor:
        futures = [
            executor.submit(consume, database_url, worker_id, tier, work_ms, start_at)
            for worker_id, tier in worker_specs
        ]
        outcomes = [future.result() for future in futures]
    completed_by_workers = [completed for completed, _ in outcomes]
    duration_seconds = max(finished for _, finished in outcomes) - start_at
    # Tasks are submitted before the timer starts, so queue wait is measured from
    # the moment workers are released, not from each task's created_at.
    released_at = datetime.fromtimestamp(start_at, timezone.utc)

    tasks = setup_store.list_tasks(limit=task_count + 10)
    # Every claim writes an event in the same transaction, so a task claimed more
    # than once (or retried) would show more than one claim event.
    with setup_store.Session() as session:
        claims_per_task = session.execute(
            select(func.count()).where(Event.kind == "claimed").group_by(Event.task_id)
        ).scalars().all()
    duplicate_claims = sum(count - 1 for count in claims_per_task if count > 1)
    succeeded = [task for task in tasks if task["status"] == "succeeded"]
    def ms_since_release(timestamp: str) -> float:
        elapsed = as_utc(datetime.fromisoformat(timestamp)) - released_at
        return max(0.0, elapsed.total_seconds() * 1000)

    queue_latencies = [ms_since_release(task["started_at"]) for task in succeeded]
    total_latencies = [ms_since_release(task["finished_at"]) for task in succeeded]
    result = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "database": setup_store.engine.dialect.name,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tasks_submitted": task_count,
        "tasks_succeeded": len(succeeded),
        "workers": worker_count,
        "worker_mode": WORKER_MODES[worker_mode],
        "duplicate_claims": duplicate_claims,
        "tasks_with_single_attempt": sum(task["attempt"] == 1 for task in tasks),
        "execution_mode": (f"mock inference with {work_ms:g} ms simulated provider time"
                           if work_ms else "mock inference without provider delay"),
        "work_ms": work_ms,
        "workload": ("burst submission with forced 50/50 tier distribution"
                     if len(tiers) == 2 else "burst submission on one tier"),
        "worker_completions": completed_by_workers,
        "duration_seconds": round(duration_seconds, 4),
        "throughput_tasks_per_second": round(
            len(succeeded) / duration_seconds if duration_seconds else 0, 2
        ),
        "latency_measured_from": "worker release (all tasks pre-submitted)",
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
    parser.add_argument("--work-ms", type=float, default=0.0,
                        help="simulated provider time per task, in milliseconds")
    parser.add_argument("--worker-mode", choices=sorted(WORKER_MODES), default="threads")
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
        result = run_benchmark(args.database_url, args.tasks, args.workers,
                               args.work_ms, args.worker_mode)
    else:
        print("BENCHMARK_DATABASE_URL is not set; using a temporary SQLite database.",
              file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="scheduler-benchmark-") as directory:
            database_url = f"sqlite:///{Path(directory, 'benchmark.db').as_posix()}"
            result = run_benchmark(database_url, args.tasks, args.workers,
                                   args.work_ms, args.worker_mode)

    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
