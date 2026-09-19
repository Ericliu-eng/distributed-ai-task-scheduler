# Orbit — Distributed AI Task Scheduler

Orbit is a demo-ready multi-worker inference scheduler that routes each request to a small or large model tier using prompt difficulty, SLA, and live queue pressure. Every decision is explainable, workers execute concurrently, failed calls retry automatically, and the dashboard quantifies latency, reliability, routing mix, and estimated savings.

## Why this design

AI requests are not equally difficult or urgent, but a naive gateway sends everything to one expensive model. Orbit separates routing from execution. A deterministic policy makes the cost/latency decision auditable; a transactional task store makes state changes observable; tier-affine workers consume the queue independently. The execution contract is **at-least-once**, with idempotency keys preventing duplicate logical submissions and the claim attempt acting as a fencing token on write-back.

The MVP uses mock inference deliberately, so the entire system can be judged without an API key, network access, rate limits, or nondeterministic model behavior. Replace `mock_inference()` with a provider adapter to connect a real model.

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
 Small-tier worker          Large-tier worker
        └──────── result, retries, timing ────────┘
```

## Quick start

### Local

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.api:app --reload
```

Open **http://localhost:8000** and click **Run 90-sec demo**. API documentation is at `/docs`.

### Docker + PostgreSQL

```bash
docker compose up --build
```

The app waits for PostgreSQL health, creates its schema, starts two worker loops, and serves the same dashboard at **http://localhost:8000**.

## Demo script

1. Click **Run 90-sec demo** to dispatch simple, urgent, complex, and failure-injected work.
2. Point out the `SMALL` / `LARGE` route and readable reason under every task.
3. Watch the small and large workers process tasks concurrently.
4. Find the simulated `HTTP 503` task in Activity: it is requeued and succeeds on attempt 2.
5. Close on success rate, average latency, model distribution, and estimated saving vs. all-large.

## API

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Explain why indexes speed up reads","sla":"standard","priority":5,"idempotency_key":"demo-001"}'

curl http://localhost:8000/tasks/{task_id}
curl http://localhost:8000/metrics/summary
```

`POST /tasks` returns the task ID, initial state, selected tier, and route reason. Reusing an `idempotency_key` returns the existing task instead of creating another.

## Failure semantics

- **Provider failure:** a retryable failure returns the task to `queued` until `max_attempts` is reached.
- **Duplicate submission:** the unique idempotency key returns the existing logical task.
- **Late write-back:** completion requires the same worker ID, attempt, and `running` state, so a stale owner cannot overwrite a newer result.
- **Worker crash:** full lease/heartbeat recovery is intentionally post-MVP; the schema and fencing contract are ready for it.

## Routing policy

- Urgent requests choose the shorter tier queue.
- Standard requests with difficulty `len(prompt) / 1200 ≥ 0.55` use the large tier.
- A saturated small queue overflows to the large tier.
- All remaining work uses the cheaper small tier.

Prompt length is a transparent MVP proxy, not a universal measure of difficulty. A production system should use a lightweight classifier, token estimates, historical quality data, or all three.

## Verification

```bash
pytest -q
```

Tests cover routing, idempotency, retry, priority claims, and fencing-token rejection.

## Known limitations and next steps

- The two demo workers run as threads in the API container; split them into independent services for crash isolation.
- PostgreSQL is used for transactional state management, not claimed as universally superior to dedicated brokers.
- Add heartbeat/lease expiry and a recovery monitor for worker crash recovery.
- Replace mock inference with provider adapters and measure cost/quality on a fixed, programmatically graded benchmark.
