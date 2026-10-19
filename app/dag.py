"""Validate workflow graphs before anything is written to the database."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
import re

MAX_NODES = 100
NODE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DagError(ValueError):
    """A workflow graph that cannot run; ``problem`` names the rule it breaks."""

    def __init__(self, problem: str, message: str, nodes: Sequence[str] = ()):
        super().__init__(message)
        self.problem = problem
        self.nodes = list(nodes)


def topological_order(dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    """Return node keys so every node comes after all of its dependencies.

    ``dependencies`` maps each node key to the keys it depends on, in
    submission order. Uses Kahn's algorithm, O(V + E): repeatedly take a node
    with no unfinished dependencies. Ties keep submission order, so the result
    is deterministic. Raises DagError for an empty or oversized graph, a bad
    key, a duplicate or unknown dependency, a self-dependency, or a cycle.
    """
    if not dependencies:
        raise DagError("empty", "a workflow needs at least one node")
    if len(dependencies) > MAX_NODES:
        raise DagError("too_large", f"a workflow can have at most {MAX_NODES} nodes")
    for key, needs in dependencies.items():
        if not NODE_KEY.match(key):
            raise DagError("bad_key", f"node key {key!r} must be 1-64 letters, digits, '_' or '-'", [key])
        if key in needs:
            raise DagError("self_dependency", f"node {key!r} depends on itself", [key])
        if len(set(needs)) != len(needs):
            raise DagError("duplicate_dependency", f"node {key!r} lists a dependency twice", [key])
        unknown = [need for need in needs if need not in dependencies]
        if unknown:
            raise DagError("unknown_dependency",
                           f"node {key!r} depends on unknown node {unknown[0]!r}", [key, unknown[0]])

    remaining = {key: len(needs) for key, needs in dependencies.items()}
    dependents: dict[str, list[str]] = {key: [] for key in dependencies}
    for key, needs in dependencies.items():
        for need in needs:
            dependents[need].append(key)

    ready = deque(key for key, count in remaining.items() if count == 0)
    order: list[str] = []
    while ready:
        key = ready.popleft()
        order.append(key)
        for dependent in dependents[key]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)

    if len(order) < len(dependencies):
        cycle = _find_cycle(dependencies, {key for key, count in remaining.items() if count})
        raise DagError("cycle", "dependency cycle: " + " -> ".join(cycle), cycle)
    return order


def _find_cycle(dependencies: Mapping[str, Sequence[str]], stuck: set[str]) -> list[str]:
    """Return one cycle among the nodes Kahn's algorithm could not order.

    A stuck node may only sit downstream of a cycle, so the stuck set alone
    would over-report. Every stuck node has a stuck dependency, so following
    those pointers must revisit a node; the revisited stretch is a cycle.
    """
    node = next(key for key in dependencies if key in stuck)
    path: list[str] = []
    seen: dict[str, int] = {}
    while node not in seen:
        seen[node] = len(path)
        path.append(node)
        node = next(need for need in dependencies[node] if need in stuck)
    cycle = path[seen[node]:]
    # Report in execution direction (dependency first), starting from the node
    # submitted first so the message is deterministic, and close the loop.
    cycle.reverse()
    submitted = list(dependencies)
    start = min(range(len(cycle)), key=lambda index: submitted.index(cycle[index]))
    cycle = cycle[start:] + cycle[:start]
    return cycle + [cycle[0]]
