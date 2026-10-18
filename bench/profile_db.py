"""Profile one benchmark run from the database's point of view.

Samples pg_stat_activity while workers drain the queue, then prints per-statement
counts from pg_stat_statements. The database must be started with
``shared_preload_libraries=pg_stat_statements``. It shows whether time goes to
executing SQL, waiting on locks or WAL, or waiting for the client between
round trips.
"""

from __future__ import annotations

import argparse
import collections
import os
import threading
import time

from sqlalchemy import create_engine, text

from bench.run import WORKER_MODES, run_benchmark

ACTIVITY = text(
    "select state, coalesce(wait_event_type, '-'), coalesce(wait_event, '-') "
    "from pg_stat_activity where datname = current_database() "
    "and pid <> pg_backend_pid() and backend_type = 'client backend'"
)
STATEMENTS = text(
    "select calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 3), "
    "left(regexp_replace(query, '\\s+', ' ', 'g'), 100) from pg_stat_statements "
    "where dbid = (select oid from pg_database where datname = current_database()) "
    "order by calls desc limit :limit"
)


def profile(database_url: str, workers: int, worker_mode: str, work_ms: float,
            tasks: int) -> None:
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
        connection.execute(text("select pg_stat_statements_reset()"))

    states: collections.Counter[str] = collections.Counter()
    active: list[int] = []
    result: dict = {}
    done = threading.Event()

    def bench() -> None:
        result.update(run_benchmark(database_url, tasks, workers, work_ms, worker_mode))
        done.set()

    runner = threading.Thread(target=bench)
    runner.start()
    # Skip setup and worker start-up; sample only while workers drain the queue.
    time.sleep(1.0 if worker_mode == "threads" else 5.5)
    with admin.connect() as connection:
        while not done.is_set():
            rows = connection.execute(ACTIVITY).all()
            active.append(sum(row[0] == "active" for row in rows))
            states.update(" / ".join(row) for row in rows)
            time.sleep(0.02)
    runner.join()

    total = sum(states.values()) or 1
    print(f"throughput {result['throughput_tasks_per_second']} tasks/s, "
          f"duplicate claims {result['duplicate_claims']}")
    print(f"average active backends {sum(active) / max(1, len(active)):.2f} of {workers} workers")
    print("connection state / wait type / wait event, share of samples:")
    for state, count in states.most_common(8):
        print(f"  {count / total * 100:5.1f}%  {state}")
    print("statements (setup included): calls | total ms | mean ms | query")
    with admin.connect() as connection:
        for row in connection.execute(STATEMENTS, {"limit": 16}):
            print(f"  {row[0]} | {row[1]} | {row[2]} | {row[3]}")
    admin.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--worker-mode", choices=sorted(WORKER_MODES), default="processes")
    parser.add_argument("--work-ms", type=float, default=0.0)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--database-url", default=os.getenv("BENCHMARK_DATABASE_URL"))
    parser.add_argument("--allow-reset", action="store_true")
    args = parser.parse_args()
    if not args.database_url or not args.allow_reset:
        raise SystemExit("Set BENCHMARK_DATABASE_URL to a disposable database and pass --allow-reset.")
    profile(args.database_url, args.workers, args.worker_mode, args.work_ms, args.tasks)


if __name__ == "__main__":
    main()
