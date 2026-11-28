"""Nodes that read git's config, many in one process."""

# The plan and its nodes share their outcomes (_done, _error, _set...), private to the package.
# pyright: reportPrivateUsage=false

import re
from collections.abc import Callable, Generator, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast, override

from git_client.batch import CONFIG_LIST, config_entries
from git_client.client import Started, decode, error_from, stdout_of
from git_client.config_file import ConfigChange, ConfigResult
from git_client.plan.nodes import Effect, Map, Node, lift
from git_client.plan.resources import CONFIG, Resource

if TYPE_CHECKING:
    from git_client.plan.running import Plan


@dataclass(frozen=True, eq=False, init=False)
class ConfigValue(Node[str | None]):
    """A config key's value as git reads it (the last one, for a key with several), or None
    if it's not set. `key` is in the form git prints (`config()`'s): section and name in
    lower case. A key with no value (`[core] bare`) gives "". The ConfigValues that run at
    once are read by one `git config --list -z`, however many keys."""

    key: Node[str]

    def __init__(self, key: str | Node[str]) -> None:
        object.__setattr__(self, "key", lift(key))

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.key,)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return None

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return (CONFIG,)

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> Generator[None]:
        with Started(plan.git, *CONFIG_LIST) as started:
            yield
            entries = config_entries(stdout_of(CONFIG_LIST, started.result()))
        for node in nodes:
            values = entries.get(node.key.get())
            node._set((values[-1] or "") if values else None)

    def as_bool(self, default: bool = False) -> TypedConfig[bool]:
        """A node of the value as git reads a bool (`--type=bool`: "yes", "on", "1", no
        value...), or `default` if it's not set. It reads nothing until the plan runs."""
        return TypedConfig(self.key, "bool", default)

    def as_int(self, default: int = 0) -> TypedConfig[int]:
        """A node of the value as git reads an int (`--type=int`: "1k" is 1024), or
        `default`. It reads nothing until the plan runs."""
        return TypedConfig(self.key, "int", default)

    def or_default(self, default: str) -> Node[str]:
        """A node of the value, or `default` if it's not set."""

        def value_or_default(value: str | None) -> str:
            return default if value is None else value

        return Map(value_or_default, self)


# What git prints for each of TypedConfig's types, to the value.
_PARSERS: dict[str, Callable[[str], object]] = {"bool": lambda text: text == "true", "int": int}

# The characters special in a POSIX extended regular expression.
_ERE_SPECIAL = re.compile(r"[.\[\\()*+?{|^$]")


@dataclass(frozen=True, eq=False)
class TypedConfig[T](Node[T]):
    """A config key's value as git reads a `git_type` ("bool" or "int": `git config --type`),
    or `default` if it's not set: made by `ConfigValue.as_bool` and `as_int`. The
    TypedConfigs of one type that run at once are read by one `git config --type=<type>
    --get-regexp`, whose pattern matches their keys alone (in the form git prints, as
    ConfigValue's): git, not Python, reads the config syntax. A value of a key's that isn't
    one (a bool "maybe") fails the whole group."""

    key: Node[str]
    git_type: str
    default: T

    @override
    def inputs(self) -> Iterable[Node[object]]:
        return (self.key,)

    @override
    def batch_key(self, plan: Plan) -> Hashable:
        return self.git_type

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return (CONFIG,)

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> Generator[None]:
        keys = dict.fromkeys(node.key.get() for node in nodes)
        pattern = "|".join(_ERE_SPECIAL.sub(r"\\\g<0>", key) for key in keys)
        git_type = nodes[0].git_type
        args = ["config", "-z", f"--type={git_type}", "--get-regexp", f"^({pattern})$"]
        with Started(plan.git, *args) as started:
            yield
            result = started.result()
        if result.returncode not in (0, 1):  # 1: none of them is set
            raise error_from(args, result)
        typed: dict[str, str] = {}
        for entry in decode(result.stdout).split("\0"):
            key, _, value = entry.partition("\n")
            typed[key] = value  # the last one wins, as in `git config --get`
        for node in nodes:
            key = node.key.get()
            node._set(cast(T, _PARSERS[git_type](typed[key])) if key in typed else node.default)


class WriteConfig(Effect[ConfigResult]):
    """Make `change` (a ConfigSet or ConfigUnset) to the repository's config, as
    `Git.write_configs` does (one `git config`), giving what it did."""

    def __init__(self, change: ConfigChange, *, after: Iterable[Node[object]] = ()) -> None:
        self.change = change
        self.after = list(after)

    @override
    def reads(self, plan: Plan) -> Iterable[Resource]:
        return ()

    @override
    def writes(self, plan: Plan) -> Iterable[Resource]:
        return (CONFIG,)

    @override
    @classmethod
    def execute_group(cls, nodes: Sequence[Self], plan: Plan) -> None:
        for node, result in zip(
            nodes, plan.git.write_configs(*(node.change for node in nodes)), strict=True
        ):
            node._set(result)

    @override
    def __repr__(self) -> str:
        return f"WriteConfig({self.change!r})"
