"""Running git: finding it once, starting it the quickest way each system allows, and passing
text and paths to it as git expects them."""

import contextlib
import copy
import enum
import functools
import os
import re
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Final, Literal

from git_client import config_file
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
from git_client.reading import READING_COMMANDS, names, reading_form
from git_client.snapshot import ref_changes

# Whether short-lived git processes are started with close_fds. Not on Linux or macOS: Python
# opens its own files non-inheritable (PEP 446), so git inherits only what the tool itself
# inherited, as it would if run from a shell, and without close_fds (and with git's full path,
# and no cwd) Python starts git with posix_spawn, which on macOS is about a third faster than
# forking. (On macOS a new pipe is inheritable for a moment; see Git.popen. A command given a
# process group of its own doesn't get posix_spawn either: see _own_process_group.) On
# Windows, yes: without it, a process one thread starts can inherit, and hold open, the pipes
# another thread is setting up for its own process.
CLOSE_FDS = sys.platform == "win32"

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
    result = git.process(*args, capture_output=True, stdout=subprocess.DEVNULL, timeout=timeout)
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
    return error_for(stderr, stdout)(args, result.returncode, stderr, stdout)


class ReadOnlyError(ValueError):
    """A ReadOnlyGit refused a command, or an environment for one, that could write."""


# Options that make a command in READING_COMMANDS write: a file (--output), the index
# (describe's --dirty and --broken, whatever GIT_OPTIONAL_LOCKS says), or what git or a program
# it runs likes: a textconv's cache, in refs/notes/textconv/<driver>, which a driver's
# cachetextconv asks for (--textconv), or an external diff (--ext-diff: GIT_EXTERNAL_DIFF, or a
# driver's command). Most commands (not log's or diff's options) take an unambiguous
# abbreviation of a long option (e.g. describe's --dirt), so those are refused too.
_WRITING_OPTIONS = (
    "--output",
    "--output-directory",
    "--dirty",
    "--broken",
    "--textconv",
    "--ext-diff",
)

# What git takes as an abbreviation of one of _WRITING_OPTIONS but is an option of its own.
_NOT_WRITING_OPTIONS = frozenset({"--text"})

# Arguments that write through what they have git run: `--submodule=diff` runs a porcelain
# `git diff` in each changed submodule, which runs a textconv (and caches it), an external
# diff (GIT_EXTERNAL_DIFF) and refreshes the submodule's index, none of which it's told not
# to. (Diff's options take no abbreviation; nor does --submodule's value.)
_WRITING_ARGUMENTS = frozenset({"--submodule=diff"})

# The commands that run a diff driver's textconv unless told not to; a ReadOnlyGit tells them.
_TEXTCONV_COMMANDS = frozenset({"log", "show", "blame"})

# What a ReadOnlyGit gives git before any command but config (whose output it would join):
# config that the repository's, or the tool's environment's, can't override (ours comes last),
# so git runs no core.fsmonitor hook, a program that could write anything, and, whatever
# diff.submodule says, no `--submodule=diff` (see _WRITING_ARGUMENTS).
_READ_ONLY_CONFIG = ("-c", "core.fsmonitor=false", "-c", "diff.submodule=short")

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


def _read_only_args(args: Sequence[Arg]) -> list[Arg]:
    """`args` as a ReadOnlyGit runs them: with _READ_ONLY_CONFIG, and for a command that
    would run a textconv, `--no-textconv`. Raises ReadOnlyError unless they're a command a
    ReadOnlyGit runs."""
    start = _refuse_writes(args)
    command = os.fsdecode(args[start])
    if command == "config":
        return list(args)
    no_textconv: list[Arg] = ["--no-textconv"] if command in _TEXTCONV_COMMANDS else []
    return [*_READ_ONLY_CONFIG, *args[: start + 1], *no_textconv, *args[start + 1 :]]


def _refuse_writes(args: Sequence[Arg]) -> int:
    """Raise ReadOnlyError unless `args` are a command a ReadOnlyGit runs; else return where
    the command is in them."""
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
    if command not in READING_COMMANDS and not reading_form(command, rest):
        raise ReadOnlyError(f"ReadOnlyGit runs only commands that read, not git {texts}")
    for arg in rest:
        if arg in _WRITING_ARGUMENTS or (
            arg not in _NOT_WRITING_OPTIONS
            and any(names(arg, option) for option in _WRITING_OPTIONS)
        ):
            raise ReadOnlyError(f"ReadOnlyGit doesn't take {arg}: it writes")
        # status -v shows the index's diff through a textconv, which caches it; even bundled
        # with other short options (-sv), none of which holds a "v".
        short = arg.startswith("-") and not arg.startswith("--")
        if command == "status" and (names(arg, "--verbose") or short and "v" in arg):
            raise ReadOnlyError(f"ReadOnlyGit doesn't take status {arg}: it writes")
    return start


