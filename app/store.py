from __future__ import annotations
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, case, create_engine, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
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
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
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

class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_tier: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="idle")
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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
            "max_attempts": task.max_attempts, "lease_until": iso(task.lease_until),
            "recovery_count": task.recovery_count, "result": task.result, "error": task.error,
            "created_at": iso(task.created_at), "started_at": iso(task.started_at),
            "finished_at": iso(task.finished_at), "queue_ms": queue_ms, "run_ms": run_ms}

class SchedulerStore:
    def __init__(self, database_url: str | None = None):
        url = database_url or os.getenv("DATABASE_URL", "sqlite:///./scheduler.db")
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()

    def init(self):
        Base.metadata.create_all(self.engine)
        self._migrate_task_columns()

    def _migrate_task_columns(self) -> None:
        """Apply the two additive v1 columns to databases created by the MVP.

        This keeps existing local SQLite files and PostgreSQL volumes usable without
        introducing a migration framework solely for this additive schema change.
        """
        existing = {column["name"] for column in inspect(self.engine).get_columns("tasks")}
        statements = []
        if "lease_until" not in existing:
            column_type = "TIMESTAMPTZ" if self.engine.dialect.name == "postgresql" else "DATETIME"
            statements.append(f"ALTER TABLE tasks ADD COLUMN lease_until {column_type}")
        if "recovery_count" not in existing:
            statements.append("ALTER TABLE tasks ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
        for statement in statements:
            try:
                with self.engine.begin() as connection:
                    connection.execute(text(statement))
            except (OperationalError, ProgrammingError) as exc:
                # API and workers can start together. A concurrent process may have
                # added the column after inspection; only ignore that exact race.
                message = str(exc).lower()
                if "duplicate column" not in message and "already exists" not in message:
                    raise

    def _depths(self, session) -> tuple[int, int]:
        rows = session.execute(select(Task.route_tier, func.count()).where(Task.status == "queued").group_by(Task.route_tier)).all()
        counts = dict(rows)
        return counts.get("small", 0), counts.get("large", 0)

    def create_task(self, prompt: str, sla: str = "standard", priority: int = 5,
                    idempotency_key: str | None = None, fail_once: bool = False) -> tuple[dict, bool]:
        try:
            with self.lock, self.Session.begin() as session:
                if idempotency_key is not None:
                    existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                    if existing: return task_dict(existing), False
                small, large = self._depths(session)
                d = route_task(prompt, sla, small, large)
                task = Task(id=str(uuid.uuid4()), prompt=prompt, sla=sla, priority=priority,
                            idempotency_key=idempotency_key, route_tier=d.tier, route_reason=d.reason,
                            difficulty_score=d.difficulty, estimated_cost=d.estimated_cost, fail_once=fail_once)
                session.add(task)
                session.add(Event(task_id=task.id, kind="routed", message=f"Routed to {d.tier.upper()}: {d.reason}"))
                # Apply defaults and detect uniqueness conflicts before serializing.
                session.flush()
                return task_dict(task), True
        except IntegrityError:
            # Another API process may win after our initial lookup. The failed
            # transaction (including its routed event) has rolled back here.
            if idempotency_key is not None:
                with self.Session() as session:
                    existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                    if existing: return task_dict(existing), False
            raise

    def claim(self, worker_id: str, tier: str, lease_seconds: float = 15.0) -> dict | None:
        with self.lock, self.Session.begin() as session:
            stmt = select(Task).where(Task.status == "queued", Task.route_tier == tier).order_by(Task.priority.desc(), Task.created_at.asc()).limit(1)
            if self.engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            task = session.scalar(stmt)
            if not task: return None
            if self.engine.dialect.name == "sqlite":
                claimed = session.execute(
                    update(Task).where(Task.id == task.id, Task.status == "queued").values(
                        status="running", worker_id=worker_id, started_at=utcnow(),
                        lease_until=utcnow() + timedelta(seconds=lease_seconds),
                        attempt=Task.attempt + 1, error=None
                    )
                )
                if claimed.rowcount != 1:
                    return None
                session.flush()
                session.refresh(task)
            else:
                task.status, task.worker_id, task.started_at = "running", worker_id, utcnow()
                task.lease_until = utcnow() + timedelta(seconds=lease_seconds)
                task.attempt += 1
                task.error = None
            session.add(Event(task_id=task.id, kind="claimed", message=f"{worker_id} claimed attempt {task.attempt}"))
            return task_dict(task)

    def renew_lease(self, task_id: str, worker_id: str, attempt: int,
                    lease_seconds: float = 15.0) -> bool:
        """Extend a lease only if the caller still owns the exact attempt."""
        with self.Session.begin() as session:
            renewed = session.execute(
                update(Task).where(
                    Task.id == task_id,
                    Task.worker_id == worker_id,
                    Task.attempt == attempt,
                    Task.status == "running",
                ).values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
            )
            return renewed.rowcount == 1

    def get_expired_tasks(self, now: datetime | None = None) -> list[dict]:
        cutoff = now or utcnow()
        with self.Session() as session:
            tasks = session.scalars(
                select(Task).where(
                    Task.status == "running",
                    Task.lease_until.is_not(None),
                    Task.lease_until < cutoff,
                ).order_by(Task.lease_until.asc())
            ).all()
            return [task_dict(task) for task in tasks]

    def requeue_stale_tasks(self, now: datetime | None = None) -> list[str]:
        """Recover expired work without racing a concurrent lease renewal."""
        cutoff = now or utcnow()
        recovered: list[str] = []
        with self.lock, self.Session.begin() as session:
            stmt = select(Task).where(
                Task.status == "running",
                Task.lease_until.is_not(None),
                Task.lease_until < cutoff,
            ).order_by(Task.lease_until.asc())
            if self.engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            for task in session.scalars(stmt).all():
                previous_worker = task.worker_id
                previous_attempt = task.attempt
                if task.attempt >= task.max_attempts:
                    values = {
                        "status": "failed", "worker_id": None, "lease_until": None,
                        "finished_at": cutoff, "error": "Lease expired after final attempt",
                        "recovery_count": Task.recovery_count + 1,
                    }
                    event_kind = "failed"
                    message = f"Lease expired on final attempt {previous_attempt}; task failed"
                else:
                    values = {
                        "status": "queued", "worker_id": None, "lease_until": None,
                        "started_at": None, "error": "Worker lease expired; task recovered",
                        "recovery_count": Task.recovery_count + 1,
                    }
                    event_kind = "recovered"
                    message = f"Recovered expired attempt {previous_attempt} from {previous_worker}"
                changed = session.execute(
                    update(Task).where(
                        Task.id == task.id,
                        Task.status == "running",
                        Task.worker_id == previous_worker,
                        Task.attempt == previous_attempt,
                        Task.lease_until < cutoff,
                    ).values(**values).execution_options(synchronize_session=False)
                )
                if changed.rowcount == 1:
                    recovered.append(task.id)
                    session.add(Event(task_id=task.id, kind=event_kind, message=message))
        return recovered

    def heartbeat(self, worker_id: str, tier: str, status: str = "idle",
                  task_id: str | None = None) -> None:
        with self.Session.begin() as session:
            worker = session.get(Worker, worker_id)
            if worker is None:
                worker = Worker(id=worker_id, model_tier=tier)
                session.add(worker)
            worker.model_tier = tier
            worker.status = status
            worker.current_task_id = task_id
            worker.last_heartbeat = utcnow()

    def mark_worker_offline(self, worker_id: str) -> None:
        with self.Session.begin() as session:
            worker = session.get(Worker, worker_id)
            if worker:
                worker.status = "offline"
                worker.current_task_id = None
                worker.last_heartbeat = utcnow()

    def list_workers(self, stale_after_seconds: float = 8.0) -> list[dict]:
        now = utcnow().replace(tzinfo=None)
        with self.Session() as session:
            rows = session.scalars(select(Worker).order_by(Worker.id)).all()
            result = []
            for worker in rows:
                heartbeat = worker.last_heartbeat
                age = (now - heartbeat.replace(tzinfo=None)).total_seconds()
                status = "offline" if age > stale_after_seconds else worker.status
                result.append({"id": worker.id, "tier": worker.model_tier, "status": status,
                               "task_id": worker.current_task_id if status != "offline" else None,
                               "last_heartbeat": heartbeat.isoformat()})
            return result

    def finish(self, task_id: str, worker_id: str, attempt: int, result: str) -> bool:
        with self.lock, self.Session.begin() as session:
            # Ownership must be checked by the write itself: a Python lock only
            # protects this store instance, not recovery/worker processes.
            changed = session.execute(
                update(Task).where(
                    Task.id == task_id, Task.worker_id == worker_id,
                    Task.attempt == attempt, Task.status == "running",
                ).values(status="succeeded", result=result, finished_at=utcnow(), lease_until=None)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1: return False
            session.add(Event(task_id=task_id, kind="succeeded", message=f"{worker_id} completed the task"))
            return True

    def fail_or_retry(self, task_id: str, worker_id: str, attempt: int, error: str) -> str:
        with self.lock, self.Session.begin() as session:
            retryable = Task.attempt < Task.max_attempts
            changed = session.execute(
                update(Task).where(
                    Task.id == task_id, Task.worker_id == worker_id,
                    Task.attempt == attempt, Task.status == "running",
                ).values(
                    error=error, worker_id=None, lease_until=None,
                    status=case((retryable, "queued"), else_="failed"),
                    finished_at=case((retryable, None), else_=utcnow()),
                ).execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1: return "fenced"
            # The UPDATE holds the row's write lock until this transaction commits.
            status = session.scalar(select(Task.status).where(Task.id == task_id))
            kind = "retry" if status == "queued" else "failed"
            message = (f"Attempt {attempt} failed; requeued" if status == "queued"
                       else f"Failed after {attempt} attempts")
            session.add(Event(task_id=task_id, kind=kind, message=message))
            return status

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
                "recoveries": sum(t["recovery_count"] for t in tasks),
                "all_large_cost": round(total * 0.018, 3),
                "cost_saved_pct": round((1 - spent / (total * 0.018)) * 100, 1) if total else 0,
                "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0}

    def reset(self):
        with self.lock, self.Session.begin() as session:
            session.query(Event).delete()
            session.query(Task).delete()
