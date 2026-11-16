"""One long-lived `git cat-file --batch-command`, for resolving names and reading objects
without a process each."""

import contextlib
import re
import subprocess
import threading
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from git_client.client import Git, GitError, encode

OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A full object id: SHA-1 or SHA-256."""

USAGE_ERROR = 129
"""git's exit status for an option it doesn't know (e.g. `--batch-command`, before git 2.36),
unlike the 128 of `cat-file` exiting on a name."""


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
    resolves to nothing, and the next request starts another process. Any request raises
    GitError if git is too old for `--batch-command` (before 2.36).
    """

    def __init__(self, git: Git) -> None:
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
        if not name or any(c in name for c in "\n\r\0"):
            # A line ending would put the process out of step with what's read back, and git
            # would read a name only up to a NUL.
            return None
        with self._lock:
            header = self._request(b"info " + encode(name))
        if header is None or len(header) != 3:
            # "<name> missing" (or "ambiguous"), which splits in three if name has spaces.
            return None
        object_id, kind, size = (part.decode() for part in header)
        if not OBJECT_ID.fullmatch(object_id) or not size.isdigit():
            return None
        return ObjectInfo(object_id, kind, int(size))

    def resolve(self, name: str) -> str | None:
        """The id of the object `name` names, or None if it names nothing."""
        found = self.info(name)
        return None if found is None else found.id

    def read(self, object_id: str) -> tuple[str, bytes]:
        """The type and content of the object with this full id (as `git cat-file <type>
        <id>` prints it: a commit's headers and message exactly as they are). Raises
        GitError if there's no such object, if git stops partway through it, or if git is
        too old for this (see CatFile)."""
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError(f"{object_id!r} isn't a full object id")
        with self._lock:
            header = self._request(b"contents " + object_id.encode())
            if header is None or len(header) != 3:
                raise GitError(
                    ["cat-file", "contents", object_id], 1, f"{object_id} isn't an object"
                )
            assert self._process is not None and self._process.stdout is not None
            size = int(header[2])
            content = self._process.stdout.read(size)
            newline = self._process.stdout.read(1)
            if len(content) != size or newline != b"\n":
                # It stopped partway through (killed, say): what came is no object's content.
                self._stop()
                raise GitError(
                    ["cat-file", "contents", object_id],
                    1,
                    f"cat-file stopped after {len(content)} of its {size} bytes",
                )
        return header[1].decode(), content

    def _request(self, command: bytes) -> list[bytes] | None:
        """Send command, and return the first line of the reply, split, or None if the process
        stopped (the next request starts another). Raises GitError if it stopped with a usage
        error, as a git older than 2.36 does at --batch-command. Hold the lock."""
        if self._process is None:
            # Its stderr goes nowhere: it warns even for names it resolves (e.g. a name that's
            # both a branch and a tag), as rev-parse --quiet doesn't.
            self._process = self.git.popen("cat-file", "--batch-command")
        assert self._process.stdin is not None and self._process.stdout is not None
        try:
            self._process.stdin.write(command + b"\n")
            self._process.stdin.flush()
            reply = self._process.stdout.readline()
        except OSError:
            reply = b""
        if not reply:
            process = self._process
            self._stop()
            if process.returncode == USAGE_ERROR:
                raise GitError(
                    ["cat-file", "--batch-command"], USAGE_ERROR, "git 2.36 or newer is needed"
                )
            return None
        return reply.split()

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
