# Design

## Why this design

AI requests are not equally difficult or urgent, but a naive gateway sends everything to one expensive model. Orbit separates routing from execution. A deterministic policy makes the cost/latency decision auditable; a transactional task store makes state changes observable; tier-affine workers consume the queue independently. The execution contract is **at-least-once**, with idempotency keys preventing duplicate logical submissions and the claim attempt acting as a fencing token on write-back.

The live system uses mock inference deliberately, so the entire system can be judged without an API key, network access, rate limits, or nondeterministic model behavior. Replace `execute()` in `worker/executor.py` with a provider adapter to connect a real model; the routing evaluation already calls Claude through `bench/claude_adapter.py`.

```text
Browser / API client
        │
        ▼
 FastAPI Gateway ─── Explainable Router
        │                 │ small / large + reason
        ▼                 ▼
     SQL task queue (SQLite locally, PostgreSQL in Docker)
        │                         │
        ▼                         ▼
 Small worker process       Large worker process
        └──────── result, retries, timing ────────┘
                         ▲
              Recovery monitor (lease expiry)
```

## Routing policy

Rules are evaluated in this order:

1. Urgent requests choose the shorter tier queue.
2. Standard requests with difficulty `len(prompt) / 1200 ≥ 0.55` use the large tier.
3. A saturated small queue (more than 4 queued) overflows to the large tier.
4. All remaining work uses the cheaper small tier.

Every decision stores a human-readable reason with the task. Prompt length is a transparent MVP proxy, not a universal measure of difficulty. A production system should use a lightweight classifier, token estimates, historical quality data, or all three. The [routing evaluation](EVALUATION.md) measures the threshold against real models.

## Failure semantics

- **Provider failure:** a retryable failure returns the task to `queued` with a persisted `next_attempt_at`. The delay doubles from a 1-second base up to a 30-second cap, with deterministic 50–100% per-task jitter, configured through `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS`. With the default 3 attempts the waits are 0.5–1 s and then 1–2 s; the cap only matters with more attempts. After the third failed attempt the task is marked `failed`.
- **Duplicate submission:** the unique idempotency key returns the existing logical task, including concurrent submissions through independent API processes. The winning request returns HTTP 201; duplicates return HTTP 200 without extra routing events. Each task stores a fingerprint of its prompt, SLA, priority, and failure flag, so reusing a key with a different body returns HTTP 409 instead of silently returning an unrelated task.
- **Late write-back:** completion, failure/retry, and lease renewal atomically require the same worker ID, attempt, and `running` state in the database update. Recovery invalidates the old owner; a reused worker ID still cannot write for an earlier attempt. State changes and their events commit together.
- **Worker crash:** claims receive a 15-second lease that is renewed every 3 seconds. The monitor checks every 2 seconds and requeues expired work for a new attempt.
- **Single-statement hot path:** on PostgreSQL, a claim is one autocommit statement whose data-modifying CTEs claim the task with `SKIP LOCKED`, insert its event, and record the worker heartbeat; a successful write-back is likewise one fenced statement. Each statement is atomic, so a failure leaves no partial write. Each worker renews leases from one long-lived thread, and a renewal records its heartbeat in the same fenced transaction, so a renewal that loses the race with the write-back changes nothing.
- **Clock skew:** on PostgreSQL, lease deadlines, retry times, heartbeats, and completion times all come from the database's `now()`, never from a worker's or the monitor's local clock, so a machine whose clock drifts cannot make live work look expired.
- **Database outage:** workers and the monitor log the error and keep running; workers back off exponentially up to 10 seconds. A task claimed before the outage keeps its lease, and the monitor recovers it if that attempt is lost.
- **Demo controls:** `DEMO_MODE` (default `true`) enables `POST /demo/reset`, which deletes all tasks, and the `fail_once` simulated-failure flag. Set `DEMO_MODE=false` anywhere the API is reachable by others; both then return HTTP 403 and the dashboard hides them.

## API

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Explain why indexes speed up reads","sla":"standard","priority":5,"idempotency_key":"demo-001"}'

curl http://localhost:8000/tasks/{task_id}
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics/summary
curl "http://localhost:8000/metrics/timeseries?minutes=15"
curl http://localhost:8000/benchmarks/performance
curl http://localhost:8000/benchmarks/recovery
curl http://localhost:8000/benchmarks/routing
```

`POST /tasks` returns the task ID, initial state, selected tier, and route reason. Reusing an `idempotency_key` with the same body returns the existing task; reusing it with a different body returns HTTP 409.

`GET /healthz` reports liveness without touching the database. `GET /readyz` returns HTTP 503 when the database is unreachable; Compose uses it as the API health check.

`GET /benchmarks/{performance|recovery|routing}` serves the saved reports from `bench/results/` without touching the queue; the dashboard's Benchmarks section renders all three.

`GET /metrics/timeseries?minutes=15` reconstructs 30 intervals from persisted task events and completion timestamps. The dashboard refreshes these trends every five seconds. Queue pressure shows each interval's peak queued and running counts; completion rate counts successful tasks per minute. Latency measures creation to successful completion, including retry and recovery waits. Intervals without completions have no latency sample. These live trends are separate from the saved benchmark reports.

### Workflows (in progress)

`POST /workflows` submits a DAG of tasks. Each node is routed like a standalone task, and lists the nodes it depends on:

```bash
curl -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" \
  -d '{"name":"document-ai-pipeline","nodes":[
        {"key":"parse","prompt":"Parse the uploaded document"},
        {"key":"chunk","prompt":"Split the text into chunks","depends_on":["parse"]},
        {"key":"metadata","prompt":"Extract title and authors","depends_on":["parse"]},
        {"key":"embed","prompt":"Embed every chunk","depends_on":["chunk","metadata"]},
        {"key":"index","prompt":"Build the vector index","depends_on":["embed"]}]}'

