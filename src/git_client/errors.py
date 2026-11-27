"""The errors git's commands raise: GitError, and a subclass for each failure a tool may
want to tell from the rest, chosen from what git said (in English: see Git.process); and
NoAnswerError, when git said nothing."""

import os
import re
import shlex
from collections.abc import Iterator, Sequence
from typing import ClassVar, Self, cast

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
    object of the wrong type (e.g. a tree where git wants a commit), a WrongTypeError.
    `reason` is `stderr`, but from CatFile, which reads git's reason apart: there, it's what
    git said but its warnings and hints (e.g. "fatal: no upstream configured for branch
    'main'"), or "" when git gave no reason beyond the name naming nothing (as for most names,
    whose `stderr` then says so in rev-parse's words). From other commands, a reason of "" is
    never given: their `stderr` may say no more than that (e.g. "fatal: Needed a single
    revision")."""

    def __init__(
        self,
        args: Sequence[str | bytes],
        returncode: int | None,
        stderr: str,
        stdout: str = "",
        *,
        reason: str | None = None,
    ) -> None:
        super().__init__(args, returncode, stderr, stdout)
        self.reason = stderr if reason is None else reason


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


class MissingObjectError(GitError):
    """An object a partial clone lacks, which git couldn't fetch from its promisor remote: it
    couldn't be reached, or lacks it (its `stderr` says why), so trying again fails the same
    way. (A ReadOnlyGit fetches none with git 2.45 and newer, and 2.39.4 to 2.44.1, which take its
    GIT_NO_LAZY_FETCH=1: its `resolve` and CatFile then take a missing object as a name that names
    nothing. Older git fetches it.) It's raised where a failed fetch raised RemoteError before, so a
    tool that catches RemoteError around a partial clone's commands should catch this too. Only from
    git 2.39, which says which object it couldn't fetch: older git says only why the
    fetch failed, so its failure raises what that says (e.g. RemoteError)."""


class ConfigLockedError(GitError):
    """Another process has the config file locked, and kept it so for as long as git
    retries (core.configLockTimeout, a second by default, since git 2.56); a write can be
    tried again, as `Git.write_configs` does. Only for `git config`'s writes of keys:
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
# What git says as it exits for an object a partial clone lacks and couldn't fetch.
MISSING_OBJECT = re.compile(r"^fatal: could not fetch \S+ from promisor remote$", re.MULTILINE)
# A config path can span lines. Only the reason at the end of the diagnostic counts.
_CONFIG_LOCK_BUSY = re.compile(
    r"^error: could not lock config file (?s:.+): File exists(?:\r?\n)?\Z", re.MULTILINE
)
_TRACE_TIME = r"\d\d:\d\d:\d\d\.\d+[ \t]+"
_TRACE_LOCATION = rf"{_TRACE_TIME}\S+:\d+[ \t]+"


def _trace2_records(event: str) -> str:
    """Match one or more lines for a Trace2 event in JSON, normal or perf format."""
    return (
        rf'(?:(?:\{{"event":"{event}","sid":[^\r\n]+\}}'
        rf"|(?:{_TRACE_LOCATION})?{event} elapsed:[\d.]+ code:-?\d+"
        rf"|(?:{_TRACE_LOCATION}\| )?d\d+ \| main +\| {event} +\|[^\r\n]*\| code:-?\d+)\r?\n)+"
    )


_PERFORMANCE_SUMMARY = re.compile(
    rf"^(?:{_TRACE_TIME}(?:\S*[/\\])?trace\.c:\d+[ \t]+)?performance: [\d.]+ s: git command: ",
    re.MULTILINE,
)
_TRACE2_ATEXIT = _trace2_records("atexit")
_TRACE2_ATEXIT_TRAILER = re.compile(rf"^{_TRACE2_ATEXIT}\Z", re.MULTILINE)
# Git 2.36 logs fsync counters just before exit. Git for Windows logs memory usage there.
_TRACE2_FSYNC_KEY = r"fsync/(?:writeout-only|hardware-flush)"
_TRACE2_MEMORY_VALUE = r"\{[^\r\n]*\}"
_TRACE2_EXIT_DATA = (
    r'(?:(?:\{"event":"data","sid":[^\r\n]+,"category":"fsync",'
    rf'"key":"{_TRACE2_FSYNC_KEY}","value":"\d+"\}}'
    r'|\{"event":"data_json","sid":[^\r\n]+,"category":"process","key":"windows/memory",'
    rf'"value":{_TRACE2_MEMORY_VALUE}\}}'
    rf"|(?:{_TRACE_LOCATION}\| )?d\d+ \| main +\| (?:"
    rf"data +\|[^\r\n]*\| fsync +\| {_TRACE2_FSYNC_KEY}:\d+"
    rf"|data_json +\|[^\r\n]*\| process +\| windows/memory:{_TRACE2_MEMORY_VALUE}))\r?\n)*"
)
_TRACE2_TRAILER = re.compile(
    rf"^{_TRACE2_EXIT_DATA}{_trace2_records('exit')}{_TRACE2_ATEXIT}\Z", re.MULTILINE
)
_TRACE2_CHILD_EXIT_TRAILER = re.compile(
    r'^(?:(?:\{"event":"child_exit","sid":[^\r\n]+\}'
    rf"|(?:{_TRACE_LOCATION})?child_exit\[\d+\] pid:-?\d+ code:-?\d+ elapsed:[\d.]+"
    rf"|(?:{_TRACE_LOCATION}\| )?d\d+ \| main +\| child_exit +\|[^\r\n]*\| "
    r"\[ch\d+\] pid:-?\d+ code:-?\d+)\r?\n)+\Z",
    re.MULTILINE,
)


def _performance_summaries(stderr: str) -> Iterator[tuple[list[str], str]]:
    """Yield each candidate command and the stderr around its summary."""
    # The performance summary can appear between Trace2's exit and atexit records.
    atexit = _TRACE2_ATEXIT_TRAILER.search(stderr)
    end = atexit.start() if atexit else len(stderr)
    for summary in _PERFORMANCE_SUMMARY.finditer(stderr, 0, end):
        try:
            reported = shlex.split(stderr[summary.end() : end])
        except ValueError:  # a path can look like the beginning of a summary
            continue
        if reported:
            yield reported, stderr[: summary.start()] + stderr[end:]


def _config_alias_diagnostic(stderr: str, argv: Sequence[object]) -> str:
    """Accept a lock diagnostic followed by the trace for an alias's Git config child."""
    child_stderr = _TRACE2_CHILD_EXIT_TRAILER.sub("", stderr)
    for reported, remaining in _performance_summaries(child_stderr):
        config_args = reported[2:]
        if (
            reported[1:2] == ["config"]
            and os.path.basename(reported[0]).lower() in ("git", "git.exe")
            and config_args
            and argv[-len(config_args) :] == config_args
        ):
            diagnostic = _TRACE2_TRAILER.sub("", remaining)
            if _CONFIG_LOCK_BUSY.search(diagnostic):
                return diagnostic
    return stderr


def _config_diagnostic(stderr: str, command: object) -> str:
    """Get the config lock diagnostic before Git's exit traces."""
    if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
        args = cast("Sequence[object]", command)
        # Skip argv[0] because a Git launcher can replace the executable path.
        argv = [
            arg.decode("utf-8", "surrogateescape") if isinstance(arg, bytes) else arg
            for arg in args[1:]
        ]
        for reported, remaining in _performance_summaries(stderr):
            if reported[1:] == argv:
                diagnostic = _TRACE2_TRAILER.sub("", remaining)
                if _CONFIG_LOCK_BUSY.search(diagnostic):
                    return diagnostic
                return _config_alias_diagnostic(diagnostic, argv)
    return _TRACE2_TRAILER.sub("", stderr)


_STDERR_SAYS: tuple[tuple[type[GitError], re.Pattern[str]], ...] = (
    (PushRejectedError, re.compile(r"^ ! \[(remote )?rejected\]", re.MULTILINE)),
    # Before RemoteError: what's missing is what the tool can act on (see MissingObjectError).
    (MissingObjectError, MISSING_OBJECT),
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
            "| is not a valid object$|Could not parse object '|^fatal: unknown commit "
            # merge-base --fork-point's, of a name that names no ref.
            "|^fatal: No such ref: '",
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


def error_for(stderr: str, stdout: str, *, command: object) -> type[GitError]:
    """The GitError for a command that failed, saying stderr and stdout (in English)."""
    for error, says in _STDOUT_SAYS:
        if says.search(stdout):
            return error
    if "error: could not lock config file " in stderr:
        diagnostic = _config_diagnostic(stderr, command)
        if _CONFIG_LOCK_BUSY.search(diagnostic):
            return ConfigLockedError
    for error, says in _STDERR_SAYS:
        if says.search(stderr):
            if error is UnknownRevisionError and WRONG_TYPE.search(stderr):
                return WrongTypeError  # it names something
            return error
    return GitError
