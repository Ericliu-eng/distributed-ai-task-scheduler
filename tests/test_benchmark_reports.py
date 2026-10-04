import json

from fastapi import HTTPException
import pytest

from app import benchmarks
from app.api import app, benchmark_report
from app.benchmarks import load_result


def test_api_route_serves_saved_reports():
    assert any(getattr(route, "path", None) == "/benchmarks/{kind}" for route in app.routes)
    assert benchmark_report("routing") == load_result("routing")


def test_saved_reports_are_served_by_kind():
    performance = load_result("performance")
    routing = load_result("routing")
    recovery = load_result("recovery")

    assert performance["tasks_succeeded"] == performance["tasks_submitted"]
    assert set(performance["queue_latency_ms"]) == {"p50", "p95", "p99"}
    assert routing["dataset_size"] == 50
    assert routing["threshold_runs"]
    assert routing["limitations"]
    assert recovery["tasks_succeeded"] == recovery["trials"]
    assert recovery["stale_writes_rejected"] == recovery["trials"]
    assert "crash_to_completion_ms" in recovery["timings_ms"]


@pytest.mark.parametrize("kind", ["unknown", "../app/store", "postgres-1000-tasks.json"])
def test_only_known_report_names_are_accepted(kind):
    with pytest.raises(HTTPException) as error:
        load_result(kind)
    assert error.value.status_code == 404


def test_missing_report_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmarks, "RESULTS_DIR", tmp_path)
    with pytest.raises(HTTPException) as error:
        load_result("performance")
    assert error.value.status_code == 404


@pytest.mark.parametrize("content", ["{not json", json.dumps([1, 2, 3])])
def test_unreadable_report_returns_service_unavailable(tmp_path, monkeypatch, content):
    monkeypatch.setattr(benchmarks, "RESULTS_DIR", tmp_path)
    (tmp_path / benchmarks.RESULT_FILES["routing"]).write_text(content, encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        load_result("routing")
    assert error.value.status_code == 503
