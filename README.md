# Orbit — Distributed AI Task Scheduler

Orbit is a demo-ready multi-worker inference scheduler that routes each request to a small or large model tier using prompt difficulty, SLA, and live queue pressure. Every decision is explainable, workers execute concurrently, failed calls retry automatically, and the dashboard quantifies latency, reliability, routing mix, and estimated savings.

Reference results on local PostgreSQL 16: **1,000/1,000 tasks completed at 129.7 tasks/s**, plus **20/20 crashed tasks recovered with 197.12 ms p95 crash-to-completion time** under the benchmark's short lease configuration. See the methodology and limitations below before comparing these numbers.

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

Open **http://localhost:8000** and click **Run demo** (confirm replacing the current tasks), or use **New task** to submit a workload. API documentation is at `/docs`.

### Docker + PostgreSQL

```bash
docker compose up --build
```

Compose waits for PostgreSQL health, then starts the API, two tier-affine workers, and a lease recovery monitor. The dashboard is served at **http://localhost:8000**.

## Demo script

1. Click **Run demo** and confirm replacing the current tasks and activity with the sample workload.
2. In **Overview**, check current status, then inspect completion rate, queue pressure, and p50/p95 end-to-end latency over the last 5, 15, or 60 minutes. Expand a task with **+** to inspect its routing reason, attempts, and result.
3. Search by prompt or task ID, or filter the queue by status to focus on active or failed work.
4. Switch to **Activity** and select **Retries & failures** to find the simulated failure; its task succeeds on attempt 2.
5. Review model distribution and estimated savings in the routing summary, then open **Benchmarks** for saved queue, crash-recovery, and fixture-evaluation reports.

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

`GET /benchmarks/{performance|routing}` serves the saved reports from `bench/results/` without touching the queue; the dashboard's Benchmarks section renders both.

`GET /metrics/timeseries?minutes=15` reconstructs 30 intervals from persisted task events and completion timestamps. The dashboard refreshes these trends every five seconds. Queue pressure shows each interval's peak queued and running counts; completion rate counts successful tasks per minute. Latency measures creation to successful completion, including retry and recovery waits. Intervals without completions have no latency sample. These live trends are separate from the saved benchmark reports.

## Failure semantics

- **Provider failure:** a retryable failure returns the task to `queued` with a persisted `next_attempt_at`. The default delay grows exponentially from 1 to 30 seconds with deterministic 50–100% per-task jitter, configured through `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS`.
- **Duplicate submission:** the unique idempotency key returns the existing logical task, including concurrent submissions through independent API processes. The winning request returns HTTP 201; duplicates return HTTP 200 without extra routing events.
- **Late write-back:** completion, failure/retry, and lease renewal atomically require the same worker ID, attempt, and `running` state in the database update. Recovery invalidates the old owner; a reused worker ID still cannot write for an earlier attempt. State changes and their events commit together.
- **Worker crash:** claims receive a 15-second lease that is renewed every 3 seconds. The monitor checks every 2 seconds and requeues expired work for a new attempt.

## Routing policy

- Urgent requests choose the shorter tier queue.
- Standard requests with difficulty `len(prompt) / 1200 ≥ 0.55` use the large tier.
- A saturated small queue overflows to the large tier.
- All remaining work uses the cheaper small tier.

Prompt length is a transparent MVP proxy, not a universal measure of difficulty. A production system should use a lightweight classifier, token estimates, historical quality data, or all three.

## Verification

`requirements.txt` holds only runtime dependencies (it is what the Docker image installs); test tooling lives in `requirements-dev.txt`.

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

Tests cover routing, concurrent idempotent submissions, jittered retry backoff and eligibility, priority claims, lease renewal, stale-task recovery, competing completion/failure writes, recovery during stale write-back, benchmark statistics, saved benchmark reports, all four graders, and the reference cost-quality result. Concurrency regressions use independent database connections and synchronized interleavings, with PostgreSQL coverage enabled by `TEST_DATABASE_URL`.
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

## Crash-recovery benchmark

Run repeated worker-crash trials against a disposable SQLite database:

```bash
python -m bench.recovery --tasks 20 --lease-seconds 0.2 --poll-interval 0.02
```

For PostgreSQL, use a dedicated database and the same explicit reset guard as the queue benchmark:

```bash
BENCHMARK_DATABASE_URL=postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_recovery \
  python -m bench.recovery --tasks 20 --lease-seconds 0.2 --poll-interval 0.02 \
  --allow-reset --output recovery-results.json
```

Each trial starts a worker subprocess that claims a task and exits without writing a result. The benchmark waits for lease recovery, lets a replacement claim attempt 2 and finish, then verifies that the crashed attempt is fenced. It reports p50/p95/p99 for crash-to-detection, lease-expiry-to-detection, detection-to-reclaim, reclaim-to-completion, and total crash-to-completion time.

| Recovery workload | Result |
| --- | ---: |
| Tasks recovered and completed | 20 / 20 |
| Stale writes rejected | 20 / 20 |
| Lease / monitor poll | 200 ms / 20 ms |
| Crash to completion p50 | 177.68 ms |
| Crash to completion p95 | 197.12 ms |
| Lease expiry to detection p95 | 48.42 ms |

This is a single local Windows 11 / Python 3.11 / PostgreSQL 16 run. It measures scheduler and database recovery with mock inference delay disabled; it excludes provider latency, container restart time, network faults, and orchestrator scheduling. The deliberately short lease makes the benchmark fast and does not represent the 15-second production default. The machine-readable result is stored in [`bench/results/postgres-recovery-20.json`](bench/results/postgres-recovery-20.json).

## Routing cost-quality evaluation

The repository includes a fixed 50-case dataset with arithmetic, structured extraction, classification, and executable code graders. Compare the all-large baseline with multiple routing thresholds:

```bash
python -m bench.evaluate --output bench/results/routing-evaluation.json
```

At the default `0.55` difficulty threshold, the deterministic fixture adapter retains **90%** of the all-large baseline score while reducing estimated cost by **50%** (30 small / 20 large). The full threshold curve and failed case IDs are stored in [`bench/results/routing-evaluation.json`](bench/results/routing-evaluation.json).

| Difficulty threshold | Quality retention | Cost saving | Small / large |
| ---: | ---: | ---: | ---: |
| 0.30 | 96% | 45% | 27 / 23 |
| 0.45 | 90% | 50% | 30 / 20 |
| 0.55 | 90% | 50% | 30 / 20 |
| 0.70 | 50% | 83.33% | 50 / 0 |

This fixture adapter makes routing, grading, and cost accounting deterministic and CI-safe; it is not evidence of real LLM quality. Before placing quality retention on a resume, run the unchanged dataset and graders through real small- and large-model provider adapters.

## Known limitations and next steps

- The additive startup migration handles lease, recovery-count, and retry-eligibility columns; a production deployment should adopt a full migration framework before more schema changes.
- PostgreSQL is used for transactional state management, not claimed as universally superior to dedicated brokers.
- Replace deterministic fixture responses with real provider adapters before treating the routing evaluation as evidence of model quality.