curl http://localhost:8000/workflows/{workflow_id}
```

- **Validation before any write:** the graph is ordered with Kahn's algorithm in O(V + E). Empty or oversized graphs (over 100 nodes), bad or repeated keys, unknown or duplicate dependencies, self-dependencies, and cycles return HTTP 422 with the rule broken and the nodes involved; a cycle names one actual loop, such as `a -> b -> a`.
- **Atomic submission:** the workflow, every node task, and every dependency edge commit in one transaction.
- **Blocked until ready:** nodes without dependencies start `queued`; the rest start `blocked`. Claims only take `queued` rows, so a blocked node is never handed to a worker.
- **Unlocking:** each node stores how many dependencies have not succeeded yet. A successful write-back decrements that count for every node waiting on it, and the decrement that reaches zero moves the node to `queued` and logs an `unblocked` event. On PostgreSQL this rides in the single-statement write-back, so a task still costs two round trips; a fenced write-back decrements nothing.
- **Why a countdown:** re-checking "have all my dependencies succeeded?" loses the wake-up when two upstreams finish at once. Under READ COMMITTED both statements can snapshot the other upstream as still running, and the join never runs. Each decrement takes the dependent's row lock, and the second writer re-evaluates against the first one's committed row, so exactly one of them reaches zero. A test forces that interleaving by locking both upstream rows until both write-backs have taken their snapshots. The countdown passes, and a status-check variant leaves the join blocked.

- **Fail-fast:** when a node fails permanently, because its retries ran out or its final attempt's lease expired, its workflow becomes `failed`. Every node still `blocked` or `queued` becomes `skipped` with the reason recorded. Nodes already running may finish, but they unlock nothing.
- **Completion:** each workflow counts the nodes that have not succeeded; the success that reaches zero marks it `succeeded`, for the same lost-wake-up reason as unlocking. A test forces both final write-backs to snapshot before either commits. A naive "am I the last node?" check leaves the workflow running forever; the countdown completes it exactly once.
- **Lock order:** a failure and a concurrent success both update task rows and the workflow row. Every path locks task rows first, in id order, then the workflow row, so the two never wait on each other in opposite orders. A stress test reports 8 results at once, one of them a failure, in a 20-node workflow. Locking the workflow row first produced `DeadlockDetected`; the fixed order has not, and no node was left blocked or queued.

**Not finished yet:** the dashboard does not show workflows yet.

Interactive API documentation is served at `/docs`.

## Dashboard walkthrough

1. Click **Run demo** and confirm replacing the current tasks and activity with the sample workload.
2. In **Overview**, check current status, then inspect completion rate, queue pressure, and p50/p95 end-to-end latency over the last 5, 15, or 60 minutes. Expand a task with **+** to inspect its routing reason, attempts, and result.
3. Search by prompt or task ID, or filter the queue by status to focus on active or failed work.
4. Switch to **Activity** and select **Retries & failures** to find the simulated failure; its task succeeds on attempt 2.
5. Review model distribution and estimated savings in the routing summary, then open **Benchmarks** for saved queue, crash-recovery, and Claude routing-evaluation reports.

## Tests

`requirements.txt` holds only runtime dependencies (it is what the Docker image installs); test tooling lives in `requirements-dev.txt`.

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

Tests cover routing, concurrent idempotent submissions, jittered retry backoff and eligibility, priority claims, lease renewal, stale-task recovery, competing completion/failure writes, recovery during stale write-back, database-outage resilience, timestamp normalization under non-UTC PostgreSQL sessions, benchmark statistics, saved benchmark reports, all four graders, answer extraction from model output, resumable Claude response collection, and the reference cost-quality result. Concurrency regressions use independent database connections and synchronized interleavings, with PostgreSQL coverage enabled by `TEST_DATABASE_URL`.

Pull requests also run a PostgreSQL integration test in GitHub Actions: eight worker threads, each with its own database connection, claim and complete 100 tasks through `SKIP LOCKED`, while the test verifies that every task has exactly one owner and one successful attempt.

## Known limitations and next steps

- The additive startup migration handles lease, recovery-count, and retry-eligibility columns plus the claim (`status, route_tier, priority, created_at`) and lease-scan (`status, lease_until`) indexes; a production deployment should adopt a full migration framework before more schema changes.
- The API has no authentication or rate limiting; it is a single-tenant demo service.
- PostgreSQL is used for transactional state management, not claimed as universally superior to dedicated brokers.
- Prompt length is a weak difficulty signal. Add harder evaluation cases and compare it with a learned classifier before tuning the threshold further.
