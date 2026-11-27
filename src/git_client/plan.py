"""A plan: git operations collected as a graph of nodes, then run in as few git processes as
git's own batch forms allow, the independent ones in parallel. A prototype: not exported from
`git_client` yet.

    plan = Plan(git)
    for branch in ("a", "b"):
        plan.add(Push("origin", branch, force=ConfigValue("example.force").as_bool()))
    plan.execute()

Each node's fields are nodes too (`lift` wraps a plain value in a `Value`), and a node reads
them only through `get()` once they've run. The plan crawls the graph from the ops it was
given (`Node.inputs`), and runs it in rounds: the nodes whose inputs are done, grouped by
class and `batch_key`, each group run once by its class's `execute_group` (one git process,
where git has a batch form), the groups on different lanes in parallel. Nodes are never
replaced: a group writes each member's result back onto the member.

Only data and `after=` order nodes: ops with no edge between them are independent, whatever
order they were added in. To run fewer processes, a ready group may wait for a node of its
class that isn't ready yet (a push whose `force` is still to be read from config, say), but
only where that gives the same result as running the graph in order: each node declares the
resources it reads and writes (`Node.reads`, `Node.writes`; e.g. CONFIG, a remote's ref), and
a node never moves past one it conflicts with (one writes what the other reads or writes), nor
past an edge. A node that declares nothing conflicts with everything, so nothing moves past it.
Resources that may be the same without being equal (`Resource.overlaps`, e.g. two remotes'
remote-tracking refs) count as conflicting. A node whose group could give another result
than its members one by one (an atomic push: `Node.waits`) never waits.
"""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the module.
# pyright: reportPrivateUsage=false

import graphlib
import itertools
import re
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Self, cast, override

from git_client.batch import config
from git_client.client import Git, decode, error_from
from git_client.errors import PushRejectedError, error_for
from git_client.pushing import PushRef, PushStatus, PushUpdate, push


class PlanError(Exception):
    """What a plan raises for itself, rather than git."""


class NotExecuted(PlanError):
    """`get()` on a node that has no result: it hasn't run, it failed, or it depends on a
    node that failed (its `__cause__` says why, if it failed)."""


class ExecutionException(PlanError):
    """A node failed: `node` is the first that did (in the order the plan crawled them), and
    `group` the nodes it ran with; the exception is chained from its error. `failures` has
    every node that failed and its error. The results of the nodes that ran stay readable;
    the nodes that depend on a failed one didn't run."""

    def __init__(
        self,
        node: Node[object],
        group: Sequence[Node[object]],
        failures: dict[Node[object], Exception],
    ) -> None:
        super().__init__(f"{node!r} failed: {failures[node]}")
        self.node = node
        self.group = tuple(group)
        self.failures = failures


class Node[T]:
    """One value or operation of a plan, giving a `T` once it has run.

    A subclass says what it reads (`inputs`), which nodes may run together (`batch_key`, and
    the class: nodes of different classes never share a group), which groups mustn't run at
    once (`lane`), and how a group runs (`execute_group`). Nodes compare by identity, but for
    frozen dataclasses (reads), which compare by value: the plan runs equal ones once, and
    gives each the result.
    """

    _done: bool = False
    _error: Exception | None = None
    _result: object = None

    def inputs(self) -> Iterable[Node[object]]:
        """The nodes this one reads, which run before it."""
        return ()

    def batch_key(self, plan: Plan) -> Hashable:
        """Nodes of this class with equal keys run as one group. Asked only once the inputs
        are done. By default, each node is a group of its own."""
        del plan
        return id(self)

    def lane(self, plan: Plan) -> Hashable | None:
        """Groups with the same lane (not None) run one after another; the others run in
        parallel. Asked once the inputs are done."""
        del plan
        return None

    def reads(self, plan: Plan) -> Iterable[Hashable] | None:
        """What the node reads (e.g. CONFIG), asked once its inputs are done, so the plan
        knows which nodes it may run it before or after (see Plan). None, the default:
        undeclared, as if it read and wrote everything, so it's never moved."""
        del plan
        return None

    def writes(self, plan: Plan) -> Iterable[Hashable] | None:
        """What the node writes, as `reads`."""
        del plan
        return None

    def waits(self, plan: Plan) -> bool:
        """Whether the node may wait for a node that isn't ready yet, to share its group
        (see Plan): False for one whose group's result could differ from running its members
        one after another (e.g. an atomic push). Asked once its inputs are done."""
        del plan
        return True

    # Its nodes are Any so that a subclass can take Sequence[Self]: basedpyright refuses a
    # Self parameter's override.
    @classmethod
    def execute_group(cls, nodes: Sequence[Any], plan: Plan) -> None:
        """Run the group (nodes of this class), and give each member its result (`_set`)
        or its own error (`_fail`). An exception it raises fails every member left without
        either."""
        raise NotImplementedError

    def get(self) -> T:
        """The result. Raises NotExecuted if there's none (see NotExecuted)."""
        if self._error is not None:
            raise NotExecuted(f"{self!r} failed") from self._error
        if not self._done:
            raise NotExecuted(f"{self!r} hasn't run")
        return cast(T, self._result)

    def _set(self, result: object) -> None:
        # It takes any result: a T parameter would keep T from being covariant.
        # object's own, as a frozen dataclass refuses its fields' setattr.
        object.__setattr__(self, "_result", result)
        object.__setattr__(self, "_done", True)

    def _fail(self, error: Exception) -> None:
        object.__setattr__(self, "_error", error)

    def _copy_outcome(self, other: Node[object]) -> None:
        for name in ("_done", "_error", "_result"):
            object.__setattr__(self, name, getattr(other, name))


