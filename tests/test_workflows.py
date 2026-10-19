import os

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app import api
from app.dag import DagError
from app.store import Event, SchedulerStore, Task, TaskDependency, Workflow

POSTGRES_TEST_URL = os.getenv("TEST_DATABASE_URL")

PIPELINE = [
    {"key": "parse", "prompt": "parse the uploaded document"},
    {"key": "chunk", "prompt": "split the text into chunks", "depends_on": ["parse"]},
    {"key": "metadata", "prompt": "extract title and authors", "depends_on": ["parse"]},
    {"key": "embed", "prompt": "embed every chunk", "depends_on": ["chunk", "metadata"]},
    {"key": "index", "prompt": "build the vector index", "depends_on": ["embed"]},
]


@pytest.fixture(params=["sqlite"] + (["postgresql"] if POSTGRES_TEST_URL else []))
def store(request):
    url = "sqlite:///:memory:" if request.param == "sqlite" else POSTGRES_TEST_URL
    store = SchedulerStore(url)
    store.init()
    store.reset()
    yield store
    store.engine.dispose()


def counts(store):
    with store.Session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (Workflow, Task, TaskDependency, Event))


def test_workflow_writes_nodes_and_edges_with_only_roots_queued(store):
    workflow = store.create_workflow("document-ai-pipeline", PIPELINE)

    nodes = {node["node_key"]: node for node in workflow["nodes"]}
    assert workflow["name"] == "document-ai-pipeline" and workflow["status"] == "running"
    assert [node["node_key"] for node in workflow["nodes"]][0] == "parse"
    assert {key: node["status"] for key, node in nodes.items()} == {
        "parse": "queued", "chunk": "blocked", "metadata": "blocked",
        "embed": "blocked", "index": "blocked",
    }
    assert nodes["embed"]["depends_on"] == ["chunk", "metadata"]
    assert all(node["workflow_id"] == workflow["id"] for node in nodes.values())
    assert all(node["route_tier"] in {"small", "large"} for node in nodes.values())
    # One workflow, five tasks, five edges, one routing event per node.
    assert counts(store) == (1, 5, 5, 5)
    assert store.get_workflow(workflow["id"]) == workflow


@pytest.mark.parametrize("nodes,problem", [
    ([{"key": "a", "prompt": "first", "depends_on": ["b"]},
      {"key": "b", "prompt": "second", "depends_on": ["a"]}], "cycle"),
    ([{"key": "a", "prompt": "first"}, {"key": "a", "prompt": "again"}], "duplicate_key"),
    ([{"key": "a", "prompt": "first", "depends_on": ["ghost"]}], "unknown_dependency"),
])
def test_invalid_workflow_writes_nothing(store, nodes, problem):
    with pytest.raises(DagError) as error:
        store.create_workflow("broken", nodes)
    assert error.value.problem == problem
    assert counts(store) == (0, 0, 0, 0)


def test_blocked_nodes_are_never_claimed(store):
    workflow = store.create_workflow("document-ai-pipeline", PIPELINE)
    root = next(node for node in workflow["nodes"] if node["node_key"] == "parse")

    claimed = [task for tier in ("small", "large") for task in [store.claim("w", tier)] if task]
    assert [task["id"] for task in claimed] == [root["id"]]
    # Four blocked tasks remain, and no tier will hand any of them out.
    assert store.claim("w", "small") is None and store.claim("w", "large") is None
    statuses = [node["status"] for node in store.get_workflow(workflow["id"])["nodes"]]
    assert statuses.count("blocked") == 4


def test_standalone_tasks_are_unaffected(store):
    store.create_workflow("document-ai-pipeline", PIPELINE)
    task, created = store.create_task("a standalone request")
    assert created and task["workflow_id"] is None and task["node_key"] is None
    assert store.get_task(task["id"])["status"] == "queued"


def test_api_creates_reads_and_rejects_workflows(monkeypatch):
    monkeypatch.setattr(api, "store", SchedulerStore("sqlite:///:memory:"))
    api.store.init()
    created = api.create_workflow(api.WorkflowCreate(name="pipeline", nodes=PIPELINE))
    assert api.get_workflow(created["id"]) == created

    with pytest.raises(HTTPException) as error:
        api.create_workflow(api.WorkflowCreate(name="loop", nodes=[
            {"key": "a", "prompt": "first", "depends_on": ["b"]},
            {"key": "b", "prompt": "second", "depends_on": ["a"]},
        ]))
    assert error.value.status_code == 422
    assert error.value.detail["problem"] == "cycle"
    assert error.value.detail["nodes"] == ["a", "b", "a"]

    with pytest.raises(HTTPException) as error:
        api.get_workflow("missing")
    assert error.value.status_code == 404