type Location = tuple[str, tuple[tuple[str, EnvValue], ...]]


def location(git: ReadOnlyGit) -> Location:
    """What decides where git finds `git`'s repository: its directory, as an absolute path
    (so a change of the process's directory counts for a relative one), and its `env` (e.g.
    GIT_DIR); not the process's own environment, which a tool sets before it makes clients.
    The key under which a client's answers about where things are are kept, so reassigning
    `directory` or changing `env` asks git again."""
    return os.path.abspath(git.directory), tuple(sorted(git.env.items()))


prefixes: weakref.WeakKeyDictionary[ReadOnlyGit, dict[Location, str]] = weakref.WeakKeyDictionary()
"""Each client's `prefix()`, per location: kept here rather than on the client, so that
`git_paths` can learn it from the `rev-parse` it runs anyway. A client and its `read_only()`
share theirs."""

known_git_paths: weakref.WeakKeyDictionary[ReadOnlyGit, dict[tuple[Location, str], Path]] = (
    weakref.WeakKeyDictionary()
)
"""Where git keeps each name, per client, location and name (see `git_paths`). A client and its
`read_only()` share theirs."""


# Guards each client's object id lengths: held only to read or change them, never while git
# runs.
_lengths_lock = threading.Lock()


def learn_object_id_length(git: ReadOnlyGit, where: Location, object_id: str) -> None:
    """Remember the length of `object_id`, a full id git printed at `where` (the client's
    `location` when it started that git), as the repository's there, so `object_id_length()`
    needn't ask git; and wake any thread waiting for git's answer. Not if it's no full id
    (e.g. a ref's empty object)."""
    if not OBJECT_ID.fullmatch(object_id):
        return
    with _lengths_lock:
        known = git._object_id_lengths.get(where)  # pyright: ignore[reportPrivateUsage]
        if isinstance(known, int):
            return
        git._object_id_lengths[where] = len(object_id)  # pyright: ignore[reportPrivateUsage]
    if known is not None:
        known.set()


