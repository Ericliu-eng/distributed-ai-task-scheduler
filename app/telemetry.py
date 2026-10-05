"""Read-only dashboard trends reconstructed from durable task events."""

from datetime import datetime, timedelta, timezone
from math import floor

from sqlalchemy import func, select

from app.store import Event, SchedulerStore, Task, as_utc


STATES = {
    "routed": "queued", "claimed": "running", "retry": "queued",
    "recovered": "queued", "succeeded": "succeeded", "failed": "failed",
}


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def timeseries(store: SchedulerStore, minutes: int = 15,
               now: datetime | None = None) -> dict:
    if not 1 <= minutes <= 60:
        raise ValueError("minutes must be between 1 and 60")
    end = as_utc(now or datetime.now(timezone.utc))
    start = end - timedelta(minutes=minutes)
    bucket_count = 30
    interval = minutes * 60 / bucket_count

    with store.Session() as session:
        # The last transition before the window establishes the starting queue.
        # Event IDs follow insertion order within each task's state transitions.
        latest = select(func.max(Event.id)).where(
            Event.created_at < start, Event.task_id.is_not(None),
            Event.kind.in_(STATES),
        ).group_by(Event.task_id)
        baseline = session.execute(select(Event.task_id, Event.kind).where(Event.id.in_(latest))).all()
        transitions = session.execute(select(Event.task_id, Event.kind, Event.created_at).where(
            Event.created_at >= start, Event.created_at <= end,
            Event.task_id.is_not(None), Event.kind.in_(STATES),
        ).order_by(Event.created_at, Event.id)).all()
        completions = session.execute(select(Task.created_at, Task.finished_at).where(
            Task.status == "succeeded", Task.finished_at >= start, Task.finished_at <= end,
        )).all()

    states = {task_id: STATES[kind] for task_id, kind in baseline}
    counts = {state: sum(value == state for value in states.values()) for state in ("queued", "running")}
    buckets = [[] for _ in range(bucket_count)]
    durations: list[list[float]] = [[] for _ in range(bucket_count)]

    def bucket_index(timestamp: datetime) -> int:
        return min(bucket_count - 1, max(0, floor((as_utc(timestamp) - start).total_seconds() / interval)))

    for task_id, kind, timestamp in transitions:
        buckets[bucket_index(timestamp)].append((task_id, kind))
    for created_at, finished_at in completions:
        durations[bucket_index(finished_at)].append(max(0, (as_utc(finished_at) - as_utc(created_at)).total_seconds() * 1000))

    points = []
    for index, changes in enumerate(buckets):
        peaks = counts.copy()
        for task_id, kind in changes:
            previous = states.get(task_id)
            following = STATES[kind]
            if previous in counts:
                counts[previous] -= 1
            if following in counts:
                counts[following] += 1
            states[task_id] = following
            for state in peaks:
                peaks[state] = max(peaks[state], counts[state])
        points.append({
            "at": (start + timedelta(seconds=(index + 1) * interval)).isoformat(),
            "queued": peaks["queued"], "running": peaks["running"],
            "completed": len(durations[index]),
            "throughput_per_minute": round(len(durations[index]) * 60 / interval, 2),
            "latency_p50_ms": percentile(durations[index], 50),
            "latency_p95_ms": percentile(durations[index], 95),
        })
    all_durations = [value for bucket in durations for value in bucket]
    return {
        "start_at": start.isoformat(), "measured_at": end.isoformat(),
        "minutes": minutes, "interval_seconds": interval,
        "summary": {
            "completed": len(all_durations),
            "throughput_per_minute": round(len(all_durations) / minutes, 2),
            "latency_p95_ms": percentile(all_durations, 95),
            "peak_queued": max(point["queued"] for point in points),
        },
        "points": points,
    }
