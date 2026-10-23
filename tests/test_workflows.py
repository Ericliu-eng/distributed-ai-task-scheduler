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


FORK = [
    # Priorities fix the claim order of the roots.
    {"key": "doomed", "prompt": "branch that will fail", "priority": 9},
    {"key": "healthy", "prompt": "independent branch", "priority": 8},
    {"key": "after_doomed", "prompt": "needs the failing branch", "depends_on": ["doomed"]},
    {"key": "after_healthy", "prompt": "needs the healthy branch", "depends_on": ["healthy"]},
]


def statuses(store, workflow_id):
    workflow = store.get_workflow(workflow_id)
    return workflow["status"], {n["node_key"]: n["status"] for n in workflow["nodes"]}


def final_attempt(store, task_id):
    with store.Session.begin() as session:
        session.get(Task, task_id).max_attempts = 1


def test_workflow_succeeds_once_when_its_last_node_succeeds(store):
    from worker.main import WorkerService

    workflow = store.create_workflow("document-ai-pipeline", PIPELINE)
    assert (workflow["status"], workflow["pending_tasks"]) == ("running", 5)
    workers = [WorkerService(store, f"w-{tier}", tier, renew_interval=60) for tier in ("small", "large")]
    while any([worker.run_once(delay=False) for worker in workers]):
        pass

    done = store.get_workflow(workflow["id"])
    assert (done["status"], done["pending_tasks"]) == ("succeeded", 0)
    assert done["finished_at"] is not None
    finished = [e for e in store.events(limit=500) if e["kind"] == "workflow_succeeded"]
    assert len(finished) == 1


def test_permanent_failure_skips_everything_not_yet_running(store):
    workflow = store.create_workflow("fork", FORK)
    doomed = claim_node(store, workflow["id"], "doomed", "w1")
    final_attempt(store, doomed["id"])

    assert store.fail_or_retry(doomed["id"], "w1", doomed["attempt"], "HTTP 400") == "failed"
    assert statuses(store, workflow["id"]) == ("failed", {
        "doomed": "failed", "healthy": "skipped",
        "after_doomed": "skipped", "after_healthy": "skipped",
    })
    skipped = node(store, workflow["id"], "after_healthy")
    assert skipped["error"] == "Skipped: workflow failed because doomed failed"
    assert store.get_workflow(workflow["id"])["finished_at"] is not None
    assert store.claim("w2", "small") is None and store.claim("w2", "large") is None


def test_retryable_failure_does_not_fail_the_workflow(store):
    workflow = store.create_workflow("fork", FORK)
    doomed = claim_node(store, workflow["id"], "doomed", "w1")
    assert store.fail_or_retry(doomed["id"], "w1", doomed["attempt"], "HTTP 503") == "queued"
    assert statuses(store, workflow["id"])[0] == "running"


def test_running_node_finishes_but_unlocks_nothing_after_failure(store):
    workflow = store.create_workflow("fork", FORK)
    doomed = claim_node(store, workflow["id"], "doomed", "w1")
    healthy = claim_node(store, workflow["id"], "healthy", "w2")
    final_attempt(store, doomed["id"])
    assert store.fail_or_retry(doomed["id"], "w1", doomed["attempt"], "HTTP 400") == "failed"

    # The healthy node was already running; it may finish, but the workflow stays failed.
    assert store.finish(healthy["id"], "w2", healthy["attempt"], "ok")
    assert statuses(store, workflow["id"]) == ("failed", {
        "doomed": "failed", "healthy": "succeeded",
        "after_doomed": "skipped", "after_healthy": "skipped",
    })


def test_crash_on_the_final_attempt_fails_the_workflow(store):
    from datetime import datetime, timedelta, timezone

    workflow = store.create_workflow("fork", FORK)
    doomed = claim_node(store, workflow["id"], "doomed", "w1")
    final_attempt(store, doomed["id"])
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert store.requeue_stale_tasks(now=later) == [doomed["id"]]
    assert statuses(store, workflow["id"]) == ("failed", {
        "doomed": "failed", "healthy": "skipped",
        "after_doomed": "skipped", "after_healthy": "skipped",
    })


