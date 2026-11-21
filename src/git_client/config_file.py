"""The changes `Git.write_config` makes to a repository's config, and what each did."""

import enum
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSet:
    """For `Git.write_config`: set `key` to `value`, as `git config <key> <value>`."""

    key: str
    value: str


@dataclass(frozen=True)
class ConfigUnset:
    """For `Git.write_config`: remove `key`, as `git config --unset <key>`; with a
    `value`, only that value of it, as `git config --unset --fixed-value <key> <value>` (for
    a key with several values)."""

    key: str
    value: str | None = None


type ConfigChange = ConfigSet | ConfigUnset


class ConfigResult(enum.Enum):
    """What one change of `Git.write_config` did."""

    CHANGED = "changed"
    """It was made. A set always is, even to the value the key had, which git writes again."""
    NOT_FOUND = "not found"
    """An unset of a key, or of a value, that isn't set: nothing to remove."""
    SEVERAL_VALUES = "several values"
    """An unset of a key with several values (or of a value it has more than once), which git
    refuses, leaving them all: give the value to remove."""
