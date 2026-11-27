from collections.abc import Callable
from typing import assert_type

import pytest

from git_client import Git, GitError
from git_client.plan import (
    ConfigValue,
    ExecutionException,
    Map,
    Node,
    NotExecuted,
    Plan,
    Value,
    lift,
)
from git_client.testing import GitCall


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
