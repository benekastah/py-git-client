"""One long-lived `git cat-file --batch-command`, for resolving names and reading objects
without a process each."""

import contextlib
import math
import os
import queue
import select
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Generator
from dataclasses import dataclass
from types import TracebackType
from typing import IO, Literal, Self, get_args

from git_client.client import (
    FATAL,
    OBJECT_ID,
    Location,
    ReadOnlyGit,
    batch_line,
    check_names_nothing,
    decode,
    learn_object_id_length,
    location,
    object_header,
)
from git_client.errors import (
    CORRUPT_OBJECT,
    WRONG_TYPE,
    CorruptObjectError,
    GitError,
    GitTimeoutError,
    UnknownRevisionError,
    WrongTypeError,
)

_COMMAND = ("cat-file", "--batch-command")

USAGE_ERROR = 129
"""git's exit status for an option it doesn't know (e.g. `--batch-command`, before git 2.36),
unlike the 128 (FATAL) of `cat-file` exiting on a name."""

_EXIT_WAIT = 1.0
"""How long a process asked to stop (its stdin and stdout closed) has to exit before it's
killed."""

ObjectType = Literal["blob", "commit", "tag", "tree"]
"""The types of git's objects."""

_TYPES: tuple[ObjectType, ...] = get_args(ObjectType)

_PROBE = b"info " + b"0" * 40 + b"\n"
"""The first request to each new process, sent with the caller's: a name git answers rather
than exits on (no object, or the start of a SHA-256 one's id). A process that answers it got
through reading the repository, so exiting at the next request means it exited on that
request's name, which needs no `rev-parse` to tell it from what's wrong with the repository."""


@dataclass(frozen=True)
class ObjectInfo:
    """What `cat-file` says of an object: its id, type (e.g. "commit") and size in bytes,
    and what git warned of the name that names it, a line each (e.g. "warning: refname
    'main' is ambiguous.", of a branch that's also a tag's name)."""

    id: str
    type: str
    size: int
    warnings: tuple[str, ...] = ()


