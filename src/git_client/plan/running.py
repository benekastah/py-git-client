"""The plan, which runs its nodes in rounds."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

import graphlib
import itertools
from collections import deque
from collections.abc import Hashable
from concurrent.futures import ThreadPoolExecutor

from git_client.client import Git
from git_client.plan.nodes import Effect, ExecutionException, Node, PlanError, Resource, Skipped

type Graph = dict[Node[object], tuple[Node[object], ...]]
"""Each node, with its inputs."""


class Plan:
    """Nodes to run with `git`, in as few processes as git's batch forms allow, independent
    groups in parallel on up to `max_workers` more threads (the calling thread runs one).
    `atomic_pushes` is what a Push's `atomic=None` means.

    Every process starts through `git` (its `process`), so its timeout, `stopping` and
    process groups apply as to any of its commands. Ctrl-C reaches only a git the calling
    thread runs (see ReadOnlyGit).
    """

    def __init__(
        self, git: Git, *, atomic_pushes: bool = False, max_workers: int | None = None
    ) -> None:
        self.git = git
        self.atomic_pushes = atomic_pushes
        self.max_workers = max_workers
        self.ops: list[Node[object]] = []

    def add[N: Node[object]](self, op: N) -> N:
        """Add `op` (and so what it reads) to the plan, and give it back."""
        self.ops.append(op)
        return op

    def sequence[N: Effect[object]](self, *ops: N) -> tuple[N, ...]:
        """Add `ops`, each to run after the one before it: never in the same group, nor at
        the same time."""
        for before, op in itertools.pairwise(ops):
            op.after.append(before)
        for op in ops:
            self.add(op)
        return ops

    def graph(self) -> Graph:
        """Every node the ops reach, with its inputs, in the order crawled, ops first: each
        once, and equal ones (see Node) once."""
        return self._crawl()[0]

    def _crawl(self) -> tuple[Graph, list[tuple[Node[object], Node[object]]]]:
        """graph(), and each node left out as equal to one in it, with that one."""
        graph: Graph = {}
        canonical: dict[Node[object], Node[object]] = {}
        copies: dict[int, tuple[Node[object], Node[object]]] = {}
        pending = deque(self.ops)
        while pending:
            node = pending.popleft()
            if node in graph:
                if canonical[node] is not node and id(node) not in copies:
                    copies[id(node)] = (node, canonical[node])
                    pending += node.inputs()  # equal ones too, which a caller may hold
                continue
            canonical[node] = node
            graph[node] = tuple(node.inputs())
            pending += graph[node]
        return graph, list(copies.values())

    def execute(self) -> None:
        """Run every node not done yet, so again those that failed or were skipped, after a
        failure. Raises graphlib.CycleError, running nothing, if nodes read each other;
        ExecutionException once the nodes that could run did, if one failed."""
        graph, copies = self._crawl()
        for node in (*graph, *(copy for copy, _ in copies)):
            if not node._done:
                object.__setattr__(node, "_error", None)
        sorter = graphlib.TopologicalSorter(graph)
        sorter.prepare()
        order = _Order(graph, self)
        groups_of: dict[Node[object], list[Node[object]]] = {}
        held: list[Node[object]] = []
        with ThreadPoolExecutor(self.max_workers) as pool:
            while sorter.is_active():
                now, held = order.choose([*held, *sorter.get_ready()])
                self._run_round(now, order, groups_of, pool)
                for copy, original in copies:
                    if not copy._done:
                        copy._copy_outcome(original)
                sorter.done(*now)
        failures = {
            node: node._error
            for node in graph
            if node._error is not None and not isinstance(node._error, Skipped)
        }
        if failures:
            first = next(iter(failures))
            group = groups_of.get(first, [first])
            raise ExecutionException(first, group, failures) from failures[first]

    def _run_round(
        self,
        ready: list[Node[object]],
        order: _Order,
        groups_of: dict[Node[object], list[Node[object]]],
        pool: ThreadPoolExecutor,
    ) -> None:
        """Run the ready nodes not done yet whose inputs are, in groups (each noted in
        `groups_of`), each lane's groups in turn, the lanes at once."""
        groups: dict[Hashable, list[Node[object]]] = {}
        for node in ready:
            if not node._done and node._error is None:
                key = order.key[node]
                groups_of[node] = groups.setdefault(key, [])
                groups_of[node].append(node)
        lanes: dict[Hashable, list[list[Node[object]]]] = {}
        for group in groups.values():
            lane = order.lane[group[0]]
            lanes.setdefault(object() if lane is None else lane, []).append(group)
        *others, mine = [*lanes.values()] or [[]]
        futures = [pool.submit(self._run_lane, lane) for lane in others]
        self._run_lane(mine)
        for future in futures:
            future.result()

    def _run_lane(self, groups: list[list[Node[object]]]) -> None:
        for group in groups:
            try:
                type(group[0]).execute_group(group, self)
            # Whatever it raises is its members' failure.
            except Exception as error:  # noqa: BLE001
                for node in group:
                    if not node._done:
                        node._fail(error)
            for node in group:
                if not node._done and node._error is None:
                    node._fail(PlanError(f"{node!r} was given no result"))


