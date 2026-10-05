from bench.run import percentile, run_benchmark


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
