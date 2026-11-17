"""One long-lived `git cat-file --batch-command`, for resolving names and reading objects
without a process each."""

import contextlib
import subprocess
import threading
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from git_client.client import (
    FATAL,
    OBJECT_ID,
    ReadOnlyGit,
    batch_line,
    check_names_nothing,
    object_header,
)
from git_client.errors import GitError, UnknownRevisionError

_COMMAND = ("cat-file", "--batch-command")

USAGE_ERROR = 129
"""git's exit status for an option it doesn't know (e.g. `--batch-command`, before git 2.36),
unlike the 128 (FATAL) of `cat-file` exiting on a name."""

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
    git can't read the repository at all (e.g. NotARepositoryError, or a broken config).
    """

    def __init__(self, git: ReadOnlyGit) -> None:
        self.git = git
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

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
            reply = self._request(b"info " + line, name)
        header = None if reply is None else object_header(reply)
        return None if header is None else ObjectInfo(*header)

    def resolve(self, name: str) -> str | None:
        """The id of the object `name` names, or None if it names nothing."""
        found = self.info(name)
        return None if found is None else found.id

    def read(self, object_id: str) -> tuple[str, bytes]:
        """The type and content of the object with this full id (as `git cat-file <type>
        <id>` prints it: a commit's headers and message exactly as they are). Raises
        UnknownRevisionError if there's no such object, and GitError if git stops partway
        through it, or can't read it at all (see CatFile)."""
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError(f"{object_id!r} isn't a full object id")
        with self._lock:
            reply = self._request(b"contents " + object_id.encode() + b"\n", object_id)
            if reply is None:
                raise GitError(_COMMAND, 1, f"cat-file exited at {object_id}")
            header = object_header(reply)
            if header is None:
                raise UnknownRevisionError(_COMMAND, 1, f"{object_id} missing")
            assert self._process is not None and self._process.stdout is not None
            _, kind, size = header
            content = self._process.stdout.read(size)
            newline = self._process.stdout.read(1)
            if len(content) != size or newline != b"\n":
                # It stopped partway through (killed, say): what came is no object's content.
                self._stop()
                raise GitError(
                    _COMMAND, 1, f"cat-file stopped after {len(content)} of its {size} bytes"
                )
        return kind, content

    def _request(self, command: bytes, name: str) -> bytes | None:
        """Send command (a line, about `name`), and return the first line of the reply, or
        None if git exits rather than answer, as it does on some names that name nothing (the
        next request starts another process). A process that stopped between requests (killed,
        say) is started again, for one more try. Raises GitError if a new process exits before
        it has answered anything for another reason: a usage error, as a git older than 2.36
        gives at --batch-command, or what's wrong with the repository (e.g.
        NotARepositoryError). Hold the lock."""
        while True:
            new = self._process is None
            if self._process is None:
                # Its stderr goes nowhere: it warns even for names it resolves (e.g. a name
                # that's both a branch and a tag), as rev-parse --quiet doesn't.
                self._process = self.git.popen(*_COMMAND)
            process = self._process
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
            if reply:
                return reply
            self._stop()
            if process.returncode == USAGE_ERROR:
                raise GitError(_COMMAND, USAGE_ERROR, "git 2.36 or newer is needed")
            if not answered:
                # It exited as it started, as it does when it can't read the repository.
                check_names_nothing(self.git, name)
                return None
            if new or process.returncode == FATAL:
                # It exited on this name, after answering _PROBE or other requests.
                return None
            # It stopped some other way (killed, say), maybe before this request reached it.

    def _stop(self) -> None:
        """Stop the process, if one is running. Hold the lock."""
        if self._process is None:
            return
        if self._process.stdin is not None:
            # It may have stopped already (on Windows, not always with a BrokenPipeError).
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        self._process.wait()
        if self._process.stdout is not None:
            self._process.stdout.close()
        self._process = None
