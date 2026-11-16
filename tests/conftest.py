from pathlib import Path

import pytest

from git_client import Git
from git_client.testing import RepoTemplates


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
