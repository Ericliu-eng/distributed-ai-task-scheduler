"""Measure DAG scheduling: dependency hand-off latency, overhead, and throughput."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from functools import partial
import json
import logging
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time

from sqlalchemy import func, select, update

from app.router import TIER_COST
from app.store import Event, SchedulerStore, Task, TaskDependency, Workflow, as_utc
from bench.run import run_benchmark, simulated_execute, summarize
from worker.main import WorkerService

# The document pipeline: four levels deep, with one parallel pair.
PIPELINE = [
    {"key": "parse", "prompt": "parse the document"},
    {"key": "chunk", "prompt": "split into chunks", "depends_on": ["parse"]},
    {"key": "metadata", "prompt": "extract metadata", "depends_on": ["parse"]},
    {"key": "embed", "prompt": "embed the chunks", "depends_on": ["chunk", "metadata"]},
    {"key": "index", "prompt": "build the index", "depends_on": ["embed"]},
]


def drain_workflows(database_url: str, worker_id: str, tier: str, work_ms: float,
                    poll_ms: float, start_at: float, stop=None) -> int:
    """Work like a real worker until every workflow has finished, or until ``stop`` is set.

    An empty queue does not mean the run is over: downstream nodes stay blocked
    until their dependencies succeed. Idle workers sleep ``poll_ms`` between
    claims, as the production worker sleeps ``poll_interval``.
    """
    logging.getLogger("scheduler-worker").setLevel(logging.WARNING)
    store = SchedulerStore(database_url)
    try:
        service = WorkerService(store, worker_id=worker_id, tier=tier, lease_seconds=30,
                                renew_interval=5, execute_fn=partial(simulated_execute, work_ms))
        store.heartbeat(worker_id, tier, "idle")
        time.sleep(max(0.0, start_at - time.time()))
        # Production workers start at different moments, so their idle polls are
        # out of phase. Released together, every worker would poll in lockstep.
        time.sleep(random.uniform(0, poll_ms / 1000))
        completed = 0
        while True:
            if service.run_once(delay=False):
                completed += 1
                continue
            if stop is not None:
                if stop.is_set():
                    return completed
            else:
                with store.Session() as session:
                    running = session.scalar(select(func.count()).select_from(Workflow)
                                             .where(Workflow.status == "running"))
                if not running:
                    return completed
            time.sleep(poll_ms / 1000)
    finally:
        service.stop()
        store.engine.dispose()


def run_workflow_benchmark(database_url: str, workflows: int = 100, workers: int = 8,
                           work_ms: float = 20.0, poll_ms: float = 250.0) -> dict:
    if workflows < 1 or workers < 2:
        raise ValueError("need at least one workflow and two workers")
    setup = SchedulerStore(database_url)
    setup.init()
    setup.reset()
    created = [setup.create_workflow(f"bench-{index}", PIPELINE) for index in range(workflows)]
    # Pin whole workflows to alternating tiers so both worker tiers stay busy.
    with setup.Session.begin() as session:
        for index, workflow in enumerate(created):
            tier = "small" if index % 2 == 0 else "large"
            session.execute(update(Task).where(Task.workflow_id == workflow["id"]).values(
                route_tier=tier, estimated_cost=TIER_COST[tier],
                route_reason="Benchmark workload: tier pinned per workflow"))

    specs = [(f"dag-worker-{i}", "small" if i % 2 == 0 else "large") for i in range(workers)]
    start_at = time.time() + 5.0  # processes start and import before the clock
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(drain_workflows, database_url, worker_id, tier, work_ms, poll_ms, start_at)
                   for worker_id, tier in specs]
        completions = [future.result(timeout=600) for future in futures]

    with setup.Session() as session:
        tasks = {task.id: task for task in session.scalars(select(Task))}
        edges = session.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id)).all()
        flows = session.scalars(select(Workflow)).all()
        events = Counter(session.execute(select(Event.kind)).scalars())
        claims = Counter(session.execute(select(Event.task_id).where(Event.kind == "claimed")).scalars())
    setup.engine.dispose()

    # The upstream that finished last is the one whose write-back unlocked the node.
    handoffs = handoff_latencies(tasks, edges)
    waiting = {task_id for task_id, _ in edges}
    by_workflow: dict[str, list[Task]] = {}
    for task in tasks.values():
        by_workflow.setdefault(task.workflow_id, []).append(task)
    durations = [
        (max(as_utc(t.finished_at) for t in nodes) - min(as_utc(t.started_at) for t in nodes)).total_seconds() * 1000
        for nodes in by_workflow.values()
    ]
    first_start = min(as_utc(t.started_at) for t in tasks.values())
    last_finish = max(as_utc(t.finished_at) for t in tasks.values())
    makespan = (last_finish - first_start).total_seconds()
    gaps = scheduling_gaps(tasks, edges)
    return {
        "workflows": workflows,
        "nodes": len(tasks),
        "workers": workers,
        "work_ms": work_ms,
        "idle_poll_ms": poll_ms,
        "workflows_succeeded": sum(flow.status == "succeeded" for flow in flows),
        "duplicate_claims": sum(count - 1 for count in claims.values() if count > 1),
        "unblocked_events": events["unblocked"],
        "edges_waiting_on_upstream": len(waiting),
        "worker_completions": completions,
        "makespan_seconds": round(makespan, 3),
        "tasks_per_second": round(len(tasks) / makespan, 1),
        "workflows_per_second": round(workflows / makespan, 1),
        "handoff_latency_ms": summarize(handoffs),
        "workflow_duration_ms": summarize(durations),
        "critical_path_run_ms": summarize([critical for _, critical in gaps]),
        "scheduling_gap_ms": summarize([gap for gap, _ in gaps]),
    }


def scheduling_gaps(tasks: dict, edges) -> list[tuple[float, float]]:
    """Per workflow: (time spent between nodes, measured critical-path run time), in ms.

    The critical path is the longest dependency chain by measured run time
    (start to finish of each node). Subtracting it from the workflow's span
    leaves only scheduling gaps, so a slow sleep() or provider call is not
    mistaken for scheduler overhead.
    """
    upstream: dict[str, list[str]] = {}
    for task_id, depends_on in edges:
        upstream.setdefault(task_id, []).append(depends_on)
    run_ms = {task_id: (as_utc(t.finished_at) - as_utc(t.started_at)).total_seconds() * 1000
              for task_id, t in tasks.items()}
    longest: dict[str, float] = {}

    def chain(task_id: str) -> float:
        if task_id not in longest:
            longest[task_id] = run_ms[task_id] + max((chain(up) for up in upstream.get(task_id, [])),
                                                     default=0.0)
        return longest[task_id]

    by_workflow: dict[str, list[str]] = {}
    for task_id, task in tasks.items():
        by_workflow.setdefault(task.workflow_id, []).append(task_id)
    gaps = []
    for ids in by_workflow.values():
        span = (max(as_utc(tasks[i].finished_at) for i in ids)
                - min(as_utc(tasks[i].started_at) for i in ids)).total_seconds() * 1000
        critical = max(chain(i) for i in ids)
        gaps.append((span - critical, critical))
    return gaps


def handoff_latencies(tasks: dict, edges) -> list[float]:
    upstream: dict[str, list[str]] = {}
    for task_id, depends_on in edges:
        upstream.setdefault(task_id, []).append(depends_on)
    return [(as_utc(tasks[node].started_at) - max(as_utc(tasks[up].finished_at) for up in ups))
            .total_seconds() * 1000 for node, ups in upstream.items()]


def run_latency_benchmark(database_url: str, workflows: int = 20, workers: int = 8,
                          work_ms: float = 20.0, poll_ms: float = 250.0) -> dict:
    """One workflow at a time on an otherwise idle system, as a single user would see it.

    Workers stay up across workflows. A parallel branch that the finishing
    worker cannot take waits for a sleeping worker's next poll.
    """
    import multiprocessing

    setup = SchedulerStore(database_url)
    setup.init()
    setup.reset()
    manager = multiprocessing.Manager()
    stop = manager.Event()
    specs = [(f"dag-worker-{i}", "small") for i in range(workers)]  # every node routes small
    start_at = time.time() + 5.0
    durations, handoffs, gaps = [], [], []
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(drain_workflows, database_url, worker_id, tier, work_ms,
                                   poll_ms, start_at, stop) for worker_id, tier in specs]
            time.sleep(max(0.0, start_at - time.time()) + 0.5)
            for index in range(workflows):
                workflow = setup.create_workflow(f"latency-{index}", PIPELINE)
                deadline = time.monotonic() + 30
                while setup.get_workflow(workflow["id"])["status"] == "running":
                    assert time.monotonic() < deadline, "workflow did not finish"
                    time.sleep(0.005)
                with setup.Session() as session:
                    tasks = {t.id: t for t in session.scalars(
                        select(Task).where(Task.workflow_id == workflow["id"]))}
                    edges = session.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
                                            .where(TaskDependency.task_id.in_(list(tasks)))).all()
                durations.append((max(as_utc(t.finished_at) for t in tasks.values())
                                  - min(as_utc(t.started_at) for t in tasks.values())).total_seconds() * 1000)
                handoffs += handoff_latencies(tasks, edges)
                gaps += scheduling_gaps(tasks, edges)
            stop.set()
            for future in futures:
                future.result(timeout=60)
    finally:
        manager.shutdown()
        setup.engine.dispose()
    return {
        "workflows": workflows, "workers": workers, "work_ms": work_ms, "idle_poll_ms": poll_ms,
        "handoff_latency_ms": summarize(handoffs),
        "workflow_duration_ms": summarize(durations),
        "critical_path_run_ms": summarize([critical for _, critical in gaps]),
        "scheduling_gap_ms": summarize([gap for gap, _ in gaps]),
    }


def run_matrix(database_url: str, poll_values: list[float], repeats: int, workflows: int,
               workers: int, work_ms: float) -> dict:
    cells = []
    for poll_ms in poll_values:
        runs = [run_workflow_benchmark(database_url, workflows, workers, work_ms, poll_ms)
                for _ in range(repeats)]
        median = statistics.median(run["tasks_per_second"] for run in runs)
        cell = min(runs, key=lambda run: abs(run["tasks_per_second"] - median))
        cell["tasks_per_second_runs"] = [run["tasks_per_second"] for run in runs]
        cell["handoff_p50_runs"] = [run["handoff_latency_ms"]["p50"] for run in runs]
        cell["all_runs_correct"] = all(
            run["workflows_succeeded"] == workflows and run["duplicate_claims"] == 0
            and run["unblocked_events"] == run["edges_waiting_on_upstream"] for run in runs)
        cells.append(cell)
        print(f"poll {poll_ms:g} ms: {median:.0f} tasks/s, hand-off p50 "
              f"{cell['handoff_latency_ms']['p50']:.0f} ms, p95 {cell['handoff_latency_ms']['p95']:.0f} ms, "
              f"correct={cell['all_runs_correct']}", file=sys.stderr)
    latency = []
    for poll_ms in poll_values:
        runs = [run_latency_benchmark(database_url, 20, workers, work_ms, poll_ms) for _ in range(repeats)]
        median = statistics.median(run["workflow_duration_ms"]["p50"] for run in runs)
        cell = min(runs, key=lambda run: abs(run["workflow_duration_ms"]["p50"] - median))
        cell["duration_p50_runs"] = [run["workflow_duration_ms"]["p50"] for run in runs]
        latency.append(cell)
        print(f"one at a time, poll {poll_ms:g} ms: workflow p50 {median:.0f} ms "
              f"(critical-path run {cell['critical_path_run_ms']['p50']:.0f} ms, gaps "
              f"{cell['scheduling_gap_ms']['p50']:.0f} ms), hand-off p50 "
              f"{cell['handoff_latency_ms']['p50']:.0f} ms, p95 {cell['handoff_latency_ms']['p95']:.0f} ms",
              file=sys.stderr)
    # The same number of independent tasks, same workers and work: what dependencies cost.
    standalone = [run_benchmark(database_url, workflows * len(PIPELINE), workers, work_ms, "processes")
                  for _ in range(repeats)]
    baseline = statistics.median(run["throughput_tasks_per_second"] for run in standalone)
    print(f"standalone baseline: {baseline:.0f} tasks/s", file=sys.stderr)
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "worker_mode": "one operating-system process per worker",
        "workload": f"{workflows} five-node document pipelines submitted at once, tiers pinned per workflow",
        "repeats": repeats,
        "method": ("each cell is the run with the median tasks/s; hand-off latency is a node's start minus "
                   "the finish of the upstream that unlocked it, all from the database clock"),
        "cells": cells,
        "one_at_a_time": latency,
        "standalone_baseline_tasks_per_second": round(baseline, 1),
        "standalone_runs": [run["throughput_tasks_per_second"] for run in standalone],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflows", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--work-ms", type=float, default=20.0)
    parser.add_argument("--poll-ms", type=float, nargs="+", default=[250.0, 50.0, 10.0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--allow-reset", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.getLogger("scheduler-worker").setLevel(logging.WARNING)
    if not args.database_url or not args.allow_reset:
        raise SystemExit("Set BENCHMARK_DATABASE_URL to a disposable database and pass --allow-reset.")
    result = run_matrix(args.database_url, args.poll_ms, args.repeats, args.workflows,
                        args.workers, args.work_ms)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
