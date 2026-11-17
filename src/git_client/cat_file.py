"""One long-lived `git cat-file --batch-command`, for resolving names and reading objects
without a process each."""

import contextlib
import os
import subprocess
import tempfile
import threading
import weakref
from collections.abc import Callable
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
    UnknownRevisionError,
    WrongTypeError,
)

_COMMAND = ("cat-file", "--batch-command")

USAGE_ERROR = 129
"""git's exit status for an option it doesn't know (e.g. `--batch-command`, before git 2.36),
unlike the 128 (FATAL) of `cat-file` exiting on a name."""

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
    """What `cat-file` says of an object: its id, type (e.g. "commit") and size in bytes."""

    id: str
    type: str
    size: int


class CatFile:
    """Resolves names and reads objects through one `git cat-file --batch-command`, started
    when first needed and kept until `close` (or the end of a `with` block).

    It sees refs and objects as they are when asked, even when other processes have changed
    them since it started. Threads can share it. For some names (e.g. `@{u}` on a branch with
    no upstream), git cat-file exits rather than saying they name nothing; then that name
    resolves to nothing, and the next request starts another process. One that stopped
    between requests (killed, say) is started again for the request that finds it gone. Any
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
        # Where its stderr goes (see _request).
        self._stderr: IO[bytes] | None = None
        # Stops the process when this is freed, or as it's asked to (see _stop).
        self._stopper: weakref.finalize[[subprocess.Popen[bytes], IO[bytes]], CatFile] | None = None
        self._lock = threading.Lock()
        # The client's `location` when the process started, where its ids are from, until its
        # first id has told the client its object id length there.
        self._where: Location | None = None

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
        """Stop the process, if one is running."""
        with self._lock:
            self._stop()

    def info(self, name: str) -> ObjectInfo | None:
        """What `name` (e.g. "HEAD", "main^{tree}", an object id) names, as `git rev-parse
        --verify` would find it, or None if it names nothing (or is ambiguous)."""
        line = batch_line(name)
        if line is None:
            return None
        with self._lock:
            reply, _ = self._request(b"info " + line, name)
        header = object_header(reply)
        return None if header is None else ObjectInfo(*header)

    def resolve(self, name: str) -> str | None:
        """The id of the object `name` names, or None if it names nothing."""
        found = self.info(name)
        return None if found is None else found.id

    def verify(self, name: str) -> str:
        """The id of the object `name` names, as `resolve` finds it. If it names nothing,
        raises UnknownRevisionError, whose `stderr` is git's reason (e.g. "fatal: no upstream
        configured for branch 'main'", or only "<name> missing"); if it names an object of
        the wrong type (e.g. `<tree>^{commit}`), WrongTypeError, saying so. It takes no
        process of its own to find the reason: cat-file's own messages give it."""
        line = batch_line(name)
        if line is None:
            raise UnknownRevisionError(_COMMAND, 1, f"{name!r} names nothing")
        with self._lock:
            reply, reason = self._request(b"info " + line, name)
        header = object_header(reply)
        if header is None:
            raise _failure(name, reply, reason)
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
            reply, reason = self._request(b"contents " + name.encode() + b"\n", name)
            header = object_header(reply)
            if header is None:
                if not reply:
                    raise GitError(_COMMAND, 1, reason or f"cat-file exited at {name}")
                raise _failure(name, reply, reason)
            assert self._process is not None and self._process.stdout is not None
            _, kind, size = header
            content = self._process.stdout.read(size)
            newline = self._process.stdout.read(1)
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

    def _request(self, command: bytes, name: str) -> tuple[bytes, str]:
        """Send command (a line, about `name`), and return the first line of the reply (empty
        if git exits rather than answer, as it does on some names that name nothing: the next
        request starts another process), and if that's no object's header, what git said of
        the request on stderr. A process that stopped between requests (killed, say) is
        started again, for one more try. Raises CorruptObjectError if git found an object it
        read for the request damaged, and GitError if a new process exits before it has
        answered anything for another reason: a usage error, as a git older than 2.36 gives at
        --batch-command, or what's wrong with the repository (e.g. NotARepositoryError). Hold
        the lock."""
        while True:
            new = self._process is None
            if self._process is None:
                # Its stderr goes to a file, read only for what it says of a name that names
                # nothing: it warns even of names it resolves (e.g. a name that's both a
                # branch and a tag), as rev-parse --quiet doesn't. _end closes it, with the
                # process; or here, if the process can't start.
                file = tempfile.TemporaryFile(buffering=0)  # noqa: SIM115
                try:
                    self._where = location(self.git)
                    self._process = self.git.popen(*_COMMAND, stderr=file)
                except BaseException:
                    file.close()
                    raise
                self._stderr = file
                self._stopper = weakref.finalize(self, _end, self._process, file)
                # Not as Python exits, when a daemon thread may be in a request: the process
                # gets EOF on its stdin then, and exits by itself.
                self._stopper.atexit = False
            elif self._stderr is not None and os.fstat(self._stderr.fileno()).st_size:
                # What it said of earlier requests.
                self._stderr.truncate(0)
                self._stderr.seek(0)
            process, stderr = self._process, self._stderr
            assert stderr is not None
            assert process.stdin is not None and process.stdout is not None
            # Whether it has answered a request: _PROBE, for a new process.
            answered = not new
            reply = b""
            with contextlib.suppress(OSError):
                process.stdin.write(_PROBE + command if new else command)
                process.stdin.flush()
                if new:
                    answered = bool(process.stdout.readline())
                if answered:
                    reply = process.stdout.readline()
            if (header := object_header(reply)) is not None:
                if self._where is not None and (git := self._git()) is not None:
                    learn_object_id_length(git, self._where, header[0])
                    self._where = None
                return reply, ""
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

    def _stop(self) -> None:
        """Stop the process, if one is running. Hold the lock."""
        if self._stopper is not None:
            self._stopper()
        self._process = self._stderr = self._stopper = None


def _said(stderr: IO[bytes]) -> str:
    """What cat-file wrote to its stderr file since it was last emptied."""
    stderr.seek(0)
    return decode(stderr.read()).strip()


def _check_intact(reason: str, returncode: int) -> None:
    """Raise CorruptObjectError if what cat-file said of a request, `reason`, is that an object
    it read is damaged: then it can't tell what the name names."""
    if CORRUPT_OBJECT.search(reason):
        raise CorruptObjectError(_COMMAND, returncode, reason)


def _failure(name: str, reply: bytes, reason: str) -> GitError:
    """The error for `name`, which cat-file found nothing for: it answered `reply` (empty if
    it exited), saying `reason` on stderr (empty if its stderr didn't reach the file, as from
    a popen that doesn't pass it on)."""
    reason = reason or decode(reply).strip() or f"{name} missing"
    error = WrongTypeError if WRONG_TYPE.search(reason) else UnknownRevisionError
    return error(_COMMAND, FATAL if not reply else 1, reason)


def _end(process: subprocess.Popen[bytes], stderr: IO[bytes]) -> None:
    """Stop a CatFile's process, and wait for it to exit, so that it's gone when it's no longer
    used, rather than left for Python to warn of and reap."""
    if process.stdin is not None:
        # It may have stopped already (on Windows, not always with a BrokenPipeError).
        with contextlib.suppress(OSError):
            process.stdin.close()
    process.wait()
    if process.stdout is not None:
        process.stdout.close()
    stderr.close()
