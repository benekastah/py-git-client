"""git's porcelain writes (switch, reset, merge, cherry-pick, revert, commit), typed: each
starts one git, its hooks running, and raises what stopped or failed it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Literal

from git_client.client import (
    CLIENT_TIMEOUT,
    Arg,
    EnvValue,
    Git,
    Timeout,
    decode,
    error_from,
)
from git_client.errors import ConflictError, GitError

type Stdout = int | IO[bytes] | IO[str] | None
"""Where git's stdout goes with `inherit_output` (None: the tool's), e.g. sys.stderr."""

# git's exit status for a stop: a conflict, or a hook's or an empty commit's.
_STOPPED = 1


@dataclass(frozen=True)
class CommandOutput:
    """What a write printed, as text (see `decode`): each None with `inherit_output`, which
    left git's output to the tool's own streams."""

    stdout: str | None
    stderr: str | None


def _write(
    git: Git,
    args: tuple[Arg, ...],
    *,
    inherit_output: bool,
    stdout: Stdout,
    env: Mapping[str, EnvValue] | None,
    timeout: Timeout,
    can_conflict: bool = False,
) -> CommandOutput:
    """Run a write through the client's `process`, so a subclass and `git_calls` see it."""
    if not inherit_output:
        if stdout is not None:
            raise ValueError("stdout is where an inherited output goes: give inherit_output")
        result = git.process(*args, capture_output=True, env=env, timeout=timeout)
        if result.returncode:
            raise error_from(args, result)
        return CommandOutput(decode(result.stdout), decode(result.stderr))
    result = git.process(*args, stdout=stdout, env=env, timeout=timeout)
    if not result.returncode:
        return CommandOutput(None, None)
    if can_conflict and result.returncode == _STOPPED and _unmerged(git, env, timeout):
        raise ConflictError(args, result.returncode, "")
    raise GitError(args, result.returncode, "")


