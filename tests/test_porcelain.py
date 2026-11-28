"""The porcelain writes, against real git: what each switch does, and how a stop or a failure
is raised."""

import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import (
    CommandOutput,
    ConflictError,
    Git,
    GitError,
    UnknownRevisionError,
    abort,
    cherry_pick,
    commit,
    merge,
    rebase,
    reset,
    revert,
    skip,
    switch,
)
from git_client.testing import FakeGit, GitCall

RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def branches(git: Git) -> None:
    """From main (a: "a"): b changes a to "b", c adds c, ahead (on main) adds e; then main
    changes a to "m". So b conflicts with main, c doesn't, and ahead is main's fast-forward."""
    root = Path(git.directory)
    for branch, path, text in (("b", "a", "b\n"), ("c", "c", "c\n")):
        git.run("switch", "--quiet", "-c", branch, "main")
        (root / path).write_bytes(text.encode())
        git.run("add", path)
        git.run("commit", "--quiet", "-m", branch)
    git.run("switch", "--quiet", "main")
    (root / "a").write_bytes(b"m\n")
    git.run("commit", "--quiet", "-am", "m")
    git.run("switch", "--quiet", "-c", "ahead")
    (root / "e").write_bytes(b"e\n")
    git.run("add", "e")
    git.run("commit", "--quiet", "-m", "e")
    git.run("switch", "--quiet", "main")


@pytest.fixture
def repo(repo_from: RepoFrom) -> Git:
    return repo_from("porcelain", branches)


def head(git: Git) -> str:
    """HEAD's branch, or its commit when detached."""
    result = git.process("symbolic-ref", "-q", "HEAD", capture_output=True)
    return git.run("rev-parse", "HEAD") if result.returncode else result.stdout.decode().strip()


def text(git: Git, path: str) -> str:
    return (Path(git.directory) / path).read_text(encoding="utf-8")


def hook(git: Git, name: str, script: str) -> None:
    """Install a hook; hooks run in git's sh on every system."""
    path = Path(git.directory) / ".git" / "hooks" / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)


# switch


def test_switch_moves_head_to_a_branch(repo: Git) -> None:
    result = switch(repo, "b")

    assert head(repo) == "refs/heads/b"
    assert text(repo, "a") == "b\n"
    assert result.stderr == "Switched to branch 'b'\n"


def test_switch_quiet_prints_nothing(repo: Git) -> None:
    assert switch(repo, "b", quiet=True) == CommandOutput("", "")


def test_switch_detach_checks_out_a_commit(repo: Git) -> None:
    switch(repo, "b", detach=True)

    assert head(repo) == repo.run("rev-parse", "b")


def test_switch_creates_a_branch_at_a_start_point(repo: Git) -> None:
    switch(repo, "new", create=True, start_point="c")

    assert head(repo) == "refs/heads/new"
    assert repo.run("rev-parse", "new") == repo.run("rev-parse", "c")


@pytest.mark.parametrize(
    ("setting", "track", "upstream"),
    [("always", False, ""), ("false", True, "refs/heads/c"), ("false", None, "")],
)
def test_switch_track_sets_the_new_branchs_upstream(
    repo: Git, setting: str, track: bool | None, upstream: str
) -> None:
    repo.run("config", "branch.autoSetupMerge", setting)

    switch(repo, "new", create=True, start_point="c", track=track)

    assert repo.run("config", "--default", "", "branch.new.merge") == upstream


