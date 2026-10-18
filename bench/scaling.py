"""Measure how queue throughput scales with the number of workers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import statistics
import sys

from bench.run import WORKER_MODES, run_benchmark


def run_matrix(database_url: str, worker_counts: list[int], work_ms_values: list[float],
               repeats: int, task_count: int, worker_mode: str) -> dict:
    """Run every (workers, work_ms) cell ``repeats`` times and keep the median run."""
    cells = []
    for work_ms in work_ms_values:
        for workers in worker_counts:
            runs = [run_benchmark(database_url, task_count, workers, work_ms, worker_mode)
                    for _ in range(repeats)]
            throughputs = [run["throughput_tasks_per_second"] for run in runs]
            median = statistics.median(throughputs)
            # The reported latencies come from the run whose throughput is the median.
            representative = min(runs, key=lambda run: abs(run["throughput_tasks_per_second"] - median))
            cells.append({
                "workers": workers,
                "work_ms": work_ms,
                "throughput_tasks_per_second": round(median, 2),
                "throughput_runs": throughputs,
                "duplicate_claims": sum(run["duplicate_claims"] for run in runs),
                "queue_latency_ms": representative["queue_latency_ms"],
                "worker_completions": representative["worker_completions"],
            })
            print(f"work_ms={work_ms:g} workers={workers}: {median:.1f} tasks/s "
                  f"(runs {', '.join(f'{t:.1f}' for t in throughputs)})", file=sys.stderr)
    for work_ms in work_ms_values:
        row = [cell for cell in cells if cell["work_ms"] == work_ms]
        base = row[0]["throughput_tasks_per_second"] / row[0]["workers"]
        for cell in row:
            # Perfect scaling would multiply single-worker throughput by the worker count.
            ideal = base * cell["workers"]
            cell["scaling_efficiency_pct"] = round(cell["throughput_tasks_per_second"] / ideal * 100, 1)
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "worker_mode": WORKER_MODES[worker_mode],
        "tasks_per_run": task_count,
        "repeats": repeats,
        "method": ("each cell is the median of its runs; every run resets the database, "
                   "pre-submits all tasks, and times from worker release to the last completion"),
        "cells": cells,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--work-ms", type=float, nargs="+", default=[0.0, 20.0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--worker-mode", choices=sorted(WORKER_MODES), default="threads")
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--allow-reset", action="store_true",
                        help="allow deleting existing rows in an explicitly configured database")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.getLogger("scheduler-worker").setLevel(logging.WARNING)
    if not args.database_url:
        raise SystemExit("Set BENCHMARK_DATABASE_URL to a disposable PostgreSQL database.")
    if not args.allow_reset:
        raise SystemExit("Every run resets the database; pass --allow-reset to confirm it is disposable.")
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    result = run_matrix(args.database_url, sorted(args.workers), args.work_ms,
                        args.repeats, args.tasks, args.worker_mode)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
