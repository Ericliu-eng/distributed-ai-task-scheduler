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
| Throughput | 683.16 tasks/s |
| Queue wait p50 | 738 ms |
| Queue wait p95 | 1,373 ms |
| Queue wait p99 | 1,431 ms |
| Total latency p99 | 1,436 ms |

This is the median of three runs (644.0, 683.2, and 695.6 tasks/s, measured 2026-10-18) on Windows 11 / Python 3.11 against PostgreSQL 16 in Docker, using mock inference with provider delay disabled. All 1,000 tasks are submitted before the timer starts and pinned to a 50/50 tier distribution so all eight workers participate. Queue wait and total latency are measured from the moment workers are released, so they show how long a task waits behind the burst, not how long submission took; earlier reports measured from task creation and overstated queue wait by roughly the submission time. It measures scheduler and database behavior, not model-provider latency. The machine-readable result is stored in [`bench/results/postgres-1000-tasks.json`](../bench/results/postgres-1000-tasks.json). Before the round-trip work described under [Scaling with worker count](#scaling-with-worker-count), the same benchmark measured 139 tasks/s (2026-10-16). Throughput varies noticeably between sessions on the same machine, so compare branches with interleaved runs against the same database rather than against numbers from another day. Every run had zero duplicate claims.

For contrast, the same 1,000-task workload on SQLite also had zero duplicate claims but reached only about 60 tasks/s with very uneven work per worker, because SQLite serializes writers behind one database lock.

## Scaling with worker count

Cutting per-task round trips raised throughput 2–7x: 6.6x with 8 threads, 3.6x with 8 processes, 1.9x with 16 processes. The fix came from measuring first: profiling showed the database idle while workers waited on their own round trips. Run the matrix against a disposable database:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_benchmark \
  python -m bench.scaling --worker-mode processes --allow-reset --output scaling.json
```

Each cell is the median of three 1,000-task runs on PostgreSQL 16 in Docker, on a 20-core Windows 11 machine. The 0 ms columns measure pure scheduling overhead; the 20 ms columns add a fixed simulated provider call. Every run had zero duplicate claims.

| Workers | Threads, 0 ms | Processes, 0 ms | Threads, 20 ms | Processes, 20 ms |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 49 → 129 | 52 → 149 | 23 → 34 | 23 → 35 |
| 4 | 146 → 420 | 190 → 446 | 80 → 139 | 94 → 141 |
| 8 | 149 → 891 | 313 → 1,175 | 117 → 304 | 177 → 303 |
| 16 | 135 → 879 | 501 → 1,241 | 131 → 532 | 328 → 602 |

All values are tasks/s, before → after. The before matrix ran on 2026-10-16 and the after matrix on 2026-10-18; raw results are in [`scaling-threads-before.json`](../bench/results/scaling-threads-before.json), [`scaling-processes-before.json`](../bench/results/scaling-processes-before.json), [`scaling-threads.json`](../bench/results/scaling-threads.json), and [`scaling-processes.json`](../bench/results/scaling-processes.json). Because throughput drifts between sessions, the gain was also measured with interleaved runs of both versions against one database, five runs each:

| Configuration | Previous code | Optimized | Change |
| --- | ---: | ---: | ---: |
| 1 worker | 44 tasks/s | 144 tasks/s | 3.3x |
| 8 threads | 128 | 843 | 6.6x |
| 8 processes | 219 | 789 | 3.6x |
| 16 processes | 324 | 612 | 1.9x |

With 16 processes the optimized runs ranged from 403 to 1,210 tasks/s. At that rate the result depends on the shared Docker VM and the database, not the client.

### What the profile showed

`python -m bench.profile_db` samples `pg_stat_activity` during a run and reads `pg_stat_statements`; it needs PostgreSQL started with `shared_preload_libraries=pg_stat_statements`. The tables above follow these findings.

1. **The database was mostly idle.** Before the change it waited for the client in 82–96% of samples; with 16 threads it executed SQL only 2–4% of the time. There were no lock waits. Threads plateaued near 150 tasks/s because they share one interpreter, while separate processes kept scaling.
2. **Each task cost five transactions.** These were the claim, a busy heartbeat, a `should_fail` lookup, the write-back, and an idle heartbeat, which came to about eleven round trips and five commits. Each step was cheap; the cost was the waiting between them.
3. **Fold the extra work into the two essential writes.** The claim now records the busy heartbeat and returns the simulated-failure flag, and the write-back records the idle heartbeat. A test counts transactions to keep each task at two. This alone gave about +60% at every worker count.
4. **One statement each on PostgreSQL.** The claim is one autocommit statement. Data-modifying CTEs pick the task with `SKIP LOCKED`, claim it, insert its event, and record the heartbeat. The fenced write-back is the same, so a task costs two statements and no BEGIN or COMMIT round trips.
5. **The single statements first ran slower.** Profiling the client found SQLAlchemy recompiling the claim on every call (3.6 ms of client CPU against a 0.5 ms round trip). It cannot cache PostgreSQL-specific `INSERT … ON CONFLICT`. Building both statements once with bound parameters and replacing the upsert with a cacheable `UPDATE` plus `INSERT … WHERE NOT EXISTS` cut client CPU per claim to 0.4 ms. A test asserts both statements stay cacheable.
6. **One renewal thread per worker** replaces a thread started and joined for every task. The renewal records the busy heartbeat in its own fenced transaction, so a renewal that loses the race with the write-back changes nothing.

After the change, transactions held open while waiting for the client fell from 37% to 2% of samples with 16 processes. The largest server-side wait is now the WAL flush on commit (`LWLock / WALWrite`, 14% of samples), because every task still commits twice. Possible next steps are batching claims and relying on PostgreSQL group commit; `synchronous_commit = off` would trade durability for speed and is not a default this scheduler should take.

## Workflows

Dependencies cost almost nothing in throughput. 100 five-node pipelines ran at 275–294 tasks/s, against 293 for the same 500 tasks with no dependencies. Every one of the 400 dependent nodes was unlocked exactly once. On an idle system, the worker poll interval decides how long a ready node waits.

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_benchmark \
  python -m bench.workflows --allow-reset --output workflows.json
```

The workload is the document pipeline `parse → chunk / metadata → embed → index`: four levels deep, with one parallel pair. It runs on 8 worker processes with a simulated 20 ms provider call per node, on PostgreSQL 16 in Docker (2026-10-23). Each cell is the median of three runs. Two measurements matter for a DAG:

- **Hand-off latency:** a node's start minus the finish of the upstream node that unlocked it, all from the database clock.
- **Scheduling gap:** a workflow's span minus the run time of its critical path (the longest chain of node runs, measured). Subtracting measured run time keeps slow provider calls out of the scheduler's account.

### Many workflows at once

| Idle poll | Throughput | Hand-off p50 / p95 | Gap p50 / p95 |
| ---: | ---: | ---: | ---: |
| 250 ms | 275 tasks/s | 3 / 26 ms | 20 / 31 ms |
| 50 ms | 294 tasks/s | 3 / 29 ms | 19 / 34 ms |
| 10 ms | 290 tasks/s | 3 / 29 ms | 16 / 36 ms |
| No dependencies (500 independent tasks) | 293 tasks/s | — | — |

Every run finished 100/100 workflows with zero duplicate claims and exactly 400 `unblocked` events, one per dependent node. The poll interval makes no difference under load. The queue never empties, and the worker that finishes a node claims its newly queued dependent immediately. The p95 hand-off is the parallel branch waiting for a second worker that is busy with another workflow.

### One workflow at a time

| Idle poll | Workflow p50 | Critical-path run p50 | Gap p50 / p95 | Hand-off p95 |
| ---: | ---: | ---: | ---: | ---: |
| 250 ms | 122 ms | 92 ms | 31 / 33 ms | 28 ms |
| 50 ms | 123 ms | 92 ms | 31 / 34 ms | 28 ms |
| 10 ms | 98 ms | 93 ms | 5 / 10 ms | 3 ms |

Workers stay up while 20 workflows run one after another, as a single user would see them. The critical path runs about 92 ms rather than 4 × 20 ms because every node also pays its own claim and write-back. The gap is the parallel branch waiting for a second worker's next poll. A 10 ms poll cuts it to about 5 ms, at the cost of more idle queries. The 50 ms poll behaves like the 250 ms one, and that is not explained yet. Worker poll phases were randomized and `sleep()` is accurate to under 1 ms, so neither of those is the cause; see the [engineering log](ENGINEERING_LOG.md#14-a-benchmark-hypothesis-that-turned-out-wrong). PostgreSQL `LISTEN/NOTIFY` would let an idle worker wake when a node is queued instead of polling. That is the next step if idle latency matters.

The raw results are in [`bench/results/workflows.json`](../bench/results/workflows.json).

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
| Crash to completion p50 | 145.06 ms |
| Crash to completion p95 | 178.9 ms |
| Lease expiry to detection p95 | 41.15 ms |

This is a single local Windows 11 / Python 3.11 / PostgreSQL 16 run, measured 2026-10-18 after the round-trip changes. Lease deadlines come from the database clock, while the benchmark measures from the host, so a few milliseconds of host–container clock offset can appear in the lease-expiry-to-detection figure. It measures scheduler and database recovery with mock inference delay disabled; it excludes provider latency, container restart time, network faults, and orchestrator scheduling. The deliberately short lease makes the benchmark fast and does not represent the 15-second production default; with that default, a task killed mid-run in Docker Compose was recovered and completed about 17 seconds later. The machine-readable result is stored in [`bench/results/postgres-recovery-20.json`](../bench/results/postgres-recovery-20.json).
