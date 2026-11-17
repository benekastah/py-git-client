"""Running git: finding it once, starting it the quickest way each system allows, and passing
text and paths to it as git expects them."""

import contextlib
import enum
import functools
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import IO, ClassVar, Final, Literal, TypeAlias

from git_client.errors import (
    ConfigLockedError,
    GitError,
    GitTimeoutError,
    NotStartedError,
    error_for,
)

# Whether short-lived git processes are started with close_fds. Not on Linux or macOS: Python
# opens its own files non-inheritable (PEP 446), so git inherits only what the tool itself
# inherited, as it would if run from a shell, and without close_fds (and with git's full path,
# and no cwd) Python starts git with posix_spawn, which on macOS is about a third faster than
# forking. (On macOS a new pipe is inheritable for a moment; see Git.popen. A command given a
# process group of its own doesn't get posix_spawn either: see _own_process_group.) On
# Windows, yes: without it, a process one thread starts can inherit, and hold open, the pipes
# another thread is setting up for its own process.
CLOSE_FDS = sys.platform == "win32"

Arg: TypeAlias = str | bytes
"""An argument to git: text (str), or a path in the filesystem (bytes, in its encoding)."""

EnvValue: TypeAlias = str | bytes
"""A value for git's environment: text (str, e.g. an author's name), or a path in the
filesystem (bytes, in its encoding)."""


# git's exit status for a failure it explains ("fatal: ..."), unlike 129 for a usage error.
FATAL = 128

# What a git whose messages the library reads gets: as the directory of its translations, a
# path that's no directory, so git (and its shell and Perl scripts) finds none and speaks
# English, with C's dates, while what git runs (hooks, filters, credential helpers, gpg, ssh)
# keeps the user's locale. Only a git that one of those runs inherits it, and speaks English.
# git itself keeps the user's LC_CTYPE (from 2.39), which its regex matching follows (see
# ReadOnlyGit.process).
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

Timeout: TypeAlias = float | None | Literal[ClientTimeout.CLIENT]
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


def check_names_nothing(
    git: "ReadOnlyGit", name: str, *, timeout: Timeout = CLIENT_TIMEOUT
) -> None:
    """After `git cat-file` exited on `name`: return if that's only because the name names
    nothing (cat-file exits on some, e.g. `@{u}` on a branch with no upstream), else raise
    the GitError for what made it exit (e.g. NotARepositoryError, or a broken config), as
    `git rev-parse --verify --quiet` finds it."""
    args = ("rev-parse", "--verify", "--quiet", "--end-of-options", name)
    result = git.process(*args, capture_output=True, stdout=subprocess.DEVNULL, timeout=timeout)
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr):
        raise error_from(args, result)


# How long git, asked to stop, has to clean up (e.g. remove index.lock) before it's killed: as
# long as subprocess gives a child after Ctrl-C.
_GRACE = 0.25

# How long a command that was stopped is given to let go of its output.
_AFTER_STOP = 1.0


def _own_process_group(timeout: float | None) -> bool:
    """Whether git starts in a process group of its own, so that past its timeout, or on an
    interrupt, what it started (hooks, ssh, git-remote-https, credential helpers) is stopped
    with it: on Linux and macOS, for a command with a timeout, when the tool has no
    controlling terminal (as in CI, or a service). With one, git stays in the terminal's
    foreground group, as from a shell: a group of its own would be in the background, where
    reading the terminal (a password prompt, an editor, a pager) stops it until the timeout,
    and Ctrl-C would reach the tool alone. Only with a timeout, as a group keeps Python from
    using posix_spawn."""
    if timeout is None or sys.platform == "win32":
        return False
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError:
        return True
    return False


def _stop(process: subprocess.Popen[bytes], own_group: bool) -> None:
    """Stop git, past its timeout or on an interrupt. On Linux and macOS, ask it to first, with
    SIGINT (to its group, if it has one of its own), as Ctrl-C would, so it removes its lock
    files, and kill it (and its group) if it's still there after _GRACE. On Windows, kill its
    process tree (taskkill /T) and git: Windows has no signal to ask a process with no console
    of its own to stop."""
    if sys.platform == "win32":
        # By its full path, as find_git finds git's: not from the current directory.
        taskkill = os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"
        )
        # If taskkill can't run, git is still killed: what it started runs on.
        with contextlib.suppress(OSError):
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        process.kill()
        return
    # Gone already, or (on macOS) only zombies left in its group.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if own_group:
            os.killpg(process.pid, signal.SIGINT)
        else:
            process.send_signal(signal.SIGINT)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(_GRACE)
    if own_group:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
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


