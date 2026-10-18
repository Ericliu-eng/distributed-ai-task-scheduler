# Benchmarks

Both benchmarks reset their database before every run, so an explicit database URL requires `--allow-reset`. Never point them at the demo or production database. Without `BENCHMARK_DATABASE_URL`, they use a temporary SQLite database and say so on stderr.

## Queue throughput

Run a disposable SQLite benchmark locally:

```bash
python -m bench.run --tasks 200 --workers 8
```

The JSON output records throughput, p50/p95/p99 queue and total latency, task counts, routing distribution, and a duplicate-claim check (every task must have exactly one claim event and one attempt). Workers run as threads in one process, each with its own database connection pool, so claims contend in the database exactly as separate processes would. For a PostgreSQL measurement, create a dedicated disposable database and run:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_benchmark \
  python -m bench.run --tasks 1000 --workers 8 --allow-reset --output benchmark-results.json
```

### Reference result

| Workload | Result |
| --- | ---: |
| Tasks completed | 1,000 / 1,000 |
| Workers | 8 threads, separate connection pools |
| Duplicate claims | 0 |
| Tier distribution | 500 small / 500 large |
| Throughput | 139.02 tasks/s |
| Queue wait p50 | 3,418 ms |
| Queue wait p95 | 6,716 ms |
| Queue wait p99 | 7,063 ms |
| Total latency p99 | 7,101 ms |

This is the median of three runs (125.0, 139.0, and 139.9 tasks/s, measured 2026-10-16) on Windows 11 / Python 3.11 against PostgreSQL 16 in Docker, using mock inference with provider delay disabled. All 1,000 tasks are submitted before the timer starts and pinned to a 50/50 tier distribution so all eight workers participate. Queue wait and total latency are measured from the moment workers are released, so they show how long a task waits behind the burst, not how long submission took; earlier reports measured from task creation and overstated queue wait by roughly the submission time. It measures scheduler and database behavior, not model-provider latency. The machine-readable result is stored in [`bench/results/postgres-1000-tasks.json`](../bench/results/postgres-1000-tasks.json). Throughput varies noticeably between sessions on the same machine: earlier sets of three runs measured 97–117 and 131–172 tasks/s. To separate code changes from machine noise, compare branches with interleaved runs against the same database; the database-clock change in this release measured 139 tasks/s median against 139 on the previous code. Every run had zero duplicate claims.

For contrast, the same 1,000-task workload on SQLite also had zero duplicate claims but reached only about 60 tasks/s with very uneven work per worker, because SQLite serializes writers behind one database lock.

## Scaling with worker count

Throughput is flat past four workers when workers are threads, but keeps rising when each worker is its own process. The plateau comes from the benchmark client, not from PostgreSQL. Run the matrix against a disposable database:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_benchmark   python -m bench.scaling --worker-mode processes --allow-reset --output scaling.json
```

Each cell is the median of three 1,000-task runs, measured on 2026-10-16 with PostgreSQL 16 in Docker on a 20-core Windows 11 machine. Every run had zero duplicate claims. Efficiency is throughput divided by single-worker throughput times the worker count.

| Workers | Threads, 0 ms | Processes, 0 ms | Threads, 20 ms | Processes, 20 ms |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 49 tasks/s (100%) | 52 tasks/s (100%) | 23 tasks/s (100%) | 23 tasks/s (100%) |
| 4 | 146 (74%) | 190 (92%) | 80 (86%) | 94 (101%) |
| 8 | 149 (38%) | 313 (76%) | 117 (63%) | 177 (95%) |
| 16 | 135 (17%) | 501 (61%) | 131 (35%) | 328 (88%) |

The 0 ms columns measure pure scheduling overhead; the 20 ms columns add a fixed simulated provider call. Raw results are in [`bench/results/scaling-threads.json`](../bench/results/scaling-threads.json) and [`bench/results/scaling-processes.json`](../bench/results/scaling-processes.json).

### Where the time goes

`python -m bench.profile_db` samples `pg_stat_activity` during a run and reads `pg_stat_statements`; it needs PostgreSQL started with `shared_preload_libraries=pg_stat_statements`.

| Run | Average backends executing SQL | Samples waiting for the client |
| --- | ---: | ---: |
| 1 worker | 0.22 of 1 | 87% |
| 16 threads | 0.39 of 16 | 96% |
| 16 processes | 0.96 of 16 | 82% |

- **The database is mostly idle.** Its connections spend most of their time waiting for the next client statement, often inside an open transaction. Lock waits never appear; the only server-side wait of note is the WAL flush on commit.
- **Each task costs five transactions and five commits.** These are the claim, a busy heartbeat, a `should_fail` lookup, the result write-back, and an idle heartbeat. That adds up to about eleven round trips. A single worker therefore spends about 20 ms of scheduling overhead per task, which is longer than the 20 ms simulated provider call.
- **Threads share one interpreter.** With 16 threads the database executes SQL only 2–4% of the time. Transactions stay open while a thread waits for the GIL, so throughput tops out near 150 tasks/s however many threads run. Compose runs every worker as a separate container, so the process columns describe deployment.

The next step is to cut per-task round trips and commits, then rerun this matrix for a before/after comparison.

## Crash recovery

Run repeated worker-crash trials against a disposable SQLite database:

```bash
python -m bench.recovery --tasks 20 --lease-seconds 0.2 --poll-interval 0.02
```

For PostgreSQL, use a dedicated database and the same explicit reset guard:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_recovery \
  python -m bench.recovery --tasks 20 --lease-seconds 0.2 --poll-interval 0.02 \
  --allow-reset --output recovery-results.json
```

Each trial starts a worker subprocess that claims a task and exits without writing a result or releasing its lease, which is what the scheduler sees when a worker is killed. The benchmark waits for lease recovery, lets a replacement claim attempt 2 and finish, then verifies that the crashed attempt is fenced. It reports p50/p95/p99 for crash-to-detection, lease-expiry-to-detection, detection-to-reclaim, reclaim-to-completion, and total crash-to-completion time.

| Recovery workload | Result |
| --- | ---: |
| Tasks recovered and completed | 20 / 20 |
| Stale writes rejected | 20 / 20 |
| Lease / monitor poll | 200 ms / 20 ms |
| Crash to completion p50 | 154.4 ms |
| Crash to completion p95 | 168.61 ms |
| Lease expiry to detection p95 | 34.57 ms |

This is a single local Windows 11 / Python 3.11 / PostgreSQL 16 run, measured 2026-10-16. Lease deadlines come from the database clock, while the benchmark measures from the host, so a few milliseconds of host–container clock offset can appear in the lease-expiry-to-detection figure. It measures scheduler and database recovery with mock inference delay disabled; it excludes provider latency, container restart time, network faults, and orchestrator scheduling. The deliberately short lease makes the benchmark fast and does not represent the 15-second production default; with that default, a task killed mid-run in Docker Compose was recovered and completed about 17 seconds later. The machine-readable result is stored in [`bench/results/postgres-recovery-20.json`](../bench/results/postgres-recovery-20.json).
