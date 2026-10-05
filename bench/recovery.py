"""Measure end-to-end recovery after a worker process exits mid-task."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import uuid

from app.store import SchedulerStore
from bench.run import summarize
from monitor.recovery import RecoveryMonitor
from worker.executor import execute


CLAIM_AND_EXIT = (
    "import os; from app.store import SchedulerStore; "
    "store=SchedulerStore(); store.init(); "
    "task=store.claim(os.environ['BENCH_WORKER_ID'], os.environ['BENCH_TIER'], "
    "float(os.environ['BENCH_LEASE_SECONDS'])); assert task"
)


def run_recovery_benchmark(database_url: str, task_count: int = 20,
                           lease_seconds: float = 0.2,
                           recovery_poll_interval: float = 0.02) -> dict:
    if task_count < 1:
        raise ValueError("task_count must be at least 1")
    if lease_seconds <= 0 or recovery_poll_interval <= 0:
        raise ValueError("lease and poll intervals must be positive")

    store = SchedulerStore(database_url)
    store.init()
    store.reset()
    monitor = RecoveryMonitor(store)
    repository_root = Path(__file__).resolve().parents[1]
    run_id = uuid.uuid4().hex[:12]
    measurements = {
        "crash_to_detection_ms": [],
        "lease_expiry_to_detection_ms": [],
        "detection_to_reclaim_ms": [],
        "reclaim_to_completion_ms": [],
        "crash_to_completion_ms": [],
    }
    succeeded = 0
    stale_writes_rejected = 0

    try:
        for index in range(task_count):
            task, _ = store.create_task(
                f"recovery benchmark request {index}",
                idempotency_key=f"recovery-{run_id}-{index}",
            )
            environment = os.environ.copy()
            environment.update({
                "DATABASE_URL": database_url,
                "BENCH_WORKER_ID": f"crashed-worker-{index}",
                "BENCH_TIER": task["route_tier"],
                "BENCH_LEASE_SECONDS": str(lease_seconds),
            })
            subprocess.run(
                [sys.executable, "-c", CLAIM_AND_EXIT], cwd=repository_root,
                env=environment, check=True, capture_output=True, text=True,
            )
            crash_at = time.perf_counter()
            crashed = store.get_task(task["id"])
            lease_until = datetime.fromisoformat(crashed["lease_until"])
            if lease_until.tzinfo is None:
                lease_until = lease_until.replace(tzinfo=timezone.utc)
            remaining = max(0.0, (lease_until - datetime.now(timezone.utc)).total_seconds())
            expiry_at = crash_at + remaining

            while task["id"] not in monitor.run_once():
                time.sleep(recovery_poll_interval)
            detected_at = time.perf_counter()

            replacement = store.claim(
                f"replacement-worker-{index}", task["route_tier"], lease_seconds=5
            )
            reclaimed_at = time.perf_counter()
            if replacement is None or replacement["attempt"] != 2:
                raise RuntimeError("recovered task was not reclaimed on attempt 2")
            result = execute(replacement["prompt"], replacement["route_tier"], delay=False)
            if not store.finish(
                task["id"], replacement["worker_id"], replacement["attempt"], result
            ):
                raise RuntimeError("replacement worker could not complete recovered task")
            completed_at = time.perf_counter()
            if not store.finish(task["id"], crashed["worker_id"], crashed["attempt"], "stale"):
                stale_writes_rejected += 1
            if store.get_task(task["id"])["status"] == "succeeded":
                succeeded += 1

            measurements["crash_to_detection_ms"].append((detected_at - crash_at) * 1000)
            measurements["lease_expiry_to_detection_ms"].append(max(0, detected_at - expiry_at) * 1000)
            measurements["detection_to_reclaim_ms"].append((reclaimed_at - detected_at) * 1000)
            measurements["reclaim_to_completion_ms"].append((completed_at - reclaimed_at) * 1000)
            measurements["crash_to_completion_ms"].append((completed_at - crash_at) * 1000)

        return {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "database": store.engine.dialect.name,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "trials": task_count,
            "tasks_succeeded": succeeded,
            "stale_writes_rejected": stale_writes_rejected,
            "lease_seconds": lease_seconds,
            "recovery_poll_interval_seconds": recovery_poll_interval,
            "execution_mode": "worker subprocess exits after claim; replacement uses mock inference without provider delay",
            "timings_ms": {name: summarize(values) for name, values in measurements.items()},
            "limitations": [
                "Measures scheduler, database, and configured lease/poll timing on one local host.",
                "Provider inference delay and container restart/orchestrator scheduling are excluded.",
            ],
        }
    finally:
        store.engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark worker-crash recovery time")
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--lease-seconds", type=float, default=0.2)
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--allow-reset", action="store_true",
                        help="allow deleting rows in an explicitly configured benchmark database")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.database_url and not args.allow_reset:
        raise SystemExit(
            "An explicit benchmark database is reset before each run. "
            "Pass --allow-reset only after verifying it is disposable."
        )
    if args.database_url:
        result = run_recovery_benchmark(
            args.database_url, args.tasks, args.lease_seconds, args.poll_interval
        )
    else:
        print("BENCHMARK_DATABASE_URL is not set; using a temporary SQLite database.",
              file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="scheduler-recovery-") as directory:
            result = run_recovery_benchmark(
                f"sqlite:///{Path(directory, 'recovery.db').as_posix()}",
                args.tasks, args.lease_seconds, args.poll_interval,
            )
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
