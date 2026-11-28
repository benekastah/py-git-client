"""What git says of a repository's worktrees (one's status, and the list of them), and adding
and removing them."""

import os
from collections.abc import Iterator
from dataclasses import KW_ONLY, dataclass
from itertools import takewhile
from pathlib import Path
from typing import Literal

from git_client.client import (
    CLIENT_TIMEOUT,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    read_output,
    stdout_of,
)
from git_client.errors import GitError
from git_client.performance import record_read


@dataclass(frozen=True)
class SubmoduleState:
    """What changed in a submodule, from git's `S<c><m><u>`: whether its commit isn't the one
    recorded, whether its tracked files changed, and whether it has untracked files."""

    commit_changed: bool
    modified: bool
    untracked: bool


@dataclass(frozen=True)
class Changed:
    """A tracked path that changed: its one-letter status in the index (against HEAD) and in
    the worktree (against the index), as git's short status prints them ("." for
    unchanged), and, for a submodule, what changed in it."""

    path: str
    index: str
    worktree: str
    submodule: SubmoduleState | None


@dataclass(frozen=True)
class Renamed(Changed):
    """A path renamed (index "R") or copied ("C") from `original`, `similarity` percent alike."""

    _: KW_ONLY
    original: str
    similarity: int


@dataclass(frozen=True)
class Unmerged:
    """A path with a conflict, and how each side changed it, as git's short status prints
    them (e.g. "U" and "U": both modified)."""

    path: str
    index: str
    worktree: str
    submodule: SubmoduleState | None


@dataclass(frozen=True)
class Untracked:
    """An untracked path: a directory, as git shows one, ends with "/"."""

    path: str


@dataclass(frozen=True)
class Ignored:
    """An ignored path: a directory, as git shows one, ends with "/"."""

    path: str


type StatusEntry = Changed | Renamed | Unmerged | Untracked | Ignored


@dataclass(frozen=True)
class StatusBranch:
    """HEAD's commit (None on an unborn branch), branch's short name (None if detached), the
    upstream's short name (None if none is set), and how many commits HEAD is ahead of it and
    behind it (None if it has no upstream, or its upstream is gone)."""

    commit: str | None
    head: str | None
    upstream: str | None
    ahead_behind: tuple[int, int] | None


@dataclass(frozen=True)
class Status:
    """A worktree's status: its entries in git's order, and its branch if asked for."""

    entries: tuple[StatusEntry, ...]
    branch: StatusBranch | None


def _submodule(field: bytes) -> SubmoduleState | None:
    if field[:1] != b"S":
        return None  # "N..."
    return SubmoduleState(field[1:2] == b"C", field[2:3] == b"M", field[3:4] == b"U")


def _entries(fields: Iterator[bytes], args: tuple[str, ...]) -> Iterator[StatusEntry]:
    """The entries of `git status --porcelain=v2 -z`, but for its headers. Each field is one
    of git's NUL-separated fields; a rename's original path is the field after it. Raises
    GitError for an entry git doesn't document."""

    def columns(field: bytes, count: int) -> list[bytes]:
        """The entry's space-separated columns, the last (its path) whole."""
        found = field.split(b" ", count - 1)
        if len(found) != count or len(found[1]) != 2:
            raise GitError(args, 0, f"status printed an entry it doesn't document: {field!r}")
        return found

    for field in fields:
        kind = field[:2]
        if kind == b"1 ":
            _, xy, sub, *_, path = columns(field, 9)
            yield Changed(decode(path), chr(xy[0]), chr(xy[1]), _submodule(sub))
        elif kind == b"2 ":
            _, xy, sub, *_, score, path = columns(field, 10)
            original = next(fields, b"")
            if not original:
                raise GitError(args, 0, f"status printed a rename with no original: {field!r}")
            yield Renamed(
                decode(path),
                chr(xy[0]),
                chr(xy[1]),
                _submodule(sub),
                original=decode(original),
                similarity=int(score[1:]),
            )
        elif kind == b"u ":
            _, xy, sub, *_, path = columns(field, 11)
            yield Unmerged(decode(path), chr(xy[0]), chr(xy[1]), _submodule(sub))
        elif kind == b"? ":
            yield Untracked(decode(field[2:]))
        elif kind == b"! ":
            yield Ignored(decode(field[2:]))
        elif field and kind != b"# ":  # git documents that it may add headers, not entries
            raise GitError(args, 0, f"status printed an entry it doesn't document: {field!r}")


