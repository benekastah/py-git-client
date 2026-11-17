import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import Git, find_git
from git_client.testing import RepoTemplates, real_git

# These tests run git itself, rather than a wrapper on PATH, as a project's tests run its tool
# on real_git(): first on PATH, before anything but real_git asks find_git.
os.environ["PATH"] = os.pathsep.join([str(Path(real_git()).parent), os.environ.get("PATH", "")])
find_git.cache_clear()


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


@pytest.fixture
def german(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who reads git in German, if git's translations are installed (through
    LANGUAGE, which needs no German locale installed), rather than the tests' English."""
    monkeypatch.delenv("GIT_TEXTDOMAINDIR", raising=False)
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    said = subprocess.run(
        [real_git(), "-C", os.devnull, "status"], capture_output=True, check=False
    ).stderr
    if said.startswith(b"fatal: "):
        pytest.skip("git's German translation isn't installed")
