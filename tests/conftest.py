import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from git_client import Git


@pytest.fixture(autouse=True, scope="session")
def git_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """A git config of the tests' own, in place of the user's and the system's: an identity,
    no ignore or attributes file (git's defaults are in the user's home), and no background
    maintenance, which would start a process after each commit."""
    config = tmp_path_factory.mktemp("git") / "config"
    config.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"
        "[core]\n\texcludesFile =\n\tattributesFile =\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", str(config))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository on main with one commit (a file, `a`), made once per session."""
    path = tmp_path_factory.mktemp("template") / "repo"
    git = Git(path.parent)
    git.run("init", "--quiet", str(path))
    (path / "a").write_bytes(b"a\n")
    Git(path).run("add", "a")
    Git(path).run("commit", "--quiet", "-m", "first")
    return path


@pytest.fixture
def repo(repo_template: Path, tmp_path: Path) -> Git:
    """A copy of repo_template, for one test, and a client for it."""
    path = tmp_path / "repo"
    shutil.copytree(repo_template, path)
    return Git(path)
