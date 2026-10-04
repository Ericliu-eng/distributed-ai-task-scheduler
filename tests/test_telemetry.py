from datetime import datetime, timedelta, timezone

import pytest

from app.store import Event, SchedulerStore, Task
from app.telemetry import timeseries


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


@pytest.fixture
def store():
    instance = SchedulerStore("sqlite:///:memory:")
    instance.init()
    yield instance
    instance.engine.dispose()


def test_empty_window_has_zero_traffic_but_no_latency(store):
    report = timeseries(store, 5, NOW)
    assert len(report["points"]) == 30
    assert report["interval_seconds"] == 10
    assert report["summary"]["latency_p95_ms"] is None
    assert all(point["throughput_per_minute"] == 0 for point in report["points"])
    assert all(point["latency_p50_ms"] is None for point in report["points"])


def test_queue_reconstruction_carries_baseline_and_counts_retry_recovery_peaks(store):
    start = NOW - timedelta(minutes=5)
    transitions = [
        ("a", "routed", -50), ("a", "claimed", -40),
        ("b", "routed", -30), ("b", "claimed", -20), ("b", "succeeded", -10),
        ("a", "retry", 11), ("a", "claimed", 12),
        ("a", "recovered", 21), ("a", "claimed", 31),
        ("a", "failed", 41), ("future", "routed", 301),
    ]
    with store.Session.begin() as session:
        for task_id, kind, seconds in transitions:
            session.add(Event(task_id=task_id, kind=kind, message=kind,
                              created_at=start + timedelta(seconds=seconds)))
    report = timeseries(store, 5, NOW)
    points = report["points"]
    assert (points[0]["queued"], points[0]["running"]) == (0, 1)
    assert (points[1]["queued"], points[1]["running"]) == (1, 1)
    assert (points[2]["queued"], points[2]["running"]) == (1, 1)
    assert (points[3]["queued"], points[3]["running"]) == (1, 1)
    assert (points[5]["queued"], points[5]["running"]) == (0, 0)
    assert points[-1]["queued"] == 0
    assert report["summary"]["peak_queued"] == 1


def test_completion_rate_and_latency_use_finished_time_and_full_task_lifetime(store):
    start = NOW - timedelta(minutes=5)
    with store.Session.begin() as session:
        for index, (created, finished, status) in enumerate([
            (start - timedelta(seconds=10), start + timedelta(seconds=10), "succeeded"),
            (start, start + timedelta(seconds=10), "succeeded"),
            (start, start + timedelta(seconds=10), "failed"),
            (start - timedelta(seconds=20), start - timedelta(seconds=1), "succeeded"),
            (NOW - timedelta(seconds=1), NOW, "succeeded"),
        ]):
            session.add(Task(id=str(index), prompt="test", sla="standard", priority=5,
                             route_tier="small", route_reason="test", difficulty_score=0,
                             estimated_cost=0.003, status=status,
                             created_at=created, finished_at=finished))
    report = timeseries(store, 5, NOW)
    assert report["summary"]["completed"] == 3
    assert report["summary"]["throughput_per_minute"] == 0.6
    assert report["points"][1]["throughput_per_minute"] == 12
    assert report["points"][1]["latency_p50_ms"] == 15000
    assert report["points"][1]["latency_p95_ms"] == 19500
    assert report["points"][0]["latency_p95_ms"] is None
    assert report["points"][-1]["completed"] == 1


@pytest.mark.parametrize("minutes", [0, 61])
def test_window_is_bounded(store, minutes):
    with pytest.raises(ValueError):
        timeseries(store, minutes, NOW)
