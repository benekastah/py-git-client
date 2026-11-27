# So the checks fail if a type error a test expects (an ignore below) goes away:
# pyright: reportUnnecessaryTypeIgnoreComment=true
import graphlib
import random
import subprocess
import time
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self, assert_type, override

import pytest

from git_client import (
    Arg,
    ConfigSet,
    ConfigUnset,
    EnvValue,
    Git,
    GitError,
    GitTimeoutError,
    PushDestination,
    PushRef,
    PushRejectedError,
    PushResult,
    PushStatus,
    RemoteError,
)
from git_client.client import Started
from git_client.plan import (
    CONFIG,
    Access,
    ConfigValue,
    Effect,
    ExecutionException,
    LocalRef,
    Map,
    Node,
    NotExecuted,
    Plan,
    Push,
    RemoteRef,
    Resource,
    Skipped,
    TrackingRef,
    Value,
    WriteConfig,
    lift,
)
from git_client.plan.pushing import ConflictingPushes, push_failure
from git_client.testing import GitCall
from tests.conftest import Changes

PUSH = ["-c", "core.abbrev=no", "push", "--porcelain"]


def with_remotes(git: Git) -> None:
    """Bare remotes origin and mirror beside the repository, with branches a and b at main's
    second commit, as the repository has them."""
    git.run("commit", "--allow-empty", "-qm", "second")
    for remote in ("origin", "mirror"):
        git.run("init", "-q", "--bare", f"../{remote}.git")
        git.run("remote", "add", remote, f"../{remote}.git")
        git.run("push", "-q", remote, "main:a", "main:b")
    git.run("branch", "a")
    git.run("branch", "b")


@pytest.fixture
def remotes(repo_from: Callable[[str, Changes], Git]) -> Git:
    return repo_from("plan-remotes", with_remotes)


def remote_tip(git: Git, remote: str, branch: str) -> str:
    return Git(Path(git.directory).parent / f"{remote}.git").run("rev-parse", branch)


def pushes(calls: list[GitCall]) -> list[GitCall]:
    return [call for call in calls if call[:4] == PUSH]


def test_lift_wraps_a_value_and_keeps_a_node() -> None:
    value = lift("x")
    node = ConfigValue("example.key")

    assert_type(value, Node[str])
    assert isinstance(value, Value)
    assert value.get() == "x"
    assert lift(node) is node


def test_get_before_execute_says_so() -> None:
    with pytest.raises(NotExecuted, match="hasn't run"):
        Map(str, Value(1)).get()


def test_types_catch_a_node_of_the_wrong_type() -> None:
    # A config value may be unset (None): a remote needs a default.
    Push(ConfigValue("example.remote"), "a")  # pyright: ignore[reportArgumentType]
    Push(ConfigValue("example.remote").or_default("origin"), "a")
    flag: Node[bool] = ConfigValue("example.flag").as_bool()
    assert_type(flag.get, Callable[[], bool])


def test_graph_crawls_a_diamond_once_and_runs_it_in_order(repo: Git) -> None:
    ran: list[str] = []

    def record(name: str) -> Callable[..., str]:
        def run(*values: object) -> str:
            ran.append(name)
            return f"{name}{values}"

        return run

    base = Value(1)
    left, right = Map(record("left"), base), Map(record("right"), base)
    top = Map(record("top"), left, right)
    plan = Plan(repo)
    plan.add(top)

    assert plan.order() == [base, left, right, top]
    plan.execute()
    assert sorted(ran[:2]) == ["left", "right"]
    assert ran[2:] == ["top"]
    assert top.get() == "top('left(1,)', 'right(1,)')"


def test_a_cycle_runs_nothing(repo: Git, git_calls: list[GitCall]) -> None:
    first = Push("origin", "a")
    second = Push("origin", "b", after=[first])
    first.after.append(second)
    plan = Plan(repo)
    plan.add(first)

    with pytest.raises(graphlib.CycleError):
        plan.execute()
    assert git_calls == []


