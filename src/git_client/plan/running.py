"""The plan, which runs its nodes in rounds."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

import graphlib
import itertools
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from git_client.client import Git
from git_client.plan.nodes import Effect, ExecutionException, Node, PlanError, Skipped
from git_client.plan.schedule import Schedule

type Graph = dict[Node[object], tuple[Node[object], ...]]
"""Each node, with its inputs."""


class Plan:
    """Nodes to run with `git`, in as few processes as git's batch forms allow, groups that
    can't conflict in parallel on up to `max_workers` more threads (the calling thread runs
    one). `atomic_pushes` is what a Push's `atomic=None` means. With `reorder` (the
    default), a group may wait for a node that isn't ready yet, to share a process, where
    that gives what running the graph in order gives (see Schedule); without, nodes run in
    graph order.

    Every process starts through `git` (its `process`), so its timeout, `stopping` and
    process groups apply as to any of its commands. Ctrl-C reaches only a git the calling
    thread runs (see ReadOnlyGit): on Ctrl-C, execute() waits for the git processes other
    threads run to finish (or time out) before it raises KeyboardInterrupt.
    """

    def __init__(
        self,
        git: Git,
        *,
        atomic_pushes: bool = False,
        reorder: bool = True,
        max_workers: int | None = None,
    ) -> None:
        self.git = git
        self.atomic_pushes = atomic_pushes
        self.reorder = reorder
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
        schedule = Schedule(graph, self, reorder=self.reorder)
        groups_of: dict[Node[object], list[Node[object]]] = {}
        held: list[Node[object]] = []
        with ThreadPoolExecutor(self.max_workers) as pool:
            while sorter.is_active():
                ready = [*held, *sorter.get_ready()]
                lanes, held = schedule.round(ready)
                for lane in lanes:
                    for group in lane:
                        groups_of.update((node, group) for node in group)
                *others, mine = lanes or [[]]
                futures = [pool.submit(self._run_lane, lane) for lane in others]
                self._run_lane(mine)
                for future in futures:
                    future.result()
                for copy, original in copies:
                    if not copy._done:
                        copy._copy_outcome(original)
                sorter.done(*(node for node in ready if node not in held))
        failures = {
            node: node._error
            for node in graph
            if node._error is not None and not isinstance(node._error, Skipped)
        }
        if failures:
            first = next(iter(failures))
            group = groups_of.get(first, [first])
            raise ExecutionException(first, group, failures) from failures[first]

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