def test_switch_reads_a_name_like_an_option_as_a_name(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        switch(repo, "--orphan=x")
    assert head(repo) == "refs/heads/main"


def test_switch_refuses_to_lose_changes_unless_forced(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    with pytest.raises(GitError) as raised:
        switch(repo, "b")
    assert type(raised.value) is GitError
    assert "would be overwritten by checkout" in raised.value.stderr
    assert head(repo) == "refs/heads/main"

    switch(repo, "b", force=True)
    assert text(repo, "a") == "b\n"


def test_switch_keeps_an_untracked_file_in_the_way(repo: Git) -> None:
    (Path(repo.directory) / "c").write_bytes(b"mine\n")

    with pytest.raises(GitError, match="untracked working tree files would be overwritten"):
        switch(repo, "c")
    assert text(repo, "c") == "mine\n"


def test_switch_runs_the_post_checkout_hook(repo: Git) -> None:
    hook(repo, "post-checkout", 'echo "$3" > checked-out')

    switch(repo, "b")

    assert text(repo, "checked-out") == "1\n"


def test_switch_from_an_unborn_branch(repo: Git) -> None:
    repo.run("switch", "--quiet", "--orphan", "unborn")

    switch(repo, "c")

    assert head(repo) == "refs/heads/c"


# reset


@pytest.mark.parametrize(
    ("mode", "index", "worktree"),
    [("soft", "m\n", "m\n"), ("mixed", "a\n", "m\n"), ("hard", "a\n", "a\n")],
)
def test_reset_modes_move_the_index_and_worktree(
    repo: Git, mode: str, index: str, worktree: str
) -> None:
    parent = repo.run("rev-parse", "main~1")

    reset(repo, "main~1", mode=mode)  # pyright: ignore[reportArgumentType]

    assert repo.run("rev-parse", "HEAD") == parent
    assert repo.run("show", ":a", strip=False) == index
    assert text(repo, "a") == worktree


def test_reset_defaults_to_mixed_at_head(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")
    repo.run("add", "a")

    assert reset(repo, quiet=True) == CommandOutput("", "")

    assert repo.run("show", ":a", strip=False) == "m\n"
    assert text(repo, "a") == "changed\n"


def test_reset_keep_refuses_to_lose_a_change(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    with pytest.raises(GitError, match="not uptodate"):
        reset(repo, "b", mode="keep")
    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "main")

    reset(repo, "ahead", mode="keep")
    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "ahead")
    assert text(repo, "a") == "changed\n"


def test_reset_merge_keeps_an_unstaged_change(repo: Git) -> None:
    (Path(repo.directory) / "e").write_bytes(b"mine\n")
    repo.run("add", "e")
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    reset(repo, "ahead", mode="merge")

    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "ahead")
    assert text(repo, "a") == "changed\n"
    assert text(repo, "e") == "e\n"


def test_reset_reads_a_commit_that_is_also_a_path_as_a_commit(repo: Git) -> None:
    repo.run("branch", "a", "main~1")

    reset(repo, "a", mode="hard")

    assert text(repo, "a") == "a\n"


def test_reset_refuses_a_commit_like_an_option(repo: Git) -> None:
    with pytest.raises(ValueError, match="-q"):
        reset(repo, "-q")


# merge


def test_merge_fast_forwards_only(repo: Git) -> None:
    assert merge(repo, "ahead", ff="only", quiet=True) == CommandOutput("", "")
    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "ahead")

    with pytest.raises(GitError, match="Not possible to fast-forward"):
        merge(repo, "c", ff="only")


def test_merge_no_ff_makes_a_merge_commit_with_the_message(repo: Git) -> None:
    merge(repo, "ahead", ff="no", message="Merge ahead")

    assert repo.run("rev-parse", "HEAD^2") == repo.run("rev-parse", "ahead")
    assert repo.run("log", "-1", "--format=%B") == "Merge ahead"


def test_merge_never_opens_an_editor_unless_asked(repo: Git, tmp_path: Path) -> None:
    edited = tmp_path / "edited"
    editor = f'echo edited > "{edited.as_posix()}" #'

    merge(repo, "c", env={"GIT_EDITOR": editor, "GIT_MERGE_AUTOEDIT": "yes"})
    assert not edited.exists()
    assert repo.run("log", "-1", "--format=%s") == "Merge branch 'c'"

    repo.run("reset", "--quiet", "--hard", "HEAD^")
    merge(
        repo,
        "c",
        edit=True,
        inherit_output=True,
        stdout=subprocess.DEVNULL,
        env={"GIT_EDITOR": editor},
    )
    assert edited.exists()


@pytest.mark.parametrize("write", [merge, revert])
def test_edit_needs_inherit_output(repo: Git, write: Callable[..., CommandOutput]) -> None:
    # Else a terminal editor (e.g. vim) would wait for a terminal, forever without a timeout.
    with pytest.raises(ValueError, match="inherit_output"):
        write(repo, "c", edit=True)


