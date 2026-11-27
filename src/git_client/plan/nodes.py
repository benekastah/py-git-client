"""A plan's nodes: what every node has, plain values, derived ones, and effects."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

from collections.abc import Callable, Generator, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast, override

from git_client.plan.resources import Resource

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
    node's `batch_key`, `reads` or `writes` that raises fails that node."""

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

    A subclass says which nodes it reads (`inputs`), which resources it reads and writes
    (`reads`, `writes`), which nodes may run together (`batch_key`, and the class: nodes of
    different classes never share a group), and how a group runs (`execute_group`). Nodes
    compare by identity, frozen ones too.
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

    def reads(self, plan: Plan) -> Iterable[Resource] | None:
        """What the node reads (e.g. CONFIG), asked once its inputs are done, so the plan
        knows which nodes it may run it before (see Plan). None, the default: undeclared,
        as if it read and wrote everything, so nothing moves past it."""
        del plan
        return None

    def writes(self, plan: Plan) -> Iterable[Resource] | None:
        """What the node writes, as `reads`."""
        del plan
        return None

    # Its nodes are Any so that a subclass can take Sequence[Self]: basedpyright refuses a
    # Self parameter's override.
    @classmethod
    def execute_group(cls, nodes: Sequence[Any], plan: Plan) -> Generator[None] | None:
        """Run the group (nodes of this class), and give each member its result (`_set`)
        or its own error (`_fail`). An exception it raises fails every member left without
        either. One that runs git is a generator that starts its process (with Started),
        yields while it runs, then collects it: the plan starts every group's process
        before it collects any, so they run at once, in no thread but the caller's."""
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


@dataclass(frozen=True, eq=False)
class Value[T](Node[T]):
    """A value known already: done from the start."""

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
    against `inputs`: Python can't map each input's type to `Node[...]`. `fn` must be pure:
    it reads and writes nothing but its arguments and result, as Map declares."""

    def __init__(self, fn: Callable[..., T], *inputs: Node[object]) -> None:
        self.fn = fn
        self._inputs = inputs

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return self._inputs

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
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