def error_from(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> GitError:
    """The GitError for a command that failed: a subclass for a failure the library knows from
    what git said (see errors.py)."""
    stderr, stdout = decode(result.stderr), decode(result.stdout or b"")
    return error_for(stderr, stdout)(args, result.returncode, stderr, stdout)


class ReadOnlyError(ValueError):
    """A ReadOnlyGit refused a command, or an environment for one, that could write."""


# The commands a ReadOnlyGit runs: none writes, with GIT_OPTIONAL_LOCKS=0 (without it, status
# writes the index) and without _WRITING_OPTIONS. Not porcelain diff, which writes the index
# whatever GIT_OPTIONAL_LOCKS says when it compares the worktree: its plumbing (diff-files,
# diff-index, diff-tree) doesn't.
_READING_COMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "check-ref-format",
        "count-objects",
        "describe",
        "diff-files",
        "diff-index",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "patch-id",
        "range-diff",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "var",
    }
)

# Options that make one of those write: a file (--output), or the index (describe's --dirty and
# --broken, whatever GIT_OPTIONAL_LOCKS says). Most commands (not log's or diff's options) take
# an unambiguous abbreviation of a long option (e.g. describe's --dirt), so those are refused
# too.
_WRITING_OPTIONS = ("--output", "--output-directory", "--dirty", "--broken")

# What git's options before the command may be: none that names another config (-c),
# repository or program.
_READING_GIT_OPTIONS = frozenset(
    {
        "--no-pager",
        "-P",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-replace-objects",
        "--no-optional-locks",
    }
)

# `git config`'s actions that read, and those that write (refused, even with one that reads).
_CONFIG_READS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "--list",
        "-l",
    }
)
_CONFIG_WRITES = (
    "--add",
    "--replace-all",
    "--unset",
    "--unset-all",
    "--rename-section",
    "--remove-section",
    "--edit",
)

# The variables a ReadOnlyGit may be given for git's environment, beyond the locale's (LC_*):
# none that adds config (GIT_CONFIG_*, or HOME for another global config), names a program git
# runs (GIT_EXTERNAL_DIFF, GIT_SSH_COMMAND, ...) or another repository (GIT_DIR, as --git-dir
# isn't taken either: the client's directory says which).
_READING_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LANGUAGE",
        "TZ",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
        "GIT_NO_REPLACE_OBJECTS",
    }
)