def _unmerged(git: Git, env: Mapping[str, EnvValue] | None, timeout: Timeout) -> bool:
    """Whether the index has unmerged paths, anywhere in the worktree (`:/`)."""
    args = ("ls-files", "--unmerged", "-z", "--", ":/")
    result = git.process(*args, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        raise error_from(args, result)
    return bool(result.stdout)


def _refuse_edit(edit: bool, inherit_output: bool) -> None:
    """Refuse an editor git would start with no terminal: a terminal editor (e.g. vim) would
    wait for one, forever without a timeout."""
    if edit and not inherit_output:
        raise ValueError("edit opens the editor, which needs the terminal: give inherit_output")


def _refuse_option(name: str, what: str) -> None:
    """Refuse a name git 2.36 reads as an option, even after `--end-of-options` (in reset,
    cherry-pick and revert)."""
    if name.startswith("-"):
        raise ValueError(f"{what} {name!r} would be read as an option")


def _refuse_options(command: str, commits: tuple[str, ...]) -> None:
    if not commits:
        raise ValueError(f"{command} was given no commits")
    for name in commits:
        _refuse_option(name, "commit")


def switch(
    git: Git,
    target: str,
    *,
    detach: bool = False,
    create: bool = False,
    start_point: str | None = None,
    track: bool | None = None,
    force: bool = False,
    quiet: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Switch to the branch `target` with one `git switch`; with `detach`, check out the commit
    `target` names, HEAD detached (`--detach`); with `create`, create the branch `target` at
    `start_point` (HEAD if None) and switch to it (`--create`). `track` True or False sets, or
    doesn't, the new branch's upstream to `start_point` (`--track`, `--no-track`); None does as
    `branch.autoSetupMerge` says. `force` throws away changes and untracked files in the way
    (`--discard-changes`); without, git refuses to lose them, or to overwrite an untracked file.
    `quiet` has git print nothing (`--quiet`). See `merge` for `inherit_output`, `stdout` and
    what's raised. The post-checkout hook runs.

    The arguments reach git as they are, which decides what they mean: given a `target` that
    names no branch but a remote's (e.g. `origin/feat` for "feat"), git creates that branch
    tracking it (`checkout.guess`, true by default); `track` without `create` has git create
    a branch from the remote branch `target` (e.g. "feat" from "origin/feat"); and
    `start_point` without `create`, or `detach` with it, are git usage errors (GitError)."""
    switches = {
        "--quiet": quiet,
        "--detach": detach,
        "--discard-changes": force,
        "--track": track is True,
        "--no-track": track is False,
    }
    names = (f"--create={target}", "--end-of-options") if create else ("--end-of-options", target)
    args = (
        "switch",
        *(switch for switch, on in switches.items() if on),
        *names,
        *(() if start_point is None else (start_point,)),
    )
    return _write(git, args, inherit_output=inherit_output, stdout=stdout, env=env, timeout=timeout)


def reset(
    git: Git,
    commit: str | None = None,
    *,
    mode: Literal["soft", "mixed", "hard", "keep", "merge"] = "mixed",
    quiet: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Point the current branch (or a detached HEAD) at `commit` (HEAD if None) with one `git
    reset --<mode>`: `soft` leaves the index and worktree alone, `mixed` resets the index,
    `hard` both, throwing away changes; `keep` resets both but refuses (raising GitError,
    changing nothing) to lose a change, and `merge` keeps changes that aren't staged.
    `quiet` has git print nothing (`--quiet`). See `merge` for `inherit_output`, `stdout` and
    what's raised. Raises ValueError for a commit starting with `-`, which git 2.36 reads as an
    option, even after `--end-of-options`."""
    if commit is not None:
        _refuse_option(commit, "commit")
    args = (
        "reset",
        f"--{mode}",
        *(("--quiet",) if quiet else ()),
        *(() if commit is None else (commit, "--")),
    )
    return _write(git, args, inherit_output=inherit_output, stdout=stdout, env=env, timeout=timeout)


def merge(
    git: Git,
    commit: str,
    *,
    ff: Literal["only", "no"] | None = None,
    message: str | None = None,
    edit: bool = False,
    quiet: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Merge `commit` into the current branch with one `git merge`. `ff` "only" fast-forwards
    or fails (`--ff-only`), "no" always makes a merge commit (`--no-ff`), and None does as
    `merge.ff` says. `message` is the merge commit's (`--message`); `edit` opens the editor on
    it (`--edit`), which without is never opened (`--no-edit`), whatever git's default for a
    terminal (or `GIT_MERGE_AUTOEDIT`). `edit` needs `inherit_output` (ValueError without):
    an editor given no terminal (e.g. vim) would wait for one, forever without a timeout.
    `quiet` has git print nothing (`--quiet`). Hooks run: pre-merge-commit,
    prepare-commit-msg, commit-msg, post-merge.

    Returns what git printed, captured; or, with `inherit_output`, nothing, git's input and
    output being the tool's (its stdout going to `stdout` if given), as `Git.passthrough`
    gives them, for git to show its progress and ask the user (e.g. in the editor). Without
    it, git reads nothing (an editor gets no terminal), and giving `stdout` raises ValueError.

    A conflict that stops the merge (git's exit status 1, the merge left for the user to
    resolve and commit, or `abort`) raises ConflictError: from what git says (a `CONFLICT (`
    line on stdout, in English: see `Git.process`), or, with `inherit_output`, whose output
    the library doesn't see, from git's exit status 1 and unmerged paths in the index, asked
    with one more git (`ls-files --unmerged -z -- :/`) after a failure with exit status 1. So,
    with `inherit_output`, a conflict that `rerere.autoUpdate` resolved and staged (git still
    stops, exit status 1, leaving no unmerged path) raises a plain GitError, where captured
    output raises ConflictError. Any other failure raises
    GitError, as `Git.run` does (e.g. UnknownRevisionError for a name that names nothing, or a
    plain GitError for a hook's refusal); with `inherit_output`, a plain GitError, its stderr
    empty (git printed it to the tool's)."""
    _refuse_edit(edit, inherit_output)
    if message is not None and "\0" in message:
        raise ValueError("a message can't hold a NUL")
    switches = {
        "--quiet": quiet,
        "--ff-only": ff == "only",
        "--no-ff": ff == "no",
        "--edit": edit,
        "--no-edit": not edit,
    }
    args = (
        "merge",
        *(switch for switch, on in switches.items() if on),
        *(() if message is None else (f"--message={message}",)),
        "--end-of-options",
        commit,
    )
    return _write(
        git,
        args,
        inherit_output=inherit_output,
        stdout=stdout,
        env=env,
        timeout=timeout,
        can_conflict=True,
    )


def cherry_pick(
    git: Git,
    *commits: str,
    ff: bool = False,
    record_origin: bool = False,
    allow_empty: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Apply each commit onto HEAD, in order, with one `git cherry-pick`. `ff` fast-forwards
    to a commit whose parent is HEAD rather than make a new one (`--ff`); `record_origin`
    adds "(cherry picked from commit <id>)" to each message (`-x`); `allow_empty` keeps a
    commit that makes no change (`--allow-empty`), which otherwise stops the cherry-pick
    (a GitError: `skip` or `abort` it). A conflict stops it, the rest left for `skip`,
    `abort`, or the user (`git cherry-pick --continue`): see `merge` for that, and for
    `inherit_output`, `stdout` and what's raised. Raises ValueError without a commit, and for
    one starting with `-` (see `reset`)."""
    _refuse_options("cherry_pick", commits)
    switches = {"--ff": ff, "-x": record_origin, "--allow-empty": allow_empty}
    args = ("cherry-pick", *(switch for switch, on in switches.items() if on), *commits)
    return _write(
        git,
        args,
        inherit_output=inherit_output,
        stdout=stdout,
        env=env,
        timeout=timeout,
        can_conflict=True,
    )


def revert(
    git: Git,
    *commits: str,
    edit: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Commit the reverse of each commit, in order, with one `git revert`, each with git's
    message ("Revert ..."), which `edit` opens the editor on (`--edit`, which needs
    `inherit_output`, as `merge`'s does; without, never: `--no-edit`). A conflict stops it, as
    for `cherry_pick`; see `merge` for that, and for `inherit_output`, `stdout` and what's
    raised. Raises ValueError without a commit, and for one starting with `-` (see `reset`)."""
    _refuse_options("revert", commits)
    _refuse_edit(edit, inherit_output)
    args = ("revert", "--edit" if edit else "--no-edit", *commits)
    return _write(
        git,
        args,
        inherit_output=inherit_output,
        stdout=stdout,
        env=env,
        timeout=timeout,
        can_conflict=True,
    )


def commit(
    git: Git,
    message: str | None = None,
    *,
    all: bool = False,
    amend: bool = False,
    allow_empty: bool = False,
    no_verify: bool = False,
    quiet: bool = False,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Commit the index with one `git commit`, with `message` (`--message`, cleaned up as git
    does), never opening the editor. `all` stages every change to a tracked file first
    (`--all`; untracked files stay out). `amend` replaces HEAD's commit (`--amend`), keeping
    its message if `message` is None (`--no-edit`). `allow_empty` commits even if nothing
    changed (`--allow-empty`), which otherwise fails. Hooks run (pre-commit,
    prepare-commit-msg, commit-msg, post-commit): one that refuses raises GitError, unless
    `no_verify` skips pre-commit and commit-msg (`--no-verify`). `quiet` has git print nothing
    (`--quiet`). See `merge` for `inherit_output`, `stdout` and what's raised. Raises
    ValueError without a message, unless amending, and for one holding a NUL."""
    if message is None and not amend:
        raise ValueError("commit needs a message, unless it amends")
    if message is not None and "\0" in message:
        raise ValueError("a message can't hold a NUL")
    switches = {
        "--quiet": quiet,
        "--all": all,
        "--amend": amend,
        "--allow-empty": allow_empty,
        "--no-verify": no_verify,
        "--no-edit": message is None,
    }
    args = (
        "commit",
        *(switch for switch, on in switches.items() if on),
        *(() if message is None else (f"--message={message}",)),
    )
    return _write(git, args, inherit_output=inherit_output, stdout=stdout, env=env, timeout=timeout)


def abort(
    git: Git,
    operation: Literal["merge", "cherry-pick", "revert"],
    *,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Abort the stopped `operation`, putting back what it changed, with one `git <operation>
    --abort`. Raises GitError if there's none to abort; see `merge` for `inherit_output` and
    `stdout`."""
    args = (operation, "--abort")
    return _write(git, args, inherit_output=inherit_output, stdout=stdout, env=env, timeout=timeout)


def skip(
    git: Git,
    operation: Literal["cherry-pick", "revert"],
    *,
    inherit_output: bool = False,
    stdout: Stdout = None,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> CommandOutput:
    """Skip the commit the stopped `operation` stopped at, and go on with the rest, with one
    `git <operation> --skip`. The next commit can stop it again: see `merge` for the
    ConflictError that raises, and for `inherit_output` and `stdout`. Raises GitError if
    there's none to skip."""
    args = (operation, "--skip")
    return _write(
        git,
        args,
        inherit_output=inherit_output,
        stdout=stdout,
        env=env,
        timeout=timeout,
        can_conflict=True,
    )
