"""What a plan's nodes declare they read and write, so the plan knows which it may run
earlier (see Plan): typed, so that two names for one thing can't miss each other."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import override


@dataclass(frozen=True)
class Resource:
    """Something a node reads or writes. Two resources are the same if either `overlaps`
    the other: by default, if they're equal."""

    def overlaps(self, other: Resource) -> bool:
        """Whether `other` may be this resource, or share some of it."""
        return self == other


@dataclass(frozen=True)
class Config(Resource):
    """Git's config, all of it: CONFIG."""


CONFIG = Config()
"""What a node that reads or writes git's config declares."""


@dataclass(frozen=True)
class LocalRef(Resource):
    """A ref of the repository, by its full name (e.g. "refs/heads/main")."""

    ref: str


@dataclass(frozen=True)
class RemoteRef(Resource):
    """A ref at a remote (as a push names it: a name or a URL), by its full name there. It
    overlaps the same ref at any remote: a remote's name and its URL, or two URLs, may be
    one repository, which only git (or the server) could say."""

    remote: str
    ref: str

    @override
    def overlaps(self, other: Resource) -> bool:
        return isinstance(other, RemoteRef) and other.ref == self.ref


@dataclass(frozen=True)
class TrackingRef(Resource):
    """The remote-tracking ref git updates as it pushes `ref` to `remote`, which the remote's
    fetch refspecs say. It may be any local ref but a branch or tag: a plan assumes no
    fetch refspec maps a ref onto a branch or tag but one of the same name (a mirror's maps
    each onto itself), which a push leaves where it was. Pushes are taken not to conflict
    through it: two whose refspecs map onto one remote-tracking ref may leave either's
    value there."""

    remote: str
    ref: str

    @override
    def overlaps(self, other: Resource) -> bool:
        return isinstance(other, LocalRef) and not other.ref.startswith(
            ("refs/heads/", "refs/tags/")
        )


def overlap(these: Iterable[Resource], those: Iterable[Resource]) -> bool:
    """Whether any of these resources may be one of those."""
    those = list(those)
    return any(this.overlaps(that) or that.overlaps(this) for this in these for that in those)


@dataclass(frozen=True)
class Access:
    """What a node reads and writes."""

    reads: frozenset[Resource]
    writes: frozenset[Resource]

    def conflicts(self, other: Access) -> bool:
        """Whether one writes what the other reads or writes: their order matters."""
        return overlap(self.writes, other.reads | other.writes) or overlap(other.writes, self.reads)