@dataclass(frozen=True)
class Value[T](Node[T]):
    """A value known already: done from the start. Equal values are equal nodes, so `value`
    must be hashable."""

    value: T

    def __post_init__(self) -> None:
        self._set(self.value)


CONFIG = ("config",)
"""The resource that what reads or writes git's config declares (see Node.reads)."""


class Resource:
    """A resource that may be the same as others it doesn't equal: `overlaps` says which.
    Other resources are the same only if equal."""

    def overlaps(self, other: Hashable) -> bool:
        """Whether `other` may be the same resource (or share some of it)."""
        return self == other


@dataclass(frozen=True)
class TrackingRef(Resource):
    """The remote-tracking ref git updates as it pushes `ref` to `remote`: one of the same
    remote's for the same ref, or any other remote's, since git maps each remote's refs by
    its own fetch refspecs, which may share remote-tracking refs (and a name and its URL may
    be one remote)."""

    remote: str
    ref: str

    @override
    def overlaps(self, other: Hashable) -> bool:
        return isinstance(other, TrackingRef) and (
            other.ref == self.ref or other.remote != self.remote
        )


def lift[T](value: T | Node[T]) -> Node[T]:
    """`value` if it's a node already, else a Value of it."""
    if isinstance(value, Node):
        return cast(Node[T], value)
    return Value(value)


class Map[T](Node[T]):
    """`fn` of the values of `inputs`, computed in-process once they're done: a derived value
    (e.g. a branch name built from others), never one of git's decisions (config syntax,
    ref names, ...), which git makes. It can't add ops to the plan. It's never batched, and
    an exception it raises fails it like any other failure. `fn`'s parameters aren't typed
    against `inputs`: Python can't map each input's type to `Node[...]`. `fn` must be pure:
    it reads and writes nothing but its arguments and result, as Map declares."""

    def __init__(self, fn: Callable[..., T], *inputs: Node[object]) -> None:
        self.fn = fn
        self._inputs = inputs

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return self._inputs

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            node._set(node.fn(*(input.get() for input in node._inputs)))

    @override
    def __repr__(self) -> str:
        return f"Map({self.fn!r})"


@dataclass(frozen=True, init=False)
class ConfigValue(Node[str | None]):
    """A config key's value as git reads it (the last one, for a key with several), or None
    if it's not set. `key` is in the form git prints (`config()`'s): section and name in
    lower case. A key with no value (`[core] bare`) gives "". Every ConfigValue of a round
    is read by one `git config --list -z`, however many keys."""

    key: Node[str]

    def __init__(self, key: str | Node[str]) -> None:
        object.__setattr__(self, "key", lift(key))

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.key,)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return None

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        return (CONFIG,)

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        entries = config(plan.git)
        for node in nodes:
            values = entries.get(node.key.get())
            node._set((values[-1] or "") if values else None)

    def as_bool(self, default: bool = False) -> TypedConfig[bool]:
        """A node of the value as git reads a bool (`--type=bool`: "yes", "on", "1", no
        value...), or `default` if it's not set. It reads nothing until the plan runs."""
        return TypedConfig(self.key, "bool", default)

    def as_int(self, default: int = 0) -> TypedConfig[int]:
        """A node of the value as git reads an int (`--type=int`: "1k" is 1024), or
        `default`. It reads nothing until the plan runs."""
        return TypedConfig(self.key, "int", default)

    def or_default(self, default: str) -> Node[str]:
        """A node of the value, or `default` if it's not set."""

        def value_or_default(value: str | None) -> str:
            return default if value is None else value

        return Map(value_or_default, self)


