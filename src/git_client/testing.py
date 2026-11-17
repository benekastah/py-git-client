"""For testing tools that run git: a git config of the tests' own, the real git, repositories
built once per session and copied for each test, and a fake `Git` for unit tests of the
commands a tool runs.

Load it as a pytest plugin, in a project's top conftest.py (`pytest_plugins =
["git_client.testing"]`) or with `-p git_client.testing`, for its fixtures (`git_config`,
`git_config_text`, `repo_templates`, `git_calls`, `fresh_find_git`,
`outside_any_repository`). It imports pytest, so only tests should import it.
"""

import contextlib
import contextvars
import functools
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest

from git_client.client import (
    CLIENT_TIMEOUT,
    Arg,
    EnvValue,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    encode,
    find_git,
    refuse_if_stopping,
)

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
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_TEMPLATE_DIR",
    "GIT_DEFAULT_HASH",
    "GIT_DEFAULT_REF_FORMAT",
    "GIT_SEQUENCE_EDITOR",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)
"""git's variables that the tests' git mustn't inherit (e.g. from a git hook that runs the
tests), since they win over any config: which repository to use and what's in it (those `git
rev-parse --local-env-vars` lists), who's committing, extra config, GIT_TEMPLATE_DIR, which
would copy a user's hooks into each new repository, the object and ref formats of a new
repository (GIT_DEFAULT_HASH, GIT_DEFAULT_REF_FORMAT: e.g. sha256 ids), the user's editor for
`rebase -i`, and the programs git runs to ask for a username or password (GIT_ASKPASS, then
SSH_ASKPASS, which a desktop may set), even with GIT_TERMINAL_PROMPT=0."""