def refuse_if_stopping(git: ReadOnlyGit, args: Sequence[Arg]) -> None:
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
    ReadOnlyGit says what the function asks of git, not that the client can't write. A Git's
    `read_only()` gives one that can't.

    It runs `blame`, `cat-file`, `check-attr`, `check-ignore`, `check-ref-format`,
    `count-objects`, `describe`, `diff-files`, `diff-index`, `diff-tree`, `for-each-ref`,
    `log`, `ls-files`, `ls-tree`, `merge-base`, `name-rev`, `patch-id`, `rev-list`,
    `rev-parse`, `shortlog`, `show`, `show-ref`, `status`, `var`, `config` with an action that
    reads (e.g. `--get`, `--list`, `get`) or a lone key, after options that say which files
    it reads and how it gives values (e.g. `--global`, `-f <file>`, `--type=bool`,
    `--default <value>`), `branch --show-current`, `symbolic-ref` naming one ref (`--quiet`,
    `--short`), `remote` (`-v`) and `remote get-url`, and `worktree list`; git's options
    before them only if they name no config, repository or program (e.g. `--no-pager`, not
    `-c`); and none of the options that write: `--output`, describe's `--dirty` and
    `--broken` (the index), `--textconv` and status's `-v` (a textconv's cache),
    `--ext-diff` (which runs a program), and `--submodule=diff` (which runs a `git diff` in
    each submodule, which does all of that). Not `diff`, which writes the index when it compares
    the worktree: `diff-index`, `diff-files` and `diff-tree` don't. Not `range-diff`, whose
    log runs a textconv whatever it's told. Not `merge-tree`, which writes the merge's
    objects; with `--quiet` (git 2.50 and newer), still those of the merge it makes of
    several merge bases. Not `ls-remote`, which runs ssh or credential
    helpers, which may store what they're given. Each argument is checked, wherever it is
    (after `--` too), and config's action must come first. `env` (the client's or a
    command's) may set only the locale (LANG, LANGUAGE, LC_*), TZ, and which index and paths
    git reads (e.g. GIT_INDEX_FILE, GIT_LITERAL_PATHSPECS).

    What it runs, it runs so that what the repository's config, or the tool's environment, tells
    git to run doesn't write: `log`, `show` and `blame` get `--no-textconv` (a textconv whose
    driver has cachetextconv writes its cache, refs/notes/textconv/<driver>), so they show git's
    own diff of a file a textconv would show otherwise; every command but `config` gets `-c
    core.fsmonitor=false -c diff.submodule=short`, which win over the config git inherits too,
    so no core.fsmonitor hook runs, and diff.submodule can't ask for a submodule's `git diff`
    (`log -p` and `show` show a submodule's change as its commits' ids, whatever diff.submodule,
    `log` or `diff`, says); and git gets GIT_OPTIONAL_LOCKS=0, so `status` doesn't write the
    index, and GIT_NO_LAZY_FETCH=1, so in a partial clone a missing object isn't fetched (git
    2.45 and newer, and 2.39.4 to 2.44.1; older git fetches it), and what needs one fails: the
    commands that read a blob's content to diff or search it (`log -p`, `show <commit>`,
    `diff-tree -p`, ...) raise MissingObjectError, but those given a blob's name don't (see the
    README), and `resolve` and CatFile take it as a name that names nothing. What git still runs
    can write: a filter (e.g. git-lfs's smudge, run by `cat-file --filters`, or its clean, which
    `status` runs on a file whose stat information changed), a merge driver
    (merge.<driver>.driver, which `log --remerge-diff` runs, as does `log -m` under
    log.diffMerges=remerge), the program gpg.program or gpg.ssh.program names (`log
    --show-signature`, or log.showSignature), and, through `passthrough`, the pager. The rest of
    the tool's environment reaches git as it is: GIT_DIR (which `Git` follows too), GIT_TRACE*
    (which write where they name), GIT_EXTERNAL_DIFF (which git runs only for `--ext-diff`, and
    in a submodule's `git diff`, both refused), and config from GIT_CONFIG_PARAMETERS or
    GIT_CONFIG_COUNT, which can't get past the above.

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
    for a tool that already knows it, e.g. from the length of an id it has read. It's trusted,
    not checked (that would start the process it saves): a wrong one makes what reads ids by
    it (e.g. `patch_ids`) fail.

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

    Every command is given its directory with `-C` rather than as a working directory, which
    would keep Python from using posix_spawn.
    """

    # Whether it runs only commands that read (see ReadOnlyGit); Git's runs any, but for the
    # one read_only() makes.
    _read_only: bool = True

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
        # Per location: the length, or an Event a thread asking git sets once it has answered.
        self._object_id_lengths: dict[Location, int | threading.Event] = {}

    def read_only(self) -> ReadOnlyGit:
        """This client, made read-only: a copy of it that refuses what a ReadOnlyGit refuses
        (see there), and otherwise runs as the client does, with the same `program`,
        `timeout`, `stopping`, `process_groups` and `env`, as they are now. It's of the
        client's own class, so a subclass's methods (e.g. its own `process`) and attributes
        are kept (an attribute that holds the client itself still writes through it); and it
        shares what git told the client about where things are (`prefix()`,
        `object_id_length()`, `git_paths`), so it asks no question the client asked, nor the
        client one it asked. A new copy each call; a read-only client gives itself. Raises
        ReadOnlyError if `env` holds a variable a ReadOnlyGit doesn't take."""
        if self._read_only:
            return self
        _refuse_environment(self.env)
        reader = copy.copy(self)
        reader.env = dict(self.env)
        reader._read_only = True
        prefixes[reader] = prefixes.setdefault(self, {})
        known_git_paths[reader] = known_git_paths.setdefault(self, {})
        return reader

    def object_id_length(self) -> int:
        """How many hex digits the repository's object ids have: 40 for SHA-1, 64 for
        SHA-256: the client's `object_id_length` if given, else asked of git once per
        `location` (the directory and `env`, which a tool may change) and remembered, or
        learned from a full id the library has read there (`refs`, `resolve`, CatFile).
        Threads that ask at once wait for one git's answer; if it fails, the next asks."""
        if self._object_id_length is not None:
            return self._object_id_length
        key = location(self)
        while True:
            with _lengths_lock:
                known = self._object_id_lengths.get(key)
                if isinstance(known, int):
                    return known
                asking = known is None
                if known is None:
                    known = self._object_id_lengths[key] = threading.Event()
            if not asking:
                known.wait()
                continue  # its answer, or (if it failed) ask again
            length: int | None = None
            try:
                sha256 = self.run("rev-parse", "--show-object-format") == "sha256"
                length = 64 if sha256 else 40
                return length
            finally:
                # Whatever stops it (e.g. KeyboardInterrupt), so no waiter waits for good.
                with _lengths_lock:
                    if length is not None:
                        self._object_id_lengths[key] = length
                    elif self._object_id_lengths.get(key) is known:
                        del self._object_id_lengths[key]
                known.set()

    def prefix(self, *, timeout: Timeout = CLIENT_TIMEOUT) -> str:
        """The path from the top of the worktree down to the client's directory, with a slash
        after each directory (e.g. "src/app/"), as `git rev-parse --show-prefix` gives it: ""
        at the top, and outside a worktree (e.g. in a bare repository or a git directory).
        Put it before a path from the directory to name that path from the top. Raises
        GitError if git fails (e.g. NotARepositoryError).

        Asked of git once per `location` (the directory and `env`, which a tool may change)
        and remembered, so a repository moved or turned into another while the client keeps
        its directory isn't noticed."""
        known = prefixes.setdefault(self, {})
        key = location(self)
        if key not in known:
            # Not run's, which would strip a directory's leading space.
            output = self.run_bytes("rev-parse", "--show-prefix", timeout=timeout)
            known[key] = decode(output.removesuffix(b"\n"))
        return known[key]

    def command(self, *args: Arg) -> list[str] | list[bytes]:
        """git's command line. On Windows, as str, as the OS keeps it. Elsewhere, as bytes:
        text as git wrote it (whatever the locale's encoding, which in the C locale can't hold
        a name that isn't ASCII), and paths in the filesystem's encoding. Raises ReadOnlyError
        for a command a ReadOnlyGit doesn't run."""
        if self._read_only:
            args = tuple(_read_only_args(args))
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
        variable in `env` (or the client's) that it doesn't take. What it builds is kept, for
        the next command given the same, until os.environ changes."""
        added = {**self.env, **(env or {})}
        if self._read_only:
            _refuse_environment(added)
        read_only = self._read_only

        def build() -> dict[str, str]:
            return {
                **os.environ,
                **(_ENGLISH if english else {}),
                **{key: self._environment_value(value) for key, value in added.items()},
                **(_READ_ONLY_ENVIRONMENT if read_only else {}),
            }

        # A copy of the one kept (see _Environments), which costs little: a dict's, not
        # os.environ's, which decodes each variable.
        return dict(_environments.get((english, read_only, tuple(added.items())), build))

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
        to change how each runs, calling `super().process`: once git ends, it counts one that
        may have changed refs for RefSnapshot, whether it succeeded or not, and an override
        that starts git itself leaves RefSnapshot keeping what such a command changed.

        git reads `input`, if given; else, with `capture_output`, nothing (as from
        /dev/null), so a command that reads stdin when given no input (e.g. `shortlog` with no
        revision) neither waits on the tool's stdin nor takes it; without, the tool's stdin, as
        from a shell."""
        command = self.command(*args)
        refuse_if_stopping(self, args)
        seconds = self.timeout if timeout is CLIENT_TIMEOUT else timeout
        own_group = _own_process_group(seconds, self.process_groups)
        try:
            with subprocess.Popen(
                command,
                stdin=_stdin(input, capture_output),
                stdout=subprocess.PIPE if capture_output and stdout is None else stdout,
                stderr=subprocess.PIPE if capture_output else None,
                env=self.environment(env, english=capture_output),
                close_fds=CLOSE_FDS,
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
        finally:
            # Whether git succeeded or not: e.g. a rebase that stops on a conflict has moved HEAD.
            ref_changes.ran(args)

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
        command = self.command(*args)
        refuse_if_stopping(self, args)
        # When it starts: when it ends isn't known here (see RefSnapshot).
        ref_changes.ran(args)
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
        """The id of the object `name` names, as `CatFile.resolve` finds it, or None if it names
        nothing: as `git rev-parse --verify` finds it, but only if the repository has that
        object (not for a full id it lacks, nor for a submodule's commit). Raises
        CorruptObjectError if an object git reads for it is damaged, MissingObjectError if it's
        in a partial clone that couldn't fetch it (from a Git: a ReadOnlyGit's gives None), and
        GitError for another failure (e.g. NotARepositoryError). It takes one process; for many
        names, a CatFile takes one for all of them."""
        line = batch_line(name)
        if line is None:
            return None
        where = location(self)
        args = ("cat-file", "--batch-check")
        result = self.process(*args, capture_output=True, input=_PROBE_LINE + line, timeout=timeout)
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
        learn_object_id_length(self, where, header[0])
        return header[0]

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
        """Run git with its input and output ours (stdout to `stdout`, if given, e.g. stderr
        when ours is for JSON), and say whether it succeeded. Raises GitTimeoutError, rather
        than say no, if it runs past its timeout."""
        return self.process(*args, stdout=stdout, env=env, timeout=timeout).returncode == 0


class Git(ReadOnlyGit):
    """Runs any git command in `directory`, as `git -C <directory>` does: a ReadOnlyGit
    without its limits on commands and their environment, nor the variables it sets for git
    (GIT_OPTIONAL_LOCKS, GIT_NO_LAZY_FETCH). How it runs them, finds git and stops one past
    its timeout is ReadOnlyGit's (see there)."""

    _read_only = False

    def write_config(self, *changes: ConfigChange, wait: float = 5.0) -> list[ConfigResult]:
        """Make each change (a ConfigSet or ConfigUnset) to the config file `git config`
        writes, in order, as `git config` makes it, and say what each did. An unset that
        removes nothing doesn't fail: it's NOT_FOUND, or SEVERAL_VALUES for a key with several
        values unset without the one to remove (git config's exit status 5). Called with no
        changes, it starts no process. Raises TypeError, making no change, if an argument
        isn't a ConfigSet or ConfigUnset. Other config writes (e.g. `--add`,
        `--rename-section`, `--worktree`) go through `run("config", ...)`, which doesn't try
        again while locked. Raises NotStartedError, writing nothing, once `stopping` says so.

        Where every change is of a `branch.<name>.<key>`, it starts no git process but the one
        `git_paths` asks where the file is (none once known): it edits the file itself, as git
        would (see config_file for the two points where it doesn't write git's bytes), all or
        none, under git's lock, which it waits up to `wait` seconds for before raising
        ConfigLockedError as `git config` of the first change would. If writing the file
        fails (e.g. a full disk), it has changed nothing, and the changes are git's to make
        (or to say why not), as below: its errors are git's.

        Otherwise, or where it can't be sure of making a change as git would (see
        config_file.edit), when $GIT_CONFIG names another file, or the file is a symbolic link,
        read-only or can't be read, it runs `git config` for each change, as a FakeGit does:
        anything else git rejects (e.g. a set of a key with several values, or a key that isn't
        valid) then raises GitError, the changes before it made, one at a time, not all or
        nothing. While another process has the file locked, each change is tried again, starting
        no try after `wait` seconds, then raises ConfigLockedError (a write is made whole or not
        at all, so trying again is safe). Each try can take as long as git's own wait for the
        lock and the client's timeout allow: git 2.56 and newer wait core.configLockTimeout (a
        second by default; -1: for as long as it's locked) on each try, so a change can run that
        much past `wait`. Older git fails at once, so without this, any write racing another
        process's (e.g. an editor's git) would fail."""
        for change in changes:
            # Before any is made: a call of the old form, with git config's own arguments,
            # comes here too.
            wrong = not isinstance(change, (ConfigSet, ConfigUnset))  # pyright: ignore[reportUnnecessaryIsInstance]
            if wrong:
                raise TypeError(
                    f"write_config takes a ConfigSet or ConfigUnset for each change, not"
                    f' {change!r}: write_config("k", "v") is write_config(ConfigSet("k", "v")),'
                    ' and git config\'s other writes (e.g. --add) go through run("config", ...)'
                )
        if not changes:
            return []
        refuse_if_stopping(self, ("config",))
        path = self._config_file(changes)
        made = None if path is None else config_file.write(path, changes, wait, self.stopping)
        if made is not None:
            return made
        return [self._write_config_change(change, wait) for change in changes]

    def _config_write(self, *args: Arg, wait: float) -> None:
        """Run `git config <args>`, a write, trying it again while the file is locked (see
        write_config)."""
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

    def _config_file(self, changes: Sequence[ConfigChange]) -> Path | None:
        """The file write_config edits itself for `changes`, or None for git to make them:
        changes `config_file.edit` can't make, or $GIT_CONFIG naming another file for `git
        config` to write."""
        if not config_file.editable(changes) or "GIT_CONFIG" in self.environment():
            return None
        from git_client.batch import git_paths  # here: batch imports this module

        return git_paths(self, "config")[0]

    def _write_config_change(self, change: ConfigChange, wait: float) -> ConfigResult:
        try:
            self._config_write(*config_file.config_args(change), wait=wait)
        except GitError as error:
            # Exit status 5 is a failure of a set (e.g. of a key with several values).
            if isinstance(change, ConfigSet) or error.returncode != _NOTHING_SET:
                raise
            if _SEVERAL_VALUES.search(error.stderr):
                return ConfigResult.SEVERAL_VALUES
            return ConfigResult.NOT_FOUND
        return ConfigResult.CHANGED