def test_merge_conflict_raises_conflict_error(repo: Git) -> None:
    with pytest.raises(ConflictError) as raised:
        merge(repo, "b")

    assert "CONFLICT (content): Merge conflict in a" in raised.value.stdout
    assert repo.run("ls-files", "--unmerged")
    abort(repo, "merge")
    assert not repo.run("ls-files", "--unmerged")
    assert text(repo, "a") == "m\n"


def test_merge_keeps_an_untracked_file_in_the_way(repo: Git) -> None:
    (Path(repo.directory) / "c").write_bytes(b"mine\n")

    with pytest.raises(GitError) as raised:
        merge(repo, "c")
    assert type(raised.value) is GitError
    assert text(repo, "c") == "mine\n"


def test_merge_of_nothing_named_raises_unknown_revision(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        merge(repo, "nothing")


def test_merge_reads_a_name_like_an_option_as_a_name(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        merge(repo, "--abort")


def test_merge_refused_by_its_hook_is_an_error(repo: Git) -> None:
    hook(repo, "pre-merge-commit", "echo no >&2; exit 1")

    with pytest.raises(GitError) as raised:
        merge(repo, "c")
    assert type(raised.value) is GitError
    assert raised.value.stderr.startswith("no\n")


# cherry-pick, revert, abort and skip


def test_cherry_pick_applies_each_commit(repo: Git) -> None:
    cherry_pick(repo, "c", "ahead")

    assert repo.run("log", "--format=%s", "-3") == "e\nc\nm"
    assert repo.run("log", "-1", "--format=%b") == ""


def test_cherry_pick_record_origin_adds_the_line(repo: Git) -> None:
    cherry_pick(repo, "c", record_origin=True)

    assert repo.run("log", "-1", "--format=%b") == (
        f"(cherry picked from commit {repo.run('rev-parse', 'c')})"
    )


def test_cherry_pick_ff_fast_forwards(repo: Git) -> None:
    # A later date than the template's commits', or git would make the same commit again.
    cherry_pick(repo, "ahead", ff=True, env={"GIT_COMMITTER_DATE": "@4000000000 +0000"})
    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "ahead")


def test_cherry_pick_without_ff_makes_a_new_commit(repo: Git) -> None:
    # A later date than the template's commits', or git would make the same commit again.
    cherry_pick(repo, "ahead", env={"GIT_COMMITTER_DATE": "@4000000000 +0000"})
    assert repo.run("rev-parse", "HEAD") != repo.run("rev-parse", "ahead")
    assert repo.run("rev-parse", "HEAD^") == repo.run("rev-parse", "main@{1}")


def test_cherry_pick_allow_empty_keeps_an_empty_commit(repo: Git) -> None:
    repo.run("commit", "--quiet", "--allow-empty", "-m", "empty")
    empty = repo.run("rev-parse", "HEAD")
    repo.run("reset", "--quiet", "--hard", "HEAD^")

    with pytest.raises(GitError):
        cherry_pick(repo, empty)
    skip(repo, "cherry-pick")
    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "main")

    cherry_pick(repo, empty, allow_empty=True)
    assert repo.run("log", "-1", "--format=%s") == "empty"


def test_cherry_pick_conflict_raises_and_skip_goes_on(repo: Git) -> None:
    with pytest.raises(ConflictError):
        cherry_pick(repo, "b", "c")

    skip(repo, "cherry-pick")

    assert repo.run("log", "--format=%s", "-2") == "c\nm"


def test_abort_puts_back_what_a_cherry_pick_changed(repo: Git) -> None:
    with pytest.raises(ConflictError):
        cherry_pick(repo, "c", "b")

    abort(repo, "cherry-pick")

    assert repo.run("rev-parse", "HEAD") == repo.run("rev-parse", "main")


def test_abort_with_nothing_to_abort_raises(repo: Git) -> None:
    with pytest.raises(GitError, match="no cherry-pick or revert in progress"):
        abort(repo, "cherry-pick")


@pytest.mark.parametrize("write", [cherry_pick, revert])
def test_cherry_pick_and_revert_refuse_a_commit_like_an_option(
    repo: Git, write: Callable[..., CommandOutput]
) -> None:
    with pytest.raises(ValueError, match="--quit"):
        write(repo, "c", "--quit")
    with pytest.raises(ValueError, match="no commits"):
        write(repo)