# What git prints for each of TypedConfig's types, to the value.
_PARSERS: dict[str, Callable[[str], object]] = {"bool": lambda text: text == "true", "int": int}

# The characters special in a POSIX extended regular expression.
_ERE_SPECIAL = re.compile(r"[.\[\\()*+?{|^$]")


@dataclass(frozen=True)
class TypedConfig[T](Node[T]):
    """A config key's value as git reads a `git_type` ("bool" or "int": `git config --type`),
    or `default` if it's not set: made by `ConfigValue.as_bool` and `as_int`. Each round's
    TypedConfigs of one type are read by one `git config --type=<type> --get-regexp`, whose
    pattern matches their keys alone (in the form git prints, as ConfigValue's): git, not
    Python, reads the config syntax. A value of a key's that isn't one (a bool "maybe")
    fails the whole group."""

    key: Node[str]
    git_type: str
    default: T

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.key,)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return self.git_type

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        return (CONFIG,)

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        keys = dict.fromkeys(node.key.get() for node in nodes)
        pattern = "|".join(_ERE_SPECIAL.sub(r"\\\g<0>", key) for key in keys)
        git_type = nodes[0].git_type
        args = ["config", "-z", f"--type={git_type}", "--get-regexp", f"^({pattern})$"]
        result = plan.git.process(*args, capture_output=True)
        if result.returncode not in (0, 1):  # 1: none of them is set
            raise error_from(args, result)
        typed: dict[str, str] = {}
        for entry in decode(result.stdout).split("\0"):
            key, _, value = entry.partition("\n")
            typed[key] = value  # the last one wins, as in `git config --get`
        for node in nodes:
            key = node.key.get()
            node._set(cast(T, _PARSERS[git_type](typed[key])) if key in typed else node.default)


class Effect[T](Node[T]):
    """A node that changes something, which can be ordered after other nodes with `after`,
    on top of what it reads (see Plan.sequence)."""

    after: list[Node[object]]

    def _reads(self) -> Iterable[Node[object]]:
        return ()

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (*self._reads(), *self.after)


# The statuses of a ref a push did, or found done.
_PUSHED = {
    PushStatus.FAST_FORWARD,
    PushStatus.FORCED,
    PushStatus.CREATED,
    PushStatus.DELETED,
    PushStatus.UP_TO_DATE,
}


