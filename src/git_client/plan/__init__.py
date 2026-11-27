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
order they were added in. Each node declares the resources it reads and writes
(`Node.reads`, `Node.writes`: typed Resources such as CONFIG, a RemoteRef or a TrackingRef);
one that declares nothing may conflict with everything. To run fewer processes, a group of a
class that allows it (`Node.waits`: a non-atomic Push) may wait for a node of its class that
isn't ready yet (a push whose `force` is still to be read from config, say), so that they
share a process. The schedule (see Schedule) keeps the result what running the graph in
order gives: no node runs before a node of a lower level that may conflict with it, which
a node not ready yet (its resources unknown) always may; and nodes that may conflict never
run at once.
"""

from git_client.plan.config import ConfigValue, TypedConfig
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
from git_client.plan.pushing import Push
from git_client.plan.resources import (
    CONFIG,
    Access,
    Config,
    LocalRef,
    RemoteRef,
    Resource,
    TrackingRef,
)
from git_client.plan.running import Graph, Plan

__all__ = [
    "CONFIG",
    "Access",
    "Config",
    "ConfigValue",
    "Effect",
    "ExecutionException",
    "Graph",
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
    "lift",
]
