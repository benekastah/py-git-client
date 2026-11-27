"""A plan: git operations collected as a graph of nodes, then run in as few git processes as
git's own batch forms allow, the independent ones in parallel. A prototype: not exported from
`git_client` yet.

    plan = Plan(git)
    for branch in ("a", "b"):
        plan.add(Push("origin", branch, force=ConfigValue("example.force").as_bool()))
    plan.execute()

Each node's fields are nodes too (`lift` wraps a plain value in a `Value`), and a node reads
them only through `get()` once they've run. A group of nodes (of one class and
`batch_key`) runs once, by its class's `execute_group` (one git process, where git has a
batch form), which writes each member's result back onto the member.

The semantics are sequential: a plan gives what running its nodes one at a time in program
order gives (`Plan.order`: the ops in add() order, each after the nodes it reads, `after=`
ones too). To run fewer processes, the plan only runs nodes earlier than that, where it
can't change a result (see Plan), given what each node declares it reads and writes (typed
Resources: CONFIG, a LocalRef, a RemoteRef, a TrackingRef; a node that declares nothing
conflicts with everything):

- a read runs as early as its inputs allow, but never before an earlier write of what it
  reads (a config read waits for an earlier config write);
- pushes don't conflict with each other, so their order doesn't matter (see TrackingRef),
  but for two of one ref of one remote: the same push again is the first, unless a node
  between may change what it pushes (then it runs again, in turn), and two that differ
  with nothing between are a ConflictingPushes error;
- so a remote's pushes, once the reads they need have run, are one `git push`, and pushes to
  different remotes run at once.

A read is always git's: the plan doesn't decide in Python what git would answer (e.g. a
config read after the plan's own write, which an include may override).
"""

from git_client.plan.config import ConfigValue, TypedConfig, WriteConfig
from git_client.plan.nodes import (
    Effect,
    ExecutionException,
    Map,
    Node,
    NotExecuted,
    PlanError,
    Skipped,
    Value,
    lift,
)
from git_client.plan.pushing import ConflictingPushes, Push
from git_client.plan.resources import (
    CONFIG,
    Access,
    Config,
    LocalRef,
    RemoteRef,
    Resource,
    TrackingRef,
)
from git_client.plan.running import Plan

__all__ = [
    "CONFIG",
    "Access",
    "Config",
    "ConfigValue",
    "ConflictingPushes",
    "Effect",
    "ExecutionException",
    "LocalRef",
    "Map",
    "Node",
    "NotExecuted",
    "Plan",
    "PlanError",
    "Push",
    "RemoteRef",
    "Resource",
    "Skipped",
    "TrackingRef",
    "TypedConfig",
    "Value",
    "WriteConfig",
    "lift",
]
