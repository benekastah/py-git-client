# So the checks fail if a type error a test expects (an ignore below) goes away:
# pyright: reportUnnecessaryTypeIgnoreComment=true
import graphlib
import threading
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self, assert_type, override

import pytest

import git_client.plan.pushing
from git_client import (
    Git,
    GitError,
    PushDestination,
    PushRef,
    PushRejectedError,
    PushResult,
    PushStatus,
    PushUpdate,
    RemoteError,
    push,
)
from git_client.plan import (
    CONFIG,
    ConfigValue,
    Effect,
    ExecutionException,
    Map,
    Node,
    NotExecuted,
    Plan,
    Push,
    Skipped,
    Value,
    lift,
)
from git_client.plan.pushing import push_failure
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
    assert value == Value("x")
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

    assert list(plan.graph()) == [top, left, right, base]
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


@dataclass(frozen=True)
class Echo(Node[str]):
    """Gives its input's value, or "" for None: a frozen node, so equal ones are one."""

    source: Node[str | None]

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.source,)

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
            node._set(node.source.get() or "")


def test_each_of_equal_nodes_gets_the_result(repo: Git) -> None:
    repo.run("config", "example.name", "example.value")
    repo.run("config", "example.value", "found")
    plan = Plan(repo)
    first = plan.add(ConfigValue(Echo(ConfigValue("example.name"))))
    inner = Echo(ConfigValue("example.name"))  # equal to first's key, but another object
    again = plan.add(ConfigValue(inner))

    plan.execute()

    assert (first.get(), again.get(), inner.get()) == ("found", "found", "example.value")


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
    def lane(self, plan: Plan) -> Hashable | None:
        self._ask("lane")
        return super().lane(plan)

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        self._ask("reads")
        return super().reads(plan)

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        self._ask("writes")
        return super().writes(plan)

    @override
    def waits(self, plan: Plan) -> bool:
        self._ask("waits")
        return super().waits(plan)


@pytest.mark.parametrize("hook", ["batch_key", "lane", "reads", "writes", "waits"])
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
    """Sets a config key: declares that it writes config."""

    def __init__(self, key: str, value: str, *, after: Iterable[Node[object]] = ()) -> None:
        self.key, self.value, self.after = key, value, list(after)

    @override
    def reads(self, plan: Plan) -> Iterable[Hashable]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Hashable]:
        return (CONFIG,)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node in nodes:
            plan.git.run("config", node.key, node.value)
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


def test_an_atomic_push_waits_for_no_later_one(remotes: Git, git_calls: list[GitCall]) -> None:
    remotes.run("branch", "-f", "a", "HEAD~1")
    remotes.run("branch", "c")
    git_calls.clear()
    plan = Plan(remotes, atomic_pushes=True)
    # Run in graph order, c's push isn't in a's, which git rejects: so c's mustn't wait.
    created = plan.add(Push("origin", "c"))
    rejected = plan.add(Push("origin", "a", force=ConfigValue("x.y").as_bool()))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    assert [call[-1] for call in pushes(git_calls)] == [
        "refs/heads/c:refs/heads/c",
        "refs/heads/a:refs/heads/a",
    ]
    assert raised.value.node is rejected
    assert [ref.status for ref in created.get()] == [PushStatus.CREATED]


def test_pushes_to_two_remotes_keep_their_order(
    remotes: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def recording_push(
        git: Git, remote: str, updates: Iterable[PushUpdate], *, atomic: bool
    ) -> PushResult:
        events.append(f"start {remote}")
        result = push(git, remote, updates, atomic=atomic)
        events.append(f"end {remote}")
        return result

    monkeypatch.setattr(git_client.plan.pushing, "push", recording_push)
    remotes.run("config", "example.remote", "mirror")
    plan = Plan(remotes)
    # Their remote-tracking refs may be the same (git maps them by each remote's fetch
    # refspecs), so the push to origin, the earlier, mustn't wait for the one to mirror.
    plan.add(Push("origin", "a"))
    plan.add(Push(ConfigValue("example.remote").or_default("origin"), "b"))

    plan.execute()

    assert events == ["start origin", "end origin", "start mirror", "end mirror"]


def test_pushes_to_two_remotes_run_at_once(
    remotes: Git, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    meeting = threading.Barrier(2, timeout=10)

    def meeting_push(
        git: Git, remote: str, updates: Iterable[PushUpdate], *, atomic: bool
    ) -> PushResult:
        meeting.wait()  # till the other push starts too
        return push(git, remote, updates, atomic=atomic)

    monkeypatch.setattr(git_client.plan.pushing, "push", meeting_push)
    remotes.run("config", "example.remote", "mirror")
    git_calls.clear()
    plan = Plan(remotes)
    # Both remotes come from config, so both pushes are ready in the same round.
    to_origin = plan.add(Push(ConfigValue("example.unset").or_default("origin"), "a"))
    to_mirror = plan.add(Push(ConfigValue("example.remote").or_default("origin"), "a"))

    plan.execute()

    assert sorted(pushes(git_calls)) == [
        [*PUSH, "--", remote, "refs/heads/a:refs/heads/a"] for remote in ("mirror", "origin")
    ]
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
