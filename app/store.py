from __future__ import annotations
import os
import threading
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from app.router import route_task

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    sla: Mapped[str] = mapped_column(String(16), default="standard")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    route_tier: Mapped[str] = mapped_column(String(16))
    route_reason: Mapped[str] = mapped_column(Text)
    difficulty_score: Mapped[float] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    fail_once: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

def task_dict(task: Task) -> dict:
    def iso(value): return value.isoformat() if value else None
    # SQLite drops timezone metadata while PostgreSQL preserves it. Normalize both
    # sides for portable duration math; all stored timestamps are UTC.
    def elapsed_ms(later, earlier):
        if not later or not earlier: return None
        return max(0, int((later.replace(tzinfo=None) - earlier.replace(tzinfo=None)).total_seconds() * 1000))
    queue_ms = elapsed_ms(task.started_at, task.created_at)
    run_ms = elapsed_ms(task.finished_at, task.started_at)
    return {"id": task.id, "prompt": task.prompt, "sla": task.sla, "priority": task.priority,
            "route_tier": task.route_tier, "route_reason": task.route_reason,
            "difficulty_score": round(task.difficulty_score, 3), "estimated_cost": task.estimated_cost,
            "status": task.status, "worker_id": task.worker_id, "attempt": task.attempt,
            "max_attempts": task.max_attempts, "result": task.result, "error": task.error,
            "created_at": iso(task.created_at), "started_at": iso(task.started_at),
            "finished_at": iso(task.finished_at), "queue_ms": queue_ms, "run_ms": run_ms}

class SchedulerStore:
    def __init__(self, database_url: str | None = None):
        url = database_url or os.getenv("DATABASE_URL", "sqlite:///./scheduler.db")
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()

    def init(self): Base.metadata.create_all(self.engine)

    def _depths(self, session) -> tuple[int, int]:
        rows = session.execute(select(Task.route_tier, func.count()).where(Task.status == "queued").group_by(Task.route_tier)).all()
        counts = dict(rows)
        return counts.get("small", 0), counts.get("large", 0)

    def create_task(self, prompt: str, sla: str = "standard", priority: int = 5,
                    idempotency_key: str | None = None, fail_once: bool = False) -> tuple[dict, bool]:
        with self.lock, self.Session.begin() as session:
            if idempotency_key:
                existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                if existing: return task_dict(existing), False
            small, large = self._depths(session)
            d = route_task(prompt, sla, small, large)
            task = Task(id=str(uuid.uuid4()), prompt=prompt, sla=sla, priority=priority,
                        idempotency_key=idempotency_key, route_tier=d.tier, route_reason=d.reason,
                        difficulty_score=d.difficulty, estimated_cost=d.estimated_cost, fail_once=fail_once)
            session.add(task)
            session.add(Event(task_id=task.id, kind="routed", message=f"Routed to {d.tier.upper()}: {d.reason}"))
            return task_dict(task), True

    def claim(self, worker_id: str, tier: str) -> dict | None:
        with self.lock, self.Session.begin() as session:
            stmt = select(Task).where(Task.status == "queued", Task.route_tier == tier).order_by(Task.priority.desc(), Task.created_at.asc()).limit(1)
            if self.engine.dialect.name == "postgresql": stmt = stmt.with_for_update(skip_locked=True)
            task = session.scalar(stmt)
            if not task: return None
            task.status, task.worker_id, task.started_at = "running", worker_id, utcnow()
            task.attempt += 1
            task.error = None
            session.add(Event(task_id=task.id, kind="claimed", message=f"{worker_id} claimed attempt {task.attempt}"))
            return task_dict(task)

    def finish(self, task_id: str, worker_id: str, attempt: int, result: str) -> bool:
        with self.lock, self.Session.begin() as session:
            task = session.get(Task, task_id)
            if not task or task.status != "running" or task.worker_id != worker_id or task.attempt != attempt: return False
            task.status, task.result, task.finished_at = "succeeded", result, utcnow()
            session.add(Event(task_id=task.id, kind="succeeded", message=f"{worker_id} completed the task"))
            return True

    def fail_or_retry(self, task_id: str, worker_id: str, attempt: int, error: str) -> str:
        with self.lock, self.Session.begin() as session:
            task = session.get(Task, task_id)
            if not task or task.status != "running" or task.worker_id != worker_id or task.attempt != attempt: return "fenced"
            task.error, task.worker_id = error, None
            if task.attempt < task.max_attempts:
                task.status = "queued"
                session.add(Event(task_id=task.id, kind="retry", message=f"Attempt {task.attempt} failed; requeued"))
                return "queued"
            task.status, task.finished_at = "failed", utcnow()
            session.add(Event(task_id=task.id, kind="failed", message=f"Failed after {task.attempt} attempts"))
            return "failed"

    def should_fail(self, task_id: str, attempt: int) -> bool:
        with self.Session() as session:
            task = session.get(Task, task_id)
            return bool(task and task.fail_once and attempt == 1)

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self.Session() as session:
            return [task_dict(t) for t in session.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)).all()]

    def get_task(self, task_id: str) -> dict | None:
        with self.Session() as session:
            task = session.get(Task, task_id)
            return task_dict(task) if task else None

    def events(self, limit: int = 30) -> list[dict]:
        with self.Session() as session:
            rows = session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
            return [{"id": e.id, "task_id": e.task_id, "kind": e.kind, "message": e.message, "created_at": e.created_at.isoformat()} for e in rows]

    def metrics(self) -> dict:
        tasks = self.list_tasks(10000)
        total, succeeded = len(tasks), sum(t["status"] == "succeeded" for t in tasks)
        terminal = sum(t["status"] in ("succeeded", "failed") for t in tasks)
        latencies = [t["queue_ms"] + t["run_ms"] for t in tasks if t["queue_ms"] is not None and t["run_ms"] is not None]
        spent = sum(t["estimated_cost"] for t in tasks)
        return {"total": total, "queued": sum(t["status"] == "queued" for t in tasks),
                "running": sum(t["status"] == "running" for t in tasks), "succeeded": succeeded,
                "failed": sum(t["status"] == "failed" for t in tasks),
                "success_rate": round((succeeded / terminal * 100) if terminal else 100, 1),
                "small": sum(t["route_tier"] == "small" for t in tasks), "large": sum(t["route_tier"] == "large" for t in tasks),
                "retries": sum(max(0, t["attempt"] - 1) for t in tasks), "estimated_cost": round(spent, 3),
                "all_large_cost": round(total * 0.018, 3),
                "cost_saved_pct": round((1 - spent / (total * 0.018)) * 100, 1) if total else 0,
                "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0}

    def reset(self):
        with self.lock, self.Session.begin() as session:
            session.query(Event).delete()
            session.query(Task).delete()