# What a ReadOnlyGit's git gets, last: no optional locks (status would write the index), and no
# fetching a missing object in a partial clone (which writes a pack; git before 2.45 ignores
# this, and fetches, but for the security releases 2.39.4 to 2.44.1).
_READ_ONLY_ENVIRONMENT = {"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}


def _names(arg: str, option: str) -> bool:
    """Whether git takes `arg` as `option`: it, or (for a long option) an abbreviation of it,
    with or without a value after "="."""
    name = arg.split("=", 1)[0]
    if name == option:
        return True
    return name.startswith("--") and len(name) > 2 and option.startswith(name)


def _refuse_writes(args: Sequence[Arg]) -> None:
    """Raise ReadOnlyError unless `args` are a command a ReadOnlyGit runs."""
    texts = [os.fsdecode(arg) for arg in args]
    start = 0
    while start < len(texts) and texts[start].startswith("-"):
        if texts[start] not in _READING_GIT_OPTIONS:
            raise ReadOnlyError(f"ReadOnlyGit doesn't take git's option {texts[start]}")
        start += 1
    if start == len(texts):
        raise ReadOnlyError("ReadOnlyGit was given no command")
    # Every argument is checked, even after "--" or "--end-of-options": either can be another
    # option's value (`log --grep --end-of-options --output=x`), and git then reads on.
    command, rest = texts[start], texts[start + 1 :]
    if command == "config":
        # Its action first: anywhere else, it can be another option's value (`config --file
        # --list a.b c` writes a.b to a file named --list).
        reads = rest[:1] in (["get"], ["list"]) or (bool(rest) and rest[0] in _CONFIG_READS)
        writes = any(_names(arg, action) for arg in rest for action in _CONFIG_WRITES)
        # -e (edit), even bundled with other short options (-ze): any short-option argument
        # holding an "e" is refused, which takes in some that don't edit (e.g. -f<path>).
        edits = any(arg[:1] == "-" and arg[1:2] != "-" and "e" in arg for arg in rest)
        if not reads or writes or edits:
            raise ReadOnlyError(f"ReadOnlyGit runs only a config that reads: {texts}")
    elif command == "worktree":
        if rest[:1] != ["list"]:
            raise ReadOnlyError(f"ReadOnlyGit runs only worktree list: {texts}")
    elif command not in _READING_COMMANDS:
        raise ReadOnlyError(f"ReadOnlyGit doesn't run git {command}")
    for arg in rest:
        if any(_names(arg, option) for option in _WRITING_OPTIONS):
            raise ReadOnlyError(f"ReadOnlyGit doesn't take {arg}: it writes")


def refuse_if_stopping(git: "ReadOnlyGit", args: Sequence[Arg]) -> None:
    """Raise NotStartedError if `git`'s `stopping` says the tool is stopping: checked before
    each git process starts (FakeGit's too), but after a ReadOnlyGit refuses what it won't
    run, so that programming error isn't passed off as stopping."""
    if git.stopping is not None and git.stopping():
        raise NotStartedError(args, None, "the tool is stopping")


def _refuse_environment(env: Mapping[str, EnvValue]) -> None:
    """Raise ReadOnlyError unless each of `env`'s variables is one a ReadOnlyGit takes."""
    for key in env:
        if key not in _READING_ENVIRONMENT and not key.startswith("LC_"):
            raise ReadOnlyError(f"ReadOnlyGit doesn't take {key} for git's environment")


class ReadOnlyGit:
    """Runs git in `directory`, as `git -C <directory>` does, but only commands that read:
    before it starts git, it raises ReadOnlyError for anything else, so a tool that only
    reads can show that it does by making one. Git extends it to run any command, so a
    function that only reads takes a ReadOnlyGit, and is given either: a parameter typed
    ReadOnlyGit says what the function asks of git, not that the client can't write.

    It runs `blame`, `cat-file`, `check-attr`, `check-ignore`, `check-ref-format`,
    `count-objects`, `describe`, `diff-files`, `diff-index`, `diff-tree`, `for-each-ref`,
    `log`, `ls-files`, `ls-tree`, `merge-base`, `name-rev`, `patch-id`, `range-diff`,
    `rev-list`, `rev-parse`, `shortlog`, `show`, `show-ref`, `status`, `var`, `config` with
    an action that reads (e.g. `--get`, `--list`, `get`), and `worktree list`; git's options
    before them only if they name no config, repository or program (e.g. `--no-pager`, not
    `-c`); and no `--output`, nor describe's `--dirty` or `--broken`, which write the index.
    Not `diff`, which writes the index when it compares the worktree: `diff-index`,
    `diff-files` and `diff-tree` don't. Each argument is checked, wherever it is (after `--`
    too), and config's action must come first. Git gets GIT_OPTIONAL_LOCKS=0, so `status`
    doesn't write the index either, and GIT_NO_LAZY_FETCH=1, so in a partial clone a missing
    object isn't fetched (git 2.45 and newer, and 2.39.4 to 2.44.1; older git fetches it).
    `env` (the client's or a command's) may set only the locale (LANG, LANGUAGE, LC_*), TZ,
    and which index and paths git reads (e.g. GIT_INDEX_FILE, GIT_LITERAL_PATHSPECS).

    That's what the tool asks for. What the repository's config tells git to run can still
    write (e.g. a diff driver's textconv with cachetextconv, a filter like git-lfs's, or
    core.fsmonitor), as can what the tool's own environment gives git.

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
    that has to start (again) raises.

    Past its timeout, or when the tool is interrupted (KeyboardInterrupt) while it runs, git
    is first asked to stop, so it can remove its lock files (e.g. index.lock), and killed a
    quarter of a second later; the call then waits at most a second more for its output. On
    Linux and macOS it's asked with SIGINT, as Ctrl-C asks it. When the tool has no
    controlling terminal (as in CI, or a service), a command with a timeout runs in a process
    group of its own, so what git started (hooks, ssh, credential helpers) is stopped with
    it. With a terminal, git stays in its foreground, as from a shell, so it can ask for a
    password and open an editor, and Ctrl-C reaches it; only git is stopped then. On
    Windows, git and what it started are killed (taskkill /T), with no chance to clean up.

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

    Every command is given its directory with `-C` rather than as a working directory, which
    would keep Python from using posix_spawn.
    """

    # Whether it runs only commands that read (see ReadOnlyGit); Git's runs any.
    _read_only: ClassVar[bool] = True

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
        self.directory = os.fspath(directory)
        self.program = program or find_git(env_var)
        self.env: dict[str, EnvValue] = dict(env or {})
        self.timeout = timeout
        self.stopping = stopping
        self._object_id_length: int | None = None

    def object_id_length(self) -> int:
        """How many hex digits the repository's object ids have: 40 for SHA-1, 64 for
        SHA-256. Read from git the first time it's asked; a repository's can't change."""
        if self._object_id_length is None:
            sha256 = self.run("rev-parse", "--show-object-format") == "sha256"
            self._object_id_length = 64 if sha256 else 40
        return self._object_id_length

    def prefix(self, *, timeout: Timeout = CLIENT_TIMEOUT) -> str:
        """The path from the top of the worktree down to the client's directory, with a slash
        after each directory (e.g. "src/app/"), as `git rev-parse --show-prefix` gives it: ""
        at the top, and outside a worktree (e.g. in a bare repository or a git directory).
        Put it before a path from the directory to name that path from the top. Raises
        GitError if git fails (e.g. NotARepositoryError)."""
        # Not run's, which would strip a directory's leading space.
        output = self.run_bytes("rev-parse", "--show-prefix", timeout=timeout)
        return decode(output.removesuffix(b"\n"))

    def command(self, *args: Arg) -> list[str] | list[bytes]:
        """git's command line. On Windows, as str, as the OS keeps it. Elsewhere, as bytes:
        text as git wrote it (whatever the locale's encoding, which in the C locale can't hold
        a name that isn't ASCII), and paths in the filesystem's encoding. Raises ReadOnlyError
        for a command a ReadOnlyGit doesn't run."""
        if self._read_only:
            _refuse_writes(args)
        if sys.platform == "win32":
            return [self.program, "-C", self.directory, *map(os.fsdecode, args)]
        return [
            os.fsencode(self.program),
            b"-C",
            os.fsencode(self.directory),
            *(arg if isinstance(arg, bytes) else encode(arg) for arg in args),
        ]

    def environment(
        self, env: Mapping[str, EnvValue] | None = None, *, english: bool = False
    ) -> dict[str, str]:
        """The environment for a command: ours, then (if `english`) _ENGLISH, so git's
        messages are in English for the library to read (what git runs keeps the user's
        locale), then this client's `env`, then `env`. Off Windows, git gets each value as
        bytes, like `command`: text as UTF-8 (whatever the locale's encoding), and a path
        given as bytes as it is. Each is given here as the str that os.fsdecode makes of
        those bytes, which Python turns back into them as it starts git. A ReadOnlyGit's sets
        GIT_OPTIONAL_LOCKS=0 and GIT_NO_LAZY_FETCH=1 last, and raises ReadOnlyError for a
        variable in `env` (or the client's) that it doesn't take."""
        added = {**self.env, **(env or {})}
        if self._read_only:
            _refuse_environment(added)
        return {
            **os.environ,
            **(_ENGLISH if english else {}),
            **{key: self._environment_value(value) for key, value in added.items()},
            **(_READ_ONLY_ENVIRONMENT if self._read_only else {}),
        }

    @staticmethod
    def _environment_value(value: EnvValue) -> str:
        if isinstance(value, bytes):
            return os.fsdecode(value)
        return value if sys.platform == "win32" else os.fsdecode(encode(value))

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
        says so. Every git process but `popen`'s starts here, so a subclass can override this
        to change how each runs."""
        command = self.command(*args)
        refuse_if_stopping(self, args)
        seconds = self.timeout if timeout is CLIENT_TIMEOUT else timeout
        own_group = _own_process_group(seconds)
        with subprocess.Popen(
            command,
            stdin=None if input is None else subprocess.PIPE,
            stdout=subprocess.PIPE if capture_output and stdout is None else stdout,
            stderr=subprocess.PIPE if capture_output else None,
            env=self.environment(env, english=capture_output),
            close_fds=CLOSE_FDS,
            process_group=0 if own_group else None,
        ) as process:
            try:
                output, errors = process.communicate(input, timeout=seconds)
            except subprocess.TimeoutExpired:
                _stop(process, own_group)
                output, errors = _remaining_output(process)
                raise GitTimeoutError(
                    args, None, decode(errors or b""), decode(output or b"")
                ) from None
            except BaseException:  # e.g. KeyboardInterrupt
                _stop(process, own_group)
                _let_go_of_output(process)
                raise
        return subprocess.CompletedProcess(process.args, process.returncode, output, errors)

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
        NotStartedError, starting nothing, once `stopping` says so."""
        command = self.command(*args)
        refuse_if_stopping(self, args)
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=self.environment(env, english=True),
            # On every system, unlike CLOSE_FDS: macOS makes a pipe, then makes it
            # non-inheritable, so a process that lives as long as the tool could hold the pipe
            # another thread is making, and that thread's process would never see EOF. One
            # process a session doesn't need posix_spawn's speed.
            close_fds=True,
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
        return self._stdout(args, result)

    @staticmethod
    def _stdout(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> bytes:
        """What git printed to stdout, if it succeeded. Raises GitError if it failed."""
        if result.returncode != 0:
            raise error_from(args, result)
        return result.stdout

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
        past its timeout, an OSError if git can't be started, and a ReadOnlyGit's
        ReadOnlyError for a command it refuses."""
        result = self.process(
            *args, capture_output=True, stdout=subprocess.DEVNULL, env=env, timeout=timeout
        )
        return result.returncode == 0

    def resolve(self, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> str | None:
        """The id of the object `name` names, as `CatFile.resolve` finds it, or None if it
        names nothing: as `git rev-parse --verify` finds it, but only if the repository has
        that object (not for a full id it lacks, nor for a submodule's commit). Raises
        GitError for another failure (e.g. NotARepositoryError). For many names, a CatFile
        takes one process for all of them."""
        line = batch_line(name)
        if line is None:
            return None
        result = self.process(
            "cat-file", "--batch-check", capture_output=True, input=line, timeout=timeout
        )
        if result.returncode != 0:
            check_names_nothing(self, name, timeout=timeout)
            return None
        header = object_header(result.stdout)
        return None if header is None else header[0]

    def var(self, name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> str | None:
        """git's value for one of its variables (as `git var`, e.g. GIT_AUTHOR_IDENT), or None
        if no identity is set for it. Raises GitError for an unknown variable, or another
        failure (e.g. a broken config)."""
        result = self.process("var", name, capture_output=True, timeout=timeout)
        if result.returncode == FATAL and _NO_IDENTITY.search(decode(result.stderr)):
            return None
        return decode(self._stdout(("var", name), result)).strip()

    def passthrough(
        self,
        *args: Arg,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bool:
        """Run git with its output going to ours (stdout to `stdout`, if given, e.g. stderr
        when ours is for JSON), and say whether it succeeded. Raises GitTimeoutError, rather
        than say no, if it runs past its timeout."""
        return self.process(*args, stdout=stdout, env=env, timeout=timeout).returncode == 0


class Git(ReadOnlyGit):
    """Runs any git command in `directory`, as `git -C <directory>` does: a ReadOnlyGit
    without its limits on commands and their environment, nor the variables it sets for git
    (GIT_OPTIONAL_LOCKS, GIT_NO_LAZY_FETCH). How it runs them, finds git and stops one past
    its timeout is ReadOnlyGit's (see there)."""

    _read_only: ClassVar[bool] = False

    def write_config(self, *args: Arg, wait: float = 5.0) -> str:
        """Run `git config <args>`, a write (e.g. `"branch.topic.base", "main"`, or
        `"--unset", "branch.topic.base"`), and return what it printed. While another process
        has the config file locked, try it again, starting no try after `wait` seconds, then
        raise ConfigLockedError. A write is made whole or not at all, so trying again is safe.

        Each try can take as long as git's own wait for the lock and the client's timeout
        allow: git 2.56 and newer wait core.configLockTimeout (a second by default; -1: for
        as long as it's locked) on each try, so the call can run that much past `wait`.
        Older git fails at once, so without this, any write racing another process's (e.g.
        an editor's git) would fail."""
        deadline = time.monotonic() + wait
        delay = 0.01
        while True:
            try:
                return self.run("config", *args)
            except ConfigLockedError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)
