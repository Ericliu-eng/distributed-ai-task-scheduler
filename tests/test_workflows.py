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


DIAMOND = [
    # Distinct priorities fix the claim order of the two roots.
    {"key": "left", "prompt": "left branch", "priority": 9},
    {"key": "right", "prompt": "right branch", "priority": 8},
    {"key": "join", "prompt": "join both branches", "depends_on": ["left", "right"]},
]


def node(store, workflow_id, key):
    return next(n for n in store.get_workflow(workflow_id)["nodes"] if n["node_key"] == key)


def events_of(store, task_id, kind):
    return [event for event in store.events(limit=500) if event["task_id"] == task_id and event["kind"] == kind]


def claim_node(store, workflow_id, key, worker_id):
    tier = node(store, workflow_id, key)["route_tier"]
    claimed = store.claim(worker_id, tier)
    assert claimed["node_key"] == key
    return claimed


def test_dependent_unlocks_only_after_its_last_dependency(store):
    workflow = store.create_workflow("diamond", DIAMOND)
    left = claim_node(store, workflow["id"], "left", "w1")
    right = claim_node(store, workflow["id"], "right", "w2")

    assert store.finish(left["id"], "w1", left["attempt"], "ok")
    join = node(store, workflow["id"], "join")
    assert (join["status"], join["pending_dependencies"]) == ("blocked", 1)

    assert store.finish(right["id"], "w2", right["attempt"], "ok")
    join = node(store, workflow["id"], "join")
    assert (join["status"], join["pending_dependencies"]) == ("queued", 0)
    assert len(events_of(store, join["id"], "unblocked")) == 1


def test_rejected_write_back_does_not_count_as_success(store):
    workflow = store.create_workflow("diamond", DIAMOND)
    left = claim_node(store, workflow["id"], "left", "w1")
    assert store.finish(left["id"], "w1", left["attempt"], "ok")
    # A duplicate report and a stale attempt are both fenced: no second decrement.
    assert not store.finish(left["id"], "w1", left["attempt"], "duplicate")
    assert not store.finish(left["id"], "w1", left["attempt"] - 1, "stale")
    assert node(store, workflow["id"], "join")["pending_dependencies"] == 1


def test_workers_run_the_pipeline_in_dependency_order(store):
    from worker.main import WorkerService

    workflow = store.create_workflow("document-ai-pipeline", PIPELINE)
    workers = [WorkerService(store, f"w-{tier}", tier, renew_interval=60) for tier in ("small", "large")]
    while any([worker.run_once(delay=False) for worker in workers]):
        pass
    for worker in workers:
        worker.stop()

    nodes = {n["node_key"]: n for n in store.get_workflow(workflow["id"])["nodes"]}
    assert all(n["status"] == "succeeded" and n["attempt"] == 1 for n in nodes.values())
    for n in nodes.values():
        for upstream in n["depends_on"]:
            assert n["started_at"] >= nodes[upstream]["finished_at"]
    assert sum(len(events_of(store, n["id"], "unblocked")) for n in nodes.values()) == 4


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="set TEST_DATABASE_URL to force the unlock race")
def test_concurrent_last_dependencies_unlock_exactly_once(monkeypatch):
    """Both upstream write-backs take their snapshot before either commits.

    A third connection locks both upstream rows, so each write-back starts and
    waits on its own upstream. Under READ COMMITTED each statement's snapshot is
    taken before that wait, while both upstreams are still running. Checking
    "are all my dependencies done?" from those snapshots finds the other
    upstream unfinished in both, and the join never runs. The countdown must
    reach zero exactly once.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text

    # A bug here must fail the test, not hang it. libpq applies PGOPTIONS at
    # connect time, without opening a transaction.
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=10s")
    stores = [SchedulerStore(POSTGRES_TEST_URL) for _ in range(3)]
    setup, left_store, right_store = stores
    setup.init()
    setup.reset()
    waiting_write_backs = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query LIKE 'WITH done AS%'"
    )
    try:
        for _ in range(5):
            workflow = setup.create_workflow("diamond", DIAMOND)
            left = claim_node(setup, workflow["id"], "left", "w1")
            right = claim_node(setup, workflow["id"], "right", "w2")
            join_id = node(setup, workflow["id"], "join")["id"]

            with ThreadPoolExecutor(max_workers=2) as pool:
                with setup.engine.connect() as blocker, setup._autocommit() as probe:
                    try:
                        blocker.execute(text("SELECT id FROM tasks WHERE id IN (:left, :right) FOR UPDATE"),
                                        {"left": left["id"], "right": right["id"]})
                        results = [
                            pool.submit(left_store.finish, left["id"], "w1", left["attempt"], "ok"),
                            pool.submit(right_store.finish, right["id"], "w2", right["attempt"], "ok"),
                        ]
                        deadline = time.monotonic() + 5
                        while probe.execute(waiting_write_backs).scalar_one() < 2:
                            assert time.monotonic() < deadline, "write-backs never queued on the lock"
                            time.sleep(0.02)
                    finally:
                        blocker.rollback()  # release both upstreams; the write-backs proceed
                assert [future.result(timeout=15) for future in results] == [True, True]

            join = node(setup, workflow["id"], "join")
            assert (join["status"], join["pending_dependencies"]) == ("queued", 0)
            assert len(events_of(setup, join_id, "unblocked")) == 1
    finally:
        for s in stores:
            s.engine.dispose()


def test_tasks_inserted_without_new_columns_still_work(store):
    """Code from before workflows (as in a rolling upgrade) inserts without them."""
    from sqlalchemy import insert

    with store.Session.begin() as session:
        session.execute(insert(Task.__table__).values(
            id="legacy-task", prompt="inserted by older code", sla="standard", priority=5,
            route_tier="small", route_reason="legacy", difficulty_score=0.1,
            estimated_cost=0.003, status="queued", attempt=0, max_attempts=3,
            fail_once=False, recovery_count=0,
        ))
    task = store.get_task("legacy-task")
    assert (task["pending_dependencies"], task["workflow_id"]) == (0, None)
    assert store.claim("w", "small")["id"] == "legacy-task"
