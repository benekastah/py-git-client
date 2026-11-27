"""Merges of commits without a worktree or an index: many `git merge-tree --write-tree` in one
process."""

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from git_client.client import OBJECT_ID, Arg, Git, decode, error_from
from git_client.errors import (
    CorruptObjectError,
    GitError,
    UnknownRevisionError,
    WrongTypeError,
)

# The options that make merge-tree merge two commits and write the result (git 2.38), and
# take many pairs on its stdin (git 2.39); and the merge base, as an option and on a line of
# stdin, before the pair (git 2.40). Variables, so a test can have git refuse each, as an
# older git does.
_WRITE_TREE = "--write-tree"
_STDIN = "--stdin"
_MERGE_BASE = "--merge-base"
_BASE_SEPARATOR = "--"

# What git before 2.40 says of a line of stdin with a merge base.
_MALFORMED = "fatal: malformed input line"

# What merge_trees asks of every merge: conflicted paths, each once, and not the messages
# ("Auto-merging a", "CONFLICT (content): ..."), which git's documentation says aren't made
# to be parsed (a submodule's conflict adds lines of plain text among them).
_OUTPUT = ("--name-only", "--no-messages")

# git's exit status for a usage error: an option it doesn't know.
_USAGE = 129

# The errors that are about the pair git stopped at: an id that names no commit, or one that
# names another object (e.g. a tree), and a damaged object of its history. Not
# MissingObjectError: a partial clone that can't fetch one object (most often, as its remote
# can't be reached) can't fetch the next pair's either.
_ABOUT_THE_PAIR = (UnknownRevisionError, WrongTypeError, CorruptObjectError)