class Push(Effect[tuple[PushRef, ...]]):
    """Push the branch `branch` to the same branch of `remote` (a name or URL), forced if
    `force` says so, or only if the remote's branch is at `lease` (see PushUpdate). Its
    result is what git reported for the branch: a PushRef for each of the remote's push URLs.

    The round's pushes to one remote with the same `atomic` (None: the plan's
    `atomic_pushes`) are one `push` with an update each: atomic, all or nothing; else each
    branch succeeds or fails alone. Groups to one remote run one after another; to
    different remotes, in parallel. A branch fails if git rejected it (PushRejectedError),
    if a destination's report of it isn't done, or if git didn't report it, or failed with
    nothing rejected (as git's message says).
    """

    def __init__(
        self,
        remote: str | Node[str],
        branch: str | Node[str],
        *,
        force: bool | Node[bool] = False,
        lease: str | None | Node[str | None] = None,
        atomic: bool | None = None,
        after: Iterable[Node[object]] = (),
    ) -> None:
        self.remote = lift(remote)
        self.branch = lift(branch)
        self.force = lift(force)
        self.lease = lift(lease)
        self.atomic = atomic
        self.after = list(after)

    @override
    def _reads(self) -> Iterable[Node[object]]:
        return (self.remote, self.branch, self.force, self.lease)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return self.remote.get(), self._atomic(plan)

    @override
    def lane(self, plan: Plan) -> Hashable | None:
        return Push, self.remote.get()

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        # The remote's config, and the local branch.
        return CONFIG, ("ref", ".", self._ref())

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        # The branch at the remote, whichever: two remotes may be one repository (a name and
        # its URL, say), which only git could tell. And the remote-tracking ref git updates.
        return ("ref", "remote", self._ref()), TrackingRef(self.remote.get(), self._ref())

    @override
    def waits(self, plan: Plan) -> bool:
        # An atomic push of more branches can fail where one of fewer wouldn't.
        return not self._atomic(plan)

    def _atomic(self, plan: Plan) -> bool:
        return plan.atomic_pushes if self.atomic is None else self.atomic

    def _ref(self) -> str:
        return f"refs/heads/{self.branch.get()}"

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        remote, atomic = cast(tuple[str, bool], nodes[0].batch_key(plan))
        updates = dict.fromkeys(
            PushUpdate(node._ref(), node._ref(), force=node.force.get(), lease=node.lease.get())
            for node in nodes
        )
        result = push(plan.git, remote, updates, atomic=atomic)
        reports = [
            (destination, ref) for destination in result.destinations for ref in destination.refs
        ]
        # Git failed though every ref it reported is pushed: for a destination it didn't
        # report (see push).
        unexplained = result.returncode != 0 and all(
            destination.done and ref.status in _PUSHED for destination, ref in reports
        )
        # What a GitError shows as the command: push() doesn't say its own.
        command = ["push", remote, *(update.target for update in updates)]
        for node in nodes:
            mine = [(destination, ref) for destination, ref in reports if ref.target == node._ref()]
            if (
                mine
                and not unexplained
                and all(destination.done and ref.status in _PUSHED for destination, ref in mine)
            ):
                node._set(tuple(ref for _, ref in mine))
                continue
            rejected = any(ref.status is PushStatus.REJECTED for _, ref in mine)
            error = (
                PushRejectedError
                if rejected
                else error_for(result.stderr, result.stdout, command=command)
            )
            node._fail(error(command, result.returncode, result.stderr, result.stdout))

    @override
    def __repr__(self) -> str:
        return f"Push({self.remote!r}, {self.branch!r})"


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
                if canonical[node] is not node:
                    copies[id(node)] = (node, canonical[node])
                continue
            canonical[node] = node
            graph[node] = tuple(node.inputs())
            pending += graph[node]
        return graph, list(copies.values())

    def execute(self) -> None:
        """Run every node not done yet. Raises graphlib.CycleError, running nothing, if
        nodes read each other; ExecutionException once the nodes that could run did, if
        one failed."""
        graph, copies = self._crawl()
        sorter = graphlib.TopologicalSorter(graph)
        sorter.prepare()
        order = _Order(graph, self)
        groups_of: dict[Node[object], list[Node[object]]] = {}
        held: list[Node[object]] = []
        with ThreadPoolExecutor(self.max_workers) as pool:
            while sorter.is_active():
                now, held = order.choose([*held, *sorter.get_ready()])
                self._run_round(now, graph, groups_of, pool)
                for copy, original in copies:
                    if not copy._done:
                        copy._copy_outcome(original)
                sorter.done(*now)
        failures = {node: node._error for node in graph if node._error is not None}
        if failures:
            first = next(iter(failures))
            raise ExecutionException(first, groups_of[first], failures) from failures[first]

    def _run_round(
        self,
        ready: list[Node[object]],
        graph: Graph,
        groups_of: dict[Node[object], list[Node[object]]],
        pool: ThreadPoolExecutor,
    ) -> None:
        """Run the ready nodes not done yet whose inputs are, in groups (each noted in
        `groups_of`), each lane's groups in turn, the lanes at once."""
        groups: dict[Hashable, list[Node[object]]] = {}
        for node in ready:
            blocked = any(not input._done for input in graph[node])
            if not node._done and not blocked:
                key = (type(node), node.batch_key(self))
                groups_of[node] = groups.setdefault(key, [])
                groups_of[node].append(node)
        lanes: dict[Hashable, list[list[Node[object]]]] = {}
        for group in groups.values():
            lane = group[0].lane(self)
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

    def choose(self, ready: list[Node[object]]) -> tuple[list[Node[object]], list[Node[object]]]:
        """The ready nodes to run now, and those to hold back."""
        self.seen.update(ready)
        movable: list[Node[object]] = []
        now: list[Node[object]] = []
        for node in ready:
            # One done, or that won't run (it reads a node that failed), goes at once.
            runs = not node._done and all(input._done for input in self.graph[node])
            (movable if runs else now).append(node)
        for node in movable:
            if node not in self.resources:
                reads, writes = node.reads(self.plan), node.writes(self.plan)
                if reads is None or writes is None:
                    self.resources[node] = None
                else:
                    self.resources[node] = (frozenset(reads), frozenset(writes))
        groups: dict[Hashable, list[Node[object]]] = {}
        for node in movable:
            groups.setdefault((type(node), node.batch_key(self.plan)), []).append(node)
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
        if any(self.resources[node] is None or not node.waits(self.plan) for node in group):
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
