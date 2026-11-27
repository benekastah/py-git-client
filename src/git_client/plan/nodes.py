"""A plan's nodes: what every node has, plain values, derived ones, and the resources nodes
declare."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast, override

if TYPE_CHECKING:
    from git_client.plan.running import Plan


class PlanError(Exception):
    """What a plan raises for itself, rather than git."""


class NotExecuted(PlanError):
    """`get()` on a node that has no result: it hasn't run, or it failed (chained from its
    error), or it wasn't run because a node it reads failed (Skipped)."""


class Skipped(NotExecuted):
    """`get()` on a node that didn't run because `failed`, a node it reads (directly or
    not), failed; chained from that node's error."""

    def __init__(self, node: Node[object], failed: Node[object]) -> None:
        super().__init__(f"{node!r} wasn't run: {failed!r} failed")
        self.failed = failed


class ExecutionException(PlanError):
    """A node failed: `node` is the first that did (in the order the plan crawled them), and
    `group` the nodes it ran with; the exception is chained from its error. `failures` has
    every node that failed and its error. The results of the nodes that ran stay readable;
    the nodes that depend on a failed one didn't run (their `get()` raises Skipped). A
    node's `batch_key`, `lane`, `reads`, `writes` or `waits` that raises fails that node."""

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
        if isinstance(self._error, Skipped):
            raise Skipped(self, self._error.failed) from self._error.__cause__
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
        object.__setattr__(self, "_error", None)

    def _fail(self, error: Exception) -> None:
        object.__setattr__(self, "_error", error)

    def _skip(self, input: Node[object]) -> None:
        """Mark the node as not run because `input` failed or was skipped."""
        error = input._error
        failed = error.failed if isinstance(error, Skipped) else input
        skipped = Skipped(self, failed)
        skipped.__cause__ = failed._error
        self._fail(skipped)

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


class Effect[T](Node[T]):
    """A node that changes something, which can be ordered after other nodes with `after`,
    on top of what it reads (see Plan.sequence)."""

    after: list[Node[object]]

    def _reads(self) -> Iterable[Node[object]]:
        return ()

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (*self._reads(), *self.after)
