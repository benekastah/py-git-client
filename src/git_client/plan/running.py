"""The plan: its nodes in program order, run in rounds."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

import graphlib
import itertools
from collections.abc import Generator, Hashable

from git_client.client import Git
from git_client.plan.nodes import Effect, ExecutionException, Node, PlanError, Skipped
from git_client.plan.resources import Access


class Plan:
    """Nodes to run with `git`, as if one at a time in program order (see `order`), in as
    few processes as git's batch forms allow. `atomic_pushes` is what a Push's
    `atomic=None` means.

    With `reorder` (the default), a node runs earlier than program order says where that
    can't change a result: once its inputs are done, if no node before it that hasn't run
    may conflict with it (writes what it reads or writes, or reads what it writes). A node
    whose resources aren't known (it declares none, or its inputs haven't run) may conflict
    with anything. Each round runs every node that may run, but for those that write, while
    a read may run: so reads run as early as they can, and then the writes that are left
    wait for nothing but each other, and share groups. A round's nodes don't conflict, so
    its groups run at once: the plan starts every group's git process, then waits for each
    in turn, in the calling thread (work in-process just runs). Without `reorder`, nodes
    run one at a time, in program order. In both, a node may settle its outcome from what
    came before it without running (Node.settle: a repeated Push), and say before anything
    runs that the plan is wrong (Node.check: ConflictingPushes).

    Each round checks each node left against every one left before it, so a round costs
    O(n²) for n nodes, and a plan O(n³) at worst over its rounds: fine for the hundreds of
    nodes a tool's plan has, not for millions.

    Every process starts through `git` (its `_start`, see Started), so its timeout,
    `stopping` and process groups apply as to any of its commands; its `performance`
    recorder times them. On Ctrl-C, execute()
    stops every git process the round started (as an interrupted command is stopped: see
    ReadOnlyGit), then raises KeyboardInterrupt.
    """

    def __init__(
        self,
        git: Git,
        *,
        atomic_pushes: bool = False,
        reorder: bool = True,
    ) -> None:
        self.git = git
        self.atomic_pushes = atomic_pushes
        self.reorder = reorder
        self.ops: list[Node[object]] = []
        self._access: dict[Node[object], Access | None] = {}
        self._key: dict[Node[object], Hashable] = {}
        self._order: list[Node[object]] = []
        self._position: dict[Node[object], int] = {}

    def add[N: Node[object]](self, op: N) -> N:
        """Add `op` (and so what it reads) to the plan, after what's there, and give it
        back."""
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

    def order(self) -> list[Node[object]]:
        """Every node the ops reach, once each, in program order: the ops in the order they
        were added, each after the nodes it reads (`inputs`, in their order) that aren't
        before it already. Raises graphlib.CycleError if nodes read each other."""
        order: list[Node[object]] = []
        placed: set[Node[object]] = set()
        for op in self.ops:
            if op in placed:
                continue
            stack = [(op, iter(op.inputs()))]
            reading = {op}  # the nodes on the stack
            while stack:
                node, inputs = stack[-1]
                input = next((input for input in inputs if input not in placed), None)
                if input is None:
                    stack.pop()
                    reading.discard(node)
                    placed.add(node)
                    order.append(node)
                elif input in reading:
                    raise graphlib.CycleError("nodes read each other", [*reading, input])
                else:
                    stack.append((input, iter(input.inputs())))
                    reading.add(input)
        return order

    def execute(self) -> None:
        """Run every node not done yet, so again those that failed or were skipped, after a
        failure. Raises graphlib.CycleError, running nothing, if nodes read each other;
        ExecutionException once the nodes that could run did, if one failed."""
        order = self.order()
        self._position = {node: index for index, node in enumerate(order)}
        self._order = order
        left = [node for node in order if not node._done]
        for node in left:
            object.__setattr__(node, "_error", None)
        self._access.clear()
        self._key.clear()
        self._check(left)
        groups_of: dict[Node[object], list[Node[object]]] = {}
        while left:
            groups = self._round(left)
            for group in groups:
                groups_of.update((node, group) for node in group)
            self._run(groups)
            still = [node for node in left if not node._done and node._error is None]
            # Each round finishes at least one node: the first left (all before it have
            # finished), or the reads that run before it. So this is a bug's, never a plan's.
            if len(still) == len(left):
                raise PlanError(f"no node of {left!r} could run")
            left = still
        failures = {
            node: node._error
            for node in order
            if node._error is not None and not isinstance(node._error, Skipped)
        }
        if failures:
            first = next(iter(failures))
            group = groups_of.get(first, [first])
            raise ExecutionException(first, group, failures) from failures[first]

    def _round(self, left: list[Node[object]]) -> list[list[Node[object]]]:
        """The groups to run now, of the nodes `left` (in program order): see Plan. Skips
        a node whose input failed, and fails one whose questions raise."""
        may_run: list[Node[object]] = []
        before: list[Node[object]] = []  # the nodes left before this one
        for node in left:
            inputs = list(node.inputs())
            failed = next((input for input in inputs if input._error is not None), None)
            if failed is not None and all(i._done or i._error is not None for i in inputs):
                node._skip(failed)  # once all have finished: the first that failed
                continue
            if all(input._done for input in node.inputs()):
                if not self._ask(node):
                    continue
                if not any(self._conflict(earlier, node) for earlier in before):
                    if node.settle(self, self._order[: self._position[node]]):
                        continue  # given its outcome: finished
                    may_run.append(node)
            before.append(node)
        if not self.reorder:
            may_run = may_run[:1]
        reads = [node for node in may_run if (access := self._access[node]) and not access.writes]
        groups: dict[Hashable, list[Node[object]]] = {}
        for node in reads or may_run:
            groups.setdefault(self._key[node], []).append(node)
        return list(groups.values())

    def _ask(self, node: Node[object]) -> bool:
        """Ask the node what the plan needs (once its inputs are done), or fail it with what
        it raises: whether it can run."""
        if node in self._key:
            return True
        try:
            key = (type(node), node.batch_key(self))
            reads, writes = node.reads(self), node.writes(self)
        # Whatever it raises is its failure.
        except Exception as error:  # noqa: BLE001
            node._fail(error)
            return False
        self._key[node] = key
        self._access[node] = (
            None if reads is None or writes is None else Access(frozenset(reads), frozenset(writes))
        )
        return True

    def access(self, node: Node[object]) -> Access | None:
        """What a node of this run declared it reads and writes, once asked (when its
        inputs were done); nothing for a node done before this run, which changes nothing
        now; None if it declared nothing or wasn't asked."""
        if node in self._access:
            return self._access[node]
        return Access(frozenset(), frozenset()) if node._done else None

    def _check(self, left: list[Node[object]]) -> None:
        """Ask each node whose inputs are done whether the plan is wrong (Node.check),
        before anything runs."""
        for node in left:
            if all(input._done for input in node.inputs()):
                self._ask(node)
        for node in left:
            if node in self._key:
                node.check(self, self._order[: self._position[node]])

    def _conflict(self, earlier: Node[object], node: Node[object]) -> bool:
        a, b = self._access.get(earlier), self._access[node]
        return a is None or b is None or a.conflicts(b)

    def _run(self, groups: list[list[Node[object]]]) -> None:
        """Run the groups: start each one's processes, then collect each (see
        Node.execute_group). On an interrupt, stop every process started."""
        running: list[tuple[list[Node[object]], Generator[None]]] = []
        try:
            for group in groups:
                try:
                    steps = type(group[0]).execute_group(group, self)
                    if steps is not None:
                        running.append((group, steps))
                        next(steps, None)
                # Whatever it raises is its members' failure.
                except Exception as error:  # noqa: BLE001
                    _fail(group, error)
            for group, steps in running:
                try:
                    for _ in steps:
                        pass
                except Exception as error:  # noqa: BLE001
                    _fail(group, error)
        except BaseException:
            for _, steps in running:
                steps.close()  # which stops its processes
            raise
        for group in groups:
            for node in group:
                if not node._done and node._error is None:
                    node._fail(PlanError(f"{node!r} was given no result"))


def _fail(group: list[Node[object]], error: Exception) -> None:
    """Fail the group's members left without a result with `error`."""
    for node in group:
        if not node._done:
            node._fail(error)
