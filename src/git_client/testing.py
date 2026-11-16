"""For testing tools that run git: a git config of the tests' own, the real git, repositories
built once per session and copied for each test, and a fake `Git` for unit tests of the
commands a tool runs.

Load it as a pytest plugin, in a project's top conftest.py (`pytest_plugins =
["git_client.testing"]`) or with `-p git_client.testing`, for the `git_config` and
`repo_templates` fixtures. It imports pytest, so only tests should import it.
"""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from git_client.client import Arg, EnvValue, Git, encode, find_git

TEST_GIT_CONFIG = """\
[user]
\tname = Test
\temail = test@example.com
[init]
\tdefaultBranch = main
[maintenance]
\tauto = false
[gc]
\tauto = 0
"""
"""The tests' git config: an identity, main as the first branch, and no background
maintenance, which would start a `git maintenance run --auto` after each commit, fetch and
push."""


def real_git() -> str:
    """git itself, from `git --exec-path`, rather than a wrapper (e.g. git-ai) or Git for
    Windows' launcher on PATH, which start another process for each; else the git on PATH."""
    found = find_git()
    exec_path = Git(".", program=found).run_bytes("--exec-path")
    candidate = Path(os.fsdecode(exec_path.strip())) / ("git.exe" if os.name == "nt" else "git")
    return str(candidate) if candidate.exists() else found


def isolated_environment(config: Path) -> dict[str, str]:
    """The environment variables that give git `config` (written with TEST_GIT_CONFIG) as its
    global config, and no system config, so the user's and the system's settings don't reach
    the tests."""
    config.write_text(TEST_GIT_CONFIG, encoding="utf-8")
    return {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture(scope="session", autouse=True)
def git_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Every test's git sees the tests' own config (TEST_GIT_CONFIG), not the user's or the
    system's. Yields the config file's path."""
    config = tmp_path_factory.mktemp("git-config") / "config"
    with pytest.MonkeyPatch.context() as patch:
        for name, value in isolated_environment(config).items():
            patch.setenv(name, value)
        yield config


class RepoTemplates:
    """Repositories built once per session, by name, and copied for each test that wants one:
    copying a directory is much quicker than the git processes that built it.

    `build(name, make)` calls `make(directory)` the first time, to fill an empty directory
    with one or more repositories (e.g. `origin.git` and a clone of it); `copy(name, dest)`
    copies them for a test.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._built: set[str] = set()

    def build(self, name: str, make: Callable[[Path], None]) -> Path:
        """The directory `make` filled, making it the first time `name` is asked for."""
        directory = self.root / name
        if name not in self._built:
            directory.mkdir(parents=True)
            make(directory)
            self._built.add(name)
        return directory

    def copy(self, name: str, dest: Path) -> Path:
        """Copy the repositories `build` made for `name` to `dest`, pointing remotes that named
        one of them at its copy. Returns `dest`."""
        source = self.root / name
        shutil.copytree(source, dest)
        repoint_remotes(source, dest)
        return dest


def repoint_remotes(source: Path, dest: Path) -> None:
    """In each repository under `dest` (copied from `source`), point every remote whose URL is
    a path under `source` at the same path under `dest`. Through `git config`, not by editing
    the file, where git escapes backslashes (Windows' paths)."""
    for config in [*dest.glob("*/.git/config"), *dest.glob("*/config"), dest / ".git" / "config"]:
        if not config.is_file():
            continue
        repo = config.parent.parent if config.parent.name == ".git" else config.parent
        client = Git(repo)
        listed = client.process(
            "config", "--local", "--get-regexp", r"^remote\..*\.url$", capture_output=True
        )
        for line in listed.stdout.decode("utf-8", "surrogateescape").splitlines():
            key, _, url = line.partition(" ")
            try:
                relative = Path(url).relative_to(source)
            except ValueError:
                continue
            client.run("config", "--local", key, (dest / relative).as_posix())


@pytest.fixture(scope="session")
def repo_templates(tmp_path_factory: pytest.TempPathFactory, git_config: Path) -> RepoTemplates:
    """Repositories built once per session and copied for each test (see RepoTemplates)."""
    return RepoTemplates(tmp_path_factory.mktemp("repo-templates"))


class UnexpectedCommand(AssertionError):
    """FakeGit was asked to run a command no response matches."""


class FakeGit(Git):
    """A Git that starts no process, for unit tests of the git commands a tool runs: it
    records each command's arguments in `commands`, and answers with the first response whose
    arguments begin the command's. A command no response matches fails the test.

        git = FakeGit()
        git.respond(["rev-parse", "--abbrev-ref", "HEAD"], stdout="main\\n")
        git.respond(["merge-base", "--is-ancestor"], returncode=1)
        ... run the code under test with git ...
        assert git.commands[-1] == ["push", "origin", "main"]

    Assert the commands where they're the behavior (what's pushed, deleted or rewritten);
    otherwise give realistic output and assert the result. It can't start long-lived processes
    (`popen`), so CatFile needs a real Git.
    """

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        super().__init__(directory, program="git")
        self.commands: list[list[str]] = []
        self._responses: list[tuple[list[str], subprocess.CompletedProcess[bytes]]] = []

    def respond(
        self,
        args: Sequence[str],
        stdout: str | bytes = b"",
        stderr: str | bytes = b"",
        returncode: int = 0,
    ) -> None:
        """Answer each command whose arguments begin with `args` with this output and exit
        status. Earlier responses win."""
        completed = subprocess.CompletedProcess(
            list(args),
            returncode,
            stdout if isinstance(stdout, bytes) else encode(stdout),
            stderr if isinstance(stderr, bytes) else encode(stderr),
        )
        self._responses.append((list(args), completed))

    def process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: object = None,
        env: Mapping[str, EnvValue] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [os.fsdecode(arg) for arg in args]
        self.commands.append(command)
        for prefix, completed in self._responses:
            if command[: len(prefix)] == prefix:
                return completed
        raise UnexpectedCommand(f"no response for git {' '.join(command)}")

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        raise UnexpectedCommand(
            f"FakeGit can't start a long-lived git {' '.join(map(os.fsdecode, args))}"
        )