def test_a_failure_names_its_node_and_stops_its_dependents(repo: Git) -> None:
    def fail() -> int:
        raise ZeroDivisionError

    failing = Map(fail)
    dependent = Map(str, failing)
    independent = Map(str, Value(2))
    plan = Plan(repo)
    plan.add(dependent)
    plan.add(independent)

    with pytest.raises(ExecutionException) as raised:
        plan.execute()
    assert raised.value.node is failing
    assert raised.value.group == (failing,)
    assert isinstance(raised.value.__cause__, ZeroDivisionError)
    with pytest.raises(NotExecuted, match="failed"):
        failing.get()
    with pytest.raises(Skipped) as skipped:
        dependent.get()
    assert skipped.value.failed is failing
    assert isinstance(skipped.value.__cause__, ZeroDivisionError)
    assert independent.get() == "2"


def test_a_node_skipped_further_down_names_the_node_that_failed(repo: Git) -> None:
    def fail() -> int:
        raise ZeroDivisionError

    failing = Map(fail)
    further = Map(str, Map(str, failing))
    plan = Plan(repo)
    plan.add(further)

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert set(raised.value.failures) == {failing}
    with pytest.raises(Skipped, match="failed") as skipped:
        further.get()
    assert skipped.value.failed is failing


def test_a_plan_runs_again_what_failed(repo: Git) -> None:
    runs: list[int] = []

    def fail_once() -> int:
        runs.append(len(runs))
        if len(runs) == 1:
            raise ZeroDivisionError
        return len(runs)

    flaky = Map(fail_once)
    dependent = Map(str, flaky)
    plan = Plan(repo)
    plan.add(dependent)
    with pytest.raises(ExecutionException):
        plan.execute()

    plan.execute()

    assert (flaky.get(), dependent.get()) == (2, "2")


class Broken(Map[int]):
    """A Map whose `hook` raises when the plan asks it."""

    def __init__(self, hook: str) -> None:
        super().__init__(int)
        self.hook = hook

    def _ask(self, name: str) -> None:
        if name == self.hook:
            raise ZeroDivisionError(name)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        self._ask("batch_key")
        return super().batch_key(plan)

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        self._ask("reads")
        return super().reads(plan)

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        self._ask("writes")
        return super().writes(plan)


@pytest.mark.parametrize("hook", ["batch_key", "reads", "writes"])
def test_a_node_whose_question_fails_fails_itself(repo: Git, hook: str) -> None:
    broken = Broken(hook)
    plan = Plan(repo)
    plan.add(broken)
    fine = plan.add(Map(str, Value(1)))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert raised.value.node is broken
    assert isinstance(raised.value.__cause__, ZeroDivisionError)
    assert fine.get() == "1"


def test_config_values_are_read_by_one_process(repo: Git, git_calls: list[GitCall]) -> None:
    repo.run("config", "example.key", "first")
    repo.run("config", "--add", "example.key", "last")
    git_calls.clear()
    plan = Plan(repo)
    one, again = plan.add(ConfigValue("example.key")), plan.add(ConfigValue("example.key"))
    unset = plan.add(ConfigValue("example.unset"))

    plan.execute()

    assert git_calls == [["config", "--list", "-z"]]
    assert (one.get(), again.get(), unset.get()) == ("last", "last", None)


def test_as_bool_is_a_node_that_reads_when_the_plan_runs(
    repo: Git, git_calls: list[GitCall]
) -> None:
    repo.run("config", "example.flag", "yes")
    git_calls.clear()
    flag = ConfigValue("example.flag").as_bool()

    assert git_calls == []
    with pytest.raises(NotExecuted):
        flag.get()
    plan = Plan(repo)
    plan.add(flag)
    plan.execute()
    assert flag.get() is True


