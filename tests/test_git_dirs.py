import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import (
    Git,
    NotARepositoryError,
    ReadOnlyGit,
    git_common_dir,
    git_dir,
    git_paths,
    git_paths_and_head,
)
from git_client.testing import GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def git_says(git: ReadOnlyGit, option: str) -> Path:
    """What git prints for one rev-parse option, asked on its own, as a path."""
    output = git.run_bytes("rev-parse", "--path-format=absolute", option)
    # Not run's, which would strip a trailing space from the path.
    return Path(os.fsdecode(output.removesuffix(b"\n")))


def assert_as_git_says(git: ReadOnlyGit) -> None:
    """git_dir and git_common_dir give what git gives, each asked on its own."""
    assert git_dir(git) == git_says(git, "--absolute-git-dir")
    assert git_common_dir(git) == git_says(git, "--git-common-dir")


def test_a_checkout(repo: Git) -> None:
    assert_as_git_says(repo)
    assert git_dir(repo) == git_common_dir(repo)


def test_a_git_directory(repo: Git) -> None:
    assert_as_git_says(Git(Path(repo.directory) / ".git"))


def test_a_subdirectory_of_a_checkout(repo: Git) -> None:
    (Path(repo.directory) / "sub").mkdir()
    sub = Git(Path(repo.directory) / "sub")

    assert_as_git_says(sub)
    assert git_dir(sub) == git_dir(repo)


def test_a_linked_worktree_has_its_own_and_the_main_ones(repo: Git, tmp_path: Path) -> None:
    repo.run("worktree", "add", "--quiet", str(tmp_path / "linked"))
    linked = Git(tmp_path / "linked")

    assert_as_git_says(linked)
    assert git_dir(linked) != git_common_dir(linked) == git_common_dir(repo)


def test_a_read_only_client_runs_the_command_itself(repo: Git, tmp_path: Path) -> None:
    repo.run("worktree", "add", "--quiet", str(tmp_path / "linked"))

    assert_as_git_says(ReadOnlyGit(tmp_path / "linked"))


def test_git_dir_in_the_clients_env_wins_as_for_git(repo: Git, tmp_path: Path) -> None:
    other = Git(tmp_path, env={"GIT_DIR": str(Path(repo.directory) / ".git")})

    assert_as_git_says(other)
    assert git_dir(other) == git_dir(repo)


def test_a_bare_repository(repo: Git, tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    repo.run("clone", "--quiet", "--bare", repo.directory, str(bare))

    assert_as_git_says(Git(bare))


def add_submodule(repo: Git) -> None:
    """Make a repository beside repo, and add it to repo as a submodule, `mod`."""
    module = Path(repo.directory).parent / "module"
    repo.run("clone", "--quiet", repo.directory, str(module))
    repo.run("-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(module), "mod")
    repo.run("commit", "--quiet", "-m", "add mod")


def test_a_submodule(repo_from: RepoFrom) -> None:
    assert_as_git_says(Git(Path(repo_from("submodule", add_submodule).directory) / "mod"))


# With a trailing space, which the path git prints keeps, where the system allows one.
SEPARATE = "separate" if sys.platform == "win32" else "separate "


def test_a_checkout_with_a_separate_git_directory(repo: Git, tmp_path: Path) -> None:
    checkout = Git(tmp_path / "checkout")
    repo.run("init", "--quiet", "--separate-git-dir", str(tmp_path / SEPARATE), checkout.directory)

    assert_as_git_says(checkout)
    assert git_dir(checkout).name == SEPARATE


def test_a_checkout_reached_through_a_symbolic_link(repo: Git, tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to(repo.directory, target_is_directory=True)
    except OSError:  # Windows without the privilege
        pytest.skip("this system can't make a symbolic link")

    assert_as_git_says(Git(link))


@pytest.mark.usefixtures("outside_any_repository")
def test_a_directory_that_isnt_a_repository_raises_gits_error(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        git_dir(Git(tmp_path))
    with pytest.raises(NotARepositoryError):
        git_common_dir(Git(tmp_path))


@pytest.mark.parametrize("first", [git_dir, git_common_dir])
def test_one_process_answers_both_once(
    repo: Git, git_calls: list[GitCall], first: Callable[[ReadOnlyGit], Path]
) -> None:
    first(repo)
    git_dir(repo)
    git_common_dir(repo)

    assert len(git_calls) == 1


@pytest.mark.parametrize("first", [Git.prefix, git_paths, git_paths_and_head])
def test_the_rev_parse_of_where_things_are_tells_both(
    repo: Git, git_calls: list[GitCall], first: Callable[..., object]
) -> None:
    first(repo, "HEAD") if first is not Git.prefix else repo.prefix()
    started = len(git_calls)

    assert git_dir(repo) == git_common_dir(repo) == Path(repo.directory) / ".git"
    assert len(git_calls) == started


def test_git_dir_tells_where_things_are_in_the_same_process(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git_dir(repo)

    assert (repo.prefix(), repo.cdup(), repo.object_id_length()) == ("", "", 40)
    assert git_paths(repo, "HEAD") == [Path(repo.directory) / ".git" / "HEAD"]
    assert len(git_calls) == 2  # and the path, which git_dir doesn't ask


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' file names can't hold a newline")
@pytest.mark.parametrize("first", [git_dir, git_common_dir])
def test_a_git_directory_whose_path_holds_a_newline(
    repo: Git, tmp_path: Path, first: Callable[[ReadOnlyGit], Path]
) -> None:
    checkout = Git(tmp_path / "checkout")
    separate = tmp_path / "g\ntrue"
    repo.run("init", "--quiet", "--separate-git-dir", str(separate), checkout.directory)

    first(checkout)
    assert (git_dir(checkout), git_common_dir(checkout)) == (separate, separate)
    assert (checkout.prefix(), checkout.cdup()) == ("", "")


def test_a_client_asks_again_once_it_points_elsewhere(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    repo.run("worktree", "add", "--quiet", str(tmp_path / "linked"))
    main = git_dir(repo)
    git_calls.clear()
    repo.directory = str(tmp_path / "linked")

    assert git_dir(repo) != main
    assert len(git_calls) == 1


def test_a_read_only_copy_shares_the_answers(repo: Git, git_calls: list[GitCall]) -> None:
    common = git_common_dir(repo)

    assert git_common_dir(repo.read_only()) == common
    assert len(git_calls) == 1
