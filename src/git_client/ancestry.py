"""How commits are related, as `git merge-base` answers: whether one is an ancestor of another,
their merge bases, which of many no other reaches, and where a branch forked from a ref."""

import re

from git_client.client import CLIENT_TIMEOUT, ReadOnlyGit, Timeout, decode, error_from, read_process
from git_client.errors import GitError
from git_client.performance import record_read

# merge-base's exit status for a no: not an ancestor, or no merge base or fork point found.
_NO = 1
# What git says as it fails, which --fork-point does with exit 1 too (e.g. of a tree): then it's
# no answer.
_ERROR = re.compile(r"^error: ", re.MULTILINE)


def _merge_base(
    git: ReadOnlyGit, *options: str, revisions: tuple[str, ...], timeout: Timeout
) -> tuple[tuple[str, ...], list[str] | None]:
    """git's arguments for `git merge-base <options> <revisions>`, and the lines it printed, or
    None for its no (exit 1, with no error said)."""
    for name in revisions:
        # Which no argument can hold (Python would refuse it as git starts).
        if "\0" in name:
            raise ValueError(f"not a revision: {name!r}")
    args = ("merge-base", *options, "--end-of-options", *revisions)
    result = read_process(git, *args, capture_output=True, timeout=timeout)
    if result.returncode == _NO and not _ERROR.search(decode(result.stderr)):
        return args, None
    if result.returncode != 0:
        raise error_from(args, result)
    return args, decode(result.stdout).splitlines()


def _one(args: tuple[str, ...], lines: list[str] | None) -> str | None:
    """The one commit merge-base found, or None if it found none."""
    if lines is not None and len(lines) != 1:
        raise GitError(args, 0, f"merge-base printed {len(lines)} commits, not one: {lines!r}")
    return None if lines is None else lines[0]


@record_read("git")
def is_ancestor(
    git: ReadOnlyGit, ancestor: str, descendant: str, *, timeout: Timeout = CLIENT_TIMEOUT
) -> bool:
    """Whether the commit `ancestor` names is in the history of the one `descendant` names (or
    is it), as git sees that history (with its replace refs, grafts and shallow boundary).

    One `git merge-base --is-ancestor` per pair, as git has no form for many pairs (for many
    refs against one commit, `refs(merged=...)` asks it once). Raises UnknownRevisionError for
    a name that names nothing, WrongTypeError for one that names no commit (e.g. a tree), and
    ValueError for one that holds a NUL."""
    _, lines = _merge_base(git, "--is-ancestor", revisions=(ancestor, descendant), timeout=timeout)
    return lines is not None


@record_read("git")
def merge_base(
    git: ReadOnlyGit, a: str, b: str, *, timeout: Timeout = CLIENT_TIMEOUT
) -> str | None:
    """A best common ancestor of the commits `a` and `b` name, as `git merge-base` picks one
    (where there are several, see `merge_bases`), or None if they have no history in common.
    Raises as `is_ancestor` does."""
    return _one(*_merge_base(git, revisions=(a, b), timeout=timeout))


@record_read("git")
def merge_bases(
    git: ReadOnlyGit, a: str, b: str, *, timeout: Timeout = CLIENT_TIMEOUT
) -> list[str]:
    """Every best common ancestor of the commits `a` and `b` name (`git merge-base --all`):
    one, more after criss-cross merges, or none if they have no history in common. Raises as
    `is_ancestor` does."""
    args, lines = _merge_base(git, "--all", revisions=(a, b), timeout=timeout)
    if lines is None:
        return []
    if not lines:
        raise GitError(args, 0, "merge-base printed no commit, and said it found one")
    return lines


@record_read("git")
def independent(git: ReadOnlyGit, *commits: str, timeout: Timeout = CLIENT_TIMEOUT) -> list[str]:
    """The ids of the commits named that none of the others can reach (`git merge-base
    --independent`), each once, in the order git gives them; [] for none (and no process).
    One process for all of them. Raises as `is_ancestor` does."""
    if not commits:
        return []
    args, lines = _merge_base(git, "--independent", revisions=commits, timeout=timeout)
    if not lines:  # nor exit 1, git's no, which it has none of for --independent
        status = _NO if lines is None else 0
        raise GitError(args, status, "merge-base printed no commit, of some")
    return lines


@record_read("git")
def fork_point(
    git: ReadOnlyGit, upstream: str, commit: str, *, timeout: Timeout = CLIENT_TIMEOUT
) -> str | None:
    """Where the history of `commit` forked from the ref `upstream` (`git merge-base
    --fork-point`), or None if git finds no such point: not only their merge base, as git
    looks in upstream's reflog too, for a commit upstream once pointed at, before it was
    rewritten, that the history of `commit` was built on. `upstream` is a ref
    ("refs/heads/main", or a name git expands to one): one that names no ref (an object id
    too) raises UnknownRevisionError, and one that names no commit (an annotated tag with no
    reflog) GitError. Raises as `is_ancestor` does for `commit`, but for a full id the
    repository lacks, of which git says nothing, as when it finds no fork point: None."""
    return _one(*_merge_base(git, "--fork-point", revisions=(upstream, commit), timeout=timeout))
