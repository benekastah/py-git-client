"""The errors git's commands raise: GitError, and a subclass for each failure a tool may
want to tell from the rest, chosen from what git said (in English: see Git.process); and
NoAnswerError, when git said nothing."""

import os
import re
from collections.abc import Sequence
from typing import ClassVar, Self

# How much of a command line a GitError's message shows: a long one (thousands of paths, say)
# would bury what git said.
MAX_COMMAND_IN_MESSAGE = 200


class _CommandError(RuntimeError):
    """What GitError and NoAnswerError share (see GitError): a private base, so that a tool
    catches one or the other, or both by name."""

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


class GitError(_CommandError):
    """A git command failed. `command` is git's arguments, `returncode` its exit status (None
    if it has none, e.g. a tool's own GitError for a command it refused, saying why in
    `stderr`), and `stderr` and `stdout` what it printed there (stdout only from `Git.run` and
    `Git.run_bytes`, e.g. the report of a command that printed one and failed). The message's
    first line is `git <command> failed (<status>):`, the status being the exit status or,
    without one, "no exit status"; `stderr` follows. It shows at most MAX_COMMAND_IN_MESSAGE
    characters of the command line, ending with "…" when cut; `command` keeps all of it. It
    shows bytes that aren't UTF-8 (in a path, say) escaped, as `\xe9`, so it can be written
    anywhere; `command`, `stderr` and `stdout` keep them, as `decode` gives them.

    Not raised when git gives no answer (NoAnswerError), so `except GitError` handles git's
    no without taking a timeout or a tool that's stopping for one."""


class NoAnswerError(_CommandError):
    """git gave no answer: it ran past its timeout (GitTimeoutError), or wasn't started
    (NotStartedError). Not a GitError: catch `(GitError, NoAnswerError)` to handle both. It
    has a GitError's attributes and message, its `returncode` None and its status saying
    which ("timed out", "not started")."""


