import random

import pytest

from app.dag import MAX_NODES, DagError, topological_order


def assert_valid_order(dependencies, order):
    assert sorted(order) == sorted(dependencies)
    position = {key: index for index, key in enumerate(order)}
    for key, needs in dependencies.items():
        assert all(position[need] < position[key] for need in needs)


def test_document_pipeline_orders_dependencies_first():
    dependencies = {
        "parse": [],
        "chunk": ["parse"],
        "metadata": ["parse"],
        "embed": ["chunk", "metadata"],
        "index": ["embed"],
    }
    order = topological_order(dependencies)
    assert order == ["parse", "chunk", "metadata", "embed", "index"]
    assert_valid_order(dependencies, order)


def test_independent_roots_keep_submission_order():
    assert topological_order({"b": [], "a": [], "c": ["a", "b"]}) == ["b", "a", "c"]


@pytest.mark.parametrize("dependencies,cycle", [
    ({"a": ["b"], "b": ["a"]}, ["a", "b", "a"]),
    ({"a": ["c"], "b": ["a"], "c": ["b"]}, ["a", "b", "c", "a"]),
    # "d" only sits downstream of the cycle and must not be reported as part of it.
    ({"root": [], "a": ["root", "b"], "b": ["a"], "d": ["b"]}, ["a", "b", "a"]),
])
def test_cycle_is_reported_precisely(dependencies, cycle):
    with pytest.raises(DagError) as error:
        topological_order(dependencies)
    assert error.value.problem == "cycle"
    assert error.value.nodes == cycle
    assert " -> ".join(cycle) in str(error.value)


@pytest.mark.parametrize("dependencies,problem", [
    ({}, "empty"),
    ({"a": ["a"]}, "self_dependency"),
    ({"a": [], "b": ["a", "a"]}, "duplicate_dependency"),
    ({"a": ["missing"]}, "unknown_dependency"),
    ({"bad key": []}, "bad_key"),
    ({"x" * 65: []}, "bad_key"),
    ({f"n{index}": [] for index in range(MAX_NODES + 1)}, "too_large"),
])
def test_invalid_graphs_are_rejected(dependencies, problem):
    with pytest.raises(DagError) as error:
        topological_order(dependencies)
    assert error.value.problem == problem


def random_dag(rng, size):
    """Edges only point from earlier to later keys in a shuffled order, so it is acyclic."""
    keys = [f"n{index}" for index in range(size)]
    rng.shuffle(keys)
    return {key: [earlier for earlier in keys[:position] if rng.random() < 0.3]
            for position, key in enumerate(keys)}


def test_random_dags_order_and_random_back_edges_are_caught():
    rng = random.Random(20261019)
    for _ in range(200):
        dependencies = random_dag(rng, rng.randint(1, 40))
        assert_valid_order(dependencies, topological_order(dependencies))

        # Add one edge pointing backwards along a dependency chain to create a cycle.
        downstream = [key for key, needs in dependencies.items() if needs]
        if not downstream:
            continue
        child = rng.choice(downstream)
        parent = rng.choice(dependencies[child])
        dependencies[parent] = [*dependencies[parent], child]
        with pytest.raises(DagError) as error:
            topological_order(dependencies)
        cycle = error.value.nodes
        assert error.value.problem == "cycle" and cycle[0] == cycle[-1]
        # Every reported edge exists: each node depends on the one before it.
        assert all(cycle[i] in dependencies[cycle[i + 1]] for i in range(len(cycle) - 1))
