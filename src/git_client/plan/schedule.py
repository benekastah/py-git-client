"""Which of a plan's ready nodes run each round, in which groups and lanes, so that the
result is what running the graph in order gives (see Plan).

Running the graph in order, each node runs at its level (one past its inputs' highest), a
level at a time. Of two nodes that conflict (one writes what the other reads or writes, or
either doesn't say: see Access), the order matters only if their levels differ, and then
the lower one runs first. So a schedule gives the same result if, for every two nodes that
may conflict, the lower one has finished before the other starts, and two of one level don't
run at once. A node that isn't ready yet may conflict with anything: its resources aren't
known till its inputs are done.
"""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

import graphlib
import math
from collections.abc import Hashable, Iterable
from typing import TYPE_CHECKING

from git_client.plan.nodes import Node
from git_client.plan.resources import Access

if TYPE_CHECKING:
    from git_client.plan.running import Graph, Plan

type Group = list[Node[object]]
"""Nodes that run together, in level order."""


class Schedule:
    """Each round of a plan's run: `round` takes the ready nodes, and gives the lanes to run
    (each a list of groups to run in turn; the lanes at once), and the nodes held back.

    A group of nodes that may `wait` holds back while a node of its class that isn't ready
    yet (and depends on none of the group) might join it, unless a ready node of a higher
    level conflicts with it. Any node runs only if no node of a lower level that hasn't run
    may conflict with it: one held back, or one not ready yet, which may conflict with
    anything. Of the nodes that run in a round, those that conflict share a lane, in level
    order, and nodes of different levels share a group only if their class `keeps_order`.
    """

    def __init__(self, graph: Graph, plan: Plan, *, reorder: bool) -> None:
        self.graph = graph
        self.plan = plan
        self.reorder = reorder
        self.level: dict[Node[object], int] = {}
        for node in graphlib.TopologicalSorter(graph).static_order():
            self.level[node] = 1 + max((self.level[input] for input in graph[node]), default=-1)
        self.dependents: dict[Node[object], list[Node[object]]] = {node: [] for node in graph}
        for node, inputs in graph.items():
            for input in inputs:
                self.dependents[input].append(node)
        self.finished: set[Node[object]] = set()  # run, failed or skipped
        self.key: dict[Node[object], Hashable] = {}
        self.lane: dict[Node[object], Hashable | None] = {}
        self.access: dict[Node[object], Access | None] = {}
        self.waits: dict[Node[object], bool] = {}

    def round(self, ready: Iterable[Node[object]]) -> tuple[list[list[Group]], list[Node[object]]]:
        """The lanes to run now, and the ready nodes to hold back. The other ready nodes
        (done, skipped, or failed as they were asked about) need no running."""
        candidates: list[Node[object]] = []
        for node in ready:
            failed = next((input for input in self.graph[node] if not input._done), None)
            if failed is not None:
                node._skip(failed)
            if node._done or node._error is not None or not self._ask(node):
                self.finished.add(node)
            else:
                candidates.append(node)
        chosen = set(candidates)
        lowest = min(
            (
                self.level[node]
                for node in self.graph
                if not node._done and node not in self.finished and node not in chosen
            ),
            default=math.inf,
        )
        groups: dict[Hashable, Group] = {}
        for node in candidates:
            groups.setdefault(self.key[node], []).append(node)
        waiting = {
            node
            for group in groups.values()
            if self._worth_waiting(group, chosen)
            for node in group
        }
        run = self._runnable(candidates, groups, waiting, lowest) or self._runnable(
            candidates, groups, set(), lowest
        )
        self.finished |= run
        return self._lanes([node for node in candidates if node in run]), [
            node for node in candidates if node not in run
        ]

    def _ask(self, node: Node[object]) -> bool:
        """Ask the node what the schedule needs (once its inputs are done), or fail it with
        what it raises: whether it can run."""
        if node in self.key:
            return True
        try:
            key = (type(node), node.batch_key(self.plan))
            lane = node.lane(self.plan)
            reads, writes = node.reads(self.plan), node.writes(self.plan)
            waits = node.waits(self.plan)
        # Whatever it raises is its failure.
        except Exception as error:  # noqa: BLE001
            node._fail(error)
            return False
        self.key[node], self.lane[node], self.waits[node] = key, lane, waits
        self.access[node] = (
            None if reads is None or writes is None else Access(frozenset(reads), frozenset(writes))
        )
        return True

    def _conflict(self, first: Node[object], then: Node[object]) -> bool:
        a, b = self.access[first], self.access[then]
        return a is None or b is None or a.conflicts(b)

    def _runnable(
        self,
        candidates: list[Node[object]],
        groups: dict[Hashable, Group],
        waiting: set[Node[object]],
        lowest: float,
    ) -> set[Node[object]]:
        """The candidates that may run now, all but those `waiting`: none above `lowest`,
        the lowest level of a node that hasn't run and isn't a candidate, nor above a
        candidate that won't run and may conflict with it. A waiting group that keeps a
        higher candidate from running runs."""
        waiting = set(waiting)
        while True:
            run = {
                node for node in candidates if node not in waiting and self.level[node] <= lowest
            }
            released = False
            changed = True
            while changed and not released:
                changed = False
                for then in sorted(run, key=self.level.__getitem__):
                    first = next(
                        (
                            node
                            for node in candidates
                            if node not in run
                            and self.level[node] < self.level[then]
                            and self._conflict(node, then)
                        ),
                        None,
                    )
                    if first is None:
                        continue
                    if first in waiting:
                        waiting -= set(groups[self.key[first]])
                        released = True
                    else:
                        run.discard(then)
                        changed = True
                    break
            if not released:
                return run

    def _worth_waiting(self, group: Group, candidates: set[Node[object]]) -> bool:
        """Whether every member may wait, and a node of their class that's not been ready
        yet (nor a candidate now), and depends on none of them, might join them."""
        if not self.reorder or not all(self.waits[node] for node in group):
            return False
        after = self._descendants(group)
        return any(
            type(other) is type(group[0])
            and not other._done
            and other not in self.finished
            and other not in after
            and other not in candidates
            for other in self.graph
        )

    def _descendants(self, nodes: Group) -> set[Node[object]]:
        found: set[Node[object]] = set()
        pending = [*nodes]
        while pending:
            for dependent in self.dependents[pending.pop()]:
                if dependent not in found:
                    found.add(dependent)
                    pending.append(dependent)
        return found

    def _lanes(self, run: list[Node[object]]) -> list[list[Group]]:
        """The nodes to run, in groups, in lanes: a group's members in level order; the
        groups that may conflict on one lane, in level order."""
        by_key: dict[Hashable, Group] = {}
        for node in sorted(run, key=self.level.__getitem__):
            by_key.setdefault(self.key[node], []).append(node)
        groups = [part for group in by_key.values() for part in self._split(group)]
        try:
            order = self._order(groups)
        except graphlib.CycleError:  # a group must run both before and after another
            groups = [part for group in groups for part in self._by_level(group)]
            order = self._order(groups)
        lanes: dict[int, list[Group]] = {}
        shared = self._shared_lanes(groups)
        for index in order:
            lanes.setdefault(shared[index], []).append(groups[index])
        return list(lanes.values())

    def _split(self, group: Group) -> list[Group]:
        """The group, or its levels apart if two of its levels conflict in a way its class
        doesn't keep in order."""
        cls = type(group[0])
        for first in group:
            for then in group:
                a, b = self.access[first], self.access[then]
                kept = a is not None and b is not None and cls.keeps_order(a, b)
                if (
                    self.level[first] < self.level[then]
                    and self._conflict(first, then)
                    and not kept
                ):
                    return self._by_level(group)
        return [group]

    def _by_level(self, group: Group) -> list[Group]:
        levels: dict[int, Group] = {}
        for node in group:
            levels.setdefault(self.level[node], []).append(node)
        return list(levels.values())

    def _order(self, groups: list[Group]) -> list[int]:
        """The groups' indexes, each after those with a lower node it may conflict with."""
        before: dict[int, set[int]] = {index: set() for index in range(len(groups))}
        for i, first in enumerate(groups):
            for j, then in enumerate(groups):
                if i != j and any(
                    self.level[a] < self.level[b] and self._conflict(a, b)
                    for a in first
                    for b in then
                ):
                    before[j].add(i)
        return list(graphlib.TopologicalSorter(before).static_order())

    def _shared_lanes(self, groups: list[Group]) -> list[int]:
        """Each group's lane: one for groups that may conflict, or that name one lane."""
        lane = list(range(len(groups)))

        def find(index: int) -> int:
            while lane[index] != index:
                index = lane[index]
            return index

        for i, first in enumerate(groups):
            for j in range(i):
                then = groups[j]
                named = self.lane[first[0]]
                if (named is not None and named == self.lane[then[0]]) or any(
                    self._conflict(a, b) for a in first for b in then
                ):
                    lane[find(i)] = find(j)
        return [find(index) for index in range(len(groups))]
