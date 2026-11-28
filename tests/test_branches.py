from collections.abc import Callable

import pytest

from git_client import (
    Git,
    GitError,
    create_branch,
    delete_branches,
    rename_branch,
    set_upstream,
    valid_branch_name,
)
from git_client.testing import FakeGit, GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def make_branches(git: Git) -> None:
    """On make_repo's main: `merged` at main, with config of its own, `ahead` a commit past
    it, and `previous`, checked out before main (so `@{-1}` names it); a remote, `origin`
    (never fetched), with `topic`, at main's commit."""
    ahead = git.run("commit-tree", "-p", "main", "-m", "ahead", "main^{tree}")
    git.run("branch", "merged")
    git.run("config", "branch.merged.note", "kept until deleted")
    git.run("branch", "ahead", ahead)
    git.run("switch", "--quiet", "--create", "previous")
    git.run("switch", "--quiet", "main")
    git.run("remote", "add", "origin", "../nowhere")
    git.run("update-ref", "refs/remotes/origin/topic", "main")


@pytest.fixture
def branches(repo_from: RepoFrom) -> Git:
    return repo_from("branches", make_branches)


def tip(git: Git, branch: str) -> str | None:
    return git.resolve(f"refs/heads/{branch}")


def upstream(git: Git, branch: str) -> list[str]:
    """branch's remote and merge config, as set."""
    keys = (f"branch.{branch}.remote", f"branch.{branch}.merge")
    return [git.run("config", "--default", "", key) for key in keys]


def test_delete_branches_deletes_all_with_one_process(
    branches: Git, git_calls: list[GitCall]
) -> None:
    merged, ahead = tip(branches, "merged"), tip(branches, "ahead")

    result = delete_branches(branches, ["ahead", "merged"], force=True)

    assert result.deleted == {"ahead": ahead, "merged": merged}  # full ids, each its own
    assert result.returncode == 0
    calls = [call[:4] for call in git_calls if call[0] != "cat-file"]  # but tip()'s
    assert calls == [["-c", "core.abbrev=no", "branch", "-D"]]
    assert tip(branches, "ahead") is None and tip(branches, "merged") is None
    assert not branches.ok("config", "branch.merged.note")  # git drops its config too


def test_delete_branches_deletes_what_git_can_and_says_why_not(branches: Git) -> None:
    merged = tip(branches, "merged")

    result = delete_branches(branches, ["missing", "ahead", "merged"])

    assert result.deleted == {"merged": merged}
    assert result.returncode == 1
    assert "'missing' not found" in result.stderr
    assert "'ahead' is not fully merged" in result.stderr.replace("The branch", "the branch")
    assert tip(branches, "ahead") is not None


def test_delete_branches_reads_names_as_names(branches: Git) -> None:
    # Read as an option, "-D" would force ahead's deletion.
    result = delete_branches(branches, ["ahead", "-D"])

    assert result.deleted == {}
    assert result.returncode == 1
    assert tip(branches, "ahead") is not None


def test_delete_branches_of_none_starts_nothing(repo: Git, git_calls: list[GitCall]) -> None:
    assert delete_branches(repo, []).deleted == {}
    assert git_calls == []


def test_delete_branches_refuses_a_name_with_a_line_ending(repo: Git) -> None:
    with pytest.raises(ValueError, match="line ending"):
        delete_branches(repo, ["a\nDeleted branch main (was 1)."])


def test_delete_branches_raises_on_a_fatal_error() -> None:
    git = FakeGit()
    git.respond(
        ["-c", "core.abbrev=no", "branch"], stderr="fatal: not a git repository\n", returncode=128
    )
    with pytest.raises(GitError, match="not a git repository"):
        delete_branches(git, ["a"])


@pytest.mark.parametrize("line", ["Deleted branch a.", "Deleted branch a (was 1)", "a"])
def test_delete_branches_raises_on_output_git_doesnt_print(line: str) -> None:
    git = FakeGit()
    git.respond(["-c", "core.abbrev=no", "branch"], stdout=line + "\n")
    with pytest.raises(GitError, match="printed"):
        delete_branches(git, ["a"])