TWO_LEAVES = [
    {"key": "left", "prompt": "left leaf", "priority": 9},
    {"key": "right", "prompt": "right leaf", "priority": 8},
]


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="set TEST_DATABASE_URL to force the completion race")
def test_concurrent_last_successes_complete_the_workflow_exactly_once(monkeypatch):
    """Same lost wake-up as unlocking, one level up.

    Both leaf write-backs snapshot the workflow while the other leaf is still
    running; "are all my nodes done?" would leave the workflow running forever.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text

    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=10s")
    stores = [SchedulerStore(POSTGRES_TEST_URL) for _ in range(3)]
    setup, left_store, right_store = stores
    setup.init()
    setup.reset()
    waiting = text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                   "AND wait_event_type = 'Lock' AND query LIKE 'WITH done AS%'")
    try:
        for _ in range(5):
            workflow = setup.create_workflow("leaves", TWO_LEAVES)
            left = claim_node(setup, workflow["id"], "left", "w1")
            right = claim_node(setup, workflow["id"], "right", "w2")
            with ThreadPoolExecutor(max_workers=2) as pool:
                with setup.engine.connect() as blocker, setup._autocommit() as probe:
                    try:
                        blocker.execute(text("SELECT id FROM tasks WHERE id IN (:a, :b) FOR UPDATE"),
                                        {"a": left["id"], "b": right["id"]})
                        results = [
                            pool.submit(left_store.finish, left["id"], "w1", left["attempt"], "ok"),
                            pool.submit(right_store.finish, right["id"], "w2", right["attempt"], "ok"),
                        ]
                        deadline = time.monotonic() + 5
                        while probe.execute(waiting).scalar_one() < 2:
                            assert time.monotonic() < deadline, "write-backs never queued on the lock"
                            time.sleep(0.02)
                    finally:
                        blocker.rollback()
                assert [future.result(timeout=15) for future in results] == [True, True]
            done = setup.get_workflow(workflow["id"])
            assert (done["status"], done["pending_tasks"]) == ("succeeded", 0)
            completions = [e for e in setup.events(limit=500)
                           if e["kind"] == "workflow_succeeded" and e["task_id"] in (left["id"], right["id"])]
            assert len(completions) == 1
    finally:
        for s in stores:
            s.engine.dispose()


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="set TEST_DATABASE_URL to stress fail-fast against unlocking")
def test_concurrent_successes_and_failures_never_deadlock_or_strand_nodes(monkeypatch):
    """Many nodes succeed while one fails, all at once, in a wide two-level DAG.

    Unlocks and fail-fast skips lock overlapping task rows and the same
    workflow row. Every path locks task rows in id order before the workflow
    row, so nothing may deadlock, and a failed workflow may leave no node
    blocked or queued.
    """
    import random
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=10s")
    stores = [SchedulerStore(POSTGRES_TEST_URL) for _ in range(9)]
    setup, racers = stores[0], stores[1:]
    setup.init()
    setup.reset()
    rng = random.Random(20261021)
    try:
        for round_number in range(6):
            roots = [{"key": f"r{i}", "prompt": f"root task {i}", "priority": 5} for i in range(8)]
            joins = [{"key": f"j{i}", "prompt": f"join task {i}",
                      "depends_on": rng.sample([r["key"] for r in roots], 3)} for i in range(12)]
            workflow = setup.create_workflow(f"stress-{round_number}", roots + joins)
            claimed = []
            while len(claimed) < len(roots):
                for tier in ("small", "large"):
                    task = setup.claim(f"w{len(claimed)}", tier)
                    if task:
                        claimed.append(task)
            loser = rng.choice(claimed)
            final_attempt(setup, loser["id"])
            start = threading.Barrier(len(claimed))

            def report(index, task):
                start.wait(timeout=10)
                store = racers[index]
                if task["id"] == loser["id"]:
                    return store.fail_or_retry(task["id"], task["worker_id"], task["attempt"], "HTTP 400")
                return store.finish(task["id"], task["worker_id"], task["attempt"], "ok")

            with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
                outcomes = list(pool.map(report, range(len(claimed)), claimed))
            assert outcomes.count("failed") == 1 and outcomes.count(True) == len(claimed) - 1

            status, nodes = statuses(setup, workflow["id"])
            assert status == "failed"
            assert not {key for key, state in nodes.items() if state in ("blocked", "queued")}
    finally:
        for s in stores:
            s.engine.dispose()



def drain(store):
    """Run small- and large-tier workers until neither finds work, making retries eligible."""
    from datetime import datetime, timezone
    from worker.main import WorkerService

    workers = [WorkerService(store, f"w-{tier}", tier, renew_interval=60) for tier in ("small", "large")]
    for _ in range(50):
        if not any([worker.run_once(delay=False) for worker in workers]):
            with store.Session.begin() as session:
                waiting = session.scalars(select(Task).where(Task.next_attempt_at.is_not(None),
                                                             Task.status == "queued")).all()
                for task in waiting:  # skip the real backoff wait
                    task.next_attempt_at = datetime.now(timezone.utc)
            if not waiting:
                return


@pytest.fixture
def demo_api(monkeypatch):
    monkeypatch.setattr(api, "store", SchedulerStore("sqlite:///:memory:"))
    monkeypatch.setattr(api, "DEMO_MODE", True)
    api.store.init()
    return api


def test_list_workflows_shows_newest_first_with_node_states(demo_api):
    import time

    first = demo_api.store.create_workflow("first", PIPELINE)
    # Order comes from created_at; equal timestamps made an earlier test flaky.
    time.sleep(0.05)
    second = demo_api.store.create_workflow("second", DIAMOND)
    listed = demo_api.list_workflows(limit=20)
    assert [w["id"] for w in listed] == [second["id"], first["id"]]
    assert listed[0]["nodes"] == 3 and listed[0]["node_states"] == {"queued": 2, "blocked": 1}
    assert listed[1]["node_states"] == {"queued": 1, "blocked": 4}


def test_retry_demo_recovers_and_completes(demo_api):
    workflow = demo_api.demo_workflow(scenario="retry")
    drain(demo_api.store)
    done = demo_api.get_workflow(workflow["id"])
    nodes = {n["node_key"]: n for n in done["nodes"]}
    assert done["status"] == "succeeded"
    assert nodes["metadata"]["attempt"] == 2 and all(n["status"] == "succeeded" for n in nodes.values())


def test_failure_demo_fails_fast(demo_api):
    workflow = demo_api.demo_workflow(scenario="failure")
    drain(demo_api.store)
    done = demo_api.get_workflow(workflow["id"])
    states = {n["node_key"]: n["status"] for n in done["nodes"]}
    assert done["status"] == "failed"
    assert states["parse"] == "succeeded" and states["metadata"] == "failed"
    assert states["embed"] == states["index"] == "skipped"
    # chunk ran in parallel with metadata and may finish or be skipped, never stranded.
    assert states["chunk"] in ("succeeded", "skipped")


def test_demo_workflows_and_simulated_failures_need_demo_mode(demo_api, monkeypatch):
    monkeypatch.setattr(api, "DEMO_MODE", False)
    with pytest.raises(HTTPException) as error:
        demo_api.demo_workflow(scenario="retry")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        demo_api.create_workflow(demo_api.WorkflowCreate(name="x", nodes=[
            {"key": "a", "prompt": "fails on purpose", "fail_once": True}]))
    assert error.value.status_code == 403
