import os
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import Git, git_common_dir

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def common_dir_git_says(git: Git) -> Path:
    """The common git directory, as git itself finds it."""
    return Path(git.run("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def test_git_common_dir_of_a_checkout_is_its_git_directory(repo: Git) -> None:
    top = Path(repo.directory)

    assert git_common_dir(top) == (top / ".git").resolve() == common_dir_git_says(repo)


def test_git_common_dir_of_a_git_directory_is_itself(repo: Git) -> None:
    dot_git = Path(repo.directory) / ".git"

    assert git_common_dir(dot_git) == dot_git.resolve()


def test_git_common_dir_of_a_linked_worktree_is_the_main_ones(repo: Git, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))
    own = Path(Git(linked).run("rev-parse", "--path-format=absolute", "--git-dir"))

    expected = (Path(repo.directory) / ".git").resolve()
    assert git_common_dir(linked) == expected == common_dir_git_says(Git(linked))
    assert git_common_dir(own) == expected


def test_git_common_dir_of_a_bare_repository_is_itself(repo: Git, tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    repo.run("clone", "--quiet", "--bare", repo.directory, str(bare))

    assert git_common_dir(bare) == bare.resolve() == common_dir_git_says(Git(bare))


def add_submodule(repo: Git) -> None:
    """Make a repository beside repo, and add it to repo as a submodule, `mod`."""
    module = Path(repo.directory).parent / "module"
    repo.run("clone", "--quiet", repo.directory, str(module))
    repo.run("-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(module), "mod")
    repo.run("commit", "--quiet", "-m", "add mod")


def test_git_common_dir_of_a_submodule_is_its_own_git_directory(
    repo_from: RepoFrom,
) -> None:
    repo = repo_from("submodule", add_submodule)
    mod = Path(repo.directory) / "mod"

    expected = (Path(repo.directory) / ".git" / "modules" / "mod").resolve()
    assert git_common_dir(mod) == expected == common_dir_git_says(Git(mod))


def test_git_common_dir_is_none_where_there_is_no_repository(tmp_path: Path) -> None:
    assert git_common_dir(tmp_path) is None
    assert git_common_dir(tmp_path / "no-such-directory") is None


def test_git_common_dir_is_none_below_the_top_of_a_checkout(repo: Git) -> None:
    sub = Path(repo.directory) / "sub"
    sub.mkdir()

    assert git_common_dir(sub) is None


def test_git_common_dir_is_none_for_a_git_file_naming_no_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").write_bytes(b"not a gitdir line\n")
    assert git_common_dir(tmp_path) is None

    (tmp_path / ".git").write_bytes(b"gitdir: no-such-directory\n")
    assert git_common_dir(tmp_path) is None


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_git_common_dir_reads_a_git_file_with_any_line_ending(
    repo: Git, tmp_path: Path, ending: bytes
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    dot_git = Path(repo.directory) / ".git"
    (checkout / ".git").write_bytes(b"gitdir: " + os.fsencode(dot_git) + ending)

    assert git_common_dir(checkout) == dot_git.resolve()
