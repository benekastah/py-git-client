"""A plan: git operations collected as a graph of nodes, then run in as few git processes as
git's own batch forms allow, the independent ones in parallel. A prototype: not exported from
`git_client` yet.

    plan = Plan(git)
    force = plan.add(ConfigValue("example.force").as_bool())
    plan.execute()
    force.get()

Each node's fields are nodes too (`lift` wraps a plain value in a `Value`), and a node reads
them only through `get()` once they've run. The plan crawls the graph from the ops it was
given (`Node.inputs`), and runs it in rounds: the nodes whose inputs are done, grouped by
class and `batch_key`, each group run once by its class's `execute_group` (one git process,
where git has a batch form), the groups on different lanes in parallel. Nodes are never
replaced: a group writes each member's result back onto the member.

Only data orders nodes: ops with no edge between them are independent, whatever order they
were added in.
"""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the module.
# pyright: reportPrivateUsage=false

import graphlib
import re
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Self, cast, override

from git_client.batch import config
from git_client.client import Git, decode, error_from


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
    against `inputs`: Python can't map each input's type to `Node[...]`."""

    def __init__(self, fn: Callable[..., T], *inputs: Node[object]) -> None:
        self.fn = fn
        self._inputs = inputs

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return self._inputs

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


type Graph = dict[Node[object], tuple[Node[object], ...]]
"""Each node, with its inputs."""


class Plan:
    """Nodes to run with `git`, in as few processes as git's batch forms allow, independent
    groups in parallel on up to `max_workers` more threads (the calling thread runs one).

    Every process starts through `git` (its `process`), so its timeout, `stopping` and
    process groups apply as to any of its commands. Ctrl-C reaches only a git the calling
    thread runs (see ReadOnlyGit).
    """

    def __init__(self, git: Git, *, max_workers: int | None = None) -> None:
        self.git = git
        self.max_workers = max_workers
        self.ops: list[Node[object]] = []

    def add[N: Node[object]](self, op: N) -> N:
        """Add `op` (and so what it reads) to the plan, and give it back."""
        self.ops.append(op)
        return op

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
        groups_of: dict[Node[object], list[Node[object]]] = {}
        with ThreadPoolExecutor(self.max_workers) as pool:
            while sorter.is_active():
                now = [*sorter.get_ready()]
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
