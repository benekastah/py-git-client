"""What a repository's remotes have, and fetches from them."""

import enum
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field

from git_client.client import (
    CLIENT_TIMEOUT,
    OBJECT_ID,
    Arg,
    EnvValue,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    read_output,
)
from git_client.errors import GitError

# fetch's machine-readable output (git 2.41). A variable, so a test can have git refuse it,
# as an older git does.
_PORCELAIN = "--porcelain"

# What starts a line of fetch --porcelain's: a flag, then two ids (which may be malformed).
_PORCELAIN_LINE = re.compile(r". [0-9a-f]+ [0-9a-f]+(?: |$)")

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
    """What fetch did to a ref: git's porcelain flag. git prints UP_TO_DATE only with
    `--verbose`, which `fetch` doesn't pass: a ref already up to date isn't in its `refs`."""

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
    """What one fetch did: its exit status, captured output, and each ref git reported (each
    it changed or rejected: not one already up to date), or None on git older than 2.41, which
    can't report them for a program."""

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

    It fetches no submodules, whatever `fetch.recurseSubmodules` or `submodule.recurse` say,
    where `git fetch` by default fetches the commits a fetched superproject commit needs: git
    takes no `--recurse-submodules` with `--porcelain`. A tool that wants them fetches them
    itself (e.g. `git submodule update`, or a fetch in each submodule).

    It runs `git fetch --porcelain [options] -- <remote> <refspecs>`, so a `FakeGit` test
    responds to `["fetch", "--porcelain"]` with porcelain output. Its stdout is captured, so
    nothing shows while git fetches, and git prints no summary of its own (`From ...`, `[new
    branch]`). `--porcelain` is git 2.41's: an older git refuses it, and fetch runs again
    without it (but with `--no-recurse-submodules`, so it fetches the same), with `refs` None.
    Raises GitError for a porcelain line git doesn't document, and skips lines of other shapes
    (git prints some messages to stdout); launch errors and timeouts are raised as usual."""
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
        # As --porcelain does: so whatever the git, fetch fetches no submodules.
        args = ("fetch", "--no-recurse-submodules", *options, "--", remote, *specs)
        result = git.process(*args, capture_output=True, env=env, timeout=timeout)
        reported = None
    else:
        reported = _fetched(args, result)
    return FetchResult(result.returncode, decode(result.stdout), decode(result.stderr), reported)


def _fetched(
    args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]
) -> tuple[FetchRef, ...]:
    """The refs in `fetch --porcelain`'s output: a line for each, `<flag> <old id> <new id>
    <ref>`, an id of zeros for none. Other lines are skipped: git prints some messages to
    stdout among them, even with --porcelain (from 2.48, followRemoteHEAD's warning that the
    remote's HEAD isn't what `<remote>/HEAD` says)."""
    fetched: list[FetchRef] = []
    for line in decode(result.stdout).splitlines():
        if not _PORCELAIN_LINE.match(line):
            continue
        flag, rest = line[0], line[2:]
        fields = rest.split(" ")
        if (
            flag not in FetchStatus
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


@dataclass(frozen=True)
class RemoteRefs:
    """What `remote_refs` found, as `git ls-remote` lists it: `refs`, each ref's object id, by
    its full name (`HEAD` too, and an annotated tag's object as `refs/tags/<tag>^{}` beside
    the tag's own); `symrefs`, each symbolic ref's target, by name (asked for only)."""

    refs: dict[str, str]
    symrefs: dict[str, str] = field(default_factory=dict[str, str])


def remote_refs(
    git: ReadOnlyGit,
    remote: str,
    patterns: Iterable[str] = (),
    *,
    heads: bool = False,
    symrefs: bool = False,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> RemoteRefs:
    """The refs `remote` (a remote's name, a URL or a path) has, from one `git ls-remote`, which
    asks the remote, not the remote-tracking refs: each one, or those that match one of
    `patterns` (as ls-remote matches them: a pattern matches a ref whose name ends with it, at
    a `/`, so `main` matches refs/heads/main and refs/remotes/origin/main), only branches
    with `heads`, and each symbolic ref's target with `symrefs` (e.g. what HEAD points at).
    `env` is added to the environment git runs with (e.g. a config change by
    `GIT_CONFIG_COUNT`). A remote each: git asks one per process.

    Raises GitError if git fails (e.g. no such remote, or one it can't reach), or for output
    git doesn't document. A `FakeGit` test responds to `["ls-remote"]`."""
    switches = {"--heads": heads, "--symref": symrefs}
    args = ("ls-remote", *(switch for switch, on in switches.items() if on), "--", remote)
    args += tuple(patterns)
    output = decode(read_output(git, *args, env=env, timeout=timeout))
    found = RemoteRefs({})
    for line in output.splitlines():
        value, tab, name = line.partition("\t")
        symref = value.startswith("ref: ")
        target = value.removeprefix("ref: ")
        if not tab or not name or not (target if symref else OBJECT_ID.fullmatch(value)):
            error = GitError(args, 0, "", output)
            error.add_note("ls-remote printed what it can't read")
            raise error
        (found.symrefs if symref else found.refs)[name] = target
    return found


def remote_urls(
    git: ReadOnlyGit, remote: str, *, push: bool = False, timeout: Timeout = CLIENT_TIMEOUT
) -> list[str]:
    """The URLs git fetches `remote` (a remote's name) from, or with `push`, pushes it to, as
    git rewrites them (url.<base>.insteadOf, and for a push, url.<base>.pushInsteadOf), from
    one `git remote get-url --all [--push]`. git prints them a line each, as they are, so a
    URL with a newline in it reads as two. A remote with no URL gives its name, as git takes
    it for one (a path).

    Raises GitError if git fails, e.g. for a remote the repository's own config doesn't name:
    git ignores one set only in the global or system config, by `-c` or by the environment
    (which `fetch` and `remote_refs` take). A `FakeGit` test responds to `["remote",
    "get-url"]`."""
    args = ("remote", "get-url", "--all", *(["--push"] if push else []), "--", remote)
    return decode(read_output(git, *args, timeout=timeout)).splitlines()