def _escaped(text: str) -> str:
    r"""text, with the surrogates that stand for bytes that aren't UTF-8 (see `decode`) as
    escapes (`\xe9`), and any other surrogate too (`\ud800`)."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


class GitTimeoutError(NoAnswerError):
    """git ran past its timeout (see Git), and was stopped. Its `stderr` and `stdout` are what
    it printed by then."""

    _without_exit_status: ClassVar[str] = "timed out"


class NotStartedError(NoAnswerError):
    """git wasn't started: the client's `stopping` said the tool is stopping (see Git)."""

    _without_exit_status: ClassVar[str] = "not started"


class NotARepositoryError(GitError):
    """There's no repository at the client's directory (or above it), or no such directory."""


class UnknownRevisionError(GitError):
    """A revision (a commit, a ref, an object id) that names nothing: not one that names an
    object of the wrong type (e.g. a tree where git wants a commit), a WrongTypeError."""


class WrongTypeError(GitError):
    """A name that names an object of the wrong type (e.g. a tree where git wants a commit).
    `expected` is the type git wanted, and `found` the one it found (e.g. "commit" and
    "tree"), as git's message says them; None if it doesn't (e.g. a tool's own error)."""

    def __init__(
        self, args: Sequence[str | bytes], returncode: int | None, stderr: str, stdout: str = ""
    ) -> None:
        super().__init__(args, returncode, stderr, stdout)
        said = WRONG_TYPE.search(stderr)
        self.expected: str | None = None if said is None else said["expected"] or said["expected2"]
        self.found: str | None = None if said is None else said["found"] or said["found2"]


class CorruptObjectError(GitError):
    """An object git found damaged as it read it (e.g. a loose object, or a pack's copy of
    one, that won't inflate): not one that isn't there, an UnknownRevisionError."""


class ConfigLockedError(GitError):
    """Another process has the config file locked, and kept it so for as long as git
    retries (core.configLockTimeout, a second by default, since git 2.56); a write can be
    tried again, as `Git.write_config` does. Only for `git config`'s writes of keys:
    renaming or removing a section (e.g. `git branch -m`) doesn't say why it couldn't lock
    the file, so it's a plain GitError."""


class RemoteError(GitError):
    """A remote that can't be reached or read from, or that has no such repository."""


class PushRejectedError(GitError):
    """The remote rejected a push (e.g. not a fast-forward, or it has commits to fetch
    first), or would have, in a dry run."""


class ConflictError(GitError):
    """A rebase, merge or cherry-pick stopped on a conflict."""


# What git says (on stderr, but for a conflict: on stdout) for each failure, in the order
# they're tried: a push to a remote that can't be reached is a RemoteError, say, and one that
# stops a rebase on a conflict a ConflictError.
_STDOUT_SAYS: tuple[tuple[type[GitError], re.Pattern[str]], ...] = (
    (ConflictError, re.compile(r"^CONFLICT \(", re.MULTILINE)),
)
# What git says of an object it found damaged as it read it: as it exits, or (from cat-file)
# before saying the name is missing.
CORRUPT_OBJECT = re.compile(
    r"^fatal: (loose|packed) object \S+ \(stored in .*\) is corrupt$"
    r"|^error: unable to unpack \S+ header$",
    re.MULTILINE,
)
_STDERR_SAYS: tuple[tuple[type[GitError], re.Pattern[str]], ...] = (
    (PushRejectedError, re.compile(r"^ ! \[(remote )?rejected\]", re.MULTILINE)),
    (
        RemoteError,
        re.compile(
            "Could not read from remote repository"
            "|does not appear to be a git repository"
            # Of a URL (`<scheme>://`): git says the same of a local file it can't read.
            "|unable to access '[a-zA-Z][a-zA-Z0-9+.-]*://"
            "|repository '.*' not found"
        ),
    ),
    # Not for another reason the lock file can't be made (e.g. "No such file or directory").
    (ConfigLockedError, re.compile("could not lock config file .*: File exists$", re.MULTILINE)),
    # Not UnknownRevisionError: what git says of the object, then of the name it can't read.
    (CorruptObjectError, CORRUPT_OBJECT),
    # The second is -C's, for a directory that isn't there.
    (NotARepositoryError, re.compile("not a git repository|cannot change to '")),
    (
        UnknownRevisionError,
        # "ambiguous argument 'x': unknown revision or path not in the working tree", but not
        # "ambiguous argument 'x': both revision and filename", which names something.
        re.compile(
            "unknown revision|bad revision '|Needed a single revision|^fatal: bad object "
            "|(?i:not a valid object name)|invalid reference: |invalid upstream '"
            "| - not something we can merge|Failed to resolve '.*' as a valid ref"
            # Of a full id the repository lacks, as some commands say it.
            "|Not a valid commit name |Invalid revision range |Does not point to a valid commit '"
            "| is not a valid object$|Could not parse object '|^fatal: unknown commit ",
            re.MULTILINE,
        ),
    ),
)


# What git says first of a name that names an object of the wrong type (e.g. a tree where it
# wants a commit), before what it says of a name that names nothing: most commands' words,
# then cat-file's and merge's.
WRONG_TYPE = re.compile(
    r"^error: object \S+ is a (?P<found>\w+), not an? (?P<expected>\w+)$"
    r"|^error: .*: expected (?P<expected2>\w+) type, but the object dereferences to"
    r" (?P<found2>\w+) type$",
    re.MULTILINE,
)


def error_for(stderr: str, stdout: str) -> type[GitError]:
    """The GitError for a command that failed, saying stderr and stdout (in English)."""
    for error, says in _STDOUT_SAYS:
        if says.search(stdout):
            return error
    for error, says in _STDERR_SAYS:
        if says.search(stderr):
            if error is UnknownRevisionError and WRONG_TYPE.search(stderr):
                return WrongTypeError  # it names something
            return error
    return GitError
