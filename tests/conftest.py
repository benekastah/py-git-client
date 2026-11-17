import functools
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import Git, find_git
from git_client.testing import RepoTemplates, real_git

# These tests run git itself, rather than a wrapper on PATH, as a project's tests run its tool
# on real_git(): first on PATH, before anything but real_git asks find_git.
os.environ["PATH"] = os.pathsep.join([str(Path(real_git()).parent), os.environ.get("PATH", "")])
find_git.cache_clear()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "needs_git(major, minor, reason=...): skip the test with a git older than major.minor",
    )


@functools.cache
def git_version() -> tuple[int, int]:
    """The tests' git's version, e.g. (2, 56): "git version 2.56.0.windows.4" too."""
    found = re.match(r"git version (\d+)\.(\d+)", Git(".").run("version"))
    assert found, "git's version isn't one"
    return int(found[1]), int(found[2])


def pytest_runtest_setup(item: pytest.Item) -> None:
    for mark in item.iter_markers("needs_git"):
        needed: tuple[int, int] = mark.args
        if git_version() < needed:
            reason = mark.kwargs.get("reason", "")
            pytest.skip(
                f"needs git {needed[0]}.{needed[1]} or newer" + (f": {reason}" if reason else "")
            )


Changes = Callable[[Git], None]
"""Changes to make_repo's repository, for repo_from: they may add repositories beside it."""


def make_repo(directory: Path) -> None:
    """A repository, `repo`, on main with one commit (a file, `a`)."""
    path = directory / "repo"
    Git(directory).run("init", "--quiet", str(path))
    (path / "a").write_bytes(b"a\n")
    Git(path).run("add", "a")
    Git(path).run("commit", "--quiet", "-m", "first")


@pytest.fixture
def repo(repo_templates: RepoTemplates, tmp_path: Path) -> Git:
    """A copy of make_repo's repository, for one test, and a client for it."""
    repo_templates.build("repo", make_repo)
    return Git(repo_templates.copy("repo", tmp_path / "copy") / "repo")


@pytest.fixture
def repo_from(repo_templates: RepoTemplates, tmp_path: Path) -> Callable[[str, Changes], Git]:
    """Gives a copy, for one test, of make_repo's repository with `changes` made to it (each
    a template, `name`, built once per session), and a client for it."""

    def copy(name: str, changes: Changes) -> Git:
        def make(directory: Path) -> None:
            make_repo(directory)
            changes(Git(directory / "repo"))

        repo_templates.build(name, make)
        return Git(repo_templates.copy(name, tmp_path / "copy") / "repo")

    return copy


def corrupt_loose_commit(git: Git) -> None:
    """Make the commit at HEAD, a loose object, garbage, as a disk's damage could."""
    commit = git.run("rev-parse", "HEAD")
    path = Path(git.directory) / ".git" / "objects" / commit[:2] / commit[2:]
    path.chmod(0o644)  # git makes it read-only
    path.write_bytes(b"garbage")


def corrupt_packed_commit(git: Git) -> None:
    """Pack every object, then make the commit at HEAD's data in the pack garbage, past the
    header that says its type and size."""
    commit = git.run("rev-parse", "HEAD")
    git.run("repack", "-a", "-d", "-q")
    pack_dir = Path(git.directory) / ".git" / "objects" / "pack"
    (index,) = pack_dir.glob("*.idx")
    # "<id> <type> <size> <size in the pack> <offset>"
    (line,) = (
        line
        for line in git.run("verify-pack", "-v", str(index)).splitlines()
        if line.startswith(commit)
    )
    offset = int(line.split()[4])
    pack = index.with_suffix(".pack")
    data = bytearray(pack.read_bytes())
    for i in range(offset + 3, offset + 12):
        data[i] ^= 0xFF
    pack.chmod(0o644)
    pack.write_bytes(bytes(data))


@pytest.fixture(params=[corrupt_loose_commit, corrupt_packed_commit])
def damaged_repo(request: pytest.FixtureRequest, repo_from: Callable[[str, Changes], Git]) -> Git:
    """make_repo's repository with its commit's object corrupt: loose, or in a pack."""
    corrupt: Changes = request.param
    return repo_from(corrupt.__name__, corrupt)
