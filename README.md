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
5. Review model distribution and estimated savings in the routing summary, then open **Benchmarks** for saved queue, crash-recovery, and Claude routing-evaluation reports.

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
- **Database outage:** workers and the monitor log the error and keep running; workers back off exponentially up to 10 seconds. A task claimed before the outage keeps its lease, and the monitor recovers it if that attempt is lost.
- **Demo controls:** `DEMO_MODE` (default `true`) enables `POST /demo/reset`, which deletes all tasks, and the `fail_once` simulated-failure flag. Set `DEMO_MODE=false` anywhere the API is reachable by others; both then return HTTP 403 and the dashboard hides them.

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

Tests cover routing, concurrent idempotent submissions, jittered retry backoff and eligibility, priority claims, lease renewal, stale-task recovery, competing completion/failure writes, recovery during stale write-back, benchmark statistics, saved benchmark reports, all four graders, answer extraction from model output, resumable Claude response collection, and the reference cost-quality result. Concurrency regressions use independent database connections and synchronized interleavings, with PostgreSQL coverage enabled by `TEST_DATABASE_URL`.
Pull requests also run a PostgreSQL integration test in GitHub Actions: eight worker threads, each with its own database connection, claim and complete 100 tasks through `SKIP LOCKED`, while the test verifies that every task has exactly one owner and one successful attempt.

## Performance benchmark

Run a disposable SQLite benchmark locally:

```bash
python -m bench.run --tasks 200 --workers 8
```

The JSON output records throughput, p50/p95/p99 queue and total latency, task counts, routing distribution, and a duplicate-claim check (every task must have exactly one claim event and one attempt). Workers run as threads in one process, each with its own database connection pool, so claims contend in the database exactly as separate processes would. For a PostgreSQL measurement, create a dedicated disposable database and run:

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

This is a single local Windows 11 / Python 3.13.12 / PostgreSQL 16 run using mock inference with provider delay disabled. Tasks are submitted as a burst and pinned to a 50/50 tier distribution so all eight workers participate; queue latency starts at each task's creation time and therefore includes time spent waiting behind the burst. It measures scheduler and database behavior, not model-provider latency. The machine-readable result is stored in [`bench/results/postgres-1000-tasks.json`](bench/results/postgres-1000-tasks.json). Throughput depends on the host and database setup; three runs against PostgreSQL 16 in Docker on the same machine measured 97–117 tasks/s, each with zero duplicate claims.

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

The repository includes a fixed 50-case dataset (25 easy, 25 hard) with arithmetic, structured extraction, classification, and executable code graders. Generated code runs in a separate isolated Python process with restricted builtins and a 5-second timeout; that contains hangs and crashes but is not a security sandbox. Every prompt is self-contained: hard cases carry their own incident timelines, contract histories, ledgers, and specifications, so prompt length reflects real content. The evaluation compares an all-large baseline with multiple routing thresholds.

### Claude results

```bash
python -m pip install -r requirements-dev.txt
ANTHROPIC_API_KEY=... python -m bench.evaluate --provider anthropic \
  --output bench/results/routing-evaluation-claude.json
```

The small tier is `claude-haiku-4-5` and the large tier is `claude-sonnet-5-5`. Each response, with its token usage and cost, is appended to [`bench/results/claude-responses.jsonl`](bench/results/claude-responses.jsonl) as it arrives. Interrupted runs resume, and rerunning the command re-grades the recorded responses without new API calls. The full run made 100 calls and cost **$0.05**.

| Strategy / difficulty threshold | Correct | Quality retention | Cost saving | Small / large |
| --- | ---: | ---: | ---: | ---: |
| All large (Sonnet 5.5) | 50 / 50 | 100% | — | 0 / 50 |
| All small (Haiku 4.5) | 49 / 50 | 98% | 44.14% | 50 / 0 |
| 0.25 | 50 / 50 | 100% | 11.99% | 27 / 23 |
| 0.40 | 50 / 50 | 100% | 24.47% | 35 / 15 |
| **0.55 (default)** | **50 / 50** | **100%** | **35.00%** | 48 / 2 |
| 0.70 | 49 / 50 | 98% | 44.14% | 50 / 0 |

At the default threshold, only the two longest prompts reach Sonnet. One of them is the multi-step inventory ledger (`numeric-009`), the only case Haiku answered incorrectly, so routing keeps full baseline quality at 35% lower measured cost. Cost savings are capped at about 44% because Sonnet 5.5's list price is only 2x Haiku 4.5's.

Read these numbers with their limits:

- One recorded sample per case and tier at default sampling settings. A rerun can differ.
- The dataset separates the tiers on only one case. It shows the routing and cost mechanics, not a general quality benchmark; harder cases are needed to measure a real quality gap.
- Graders extract answers instead of requiring exact formatting. String fields compare case-insensitively, a classification label counts on its own first or last line, and fenced code or JSON is unwrapped. These rules were finalized after reviewing the first run, where both models lost points on formatting and on one ambiguous answer key (`keyboard` vs. the source text's `keyboards`). The rules apply equally to both tiers, and the raw responses are committed for review.

### Deterministic fixture adapter

```bash
python -m bench.evaluate --output bench/results/routing-evaluation.json
```

Without an API key, the evaluation uses canned responses stored in the dataset. It keeps routing, grading, and cost accounting deterministic for CI. It is not evidence of model quality. Results are stored in [`bench/results/routing-evaluation.json`](bench/results/routing-evaluation.json).

## Known limitations and next steps

- The additive startup migration handles lease, recovery-count, and retry-eligibility columns plus the claim (`status, route_tier, priority, created_at`) and lease-scan (`status, lease_until`) indexes; a production deployment should adopt a full migration framework before more schema changes.
- The API has no authentication or rate limiting; it is a single-tenant demo service.
- PostgreSQL is used for transactional state management, not claimed as universally superior to dedicated brokers.
- Prompt length is a weak difficulty signal. Add harder evaluation cases and compare it with a learned classifier before tuning the threshold further.