class CatFile:
    """Resolves names and reads objects through one `git cat-file --batch-command`, started
    when first needed and kept until `close` (or the end of a `with` block).

    It sees refs and objects as they are when asked, even when other processes have changed
    them since it started. Threads can share it. For some names (e.g. `@{u}` on a branch with
    no upstream), git cat-file exits rather than saying they name nothing; then that name
    resolves to nothing, and the next request starts another process. One that stopped
    between requests (killed, say) is started again for the request that finds it gone. A
    request interrupted before all its answer is read (e.g. by KeyboardInterrupt) leaves the
    rest to the process, which the next request kills, and starts another; a request that
    isn't answered within the client's `timeout` (if it has one) kills it at once, raising
    GitTimeoutError. Any
    request raises GitError if git is too old for `--batch-command` (before 2.36), or if
    git can't read the repository at all (e.g. NotARepositoryError, or a broken config), and
    CorruptObjectError if an object git reads for it is damaged (e.g. a corrupt loose object),
    rather than say the name names nothing: git can't tell what it would name. A ref git
    ignores as broken names nothing, as `git rev-parse --verify` finds it.

    Its process stops when it's no longer used, as Python frees it (or, at exit, as its stdin
    closes). With `weak`, it holds
    `git` by a weak reference, for a CatFile the client keeps itself (e.g. `self.cat_file =
    CatFile(self, weak=True)` in a Git subclass): a strong one would make a reference cycle,
    which keeps a client no longer used, and its process, until the garbage collector finds
    it. A request after `git` is freed raises ReferenceError.
    """

    def __init__(self, git: ReadOnlyGit, *, weak: bool = False) -> None:
        self._git: Callable[[], ReadOnlyGit | None] = weakref.ref(git) if weak else lambda: git
        self._process: subprocess.Popen[bytes] | None = None
        self._output: _Output | None = None
        # Where its stderr goes (see _request).
        self._stderr: IO[bytes] | None = None
        # Stops the process when this is freed, or as it's asked to (see _stop).
        self._stopper: (
            weakref.finalize[[subprocess.Popen[bytes], _Output, IO[bytes]], CatFile] | None
        ) = None
        self._lock = threading.Lock()
        # The client's `location` when the process started, where its ids are from, until its
        # first id has told the client its object id length there.
        self._where: Location | None = None
        # Whether a request was sent whose answer hasn't all been read: interrupted (e.g. by
        # KeyboardInterrupt), its answer would answer the next request, which kills the
        # process first.
        self._unanswered = False

    @property
    def git(self) -> ReadOnlyGit:
        """The client it runs git with. Raises ReferenceError if it held it weakly, and it was
        freed."""
        git = self._git()
        if git is None:
            raise ReferenceError("the Git this CatFile runs git with was freed")
        return git

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop the process, if one is running: it's killed if it doesn't exit within a second
        (e.g. one waiting on a lazy fetch in a partial clone)."""
        with self._lock:
            self._stop()

    def info(self, name: str) -> ObjectInfo | None:
        """What `name` (e.g. "HEAD", "main^{tree}", an object id) names, as `git rev-parse
        --verify` would find it, with what git warned of it, or None if it names nothing (or
        is ambiguous)."""
        line = batch_line(name)
        if line is None:
            return None
        with self._lock:
            reply, said = self._request(b"info " + line, name, self._deadline())
            self._unanswered = False
        header = object_header(reply)
        return None if header is None else ObjectInfo(*header, tuple(said.splitlines()))

    def resolve(self, name: str) -> str | None:
        """The id of the object `name` names, or None if it names nothing."""
        found = self.info(name)
        return None if found is None else found.id

    def verify(self, name: str) -> str:
        """The id of the object `name` names, as `resolve` finds it. If it names nothing,
        raises UnknownRevisionError, whose `reason` is git's (e.g. "fatal: no upstream
        configured for branch 'main'"), or "" if it gave none (cat-file says only "<name>
        missing" of most names): its `stderr` is then rev-parse's "fatal: bad revision
        '<name>'". If it names an object of the wrong type (e.g. `<tree>^{commit}`), it raises
        WrongTypeError, saying so. It takes no process of its own to find the reason:
        cat-file's own messages give it."""
        line = batch_line(name)
        if line is None:
            raise _failure(name, "", exited=False)
        with self._lock:
            reply, reason = self._request(b"info " + line, name, self._deadline())
            self._unanswered = False
        header = object_header(reply)
        if header is None:
            raise _failure(name, reason, exited=not reply)
        return header[0]

    def read(self, object_id: str, type: ObjectType | None = None) -> tuple[str, bytes]:
        """The type and content of the object with this full id, as `git cat-file <type>
        <id>` prints it (a commit's headers and message exactly as they are). Given a `type`
        ("blob", "commit", "tag" or "tree"), the object must be of that type, or peel to one
        as `<id>^{<type>}` does (e.g. a commit to its tree): else it raises WrongTypeError.
        Raises UnknownRevisionError if there's no such object, CorruptObjectError if it's
        damaged, and GitError if git stops partway through it, or can't read it at all (see
        CatFile)."""
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError(f"{object_id!r} isn't a full object id")
        if type is not None and type not in _TYPES:
            raise ValueError(f"{type!r} isn't a type of object")
        name = object_id if type is None else f"{object_id}^{{{type}}}"
        with self._lock:
            deadline = self._deadline()
            reply, reason = self._request(b"contents " + name.encode() + b"\n", name, deadline)
            header = object_header(reply)
            if header is None:
                self._unanswered = False
                if not reply:
                    raise GitError(_COMMAND, 1, reason or f"cat-file exited at {name}")
                raise _failure(name, reason, exited=False)
            output = self._output
            assert output is not None
            _, kind, size = header
            with self._exchange(name):
                content = output.read(size, deadline)
                newline = output.read(1, deadline)
            self._unanswered = False
            if len(content) != size or newline != b"\n":
                # It stopped partway through (killed, or on a corrupt object in a pack): what
                # came is no object's content.
                assert self._stderr is not None
                reason = _said(self._stderr)
                self._stop()
                _check_intact(reason, FATAL)
                raise GitError(
                    _COMMAND, 1, f"cat-file stopped after {len(content)} of its {size} bytes"
                )
        return kind, content

    def _request(self, command: bytes, name: str, deadline: float | None) -> tuple[bytes, str]:
        """Send command (a line, about `name`), and return the first line of the reply (empty
        if git exits rather than answer, as it does on some names that name nothing: the next
        request starts another process), and what git said of the request on stderr: its
        reason, if that's no object's header, else its warnings. A process that stopped between requests (killed, say) is
        started again, for one more try. Raises CorruptObjectError if git found an object it
        read for the request damaged, and GitError if a new process exits before it has
        answered anything for another reason: a usage error, as a git older than 2.36 gives at
        --batch-command, or what's wrong with the repository (e.g. NotARepositoryError). Hold
        the lock."""
        if self._unanswered:
            self._kill()
        while True:
            new = self._process is None
            if self._process is None:
                # Its stderr goes to a file, for what it says of each request: why a name
                # names nothing, or its warnings of one it resolves (e.g. a name that's both a
                # branch and a tag). _end closes it, with the process; or here, if the process
                # can't start.
                file = tempfile.TemporaryFile(buffering=0)  # noqa: SIM115
                try:
                    self._where = location(self.git)
                    self._process = self.git.popen(*_COMMAND, stderr=file)
                except BaseException:
                    file.close()
                    raise
                assert self._process.stdout is not None
                self._output = _Output(self._process.stdout)
                self._stderr = file
                self._stopper = weakref.finalize(self, _end, self._process, self._output, file)
                # Not as Python exits, when a daemon thread may be in a request: the process
                # gets EOF on its stdin then, and exits by itself.
                self._stopper.atexit = False
            elif self._stderr is not None and os.fstat(self._stderr.fileno()).st_size:
                # What it said of earlier requests.
                self._stderr.truncate(0)
                self._stderr.seek(0)
            process, output, stderr = self._process, self._output, self._stderr
            assert output is not None and stderr is not None
            assert process.stdin is not None
            # Whether it has answered a request: _PROBE, for a new process.
            answered = not new
            reply = b""
            self._unanswered = True
            with self._exchange(name), contextlib.suppress(OSError):
                process.stdin.write(_PROBE + command if new else command)
                process.stdin.flush()
                if new:
                    answered = bool(output.readline(deadline))
                if answered:
                    reply = output.readline(deadline)
            if (header := object_header(reply)) is not None:
                if self._where is not None and (git := self._git()) is not None:
                    learn_object_id_length(git, self._where, header[0])
                    self._where = None
                # It writes its warnings before the reply.
                return reply, _said(stderr) if os.fstat(stderr.fileno()).st_size else ""
            if reply:
                reason = _said(stderr)
                _check_intact(reason, 1)
                return reply, reason
            # It has closed its stdout, exiting: all it said is in the file.
            reason = _said(stderr)
            self._stop()
            if process.returncode == USAGE_ERROR:
                raise GitError(_COMMAND, USAGE_ERROR, "git 2.36 or newer is needed")
            if not answered:
                # It exited as it started, as it does when it can't read the repository.
                check_names_nothing(self.git, name)
                return reply, reason
            if new or process.returncode == FATAL:
                # It exited on this name, after answering _PROBE or other requests.
                _check_intact(reason, FATAL)
                return reply, reason
            # It stopped some other way (killed, say), maybe before this request reached it.

    def _deadline(self) -> float | None:
        """When a request starting now must be answered by (time.monotonic()), as the
        client's timeout says: None if it has none."""
        seconds = self.git.timeout
        return None if seconds is None else time.monotonic() + seconds

    @contextlib.contextmanager
    def _exchange(self, name: str) -> Generator[None]:
        """Around writing a request about `name` to the process and reading its answer: if
        the answer doesn't come by the request's deadline, kill the process (see _kill) and
        raise GitTimeoutError. Hold the lock."""
        try:
            yield
        except _PastDeadline:
            self._kill()
            raise GitTimeoutError(
                _COMMAND, None, f"{name}: no answer within {self.git.timeout} seconds"
            ) from None

    def _kill(self) -> None:
        """Kill the process, after a request was sent but not all of its answer read: what's
        left would answer the next request, and the process, writing it, might not exit. The
        next request starts another. Hold the lock."""
        if self._process is not None:
            self._process.kill()
        self._stop()

    def _stop(self) -> None:
        """Stop the process, if one is running. Hold the lock."""
        if self._stopper is not None:
            self._stopper()
        self._process = self._output = self._stderr = self._stopper = None
        self._unanswered = False