def test_revert_makes_the_reverting_commit_without_an_editor(repo: Git, tmp_path: Path) -> None:
    edited = tmp_path / "edited"
    editor = f'echo edited > "{edited.as_posix()}" #'

    revert(repo, "HEAD", env={"GIT_EDITOR": editor})

    assert not edited.exists()
    assert repo.run("log", "-1", "--format=%s") == 'Revert "m"'
    assert text(repo, "a") == "a\n"

    revert(
        repo,
        "HEAD",
        edit=True,
        inherit_output=True,
        stdout=subprocess.DEVNULL,
        env={"GIT_EDITOR": editor},
    )
    assert edited.exists()


def test_revert_conflict_raises_conflict_error(repo: Git) -> None:
    repo.run("switch", "--quiet", "b")
    (Path(repo.directory) / "a").write_bytes(b"later\n")
    repo.run("commit", "--quiet", "-am", "later")

    with pytest.raises(ConflictError):
        revert(repo, "HEAD~1")
    abort(repo, "revert")
    assert text(repo, "a") == "later\n"


# commit


def test_commit_all_commits_tracked_changes_alone(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")
    (Path(repo.directory) / "new").write_bytes(b"untracked\n")

    assert commit(repo, "WIP", all=True, quiet=True) == CommandOutput("", "")

    assert repo.run("log", "-1", "--format=%B") == "WIP"
    assert repo.run("show", "HEAD:a", strip=False) == "changed\n"
    assert repo.run("status", "--porcelain") == "?? new"


def test_commit_of_nothing_raises_unless_empty_allowed(repo: Git) -> None:
    with pytest.raises(GitError) as raised:
        commit(repo, "nothing")
    assert "nothing to commit" in raised.value.stdout

    commit(repo, "empty", allow_empty=True)
    assert repo.run("log", "-1", "--format=%s") == "empty"


def test_commit_amend_keeps_or_replaces_the_message(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")
    repo.run("add", "a")

    commit(repo, amend=True, env={"GIT_EDITOR": "false"})  # which would fail it
    assert repo.run("log", "-1", "--format=%s") == "m"
    assert repo.run("rev-parse", "HEAD^") == repo.run("rev-parse", "main@{1}^")

    commit(repo, "reworded", amend=True)
    assert repo.run("log", "-1", "--format=%s") == "reworded"


def test_commit_needs_a_message_unless_amending(repo: Git) -> None:
    with pytest.raises(ValueError, match="message"):
        commit(repo)


@pytest.mark.parametrize("name", ["pre-commit", "commit-msg"])
def test_commit_refused_by_a_hook_is_an_error_unless_no_verify(repo: Git, name: str) -> None:
    hook(repo, name, "exit 1")

    with pytest.raises(GitError):
        commit(repo, "hooked", allow_empty=True)
    assert repo.run("log", "-1", "--format=%s") == "m"

    commit(repo, "hooked", allow_empty=True, no_verify=True)
    assert repo.run("log", "-1", "--format=%s") == "hooked"


def test_commit_message_like_an_option_is_the_message(repo: Git) -> None:
    commit(repo, "--amend", allow_empty=True)
    assert repo.run("log", "-1", "--format=%s") == "--amend"


def test_commit_on_an_unborn_branch(repo: Git) -> None:
    repo.run("switch", "--quiet", "--orphan", "unborn")
    (Path(repo.directory) / "a").write_bytes(b"root\n")
    repo.run("add", "a")

    commit(repo, "root")

    assert repo.run("rev-list", "--count", "unborn") == "1"


def test_commit_in_detached_head(repo: Git) -> None:
    repo.run("switch", "--quiet", "--detach", "main")

    commit(repo, "detached", allow_empty=True)

    assert repo.run("rev-parse", "HEAD^") == repo.run("rev-parse", "main")


# inherit_output


def test_inherit_output_leaves_nothing_captured(repo: Git, tmp_path: Path) -> None:
    printed = tmp_path / "printed"
    with printed.open("wb") as stdout:
        result = commit(repo, "shown", allow_empty=True, inherit_output=True, stdout=stdout)

    assert result == CommandOutput(None, None)
    assert "shown" in printed.read_text(encoding="utf-8")


def test_stdout_needs_inherit_output(repo: Git) -> None:
    with pytest.raises(ValueError, match="inherit_output"):
        switch(repo, "b", stdout=io.BytesIO())


def test_inherited_conflict_raises_conflict_error(repo: Git) -> None:
    # From a subdirectory, the conflict in the one above it.
    (Path(repo.directory) / "sub").mkdir()
    with pytest.raises(ConflictError) as raised:
        merge(
            Git(Path(repo.directory) / "sub"), "b", inherit_output=True, stdout=subprocess.DEVNULL
        )

    assert raised.value.returncode == 1
    assert raised.value.stdout == raised.value.stderr == ""


def test_inherited_skip_that_stops_at_the_next_conflict_raises_conflict_error(repo: Git) -> None:
    repo.run("switch", "--quiet", "-c", "b2", "b")
    (Path(repo.directory) / "a").write_bytes(b"b2\n")
    repo.run("commit", "--quiet", "-am", "b2")
    repo.run("switch", "--quiet", "main")
    with pytest.raises(ConflictError):
        cherry_pick(repo, "b", "b2", inherit_output=True, stdout=subprocess.DEVNULL)

    with pytest.raises(ConflictError):
        skip(repo, "cherry-pick", inherit_output=True, stdout=subprocess.DEVNULL)
    assert repo.run("rev-parse", "CHERRY_PICK_HEAD") == repo.run("rev-parse", "b2")


def test_inherited_stop_without_a_conflict_raises_git_error(repo: Git) -> None:
    hook(repo, "pre-merge-commit", "exit 1")

    with pytest.raises(GitError) as raised:
        merge(repo, "c", inherit_output=True, stdout=subprocess.DEVNULL)

    assert type(raised.value) is GitError
    assert raised.value.returncode == 1


def test_inherited_refusal_over_an_earlier_conflict_raises_git_error(repo: Git) -> None:
    with pytest.raises(ConflictError):
        merge(repo, "b")

    with pytest.raises(GitError) as raised:
        merge(repo, "c", inherit_output=True, stdout=subprocess.DEVNULL)

    assert type(raised.value) is GitError
    assert raised.value.returncode == 128


# Through the client's process


def test_each_write_starts_one_process_through_the_client(
    repo: Git, git_calls: list[GitCall]
) -> None:
    switch(repo, "b", quiet=True)

    assert git_calls == [GitCall(["switch", "--quiet", "--end-of-options", "b"], repo.program)]


def test_fake_git_answers_a_write() -> None:
    git = FakeGit()
    git.respond(["merge"], stdout="CONFLICT (content): Merge conflict in a\n", returncode=1)

    with pytest.raises(ConflictError):
        merge(git, "b")
    assert git.commands == [["merge", "--no-edit", "--end-of-options", "b"]]


def test_fake_git_answers_an_inherited_stop_with_the_unmerged_paths() -> None:
    git = FakeGit()
    git.respond(["cherry-pick"], returncode=1)
    git.respond(["ls-files", "--unmerged", "-z", "--", ":/"], stdout="100644 x 1\ta\0")

    with pytest.raises(ConflictError):
        cherry_pick(git, "b", inherit_output=True)


# rebase


def chain(git: Git) -> None:
    """branches(), then x on main and y on x, each adding a file of its name."""
    branches(git)
    for branch in ("x", "y"):
        git.run("switch", "--quiet", "-c", branch)
        (Path(git.directory) / branch).write_bytes(f"{branch}\n".encode())
        git.run("add", branch)
        git.run("commit", "--quiet", "-m", branch)
    git.run("switch", "--quiet", "main")


@pytest.fixture
def chained(repo_from: RepoFrom) -> Git:
    return repo_from("porcelain-chain", chain)


def subjects(git: Git, revision: str) -> str:
    return git.run("log", "--format=%s", revision, "--")


def rebasing(git: Git) -> bool:
    return Path(
        git.run("rev-parse", "--path-format=absolute", "--git-path", "rebase-merge")
    ).exists()


def test_rebase_onto_moves_the_branch(chained: Git) -> None:
    rebase(chained, "x", "y", onto="ahead")

    assert subjects(chained, "y") == "y\ne\nm\nfirst"
    assert head(chained) == "refs/heads/y"


@pytest.mark.needs_git(2, 38, reason="--update-refs")
def test_rebase_update_refs_moves_the_branches_between(chained: Git) -> None:
    rebase(chained, "main", "y", onto="ahead", update_refs=True)

    assert subjects(chained, "x") == "x\ne\nm\nfirst"


def test_rebase_without_update_refs_leaves_them_whatever_the_config(chained: Git) -> None:
    chained.run("config", "rebase.updateRefs", "true")
    x = chained.run("rev-parse", "x")

    rebase(chained, "main", "y", onto="ahead", update_refs=False)

    assert chained.run("rev-parse", "x") == x
    assert subjects(chained, "y") == "y\nx\ne\nm\nfirst"


def test_rebase_update_refs_raises_before_git_2_38(
    chained: Git, git_version: tuple[int, int, int]
) -> None:
    if git_version >= (2, 38):
        pytest.skip("git 2.38 has --update-refs")
    with pytest.raises(GitError):
        rebase(chained, "main", "y", onto="ahead", update_refs=True)


def test_rebase_conflict_raises_conflict_error(repo: Git) -> None:
    with pytest.raises(ConflictError) as raised:
        rebase(repo, "main", "b")

    assert "CONFLICT (content): Merge conflict in a" in raised.value.stdout
    assert rebasing(repo)


def test_inherited_rebase_conflict_raises_conflict_error(repo: Git) -> None:
    with pytest.raises(ConflictError):
        rebase(repo, "main", "b", inherit_output=True, stdout=subprocess.DEVNULL)
    assert rebasing(repo)


def test_rebase_refuses_a_dirty_worktree(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    with pytest.raises(GitError) as raised:
        rebase(repo, "main", "b")
    assert type(raised.value) is GitError
    assert not rebasing(repo)


def test_rebase_refused_by_its_hook_is_an_error(repo: Git) -> None:
    hook(repo, "pre-rebase", "exit 1")

    with pytest.raises(GitError, match="pre-rebase hook refused"):
        rebase(repo, "main", "c")


def test_rebase_reads_names_like_options_as_names(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        rebase(repo, "--root")
    with pytest.raises(GitError):
        rebase(repo, "main", "--root")
    assert not rebasing(repo)
    assert head(repo) == "refs/heads/main"


@pytest.mark.parametrize("abbreviate", ["false", "true"])
def test_rebase_stops_at_a_commit_to_edit(chained: Git, abbreviate: str) -> None:
    chained.run("config", "rebase.abbreviateCommands", abbreviate)
    chained.run("config", "rebase.autoSquash", "true")  # which it doesn't
    chained.run("switch", "--quiet", "y")
    chained.run("commit", "--quiet", "--allow-empty", "-m", "fixup! x")
    x = chained.run("rev-parse", "x")

    rebase(chained, "main", stop_at=[x, x])

    assert rebasing(chained)
    assert chained.run("rev-parse", "HEAD") == x
    chained.run("commit", "--quiet", "--amend", "-m", "x amended")
    chained.run("rebase", "--continue")
    assert subjects(chained, "y") == "fixup! x\ny\nx amended\nm\nfirst"


def test_rebase_root_stops_at_the_root(chained: Git) -> None:
    root = chained.run("rev-list", "--max-parents=0", "HEAD")

    rebase(chained, None, stop_at=[root])

    assert chained.run("rev-parse", "HEAD") == root
    assert rebasing(chained)


def test_rebase_stop_at_a_commit_outside_it_raises(chained: Git) -> None:
    with pytest.raises(GitError):
        rebase(chained, "main", "y", stop_at=[chained.run("rev-parse", "c")])

    assert not rebasing(chained)
    assert subjects(chained, "y") == "y\nx\nm\nfirst"


def test_rebase_stop_at_needs_full_ids(chained: Git) -> None:
    with pytest.raises(ValueError, match="full"):
        rebase(chained, "main", stop_at=["x"])


def test_rebase_argv(git_calls: list[GitCall], chained: Git) -> None:
    rebase(chained, "x", "y", onto="ahead", update_refs=False)

    assert git_calls == [
        GitCall(
            [
                "-c",
                "rebase.updateRefs=false",
                "rebase",
                "--onto=ahead",
                "--end-of-options",
                "x",
                "y",
            ],
            chained.program,
        )
    ]
