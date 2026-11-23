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

import git_client.client
from git_client.client import (
    CLIENT_TIMEOUT,
    Arg,
    EnvValue,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    encode,
    error_from,
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
        # Each template built, by name: its repositories' remote URLs (see _remote_urls).
        self._built: dict[str, dict[Path, dict[str, list[str]]]] = {}

    def build(self, name: str, make: Callable[[Path], None]) -> Path:
        """The directory `make` filled, making it the first time `name` is asked for, then
        deleting git's sample hooks (`hooks/*.sample`), and reading its remotes' URLs, for
        `copy` to repoint. If `make` fails, nothing is kept, so the next build tries again and
        fails as it did."""
        directory = self.root / name
        if name not in self._built:
            directory.mkdir(parents=True)
            try:
                with pytest.MonkeyPatch.context() as patch, _unrecorded():
                    patch.setenv("GIT_CONFIG_GLOBAL", str(self.config))
                    make(directory)
                    urls = _remote_urls(directory)
            except BaseException:
                _remove(directory)
                raise
            # Fewer files to copy; the hooks directories stay, for a test to add a hook to.
            for sample in directory.glob("**/hooks/*.sample"):
                sample.unlink()
            self._built[name] = urls
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
            _repoint_remotes(self._built[name], source, dest)
        return dest


def _remove(directory: Path) -> None:
    """Delete directory and what's in it, read-only files included: git makes its objects
    read-only, which on Windows keeps rmtree from deleting them."""

    def make_writable_and_retry(remove: Callable[[str], object], path: str, *_: object) -> None:
        os.chmod(path, stat.S_IWRITE)
        remove(path)

    shutil.rmtree(directory, onexc=make_writable_and_retry)


def _repositories(root: Path) -> Iterator[Path]:
    """The git directory of each repository under root, root included, that's no submodule or
    linked worktree: each `.git` directory, and each bare repository's (a directory with
    `HEAD`, `config` and `objects`), which git then reads as one, or can't. A `.git` file
    (a submodule's, or a linked worktree's) isn't followed, so a submodule's own repository
    (under `.git/modules/`) isn't listed."""
    for directory, subdirectories, files in os.walk(root):
        if ".git" in subdirectories:
            yield Path(directory) / ".git"
            subdirectories.remove(".git")
        elif (
            ".git" not in files and {"HEAD", "config"} <= set(files) and "objects" in subdirectories
        ):
            yield Path(directory)
            subdirectories.clear()


# The keys of remotes' URLs, as `git config --get-regexp` takes them.
_URL_KEYS = r"^remote\..*\.(push)?url$"


def _remote_urls(root: Path) -> dict[Path, dict[str, list[str]]]:
    """The remotes' URLs (`url` and `pushurl`) in the config file of each repository under
    root (see _repositories; not files it includes), by its git directory, relative to root,
    then by key, in order, as git reads them: one `git config --get-regexp` per repository.
    One git can't read (`rev-parse --git-dir` fails too) has none; raises GitError where git
    fails otherwise (e.g. killed, or a usage error)."""
    found: dict[Path, dict[str, list[str]]] = {}
    for git_dir in _repositories(root):
        args = (f"--git-dir={git_dir}", "config", "--local", "-z", "--get-regexp", _URL_KEYS)
        git = Git(root, program=real_git())
        result = git.process(*args, capture_output=True)
        if result.returncode == 1:
            continue  # no URL
        if result.returncode != 0:
            if not git.succeeds(f"--git-dir={git_dir}", "rev-parse", "--git-dir"):
                # git can't read it (e.g. a bad config line, or no repository), so its copy
                # can't run git either: it's left as it is, for git to say so there.
                continue
            raise error_from(args, result)
        urls: dict[str, list[str]] = {}
        for entry in decode(result.stdout).split("\0"):
            # A key, then its value after a newline; a key with no value has no newline.
            key, newline, url = entry.partition("\n")
            if newline:
                urls.setdefault(key, []).append(url)
        if urls:
            found[git_dir.relative_to(root)] = urls
    return found


def _repoint_remotes(urls: dict[Path, dict[str, list[str]]], source: Path, dest: Path) -> None:
    """In each repository copied from `source` to `dest`, point every remote URL in `urls`
    (from `_remote_urls(source)`) that's a path under `source`, or a `file://` URL of one, at
    the same path under `dest`. Each that moves is set through `git config`, which writes it
    as git reads it (e.g. escaping Windows' backslashes): one process for a key with one
    value, and for one with several, one to clear it and one per value. Submodules aren't
    repointed (their remote, in `.git/modules/<name>/config`, and the superproject's
    `submodule.<name>.url` still name the template), nor linked worktrees (whose `gitdir:` is
    an absolute path into the template)."""
    for git_dir, keys in urls.items():
        git = Git(dest, program=real_git())
        local = (f"--git-dir={dest / git_dir}", "config", "--local")
        for key, values in keys.items():
            moved = [_moved(url, source, dest) for url in values]
            if moved == values:
                continue
            if len(moved) == 1:
                git.run(*local, key, moved[0])
            else:
                git.run(*local, "--unset-all", key)
                for url in moved:
                    git.run(*local, "--add", key, url)


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
    """Each git process a client (a `Git`, a `ReadOnlyGit`, or a subclass of either) starts
    during the test, in order, while git runs as it would: to count a tool's processes, or
    check the ones it starts. Each is a GitCall: git's arguments as the client was given them
    (without `git -C <directory>`, or a ReadOnlyGit's `-c`), and the git it ran in `program`.
    It leaves out the plugin's own, building and copying templates (see RepoTemplates),
    whichever fixture is set up first."""
    calls: list[GitCall] = []
    start_process, start_popen = git_client.client.start_process, git_client.client.start_popen

    def record(git: ReadOnlyGit, args: Sequence[Arg]) -> None:
        refuse_if_stopping(git, args)  # which starts nothing
        if not _setting_up.get():
            calls.append(GitCall([os.fsdecode(arg) for arg in args], git.program))

    def recording_process(
        git: ReadOnlyGit, args: Sequence[Arg], *rest: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        record(git, args)
        return start_process(git, args, *rest, **kwargs)

    def recording_popen(
        git: ReadOnlyGit, args: Sequence[Arg], *rest: Any
    ) -> subprocess.Popen[bytes]:
        record(git, args)
        return start_popen(git, args, *rest)

    # Where every git process of every client starts.
    monkeypatch.setattr(git_client.client, "start_process", recording_process)
    monkeypatch.setattr(git_client.client, "start_popen", recording_popen)
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
    once its `stopping` says so. Its `read_only()` is a ReadOnlyGit (not a FakeGit) that
    answers from the fake's responses and records in its `calls` (through FakeGit's own
    `process`, not a faked subclass's, as with a real Git): give that to code that only reads,
    so it has no public way to run a command of its own.

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
        object_id_length: int | None = None,
        process_groups: bool = True,
        remember_for: float | None = None,
    ) -> None:
        super().__init__(
            directory,
            program=program or "git",
            env_var=env_var,
            env=env,
            timeout=timeout,
            stopping=stopping,
            object_id_length=object_id_length,
            process_groups=process_groups,
            remember_for=remember_for,
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
        refuse_if_stopping(self, args)  # as a git process would
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

    def read_only(self, *, native: bool = False) -> ReadOnlyGit:
        """A ReadOnlyGit whose processes are the fake's, sharing what it was told of where
        things are (see Git.read_only). The fake always uses its scripted responses."""
        return _FakeReadOnlyGit(self)


class _FakeReadOnlyGit(ReadOnlyGit):
    """A FakeGit's `read_only()`: a ReadOnlyGit with the fake's settings and memory (as
    Git.read_only gives them) that answers each read through FakeGit's own `process` and
    `popen`, recording it in the fake's `calls`, but not through a faked subclass's override
    of them, as a real Git's subclass doesn't see what its read_only() starts."""

    def __init__(self, fake: FakeGit) -> None:
        super().__init__(
            fake.directory,
            program=fake.program,
            env=fake.env,
            timeout=fake.timeout,
            stopping=fake.stopping,
            object_id_length=fake._object_id_length,
            process_groups=fake.process_groups,
            remember_for=fake.remember_for,
        )
        self._memory = fake._memory
        self._fake = fake

    def _process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        # FakeGit's own, not a faked subclass's override, which a real Git's read_only() doesn't
        # reach either.
        return FakeGit.process(
            self._fake,
            *args,
            capture_output=capture_output,
            input=input,
            stdout=stdout,
            timeout=timeout,
        )

    def _popen(
        self, *args: Arg, stderr: int | IO[bytes] = subprocess.DEVNULL
    ) -> subprocess.Popen[bytes]:
        return FakeGit.popen(self._fake, *args, stderr=stderr)
