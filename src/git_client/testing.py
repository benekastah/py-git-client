"""For testing tools that run git: a git config of the tests' own, the real git, repositories
built once per session and copied for each test, and a fake `Git` for unit tests of the
commands a tool runs.

Load it as a pytest plugin, in a project's top conftest.py (`pytest_plugins =
["git_client.testing"]`) or with `-p git_client.testing`, for the `git_config` and
`repo_templates` fixtures. It imports pytest, so only tests should import it.
"""

import functools
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
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
[core]
\texcludesFile =
\tattributesFile =
"""
"""The tests' git config: an identity, main as the first branch, no background maintenance
(which would start a `git maintenance run --auto` after each commit, fetch and push), and no
global ignore or attributes file (`~/.config/git/ignore` and `attributes` would otherwise
still apply)."""

GIT_ENVIRONMENT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_REFLOG_ACTION",
)
"""git's variables for which repository to use and who's committing, which win over any
config: the tests' git mustn't inherit them, e.g. from a git hook that runs the tests."""


@functools.cache
def real_git() -> str:
    """git itself, from `git --exec-path`, rather than a wrapper (e.g. git-ai) or Git for
    Windows' launcher on PATH, which start another process for each; else the git on PATH.
    Found once per process."""
    found = find_git()
    exec_path = Git(".", program=found).run_bytes("--exec-path")
    candidate = Path(os.fsdecode(exec_path.strip())) / (
        "git.exe" if sys.platform == "win32" else "git"
    )
    return str(candidate) if candidate.exists() else found


@pytest.fixture(scope="session", autouse=True)
def git_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Every test's git gets the tests' own config (TEST_GIT_CONFIG) as its global config, no
    system config, and none of git's variables for which repository to use or who's
    committing (GIT_ENVIRONMENT_VARIABLES, and each GIT_CONFIG_KEY_n and GIT_CONFIG_VALUE_n).
    Yields the config file's path."""
    config = tmp_path_factory.mktemp("git-config") / "config"
    config.write_text(TEST_GIT_CONFIG, encoding="utf-8")
    counted = [n for n in os.environ if n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]
    with pytest.MonkeyPatch.context() as patch:
        for name in [*GIT_ENVIRONMENT_VARIABLES, *counted]:
            patch.delenv(name, raising=False)
        patch.setenv("GIT_CONFIG_GLOBAL", str(config))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
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
        """The directory `make` filled, making it the first time `name` is asked for. If
        `make` fails, nothing is kept, so the next build tries again and fails as it did."""
        directory = self.root / name
        if name not in self._built:
            directory.mkdir(parents=True)
            try:
                make(directory)
            except BaseException:
                shutil.rmtree(directory)
                raise
            self._built.add(name)
        return directory

    def copy(self, name: str, dest: Path) -> Path:
        """Copy the repositories `build` made for `name` to `dest`, pointing remotes that named
        one of them at its copy. Returns `dest`."""
        source = self.root / name
        shutil.copytree(source, dest)
        repoint_remotes(source, dest)
        return dest


def repositories(root: Path) -> Iterator[Path]:
    """Every repository under root, root included: each directory with a `.git` in it, and each
    bare repository (a directory with `HEAD`, `config` and `objects`)."""
    for directory, subdirectories, files in os.walk(root):
        if ".git" in subdirectories or ".git" in files:
            yield Path(directory)
            if ".git" in subdirectories:
                subdirectories.remove(".git")
        elif "HEAD" in files and "config" in files and "objects" in subdirectories:
            yield Path(directory)
            subdirectories.clear()


def repoint_remotes(source: Path, dest: Path) -> None:
    """In each repository under `dest` (copied from `source`), point every remote URL (`url`
    and `pushurl`) that's a path under `source` at the same path under `dest`, through `git
    config` rather than by editing the file, where git escapes backslashes (Windows' paths).
    A repository whose config names no remote starts no process."""
    for repo in repositories(dest):
        config = repo / ".git" / "config" if (repo / ".git").is_dir() else repo / "config"
        if not config.is_file() or "[remote" not in config.read_text(
            encoding="utf-8", errors="replace"
        ):
            continue
        git = Git(repo)
        listed = git.process(
            "config",
            "--local",
            "--null",
            "--get-regexp",
            r"^remote\..*\.(url|pushurl)$",
            capture_output=True,
        )
        urls: dict[str, list[str]] = {}
        for entry in listed.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if entry:
                key, _, url = entry.partition("\n")
                urls.setdefault(key, []).append(url)
        for key, values in urls.items():
            moved = [_moved(url, source, dest) for url in values]
            if moved != values:
                git.run("config", "--local", "--unset-all", key)
                for url in moved:
                    git.run("config", "--local", "--add", key, url)


def _moved(url: str, source: Path, dest: Path) -> str:
    """url, if it's a path under source, as the same path under dest; else url as it is."""
    try:
        return (dest / Path(url).relative_to(source)).as_posix()
    except ValueError:
        return url


@pytest.fixture(scope="session")
def repo_templates(tmp_path_factory: pytest.TempPathFactory, git_config: Path) -> RepoTemplates:
    """Repositories built once per session and copied for each test (see RepoTemplates)."""
    return RepoTemplates(tmp_path_factory.mktemp("repo-templates"))


class UnexpectedCommand(AssertionError):
    """FakeGit was asked to run a command no response matches."""


@dataclass(frozen=True)
class Call:
    """A command FakeGit was asked to run: git's arguments, what was sent on its stdin, and
    the environment it added to ours."""

    args: list[str]
    input: bytes | None
    env: dict[str, EnvValue]


class FakeGit(Git):
    """A Git that starts no process, for unit tests of the git commands a tool runs: it
    records each command in `calls` (its arguments alone in `commands`), and answers with the
    first response whose arguments begin the command's. A command no response matches fails
    the test.

        git = FakeGit()
        git.respond(["rev-parse", "--abbrev-ref", "HEAD"], stdout="main\\n")
        git.respond(["merge-base", "--is-ancestor"], returncode=1)
        ... run the code under test with git ...
        assert git.commands[-1] == ["push", "origin", "main"]

    Assert the commands where they're the behavior (what's pushed, deleted or rewritten;
    `calls` also has what was sent on stdin, e.g. to `update-ref --stdin`); otherwise give
    realistic output and assert the result. It can't start long-lived processes (`popen`),
    so CatFile needs a real Git.
    """

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        super().__init__(directory, program="git")
        self.calls: list[Call] = []
        self._responses: list[tuple[list[str], subprocess.CompletedProcess[bytes]]] = []

    @property
    def commands(self) -> list[list[str]]:
        """Each command's arguments, in the order they were run."""
        return [call.args for call in self.calls]

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
        self.calls.append(Call(command, input, {**self.env, **(env or {})}))
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
