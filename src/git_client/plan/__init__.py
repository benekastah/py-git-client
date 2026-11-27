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
order they were added in. To run fewer processes, a ready group may wait for a node of its
class that isn't ready yet (a push whose `force` is still to be read from config, say), but
only where that gives the same result as running the graph in order: each node declares the
resources it reads and writes (`Node.reads`, `Node.writes`; e.g. CONFIG, a remote's ref), and
a node never moves past one it conflicts with (one writes what the other reads or writes), nor
past an edge. A node that declares nothing conflicts with everything, so nothing moves past it.
Resources that may be the same without being equal (`Resource.overlaps`, e.g. two remotes'
remote-tracking refs) count as conflicting. A node whose group could give another result
than its members one by one (an atomic push: `Node.waits`) never waits.
"""

from git_client.plan.config import ConfigValue, TypedConfig
from git_client.plan.nodes import (
    CONFIG,
    Effect,
    ExecutionException,
    Map,
    Node,
    NotExecuted,
    PlanError,
    Resource,
    Skipped,
    TrackingRef,
    Value,
    lift,
)
from git_client.plan.pushing import Push
from git_client.plan.running import Graph, Plan

__all__ = [
    "CONFIG",
    "ConfigValue",
    "Effect",
    "ExecutionException",
    "Graph",
    "Map",
    "Node",
    "NotExecuted",
    "Plan",
    "PlanError",
    "Push",
    "Resource",
    "Skipped",
    "TrackingRef",
    "TypedConfig",
    "Value",
    "lift",
]