def _said(stderr: IO[bytes]) -> str:
    """What cat-file wrote to its stderr file since it was last emptied."""
    stderr.seek(0)
    return decode(stderr.read()).strip()


def _check_intact(reason: str, returncode: int) -> None:
    """Raise CorruptObjectError if what cat-file said of a request, `reason`, is that an object
    it read is damaged: then it can't tell what the name names."""
    if CORRUPT_OBJECT.search(reason):
        raise CorruptObjectError(_COMMAND, returncode, reason)


def _failure(name: str, reason: str, *, exited: bool) -> GitError:
    """The error for `name`, which cat-file found nothing for, saying `reason` on stderr
    (empty if it said nothing, or its stderr didn't reach the file, as from a popen that
    doesn't pass it on), and whether it `exited` rather than answer. Without a reason, it
    says the name names nothing in rev-parse's words."""
    returncode = FATAL if exited else 1
    if WRONG_TYPE.search(reason):
        return WrongTypeError(_COMMAND, returncode, reason)
    if reason:
        return UnknownRevisionError(_COMMAND, returncode, reason)
    said = f"fatal: bad revision '{name}'" + (": git exited without saying why" if exited else "")
    return UnknownRevisionError(_COMMAND, returncode, said, reason="")


def _end(process: subprocess.Popen[bytes], output: _Output, stderr: IO[bytes]) -> None:
    """Stop a CatFile's process, and wait for it to exit, so that it's gone when it's no longer
    used, rather than left for Python to warn of and reap. Closing its stdout stops one
    writing what nothing will read (git exits when a write fails); one that doesn't exit
    within _EXIT_WAIT (e.g. waiting on a lazy fetch in a partial clone) is killed."""
    if process.stdin is not None:
        # It may have stopped already (on Windows, not always with a BrokenPipeError).
        with contextlib.suppress(OSError):
            process.stdin.close()
    output.close()
    try:
        process.wait(_EXIT_WAIT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    stderr.close()


class _PastDeadline(Exception):
    """A request's answer didn't come by its deadline. Not TimeoutError, an OSError, which
    CatFile takes for a process that stopped."""


# How much of a process's output is read at a time, at most: what a pipe holds (on Linux).
_CHUNK = 65536


class _Output:
    """A CatFile's process's stdout, read until a deadline (from time.monotonic(), or None
    for none), past which a read raises _PastDeadline. Off Windows, it waits with poll. On
    Windows, which can't poll a pipe, a thread of its own reads the pipe, and a read waits
    for what it passes on: a read that waits on the pipe itself can't be stopped. Either
    way, a request isn't kept waiting by what holds the pipe open once its process is killed
    (e.g. the `git fetch` of a lazy fetch in a partial clone)."""

    def __init__(self, pipe: IO[bytes]) -> None:
        self._pipe = pipe
        self._fd = pipe.fileno()
        self._buffer = bytearray()
        self._ended = False  # Whether the pipe is at its end.
        # On Windows: what the thread has read, and whether to stop reading.
        self._chunks: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._closed = False
        if sys.platform == "win32":
            threading.Thread(target=self._pass_on, name="CatFile output", daemon=True).start()
        else:
            self._poll = select.poll()
            self._poll.register(self._fd, select.POLLIN)

    def readline(self, deadline: float | None) -> bytes:
        """The next line, with its newline: without one at the end of the output, which may
        be empty."""
        while (end := self._buffer.find(b"\n") + 1) == 0 and self._fill(deadline):
            pass
        return self._take(end or len(self._buffer))

    def read(self, size: int, deadline: float | None) -> bytes:
        """The next `size` bytes, or fewer at the end of the output."""
        while len(self._buffer) < size and self._fill(deadline):
            pass
        return self._take(min(size, len(self._buffer)))

    def close(self) -> None:
        """Stop reading. On Windows, the thread closes the pipe once its read returns: closing
        it under that read would wait for it."""
        if sys.platform == "win32":
            self._closed = True
        else:
            self._pipe.close()

    def _take(self, size: int) -> bytes:
        with memoryview(self._buffer) as view:
            taken = view[:size].tobytes()
        del self._buffer[:size]
        return taken

    def _fill(self, deadline: float | None) -> bool:
        """Add what comes next to the buffer, by the deadline. False at the end."""
        if self._ended:
            return False
        if sys.platform == "win32":
            try:
                chunk = self._chunks.get(timeout=_left(deadline))
            except queue.Empty:
                raise _PastDeadline from None
        else:
            left = _left(deadline)
            if left is not None and not self._poll.poll(math.ceil(left * 1000)):
                raise _PastDeadline
            chunk = os.read(self._fd, _CHUNK)
        self._buffer += chunk
        self._ended = not chunk
        return not self._ended

    def _pass_on(self) -> None:
        """On Windows, in a thread of its own: pass on what the pipe gives, then the empty
        chunk of its end, until it ends or this is closed."""
        try:
            while not self._closed and (chunk := os.read(self._fd, _CHUNK)):
                self._chunks.put(chunk)
        except OSError:
            pass
        finally:
            self._chunks.put(b"")
            self._pipe.close()


def _left(deadline: float | None) -> float | None:
    """How many seconds are left until the deadline (at least 0), or None for none."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())
