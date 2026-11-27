# So the checks fail if a type error a test expects (an ignore below) goes away:
# pyright: reportUnnecessaryTypeIgnoreComment=true
import graphlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import assert_type

import pytest

import git_client.plan
from git_client import Arg, Git, GitError, PushRejectedError, PushResult, PushStatus, push
from git_client.plan import (
    ConfigValue,
    ExecutionException,
    Map,
    Node,
    NotExecuted,
    Plan,
    Push,
    Value,
    lift,
)
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
    with pytest.raises(NotExecuted, match="hasn't run"):
        dependent.get()
    assert independent.get() == "2"


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
    remotes.run("config", "example.push-with-force", "on")
    remotes.run("branch", "-f", "a", "HEAD~1")
    remotes.run("branch", "-f", "b", "HEAD~1")
    remotes.run("branch", "c")
    git_calls.clear()
    plan = Plan(remotes)
    force = ConfigValue("example.push-with-force").as_bool()
    forced, rejected, created = (
        plan.add(Push("origin", branch, force=force if branch == "a" else False))
        for branch in "abc"
    )
    dependent = plan.add(Push("origin", "c", after=[rejected]))

    with pytest.raises(ExecutionException) as raised:
        plan.execute()

    (push,) = pushes(git_calls)
    assert push[:5] == [*PUSH, "origin"]
    assert sorted(push[5:]) == [
        "+refs/heads/a:refs/heads/a",
        "refs/heads/b:refs/heads/b",
        "refs/heads/c:refs/heads/c",
    ]
    assert raised.value.node is rejected
    assert set(raised.value.group) == {forced, rejected, created}
    assert isinstance(raised.value.__cause__, PushRejectedError)
    assert [ref.status for ref in forced.get()] == [PushStatus.FORCED]
    assert [ref.status for ref in created.get()] == [PushStatus.CREATED]
    with pytest.raises(NotExecuted, match="hasn't run"):
        dependent.get()
    assert remote_tip(remotes, "origin", "a") == remotes.run("rev-parse", "HEAD~1")
    assert remote_tip(remotes, "origin", "b") == remotes.run("rev-parse", "HEAD")


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
    assert atomic[:6] == [*PUSH, "--atomic", "origin"]
    assert sorted(atomic[6:]) == ["+refs/heads/a:refs/heads/a", "refs/heads/b:refs/heads/b"]
    assert alone_push == [*PUSH, "origin", "refs/heads/main:refs/heads/main"]
    assert set(raised.value.failures) == {forced, rejected}
    assert remote_tip(remotes, "origin", "a") == remotes.run("rev-parse", "HEAD")
    assert [ref.status for ref in alone.get()] == [PushStatus.CREATED]


def test_pushes_to_two_remotes_run_at_once(
    remotes: Git, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    meeting = threading.Barrier(2, timeout=10)

    def meeting_push(git: Git, *args: Arg) -> PushResult:
        meeting.wait()  # till the other push starts too
        return push(git, *args)

    monkeypatch.setattr(git_client.plan, "push", meeting_push)
    remotes.run("config", "example.remote", "mirror")
    git_calls.clear()
    plan = Plan(remotes)
    to_origin = plan.add(Push("origin", "a"))
    to_mirror = plan.add(Push(ConfigValue("example.remote").or_default("origin"), "a"))

    plan.execute()

    assert sorted(pushes(git_calls)) == [
        [*PUSH, remote, "refs/heads/a:refs/heads/a"] for remote in ("mirror", "origin")
    ]
    assert [ref.status for ref in to_origin.get() + to_mirror.get()] == [PushStatus.UP_TO_DATE] * 2


def test_sequence_pushes_one_after_another(remotes: Git, git_calls: list[GitCall]) -> None:
    git_calls.clear()
    plan = Plan(remotes)
    first, second = plan.sequence(Push("origin", "a"), Push("origin", "b"))

    plan.execute()

    assert pushes(git_calls) == [
        [*PUSH, "origin", f"refs/heads/{branch}:refs/heads/{branch}"] for branch in "ab"
    ]
    assert second.after == [first]