def _overlap(these: frozenset[Hashable], those: frozenset[Hashable]) -> bool:
    """Whether any of these resources may be one of those."""
    if these & those:
        return True
    return any(
        (isinstance(this, Resource) and this.overlaps(that))
        or (isinstance(that, Resource) and that.overlaps(this))
        for this in these
        for that in those
    )


type _Resources = tuple[frozenset[Hashable], frozenset[Hashable]] | None
"""What a node reads and writes, or None if it doesn't say."""


class _Order:
    """Which ready nodes a plan runs now, and which it holds back: a node may wait for a node
    of its class that's not ready yet, so they can share a group, as long as the result is
    the one running in graph order gives.

    Running in graph order, a node runs at its level (one past its inputs' highest), and the
    nodes of a level at once. So of two nodes whose resources conflict (one writes what the
    other reads or writes, or either declares nothing), the one of the lower level runs
    first, in an earlier round; nodes that don't conflict may run in any order. A node is
    held back only while that stays true: once a node of a higher level that conflicts with
    it is ready, it runs, and that node waits for the next round. A node never waits for one
    that depends on it.
    """

    def __init__(self, graph: Graph, plan: Plan) -> None:
        self.graph = graph
        self.plan = plan
        self.level: dict[Node[object], int] = {}
        for node in graphlib.TopologicalSorter(graph).static_order():
            self.level[node] = 1 + max((self.level[input] for input in graph[node]), default=-1)
        self.dependents: dict[Node[object], list[Node[object]]] = {node: [] for node in graph}
        for node, inputs in graph.items():
            for input in inputs:
                self.dependents[input].append(node)
        self.seen: set[Node[object]] = set()  # every node that's been ready
        self.resources: dict[Node[object], _Resources] = {}
        self.key: dict[Node[object], Hashable] = {}
        self.lane: dict[Node[object], Hashable | None] = {}
        self.waits: dict[Node[object], bool] = {}

    def choose(self, ready: list[Node[object]]) -> tuple[list[Node[object]], list[Node[object]]]:
        """The ready nodes to run now, and those to hold back."""
        self.seen.update(ready)
        movable: list[Node[object]] = []
        now: list[Node[object]] = []
        for node in ready:
            # One done, or that won't run (it reads a node that failed), goes at once.
            failed = next((input for input in self.graph[node] if not input._done), None)
            if failed is not None:
                node._skip(failed)
            if node._done or node._error is not None or not self._ask(node):
                now.append(node)
            else:
                movable.append(node)
        groups: dict[Hashable, list[Node[object]]] = {}
        for node in movable:
            groups.setdefault(self.key[node], []).append(node)
        waiting = {
            node for group in groups.values() if self._worth_waiting(group) for node in group
        }
        changed = True
        while changed:  # till no node that would run must run after one waiting
            changed = False
            for node in movable:
                for other in [*waiting]:
                    if node not in waiting and self._before(other, node):
                        waiting.discard(other)
                        changed = True
        if len(waiting) == len(movable):
            waiting.clear()  # else nothing would run, and nothing become ready
        held = [node for node in movable if node in waiting]
        for node in sorted((node for node in movable if node not in waiting), key=self._level):
            if any(self._before(other, node) for other in (*now, *held)):
                held.append(node)  # one it conflicts with runs first
            else:
                now.append(node)
        return now, held

    def _ask(self, node: Node[object]) -> bool:
        """Ask the node what the plan needs to know (once its inputs are done), or fail it
        with what it raises: whether it can run."""
        if node in self.key:
            return True
        try:
            key = (type(node), node.batch_key(self.plan))
            lane = node.lane(self.plan)
            reads, writes = node.reads(self.plan), node.writes(self.plan)
            self.waits[node] = node.waits(self.plan)
        # Whatever it raises is its failure.
        except Exception as error:  # noqa: BLE001
            node._fail(error)
            return False
        self.key[node], self.lane[node] = key, lane
        if reads is None or writes is None:
            self.resources[node] = None
        else:
            self.resources[node] = (frozenset(reads), frozenset(writes))
        return True

    def _level(self, node: Node[object]) -> int:
        return self.level[node]

    def _before(self, first: Node[object], then: Node[object]) -> bool:
        """Whether `first` must run in an earlier round than `then`."""
        if self.level[first] >= self.level[then]:
            return False
        if first not in self.resources or then not in self.resources:
            return False  # one done already, or skipped, runs nothing
        a, b = self.resources[first], self.resources[then]
        if a is None or b is None:
            return True
        return _overlap(a[1], b[0] | b[1]) or _overlap(b[1], a[0])

    def _worth_waiting(self, group: list[Node[object]]) -> bool:
        """Whether a node of the group's class that's not been ready yet, and depends on
        none of it, might join it."""
        if any(self.resources[node] is None or not self.waits[node] for node in group):
            return False
        after = self._descendants(group)
        return any(
            type(other) is type(group[0]) and other not in self.seen and other not in after
            for other in self.graph
        )

    def _descendants(self, nodes: list[Node[object]]) -> set[Node[object]]:
        found: set[Node[object]] = set()
        pending = [*nodes]
        while pending:
            for dependent in self.dependents[pending.pop()]:
                if dependent not in found:
                    found.add(dependent)
                    pending.append(dependent)
        return found
