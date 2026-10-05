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
| Throughput | 135.42 tasks/s |
| Queue latency p50 | 8,226 ms |
| Queue latency p95 | 13,238.7 ms |
| Queue latency p99 | 14,409.65 ms |
| Total latency p99 | 14,438.66 ms |

This is the median of three runs (131.2, 135.4, and 171.7 tasks/s) on Windows 11 / Python 3.11 against PostgreSQL 16 in Docker, using mock inference with provider delay disabled. Tasks are submitted as a burst and pinned to a 50/50 tier distribution so all eight workers participate; queue latency starts at each task's creation time and therefore includes time spent waiting behind the burst. It measures scheduler and database behavior, not model-provider latency. The machine-readable result is stored in [`bench/results/postgres-1000-tasks.json`](../bench/results/postgres-1000-tasks.json). Throughput varies noticeably between sessions on the same machine: an earlier set of three runs measured 97–117 tasks/s. Every run had zero duplicate claims.

For contrast, the same 1,000-task workload on SQLite also had zero duplicate claims but reached only about 60 tasks/s with very uneven work per worker, because SQLite serializes writers behind one database lock.

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
| Crash to completion p50 | 177.68 ms |
| Crash to completion p95 | 197.12 ms |
| Lease expiry to detection p95 | 48.42 ms |

This is a single local Windows 11 / Python 3.11 / PostgreSQL 16 run. It measures scheduler and database recovery with mock inference delay disabled; it excludes provider latency, container restart time, network faults, and orchestrator scheduling. The deliberately short lease makes the benchmark fast and does not represent the 15-second production default; with that default, a task killed mid-run in Docker Compose was recovered and completed about 17 seconds later. The machine-readable result is stored in [`bench/results/postgres-recovery-20.json`](../bench/results/postgres-recovery-20.json).
