from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.store import SchedulerStore

store = SchedulerStore()

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

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(static_dir / "index.html")

@app.get("/health")
def health():
    worker_rows = store.list_workers()
    return {"status": "ok", "workers": sum(w["status"] != "offline" for w in worker_rows)}

@app.post("/tasks", status_code=201)
def create_task(payload: TaskCreate, response: Response):
    task, created = store.create_task(**payload.model_dump())
    if not created:
        response.status_code = 200
    return {"task_id": task["id"], "status": task["status"], "route_tier": task["route_tier"],
            "route_reason": task["route_reason"], "created": created}

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

@app.get("/events")
def event_feed():
    return store.events()

@app.post("/demo/reset")
def reset_demo():
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