@record_read("git")
def status(
    git: ReadOnlyGit,
    *,
    untracked: Literal["no", "normal", "all"] | None = None,
    ignored: Literal["no", "traditional", "matching"] = "no",
    ignore_submodules: Literal["none", "untracked", "dirty", "all"] | None = None,
    renames: bool | None = None,
    branch: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> Status:
    """The status of the client's worktree, from one `git status --porcelain=v2 -z`, with paths
    from its top, whatever the client's directory.

    Each switch is git's (`--untracked-files`, `--ignored`, `--ignore-submodules`,
    `--[no-]renames`); None leaves it to git's config (`status.showUntrackedFiles`, a
    submodule's `ignore`, `status.renames`). With `branch`, `Status.branch` says where HEAD is.

    git runs with `--no-optional-locks`, so it doesn't refresh the index (git would write the
    files whose stat it found stale), which makes it a read a ReadOnlyGit offers; a
    ReadOnlyGit's `-c core.fsmonitor=false` (see ReadOnlyGit) keeps the fsmonitor hook from
    speeding it up. So a FakeGit test responds to `["--no-optional-locks", "status",
    "--porcelain=v2", "-z"]`. Raises GitError if git fails (e.g. in a bare repository), or
    prints an entry its documentation doesn't describe; a header it doesn't describe is
    skipped, as git says it may add some."""
    args = ("--no-optional-locks", "status", "--porcelain=v2", "-z")
    if untracked is not None:
        args += (f"--untracked-files={untracked}",)
    if ignored != "no":
        args += (f"--ignored={ignored}",)
    if ignore_submodules is not None:
        args += (f"--ignore-submodules={ignore_submodules}",)
    if renames is not None:
        args += ("--renames" if renames else "--no-renames",)
    if branch:
        args += ("--branch",)
    fields = read_output(git, *args, timeout=timeout).split(b"\0")
    return Status(tuple(_entries(iter(fields), args)), _branch(fields) if branch else None)


def _branch(fields: list[bytes]) -> StatusBranch:
    """The branch from `git status --porcelain=v2 --branch`'s headers ("# <key> <value>")."""
    # Only those before the entries: a rename's original path could look like one.
    headers: dict[str, str] = {}
    for field in takewhile(lambda f: f.startswith(b"# "), fields):
        key, _, value = decode(field[2:]).partition(" ")
        headers[key] = value
    commit, head = headers["branch.oid"], headers["branch.head"]
    counts = None
    if "branch.ab" in headers:  # "+<ahead> -<behind>"
        ahead, _, behind = headers["branch.ab"].partition(" ")
        counts = (int(ahead), -int(behind))
    return StatusBranch(
        None if commit == "(initial)" else commit,
        None if head == "(detached)" else head,
        headers.get("branch.upstream"),
        counts,
    )


@dataclass(frozen=True)
class Worktree:
    """A worktree of the repository: its path; its HEAD's commit (None if bare, or on an
    unborn branch); the full name of its branch (None if detached or bare); whether it's the
    bare repository or detached; and the reason it's locked and the reason git would prune
    it, each "" if git gives none, and None if it isn't."""

    path: Path
    head: str | None
    branch: str | None
    bare: bool
    detached: bool
    locked: str | None
    prunable: str | None


@record_read("git")
def worktrees(git: ReadOnlyGit, *, timeout: Timeout = CLIENT_TIMEOUT) -> list[Worktree]:
    """The repository's worktrees, the main one first, from one `git worktree list
    --porcelain -z` (git 2.36). Raises GitError if git fails (e.g. NotARepositoryError)."""
    found: list[Worktree] = []
    attributes: dict[str, str] = {}
    for field in decode(
        read_output(git, "worktree", "list", "--porcelain", "-z", timeout=timeout)
    ).split("\0"):
        if field:
            key, _, value = field.partition(" ")
            attributes[key] = value
        elif attributes:  # an empty field ends each worktree's
            head = attributes.get("HEAD")
            found.append(
                Worktree(
                    Path(attributes["worktree"]),
                    None if head is None or not head.strip("0") else head,
                    attributes.get("branch"),
                    "bare" in attributes,
                    "detached" in attributes,
                    attributes.get("locked"),
                    attributes.get("prunable"),
                )
            )
            attributes = {}
    return found


def add_worktree(
    git: Git,
    path: str | os.PathLike[str],
    commit: str,
    *,
    detach: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> None:
    """Add a worktree at `path` (from the client's directory), with `commit` checked out:
    a branch's name checks the branch out there (which git refuses if it's checked out in
    another worktree), unless `detach`, which detaches HEAD at its commit, as any other
    revision does. One `git worktree add`, which runs the post-checkout hook. Raises
    UnknownRevisionError if `commit` names nothing, and GitError for what else git refuses
    (e.g. a path that exists and isn't empty)."""
    args = ("worktree", "add", *(["--detach"] if detach else []), "--", os.fsencode(path), commit)
    stdout_of(args, git.process(*args, capture_output=True, timeout=timeout))


def remove_worktree(
    git: Git,
    path: str | os.PathLike[str],
    *,
    force: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> None:
    """Remove the worktree at `path` (from the client's directory), its files and git's
    record of it, with one `git worktree remove`. git refuses one with changes or untracked
    files unless `force`, and a locked one, or the main one, either way: it then raises
    GitError."""
    args = ("worktree", "remove", *(["--force"] if force else []), "--", os.fsencode(path))
    stdout_of(args, git.process(*args, capture_output=True, timeout=timeout))


def prune_worktrees(git: Git, *, timeout: Timeout = CLIENT_TIMEOUT) -> None:
    """Forget the worktrees git would prune (e.g. one whose directory is gone, unless it's
    locked: see `Worktree.prunable`), with one `git worktree prune`."""
    args = ("worktree", "prune")
    stdout_of(args, git.process(*args, capture_output=True, timeout=timeout))
