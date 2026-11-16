"""Running git: finding it once, starting it the quickest way each system allows, and passing
text and paths to it as git expects them."""

import enum
import functools
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Final, Literal, TypeAlias

from git_client.errors import GitError, GitTimeoutError, error_for

# Whether short-lived git processes are started with close_fds. Not on Linux or macOS: Python
# opens its own files non-inheritable (PEP 446), so git inherits only what the tool itself
# inherited, as it would if run from a shell, and without close_fds (and with git's full path,
# and no cwd) Python starts git with posix_spawn, which on macOS is about a third faster than
# forking. (On macOS a new pipe is inheritable for a moment; see Git.popen.) On Windows, yes:
# without it, a process one thread starts can inherit, and hold open, the pipes another thread
# is setting up for its own process.
CLOSE_FDS = sys.platform == "win32"

Arg: TypeAlias = str | bytes
"""An argument to git: text (str), or a path in the filesystem (bytes, in its encoding)."""

EnvValue: TypeAlias = str | bytes
"""A value for git's environment: text (str, e.g. an author's name), or a path in the
filesystem (bytes, in its encoding)."""


# git's exit status for a failure it explains ("fatal: ..."), unlike 129 for a usage error.
_FATAL = 128

# What `git var` says when no identity is set for the name or email it's asked for.
_NO_IDENTITY = re.compile(
    "unable to auto-detect email address"
    "|no (email|name) was given and auto-detection is disabled"
    r"|empty ident name \(for <.*>\) not allowed"
)


class ClientTimeout(enum.Enum):
    """CLIENT_TIMEOUT's type."""

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


def check_names_nothing(git: "Git", name: str, *, timeout: Timeout = CLIENT_TIMEOUT) -> None:
    """After `git cat-file` exited on `name`: return if that's only because the name names
    nothing (cat-file exits on some, e.g. `@{u}` on a branch with no upstream), else raise
    the GitError for what made it exit (e.g. NotARepositoryError, or a broken config), as
    `git rev-parse --verify --quiet` finds it."""
    args = ("rev-parse", "--verify", "--quiet", "--end-of-options", name)
    result = git.process(*args, capture_output=True, stdout=subprocess.DEVNULL, timeout=timeout)
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr):
        raise error_from(args, result)


def error_from(args: Sequence[Arg], result: subprocess.CompletedProcess[bytes]) -> GitError:
    """The GitError for a command that failed: a subclass for a failure the library knows from
    what git said (see errors.py)."""
    stderr, stdout = decode(result.stderr), decode(result.stdout or b"")
    return error_for(stderr, stdout)(args, result.returncode, stderr, stdout)


class Git:
    """Runs git in `directory`, as `git -C <directory>` does.

    `program` is the git to run, or else `find_git(env_var)`'s: a project names its own
    variable (e.g. `GIT_STACK_GIT`) so its users can choose. Without `program`, it raises
    FileNotFoundError if there's no git to find (see find_git). `env` is added to the
    environment of every command. `timeout` is how many seconds a command may run (None: as
    long as it takes), unless given its own; one that runs longer is stopped, and raises
    GitTimeoutError.

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
    ) -> None:
        self.directory = os.fspath(directory)
        self.program = program or find_git(env_var)
        self.env: dict[str, EnvValue] = dict(env or {})
        self.timeout = timeout
        self._object_id_length: int | None = None

    def object_id_length(self) -> int:
        """How many hex digits the repository's object ids have: 40 for SHA-1, 64 for
        SHA-256. Read from git the first time it's asked; a repository's can't change."""
        if self._object_id_length is None:
            sha256 = self.run("rev-parse", "--show-object-format") == "sha256"
            self._object_id_length = 64 if sha256 else 40
        return self._object_id_length

    def command(self, *args: Arg) -> list[str] | list[bytes]:
        """git's command line. On Windows, as str, as the OS keeps it. Elsewhere, as bytes:
        text as git wrote it (whatever the locale's encoding, which with LC_ALL=C can't hold a
        name that isn't ASCII), and paths in the filesystem's encoding."""
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
        """The environment for a command: ours, then (if `english`) LC_ALL=C, so git's
        messages are in English for the library to read, then this client's `env`, then
        `env`. Off Windows, git gets each value as bytes, like `command`: text as UTF-8
        (whatever the locale's encoding), and a path given as bytes as it is. Each is given
        here as the str that os.fsdecode makes of those bytes, which Python turns back into
        them as it starts git."""
        added = {**self.env, **(env or {})}
        return {
            **os.environ,
            **({"LC_ALL": "C"} if english else {}),
            **{key: self._environment_value(value) for key, value in added.items()},
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
        subprocess.DEVNULL); git (and any hook it runs) then has LC_ALL=C, so its messages
        are in English whatever the user's language. Otherwise its output is the user's to
        read, in theirs. Every git process but `popen`'s starts here, so a subclass can
        override this to change how each runs (e.g. to make every command fail once the tool
        is shutting down)."""
        try:
            return subprocess.run(
                self.command(*args),
                stdout=subprocess.PIPE if capture_output and stdout is None else stdout,
                stderr=subprocess.PIPE if capture_output else None,
                input=input,
                env=self.environment(env, english=capture_output),
                close_fds=CLOSE_FDS,
                check=False,
                timeout=self.timeout if timeout is CLIENT_TIMEOUT else timeout,
            )
        except subprocess.TimeoutExpired as expired:
            raise GitTimeoutError(
                args, None, decode(expired.stderr or b""), decode(expired.stdout or b"")
            ) from None

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        """Start git with pipes for its stdin and stdout (its stderr goes nowhere), for a
        process that answers requests until its stdin closes (e.g. `cat-file --batch`). It
        has no timeout, and LC_ALL=C (see `process`)."""
        return subprocess.Popen(
            self.command(*args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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
    ) -> str:
        """Run git and return its stdout as text, stripped. Raises GitError if it fails, as
        `run_bytes` does.

        Input and output pass as bytes, so line endings stay as they are: text mode would
        write each \\n as \\r\\n on Windows.
        """
        encoded = None if input is None else encode(input)
        return decode(self.run_bytes(*args, input=encoded, env=env, timeout=timeout)).strip()

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
        revision that names nothing), and GitTimeoutError if it runs past its timeout."""
        result = self.process(
            *args, capture_output=True, stdout=subprocess.DEVNULL, env=env, timeout=timeout
        )
        if result.returncode not in (0, 1):
            raise error_from(args, result)
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
        if result.returncode == _FATAL and _NO_IDENTITY.search(decode(result.stderr)):
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
