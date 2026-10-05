# Orbit — Distributed AI Task Scheduler

[![CI](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Ericliu-eng/distributed-ai-task-scheduler/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)
![FastAPI](https://img.shields.io/badge/FastAPI-009688)

A multi-worker inference scheduler that routes each request to a small or large model tier by difficulty, SLA, and queue depth, then executes it with retries, leases, and crash recovery on PostgreSQL.

![Animated flow: a short task is routed to the small tier, fails once and retries after backoff; a long task is routed to the large tier, claimed by exactly one worker, recovered after that worker is killed, and completed by a second worker while the late write is rejected; the dashboard event feed and saved benchmark results update alongside](docs/demo/orbit-flow.gif)

<sub>Illustrated flow, not a recording. Rendered by [`docs/demo/render_flow.py`](docs/demo/render_flow.py) · [static frame](docs/demo/orbit-flow.png)</sub>

## Results

| What | Result | Conditions |
| --- | --- | --- |
| Queue throughput | **135 tasks/s, 0 duplicate claims** across 1,000 tasks | 8 workers, PostgreSQL 16, mock inference, median of 3 runs · [details](docs/BENCHMARKS.md#queue-throughput) |
| Crash recovery | **20 / 20 killed tasks recovered**, every stale write rejected | Short 200 ms test lease · [details](docs/BENCHMARKS.md#crash-recovery) |
| Cost-aware routing | **100% of all-Sonnet quality at 35% lower cost** | Claude Haiku 4.5 / Sonnet 5.5, 50 graded cases, one sample each · [details](docs/EVALUATION.md) |

## How it works

- **Explainable routing:** four ordered rules (urgent SLA, difficulty threshold, small-queue overflow, default small) pick a tier, and the reason is stored with every task.
- **Exactly-one claim:** workers take tasks with `SELECT … FOR UPDATE SKIP LOCKED`, ordered by priority.
- **Leases and recovery:** a claim holds a 15 s lease renewed every 3 s; a monitor requeues tasks whose lease expires.
- **Fencing:** every write-back must match the current attempt number, so a dead or slow worker cannot overwrite newer work.
- **Retries and idempotency:** provider failures back off exponentially (1–30 s, jittered, 3 attempts); idempotency keys deduplicate submissions.

More in [docs/DESIGN.md](docs/DESIGN.md): failure semantics, the API, and known limitations.

## Quick start

```bash
docker compose up --build
```

Open **http://localhost:8000** and click **Run demo**. Compose starts PostgreSQL, the API, a small and a large worker, and the recovery monitor. Set `POSTGRES_PORT` if port 5432 is taken.

<details>
<summary>Run without Docker (SQLite)</summary>

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.api:app --reload
MODEL_TIER=small WORKER_ID=worker-small-01 python -m worker.main
MODEL_TIER=large WORKER_ID=worker-large-01 python -m worker.main
```

On PowerShell, set the worker variables with `$env:MODEL_TIER="small"; $env:WORKER_ID="worker-small-01"` before `python -m worker.main`.
</details>

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

CI also runs the PostgreSQL concurrency suite, including eight workers draining 100 tasks with exactly one owner per task.

## Documentation

| Topic | Document |
| --- | --- |
| Architecture and routing policy | [docs/DESIGN.md](docs/DESIGN.md#why-this-design) |
| Failure semantics | [docs/DESIGN.md#failure-semantics](docs/DESIGN.md#failure-semantics) |
| API and dashboard | [docs/DESIGN.md#api](docs/DESIGN.md#api), [dashboard walkthrough](docs/DESIGN.md#dashboard-walkthrough) |
| Queue throughput and crash recovery | [docs/BENCHMARKS.md](docs/BENCHMARKS.md) |
| Routing cost-quality evaluation | [docs/EVALUATION.md](docs/EVALUATION.md) |
| Tests and CI | [docs/DESIGN.md#tests](docs/DESIGN.md#tests) |
| Limitations and next steps | [docs/DESIGN.md#known-limitations-and-next-steps](docs/DESIGN.md#known-limitations-and-next-steps) |
| Animation source | [docs/demo/render_flow.py](docs/demo/render_flow.py) |
