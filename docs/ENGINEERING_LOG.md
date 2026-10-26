# Engineering log

These are problems found while building Orbit. Each one was found by a test, a benchmark, a profile, or an audit, then fixed and pinned with a test or a saved measurement. Entries are in the order they were found.

| # | Problem | How it was found | PR |
| ---: | --- | --- | --- |
| 1 | Published numbers that the code did not support | Auditing every claim against the code | [#13](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/13) |
| 2 | Leases computed from each machine's own clock | Code review, then a clock-skew test | [#13](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/13) |
| 3 | The clock fix cost 15% throughput | Interleaved A/B benchmark | [#13](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/13) |
| 4 | Idempotency key reused with a different body returned an unrelated task | Code review | [#13](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/13) |
| 5 | Thread benchmarks measured the GIL, not the scheduler | Scaling matrix and `pg_stat_activity` | [#14](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/14) |
| 6 | Five transactions per task | `pg_stat_statements` | [#15](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/15) |
| 7 | The single-statement claim made things slower | Client-side `cProfile` | [#15](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/15) |
| 8 | Bound parameters named like columns became extra `SET` values | Failing PostgreSQL tests | [#15](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/15) |
| 9 | Lost wake-up when two dependencies finish at once | Designed against, then forced in a test | [#17](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/17) |
| 10 | The first race test could not tell right from wrong | Running it against a naive implementation | [#17](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/17) |
| 11 | New column broke older code during a rolling upgrade | Benchmarking `main` against the new schema | [#17](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/17) |
| 12 | Deadlock between fail-fast and dependency unlocking | Lock-order review, confirmed by a stress test | [#18](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/18) |
| 13 | Workers crashed when starting together on a fresh database | Running the stack in a browser | [#19](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/19) |
| 14 | A benchmark hypothesis that turned out wrong | The workflow benchmark | this PR |
| 15 | A summary number that left out the weakest result | Rechecking numbers before reusing them | this PR |

## 1. Published numbers that the code did not support

**Symptom.** The README said retries back off "1–30 s", but with three attempts the waits are 0.5–1 s and then 1–2 s. The dashboard's cost saving assumed a 6x price ratio between tiers, while the evaluated models differ by 2x. `retries` also counted attempts used up by lease recovery, so recoveries were counted twice. The benchmark's queue latency started at task creation, which overstated queue wait by the time it took to submit 1,000 tasks (p50 8.2 s, really 3.4 s). One metrics test failed intermittently because two tasks could share a `created_at` value.

**Fix.** Each number now matches its code path, and the test fixes the claim order with distinct priorities. Every benchmark was rerun before any number was republished.

## 2. Leases computed from each machine's own clock

**Root cause.** Workers, the API, and the recovery monitor each used their local clock for lease deadlines. A worker whose clock ran 60 s slow wrote a 15 s lease that a correct monitor already saw as 45 s expired, so live work would be recovered and run twice.

**Fix.** On PostgreSQL every lease, retry, heartbeat, and completion time comes from the database's `now()`. A test with a worker clock 60 s behind fails on the old code and passes on the new.

## 3. The clock fix cost 15% throughput

**How it was found.** An interleaved A/B run against `main` on the same database measured 109 tasks/s against 129. The first version fetched `now()` with its own `SELECT` before every claim, renewal, heartbeat, and write-back, which added a round trip per step.

**Fix.** `now()` is evaluated inside the statements that already run. Throughput returned to parity (139 vs 139).

**Lesson.** Compare branches with interleaved runs on one database. Numbers from another day vary by more than this regression.

## 4. Reusing an idempotency key with a different body

**Symptom.** A second request with an existing key but a different prompt silently returned the first, unrelated task.

**Fix.** Each task stores a fingerprint of the fields that define it. Reusing a key with a different body returns HTTP 409, including under concurrent submission. Rows created before the fingerprint existed are still deduplicated.

## 5. Thread benchmarks measured the GIL

**How it was found.** Throughput stayed near 150 tasks/s from 4 to 16 workers when workers were threads, but reached 501 tasks/s with 16 processes. `pg_stat_activity` showed the database executing SQL in only 2–4% of samples with 16 threads. Transactions sat open while their Python thread waited for the interpreter lock.

**Consequence.** The headline 139 tasks/s understated the scheduler. Since Compose runs each worker as its own container, the process numbers describe deployment.

## 6. Five transactions per task

**How it was found.** `pg_stat_statements` counted five transactions and about eleven round trips per task: claim, busy heartbeat, a `should_fail` lookup, write-back, and idle heartbeat. With one worker that came to about 20 ms of scheduling overhead per task, more than the 20 ms simulated provider call.

**Fix.** The heartbeats and the failure flag ride in the claim and write-back transactions, and a test counts transactions per task. This alone gave about +60% at every worker count.

## 7. The single-statement claim made things slower

**Symptom.** Folding claim and write-back into one PostgreSQL statement each, using data-modifying CTEs, removed round trips but lowered throughput in thread mode.

**How it was found.** `cProfile` showed SQLAlchemy compiling the claim on every call: 3.6 ms of client CPU against a 0.5 ms round trip. SQLAlchemy cannot cache PostgreSQL-specific `INSERT … ON CONFLICT`, so the whole statement was rebuilt and recompiled each time.

**Fix.** Both statements are built once with bound parameters. The upsert became a cacheable `UPDATE` plus `INSERT … WHERE NOT EXISTS`. Client CPU per claim fell to 0.4 ms, and a test asserts both statements stay cacheable. With all of rows 6 and 7, throughput rose 2–7x (1.9x at 16 processes, 6.6x at 8 threads).

## 8. Bound parameters named like columns became `SET` values

**Symptom.** After caching the write-back, PostgreSQL tests failed with an unbound parameter, and the logged SQL showed `UPDATE tasks SET … worker_id=…, attempt=…`.

**Root cause.** When an `UPDATE` executes, SQLAlchemy treats any parameter whose name matches a column as an extra value to set. The parameters `worker_id` and `attempt`, meant for the `WHERE` clause, were also being written.

**Fix.** Bound parameters carry a `p_` prefix.

## 9. Lost wake-up when two dependencies finish at once

**Root cause.** "Have all my dependencies succeeded?" checked at write-back time fails when two upstream nodes finish together. Under READ COMMITTED each statement can see the other upstream as still running, so neither unlocks the node, and it stays blocked forever. Workflow completion has the same race one level up.

**Fix.** A countdown. Each node stores how many dependencies have not succeeded, and each workflow stores how many nodes have not succeeded. Each decrement takes the row lock, and PostgreSQL re-evaluates the second writer against the first one's committed row, so exactly one writer reaches zero.

## 10. The first race test could not tell right from wrong

**How it was found.** The test was run against a deliberately naive status-check implementation, and it still passed. It forced the race by locking the dependent row, but the naive code never touches that row, so the race never happened.

**Fix.** The test locks both upstream rows instead, so both write-backs take their snapshot before either commits. Against that test the countdown passes and the naive version leaves the node blocked forever. The test also got a `lock_timeout`, set through `PGOPTIONS`; a plain `SET` opened a transaction that broke autocommit. A failure now errors instead of hanging, which an earlier version did.

## 11. A new column broke older code during a rolling upgrade

**How it was found.** Benchmarking `main` against a database the new code had created failed on every insert: `null value in column "pending_dependencies"`.

**Root cause.** The column was `NOT NULL` with only a Python-side default. Code that predates the column, such as an API instance not yet upgraded, cannot insert into the new schema.

**Fix.** A server default, plus a test that inserts a task the way older code does.

## 12. Deadlock between fail-fast and unlocking

**Root cause.** A permanent failure locks the workflow row, then skips the remaining task rows. A concurrent success locks task rows to unlock them, then the workflow row to count down. The two acquire locks in opposite orders. Two writers locking several task rows in different orders can also deadlock.

**Fix.** Every path locks task rows first, in id order, then the workflow row. In the single-statement write-back, the countdown CTE references the unlock CTE so PostgreSQL finishes the unlock first, since CTE execution order is otherwise unspecified.

**Evidence.** In a stress test, 8 reports arrive at once in a 20-node workflow and one of them is a permanent failure. Locking the workflow row first raised `DeadlockDetected` in one of three runs. The fixed order has not deadlocked, and no node has been left blocked or queued.

## 13. Workers crashed when starting together on a fresh database

**How it was found.** Running the API and two workers at once for a browser check: the large worker exited with `table tasks already exists`.

**Root cause.** Each process saw no tables and then raced to create them. Docker Compose hid this by starting workers only after the API was healthy, but any deployment that starts processes together would hit it.

**Fix.** `init()` retries table creation on "already exists". The column and index migrations already tolerated the same race. A test starts four processes on a fresh database behind a barrier; it failed on its first run before the fix.

## 14. A benchmark hypothesis that turned out wrong

**Expectation.** Before measuring workflows, the hand-off delay between a node and its dependent was expected to be dominated by the 250 ms idle poll.

**What the measurement showed.** With 100 workflows in flight, the idle poll made no difference (hand-off p50 3 ms at 250, 50, and 10 ms). The queue never empties, and the worker that finishes a node claims its newly queued dependent immediately. The poll only matters on an idle system. With one workflow at a time, a 10 ms poll cut the gap between nodes from about 30 ms to about 6 ms.

**Two explanations were wrong.** The extra critical-path time first looked like Windows `sleep()` overshoot. `sleep()` is accurate to under 1 ms, and the extra time is each node's own claim and write-back. The 50 ms and 250 ms polls also gave nearly the same gap, which looked like workers polling in lockstep. Giving each worker a random start offset barely changed it. What makes the 50 ms poll behave like the 250 ms one is still an open question.

## 15. A summary number that left out the weakest result

**Symptom.** Since [#15](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/pull/15), the README and BENCHMARKS.md summarized the round-trip work as "3–7x higher throughput". The A/B table directly below that sentence showed 1.9x for 16 processes.

**How it was found.** While drafting resume bullets from the saved results, each summary figure was checked against the table it summarizes.

**Fix.** The summary now reads 2–7x and names the low end (1.9x at 16 processes) as well as the high end (6.6x at 8 threads).