def test_git_reads_typed_config_with_one_process_per_type(
    repo: Git, git_calls: list[GitCall]
) -> None:
    for name, value in [("yes", "yes"), ("on", "on"), ("one", "1"), ("true", "true")]:
        repo.run("config", f"example.{name}", value)
    repo.run("config", "example.off", "off")
    repo.run("config", "example.size", "1k")
    git_calls.clear()
    plan = Plan(repo)
    truths = [
        plan.add(ConfigValue(f"example.{name}").as_bool())
        for name in ("yes", "on", "one", "true", "yes")
    ]
    off = plan.add(ConfigValue("example.off").as_bool(default=True))
    unset = plan.add(ConfigValue("example.unset").as_bool(default=True))
    size = plan.add(ConfigValue("example.size").as_int())

    plan.execute()

    assert [truth.get() for truth in truths] == [True] * 5
    assert (off.get(), unset.get(), size.get()) == (False, True, 1024)
    bools, ints = sorted(git_calls, key=lambda call: call[2])
    assert bools[:4] == ["config", "-z", "--type=bool", "--get-regexp"]
    assert bools[4].count(r"example\.yes") == 1
    assert ints == ["config", "-z", "--type=int", "--get-regexp", r"^(example\.size)$"]


def test_a_value_git_cant_type_fails_its_group(repo: Git) -> None:
    repo.run("config", "example.flag", "maybe")
    plan = Plan(repo)
    flag = plan.add(ConfigValue("example.flag").as_bool())

    with pytest.raises(ExecutionException) as raised:
        plan.execute()
    assert raised.value.node is flag
    assert isinstance(raised.value.__cause__, GitError)