GIT_SETTINGS = {"GIT_EDITOR": "true", "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}
"""What the tests' git gets in its environment: an editor that leaves a message (or `rebase
-i`'s list) as it is, rather than the user's, which would make a test depend on their shell
or wait for them; only local remotes (no network: a test that needs more, e.g. ssh through a
fake ssh, widens GIT_ALLOW_PROTOCOL itself); and no asking for a username or password."""


@functools.cache
def real_git() -> str:
    """git itself, from `git --exec-path`, rather than a wrapper (e.g. git-ai) or Git for
    Windows' launcher on PATH, which start another process for each; else the git on PATH.
    Found once per process. The plugin runs it for its own git; a project points the tool it
    tests at it (see the README)."""
    found = find_git()
    exec_path = Git(".", program=found).run_bytes("--exec-path")
    candidate = Path(os.fsdecode(exec_path.strip())) / (
        "git.exe" if sys.platform == "win32" else "git"
    )
    return str(candidate) if candidate.exists() else found


@pytest.fixture(scope="session")
def git_config_text() -> str:
    """The text of the tests' git config: TEST_GIT_CONFIG. A project overrides this fixture
    to add entries, e.g. `return TEST_GIT_CONFIG + "[commit]\\n\\tgpgSign = false\\n"`."""
    return TEST_GIT_CONFIG


def _temporary_roots(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """The directories temporary ones are made in: pytest's (`tmp_path`, `tmp_path_factory`,
    whatever --basetemp says) and Python's (`tempfile.gettempdir()`, from TMPDIR, TEMP or
    TMP), resolved, for Windows: TEMP is often an 8.3 short name there (`RUNNER~1`), and
    resolving gives the long name a working directory has, whatever Git for Windows makes of
    a short one. git runs with them in GIT_CEILING_DIRECTORIES, so in a directory made in one
    it looks no higher than that directory, even if a tool's TMPDIR, or the project's
    --basetemp, is in a repository: a test that runs git in a directory that isn't a
    repository gets NotARepositoryError, rather than the repository around it (and changing
    its config)."""
    return [
        tmp_path_factory.getbasetemp().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]


@pytest.fixture(scope="session", autouse=True)
def git_client_isolation(
    tmp_path_factory: pytest.TempPathFactory, git_config_text: str
) -> Iterator[Path]:
    """Cuts git off from the user's setup for the whole session, session fixtures included:
    its global config is the tests' own (git_config_text, in a file of the session's, which
    each test then swaps for its own: see git_config), with no system config, GIT_SETTINGS,
    and none of GIT_ENVIRONMENT_VARIABLES, nor any GIT_CONFIG_KEY_n or GIT_CONFIG_VALUE_n.
    And it finds no repository above a temporary directory (see _temporary_roots), even if
    they're in one. Yields the session's config file."""
    config = tmp_path_factory.mktemp("git-config") / "config"
    config.write_text(git_config_text, encoding="utf-8")
    counted = [n for n in os.environ if n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]
    with pytest.MonkeyPatch.context() as patch:
        for name in [*GIT_ENVIRONMENT_VARIABLES, *counted]:
            patch.delenv(name, raising=False)
        patch.setenv("GIT_CONFIG_GLOBAL", str(config))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        # The user's own ceilings, if any, after the temporary roots.
        ceilings = [
            *map(str, _temporary_roots(tmp_path_factory)),
            os.environ.get("GIT_CEILING_DIRECTORIES"),
        ]
        patch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join(filter(None, ceilings)))
        for name, value in GIT_SETTINGS.items():
            patch.setenv(name, value)
        yield config


@pytest.fixture
def git_config(tmp_path_factory: pytest.TempPathFactory, git_config_text: str) -> Path:
    """This test's git config file (its global config), written from git_config_text, so
    what one test writes there (`git config --global`) no other test sees."""
    config = tmp_path_factory.mktemp("git-config") / "config"
    config.write_text(git_config_text, encoding="utf-8")
    return config


@pytest.fixture(autouse=True)
def git_client_test_config(git_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gives each test's git its own config, git_config's file: a project that overrides
    git_config changes only the file."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))


# Whether the plugin is setting up repositories (building or copying a template), whose git
# processes git_calls leaves out.
_setting_up: contextvars.ContextVar[bool] = contextvars.ContextVar("_setting_up", default=False)


@contextlib.contextmanager
def _unrecorded() -> Generator[None]:
    """Leaves the git processes started in the block out of git_calls."""
    token = _setting_up.set(True)
    try:
        yield
    finally:
        _setting_up.reset(token)


class RepoTemplates:
    """Repositories built once per session, by name, and copied for each test that wants one:
    copying a directory is much quicker than the git processes that built it.

    `build(name, make)` calls `make(directory)` the first time, to fill an empty directory
    with one or more repositories (e.g. `origin.git` and a clone of it), with `config` as
    git's global config, whichever test asks first; `copy(name, dest)` copies them for a
    test.
    """

    def __init__(self, root: Path, config: Path) -> None:
        self.root = root
        self.config = config
        self._built: set[str] = set()

    def build(self, name: str, make: Callable[[Path], None]) -> Path:
        """The directory `make` filled, making it the first time `name` is asked for, then
        deleting git's sample hooks (`hooks/*.sample`). If `make` fails, nothing is kept, so
        the next build tries again and fails as it did."""
        directory = self.root / name
        if name not in self._built:
            directory.mkdir(parents=True)
            try:
                with pytest.MonkeyPatch.context() as patch, _unrecorded():
                    patch.setenv("GIT_CONFIG_GLOBAL", str(self.config))
                    make(directory)
            except BaseException:
                _remove(directory)
                raise
            # Fewer files to copy; the hooks directories stay, for a test to add a hook to.
            for sample in directory.glob("**/hooks/*.sample"):
                sample.unlink()
            self._built.add(name)
        return directory

    def copy(self, name: str, dest: Path) -> Path:
        """Copy the repositories `build` made for `name` to `dest` (symlinks as symlinks),
        pointing remotes that named one of them at its copy. Returns `dest`.

        A copy's index keeps the template's stat data, which no longer matches its files: the
        first command that refreshes the index (e.g. `git status`, `git diff`) makes it match,
        but plumbing that doesn't (`git diff-index HEAD`, `git diff-files`) reports every file
        as modified until then. A test of a tool that uses such plumbing runs `git
        update-index -q --refresh` first."""
        source = self.root / name
        shutil.copytree(source, dest, symlinks=True)
        with _unrecorded():
            _repoint_remotes(source, dest)
        return dest


def _remove(directory: Path) -> None:
    """Delete directory and what's in it, read-only files included: git makes its objects
    read-only, which on Windows keeps rmtree from deleting them."""

    def make_writable_and_retry(remove: Callable[[str], object], path: str, *_: object) -> None:
        os.chmod(path, stat.S_IWRITE)
        remove(path)

    shutil.rmtree(directory, onexc=make_writable_and_retry)


def _repositories(root: Path) -> Iterator[Path]:
    """Every repository under root, root included: each directory with a `.git` in it, and each
    bare repository (a directory with `HEAD`, `config` and `objects`). One whose `.git` is a
    file (a submodule, or a linked worktree) is listed, but its `gitdir:` isn't followed, so a
    submodule's own repository (under `.git/modules/`) isn't."""
    for directory, subdirectories, files in os.walk(root):
        if ".git" in subdirectories or ".git" in files:
            yield Path(directory)
            if ".git" in subdirectories:
                subdirectories.remove(".git")
        elif "HEAD" in files and "config" in files and "objects" in subdirectories:
            yield Path(directory)
            subdirectories.clear()


def _repoint_remotes(source: Path, dest: Path) -> None:
    """In each repository under `dest` (copied from `source`), point every remote URL (`url`
    and `pushurl`) that's a path under `source`, or a `file://` URL of one, at the same path
    under `dest`. The URLs are read from the config file (not from files it includes), and
    each that moves is set through `git config` rather than by editing the file, where git
    escapes backslashes (Windows' paths): one process for a key with one value, and for one
    with several, one to clear it and one per value. Submodules aren't repointed (their
    remote, in `.git/modules/<name>/config`, and the superproject's `submodule.<name>.url`
    still name the template), nor linked worktrees (whose `gitdir:` is an absolute path into
    the template)."""
    for repo in _repositories(dest):
        if (repo / ".git").is_file():
            continue
        config = repo / ".git" / "config" if (repo / ".git").is_dir() else repo / "config"
        if not config.is_file():
            continue
        git = Git(repo, program=real_git())
        for key, values in _remote_urls(decode(config.read_bytes())).items():
            moved = [_moved(url, source, dest) for url in values]
            if moved == values:
                continue
            if len(moved) == 1:
                git.run("config", "--local", key, moved[0])
            else:
                git.run("config", "--local", "--unset-all", key)
                for url in moved:
                    git.run("config", "--local", "--add", key, url)


# A config file's section header, `[remote "origin"]` (or the old `[remote.origin]`), and a
# remote's URL, `url = ...` or `pushurl = ...`: names are case-insensitive.
_SECTION = re.compile(r'\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_URL = re.compile(r"\s*(url|pushurl)\s*=(.*)", re.IGNORECASE)


def _remote_urls(text: str) -> dict[str, list[str]]:
    """The remotes' URLs in a config file's text, by key (`remote.<name>.url` or
    `.pushurl`), in order, as git reads them."""
    urls: dict[str, list[str]] = {}
    remote: str | None = None
    for line in text.splitlines():
        if section := _SECTION.match(line):
            name, subsection = section[1], section[2]
            if subsection is not None:
                remote = _unescaped(subsection) if name.lower() == "remote" else None
            else:
                # The old form, whose subsection git reads in lower case.
                first, dot, rest = name.partition(".")
                remote = rest.lower() if dot and first.lower() == "remote" else None
        elif remote is not None and (entry := _URL.match(line)):
            key = f"remote.{remote}.{entry[1].lower()}"
            urls.setdefault(key, []).append(_config_value(entry[2]))
    return urls


def _unescaped(text: str) -> str:
    """A quoted subsection's text, without its escapes (\\" and \\\\)."""
    return re.sub(r"\\(.)", r"\1", text)


def _config_value(raw: str) -> str:
    """A value as git reads it from what follows `=`: without the space around it (outside
    quotes), its quotes, its escapes (\\n, \\t, \\b, \\" and \\\\), and a comment after it."""
    value: list[str] = []
    quoted = escaped = False
    for c in raw:
        if escaped:
            value.append({"n": "\n", "t": "\t", "b": "\b"}.get(c, c))
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            quoted = not quoted
        elif c in "#;" and not quoted:
            break
        else:
            value.append(c)
    return "".join(value).strip()


def _moved(url: str, source: Path, dest: Path) -> str:
    """url, if it's a path under source, or a `file://` URL of one, as the same path (or URL)
    under dest; else url as it is."""
    parsed = urllib.parse.urlsplit(url)
    is_file_url = parsed.scheme.lower() == "file"
    path = Path(urllib.request.url2pathname(parsed.path)) if is_file_url else Path(url)
    try:
        moved = dest / path.relative_to(source)
    except ValueError:
        return url
    return moved.as_uri() if is_file_url else moved.as_posix()


@pytest.fixture(scope="session")
def repo_templates(
    tmp_path_factory: pytest.TempPathFactory, git_client_isolation: Path
) -> RepoTemplates:
    """Repositories built once per session, under the session's git config, and copied for
    each test (see RepoTemplates)."""
    return RepoTemplates(tmp_path_factory.mktemp("repo-templates"), git_client_isolation)


@pytest.fixture
def outside_any_repository(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Runs the test from an empty directory where git finds no repository, even if pytest's
    temporary directories are in one (see _temporary_roots), so a git run without a directory
    (e.g. a `Git()` the code under test forgot to give one) fails rather than change the
    repository the tests were started in. Opt-in: a project makes it automatic with an
    autouse fixture of its own that asks for it. Returns the directory."""
    directory = tmp_path_factory.mktemp("outside-any-repository")
    monkeypatch.chdir(directory)
    return directory


@pytest.fixture
def fresh_find_git() -> Iterator[None]:
    """find_git as if nothing had called it yet, for a test that changes what it would find
    (e.g. PATH, or a project's variable); after the test, it forgets that test's answer
    too."""
    find_git.cache_clear()
    yield
    find_git.cache_clear()


class GitCall(list[str]):
    """A git process git_calls recorded: git's arguments, as a list (equal to a plain list of
    the same arguments), and `program`, the git it ran (the client's `program`: e.g. what a
    project's variable named, through `find_git(env_var)`)."""

    def __init__(self, args: Sequence[str], program: str) -> None:
        super().__init__(args)
        self.program = program


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[GitCall]:
    """Each git process a `Git` or `ReadOnlyGit` starts during the test (through `process` or
    `popen`), in order, while git runs as it would: to count a tool's processes, or check the
    ones it starts. Each is a GitCall: git's arguments (without `git -C <directory>`), and the
    git it ran in `program`. It leaves out the plugin's own, building and copying templates
    (see RepoTemplates), whichever fixture is set up first."""
    calls: list[GitCall] = []
    process, popen = ReadOnlyGit.process, ReadOnlyGit.popen

    def record(git: ReadOnlyGit, args: tuple[Arg, ...]) -> None:
        # Each raises, starting nothing: for a command a ReadOnlyGit refuses, then once stopping.
        git.command(*args)
        refuse_if_stopping(git, args)
        if not _setting_up.get():
            calls.append(GitCall([os.fsdecode(arg) for arg in args], git.program))

    def recording_process(
        self: ReadOnlyGit, *args: Arg, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        record(self, args)
        return process(self, *args, **kwargs)

    def recording_popen(self: ReadOnlyGit, *args: Arg, **kwargs: Any) -> subprocess.Popen[bytes]:
        record(self, args)
        return popen(self, *args, **kwargs)

    monkeypatch.setattr(ReadOnlyGit, "process", recording_process)
    monkeypatch.setattr(ReadOnlyGit, "popen", recording_popen)
    return calls


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
    so CatFile needs a real Git. Like Git, it raises NotStartedError, recording nothing,
    once its `stopping` says so.

    To fake a project's own subclass of Git, put that class first, so FakeGit stands where
    Git was beneath it: its methods, and its own `process` (which calls `super().process`),
    run over FakeGit's. Its constructor's arguments reach FakeGit's, Git's own, but FakeGit
    runs `program`, or "git" if that's None, without looking for one (`env_var` is
    ignored).

        class FakeMyGit(MyGit, FakeGit):
            pass
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] = ".",
        *,
        program: str | None = None,
        env_var: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: float | None = None,
        stopping: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            directory,
            program=program or "git",
            env_var=env_var,
            env=env,
            timeout=timeout,
            stopping=stopping,
        )
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
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        refuse_if_stopping(self, args)
        command = [os.fsdecode(arg) for arg in args]
        self.calls.append(Call(command, input, {**self.env, **(env or {})}))
        for prefix, completed in self._responses:
            if command[: len(prefix)] == prefix:
                return completed
        raise UnexpectedCommand(f"no response for git {' '.join(command)}")

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        refuse_if_stopping(self, args)
        raise UnexpectedCommand(
            f"FakeGit can't start a long-lived git {' '.join(map(os.fsdecode, args))}"
        )
