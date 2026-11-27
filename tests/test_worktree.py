import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import (
    Changed,
    Git,
    GitError,
    Ignored,
    ReadOnlyGit,
    Renamed,
    StatusBranch,
    SubmoduleState,
    Unmerged,
    Untracked,
    status,
)

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def change_each_kind(git: Git) -> None:
    """A staged rename, an unstaged change, a staged new file, untracked and ignored files."""
    top = Path(git.directory)
    (top / "b").write_bytes(b"b\n" * 10)
    git.run("add", "b")
    git.run("commit", "--quiet", "-m", "b")
    git.run("mv", "b", "moved")
    (top / "a").write_bytes(b"changed\n")
    (top / "café").write_bytes(b"new\n")
    git.run("add", "café")
    (top / ".gitignore").write_bytes(b"*.log\n")
    (top / "dir").mkdir()
    (top / "dir" / "x").write_bytes(b"x\n")
    (top / "ig.log").write_bytes(b"ignored\n")


@pytest.fixture
def changed(repo_from: RepoFrom) -> Git:
    return repo_from("each-kind-of-change", change_each_kind)


def test_status_reports_each_kind_of_entry_from_the_top(changed: Git) -> None:
    (Path(changed.directory) / "sub").mkdir()

    found = status(
        ReadOnlyGit(Path(changed.directory) / "sub"), untracked="all", ignored="matching"
    )

    assert sorted(found.entries, key=lambda e: e.path) == [
        Untracked(".gitignore"),
        Changed("a", ".", "M", None),
        Changed("café", "A", ".", None),
        Untracked("dir/x"),
        Ignored("ig.log"),
        Renamed("moved", "R", ".", None, original="b", similarity=100),
    ]
    assert found.branch is None


def test_status_takes_gits_switches(changed: Git) -> None:
    def paths(**switches: object) -> list[tuple[str, str]]:
        found = status(changed, **switches)  # pyright: ignore[reportArgumentType]
        return sorted((type(e).__name__, e.path) for e in found.entries)

    assert paths(untracked="no", renames=False) == [
        ("Changed", "a"),
        ("Changed", "b"),
        ("Changed", "café"),
        ("Changed", "moved"),
    ]
    assert ("Untracked", "dir/") in paths(untracked="normal")
    assert ("Ignored", "ig.log") not in paths()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' file names can't hold a newline")
def test_status_reads_a_path_with_a_newline(repo: Git) -> None:
    (Path(repo.directory) / "two\nlines").write_bytes(b"")

    assert status(repo).entries == (Untracked("two\nlines"),)


@pytest.mark.skipif(sys.platform != "linux", reason="macOS and Windows names are Unicode")
def test_status_keeps_a_path_that_isnt_utf_8(repo: Git) -> None:
    with open(os.path.join(os.fsencode(repo.directory), b"\xff"), "wb"):
        pass

    (entry,) = status(repo).entries

    assert os.fsencode(entry.path) == b"\xff"


def add_submodule(git: Git) -> None:
    """A submodule, `s`, at a commit of its own, committed in the repository."""
    sub = Path(git.directory) / "s"
    Git(git.directory).run("init", "--quiet", str(sub))
    (sub / "f").write_bytes(b"f\n")
    Git(sub).run("add", "f")
    Git(sub).run("commit", "--quiet", "-m", "s")
    git.run("add", "s")
    git.run("commit", "--quiet", "-m", "submodule")


def test_status_reports_a_submodules_state(repo_from: RepoFrom) -> None:
    repo = repo_from("submodule", add_submodule)
    sub = Path(repo.directory) / "s"
    (sub / "f").write_bytes(b"changed\n")
    (sub / "u").write_bytes(b"")

    assert status(repo).entries == (Changed("s", ".", "M", SubmoduleState(False, True, True)),)
    assert status(repo, ignore_submodules="untracked").entries == (
        Changed("s", ".", "M", SubmoduleState(False, True, False)),
    )

    Git(sub).run("commit", "--quiet", "--all", "-m", "moved")

    assert status(repo, ignore_submodules="dirty").entries == (
        Changed("s", ".", "M", SubmoduleState(True, False, False)),
    )
    assert status(repo, ignore_submodules="all").entries == ()


def test_status_reports_a_conflict(repo: Git) -> None:
    top = Path(repo.directory)
    repo.run("switch", "--quiet", "-c", "other")
    (top / "a").write_bytes(b"other\n")
    repo.run("commit", "--quiet", "--all", "-m", "other")
    repo.run("switch", "--quiet", "main")
    (top / "a").write_bytes(b"main\n")
    repo.run("commit", "--quiet", "--all", "-m", "main")
    repo.process("merge", "other", capture_output=True)

    assert status(repo).entries == (Unmerged("a", "U", "U", None),)


def test_status_reports_the_branch_its_upstream_and_counts(repo: Git) -> None:
    repo.run("branch", "base")
    repo.run("commit", "--quiet", "--allow-empty", "-m", "ahead")
    repo.run("branch", "--quiet", "--set-upstream-to=base")
    head = repo.run("rev-parse", "HEAD")

    assert status(repo, branch=True).branch == StatusBranch(head, "main", "base", (1, 0))

    repo.run("branch", "--quiet", "-D", "base")

    assert status(repo, branch=True).branch == StatusBranch(head, "main", "base", None)

    repo.run("switch", "--quiet", "--detach")

    assert status(repo, branch=True).branch == StatusBranch(head, None, None, None)

    repo.run("switch", "--quiet", "--orphan", "new")

    assert status(repo, branch=True).branch == StatusBranch(None, "new", None, None)


def test_status_doesnt_refresh_the_index(repo: Git) -> None:
    index = Path(repo.directory) / ".git" / "index"
    os.utime(Path(repo.directory) / "a", (1e9, 1e9))  # stale in the index: git would refresh
    before = index.read_bytes()

    assert status(repo.read_only()).entries == ()
    assert index.read_bytes() == before

    repo.run("status")  # as a plain `git status` does

    assert index.read_bytes() != before


def test_status_raises_where_git_fails(repo: Git, tmp_path: Path) -> None:
    repo.run("clone", "--quiet", "--bare", ".", str(tmp_path / "bare"))

    with pytest.raises(GitError):
        status(Git(tmp_path / "bare"))


def test_status_reads_a_renamed_path_that_looks_like_a_header(repo: Git) -> None:
    repo.run("mv", "a", "# branch.head x")
    repo.run("commit", "--quiet", "-m", "odd name")
    repo.run("mv", "# branch.head x", "b")

    found = status(repo, branch=True)

    assert found.entries == (
        Renamed("b", "R", ".", None, original="# branch.head x", similarity=100),
    )
    assert found.branch is not None
    assert found.branch.head == "main"