def test_pushes_to_a_remote_are_one_push_each_branch_alone_and_forced_as_asked(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    remotes.run("branch", "-f", "a", "HEAD~1")
    remotes.run("branch", "-f", "b", "HEAD~1")
    remotes.run("branch", "c")
    git_calls.clear()
    plan = Plan(remotes)
    forced, rejected, created = (
        plan.add(Push("origin", branch, force=branch == "a")) for branch in "abc"
    )
    dependent = plan.add(Push("origin", "c", after=[rejected]))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    (push,) = pushes(git_calls)
    assert push[:6] == [*PUSH, "--", "origin"]
    assert sorted(push[6:]) == [
        "+refs/heads/a:refs/heads/a",
        "refs/heads/b:refs/heads/b",
        "refs/heads/c:refs/heads/c",
    ]
    assert raised.value.node is rejected
    assert set(raised.value.group) == {forced, rejected, created}
    assert isinstance(raised.value.__cause__, PushRejectedError)
    assert [ref.status for ref in forced.get()] == [PushStatus.FORCED]
    assert [ref.status for ref in created.get()] == [PushStatus.CREATED]
    with pytest.raises(Skipped):
        dependent.get()
    assert remote_tip(remotes, "origin", "a") == remotes.run("rev-parse", "HEAD~1")
    assert remote_tip(remotes, "origin", "b") == remotes.run("rev-parse", "HEAD")


def test_a_push_waits_for_another_to_the_same_remote_past_reads(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    remotes.run("config", "example.push-with-force", "on")
    remotes.run("branch", "-f", "a", "HEAD~1")
    git_calls.clear()
    plan = Plan(remotes)
    # b is ready at once; a once its config is read, which no push conflicts with.
    forced = plan.add(Push("origin", "a", force=ConfigValue("example.push-with-force").as_bool()))
    plain = plan.add(Push("origin", "b", force=False))

    plan.execute()

    (push,) = pushes(git_calls)
    assert push[4:6] == ["--", "origin"]
    assert sorted(push[6:]) == ["+refs/heads/a:refs/heads/a", "refs/heads/b:refs/heads/b"]
    assert [ref.status for ref in forced.get() + plain.get()] == [
        PushStatus.FORCED,
        PushStatus.UP_TO_DATE,
    ]
    assert remote_tip(remotes, "origin", "a") == remotes.run("rev-parse", "HEAD~1")


class SetConfig(Effect[None]):
    """Sets a config key, noting in `events` when it starts and ends: declares that it
    writes config."""

    def __init__(
        self,
        key: str,
        value: str,
        *,
        after: Iterable[Node[object]] = (),
        events: list[str] | None = None,
    ) -> None:
        self.key, self.value, self.after = key, value, list(after)
        self.events = [] if events is None else events

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return (CONFIG,)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            node.events.append("start write")
            time.sleep(0.05)  # so a read started meanwhile would show
            plan.git.run("config", node.key, node.value)
            node.events.append("end write")
            node._set(None)


def test_a_push_waits_for_no_node_that_writes_what_it_reads(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    remotes.run("branch", "c")
    remotes.run("branch", "d")
    git_calls.clear()
    plan = Plan(remotes)
    early = plan.add(Push("origin", "c"))
    # Changes where origin pushes to, so c must push before it, as the graph has it.
    moving = SetConfig("remote.origin.pushurl", "../mirror.git", after=[ConfigValue("x.y")])
    late = plan.add(Push("origin", "d", force=ConfigValue("x.z").as_bool(), after=[moving]))

    plan.execute()

    assert [call[-1] for call in git_calls if call[:4] == PUSH or call[0] == "config"][-3:] == [
        "refs/heads/c:refs/heads/c",
        "../mirror.git",
        "refs/heads/d:refs/heads/d",
    ]
    assert [ref.status for ref in early.get() + late.get()] == [PushStatus.CREATED] * 2
    assert remote_tip(remotes, "origin", "c") == remote_tip(remotes, "mirror", "d")


class Undeclared(Node[None]):
    """Runs `git version` after `input`, declaring nothing of what it reads or writes."""

    def __init__(self, input: Node[object]) -> None:
        self.input = input

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.input,)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            plan.git.run("version")
            node._set(None)


def test_nothing_moves_past_a_node_that_declares_nothing(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    git_calls.clear()
    plan = Plan(remotes)
    plan.add(Push("origin", "a"))
    plan.add(Undeclared(ConfigValue("x.y")))
    later_force = Map(bool, ConfigValue("x.z").as_bool())
    plan.add(Push("origin", "b", force=later_force))

    plan.execute()

    assert [call[-1] for call in git_calls if call[:4] == PUSH or call == ["version"]] == [
        "refs/heads/a:refs/heads/a",
        "version",
        "refs/heads/b:refs/heads/b",
    ]


def test_an_atomic_push_is_all_or_nothing(remotes: Git, git_calls: list[GitCall]) -> None:
    remotes.run("branch", "-f", "a", "HEAD~1")
    remotes.run("branch", "-f", "b", "HEAD~1")
    git_calls.clear()
    plan = Plan(remotes, atomic_pushes=True)
    forced = plan.add(Push("origin", "a", force=True))
    rejected = plan.add(Push("origin", "b"))
    alone = plan.add(Push("origin", "main", atomic=False))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    atomic, alone_push = sorted(pushes(git_calls), key=lambda call: "--atomic" not in call)
    assert atomic[:7] == [*PUSH, "--atomic", "--", "origin"]
    assert sorted(atomic[7:]) == ["+refs/heads/a:refs/heads/a", "refs/heads/b:refs/heads/b"]
    assert alone_push == [*PUSH, "--", "origin", "refs/heads/main:refs/heads/main"]
    assert set(raised.value.failures) == {forced, rejected}
    assert remote_tip(remotes, "origin", "a") == remotes.run("rev-parse", "HEAD")
    assert [ref.status for ref in alone.get()] == [PushStatus.CREATED]


def test_a_push_with_a_lease_fails_if_the_remote_moved(remotes: Git) -> None:
    remotes.run("branch", "-f", "a", "HEAD~1")
    plan = Plan(remotes)
    stale = plan.add(Push("origin", "a", lease=remotes.run("rev-parse", "HEAD~1")))
    leased = plan.add(Push("origin", "b", lease=remotes.run("rev-parse", "HEAD")))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert raised.value.node is stale
    assert isinstance(raised.value.__cause__, PushRejectedError)
    assert [ref.status for ref in leased.get()] == [PushStatus.UP_TO_DATE]


def test_a_config_write_and_read_run_one_at_a_time(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    collecting(events, monkeypatch)
    plan = Plan(StartingGit(repo.directory, events))
    plan.add(SetConfig("example.key", "new", events=events))
    read = plan.add(ConfigValue("example.key"))

    plan.execute()

    assert events == ["start write", "end write", "start --list", "collect"]
    assert read.get() == "new"


def test_a_read_waits_for_an_earlier_write_that_isnt_ready_yet(remotes: Git) -> None:
    # A review's example: the config write waits for push a, and the read added after it
    # mustn't run first, though the write isn't ready when the read is.
    remotes.run("config", "example.k", "old")
    plan = Plan(remotes)
    pushed = plan.add(Push("origin", "a"))
    plan.add(SetConfig("example.k", "new", after=[pushed]))
    read = plan.add(ConfigValue(Map(str, Map(str, Value("example.k")))))
    force = Map(bool, Map(bool, Map(bool, ConfigValue("x.z").as_bool())))
    plan.add(Push("origin", "b", force=force))

    plan.execute()

    assert read.get() == "new"


def test_a_ready_map_runs_at_once(repo: Git) -> None:
    ran: list[str] = []

    def record(name: str) -> Callable[[object], str]:
        def run(value: object) -> str:
            ran.append(name)
            return str(value)

        return run

    plan = Plan(repo)
    plan.add(Map(record("early"), Value(1)))
    plan.add(Map(record("late"), Map(record("middle"), Map(record("first"), ConfigValue("x.y")))))

    plan.execute()

    assert ran == ["early", "first", "middle", "late"]


def test_identical_pushes_are_one(remotes: Git, git_calls: list[GitCall]) -> None:
    remotes.run("branch", "c")
    git_calls.clear()
    plan = Plan(remotes)
    first, again = plan.add(Push("origin", "c")), plan.add(Push("origin", "c"))

    plan.execute()

    assert len(pushes(git_calls)) == 1
    assert first.get() == again.get()
    assert [ref.status for ref in again.get()] == [PushStatus.CREATED]


def test_a_ref_pushed_twice_otherwise_is_a_plan_error(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    git_calls.clear()
    plan = Plan(remotes)
    first, forced = plan.add(Push("origin", "a")), plan.add(Push("origin", "a", force=True))

    with pytest.raises(ConflictingPushes) as raised:
        plan.execute()

    assert (raised.value.first, raised.value.second) == (first, forced)
    assert git_calls == []


def test_a_ref_pushed_twice_otherwise_as_config_says_fails_the_second(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    git_calls.clear()
    plan = Plan(remotes)
    first = plan.add(Push("origin", "a"))
    forced = plan.add(Push("origin", "a", force=ConfigValue("x.y").as_bool(default=True)))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert raised.value.node is forced
    assert isinstance(raised.value.__cause__, ConflictingPushes)
    assert raised.value.__cause__.first is first
    assert [ref.status for ref in first.get()] == [PushStatus.UP_TO_DATE]
    assert len(pushes(git_calls)) == 1


class MoveRef(Effect[None]):
    """Points a ref of the repository at `to`: declares that it writes it."""

    def __init__(self, ref: str, to: str) -> None:
        self.ref, self.to, self.after = ref, to, []

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return (LocalRef(self.ref),)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            plan.git.run("update-ref", node.ref, node.to)
            node._set(None)


def test_a_ref_moved_between_two_pushes_is_pushed_twice(
    remotes: Git, git_calls: list[GitCall]
) -> None:
    remotes.run("branch", "c", "HEAD~1")
    git_calls.clear()
    plan = Plan(remotes)
    first = plan.add(Push("origin", "c"))
    plan.add(MoveRef("refs/heads/c", remotes.run("rev-parse", "HEAD")))
    again = plan.add(Push("origin", "c"))

    plan.execute()

    assert len(pushes(git_calls)) == 2
    assert [ref.status for ref in first.get() + again.get()] == [
        PushStatus.CREATED,
        PushStatus.FAST_FORWARD,
    ]


@dataclass(frozen=True)
class Cell(Resource):
    """A cell of a test's Cells."""

    name: str


class Put(Effect[int]):
    """Sets a cell to its source's value plus one; puts run as one group."""

    def __init__(
        self, cells: dict[str, int], name: str, source: Node[int], after: list[Node[object]]
    ) -> None:
        self.cells, self.name, self.source, self.after = cells, name, source, after

    @override
    def _reads(self) -> Iterable[Node[object]]:
        return (self.source,)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return "put"

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return (Cell(self.name),)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            node.cells[node.name] = node.source.get() + 1
            node._set(node.cells[node.name])


class Take(Effect[int]):
    """A cell's value; takes run as one group."""

    def __init__(self, cells: dict[str, int], name: str, after: list[Node[object]]) -> None:
        self.cells, self.name, self.after = cells, name, after

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return "take"

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return (Cell(self.name),)

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            node._set(node.cells.get(node.name, 0))


class Barrier(Effect[int]):
    """Declares nothing, so it may conflict with everything: 0."""

    def __init__(self, after: list[Node[object]]) -> None:
        self.after = after

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            node._set(0)


def random_plan(seed: int, git: Git, reorder: bool) -> tuple[Plan, list[Node[int]], dict[str, int]]:
    """A plan of random puts, takes and barriers, added in a random order: the same for a
    seed."""
    rng = random.Random(seed)
    cells: dict[str, int] = {}
    nodes: list[Node[int]] = []
    for _ in range(10):
        name = rng.choice("xy")
        after: list[Node[object]] = [node for node in nodes if rng.random() < 0.2]
        kind = rng.random()
        if kind < 0.45:
            source = rng.choice([*nodes, Value(rng.randrange(10))])
            nodes.append(Put(cells, name, source, after))
        elif kind < 0.9:
            nodes.append(Take(cells, name, after))
        else:
            nodes.append(Barrier(after))
    plan = Plan(git, reorder=reorder)
    for node in rng.sample(nodes, len(nodes)):
        plan.add(node)
    return plan, nodes, cells


def test_reordering_gives_what_program_order_gives(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups: list[str] = []
    for cls in (Put, Take):
        run = cls.execute_group

        def counting(
            nodes: Sequence[Node[int]], plan: Plan, run: Callable[..., None] = run
        ) -> None:
            groups.append("")
            run(nodes, plan)

        monkeypatch.setattr(cls, "execute_group", counting)
    fewer = 0
    for seed in range(300):
        reference, reference_nodes, reference_cells = random_plan(seed, repo, reorder=False)
        groups.clear()
        reference.execute()
        in_order = len(groups)
        plan, nodes, cells = random_plan(seed, repo, reorder=True)
        groups.clear()
        plan.execute()

        assert [node.get() for node in nodes] == [node.get() for node in reference_nodes], seed
        assert cells == reference_cells, seed
        fewer += len(groups) < in_order
    assert fewer  # reordering merged groups in some


def test_a_read_sees_the_writes_added_before_it_and_none_after(repo: Git) -> None:
    repo.run("config", "example.k", "old")
    plan = Plan(repo)
    before = plan.add(ConfigValue("example.k"))
    plan.add(SetConfig("example.k", "new"))
    after = plan.add(ConfigValue("example.k"))

    plan.execute()

    assert (before.get(), after.get()) == ("old", "new")


def test_pushes_to_two_remotes_run_at_once(remotes: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    collecting(events, monkeypatch)
    remotes.run("config", "example.remote", "mirror")
    plan = Plan(StartingGit(remotes.directory, events))
    # The second's remote is read first: reads run before the pushes, which then run together.
    to_origin = plan.add(Push("origin", "a"))
    to_mirror = plan.add(Push(ConfigValue("example.remote").or_default("origin"), "a"))

    plan.execute()

    assert events[-4:] == ["start origin", "start mirror", "collect", "collect"]
    assert [ref.status for ref in to_origin.get() + to_mirror.get()] == [PushStatus.UP_TO_DATE] * 2


def test_sequence_pushes_one_after_another(remotes: Git, git_calls: list[GitCall]) -> None:
    git_calls.clear()
    plan = Plan(remotes)
    first, second = plan.sequence(Push("origin", "a"), Push("origin", "b"))

    plan.execute()

    assert pushes(git_calls) == [
        [*PUSH, "--", "origin", f"refs/heads/{branch}:refs/heads/{branch}"] for branch in "ab"
    ]
    assert second.after == [first]


def test_a_push_to_a_remote_that_doesnt_exist_fails_every_branch(remotes: Git) -> None:
    plan = Plan(remotes)
    first, second = plan.add(Push("nowhere", "a")), plan.add(Push("nowhere", "b"))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert set(raised.value.failures) == {first, second}
    assert all(isinstance(error, RemoteError) for error in raised.value.failures.values())


def test_a_push_that_failed_with_nothing_rejected_fails_its_branch(remotes: Git) -> None:
    # Git stops at the push URL it can't reach, having pushed to the first: every ref it
    # reported is accepted, yet the push failed.
    remotes.run("config", "--add", "remote.origin.pushurl", "../origin.git")
    remotes.run("config", "--add", "remote.origin.pushurl", "../missing.git")
    remotes.run("branch", "c")
    plan = Plan(remotes)
    branch = plan.add(Push("origin", "c"))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert raised.value.node is branch
    assert isinstance(raised.value.__cause__, RemoteError)
    assert remote_tip(remotes, "origin", "c") == remotes.run("rev-parse", "c")


def test_a_branch_a_destination_didnt_finish_reporting_isnt_pushed() -> None:
    # Real git rejects every ref of a destination that failed outright (e.g. an unpacker
    # error), so this report is made by hand: one that ends early, with the branch accepted.
    accepted = PushRef("refs/heads/a", "refs/heads/a", PushStatus.CREATED, "[new]", None, None)
    rejected = PushRef("refs/heads/b", "refs/heads/b", PushStatus.REJECTED, "[x]", None, None)
    report = PushResult(1, "", "", (PushDestination("A", (accepted, rejected), done=False),))

    assert push_failure(report, "refs/heads/a", ["push"]) is GitError


@pytest.mark.parametrize(
    "destinations",
    [
        (),
        (
            PushDestination(
                "A", (PushRef("b", "refs/heads/b", PushStatus.CREATED, "", None, None),), done=True
            ),
        ),
    ],
    ids=["no destination", "another ref"],
)
def test_a_branch_git_didnt_report_isnt_pushed(destinations: tuple[PushDestination, ...]) -> None:
    # Made by hand: real git reports every ref it was given, or fails.
    report = PushResult(0, "", "", destinations)

    assert push_failure(report, "refs/heads/a", ["push"]) is GitError


def test_typed_config_reads_the_last_value_of_a_key(repo: Git) -> None:
    repo.run("config", "example.flag", "false")
    repo.run("config", "--add", "example.flag", "true")
    plan = Plan(repo)
    flag = plan.add(ConfigValue("example.flag").as_bool())

    plan.execute()

    assert flag.get() is True


def test_what_a_push_writes_conflicts_with() -> None:
    tracking = TrackingRef("origin", "refs/heads/a")
    pushing = Access(frozenset(), frozenset({RemoteRef("origin", "refs/heads/a"), tracking}))

    # Pushes don't conflict, but for one ref of one remote.
    assert not pushing.conflicts(
        Access(frozenset(), frozenset({RemoteRef("origin", "refs/heads/b"), tracking}))
    )
    assert not pushing.conflicts(
        Access(frozenset(), frozenset({RemoteRef("mirror", "refs/heads/a")}))
    )
    assert pushing.conflicts(Access(frozenset(), frozenset({RemoteRef("origin", "refs/heads/a")})))
    # A remote-tracking ref may be any local ref but a branch or tag.
    assert pushing.conflicts(Access(frozenset({LocalRef("refs/remotes/origin/a")}), frozenset()))
    assert not pushing.conflicts(
        Access(frozenset({LocalRef("refs/heads/a"), LocalRef("refs/tags/a"), CONFIG}), frozenset())
    )


class StartingGit(Git):
    """A Git that notes each process it starts, and keeps them."""

    def __init__(self, directory: str, events: list[str], timeout: float | None = None) -> None:
        super().__init__(directory, timeout=timeout)
        self.events = events
        self.started: list[subprocess.Popen[bytes]] = []

    @override
    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        process = super().popen(*args, env=env, stderr=stderr)
        self.events.append(f"start {args[-2]}")
        self.started.append(process)
        return process


def collecting(events: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Note in `events` each process the plan collects."""
    result = Started.result

    def noting(self: Started) -> subprocess.CompletedProcess[bytes]:
        events.append("collect")
        return result(self)

    monkeypatch.setattr(Started, "result", noting)


def test_independent_groups_start_together_and_are_collected_in_turn(
    remotes: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    collecting(events, monkeypatch)
    plan = Plan(StartingGit(remotes.directory, events))
    to_origin, to_mirror = plan.add(Push("origin", "a")), plan.add(Push("mirror", "a"))

    plan.execute()

    assert events == ["start origin", "start mirror", "collect", "collect"]
    assert [ref.status for ref in to_origin.get() + to_mirror.get()] == [PushStatus.UP_TO_DATE] * 2


def slow_pushes(git: Git) -> None:
    """Make each push of `git` wait, in its pre-push hook, far longer than a test."""
    hook = Path(git.directory) / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    hook.chmod(0o755)


def test_every_started_process_past_its_timeout_fails_its_node(remotes: Git) -> None:
    slow_pushes(remotes)
    events: list[str] = []
    plan = Plan(StartingGit(remotes.directory, events, timeout=0.5))
    first, second = plan.add(Push("origin", "a")), plan.add(Push("mirror", "a"))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert set(raised.value.failures) == {first, second}
    assert all(isinstance(e, GitTimeoutError) for e in raised.value.failures.values())


def test_an_interrupt_stops_every_started_process(
    remotes: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow_pushes(remotes)

    def interrupted(self: Started) -> subprocess.CompletedProcess[bytes]:
        raise KeyboardInterrupt

    monkeypatch.setattr(Started, "result", interrupted)
    git = StartingGit(remotes.directory, [])
    plan = Plan(git)
    plan.add(Push("origin", "a"))
    plan.add(Push("mirror", "a"))

    with pytest.raises(KeyboardInterrupt):
        plan.execute()

    assert len(git.started) == 2
    assert all(process.poll() is not None for process in git.started)


@pytest.mark.parametrize("reorder", [True, False])
def test_a_read_of_a_key_just_set_is_what_was_set(
    repo: Git, git_calls: list[GitCall], reorder: bool
) -> None:
    git_calls.clear()
    plan = Plan(repo, reorder=reorder)
    plan.add(WriteConfig(ConfigSet("example.k", "new")))
    plan.add(WriteConfig(ConfigSet("example.other", "x")))
    read = plan.add(ConfigValue("example.k"))

    plan.execute()

    assert read.get() == "new"
    reads = [call for call in git_calls if call == ["config", "--list", "-z"]]
    assert len(reads) == (0 if reorder else 1)  # program order reads it; reordering knows it


def test_a_typed_read_of_a_key_just_set_asks_git(repo: Git, git_calls: list[GitCall]) -> None:
    git_calls.clear()
    plan = Plan(repo)
    plan.add(WriteConfig(ConfigSet("example.flag", "on")))
    flag = plan.add(ConfigValue("example.flag").as_bool())

    plan.execute()

    assert flag.get() is True
    assert [call[2] for call in git_calls if call[:2] == ["config", "-z"]] == ["--type=bool"]


def test_a_read_of_a_key_just_unset_asks_git(repo: Git, git_calls: list[GitCall]) -> None:
    repo.run("config", "--global", "example.k", "global")
    git_calls.clear()
    plan = Plan(repo)
    plan.add(WriteConfig(ConfigSet("example.k", "new")))
    plan.add(WriteConfig(ConfigUnset("example.k")))
    read = plan.add(ConfigValue("example.k"))

    plan.execute()

    assert read.get() == "global"
    assert ["config", "--list", "-z"] in git_calls


def test_a_read_of_a_key_whose_set_failed_asks_git(repo: Git) -> None:
    repo.run("config", "example.k", "one")
    repo.run("config", "--add", "example.k", "two")  # git refuses to set a key with two
    plan = Plan(repo)
    write = plan.add(WriteConfig(ConfigSet("example.k", "new")))
    read = plan.add(ConfigValue("example.k"))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert raised.value.node is write
    assert read.get() == "two"
