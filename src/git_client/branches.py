"""Writes to branches, each one `git branch` (`delete_branches` one for all it's given), and
whether a name is a branch's, as `git check-ref-format --branch` says."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from git_client.client import (
    CLIENT_TIMEOUT,
    FATAL,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    error_from,
    read_process,
    stdout_of,
)
from git_client.errors import GitError
from git_client.performance import record_read

# What `git branch --delete` prints of each branch it deleted, in English (see Git.process),
# with core.abbrev=no: what it pointed at is a full id (a symbolic ref's target, or "broken").
_DELETED = re.compile(r"Deleted branch (.+) \(was (.+)\)\.")
# git branch's exit status when it deleted some of the branches, but not all.
_SOME_NOT_DELETED = 1


@dataclass(frozen=True)
class DeletedBranches:
    """What `delete_branches` did: `deleted`, each branch git deleted, by its name as git
    printed it, and what it pointed at (a full object id; for a symbolic ref, the ref it
    pointed to; "broken" for a ref git couldn't read); `returncode`, git's exit status, 1 if
    it didn't delete them all; and `stderr`, what git said: why it didn't delete each branch
    it didn't, and its warnings."""

    deleted: dict[str, str]
    returncode: int
    stderr: str


def delete_branches(
    git: Git, names: Iterable[str], *, force: bool = False, timeout: Timeout = CLIENT_TIMEOUT
) -> DeletedBranches:
    """Delete the branches named, with their config (`branch.<name>.*`) and reflogs, with one
    `git branch --delete` (`-D` with `force`: even those not merged into their upstream or
    HEAD), which deletes each it can and says why not of the others: a branch that doesn't
    exist, isn't merged, or is checked out in a worktree. The others are deleted in one
    transaction, so if git fails to delete them (e.g. a ref is locked, or a name is given
    twice), it deletes none of them. A name that git expands (`@{-1}`, the branch checked out
    before) is deleted under the name it expands to.

    No names: nothing to do, and no process. Raises ValueError for a name with a line ending
    (git couldn't print it plainly), and GitError if git deletes nothing for a reason not of
    one branch (exit status other than 0 or 1, e.g. not a repository). A `FakeGit` test
    responds to `["-c", "core.abbrev=no", "branch", "--delete"]` (`-D` with `force`) with a
    line `Deleted branch <name> (was <id>).` for each branch deleted."""
    names = list(names)
    if not names:
        return DeletedBranches({}, 0, "")
    # git refuses such a name as a branch's, but a ref file of that name (made by hand) is one
    # it deletes, printing "Deleted branch a\nb (was broken)." across two lines.
    if any("\n" in name for name in names):
        raise ValueError(f"delete_branches can't take a name with a line ending: {names!r}")
    args = ("-c", "core.abbrev=no", "branch", "-D" if force else "--delete", "--end-of-options")
    args += tuple(names)
    result = git.process(*args, capture_output=True, timeout=timeout)
    if result.returncode not in (0, _SOME_NOT_DELETED):
        raise error_from(args, result)
    deleted: dict[str, str] = {}
    # Lines end at "\n" alone: splitlines() also splits at characters a name can hold (U+2028).
    for line in filter(None, decode(result.stdout).split("\n")):
        if not (found := _DELETED.fullmatch(line)):
            raise GitError(args, result.returncode, f"git branch printed {line!r}")
        deleted[found[1]] = found[2]
    return DeletedBranches(deleted, result.returncode, decode(result.stderr))


def create_branch(
    git: Git,
    name: str,
    start_point: str | None = None,
    *,
    force: bool = False,
    track: bool | Literal["inherit"] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> None:
    """Create the branch `name` at `start_point` (any revision; HEAD if None), with one `git
    branch`. With `force`, an existing branch of that name is moved there (git refuses to move
    the branch checked out in a worktree); without, it raises GitError. `track`: whether the
    new branch's upstream is `start_point` (True, `--track`: a remote-tracking or local
    branch), none (False, `--no-track`), `start_point`'s own upstream ("inherit"), or as
    git's config `branch.autoSetupMerge` says (None). Raises GitError if git refuses, e.g.
    for a name git doesn't take as a branch's (see `valid_branch_name`)."""
    switches = {"--force": force, "--track": track is True, "--no-track": track is False}
    git.run(
        "branch",
        "--quiet",
        *(switch for switch, on in switches.items() if on),
        *(["--track=inherit"] if track == "inherit" else []),
        "--end-of-options",
        name,
        *([] if start_point is None else [start_point]),
        timeout=timeout,
    )


def rename_branch(
    git: Git, old: str, new: str, *, force: bool = False, timeout: Timeout = CLIENT_TIMEOUT
) -> None:
    """Rename the branch `old` to `new`, with its config and reflog, with one `git branch
    --move` (in a worktree it's checked out in, it stays checked out). With `force`, a branch
    named `new` is replaced; without, it raises GitError. Raises GitError if git refuses,
    e.g. for a name git doesn't take as a branch's (see `valid_branch_name`)."""
    move = "-M" if force else "--move"
    git.run("branch", move, "--end-of-options", old, new, timeout=timeout)


def set_upstream(
    git: Git, branch: str, upstream: str | None, *, timeout: Timeout = CLIENT_TIMEOUT
) -> None:
    """Set the branch's upstream to `upstream` (a remote-tracking or local branch, which git
    writes as `branch.<branch>.remote` and `.merge`), with one `git branch --set-upstream-to`,
    or, given None, remove it (`--unset-upstream`). Raises GitError if git refuses: e.g. a
    branch that doesn't exist, or, to remove, one with no upstream."""
    if upstream is None:
        args = ["--unset-upstream"]
    else:
        args = ["--quiet", f"--set-upstream-to={upstream}"]
    git.run("branch", *args, "--end-of-options", branch, timeout=timeout)


@record_read("git")
def valid_branch_name(git: ReadOnlyGit, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> bool:
    """Whether a branch can have the name `name`, as git decides it (`git check-ref-format
    --branch`): not `HEAD`, nor starting with `-`, and a valid ref name under `refs/heads/`.
    A name that git expands (`@{-1}`, the branch checked out before) isn't one, though it
    names one: git printing back a name other than `name` says it expanded it. One process.
    Raises GitError if git fails other than to say no."""
    args = ("check-ref-format", "--branch", name)
    result = read_process(git, *args, capture_output=True, timeout=timeout)
    if result.returncode == FATAL:  # git's no: not a branch's name, or one it can't expand
        return False
    return decode(stdout_of(args, result)).removesuffix("\n") == name
