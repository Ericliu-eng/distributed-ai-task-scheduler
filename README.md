# Orbit — Distributed AI Task Scheduler

Orbit is a demo-ready multi-worker inference scheduler that routes each request to a small or large model tier using prompt difficulty, SLA, and live queue pressure. Every decision is explainable, workers execute concurrently, failed calls retry automatically, and the dashboard quantifies latency, reliability, routing mix, and estimated savings.

Reference queue benchmark: **1,000/1,000 tasks completed**, **129.7 tasks/s**, and **10,392 ms p99 queue latency** with eight workers on a local PostgreSQL 16 burst workload. See the methodology and limitations below before comparing these numbers.

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
 Small worker process       Large worker process
        └──────── result, retries, timing ────────┘
```

## Quick start

### Local

Install dependencies once, then start the three independent processes in separate terminals:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.api:app --reload
MODEL_TIER=small WORKER_ID=worker-small-01 python -m worker.main
MODEL_TIER=large WORKER_ID=worker-large-01 python -m worker.main
```

PowerShell worker commands:

```powershell
$env:MODEL_TIER="small"; $env:WORKER_ID="worker-small-01"; python -m worker.main
$env:MODEL_TIER="large"; $env:WORKER_ID="worker-large-01"; python -m worker.main
```

Open **http://localhost:8000** and click **Run 90-sec demo**. API documentation is at `/docs`.

### Docker + PostgreSQL

```bash
docker compose up --build
```

Compose waits for PostgreSQL health, then starts the API, two tier-affine workers, and a lease recovery monitor. The dashboard is served at **http://localhost:8000**.

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
- **Late write-back:** completion and lease renewal require the same worker ID, attempt, and `running` state, so a stale owner cannot overwrite a newer result.
- **Worker crash:** claims receive a 15-second lease that is renewed every 3 seconds. The monitor checks every 2 seconds and requeues expired work for a new attempt.

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

Tests cover routing, idempotency, retry, priority claims, lease renewal, stale-task recovery, and fencing-token rejection.
Pull requests also run a PostgreSQL integration test in GitHub Actions: eight independent workers claim and complete 100 tasks through `SKIP LOCKED`, while the test verifies that every task has exactly one owner and one successful attempt.

## Performance benchmark

Run a disposable SQLite benchmark locally:

```bash
python -m bench.run --tasks 200 --workers 8
```

The JSON output records throughput and p50/p95/p99 queue and total latency, plus task counts and routing distribution. For a PostgreSQL measurement, create a dedicated disposable database and run:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_benchmark \
  python -m bench.run --tasks 1000 --workers 8 --allow-reset --output benchmark-results.json
```

The safety flag is required because the benchmark clears task and event rows before every run. Never point it at the demo or production database.

### Reference result

| Workload | Result |
| --- | ---: |
| Tasks completed | 1,000 / 1,000 |
| Workers | 8 |
| Tier distribution | 500 small / 500 large |
| Throughput | 129.7 tasks/s |
| Queue latency p50 | 6,076.5 ms |
| Queue latency p95 | 9,853.55 ms |
| Queue latency p99 | 10,392.02 ms |
| Total latency p99 | 10,419.04 ms |

This is a single local Windows 11 / Python 3.13.12 / PostgreSQL 16 run using mock inference with provider delay disabled. Tasks are submitted as a burst and pinned to a 50/50 tier distribution so all eight workers participate; queue latency starts at each task's creation time and therefore includes time spent waiting behind the burst. It measures scheduler and database behavior, not model-provider latency. The machine-readable result is stored in [`bench/results/postgres-1000-tasks.json`](bench/results/postgres-1000-tasks.json); rerun the benchmark on the target hardware before using the number in a resume.

## Known limitations and next steps

- The additive startup migration handles the two lease columns; a production deployment should adopt a full migration framework before more schema changes.
- PostgreSQL is used for transactional state management, not claimed as universally superior to dedicated brokers.
- Replace mock inference with provider adapters and measure cost/quality on a fixed, programmatically graded benchmark.
