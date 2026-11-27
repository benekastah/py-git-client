"""The node that pushes a branch, many to one remote in one push."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

from collections.abc import Hashable, Iterable, Sequence
from typing import TYPE_CHECKING, Self, cast, override

from git_client.errors import GitError, PushRejectedError, error_for
from git_client.plan.nodes import CONFIG, Effect, Node, TrackingRef, lift
from git_client.pushing import PushRef, PushResult, PushStatus, PushUpdate, push

if TYPE_CHECKING:
    from git_client.plan.running import Plan

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
        # What a GitError shows as the command: push() doesn't say its own.
        command = ["push", remote, *(update.target for update in updates)]
        for node in nodes:
            error = push_failure(result, node._ref(), command)
            if error is None:
                refs = (ref for d in result.destinations for ref in d.refs)
                node._set(tuple(ref for ref in refs if ref.target == node._ref()))
            else:
                node._fail(error(command, result.returncode, result.stderr, result.stdout))

    @override
    def __repr__(self) -> str:
        return f"Push({self.remote!r}, {self.branch!r})"


def push_failure(result: PushResult, target: str, command: list[str]) -> type[GitError] | None:
    """The error for `target` in `result`, or None if it's pushed: PushRejectedError if git
    rejected it; else the error git's message says, unless every destination finished its
    report (see PushDestination.done) and pushed it, and git succeeded or rejected another
    ref (if not, a destination git didn't report failed: see push)."""
    reported = [ref for destination in result.destinations for ref in destination.refs]
    if any(ref.target == target and ref.status is PushStatus.REJECTED for ref in reported):
        return PushRejectedError
    every_destination = result.destinations and all(
        destination.done
        and any(ref.target == target and ref.status in _PUSHED for ref in destination.refs)
        for destination in result.destinations
    )
    explained = result.returncode == 0 or any(ref.status not in _PUSHED for ref in reported)
    if every_destination and explained:
        return None
    return error_for(result.stderr, result.stdout, command=command)
