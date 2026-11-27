"""What a repository's remotes have, and fetches from them."""

import enum
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass

from git_client.client import (
    CLIENT_TIMEOUT,
    OBJECT_ID,
    Arg,
    EnvValue,
    Git,
    Timeout,
    decode,
)
from git_client.errors import GitError

# fetch's machine-readable output (git 2.41). A variable, so a test can have git refuse it,
# as an older git does.
_PORCELAIN = "--porcelain"

# git's exit status for a usage error: an option it doesn't know.
_USAGE = 129


@dataclass(frozen=True)
class FetchRefspec:
    """What `fetch` fetches: the remote's `source` (a ref, a pattern with `*`, or an object
    id), into the local ref `destination`, or, if None, only into FETCH_HEAD. Git makes only
    fast-forwards of `destination` (and its updates of tags) unless `force`.

    Raises ValueError for what git would read otherwise in the refspec `fetch` builds: a
    `source` that's empty, starts with `+` (it would force) or `^` (a negative refspec), or
    holds a `:` (it would be read as the destination), or a `destination` of `""`.
    """

    source: str
    destination: str | None = None
    _: KW_ONLY
    force: bool = False

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a refspec with an empty source fetches nothing")
        if self.source.startswith("+"):
            raise ValueError(f"source {self.source!r} would force: give force=True to force")
        if self.source.startswith("^"):
            raise ValueError(f"source {self.source!r} would be a negative refspec")
        if ":" in self.source:
            raise ValueError(f"source {self.source!r} holds ':', which ends a source")
        if self.destination == "":
            raise ValueError("an empty destination: give None to fetch only into FETCH_HEAD")


class FetchStatus(enum.StrEnum):
    """What fetch did to a ref: git's porcelain flag."""

    FAST_FORWARD = " "
    FORCED = "+"
    PRUNED = "-"
    TAG_UPDATE = "t"
    NEW = "*"
    REJECTED = "!"
    UP_TO_DATE = "="


@dataclass(frozen=True)
class FetchRef:
    """A local ref fetch reported: `ref` (`FETCH_HEAD` for what it wrote only there), what it
    did, and the ref's object before and after, None for none (a new ref's before, a pruned
    one's after). A rejected ref's `new_oid` is the object git didn't write."""

    ref: str
    status: FetchStatus
    old_oid: str | None
    new_oid: str | None


@dataclass(frozen=True)
class FetchResult:
    """What one fetch did: its exit status, captured output, and each ref git reported, or
    None on git older than 2.41, which can't report them for a program."""

    returncode: int
    stdout: str
    stderr: str
    refs: tuple[FetchRef, ...] | None


def fetch(
    git: Git,
    remote: str,
    refspecs: Iterable[FetchRefspec] = (),
    *,
    prune: bool = False,
    tags: bool | None = None,
    write_fetch_head: bool = True,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> FetchResult:
    """Fetch from `remote` (a remote's name, a URL or a path, `.` for this repository) with one
    `git fetch`: each refspec, or, given none, the remote's configured ones. Returns its exit
    status, output and each ref it reported, even if it failed: a rejected ref (e.g. one
    that isn't a fast-forward) makes it exit 1 though the others were fetched, and one that
    couldn't fetch at all (e.g. no such remote) exits 128, with `stderr` saying why. From a
    remote's name, git also updates the remote-tracking refs its config maps what's fetched to
    (e.g. refs/remotes/origin/a for its a), and reports them too.

    `prune` deletes remote-tracking refs the remote no longer has (`--prune`). `tags` True
    fetches every tag too (`--tags`), False none (`--no-tags`), and None as the remote's
    config says (by default, the tags that point into what was fetched).
    `write_fetch_head=False` leaves FETCH_HEAD as it was (`--no-write-fetch-head`), and a
    refspec with no destination then only fetches its objects.

    It runs `git fetch --porcelain [options] -- <remote> <refspecs>`, so a `FakeGit` test
    responds to `["fetch", "--porcelain"]` with porcelain output. `--porcelain` is git 2.41's:
    an older git refuses it, and fetch runs again without it, with `refs` None. Raises GitError
    for output git doesn't document; launch errors and timeouts are raised as usual."""
    specs = [
        f"{'+' if r.force else ''}{r.source}{'' if r.destination is None else ':'}"
        f"{r.destination or ''}"
        for r in refspecs
    ]
    switches = {
        "--prune": prune,
        "--tags": tags is True,
        "--no-tags": tags is False,
        "--no-write-fetch-head": not write_fetch_head,
    }
    options = [switch for switch, on in switches.items() if on]
    args: tuple[Arg, ...] = ("fetch", _PORCELAIN, *options, "--", remote, *specs)
    result = git.process(*args, capture_output=True, env=env, timeout=timeout)
    reported: tuple[FetchRef, ...] | None
    if result.returncode == _USAGE:
        args = ("fetch", *options, "--", remote, *specs)
        result = git.process(*args, capture_output=True, env=env, timeout=timeout)
        reported = None
    else:
        reported = _fetched(args, result)
    return FetchResult(result.returncode, decode(result.stdout), decode(result.stderr), reported)


def _fetched(
    args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]
) -> tuple[FetchRef, ...]:
    """The refs in `fetch --porcelain`'s output: a line for each, `<flag> <old id> <new id>
    <ref>`, an id of zeros for none."""
    fetched: list[FetchRef] = []
    for line in decode(result.stdout).splitlines():
        flag, space, rest = line[:1], line[1:2], line[2:]
        fields = rest.split(" ")
        if (
            space != " "
            or flag not in FetchStatus
            or len(fields) != 3
            or not all(OBJECT_ID.fullmatch(oid) for oid in fields[:2])
        ):
            error = GitError(args, result.returncode, decode(result.stderr), decode(result.stdout))
            error.add_note("fetch printed what it can't read")
            raise error
        old, new, ref = fields
        fetched.append(FetchRef(ref, FetchStatus(flag), _object(old), _object(new)))
    return tuple(fetched)


def _object(object_id: str) -> str | None:
    """An object id fetch printed, or None for its zeros, which mean none."""
    return None if object_id.strip("0") == "" else object_id
