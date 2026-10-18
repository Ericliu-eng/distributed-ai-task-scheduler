from bench.run import percentile, run_benchmark
from bench.scaling import run_matrix


def test_percentile_interpolates_and_handles_edge_cases():
    assert percentile([], 99) == 0
    assert percentile([7], 50) == 7
    assert percentile([0, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5], 95) == 4.8


def test_benchmark_completes_workload_and_reports_percentiles(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'benchmark.db').as_posix()}"
    result = run_benchmark(database_url, task_count=24, worker_count=4)

    assert result["tasks_submitted"] == 24
    assert result["tasks_succeeded"] == 24
    assert result["workers"] == 4
    assert sum(result["worker_completions"]) == 24
    assert result["duplicate_claims"] == 0
    assert result["tasks_with_single_attempt"] == 24
    assert result["throughput_tasks_per_second"] > 0
    assert result["queue_latency_ms"]["p50"] >= 0
    assert result["queue_latency_ms"]["p95"] >= result["queue_latency_ms"]["p50"]
    assert result["queue_latency_ms"]["p99"] >= result["queue_latency_ms"]["p95"]
    assert result["routing"] == {"small": 12, "large": 12}


def test_single_worker_drains_every_task_on_one_tier(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'single.db').as_posix()}"
    result = run_benchmark(database_url, task_count=10, worker_count=1, work_ms=5)

    assert result["tasks_succeeded"] == 10
    assert result["worker_completions"] == [10]
    assert result["routing"] == {"small": 10, "large": 0}
    assert result["work_ms"] == 5
    # Ten tasks of 5 ms each cannot finish faster than 50 ms.
    assert result["duration_seconds"] >= 0.05


def test_process_workers_complete_without_duplicates(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'processes.db').as_posix()}"
    result = run_benchmark(database_url, task_count=12, worker_count=2, worker_mode="processes")

    assert result["tasks_succeeded"] == 12
    assert result["duplicate_claims"] == 0
    assert sum(result["worker_completions"]) == 12
    assert result["worker_mode"].startswith("one operating-system process")


def test_scaling_matrix_reports_median_and_efficiency(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'matrix.db').as_posix()}"
    result = run_matrix(database_url, worker_counts=[1, 2], work_ms_values=[0.0],
                        repeats=2, task_count=8, worker_mode="threads")

    cells = result["cells"]
    assert [cell["workers"] for cell in cells] == [1, 2]
    assert all(len(cell["throughput_runs"]) == 2 for cell in cells)
    assert all(cell["duplicate_claims"] == 0 for cell in cells)
    assert cells[0]["scaling_efficiency_pct"] == 100.0
