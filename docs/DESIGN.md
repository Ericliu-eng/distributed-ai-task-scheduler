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

- **Provider failure:** a retryable failure returns the task to `queued` with a persisted `next_attempt_at`. The default delay grows exponentially from 1 to 30 seconds with deterministic 50–100% per-task jitter, configured through `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS`. After the third failed attempt the task is marked `failed`.
- **Duplicate submission:** the unique idempotency key returns the existing logical task, including concurrent submissions through independent API processes. The winning request returns HTTP 201; duplicates return HTTP 200 without extra routing events.
- **Late write-back:** completion, failure/retry, and lease renewal atomically require the same worker ID, attempt, and `running` state in the database update. Recovery invalidates the old owner; a reused worker ID still cannot write for an earlier attempt. State changes and their events commit together.
- **Worker crash:** claims receive a 15-second lease that is renewed every 3 seconds. The monitor checks every 2 seconds and requeues expired work for a new attempt.
- **Database outage:** workers and the monitor log the error and keep running; workers back off exponentially up to 10 seconds. A task claimed before the outage keeps its lease, and the monitor recovers it if that attempt is lost.
- **Demo controls:** `DEMO_MODE` (default `true`) enables `POST /demo/reset`, which deletes all tasks, and the `fail_once` simulated-failure flag. Set `DEMO_MODE=false` anywhere the API is reachable by others; both then return HTTP 403 and the dashboard hides them.

## API

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Explain why indexes speed up reads","sla":"standard","priority":5,"idempotency_key":"demo-001"}'

curl http://localhost:8000/tasks/{task_id}
curl http://localhost:8000/metrics/summary
curl "http://localhost:8000/metrics/timeseries?minutes=15"
curl http://localhost:8000/benchmarks/performance
curl http://localhost:8000/benchmarks/recovery
curl http://localhost:8000/benchmarks/routing
```

`POST /tasks` returns the task ID, initial state, selected tier, and route reason. Reusing an `idempotency_key` returns the existing task instead of creating another.

`GET /benchmarks/{performance|recovery|routing}` serves the saved reports from `bench/results/` without touching the queue; the dashboard's Benchmarks section renders all three.

`GET /metrics/timeseries?minutes=15` reconstructs 30 intervals from persisted task events and completion timestamps. The dashboard refreshes these trends every five seconds. Queue pressure shows each interval's peak queued and running counts; completion rate counts successful tasks per minute. Latency measures creation to successful completion, including retry and recovery waits. Intervals without completions have no latency sample. These live trends are separate from the saved benchmark reports.

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