# What else git says of the pair it stopped at: commits that share no history, and an object
# lost from their history: a blob it merges, a subtree, a blob it reads to find renames, and a
# commit or a commit's tree.
_SAYS_OF_THE_PAIR = re.compile(
    "^fatal: refusing to merge unrelated histories$"
    r"|^fatal: unable to read blob object [0-9a-f]+$"
    r"|^fatal: unable to read tree \([0-9a-f]+\)$"
    r"|^fatal: unable to read [0-9a-f]+$"
    r"|^error: Could not read [0-9a-f]+$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Merge:
    """A merge for `merge_trees`, by full object ids: theirs into ours, as `git merge-tree
    --write-tree [--merge-base=<base>] <ours> <theirs>` merges them. With no `base`, git finds
    their merge base, as for a pair (ours, theirs). With one, git takes it as their only merge
    base: a cherry-pick of commit C onto ours is `Merge(ours, C, base=<C's parent>)`. From git
    2.45, with a base, all three can be trees."""

    ours: str
    theirs: str
    base: str | None = None


@dataclass(frozen=True)
class MergeResult:
    """What `merge_trees` gives for one pair, as `git merge-tree --write-tree` gives it.
    `clean`: whether the merge had no conflicts (True or False), or None if git couldn't merge
    the pair at all, in which case `error` says why. `tree`: the id of the merged tree (None
    if git couldn't merge), with conflict markers in conflicted files, which name the commits
    by the ids given: so a conflicted tree differs from one `git merge-tree --write-tree` makes
    of branch names, though a clean one is the same. `conflicts`: the paths
    with conflicts, each once, in git's order, relative to the client's directory (`../a`
    from a subdirectory), as git gives them. A merge with conflicts can have none to list
    (e.g. some conflicts of directory renames): `clean` is what says whether it had any."""

    clean: bool | None
    tree: str | None
    conflicts: tuple[str, ...] = ()
    error: GitError | None = None


def merge_trees(git: Git, pairs: Sequence[tuple[str, str] | Merge]) -> list[MergeResult]:
    """Merge each pair of commits (ours, theirs), given by their full object ids, as `git
    merge-tree --write-tree <ours> <theirs>` does, or each Merge (which can give a merge base),
    and give a MergeResult for each, in order:
    with one `git merge-tree --stdin` for them all, so many pairs cost one process. It takes a
    Git, not a ReadOnlyGit, as merge-tree writes the merged trees and blobs to the object
    database (no ref points to them, so `git gc` removes them in time).

    A pair git can't merge gets a MergeResult whose `clean` is None and whose `error` says why:
    an UnknownRevisionError for an id that names no commit the repository has, a
    WrongTypeError for one that names another object (e.g. a tree), and a GitError for
    commits that share no history (which merge-tree refuses, as `git merge` does) or whose
    objects it can't read (e.g. a damaged object store). git stops at such a pair, so the
    pairs after it are merged by another process: each pair git can't merge costs one. Any
    other failure, which isn't about one pair, is raised, wherever it comes: e.g.
    NotARepositoryError, no room to write objects, or MissingObjectError, for an object a
    partial clone couldn't fetch, as each pair after it would wait to fail the same way.

    With a base, git doesn't check that ours and theirs share history. From git 2.45, an id it
    can't read as a tree (e.g. one that names nothing) then gets a GitError; before, git stops
    at an id that names no commit with a GitError that is raised, as it isn't known to be about
    the merge.

    Raises ValueError, starting no git, for a pair or Merge that isn't full object ids: names
    (a branch, `HEAD`) aren't taken, so nothing given can be read as an option or as more than
    one name. Needs git 2.38 (merge-tree's `--write-tree`), else raises GitError. On git 2.38,
    whose merge-tree takes no `--stdin` (2.39's), each pair takes a process of its own, with
    the same results, after the batch was tried once and refused. A Merge with a base needs git
    2.40 (merge-tree's merge base), else raises GitError."""
    merges = [pair if isinstance(pair, Merge) else Merge(*pair) for pair in pairs]
    for merge in merges:
        for object_id in (merge.ours, merge.theirs, merge.base):
            if object_id is not None and not OBJECT_ID.fullmatch(object_id):
                raise ValueError(f"merge_trees takes full object ids, not {object_id!r}")
    results: list[MergeResult] = []
    while len(results) < len(merges):
        rest = merges[len(results) :]
        args = ("merge-tree", _WRITE_TREE, _STDIN, *_OUTPUT)
        lines = "".join(map(_line, rest)).encode()
        result = git.process(*args, capture_output=True, input=lines)
        if result.returncode == _USAGE:
            return _one_at_a_time(git, merges)
        merged = _merges(args, result, result.stdout)
        results += merged
        if result.returncode == 0 and len(merged) == len(rest):
            break
        if result.returncode == 0 or len(merged) >= len(rest):
            why = f"merge-tree gave {len(merged)} results for {len(rest)} pairs"
            raise _unexpected(args, result, why)
        # git stopped at the next pair.
        if rest[len(merged)].base is not None and decode(result.stderr).startswith(_MALFORMED):
            error = error_from(args, result)
            error.add_note("merge_trees needs git 2.40 or newer for a merge base")
            raise error
        results.append(_failed(args, result))
    return results


def _line(merge: Merge) -> str:
    """merge's line of `merge-tree --stdin`'s input."""
    base = "" if merge.base is None else f"{merge.base} {_BASE_SEPARATOR} "
    return f"{base}{merge.ours} {merge.theirs}\n"


def _one_at_a_time(git: Git, merges: Sequence[Merge]) -> list[MergeResult]:
    """merge_trees for a git whose merge-tree takes no --stdin: a process for each merge."""
    results: list[MergeResult] = []
    for merge in merges:
        base = () if merge.base is None else (f"{_MERGE_BASE}={merge.base}",)
        args = ("merge-tree", _WRITE_TREE, "-z", *_OUTPUT, *base, "--end-of-options")
        args += (merge.ours, merge.theirs)
        result = git.process(*args, capture_output=True)
        if result.returncode == _USAGE:
            needs = (
                "merge_trees needs git 2.38 or newer, whose merge-tree has --write-tree"
                if merge.base is None
                else "merge_trees needs git 2.40 or newer for a merge base"
            )
            raise GitError(args, result.returncode, decode(result.stderr) + needs)
        # Exit status 1 is a conflict, but also a commit git can't find, for which it prints
        # nothing.
        if result.returncode in (0, 1) and result.stdout:
            clean = b"1" if result.returncode == 0 else b"0"
            [merged] = _merges(args, result, clean + b"\0" + result.stdout + b"\0")
            results.append(merged)
        else:
            results.append(_failed(args, result))
    return results


def _merges(
    args: Sequence[Arg], result: subprocess.CompletedProcess[bytes], output: bytes
) -> list[MergeResult]:
    """The merges in `merge-tree --stdin --name-only --no-messages`'s output (`result`'s, or
    made from it) that git finished printing: for each, its status (1 if clean, 0 if not), its
    tree and its conflicted paths, each ended by a NUL, then a NUL."""
    fields = output.split(b"\0")
    # What's after the last NUL: git hadn't finished it.
    finished = len(fields) - 1
    merges: list[MergeResult] = []
    start = 0
    while start + 1 < finished:
        status, tree = fields[start], fields[start + 1]
        if status not in (b"0", b"1") or not OBJECT_ID.fullmatch(decode(tree)):
            raise _unexpected(args, result, "merge-tree printed what merge_trees can't read")
        end = start + 2
        while end < finished and fields[end]:
            end += 1
        if end == finished:
            break
        paths = tuple(decode(path) for path in fields[start + 2 : end])
        merges.append(MergeResult(status == b"1", decode(tree), paths))
        start = end + 1
    return merges


def _failed(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> MergeResult:
    """The MergeResult of a pair git stopped at, saying why. Raises git's error instead if it
    isn't known to be about the pair (e.g. not a repository)."""
    # Not its stdout, the earlier pairs' results.
    error = error_from(
        args, subprocess.CompletedProcess(args, result.returncode, b"", result.stderr)
    )
    if not isinstance(error, _ABOUT_THE_PAIR) and not _SAYS_OF_THE_PAIR.search(error.stderr):
        raise error
    return MergeResult(None, None, (), error)


def _unexpected(
    args: Sequence[Arg], result: subprocess.CompletedProcess[bytes], why: str
) -> GitError:
    """The GitError for output of git's that merge_trees can't take, saying why in a note."""
    error = GitError(args, result.returncode, decode(result.stderr), decode(result.stdout))
    error.add_note(why)
    return error
