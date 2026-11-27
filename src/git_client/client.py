"""Running git: finding it once, starting it the quickest way each system allows, and passing
text and paths to it as git expects them."""

import contextlib
import contextvars
import enum
import functools
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import IO, Final, Literal, Self

from git_client.config_file import ConfigChange, ConfigResult, ConfigSet, ConfigUnset
from git_client.errors import (
    CORRUPT_OBJECT,
    MISSING_OBJECT,
    ConfigLockedError,
    CorruptObjectError,
    GitError,
    GitTimeoutError,
    MissingObjectError,
    NotStartedError,
    error_for,
)
from git_client.native import NativeObject, read_local_object
from git_client.performance import PerformanceRecorder, open_git_process, record_read

type Arg = str | bytes
"""An argument to git: text (str), or a path in the filesystem (bytes, in its encoding)."""

type EnvValue = str | bytes
"""A value for git's environment: text (str, e.g. an author's name), or a path in the
filesystem (bytes, in its encoding)."""


# git's exit status for a failure it explains ("fatal: ..."), unlike 129 for a usage error.
FATAL = 128
# git config's exit status for an unset that removes nothing, and what it says first when
# that's because the key has several values.
_NOTHING_SET = 5
_SEVERAL_VALUES = re.compile(r"^warning: .* has multiple values$", re.MULTILINE)

# What a git whose messages the library reads gets: as the directory of its translations, a
# path that's no directory, so git (and its shell and Perl scripts) finds none and speaks
# English, with C's dates, while what git runs (hooks, filters, credential helpers, gpg, ssh)
# keeps the user's locale. Only a git that one of those runs inherits it, and speaks English:
# git takes its messages' language from its environment alone, and passes that on to what it
# runs, so no variable could reach git and not its hooks.
# git itself keeps the user's LC_CTYPE (from 2.39), which its regex matching follows (see
# Git.process).
_ENGLISH = {"GIT_TEXTDOMAINDIR": os.devnull}

# What `git var` says when no identity is set for the name or email it's asked for.
_NO_IDENTITY = re.compile(
    "unable to auto-detect email address"
    "|no (email|name) was given and auto-detection is disabled"
    r"|empty ident name \(for <.*>\) not allowed"
)


class ClientTimeout(enum.Enum):
    """CLIENT_TIMEOUT's type: a sentinel, whose one member means "the client's timeout". It's
    an enum so that `Literal[ClientTimeout.CLIENT]` can type it, apart from None (no timeout)."""

    CLIENT = enum.auto()


CLIENT_TIMEOUT: Final = ClientTimeout.CLIENT
"""A command's timeout when it's given none of its own: the client's (see Git)."""

type Timeout = float | None | Literal[ClientTimeout.CLIENT]
"""A command's timeout, in seconds: None for none, or (the default) the client's."""


# What Windows runs in cmd.exe.
_BATCH = {".bat", ".cmd"}


@functools.cache
def find_git(env_var: str | None = None) -> str:
    """The git to run: the program `$env_var` names, if that's set; else the git on PATH (a
    wrapper such as git-ai included), by its full path, which posix_spawn needs. Found once per
    process. Raises FileNotFoundError if there's none: running plain "git" would let the OS
    look in the current directory, where a repository could hold a git of its own. On
    Windows, raises ValueError if `$env_var` names a batch file (.bat or .cmd), for the
    reason on_path gives."""
    if env_var and (configured := os.environ.get(env_var)):
        if sys.platform == "win32" and os.path.splitext(configured)[1].lower() in _BATCH:
            raise ValueError(
                f"${env_var} names a batch file, {configured}, which would run in cmd.exe:"
                " it would read &, | and % in git's arguments as its own; name an .exe"
            )
        return configured
    found = on_path("git")
    if found is None:
        raise FileNotFoundError(
            "git isn't in any directory on PATH" + (f"; ${env_var} can name it" if env_var else "")
        )
    return found


def on_path(name: str) -> str | None:
    """The full path of the program `name` in a directory on PATH, or None. Only absolute
    directories count: an empty or relative entry means the current directory, and on Windows
    shutil.which looks there first whatever PATH says, so a repository holding a git.exe
    would be run as git. On Windows, only `<name>.exe`, as Windows itself runs a name: a
    batch file (e.g. an npm shim, git.cmd) runs in cmd.exe, which would read characters in
    git's arguments (&, |, %; a branch name can hold them) as commands of its own."""
    filename = name + ".exe" if sys.platform == "win32" else name
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(directory):
            continue
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def decode(output: bytes) -> str:
    """git's output as text: UTF-8, which git writes on every system (not the locale's
    encoding, which on Windows usually isn't), keeping bytes that aren't UTF-8 (e.g. a file's,
    in a diff) as surrogates, so encode gives them back unchanged."""
    return output.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """Text, from decode or typed by a user, as the bytes git reads."""
    return text.encode("utf-8", "surrogateescape")


# The surrogates that don't stand for a byte decode couldn't read (e.g. from json.loads).
_NOT_A_BYTE = re.compile("[\ud800-\udc7f\udd00-\udfff]")


def printable(text: str) -> str:
    """Text from decode (as `run`, `config` and `refs` give it) with each byte that isn't
    UTF-8 replaced by U+FFFD, and any other surrogate too (`\\ud800`), as a tool that stores
    or shows text needs: decode keeps such bytes as surrogates, which can't be written as
    UTF-8 (to a file, a web page, JSON). Only for what's shown or stored: what printable
    gives can't be given back to git for what git gave."""
    return encode(_NOT_A_BYTE.sub("\ufffd", text)).decode("utf-8", "replace")


OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A full object id: SHA-1 or SHA-256."""

_OBJECT_ID_BYTES = re.compile(OBJECT_ID.pattern.encode())


def batch_line(name: str) -> bytes | None:
    """name as a line for `git cat-file`'s batch modes, or None if it can't name anything
    there: empty, or with a line ending, which would put cat-file out of step with what's
    read back (it would also take a name ending in \r as the name without it), or a NUL, up
    to which alone git would read it."""
    if not name or any(c in name for c in "\n\r\0"):
        return None
    return encode(name) + b"\n"


def object_header(line: bytes) -> tuple[str, str, int] | None:
    """The id, type and size in `git cat-file`'s header for an object (`<id> <type>
    <size>`), or None for its answer for a name that names nothing (`<name> missing`, or
    `ambiguous`, which with spaces in the name can be three words too)."""
    parts = line.split()
    if len(parts) != 3 or not _OBJECT_ID_BYTES.fullmatch(parts[0]) or not parts[2].isdigit():
        return None
    return parts[0].decode(), decode(parts[1]), int(parts[2])


# The name `resolve` sends cat-file before the caller's, as CatFile does its first request: one
# git answers rather than exits on, so having answered it, git can only exit on the name.
_PROBE_LINE = b"0" * 40 + b"\n"


def check_names_nothing(git: ReadOnlyGit, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> None:
    """After `git cat-file` exited on `name`: return if that's only because the name names
    nothing (cat-file exits on some, e.g. `@{u}` on a branch with no upstream), else raise
    the GitError for what made it exit (e.g. NotARepositoryError, or a broken config), as
    `git rev-parse --verify --quiet` finds it."""
    args = ("rev-parse", "--verify", "--quiet", "--end-of-options", name)
    result = read_process(
        git, *args, capture_output=True, stdout=subprocess.DEVNULL, timeout=timeout
    )
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr):
        raise error_from(args, result)


# How long git, asked to stop, has to clean up (e.g. remove index.lock) before it's killed: as
# long as subprocess gives a child after Ctrl-C.
_GRACE = 0.25

# How long a command that was stopped is given to let go of its output.
_AFTER_STOP = 1.0


def _own_process_group(timeout: float | None, process_groups: bool) -> bool:
    """Whether git starts in a process group of its own, so that past its timeout, or on an
    interrupt, what it started (hooks, ssh, git-remote-https, credential helpers) is stopped
    with it: on Linux and macOS, for a command with a timeout, when the tool has no
    controlling terminal (as in CI, or a service), unless the client's `process_groups` is
    False. With a terminal, git stays in the terminal's foreground group, as from a shell: a
    group of its own would be in the background, where reading the terminal (a password
    prompt, an editor, a pager) stops it until the timeout, and Ctrl-C would reach the tool
    alone. Only with a timeout, as a group keeps Python from using posix_spawn."""
    if timeout is None or not process_groups or sys.platform == "win32":
        return False
    return not _has_terminal(os.getpid(), os.getsid(0))


@functools.cache
def _has_terminal(pid: int, session: int) -> bool:
    """Whether the tool has a controlling terminal: probed once per process (`pid`, so a
    forked child probes again) and session (`session`, which os.setsid, the usual way to lose
    the terminal, changes). Not noticed: a session leader that takes a terminal later, which
    a tool doesn't do once it runs git; and a terminal lost in the same session (closed under
    a tool that ignores SIGHUP, as under nohup, or given up with TIOCNOTTY). Then a command
    with a timeout gets no group of its own, and what git started isn't stopped with it."""
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError:
        return False
    return True


def _stop(process: subprocess.Popen[bytes], own_group: bool, *, interrupted: bool) -> None:
    """Stop git, past its timeout or on an interrupt. On Linux and macOS, ask it to first, with
    SIGINT (to its group, if it has one of its own), as Ctrl-C would, so it removes its lock
    files, and kill it (and its group) after _GRACE, or as soon as the wait is interrupted
    (e.g. by a second Ctrl-C). On Windows, kill its process tree (taskkill /T) and git:
    Windows has no signal to ask a process with no console of its own to stop. On an
    interrupt, though, Ctrl-C reached git too, from the console it shares with the tool, so
    it's given _GRACE to clean up and exit first. taskkill finds what git started through
    git, so once git has exited, what outlived it runs on."""
    if sys.platform == "win32":
        try:
            if interrupted:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(_GRACE)
        finally:
            _kill_tree(process)
        return
    try:
        # Gone already, or (on macOS) only zombies left in its group.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if own_group:
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.send_signal(signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(_GRACE)
    finally:
        if own_group:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        process.kill()


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """On Windows, kill git and what it started (taskkill /F /T), or git alone if taskkill
    can't run (what it started then runs on)."""
    # By its full path, as find_git finds git's: not from the current directory.
    taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        with contextlib.suppress(OSError):
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    finally:
        process.kill()


def _let_go_of_output(process: subprocess.Popen[bytes]) -> None:
    """Stop reading git's output, which something it started may still hold open. On Windows,
    leave the pipes to communicate's reader threads, which end when the pipes do: closing one
    would wait for its thread's read, and so for whatever holds it."""
    for pipe in (process.stdout, process.stderr):
        if pipe is not None and sys.platform != "win32":
            pipe.close()
    process.stdout = process.stderr = None


def _remaining_output(process: subprocess.Popen[bytes]) -> tuple[bytes | None, bytes | None]:
    """What a stopped git printed, waiting at most _AFTER_STOP for the rest: something it
    started that outlived it could hold its output open."""
    try:
        return process.communicate(timeout=_AFTER_STOP)
    except subprocess.TimeoutExpired as expired:
        _let_go_of_output(process)
        return expired.stdout, expired.stderr


def _stdin(input: bytes | None, capture_output: bool) -> int | None:
    """git's stdin: a pipe for `input`; else nothing (DEVNULL) for a command whose output is
    captured, so one that would read stdin (e.g. `shortlog` with no revision, or
    `hash-object --stdin`) doesn't wait on, or take, the tool's; else the tool's, for a
    command run for the user (passthrough)."""
    if input is not None:
        return subprocess.PIPE
    return subprocess.DEVNULL if capture_output else None


# How many environments _environments keeps at most: one per mix of what a command adds.
_MOST_ENVIRONMENTS = 64


class _Environments:
    """The environments built for git (see ReadOnlyGit.environment), per what was added to
    ours: copying os.environ for every command costs about 50 µs, much of what starting git
    costs. They're dropped once os.environ changes, which comparing it with a copy of what it
    was then tells in about a microsecond."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[object, object] | None = None
        self._built: dict[object, dict[str, str]] = {}

    def get(self, key: object, build: Callable[[], dict[str, str]]) -> dict[str, str]:
        """The environment for `key`, built with `build` from os.environ unless one was
        already, since os.environ last changed. It's shared: don't change it."""
        # os.environ's own dict, looked up each time, as os.environ can be replaced (e.g. by
        # mock.patch); not there (e.g. a plain dict in its place): built anew every time.
        data: Mapping[object, object] | None = getattr(os.environ, "_data", None)
        with self._lock:
            if data is None:
                return build()
            if self._seen != data:
                self._seen = dict(data)
                self._built.clear()
            if key not in self._built:
                if len(self._built) >= _MOST_ENVIRONMENTS:
                    self._built.clear()
                self._built[key] = build()
            return self._built[key]


_environments = _Environments()


def error_from(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> GitError:
    """The GitError for a command that failed: a subclass for a failure the library knows from
    what git said (see errors.py)."""
    stderr, stdout = decode(result.stderr), decode(result.stdout or b"")
    command: object = result.args
    return error_for(stderr, stdout, command=command)(args, result.returncode, stderr, stdout)


# What a ReadOnlyGit's git gets, last: no fetching a missing object in a partial clone (which
# writes a pack; git before 2.45 ignores this, and fetches, but for the security releases
# 2.39.4 to 2.44.1). Not GIT_OPTIONAL_LOCKS=0: none of its reads (rev-parse, cat-file,
# for-each-ref, var, config --list) refreshes the index, as status and diff do.
_READ_ONLY_ENVIRONMENT = {"GIT_NO_LAZY_FETCH": "1"}

# What a ReadOnlyGit gives git before each command but `config --list` (whose entries it would
# join): no core.fsmonitor hook, a program the repository's config names, which git runs as it
# reads the index for a name such as `:path` (in `resolve`, CatFile and check_names_nothing).
_NO_FSMONITOR = ("-c", "core.fsmonitor=false")


def command_line(git: ReadOnlyGit, args: Sequence[Arg]) -> list[str] | list[bytes]:
    """git's command line for `args` (see Git.command)."""
    if sys.platform == "win32":
        return [git.program, "-C", git.directory, *map(os.fsdecode, args)]
    return [
        os.fsencode(git.program),
        b"-C",
        os.fsencode(git.directory),
        *(arg if isinstance(arg, bytes) else encode(arg) for arg in args),
    ]


def environment_for(
    git: ReadOnlyGit, env: Mapping[str, EnvValue] | None, *, english: bool, read_only: bool
) -> dict[str, str]:
    """git's environment (see Git.environment), and, if `read_only`, _READ_ONLY_ENVIRONMENT
    last. What it builds is kept, for the next command given the same, until os.environ
    changes."""
    added = {**git.env, **(env or {})}

    def build() -> dict[str, str]:
        return {
            **os.environ,
            **(_ENGLISH if english else {}),
            **{key: _environment_value(value) for key, value in added.items()},
            **(_READ_ONLY_ENVIRONMENT if read_only else {}),
        }

    # A copy of the one kept (see _Environments), which costs little: a dict's, not
    # os.environ's, which decodes each variable.
    return dict(_environments.get((english, read_only, tuple(added.items())), build))


def _environment_value(value: EnvValue) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return value if sys.platform == "win32" else os.fsdecode(encode(value))


def start_process(
    git: ReadOnlyGit,
    args: Sequence[Arg],
    command: list[str] | list[bytes],
    environment: dict[str, str],
    *,
    capture_output: bool,
    input: bytes | None,
    stdout: int | IO[bytes] | IO[str] | None,
    timeout: Timeout,
) -> subprocess.CompletedProcess[bytes]:
    """Run `command` (git's, for `args`) to its end, as Git.process says: where every git
    process but a long-lived one starts, a ReadOnlyGit's and a Git's (git_calls wraps it)."""
    refuse_if_stopping(git, args)
    seconds = git.timeout if timeout is CLIENT_TIMEOUT else timeout
    own_group = _own_process_group(seconds, git.process_groups)
    with open_git_process(
        command,
        git.performance,
        args,
        persistent=False,
        stdin=_stdin(input, capture_output),
        stdout=subprocess.PIPE if capture_output and stdout is None else stdout,
        stderr=subprocess.PIPE if capture_output else None,
        env=environment,
        process_group=0 if own_group else None,
    ) as process:
        try:
            output, errors = process.communicate(input, timeout=seconds)
        except subprocess.TimeoutExpired:
            _stop(process, own_group, interrupted=False)
            output, errors = _remaining_output(process)
            raise GitTimeoutError(
                args, None, decode(errors or b""), decode(output or b"")
            ) from None
        except BaseException:  # e.g. KeyboardInterrupt
            _stop(process, own_group, interrupted=True)
            _let_go_of_output(process)
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, output, errors)


# Set while Started starts a process through popen: whether git gets a process group of its
# own, as start_process gives one for the same timeout.
_started_in_own_group: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "_started_in_own_group", default=None
)


def start_popen(
    git: ReadOnlyGit,
    args: Sequence[Arg],
    command: list[str] | list[bytes],
    environment: dict[str, str],
    stderr: int | IO[bytes],
) -> subprocess.Popen[bytes]:
    """Start `command` (git's, for `args`) as Git.popen says: where every long-lived git
    process starts, a ReadOnlyGit's and a Git's (git_calls wraps it), and those Started
    starts, to collect later."""
    refuse_if_stopping(git, args)
    own_group = _started_in_own_group.get()
    return open_git_process(
        command,
        git.performance,
        args,
        persistent=own_group is None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=environment,
        process_group=0 if own_group else None,
    )


class Started:
    """A git process started now, through the client's `popen` (so a Git subclass that
    overrides it, and git_calls, see it start), whose finished process `result()` gives
    later, as `process(..., capture_output=True)` would: git's messages in English, stdout
    captured, stderr too (in a temporary file, as popen takes no pipe for it); past the
    client's timeout (counted from the start) stopped, raising GitTimeoutError; on an
    interrupt, stopped; in a process group of its own where `process` would give it one.
    Leaving its `with` block before the result stops it. For a caller that starts several
    independent processes, then collects each in turn: none waits on another, so that can't
    deadlock."""

    def __init__(
        self,
        git: ReadOnlyGit,
        *args: Arg,
        input: bytes | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> None:
        seconds = git.timeout if timeout is CLIENT_TIMEOUT else timeout
        self._args, self._input = args, input
        self._own_group = _own_process_group(seconds, git.process_groups)
        self._deadline = None if seconds is None else time.monotonic() + seconds
        import tempfile  # here: importing it takes milliseconds every tool would pay

        # Closed in __exit__, or here if git doesn't start.
        self._errors = tempfile.TemporaryFile()  # noqa: SIM115
        token = _started_in_own_group.set(self._own_group)
        try:
            self.process = read_popen(git, *args, stderr=self._errors)
        except BaseException:
            self._errors.close()
            raise
        finally:
            _started_in_own_group.reset(token)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        if self.process.poll() is None:
            _stop(self.process, self._own_group, interrupted=True)
            _let_go_of_output(self.process)
        self._errors.close()

    def result(self) -> subprocess.CompletedProcess[bytes]:
        """The finished process (see the class)."""
        remaining = None if self._deadline is None else self._deadline - time.monotonic()
        try:
            output, _ = self.process.communicate(self._input, timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop(self.process, self._own_group, interrupted=False)
            output, _ = _remaining_output(self.process)
            raise GitTimeoutError(
                self._args, None, decode(self._stderr()), decode(output or b"")
            ) from None
        except BaseException:  # e.g. KeyboardInterrupt
            _stop(self.process, self._own_group, interrupted=True)
            _let_go_of_output(self.process)
            raise
        return subprocess.CompletedProcess(
            self.process.args, self.process.returncode, output, self._stderr()
        )

    def _stderr(self) -> bytes:
        self._errors.seek(0)
        return self._errors.read()


def stdout_of(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> bytes:
    """What git printed to stdout, if it succeeded. Raises GitError if it failed."""
    if result.returncode != 0:
        raise error_from(args, result)
    return result.stdout


def read_process(
    git: ReadOnlyGit,
    *args: Arg,
    capture_output: bool = False,
    input: bytes | None = None,
    stdout: int | IO[bytes] | IO[str] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """Run one of the library's reads, a command it fixes, through `git`: the way every typed
    read (e.g. `refs`, `config`, `resolve`) starts a process. A Git's goes through its own
    `process`, which a subclass may override; a ReadOnlyGit offers no such method."""
    return git._process(  # pyright: ignore[reportPrivateUsage]
        *args, capture_output=capture_output, input=input, stdout=stdout, timeout=timeout
    )


def read_popen(
    git: ReadOnlyGit, *args: Arg, stderr: int | IO[bytes] = subprocess.DEVNULL
) -> subprocess.Popen[bytes]:
    """Start one of the library's long-lived reads (CatFile's) through `git`, as read_process
    runs the others: a Git's through its own `popen`."""
    return git._popen(*args, stderr=stderr)  # pyright: ignore[reportPrivateUsage]


def read_output(
    git: ReadOnlyGit, *args: Arg, input: bytes | None = None, timeout: Timeout = CLIENT_TIMEOUT
) -> bytes:
    """What one of the library's reads printed, as Git.run_bytes gives it, run as read_process
    runs it."""
    result = read_process(git, *args, capture_output=True, input=input, timeout=timeout)
    return stdout_of(args, result)


type Location = tuple[str, str, str, tuple[tuple[str, EnvValue], ...]]


def location(git: ReadOnlyGit) -> Location:
    """The git program, directory and client environment that identify cached answers.

    Keep '..' in the absolute path so Git can resolve it after traversing symlinks.
    Include the current directory for relative programs, which can resolve differently
    even when the repository's path is absolute.
    """
    directory = git.directory
    program_dir = os.getcwd() if git.cache is not None and not os.path.isabs(git.program) else ""
    if not os.path.isabs(directory):
        directory = os.path.join(program_dir or os.getcwd(), directory)
    return git.program, program_dir, directory, tuple(sorted(git.env.items()))


class AskingFormat:
    """One thread's request for the object format and the answer awaited by its peers."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.format: bytes | None = None


class GitCache:
    """Optional, thread-safe cache of repository locations and object formats.

    Share it across clients in one refresh or command. Answers are kept until the
    caller clears or discards the cache. Keep the process environment stable during
    that lifetime, and clear the cache if a worktree or repository moves.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answers: dict[tuple[Location, str], bytes] = {}
        self._generation = 0
        self._asking_format: dict[tuple[Location, int], AskingFormat] = {}

    def __deepcopy__(self, memo: dict[int, object]) -> GitCache:
        """Give a deep copy its own empty cache."""
        return GitCache()

    def __reduce__(self) -> tuple[type[GitCache], tuple[()]]:
        """A cache sent to another process starts empty."""
        return GitCache, ()

    def clear(self) -> None:
        """Discard stored answers and prevent earlier queries from repopulating the cache."""
        with self._lock:
            self._answers.clear()
            self._generation += 1

    def _recall_locked(self, where: Location, questions: Sequence[str]) -> dict[str, bytes] | None:
        """Return the answers if all are known, while holding the lock."""
        found: dict[str, bytes] = {}
        for question in questions:
            if (known := self._answers.get((where, question))) is None:
                return None
            found[question] = known
        return found

    def _recall(self, where: Location, questions: Sequence[str]) -> dict[str, bytes] | None:
        with self._lock:
            return self._recall_locked(where, questions)

    def _asking(self) -> int:
        with self._lock:
            return self._generation

    def _remember(self, where: Location, answers: Mapping[str, bytes], asked: int) -> None:
        """Store the batch atomically unless it began before the cache was cleared."""
        with self._lock:
            if asked == self._generation:
                self._answers.update(
                    ((where, question), answer) for question, answer in answers.items()
                )

    def _questions(self, where: Location, needed: Sequence[str], format_given: bool) -> list[str]:
        """Get the metadata options for a batch based on one snapshot of the cache."""
        questions: list[str] = []
        with self._lock:
            if TOP_QUESTIONS[0] in needed or self._recall_locked(where, TOP_QUESTIONS[:2]) is None:
                questions += TOP_QUESTIONS
            if not format_given and (where, _FORMAT_QUESTION) not in self._answers:
                questions.append(_FORMAT_QUESTION)
            if DIR_QUESTIONS[0] in needed or self._recall_locked(where, DIR_QUESTIONS) is None:
                questions += DIR_QUESTIONS
        return questions

    def _object_format(self, where: Location, ask: Callable[[], bytes]) -> bytes:
        """Share the answer of a concurrent object-format query for this location."""
        while True:
            with self._lock:
                known = self._recall_locked(where, [_FORMAT_QUESTION])
                if known is not None:
                    return known[_FORMAT_QUESTION]
                generation = self._generation
                key = where, generation
                pending = self._asking_format.get(key)
                mine = pending is None
                if pending is None:
                    pending = self._asking_format[key] = AskingFormat()
            if not mine:
                pending.done.wait()
                if pending.format is not None:
                    return pending.format
                continue
            try:
                pending.format = ask()
                self._remember(where, {_FORMAT_QUESTION: pending.format}, generation)
                return pending.format
            finally:
                with self._lock:
                    del self._asking_format[key]
                pending.done.set()


def git_path_question(name: str) -> str:
    """What `GitCache` keeps `--git-path name`'s answer under: a name holds no line ending (see
    `git_paths`), so nothing else is kept under it."""
    return f"--git-path\n{name}"


def recall_where(git: ReadOnlyGit, where: Location, *questions: str) -> dict[str, bytes] | None:
    """Look up the requested answers if this client has a cache."""
    cache = git.cache
    return None if cache is None else cache._recall(where, questions)  # pyright: ignore[reportPrivateUsage]


def asking(git: ReadOnlyGit) -> int:
    """The cache generation at the start of a query."""
    cache = git.cache
    return 0 if cache is None else cache._asking()  # pyright: ignore[reportPrivateUsage]


def remember_where(
    git: ReadOnlyGit, where: Location, answers: Mapping[str, bytes], asked: int
) -> None:
    """Store answers if this client has a cache."""
    cache = git.cache
    if cache is not None:
        cache._remember(where, answers, asked)  # pyright: ignore[reportPrivateUsage]


# What asks for prefix() and cdup(). Where git knows no worktree (in a bare repository, or a
# git directory), it prints no cdup at all, not even an empty line (where it knows the top but
# the directory is outside it, it prints the top's absolute path). --is-inside-work-tree's
# "true" or "false" after it tells the two apart (see read_where).
TOP_QUESTIONS = ("--show-prefix", "--show-cdup", "--is-inside-work-tree")
_INSIDE_WORK_TREE = (b"true", b"false")
# What asks for git_dir and git_common_dir, absolute paths (see where_command).
DIR_QUESTIONS = ("--git-dir", "--git-common-dir")
_FORMAT_QUESTION = "--show-object-format"


class LinesUnfit(ValueError):
    """rev-parse's lines can't be told apart: there are more than the answers asked of it, or
    as many with "true" or "false" after the prefix (no cdup, and a line more). A path it
    printed holds a newline. The caller then asks each question in a process of its own (see
    `ask_where_alone`)."""


def where_questions(git: ReadOnlyGit, where: Location, needed: Sequence[str] = ()) -> list[str]:
    """The `rev-parse` options that ask git what the client doesn't know yet at `where` (its
    `location`), and those of `needed` (TOP_QUESTIONS or DIR_QUESTIONS, which the caller
    wants answered whatever is known), in this order: `--show-prefix` and `--show-cdup` (with
    `--is-inside-work-tree`) for `prefix()` and `cdup()`, `--show-object-format` for
    `object_id_length()` (unless the client was given it), and `--git-dir` and
    `--git-common-dir` for `git_dir` and `git_common_dir`. A function that runs `rev-parse`
    anyway runs `where_command(questions)`, then its own options, so they cost no process of
    their own, then reads the lines git printed with `read_where`."""
    cache = git.cache
    if cache is None:
        return list(needed)
    format_given = git._object_id_length is not None  # pyright: ignore[reportPrivateUsage]
    return cache._questions(where, needed, format_given)  # pyright: ignore[reportPrivateUsage]


def where_command(questions: Sequence[str]) -> list[str]:
    """The `rev-parse` that asks `questions` (from `where_questions`), to which a caller adds
    its own options: every path it prints is absolute (which changes neither the prefix nor
    the cdup, nor a `--git-path` given after it)."""
    return ["rev-parse", "--path-format=absolute", *questions]


def read_where(
    questions: Sequence[str], lines: list[bytes], rest: int
) -> tuple[dict[str, bytes], list[bytes]]:
    """git's answers to `questions` (from `where_questions`), by question, from the first of
    `lines`, what `rev-parse` printed, and the `rest` lines after them, the caller's answers
    (each one line, unless it's a path with a newline in it). Raises ValueError where there
    are too few lines (e.g. a FakeGit's answer a line short), and LinesUnfit where a line's
    answer can't be told (then the caller asks each question in a process of its own).

    Each answer is one line, but a path's (the prefix, an absolute cdup, the git directories,
    a `--git-path`), which is one more line for each newline in it, and the cdup's, which is
    none where git knows no worktree (and its prefix is then "", so the next line is
    --is-inside-work-tree's "true" or "false"). A missing cdup is the only answer short of a
    line, so with `n` lines asked for in all: n - 1 lines is one each but no cdup; n lines
    is either one each, or no cdup and one newline, told apart by the line after the prefix,
    "true" or "false" only after no cdup (a cdup is "", "../" repeated or an absolute path);
    more, or n with no cdup, is newlines that could be anywhere: LinesUnfit."""
    asked = len(questions) + rest
    cdup = "--show-cdup" in questions
    if len(lines) == asked and not (cdup and lines[1] in _INSIDE_WORK_TREE):
        answers = dict(zip(questions, lines, strict=False))
    elif cdup and len(lines) == asked - 1:
        answers = dict(zip([q for q in questions if q != "--show-cdup"], lines, strict=False))
        answers["--show-cdup"] = b""  # git printed none
    elif len(lines) < asked - cdup:
        raise ValueError(f"rev-parse printed {len(lines)} lines, too few for {asked}")
    else:
        raise LinesUnfit(f"rev-parse printed {len(lines)} lines for {asked}")
    if answers.get("--is-inside-work-tree", b"true") not in _INSIDE_WORK_TREE:
        # Not git's answer (e.g. a FakeGit's, made for another question).
        raise ValueError(f"rev-parse's lines don't answer {questions}")
    return answers, lines[len(lines) - rest :]


def ask_where_alone(
    git: ReadOnlyGit, questions: Sequence[str], *, timeout: Timeout = CLIENT_TIMEOUT
) -> dict[str, bytes]:
    """git's answers to `questions` (from `where_questions`), each asked in a `rev-parse` of
    its own, each its whole output but the newline that ends it: for where their answers in
    one `rev-parse` couldn't be told apart (LinesUnfit), as a path with a newline in it makes
    them."""
    return {
        question: read_output(git, *where_command([question]), timeout=timeout).removesuffix(b"\n")
        for question in questions
        if question != "--is-inside-work-tree"
    }


def ask_where(
    git: ReadOnlyGit, where: Location, needed: Sequence[str], timeout: Timeout
) -> dict[str, bytes]:
    """git's answers to `needed` (TOP_QUESTIONS or DIR_QUESTIONS) at `where`, from memory or
    else from one `rev-parse` that asks whatever else isn't known too, and remembers it all."""
    found = recall_where(git, where, *needed[:2])
    if found is None:
        asked = asking(git)
        questions = where_questions(git, where, needed)
        # Not run's, which would strip a directory's leading space.
        output = read_output(git, *where_command(questions), timeout=timeout)
        try:
            found, _ = read_where(questions, rev_parse_lines(output), 0)
        except LinesUnfit:
            found = ask_where_alone(git, questions, timeout=timeout)
        remember_where(git, where, found, asked)
    return found


def rev_parse_lines(output: bytes) -> list[bytes]:
    """The lines of rev-parse's output. Not splitlines(): an answer can be an empty line (e.g.
    the prefix's at the top), and only "\\n" ends one. A path git prints can hold a newline
    (off Windows), which makes it two lines: `read_where` tells."""
    return output.removesuffix(b"\n").split(b"\n") if output else []


def object_id_length_of(format: bytes) -> int:
    """The length of an object id in the object format `rev-parse --show-object-format`
    names."""
    return 64 if format == b"sha256" else 40


def refuse_if_stopping(git: ReadOnlyGit, args: Sequence[Arg]) -> None:
    """Raise NotStartedError if `git`'s `stopping` says the tool is stopping: checked before
    each git process starts (FakeGit's too)."""
    if git.stopping is not None and git.stopping():
        raise NotStartedError(args, None, "the tool is stopping")


def local_object(
    git: ReadOnlyGit,
    name: str,
    args: Sequence[Arg],
    *,
    contents: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> NativeObject | None:
    """Try a read that can finish without invoking git."""
    if not git.native or isinstance(git, Git):
        return None
    if (git.timeout if timeout is CLIENT_TIMEOUT else timeout) is not None:
        return None
    refuse_if_stopping(git, args)
    return read_local_object(git.directory, git.env, name, contents=contents)


class ReadOnlyGit:
    """Reads a repository with git in `directory`, as `git -C <directory>` does, offering the
    library's reads alone, each a command the library fixes: `resolve`, `var`, `prefix`,
    `cdup` and `object_id_length`, and the functions that take a ReadOnlyGit (`refs`,
    `config`, `git_paths`, `git_paths_and_head`, `git_dir`, `git_common_dir` and `CatFile`).
    It has no public way to run a command of the caller's (no `run`, `process` and the like),
    so a tool that only reads can show that it does by making one: what it doesn't offer, it
    can't be asked to run (the library's private helpers can, as anything private can be
    misused). Git extends it with those, so a function that only reads takes a
    ReadOnlyGit, and is given either; a Git's `read_only()` gives a ReadOnlyGit of its own. A
    tool that reads with other commands (`log`, `status`, ...) runs them through a Git.

    Its git gets GIT_NO_LAZY_FETCH=1, so in a partial clone a missing object isn't fetched (git
    2.45 and newer, and 2.39.4 to 2.44.1; older git fetches it): `resolve` and CatFile take it
    as a name that names nothing. Each command but `config --list` gets `-c
    core.fsmonitor=false`, so no core.fsmonitor hook runs as git reads the index for a name
    such as `:path`. They keep its reads from writing; they're not a promise that git writes
    nothing: the tool's environment reaches git as it is (e.g. GIT_TRACE*, which write where
    they name).

    `program` is the git to run, or else `find_git(env_var)`'s: a project names its own
    variable (e.g. `GIT_STACK_GIT`) so its users can choose. Without `program`, it raises
    FileNotFoundError if there's no git to find (see find_git). `env` is added to the
    environment of every command. `timeout` is how many seconds a command may run (None: as
    long as it takes), unless given its own; one that runs longer is stopped, and raises
    GitTimeoutError. `stopping`, if given, is asked before each git process starts (e.g.
    `threading.Event().is_set`, or a function of the tool's): once it says True, every
    command raises NotStartedError rather than start git, so a tool that's shutting down
    winds down fast, whichever thread runs them. It doesn't stop a git that's already running: a
    command runs on until it finishes or times out (or the thread running it is
    interrupted), and a CatFile's process keeps answering until it's closed, though one
    that has to start (again) raises. `object_id_length` (40 for SHA-1, 64 for SHA-256), if
    given, is the repository's, which `object_id_length()` then returns without starting git:
    for a tool that knows it some other way than asking git. It's trusted, not checked (that
    would start the process it saves): a wrong one makes what reads ids by it (e.g.
    `patch_ids`) fail.

    Give `cache` a GitCache to reuse repository locations and object formats across calls.
    A Git and its `read_only()` view share that cache. It keeps answers until cleared
    or discarded, so use a fresh cache for each refresh in a long-lived tool.

    Past its timeout, or when the tool is interrupted (KeyboardInterrupt) while it runs, git is
    first asked to stop, so it can remove its lock files (e.g. index.lock), and killed a quarter
    of a second later, or at once if the tool is interrupted again meanwhile; the call then
    waits at most a second more for its output. On Linux and macOS it's asked with SIGINT, as
    Ctrl-C asks it. When the tool has no controlling terminal (as in CI, or a service), a
    command with a timeout runs in a process group of its own, so what git started (hooks, ssh,
    credential helpers) is stopped with it. With a terminal, git stays in its foreground, as
    from a shell, so it can ask for a password and open an editor, and Ctrl-C reaches it; only
    git is stopped then. On Windows, git and what it started are killed (taskkill /T): past a
    timeout at once, with no chance to clean up, and on an interrupt once git has had a quarter
    of a second to handle the Ctrl-C that reached it too, from the tool's console. taskkill
    finds what git started through git, so what outlives git's own exit (e.g. a hook that
    ignores Ctrl-C) runs on.

    A git in a group of its own doesn't get a signal sent to the tool's group (e.g. SIGTERM
    from `timeout` or a CI runner cancelling a job), and Python's default SIGTERM ends the
    tool without stopping it, so git and its hooks run on. A tool should turn SIGTERM into
    an exception in its main thread, e.g. `signal.signal(signal.SIGTERM, lambda *_:
    sys.exit(143))`: any exception that reaches a running command stops git as an interrupt
    does. Like Ctrl-C's KeyboardInterrupt, it's raised in the main thread alone, so it stops
    only a git the main thread runs. A git another thread runs goes on until it finishes or
    times out, and is orphaned if the tool is killed first; to have it stopped with the
    tool, give those commands no timeout, so git stays in the tool's group and gets the
    signal itself.

    With `process_groups=False`, git never gets a group of its own: past a timeout, or on an
    interrupt, only git is stopped, and what it started (hooks, ssh, credential helpers) runs
    on, as with a terminal. In exchange, starting git costs less: on macOS, a group of its own
    keeps Python from using posix_spawn, and on Linux it costs about 50 µs a command (a
    twentieth of what starting git costs). It suits a tool that runs many short commands
    with a timeout and without a terminal, whose git starts nothing that could outlive it.

    Git commands start with only their standard streams.

    With `native=True`, this reader can use the optional pygit2 backend for local SHA-1
    objects addressed by their full ids. Git itself uses subprocesses for all operations.

    Give `performance` a PerformanceRecorder to time the client's public reads and
    subprocesses. A Git and its `read_only()` share the recorder.

    Every command is given its directory with `-C` rather than as a working directory, which
    would keep Python from using posix_spawn.
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
        cache: GitCache | None = None,
        native: bool = False,
        performance: PerformanceRecorder | None = None,
    ) -> None:
        if object_id_length not in (None, 40, 64):
            raise ValueError(f"object_id_length is 40 or 64, not {object_id_length}")
        self.directory = os.fspath(directory)
        self.program = program or find_git(env_var)
        self.env: dict[str, EnvValue] = dict(env or {})
        self.timeout = timeout
        self.stopping = stopping
        self.process_groups = process_groups
        self._object_id_length = object_id_length
        self._cache = cache
        self.native = native and not isinstance(self, Git)
        self.performance = performance

    @property
    def cache(self) -> GitCache | None:
        """The caller's cache, if one was supplied."""
        return self._cache

    def read_only(self) -> ReadOnlyGit:
        """This client itself, which is read-only already (a Git's gives a ReadOnlyGit), so
        that code given either can ask any client for one."""
        return self

    def forget(self) -> None:
        """Clear the attached cache, including answers shared with other clients."""
        if self.cache is not None:
            self.cache.clear()

    @record_read()
    def object_id_length(self) -> int:
        """Return the repository's object ID length, 40 for SHA-1 or 64 for SHA-256.

        An explicit `object_id_length` takes precedence. Otherwise ask Git or use the
        caller's cache, sharing a concurrent query for the same location when cached.
        The format can differ from an ID printed with `extensions.compatObjectFormat`.
        """
        if self._object_id_length is not None:
            return self._object_id_length

        def ask() -> bytes:
            return read_output(self, "rev-parse", _FORMAT_QUESTION).strip()

        cache = self.cache
        if cache is None:
            return object_id_length_of(ask())
        return object_id_length_of(cache._object_format(location(self), ask))  # pyright: ignore[reportPrivateUsage]

    @record_read()
    def prefix(self, *, timeout: Timeout = CLIENT_TIMEOUT) -> str:
        """The path from the top of the worktree down to the client's directory, with a slash
        after each directory (e.g. "src/app/"), as `git rev-parse --show-prefix` gives it: ""
        at the top, and outside a worktree (e.g. in a bare repository or a git directory).
        Put it before a path from the directory to name that path from the top. Raises
        GitError if git fails (e.g. NotARepositoryError).

        Asked of Git with `cdup()`. With a cache, the same process also fills in
        uncached repository metadata."""
        return self._top(timeout)[0]

    @record_read()
    def cdup(self, *, timeout: Timeout = CLIENT_TIMEOUT) -> str:
        """The path from the client's directory up to the top of the worktree, as `git
        rev-parse --show-cdup` gives it: "../" for each directory of `prefix()` ("" at the
        top), or the top's absolute path where the directory is outside a worktree whose top
        git knows (e.g. GIT_WORK_TREE names it); "" where git knows none (in a bare
        repository, or a git directory), and prints nothing. Put it before a path from the top
        to name that path from the directory. Raises GitError if git fails (e.g.
        NotARepositoryError). Asked of Git with `prefix()` and cached when a cache is attached."""
        return self._top(timeout)[1]

    def _top(self, timeout: Timeout) -> tuple[str, str]:
        """`prefix()` and `cdup()`."""
        found = ask_where(self, location(self), TOP_QUESTIONS, timeout)
        return decode(found["--show-prefix"]), decode(found["--show-cdup"])

    @record_read()
    def resolve(self, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> str | None:
        """The id of the object `name` names, as `CatFile.resolve` finds it, or None if it names
        nothing: as `git rev-parse --verify` finds it, but only if the repository has that
        object (not for a full id it lacks, nor for a submodule's commit). Raises
        CorruptObjectError if an object git reads for it is damaged, MissingObjectError if it's
        in a partial clone that couldn't fetch it (from a Git: a ReadOnlyGit's gives None), and
        GitError for another failure (e.g. NotARepositoryError). A native-enabled reader may
        answer a full SHA-1 id in-process. Otherwise it takes one process; for many names,
        a CatFile shares one process."""
        line = batch_line(name)
        if line is None:
            return None
        args = ("cat-file", "--batch-check")
        if (found := local_object(self, name, args, timeout=timeout)) is not None:
            return found.id
        result = self._process(
            *args, capture_output=True, input=_PROBE_LINE + line, timeout=timeout
        )
        answered, _, reply = result.stdout.partition(b"\n")
        if answered != _PROBE_LINE[:-1] + b" missing":
            if not answered and result.returncode != 0:
                # It exited as it started, as it does when it can't read the repository.
                raise error_from(args, result)
            # Not git's answer (e.g. a FakeGit's, made before resolve sent the name first).
            raise GitError(
                args,
                result.returncode,
                f"cat-file answered {decode(answered)!r} for the name of all zeros sent first,"
                " not that it's missing",
            )
        header = object_header(reply)
        if header is None:
            # Unless an object git read for it was damaged, as CatFile tells it.
            if CORRUPT_OBJECT.search(stderr := decode(result.stderr)):
                raise CorruptObjectError(args, result.returncode, stderr)
            if MISSING_OBJECT.search(stderr):
                raise MissingObjectError(args, result.returncode, stderr)
            return None  # if it exited, it was on the name, which names nothing
        return header[0]

    @record_read()
    def var(self, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> str | None:
        """git's value for one of its variables (as `git var`, e.g. GIT_AUTHOR_IDENT), or None
        if no identity is set for it. Raises GitError for an unknown variable, or another
        failure (e.g. a broken config)."""
        result = self._process("var", name, capture_output=True, timeout=timeout)
        if result.returncode == FATAL and _NO_IDENTITY.search(decode(result.stderr)):
            return None
        return decode(stdout_of(("var", name), result)).strip()

    def _process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one of the library's reads (see read_process), as Git.process runs a command,
        with ReadOnlyGit's environment and `-c` (see the class)."""
        command = args if args[0] == "config" else (*_NO_FSMONITOR, *args)
        return start_process(
            self,
            args,
            command_line(self, command),
            environment_for(self, None, english=capture_output, read_only=True),
            capture_output=capture_output,
            input=input,
            stdout=stdout,
            timeout=timeout,
        )

    def _popen(
        self, *args: Arg, stderr: int | IO[bytes] = subprocess.DEVNULL
    ) -> subprocess.Popen[bytes]:
        """Start one of the library's long-lived reads (see read_popen), as Git.popen starts a
        command, with ReadOnlyGit's environment and `-c` (see the class)."""
        return start_popen(
            self,
            args,
            command_line(self, (*_NO_FSMONITOR, *args)),
            environment_for(self, None, english=True, read_only=True),
            stderr,
        )


class Git(ReadOnlyGit):
    """Runs any git command in `directory`, as `git -C <directory>` does: a ReadOnlyGit, with
    the commands of the caller's (`run`, `run_bytes`, `ok`, `succeeds`, `passthrough`,
    `process` and `popen`) and writes, and without the variables and `-c` a ReadOnlyGit gives
    git. How it finds git, and stops one past its timeout, is ReadOnlyGit's (see there).

    Every process it starts, its reads' too (`resolve`, `refs`, `config`, `CatFile`, ...),
    starts through its `process` or `popen`, so a subclass that overrides them (e.g. to time
    each) sees them all; but not those of the ReadOnlyGit its `read_only()` gives, a client of
    its own."""

    def read_only(self, *, native: bool = False) -> ReadOnlyGit:
        """A new ReadOnlyGit with this client's current settings, cache and recorder.

        Pass `native=True` to enable pygit2 on the reader. Its subprocesses use the
        reader's own launch path, separate from overrides of this client's process methods.
        """
        return ReadOnlyGit(
            self.directory,
            program=self.program,
            env=self.env,
            timeout=self.timeout,
            stopping=self.stopping,
            object_id_length=self._object_id_length,
            process_groups=self.process_groups,
            cache=self.cache,
            native=native,
            performance=self.performance,
        )

    def _process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        """One of the library's reads, through `process`, which a subclass may override."""
        return self.process(
            *args, capture_output=capture_output, input=input, stdout=stdout, timeout=timeout
        )

    def _popen(
        self, *args: Arg, stderr: int | IO[bytes] = subprocess.DEVNULL
    ) -> subprocess.Popen[bytes]:
        """One of the library's long-lived reads, through `popen`, which a subclass may
        override."""
        return self.popen(*args, stderr=stderr)

    def command(self, *args: Arg) -> list[str] | list[bytes]:
        """git's command line. On Windows, as str, as the OS keeps it. Elsewhere, as bytes:
        text as git wrote it (whatever the locale's encoding, which in the C locale can't hold
        a name that isn't ASCII), and paths in the filesystem's encoding."""
        return command_line(self, args)

    def environment(
        self, env: Mapping[str, EnvValue] | None = None, *, english: bool = False
    ) -> dict[str, str]:
        """The environment for a command: ours, then (if `english`) _ENGLISH, so git's
        messages are in English for the library to read (what git runs keeps the user's
        locale), then this client's `env`, then `env`. Off Windows, git gets each value as
        bytes, like `command`: text as UTF-8 (whatever the locale's encoding), and a path
        given as bytes as it is. Each is given here as the str that os.fsdecode makes of
        those bytes, which Python turns back into them as it starts git. What it builds is
        kept, for the next command given the same, until os.environ changes."""
        return environment_for(self, env, english=english, read_only=False)

    def process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run git and return the finished process, whatever its exit status. Raises
        GitTimeoutError if it runs past its timeout (see Git). With `capture_output`, its
        stderr is captured, and its stdout too unless `stdout` says where it goes (e.g.
        subprocess.DEVNULL); git's messages are then in English whatever the user's language,
        while what git runs (hooks, filters, credential helpers, gpg, ssh) keeps the user's
        locale (see `environment`). git's own pattern matching then follows the user's
        LC_CTYPE from git 2.39 (`log --grep`, `--author`, `-G`, `-i`, `git grep`), and C's
        before, and `-P` patterns are UTF-8 on every version. Otherwise its output is the
        user's to read, in theirs. Raises NotStartedError, starting nothing, once `stopping`
        says so. Every git process the client starts but `popen`'s, its reads' too, starts
        here, so a subclass can override this to change how each runs, calling
        `super().process`.

        git reads `input`, if given; else, with `capture_output`, nothing (as from
        /dev/null), so a command that reads stdin when given no input (e.g. `shortlog` with no
        revision) neither waits on the tool's stdin nor takes it; without, the tool's stdin, as
        from a shell."""
        return start_process(
            self,
            args,
            self.command(*args),
            self.environment(env, english=capture_output),
            capture_output=capture_output,
            input=input,
            stdout=stdout,
            timeout=timeout,
        )

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        """Start git with pipes for its stdin and stdout, for a process that answers requests
        until its stdin closes (e.g. `cat-file --batch`). Its stderr goes nowhere, or to
        `stderr`: a file, say, not a pipe, which nothing would read until it filled and git
        stopped. It has no timeout, and its messages are in English (see `process`). Raises
        NotStartedError, starting nothing, once `stopping` says so. A subclass that overrides
        it must take `stderr` and pass it on to `super().popen`: CatFile reads git's reasons
        there."""
        return start_popen(
            self, args, self.command(*args), self.environment(env, english=True), stderr
        )

    def run_bytes(
        self,
        *args: Arg,
        input: bytes | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bytes:
        """Run git and return its stdout as it is (e.g. a file's content, or a patch).
        Raises GitError if it fails: a subclass (e.g. UnknownRevisionError) for a failure the
        library knows from what git said."""
        result = self.process(*args, capture_output=True, input=input, env=env, timeout=timeout)
        return stdout_of(args, result)

    def run(
        self,
        *args: Arg,
        input: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
        strip: bool = True,
    ) -> str:
        """Run git and return its stdout as text (see decode), stripped of space at both ends
        unless `strip` is False: then as git wrote it, for output whose edges matter (e.g.
        `status --porcelain`'s " M file", or a name that starts or ends with a space).
        Raises GitError if it fails, as `run_bytes` does.

        Input and output pass as bytes, so line endings stay as they are: text mode would
        write each \\n as \\r\\n on Windows.
        """
        encoded = None if input is None else encode(input)
        output = decode(self.run_bytes(*args, input=encoded, env=env, timeout=timeout))
        return output.strip() if strip else output

    def ok(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bool:
        """Run a git command whose exit status is a yes-or-no answer, discarding its output:
        True for 0, False for 1, which is git's no (e.g. `merge-base --is-ancestor`, `diff
        --quiet`, `merge-tree`'s conflicts, `config --get` of a key that isn't set). Raises
        GitError for any other exit status, as `run` does (e.g. UnknownRevisionError for a
        revision that names nothing), and GitTimeoutError if it runs past its timeout. Where
        any failure means no, use `succeeds`."""
        result = self.process(
            *args, capture_output=True, stdout=subprocess.DEVNULL, env=env, timeout=timeout
        )
        if result.returncode not in (0, 1):
            raise error_from(args, result)
        return result.returncode == 0

    def succeeds(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bool:
        """Run git, discarding its output, and say whether it succeeded: True for exit status
        0, False for any other (e.g. a revision that names nothing, or no repository). For a
        check where any failure of git's means no; where 1 is git's no and anything else a
        failure to report, use `ok`. It still raises when git gives no answer: GitTimeoutError
        past its timeout, NotStartedError once `stopping` says so, and an OSError if git can't
        be started."""
        result = self.process(
            *args, capture_output=True, stdout=subprocess.DEVNULL, env=env, timeout=timeout
        )
        return result.returncode == 0

    def passthrough(
        self,
        *args: Arg,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bool:
        """Run git with its input and output ours (stdout to `stdout`, if given, e.g. stderr
        when ours is for JSON), and say whether it succeeded. Raises GitTimeoutError, rather
        than say no, if it runs past its timeout."""
        return self.process(*args, stdout=stdout, env=env, timeout=timeout).returncode == 0

    def write_configs(self, *changes: ConfigChange, wait: float = 5.0) -> list[ConfigResult]:
        """Make each change (a ConfigSet or ConfigUnset) to the config file `git config`
        writes, in order, with one `git config` each, and say what each did. An unset that
        removes nothing doesn't fail: it's NOT_FOUND, or SEVERAL_VALUES for a key with several
        values unset without the one to remove (git config's exit status 5). Anything else git
        rejects (e.g. a set of a key with several values, or a key that isn't valid) raises
        GitError, the changes before it made: they're made one at a time, not all or nothing.
        Called with no changes, it starts no process. Raises TypeError, making no change, if
        an argument isn't a ConfigSet or ConfigUnset. Other config writes (e.g. `--add`,
        `--rename-section`, `--worktree`) go through `run("config", ...)`, which doesn't try
        again while locked.

        While another process has the config file locked, each change is tried again,
        starting no try after `wait` seconds, then raises ConfigLockedError (a write is made
        whole or not at all, so trying again is safe). Each try can take as long as git's own
        wait for the lock and the client's timeout allow: git 2.56 and newer wait
        core.configLockTimeout (a second by default; -1: for as long as it's locked) on each
        try, so a change can run that much past `wait`. Older git fails at once, so without
        this, any write racing another process's (e.g. an editor's git) would fail."""
        for change in changes:
            # Before any is made: git config's own arguments come here too.
            wrong = not isinstance(change, (ConfigSet, ConfigUnset))  # pyright: ignore[reportUnnecessaryIsInstance]
            if wrong:
                raise TypeError(
                    f"write_configs takes a ConfigSet or ConfigUnset for each change, not"
                    f' {change!r}: write_configs(ConfigSet("k", "v")) sets a key,'
                    ' and git config\'s other writes (e.g. --add) go through run("config", ...)'
                )
        return [self._write_config_change(change, wait) for change in changes]

    def _config_write(self, *args: Arg, wait: float) -> None:
        """Run `git config <args>`, a write, trying it again while the file is locked (see
        write_configs)."""
        deadline = time.monotonic() + wait
        delay = 0.01
        while True:
            try:
                self.run("config", *args)
                return
            except ConfigLockedError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)

    def _write_config_change(self, change: ConfigChange, wait: float) -> ConfigResult:
        if isinstance(change, ConfigSet):
            self._config_write(change.key, change.value, wait=wait)
            return ConfigResult.CHANGED
        if change.value is None:
            args = ["--unset", change.key]
        else:
            args = ["--unset", "--fixed-value", change.key, change.value]
        try:
            self._config_write(*args, wait=wait)
        except GitError as error:
            if error.returncode != _NOTHING_SET:
                raise
            if _SEVERAL_VALUES.search(error.stderr):
                return ConfigResult.SEVERAL_VALUES
            return ConfigResult.NOT_FOUND
        return ConfigResult.CHANGED
