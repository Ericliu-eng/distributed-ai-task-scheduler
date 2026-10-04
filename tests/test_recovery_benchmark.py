import pytest

from bench.recovery import run_recovery_benchmark


def test_recovery_benchmark_measures_crash_to_completion(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recovery-benchmark.db').as_posix()}"
    report = run_recovery_benchmark(
        url, task_count=3, lease_seconds=0.04, recovery_poll_interval=0.005
    )

    assert report["trials"] == 3
    assert report["tasks_succeeded"] == 3
    assert report["stale_writes_rejected"] == 3
    assert report["database"] == "sqlite"
    assert report["lease_seconds"] == 0.04
    expected = {
        "crash_to_detection_ms", "lease_expiry_to_detection_ms",
        "detection_to_reclaim_ms", "reclaim_to_completion_ms",
        "crash_to_completion_ms",
    }
    assert set(report["timings_ms"]) == expected
    for timing in report["timings_ms"].values():
        assert set(timing) == {"p50", "p95", "p99"}
        assert 0 <= timing["p50"] <= timing["p95"] <= timing["p99"]


@pytest.mark.parametrize(
    "tasks,lease,poll", [(0, 0.1, 0.01), (1, 0, 0.01), (1, 0.1, 0)],
)
def test_recovery_benchmark_validates_inputs(tmp_path, tasks, lease, poll):
    url = f"sqlite:///{(tmp_path / 'invalid.db').as_posix()}"
    with pytest.raises(ValueError):
        run_recovery_benchmark(url, tasks, lease, poll)
