from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import text
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.dag import MAX_NODES, DagError
from app.limits import RateLimiter
from app.store import IdempotencyConflict, SchedulerStore
from app.benchmarks import load_result
from app.telemetry import timeseries

store = SchedulerStore()
# Demo mode enables the destructive reset endpoint and the simulated-failure hook.
# Set DEMO_MODE=false anywhere the API is reachable by people other than you.
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() not in {"0", "false", "no"}


# Public-demo guards. All are off unless set, so local runs behave as before.
_per_minute = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0"))
RATE_LIMITER = RateLimiter(_per_minute) if _per_minute > 0 else None
# Reject new work while this many tasks are queued, running, or blocked (0 = no cap).
MAX_ACTIVE_TASKS = int(os.getenv("MAX_ACTIVE_TASKS", "0"))
# A shared public demo should not let one visitor wipe what others are watching.
ALLOW_DEMO_RESET = os.getenv("ALLOW_DEMO_RESET", "true").lower() not in {"0", "false", "no"}


def require_demo_mode(feature: str) -> None:
    if not DEMO_MODE:
        raise HTTPException(403, f"{feature} is only available when DEMO_MODE is enabled")


def guard_submission(request: Request) -> None:
    """Rate-limit and cap every endpoint that creates work.

    The client address is the socket peer, or the proxy's X-Forwarded-For
    when uvicorn runs with --proxy-headers behind a trusted proxy.
    """
    if RATE_LIMITER is not None:
        client = request.client.host if request.client else "unknown"
        wait = RATE_LIMITER.acquire(client)
        if wait:
            raise HTTPException(429, "Too many submissions; try again shortly",
                                headers={"Retry-After": str(max(1, round(wait)))})
    if MAX_ACTIVE_TASKS and store.count_active() >= MAX_ACTIVE_TASKS:
        raise HTTPException(503, "The demo queue is full; try again in a minute",
                            headers={"Retry-After": "30"})

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    yield

app = FastAPI(title="Distributed AI Task Scheduler", version="1.0.0", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

class TaskCreate(BaseModel):
    prompt: str = Field(min_length=3, max_length=10000)
    sla: str = Field(default="standard", pattern="^(standard|urgent)$")
    priority: int = Field(default=5, ge=1, le=10)
    idempotency_key: str | None = Field(default=None, max_length=160)
    fail_once: bool = False

class WorkflowNode(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=3, max_length=10000)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_NODES)
    sla: str = Field(default="standard", pattern="^(standard|urgent)$")
    priority: int = Field(default=5, ge=1, le=10)
    fail_once: bool = False

class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=MAX_NODES)

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(static_dir / "index.html")

@app.get("/health")
def health():
    worker_rows = store.list_workers()
    return {"status": "ok", "workers": sum(w["status"] != "offline" for w in worker_rows),
            "demo_mode": DEMO_MODE, "demo_reset": DEMO_MODE and ALLOW_DEMO_RESET}

@app.get("/healthz")
def healthz():
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok"}

@app.get("/readyz")
def readyz():
    """Readiness: the database answers, so this instance can serve requests."""
    try:
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}

@app.post("/tasks", status_code=201, dependencies=[Depends(guard_submission)])
def create_task(payload: TaskCreate, response: Response):
    if payload.fail_once:
        require_demo_mode("Simulated failure")
    try:
        task, created = store.create_task(**payload.model_dump())
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not created:
        response.status_code = 200
    return {"task_id": task["id"], "status": task["status"], "route_tier": task["route_tier"],
            "route_reason": task["route_reason"], "created": created}

@app.post("/workflows", status_code=201, dependencies=[Depends(guard_submission)])
def create_workflow(payload: WorkflowCreate):
    """Submit a DAG of tasks; nodes run only after every dependency succeeds."""
    if any(node.fail_once for node in payload.nodes):
        require_demo_mode("Simulated failure")
    try:
        return store.create_workflow(payload.name, [node.model_dump() for node in payload.nodes])
    except DagError as exc:
        raise HTTPException(422, {"problem": exc.problem, "message": str(exc), "nodes": exc.nodes}) from exc

@app.get("/workflows")
def list_workflows(limit: int = Query(20, ge=1, le=100)):
    return store.list_workflows(limit)

@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    workflow = store.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, "Workflow not found")
    return workflow

@app.get("/tasks")
def list_tasks():
    return store.list_tasks()

@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task

@app.get("/metrics/summary")
def metrics():
    return store.metrics()

@app.get("/workers")
def worker_status():
    return store.list_workers()

@app.get("/metrics/timeseries")
def metric_timeseries(minutes: int = Query(default=15, ge=1, le=60)):
    return timeseries(store, minutes)

@app.get("/events")
def event_feed():
    return store.events()

@app.get("/benchmarks/{kind}")
def benchmark_report(kind: str):
    """Return saved evidence independently of live queue/database availability."""
    return load_result(kind)

DEMO_PIPELINE = [
    {"key": "parse", "prompt": "Parse the uploaded contract PDF into plain text."},
    {"key": "chunk", "prompt": "Split the contract text into overlapping chunks.", "depends_on": ["parse"]},
    {"key": "metadata", "prompt": "Extract the parties, dates, and governing law.", "depends_on": ["parse"]},
    {"key": "embed", "prompt": "Generate an embedding for every chunk.", "depends_on": ["chunk", "metadata"]},
    {"key": "index", "prompt": "Write the embeddings and metadata to the vector index.", "depends_on": ["embed"]},
]

@app.post("/demo/workflow", status_code=201, dependencies=[Depends(guard_submission)])
def demo_workflow(scenario: str = Query("retry", pattern="^(retry|failure)$")):
    """Submit the document pipeline. ``retry``: metadata fails once, then recovers.
    ``failure``: metadata fails on its only attempt, so the workflow fails fast."""
    require_demo_mode("Demo workflow")
    nodes = [dict(node) for node in DEMO_PIPELINE]
    flaky = next(node for node in nodes if node["key"] == "metadata")
    flaky["fail_once"] = True
    if scenario == "failure":
        flaky["max_attempts"] = 1
    name = "document-pipeline" if scenario == "retry" else "document-pipeline (failing)"
    return store.create_workflow(name, nodes)

@app.post("/demo/reset", dependencies=[Depends(guard_submission)])
def reset_demo():
    require_demo_mode("Demo reset")
    if not ALLOW_DEMO_RESET:
        raise HTTPException(403, "Demo reset is disabled on this shared deployment")
    store.reset()
    demo = [
        ("Explain why database indexes speed up reads.", "standard", 5, False),
        ("Summarize this customer request into one concise action item.", "urgent", 9, False),
        (("Design a fault-tolerant migration plan with rollback, observability, consistency guarantees, "
          "capacity modeling, security controls, regional failover, validation steps, ownership, and risk mitigation. ") * 6,
         "standard", 7, False),
        ("Classify this support ticket: password reset is not working.", "standard", 4, True),
        ("Extract the order number and delivery date from a short message.", "standard", 3, False),
    ]
    ids = []
    for i, (prompt, sla, priority, fail_once) in enumerate(demo):
        task, _ = store.create_task(prompt, sla, priority, f"demo-{i}", fail_once)
        ids.append(task["id"])
    return {"seeded": len(ids), "task_ids": ids}
