from __future__ import annotations
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from sqlalchemy import Boolean, DateTime, Float, Index, Integer, Interval, String, Text, bindparam, case, cast, create_engine, func, insert, inspect, literal, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from app.dag import DagError, topological_order
from app.router import TIER_COST, route_task

class IdempotencyConflict(Exception):
    """An idempotency key was reused with a different request body."""

def request_fingerprint(prompt: str, sla: str, priority: int, fail_once: bool) -> str:
    """Hash the fields that define a logical task, for idempotency-key reuse checks."""
    return sha256(json.dumps([prompt, sla, priority, fail_once]).encode()).hexdigest()

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def as_utc(value: datetime) -> datetime:
    """Normalize database timestamps to aware UTC.

    SQLite returns naive values (stored as UTC); PostgreSQL returns aware values in
    the connection's TimeZone, which is not necessarily UTC.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def retry_delay_seconds(task_id: str, attempt: int, base_seconds: float = 1.0,
                        max_seconds: float = 30.0) -> float:
    """Return capped exponential backoff with stable per-task jitter.

    The 50-100% jitter range spreads retries without making tests and incident
    timelines nondeterministic. ``attempt`` is the failed attempt (1-based).
    """
    if attempt < 1 or base_seconds <= 0 or max_seconds <= 0:
        raise ValueError("attempt, base_seconds, and max_seconds must be positive")
    digest = sha256(f"{task_id}:{attempt}".encode()).digest()
    jitter = 0.5 + int.from_bytes(digest[:8], "big") / (2**64 - 1) * 0.5
    return min(max_seconds, base_seconds * (2 ** (attempt - 1))) * jitter

class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    # Fingerprint of the submitted fields; NULL for rows created before it existed.
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    sla: Mapped[str] = mapped_column(String(16), default="standard")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    route_tier: Mapped[str] = mapped_column(String(16))
    route_reason: Mapped[str] = mapped_column(Text)
    difficulty_score: Mapped[float] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float)
    # queued -> running -> succeeded | failed; workflow nodes start "blocked"
    # until every dependency succeeds. Claims only take "queued" rows.
    status: Mapped[str] = mapped_column(String(16), default="queued")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    fail_once: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only for workflow nodes; standalone tasks leave both NULL.
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Claim path: WHERE status/route_tier ORDER BY priority DESC, created_at.
        Index("ix_tasks_claim", "status", "route_tier", "priority", "created_at"),
        # Recovery scan: WHERE status = 'running' AND lease_until < now.
        Index("ix_tasks_lease", "status", "lease_until"),
        # One task per node key in a workflow; NULLs (standalone tasks) never collide.
        Index("ux_tasks_workflow_node", "workflow_id", "node_key", unique=True),
    )

class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class TaskDependency(Base):
    """An edge: ``task_id`` may run only after ``depends_on_task_id`` succeeds."""
    __tablename__ = "task_dependencies"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    depends_on_task_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Unlocking walks edges from a finished task to the tasks that wait on it.
    __table_args__ = (Index("ix_task_dependencies_upstream", "depends_on_task_id"),)

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
    def elapsed_ms(later, earlier):
        if not later or not earlier: return None
        return max(0, int((as_utc(later) - as_utc(earlier)).total_seconds() * 1000))
    queue_ms = elapsed_ms(task.started_at, task.created_at)
    run_ms = elapsed_ms(task.finished_at, task.started_at)
    return {"id": task.id, "prompt": task.prompt, "sla": task.sla, "priority": task.priority,
            "route_tier": task.route_tier, "route_reason": task.route_reason,
            "difficulty_score": round(task.difficulty_score, 3), "estimated_cost": task.estimated_cost,
            "status": task.status, "worker_id": task.worker_id, "attempt": task.attempt,
            "max_attempts": task.max_attempts, "lease_until": iso(task.lease_until),
            "next_attempt_at": iso(task.next_attempt_at),
            "recovery_count": task.recovery_count, "result": task.result, "error": task.error,
            "created_at": iso(task.created_at), "started_at": iso(task.started_at),
            "finished_at": iso(task.finished_at), "queue_ms": queue_ms, "run_ms": run_ms,
            "workflow_id": task.workflow_id, "node_key": task.node_key}

class SchedulerStore:
    def __init__(self, database_url: str | None = None,
                 retry_base_seconds: float | None = None,
                 retry_max_seconds: float | None = None):
        url = database_url or os.getenv("DATABASE_URL", "sqlite:///./scheduler.db")
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()
        self.retry_base_seconds = (float(os.getenv("RETRY_BASE_SECONDS", "1"))
                                   if retry_base_seconds is None else retry_base_seconds)
        self.retry_max_seconds = (float(os.getenv("RETRY_MAX_SECONDS", "30"))
                                  if retry_max_seconds is None else retry_max_seconds)
        if self.retry_base_seconds <= 0 or self.retry_max_seconds <= 0:
            raise ValueError("retry backoff settings must be positive")

    def init(self):
        Base.metadata.create_all(self.engine)
        self._migrate_task_columns()
        self._ensure_task_indexes()

    def _ensure_task_indexes(self) -> None:
        """Add indexes that databases created by earlier versions are missing."""
        existing = {column["name"] for column in inspect(self.engine).get_columns("tasks")}
        for index in Task.__table__.indexes:
            if not {column.name for column in index.columns} <= existing:
                continue
            try:
                index.create(self.engine, checkfirst=True)
            except (OperationalError, ProgrammingError, IntegrityError) as exc:
                # Same startup race as the column migration: another process won.
                message = str(exc).lower()
                if "already exists" not in message and "duplicate key" not in message:
                    raise

    def _migrate_task_columns(self) -> None:
        """Apply additive columns to databases created by earlier versions.

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
        if "request_hash" not in existing:
            statements.append("ALTER TABLE tasks ADD COLUMN request_hash VARCHAR(64)")
        if "workflow_id" not in existing:
            statements.append("ALTER TABLE tasks ADD COLUMN workflow_id VARCHAR(36)")
        if "node_key" not in existing:
            statements.append("ALTER TABLE tasks ADD COLUMN node_key VARCHAR(64)")
        if "next_attempt_at" not in existing:
            column_type = "TIMESTAMPTZ" if self.engine.dialect.name == "postgresql" else "DATETIME"
            statements.append(f"ALTER TABLE tasks ADD COLUMN next_attempt_at {column_type}")
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

    def _db_now(self, session) -> datetime:
        """Return the database clock so leases never depend on a worker's clock.

        Workers, the API, and the recovery monitor may run on different machines;
        comparing lease deadlines computed from their local clocks would let skew
        recover live work early or keep dead work leased. PostgreSQL ``now()`` is
        the transaction start time, so one transaction sees one consistent instant.
        SQLite is a single-machine development backend and uses the local clock.
        Use this only off the hot path; it costs a round trip (see ``_now_sql``).
        """
        if self.engine.dialect.name == "postgresql":
            return as_utc(session.scalar(select(func.now())))
        return utcnow()

    def _now_sql(self):
        """The database clock as a SQL expression, evaluated inside the statement.

        Claims, renewals, and write-backs embed ``now()`` in the statement they
        already run; fetching it first would add a round trip to every step.
        """
        return func.now() if self.engine.dialect.name == "postgresql" else utcnow()

    def _depths(self, session) -> tuple[int, int]:
        rows = session.execute(select(Task.route_tier, func.count()).where(Task.status == "queued").group_by(Task.route_tier)).all()
        counts = dict(rows)
        return counts.get("small", 0), counts.get("large", 0)

    @staticmethod
    def _existing_for_key(existing: Task, request_hash: str) -> tuple[dict, bool]:
        # Legacy rows have no fingerprint and cannot be checked; accept them.
        if existing.request_hash is not None and existing.request_hash != request_hash:
            raise IdempotencyConflict("idempotency key was already used with a different request")
        return task_dict(existing), False

    def create_task(self, prompt: str, sla: str = "standard", priority: int = 5,
                    idempotency_key: str | None = None, fail_once: bool = False) -> tuple[dict, bool]:
        request_hash = request_fingerprint(prompt, sla, priority, fail_once)
        try:
            with self.lock, self.Session.begin() as session:
                if idempotency_key is not None:
                    existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                    if existing: return self._existing_for_key(existing, request_hash)
                small, large = self._depths(session)
                d = route_task(prompt, sla, small, large)
                task = Task(id=str(uuid.uuid4()), prompt=prompt, sla=sla, priority=priority,
                            idempotency_key=idempotency_key, request_hash=request_hash, route_tier=d.tier, route_reason=d.reason,
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
                    if existing: return self._existing_for_key(existing, request_hash)
            raise

    def claim(self, worker_id: str, tier: str, lease_seconds: float = 15.0) -> dict | None:
        if self.engine.dialect.name == "postgresql":
            return self._claim_one_statement(worker_id, tier, lease_seconds)
        with self.lock, self.Session.begin() as session:
            now = self._now_sql()
            eligible = or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now)
            postgres = self.engine.dialect.name == "postgresql"
            # PostgreSQL returns its clock with the claimed row: no extra round trip.
            stmt = select(Task, now) if postgres else select(Task)
            stmt = stmt.where(
                Task.status == "queued", Task.route_tier == tier, eligible,
            ).order_by(Task.priority.desc(), Task.created_at.asc()).limit(1)
            if postgres:
                stmt = stmt.with_for_update(skip_locked=True, of=Task)
            row = session.execute(stmt).first()
            if not row: return None
            task, claimed_at = (row[0], as_utc(row[1])) if postgres else (row[0], now)
            if self.engine.dialect.name == "sqlite":
                claimed = session.execute(
                    update(Task).where(Task.id == task.id, Task.status == "queued", eligible).values(
                        status="running", worker_id=worker_id, started_at=claimed_at,
                        lease_until=claimed_at + timedelta(seconds=lease_seconds),
                        next_attempt_at=None, attempt=Task.attempt + 1, error=None
                    ).execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    return None
                session.flush()
                session.refresh(task)
            else:
                task.status, task.worker_id, task.started_at = "running", worker_id, claimed_at
                task.lease_until = claimed_at + timedelta(seconds=lease_seconds)
                task.next_attempt_at = None
                task.attempt += 1
                task.error = None
            session.add(Event(task_id=task.id, kind="claimed", message=f"{worker_id} claimed attempt {task.attempt}"))
            # The busy heartbeat rides in the claim transaction instead of its own.
            self._set_worker_state(session, worker_id, "busy", task.id, tier)
            claimed_task = task_dict(task)
            # Returned with the claim so the worker needs no extra lookup.
            claimed_task["fail_once"] = task.fail_once
            return claimed_task

    def _autocommit(self):
        """A connection for single-statement writes, which are atomic on their own.

        PostgreSQL hot-path writes are one statement each, so they skip the
        separate BEGIN and COMMIT round trips. Built from the current engine so
        engine options (such as a test schema map) still apply.
        """
        return self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    @staticmethod
    @lru_cache(maxsize=1)
    def _claim_statement():
        """Claim, record the event, and mark the worker busy in one statement.

        Data-modifying CTEs pick the next task with SKIP LOCKED, claim it, insert
        its event, and record the worker heartbeat. When nothing is claimed, the
        dependent inserts see no rows and write nothing. Built once with bound
        parameters: constructing the statement tree costs far more client CPU
        than the round trip it saves. Parameter names carry a ``p_`` prefix
        because an UPDATE treats a parameter named like a column as a SET value.
        """
        now = func.now()
        worker_id, tier = bindparam("p_worker_id", type_=String), bindparam("p_tier", type_=String)
        picked = (
            select(Task.id)
            .where(Task.status == "queued", Task.route_tier == tier,
                   or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now))
            .order_by(Task.priority.desc(), Task.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        claimed = (
            update(Task)
            .where(Task.id == picked.c.id)
            .values(status="running", worker_id=worker_id, started_at=now,
                    lease_until=now + bindparam("p_lease", type_=Interval),
                    next_attempt_at=None, attempt=Task.attempt + 1, error=None)
            .returning(*Task.__table__.c)
            .cte("claimed")
        )
        claim_event = insert(Event).from_select(
            ["task_id", "kind", "message", "created_at"],
            select(claimed.c.id, literal("claimed"),
                   worker_id + literal(" claimed attempt ") + cast(claimed.c.attempt, String),
                   now),
        ).cte("claim_event")
        # Two cacheable CTEs instead of an ON CONFLICT upsert: SQLAlchemy cannot
        # cache PostgreSQL-specific inserts, so the claim would be recompiled on
        # every call. Both CTEs see one snapshot, so exactly one applies: update a
        # known worker, or register a worker seen for the first time. Worker IDs
        # are unique per worker; if two processes shared one and registered at the
        # same instant, the loser's claim would roll back whole and be retried.
        heartbeat = (
            update(Worker)
            .where(Worker.id == worker_id, Worker.id.in_(select(claimed.c.worker_id)))
            .values(status="busy", current_task_id=select(claimed.c.id).scalar_subquery(),
                    last_heartbeat=now)
            .cte("heartbeat")
        )
        known = select(Worker.id).where(Worker.id == worker_id).exists()
        register = insert(Worker).from_select(
            ["id", "model_tier", "status", "current_task_id", "last_heartbeat"],
            select(worker_id, tier, literal("busy"), claimed.c.id, now).where(~known),
        ).cte("register")
        return select(claimed).add_cte(claim_event).add_cte(heartbeat).add_cte(register)

    @staticmethod
    @lru_cache(maxsize=1)
    def _finish_statement():
        """Fenced completion, its event, and the idle heartbeat in one statement."""
        now = func.now()
        worker_id = bindparam("p_worker_id", type_=String)
        done = (
            update(Task)
            .where(Task.id == bindparam("p_task_id", type_=String), Task.worker_id == worker_id,
                   Task.attempt == bindparam("p_attempt", type_=Integer), Task.status == "running")
            .values(status="succeeded", result=bindparam("p_result", type_=Text), finished_at=now,
                    lease_until=None, next_attempt_at=None)
            .returning(Task.id)
            .cte("done")
        )
        finish_event = insert(Event).from_select(
            ["task_id", "kind", "message", "created_at"],
            select(done.c.id, literal("succeeded"),
                   worker_id + literal(" completed the task"), now),
        ).cte("finish_event")
        # The worker is idle again whether or not its write was accepted.
        heartbeat = (
            update(Worker).where(Worker.id == worker_id)
            .values(status="idle", current_task_id=None, last_heartbeat=now)
            .cte("heartbeat")
        )
        return select(func.count()).select_from(done).add_cte(finish_event).add_cte(heartbeat)

    def _claim_one_statement(self, worker_id: str, tier: str,
                             lease_seconds: float) -> dict | None:
        params = {"p_worker_id": worker_id, "p_tier": tier,
                  "p_lease": timedelta(seconds=lease_seconds)}
        with self.lock, self._autocommit() as connection:
            row = connection.execute(self._claim_statement(), params).mappings().first()
        if row is None:
            return None
        claimed_task = task_dict(Task(**row))
        claimed_task["fail_once"] = row["fail_once"]
        return claimed_task

    def _finish_one_statement(self, task_id: str, worker_id: str, attempt: int,
                              result: str) -> bool:
        params = {"p_task_id": task_id, "p_worker_id": worker_id, "p_attempt": attempt,
                  "p_result": result}
        with self.lock, self._autocommit() as connection:
            return connection.execute(self._finish_statement(), params).scalar_one() == 1

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
                ).values(lease_until=self._now_sql() + timedelta(seconds=lease_seconds))
            )
            if renewed.rowcount != 1:
                return False
            # Same transaction as the fenced renewal: a renewal that loses the race
            # with the write-back cannot mark the worker busy again.
            self._set_worker_state(session, worker_id, "busy", task_id)
            return True

    def get_expired_tasks(self, now: datetime | None = None) -> list[dict]:
        with self.Session() as session:
            cutoff = now or self._db_now(session)
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
        recovered: list[str] = []
        with self.lock, self.Session.begin() as session:
            cutoff = now or self._db_now(session)
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
                        "next_attempt_at": None,
                        "finished_at": cutoff, "error": "Lease expired after final attempt",
                        "recovery_count": Task.recovery_count + 1,
                    }
                    event_kind = "failed"
                    message = f"Lease expired on final attempt {previous_attempt}; task failed"
                else:
                    values = {
                        "status": "queued", "worker_id": None, "lease_until": None,
                        "next_attempt_at": None,
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

    def _set_worker_state(self, session, worker_id: str, status: str,
                          task_id: str | None, tier: str | None = None) -> None:
        """Record a worker heartbeat inside the caller's transaction."""
        updated = session.execute(
            update(Worker).where(Worker.id == worker_id).values(
                status=status, current_task_id=task_id, last_heartbeat=self._now_sql(),
            ).execution_options(synchronize_session=False)
        )
        if updated.rowcount == 0 and tier is not None:
            session.add(Worker(id=worker_id, model_tier=tier, status=status,
                               current_task_id=task_id, last_heartbeat=self._now_sql()))

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
            worker.last_heartbeat = self._now_sql()

    def mark_worker_offline(self, worker_id: str) -> None:
        with self.Session.begin() as session:
            worker = session.get(Worker, worker_id)
            if worker:
                worker.status = "offline"
                worker.current_task_id = None
                worker.last_heartbeat = self._now_sql()

    def list_workers(self, stale_after_seconds: float = 8.0) -> list[dict]:
        with self.Session() as session:
            now = self._db_now(session)
            rows = session.scalars(select(Worker).order_by(Worker.id)).all()
            result = []
            for worker in rows:
                heartbeat = as_utc(worker.last_heartbeat)
                age = (now - heartbeat).total_seconds()
                status = "offline" if age > stale_after_seconds else worker.status
                result.append({"id": worker.id, "tier": worker.model_tier, "status": status,
                               "task_id": worker.current_task_id if status != "offline" else None,
                               "last_heartbeat": heartbeat.isoformat()})
            return result

    def finish(self, task_id: str, worker_id: str, attempt: int, result: str) -> bool:
        if self.engine.dialect.name == "postgresql":
            return self._finish_one_statement(task_id, worker_id, attempt, result)
        with self.lock, self.Session.begin() as session:
            # Ownership must be checked by the write itself: a Python lock only
            # protects this store instance, not recovery/worker processes.
            changed = session.execute(
                update(Task).where(
                    Task.id == task_id, Task.worker_id == worker_id,
                    Task.attempt == attempt, Task.status == "running",
                ).values(status="succeeded", result=result, finished_at=self._now_sql(),
                         lease_until=None, next_attempt_at=None)
                .execution_options(synchronize_session=False)
            )
            # The worker is idle again whether or not its write was accepted.
            self._set_worker_state(session, worker_id, "idle", None)
            if changed.rowcount != 1: return False
            session.add(Event(task_id=task_id, kind="succeeded", message=f"{worker_id} completed the task"))
            return True

    def fail_or_retry(self, task_id: str, worker_id: str, attempt: int, error: str) -> str:
        with self.lock, self.Session.begin() as session:
            retryable = Task.attempt < Task.max_attempts
            failed_at = self._now_sql()
            delay = retry_delay_seconds(
                task_id, attempt, self.retry_base_seconds, self.retry_max_seconds
            )
            retry_at = failed_at + timedelta(seconds=delay)
            changed = session.execute(
                update(Task).where(
                    Task.id == task_id, Task.worker_id == worker_id,
                    Task.attempt == attempt, Task.status == "running",
                ).values(
                    error=error, worker_id=None, lease_until=None,
                    status=case((retryable, "queued"), else_="failed"),
                    next_attempt_at=case((retryable, retry_at), else_=None),
                    finished_at=case((retryable, None), else_=failed_at),
                ).execution_options(synchronize_session=False)
            )
            self._set_worker_state(session, worker_id, "idle", None)
            if changed.rowcount != 1: return "fenced"
            # The UPDATE holds the row's write lock until this transaction commits.
            status = session.scalar(select(Task.status).where(Task.id == task_id))
            kind = "retry" if status == "queued" else "failed"
            message = (f"Attempt {attempt} failed; retry in {delay:.2f}s" if status == "queued"
                       else f"Failed after {attempt} attempts")
            session.add(Event(task_id=task_id, kind=kind, message=message))
            return status

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with self.Session() as session:
            return [task_dict(t) for t in session.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)).all()]

    def create_workflow(self, name: str, nodes: list[dict]) -> dict:
        """Write a workflow, its tasks, and its edges in one transaction.

        Each node is ``{"key", "prompt", "depends_on", "sla", "priority"}``. The
        graph is validated first (raising DagError), so nothing is written for
        an invalid workflow. Nodes without dependencies start queued; the rest
        start blocked. Every node is routed at submission like a standalone task.
        """
        keys = [node["key"] for node in nodes]
        # A dict would silently merge repeated keys, so check before building one.
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise DagError("duplicate_key", f"node key {duplicates[0]!r} is used twice", duplicates)
        order = topological_order({node["key"]: list(node.get("depends_on", [])) for node in nodes})
        by_key = {node["key"]: node for node in nodes}
        workflow_id = str(uuid.uuid4())
        task_ids = {key: str(uuid.uuid4()) for key in order}
        with self.lock, self.Session.begin() as session:
            session.add(Workflow(id=workflow_id, name=name))
            small, large = self._depths(session)
            for key in order:
                node = by_key[key]
                needs = list(node.get("depends_on", []))
                decision = route_task(node["prompt"], node.get("sla", "standard"), small, large)
                status = "blocked" if needs else "queued"
                # Later nodes see the queue depth this workflow has already added.
                if status == "queued":
                    small, large = (small + 1, large) if decision.tier == "small" else (small, large + 1)
                session.add(Task(
                    id=task_ids[key], workflow_id=workflow_id, node_key=key,
                    prompt=node["prompt"], sla=node.get("sla", "standard"),
                    priority=node.get("priority", 5), status=status,
                    route_tier=decision.tier, route_reason=decision.reason,
                    difficulty_score=decision.difficulty, estimated_cost=decision.estimated_cost,
                    request_hash=request_fingerprint(node["prompt"], node.get("sla", "standard"),
                                                     node.get("priority", 5), False),
                ))
                session.add_all(TaskDependency(task_id=task_ids[key], depends_on_task_id=task_ids[need])
                                for need in needs)
                session.add(Event(task_id=task_ids[key], kind="routed",
                                  message=f"{key}: routed to {decision.tier.upper()}: {decision.reason}"))
            session.flush()
        return self.get_workflow(workflow_id)

    def get_workflow(self, workflow_id: str) -> dict | None:
        """A workflow and its nodes in dependency order, each with its upstream keys."""
        with self.Session() as session:
            workflow = session.get(Workflow, workflow_id)
            if workflow is None:
                return None
            tasks = session.scalars(select(Task).where(Task.workflow_id == workflow_id)).all()
            key_of = {task.id: task.node_key for task in tasks}
            edges = session.execute(
                select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
                .where(TaskDependency.task_id.in_(list(key_of)))
            ).all()
        needs = {key: [] for key in key_of.values()}
        for task_id, upstream_id in edges:
            needs[key_of[task_id]].append(key_of[upstream_id])
        nodes = {task.node_key: task for task in tasks}
        order = topological_order({key: sorted(needs[key]) for key in sorted(needs)})
        iso = lambda value: value.isoformat() if value else None
        return {
            "id": workflow.id, "name": workflow.name, "status": workflow.status,
            "created_at": iso(workflow.created_at), "finished_at": iso(workflow.finished_at),
            "nodes": [{**task_dict(nodes[key]), "depends_on": sorted(needs[key])} for key in order],
        }

    def get_task(self, task_id: str) -> dict | None:
        with self.Session() as session:
            task = session.get(Task, task_id)
            return task_dict(task) if task else None

    def events(self, limit: int = 30) -> list[dict]:
        with self.Session() as session:
            rows = session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
            return [{"id": e.id, "task_id": e.task_id, "kind": e.kind, "message": e.message, "created_at": e.created_at.isoformat()} for e in rows]

    def _elapsed_ms_sql(self, later, earlier):
        if self.engine.dialect.name == "postgresql":
            return func.extract("epoch", later - earlier) * 1000
        return (func.julianday(later) - func.julianday(earlier)) * 86_400_000

    def metrics(self) -> dict:
        """Aggregate in the database so the cost stays flat as the table grows."""
        with self.Session() as session:
            by_status = dict(session.execute(select(Task.status, func.count()).group_by(Task.status)).all())
            by_tier = dict(session.execute(select(Task.route_tier, func.count()).group_by(Task.route_tier)).all())
            spent, recoveries = session.execute(select(
                func.coalesce(func.sum(Task.estimated_cost), 0.0),
                func.coalesce(func.sum(Task.recovery_count), 0),
            )).one()
            # Count scheduled retries, not extra attempts: attempts consumed by lease
            # recovery are already reported as recoveries.
            retries = session.scalar(select(func.count()).where(Event.kind == "retry"))
            # Same definition as task_dict: creation to finish of a task that started.
            avg_latency = session.scalar(select(func.avg(self._elapsed_ms_sql(Task.finished_at, Task.created_at))).where(
                Task.started_at.is_not(None), Task.finished_at.is_not(None),
            ))
        total = sum(by_status.values())
        succeeded, failed = by_status.get("succeeded", 0), by_status.get("failed", 0)
        terminal = succeeded + failed
        all_large_cost = total * TIER_COST["large"]
        return {"total": total, "queued": by_status.get("queued", 0),
                "running": by_status.get("running", 0), "succeeded": succeeded, "failed": failed,
                "success_rate": round((succeeded / terminal * 100) if terminal else 100, 1),
                "small": by_tier.get("small", 0), "large": by_tier.get("large", 0),
                "retries": int(retries), "estimated_cost": round(float(spent), 3),
                "recoveries": int(recoveries),
                "all_large_cost": round(all_large_cost, 3),
                "cost_saved_pct": round((1 - float(spent) / all_large_cost) * 100, 1) if total else 0,
                "avg_latency_ms": max(0, round(float(avg_latency))) if avg_latency is not None else 0}

    def reset(self):
        with self.lock, self.Session.begin() as session:
            session.query(Event).delete()
            session.query(TaskDependency).delete()
            session.query(Task).delete()
            session.query(Workflow).delete()