def test_create_branch_at_a_start_point_or_head(branches: Git, git_calls: list[GitCall]) -> None:
    create_branch(branches, "new", "ahead")
    create_branch(branches, "here")

    assert tip(branches, "new") == tip(branches, "ahead")
    assert tip(branches, "here") == tip(branches, "main")
    assert [call[:2] for call in git_calls if call[0] != "cat-file"] == [["branch", "--quiet"]] * 2


def test_create_branch_refuses_an_existing_branch_unless_forced(branches: Git) -> None:
    with pytest.raises(GitError, match="already exists"):
        create_branch(branches, "merged", "ahead")
    create_branch(branches, "merged", "ahead", force=True)
    assert tip(branches, "merged") == tip(branches, "ahead")


def test_create_branch_tracks_as_asked(branches: Git) -> None:
    branches.run("config", "branch.autoSetupMerge", "always")
    create_branch(branches, "tracking", "origin/topic", track=True)
    create_branch(branches, "inheriting", "tracking", track="inherit")
    create_branch(branches, "untracked", "origin/topic", track=False)
    create_branch(branches, "by-config", "origin/topic")

    assert upstream(branches, "tracking") == ["origin", "refs/heads/topic"]
    assert upstream(branches, "inheriting") == ["origin", "refs/heads/topic"]
    assert upstream(branches, "untracked") == ["", ""]
    assert upstream(branches, "by-config") == ["origin", "refs/heads/topic"]


def test_create_branch_reads_names_as_names(branches: Git) -> None:
    with pytest.raises(GitError, match="'--force' is not a valid branch name"):
        create_branch(branches, "--force", "main")


def test_rename_branch_moves_its_config(branches: Git) -> None:
    merged = tip(branches, "merged")
    rename_branch(branches, "merged", "renamed")

    assert tip(branches, "renamed") == merged and tip(branches, "merged") is None
    assert branches.run("config", "branch.renamed.note") == "kept until deleted"


def test_rename_branch_refuses_an_existing_name_unless_forced(branches: Git) -> None:
    ahead = tip(branches, "ahead")
    with pytest.raises(GitError, match="already exists"):
        rename_branch(branches, "ahead", "merged")
    rename_branch(branches, "ahead", "merged", force=True)
    assert tip(branches, "merged") == ahead and tip(branches, "ahead") is None


def test_set_upstream_and_unset_it(branches: Git, git_calls: list[GitCall]) -> None:
    set_upstream(branches, "merged", "origin/topic")
    assert upstream(branches, "merged") == ["origin", "refs/heads/topic"]
    set_upstream(branches, "merged", None)
    assert upstream(branches, "merged") == ["", ""]
    assert [call[:2] for call in git_calls if call[0] == "branch"] == [
        ["branch", "--quiet"],
        ["branch", "--unset-upstream"],
    ]


def test_set_upstream_raises_as_git_refuses(branches: Git) -> None:
    with pytest.raises(GitError, match="has no upstream"):
        set_upstream(branches, "ahead", None)
    with pytest.raises(GitError, match="'missing' does not exist"):
        set_upstream(branches, "missing", "origin/topic")


@pytest.mark.parametrize("name", ["good", "a/b", "@"])
def test_valid_branch_name(branches: Git, name: str, git_calls: list[GitCall]) -> None:
    assert valid_branch_name(branches.read_only(), name)
    assert [call[:2] for call in git_calls] == [["check-ref-format", "--branch"]]


# "@{-1}" names a branch (previous), but isn't one's name.
@pytest.mark.parametrize("name", ["a..b", "HEAD", "-x", "", "a b", "x/", "@{-1}", "@{u}"])
def test_invalid_branch_name(branches: Git, name: str) -> None:
    assert not valid_branch_name(branches, name)


def test_valid_branch_name_raises_when_git_fails_otherwise() -> None:
    git = FakeGit()
    git.respond(["check-ref-format"], stderr="usage: git check-ref-format\n", returncode=129)
    with pytest.raises(GitError, match="usage"):
        valid_branch_name(git, "a")
