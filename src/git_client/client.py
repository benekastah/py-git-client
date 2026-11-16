"""Running git: finding it once, starting it the quickest way each system allows, and passing
text and paths to it as git expects them."""

import functools
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Self, TypeAlias

# Whether git's processes are started with close_fds. Not on Linux or macOS: Python opens its
# own files non-inheritable (PEP 446), so there's nothing to close, and without close_fds (and
# with git's full path, and no cwd) Python starts git with posix_spawn, which on macOS is about
# a third faster than forking. On Windows, yes: without it, a process one thread starts can
# inherit, and hold open, the pipes another thread is setting up for its own process.
CLOSE_FDS = sys.platform == "win32"

Arg: TypeAlias = str | bytes
"""An argument to git: text (str), or a path in the filesystem (bytes, in its encoding)."""

EnvValue: TypeAlias = str | bytes
"""A value for git's environment: text (str), or a path in the filesystem (bytes)."""


class GitError(RuntimeError):
    """A git command failed. `command` is git's arguments, `returncode` its exit status and
    `stderr` what it printed there."""

    def __init__(self, args: Sequence[Arg], returncode: int, stderr: str) -> None:
        self.command = [os.fsdecode(arg) for arg in args]
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(self.command)} failed ({returncode}):\n{stderr.strip()}")

    def __reduce__(self) -> tuple[type[Self], tuple[list[str], int, str], dict[str, object]]:
        # So it can cross from a worker process: the default would call __init__ with only the
        # message. Its class and __dict__ keep a subclass, and notes added with add_note.
        return (type(self), (self.command, self.returncode, self.stderr), self.__dict__)


@functools.cache
def find_git(env_var: str | None = None) -> str:
    """The git to run: the program `$env_var` names, if that's set; else the git on PATH (a
    wrapper such as git-ai included), by its full path, which posix_spawn needs. Found once per
    process. Raises FileNotFoundError if there's none: running plain "git" would let the OS
    look in the current directory, where a repository could hold a git of its own."""
    if env_var and (configured := os.environ.get(env_var)):
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


class Git:
    """Runs git in `directory`, as `git -C <directory>` does.

    `program` is the git to run, or else `find_git(env_var)`'s: a project names its own
    variable (e.g. `GIT_STACK_GIT`) so its users can choose. Without `program`, it raises
    FileNotFoundError if there's no git to find. `env` is added to the environment of every
    command.

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
    ) -> None:
        self.directory = os.fspath(directory)
        self.program = program or find_git(env_var)
        self.env: dict[str, EnvValue] = dict(env or {})

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

    def environment(self, env: Mapping[str, EnvValue] | None = None) -> dict[str, str]:
        """The environment for a command: ours, then this client's `env`, then `env`. A path
        given as bytes becomes str with os.fsdecode, which Python turns back into the same
        bytes as it starts git."""
        added = {**self.env, **(env or {})}
        return {**os.environ, **{key: os.fsdecode(value) for key, value in added.items()}}

    def process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run git and return the finished process, whatever its exit status."""
        return subprocess.run(
            self.command(*args),
            capture_output=capture_output,
            stdout=None if capture_output else stdout,
            input=input,
            env=self.environment(env),
            close_fds=CLOSE_FDS,
            check=False,
        )

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        """Start git with pipes for its stdin and stdout (its stderr goes nowhere), for a
        process that answers requests until its stdin closes (e.g. `cat-file --batch`)."""
        return subprocess.Popen(
            self.command(*args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.environment(env),
            close_fds=CLOSE_FDS,
        )

    def run_bytes(
        self, *args: Arg, input: bytes | None = None, env: Mapping[str, EnvValue] | None = None
    ) -> bytes:
        """Run git and return its stdout as it is (e.g. a file's content, or a patch).
        Raises GitError if it fails."""
        result = self.process(*args, capture_output=True, input=input, env=env)
        if result.returncode != 0:
            raise GitError(args, result.returncode, decode(result.stderr))
        return result.stdout

    def run(
        self, *args: Arg, input: str | None = None, env: Mapping[str, EnvValue] | None = None
    ) -> str:
        """Run git and return its stdout as text, stripped. Raises GitError if it fails.

        Input and output pass as bytes, so line endings stay as they are: text mode would
        write each \\n as \\r\\n on Windows.
        """
        return decode(
            self.run_bytes(*args, input=None if input is None else encode(input), env=env)
        ).strip()

    def ok(self, *args: Arg, env: Mapping[str, EnvValue] | None = None) -> bool:
        """Run git, discarding its output, and say whether it succeeded."""
        return self.process(*args, capture_output=True, env=env).returncode == 0

    def passthrough(
        self,
        *args: Arg,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
    ) -> bool:
        """Run git with its output going to ours (stdout to `stdout`, if given, e.g. stderr
        when ours is for JSON), and say whether it succeeded."""
        return self.process(*args, stdout=stdout, env=env).returncode == 0
