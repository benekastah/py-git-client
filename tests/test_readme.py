"""The README's Python examples, run as they're written, against a real repository. It checks
that they run, not the values their comments give, and the blocks that only define something
(a fixture, a test) aren't called."""

import os
import re
import signal
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import git_client
import git_client.testing
from git_client import Git
from git_client.testing import RepoTemplates

README = Path(__file__).parent.parent / "README.md"

# What starts a block that needs a newer git than the oldest supported.
NEEDS_GIT = re.compile(r"# Needs git (\d+)\.(\d+)\.\n")


def runs_on(block: str, version: tuple[int, ...]) -> bool:
    """Whether a README block runs on that git: unless it starts by naming a newer one."""
    needs = NEEDS_GIT.match(block)
    return needs is None or version >= (int(needs[1]), int(needs[2]))


def make_readme_repository(directory: Path) -> None:
    """What the README's examples name: path/to/repo, a clone of origin.git, on main with a
    README.md and an upstream, and branches a, b and old."""
    Git(directory).run("init", "--quiet", "--bare", "origin.git")
    Git(directory).run("clone", "--quiet", "origin.git", str(Path("path", "to", "repo")))
    repo = Git(directory / "path" / "to" / "repo")
    (directory / "path" / "to" / "repo" / "README.md").write_bytes(b"# A repository\n")
    repo.run("add", "README.md")
    repo.run("commit", "--quiet", "-m", "first")
    repo.run("push", "--quiet", "--set-upstream", "origin", "main")
    for branch in ("a", "b", "old"):
        repo.run("branch", branch)


def publish(git: Git) -> None:
    """The README's example of a tool's code, for FakeGit to run."""
    git.run("push", "origin", "main")


# The oldest git py-git-client supports, as the README says: CatFile needs it.
@pytest.mark.needs_git(2, 36, reason="the oldest supported")
@pytest.mark.usefixtures("fresh_find_git")
def test_the_readmes_python_examples_run(
    repo_templates: RepoTemplates,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_version: tuple[int, int, int],
) -> None:
    repo_templates.build("readme", make_readme_repository)
    monkeypatch.chdir(repo_templates.copy("readme", tmp_path / "copy"))
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    names = {
        **vars(git_client),
        **vars(git_client.testing),
        "os": os,
        "Path": Path,
        "pytest": pytest,
        "publish": publish,
        "signal": signal,
        "sys": sys,
        "threading": threading,
    }

    # Examples set os.environ["MY_TOOL_GIT"], as a conftest would, and a SIGTERM handler, as a
    # tool would, which later tests in this process mustn't see.
    environ = os.environ.copy()
    on_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        for block in blocks:
            if not runs_on(textwrap.dedent(block), git_version):
                continue
            exec(textwrap.dedent(block), dict(names))  # noqa: S102 (the README's own code)
    finally:
        os.environ.clear()
        os.environ.update(environ)
        signal.signal(signal.SIGTERM, on_sigterm)

    # The last thing "Using it" does.
    assert Git(Path("path", "to", "repo")).resolve("refs/heads/old") is None


@pytest.mark.parametrize(
    ("block", "version", "runs"),
    [
        ("# Needs git 2.38.\ngit.run('merge-tree')\n", (2, 37), False),
        ("# Needs git 2.38.\ngit.run('merge-tree')\n", (2, 38), True),
        ("# Needs git 2.38.\ngit.run('merge-tree')\n", (3, 0), True),
        ("git.run('status')\n", (2, 36), True),
    ],
)
def test_a_block_marked_for_a_newer_git_is_skipped(
    block: str, version: tuple[int, ...], runs: bool
) -> None:
    assert runs_on(block, version) is runs
