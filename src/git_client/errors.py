"""The errors git's commands raise: GitError, and a subclass for each failure a tool may
want to tell from the rest, chosen from what git said (see error_for)."""

import os
import re
from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

# How much of a command line a GitError's message shows: a long one (thousands of paths, say)
# would bury what git said.
MAX_COMMAND_IN_MESSAGE = 200


class GitError(RuntimeError):
    """A git command failed. `command` is git's arguments, `returncode` its exit status (None
    if it has none, e.g. a GitTimeoutError), and `stderr` and `stdout` what it printed there
    (stdout only from `Git.run` and `Git.run_bytes`, e.g. the report of a command that printed
    one and failed). The message's first line is `git <command> failed (<status>):`, the
    status being the exit status or, without one, "timed out" for a GitTimeoutError and "no
    exit status" otherwise (e.g. for a command a tool didn't run, saying why in `stderr`);
    `stderr` follows. It shows at most MAX_COMMAND_IN_MESSAGE characters of the
    command line, ending with "…" when cut; `command` keeps all of it. It shows bytes that
    aren't UTF-8 (in a path, say) escaped, as `\xe9`, so it can be written anywhere;
    `command`, `stderr` and `stdout` keep them, as `decode` gives them."""

    # What the message says in place of the exit status, when there's none.
    _without_exit_status: ClassVar[str] = "no exit status"

    def __init__(
        self, args: Sequence[str | bytes], returncode: int | None, stderr: str, stdout: str = ""
    ) -> None:
        self.command = [os.fsdecode(arg) for arg in args]
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        line = " ".join(self.command)
        if len(line) > MAX_COMMAND_IN_MESSAGE:
            line = line[: MAX_COMMAND_IN_MESSAGE - 1] + "…"
        status = self._without_exit_status if returncode is None else returncode
        super().__init__(_escaped(f"git {line} failed ({status}):\n{stderr.strip()}"))

    def __reduce__(
        self,
    ) -> tuple[type[Self], tuple[list[str], int | None, str, str], dict[str, object]]:
        # So it can cross from a worker process: the default would call __init__ with only the
        # message. Its class and __dict__ keep a subclass, and notes added with add_note.
        return (
            type(self),
            (self.command, self.returncode, self.stderr, self.stdout),
            self.__dict__,
        )


def _escaped(text: str) -> str:
    r"""text, with the surrogates that stand for bytes that aren't UTF-8 (see `decode`) as
    escapes (`\xe9`), and any other surrogate too (`\ud800`)."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


class GitTimeoutError(GitError):
    """git ran past its timeout (see Git), and was stopped. Its returncode is None."""

    _without_exit_status: ClassVar[str] = "timed out"


class NotARepositoryError(GitError):
    """There's no repository at the client's directory (or above it), or no such directory."""


class UnknownRevisionError(GitError):
    """A revision (a commit, a ref, an object id) that names nothing: not one that names an
    object of the wrong type (e.g. a tree where git wants a commit), a plain GitError."""


class ConfigLockedError(GitError):
    """Another process has the config file locked, and kept it so for as long as git
    retries (core.configLockTimeout, a second by default); a write can be tried again. Only
    for `git config`'s writes of keys: renaming or removing a section (e.g. `git branch -m`)
    doesn't say why it couldn't lock the file, so it's a plain GitError."""


class RemoteError(GitError):
    """A remote that can't be reached or read from, or that has no such repository."""


class PushRejectedError(GitError):
    """The remote rejected a push (e.g. not a fast-forward, or it has commits to fetch
    first), or would have, in a dry run."""


class ConflictError(GitError):
    """A rebase, merge or cherry-pick stopped on a conflict."""


# Which GitError each kind of message git says (see messages/en.toml) raises: on stdout, a
# conflict; on stderr, the rest, in the order they're tried (a push to a remote that can't be
# reached is a RemoteError, say).
_STDOUT_SAYS: tuple[tuple[type[GitError], str], ...] = ((ConflictError, "conflict"),)
_STDERR_SAYS: tuple[tuple[type[GitError], str], ...] = (
    (PushRejectedError, "push_rejected"),
    (RemoteError, "remote"),
    (ConfigLockedError, "config_locked"),
    (NotARepositoryError, "not_a_repository"),
    (UnknownRevisionError, "unknown_revision"),
)


def error_for(stderr: str, stdout: str, said: Mapping[str, re.Pattern[str]]) -> type[GitError]:
    """The GitError for a command that failed, saying stderr and stdout: a subclass for what
    git says, as `said` (messages.said, for git's language) reads it, else GitError."""

    def says(kind: str, text: str) -> bool:
        pattern = said.get(kind)
        return pattern is not None and pattern.search(text) is not None

    for error, kind in _STDOUT_SAYS:
        if says(kind, stdout):
            return error
    for error, kind in _STDERR_SAYS:
        if says(kind, stderr):
            if error is UnknownRevisionError and says("wrong_type", stderr):
                return GitError  # it names something
            return error
    return GitError
