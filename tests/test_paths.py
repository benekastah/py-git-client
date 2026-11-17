import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import Git, git_common_dir, git_dir

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def common_dir_git_says(git: Git) -> Path:
    """The common git directory, as git itself finds it."""
    output = git.run_bytes("rev-parse", "--path-format=absolute", "--git-common-dir")
    # Not run's, which would strip a trailing space from the path.
    return Path(os.fsdecode(output.removesuffix(b"\n"))).resolve()


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows can't name a directory with a trailing space"
)
def test_git_common_dir_keeps_a_trailing_space_in_the_paths_it_reads(
    repo: Git, tmp_path: Path
) -> None:
    # git writes the .git file's gitdir, and reads it and commondir up to a line ending alone.
    separate = tmp_path / "separate "
    checkout = tmp_path / "checkout"
    repo.run("init", "--quiet", "--separate-git-dir", str(separate), str(checkout))
    linked_dir = separate / "worktrees" / "linked"
    linked_dir.mkdir(parents=True)
    (linked_dir / "HEAD").write_bytes((separate / "HEAD").read_bytes())
    (linked_dir / "commondir").write_bytes(os.fsencode(separate) + b"\n")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_bytes(b"gitdir: " + os.fsencode(linked_dir) + b"\n")

    assert git_common_dir(checkout) == separate.resolve() == common_dir_git_says(Git(checkout))
    assert git_common_dir(linked) == separate.resolve()


def test_git_common_dir_is_none_for_a_linked_worktree_whose_head_is_gone(
    repo: Git, tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))
    own = Path(Git(linked).run("rev-parse", "--path-format=absolute", "--git-dir"))
    (own / "HEAD").unlink()

    # As git, which says the .git file doesn't point to a valid repository.
    assert git_common_dir(linked) is None


def git_dir_git_says(git: Git) -> Path:
    """The client's own git directory, as git itself finds it."""
    output = git.run_bytes("rev-parse", "--absolute-git-dir")
    return Path(os.fsdecode(output.removesuffix(b"\n"))).resolve()


def test_git_dir_of_a_checkout_and_a_git_directory(repo: Git, tmp_path: Path) -> None:
    top = Path(repo.directory)
    bare = tmp_path / "bare.git"
    repo.run("clone", "--quiet", "--bare", repo.directory, str(bare))

    assert git_dir(top) == (top / ".git").resolve() == git_dir_git_says(repo)
    assert git_dir(top / ".git") == (top / ".git").resolve()
    assert git_dir(bare) == bare.resolve() == git_dir_git_says(Git(bare))


def test_git_dir_of_a_linked_worktree_is_its_own(repo: Git, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))

    own = git_dir_git_says(Git(linked))
    assert own != git_common_dir(linked)
    assert git_dir(linked) == own
    assert git_dir(own) == own


def test_git_dir_of_a_submodule(repo_from: RepoFrom) -> None:
    repo = repo_from("submodule", add_submodule)
    mod = Path(repo.directory) / "mod"

    assert git_dir(mod) == git_dir_git_says(Git(mod))


def test_git_dir_is_none_where_git_common_dir_is(repo: Git, tmp_path: Path) -> None:
    sub = Path(repo.directory) / "sub"
    sub.mkdir()
    (tmp_path / ".git").write_bytes(b"gitdir: no-such-directory\n")

    assert git_dir(sub) is None
    assert git_dir(tmp_path) is None
    assert git_dir(tmp_path / "no-such-directory") is None


def test_git_dir_of_a_checkout_with_a_separate_git_directory(repo: Git, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    separate = tmp_path / "separate"
    repo.run("init", "--quiet", "--separate-git-dir", str(separate), str(checkout))

    assert git_dir(checkout) == separate.resolve() == git_dir_git_says(Git(checkout))
