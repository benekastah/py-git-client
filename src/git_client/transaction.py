"""`ref_transaction`: one ref transaction, whose changes git can prepare (check, and lock their
refs) before the caller decides to commit or abort it, over one `git update-ref --stdin`."""

import contextlib
import subprocess
import time
from types import TracebackType
from typing import IO, Self

from git_client.cat_file import EXIT_WAIT, Output, PastDeadline
from git_client.client import Git, decode, encode, error_from
from git_client.errors import GitError, GitTimeoutError


class RefTransaction:
    """A ref transaction (see `ref_transaction`): changes (`update`, `delete`, `verify`)
    that git makes all of, at `commit`, or none of. `prepare` has git check them and lock
    their refs, so that nothing else changes those refs until `commit` or `abort` ends the
    transaction. Changes go to git with the next `prepare`, `commit` or `abort`.

    A ref is a full name (`refs/heads/main`), and a value an object id or a revision git
    resolves; git checks both. An `old` value is where the ref must be for the change to be
    made: all zeros for nowhere (it must not exist yet), or None (for `update` and `delete`)
    for anywhere."""

    # Set as the transaction starts (see __enter__).
    _process: subprocess.Popen[bytes]
    _output: Output
    _stderr: IO[bytes]

    def __init__(self, git: Git, args: tuple[str, ...]) -> None:
        self._git, self._args = git, args
        self._changes: list[bytes] = []
        self._closed = False

    def __enter__(self) -> Self:
        """Start git, and the transaction."""
        import tempfile  # here: at the top, it slows every tool's start

        # git's stderr, which nothing reads until it exits, goes to a file: a pipe could fill.
        self._stderr = tempfile.TemporaryFile(buffering=0)
        try:
            self._process = self._git.popen(*self._args, stderr=self._stderr)
        except BaseException:
            self._stderr.close()
            raise
        assert self._process.stdout is not None
        self._output = Output(self._process.stdout)
        try:
            self._send("start")
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """End git, if a failure didn't, which aborts the transaction if it wasn't ended.
        Raises GitError if git failed, unless the block raised."""
        try:
            if self._process.returncode is None and (returncode := self._end()) and type is None:
                stderr = encode(self._said())
                result = subprocess.CompletedProcess(self._args, returncode, b"", stderr)
                raise error_from(self._args, result)
        finally:
            self._stderr.close()

    def update(self, ref: str, new: str, old: str | None = None) -> None:
        """Point `ref` at `new` (making it if need be), if it's at `old`."""
        self._change("update", ref, new, old)

    def delete(self, ref: str, old: str | None = None) -> None:
        """Delete `ref`, if it's at `old`."""
        self._change("delete", ref, old)

    def verify(self, ref: str, old: str | None) -> None:
        """Change nothing, but only if `ref` is at `old`; None: only if it doesn't exist."""
        self._change("verify", ref, old)

    def prepare(self) -> None:
        """Have git check every change so far and lock its ref, as it would to commit. Raises
        GitError if git can't make a change (e.g. a ref isn't at its `old`, or is locked), as
        the reference-transaction hook can say too: git then gives up the transaction,
        changing nothing, and it takes nothing more."""
        self._send("prepare")

    def commit(self) -> None:
        """Make every change, preparing them first if `prepare` didn't, and end the
        transaction. Raises GitError, changing nothing, if git can't make a change."""
        self._send("commit")
        self._closed = True

    def abort(self) -> None:
        """End the transaction, making none of its changes, and let go of its locks."""
        self._changes.clear()
        self._send("abort")
        self._closed = True

    def _change(self, command: str, ref: str, *values: str | None) -> None:
        """Add a change, `command` of `ref`, given `values`, None for an empty field."""
        if self._closed:
            raise ValueError("the ref transaction is closed")
        for value in (ref, *values):
            # In git's -z input, a NUL ends a field, and an empty one is a value left out.
            if value is not None and (not value or "\0" in value):
                raise ValueError(f"a ref transaction can't take {value!r}")
        fields = [encode(ref), *(encode(value or "") for value in values)]
        self._changes.append(command.encode() + b" " + b"\0".join(fields) + b"\0")

    def _send(self, command: str) -> None:
        """Send the changes not yet sent, then `command`, and read git's `<command>: ok`."""
        if self._closed:
            raise ValueError("the ref transaction is closed")
        assert self._process.stdin is not None
        sent = b"".join(self._changes) + command.encode() + b"\0"
        self._changes.clear()
        # If git exited on a change it refused, the pipe is closed: what it said is read below.
        with contextlib.suppress(OSError):
            self._process.stdin.write(sent)
            self._process.stdin.flush()
        seconds = self._git.timeout
        try:
            reply = self._output.readline(None if seconds is None else time.monotonic() + seconds)
        except PastDeadline:
            self._closed = True
            self._end(stop=True)
            raise GitTimeoutError(self._args, None, self._said()) from None
        if reply != f"{command}: ok\n".encode():
            self._closed = True
            returncode = self._end()
            if returncode == 0:
                raise GitError(self._args, 0, f"update-ref answered {command} with {reply!r}")
            stderr = encode(self._said())
            raise error_from(
                self._args, subprocess.CompletedProcess(self._args, returncode, reply, stderr)
            )

    def _end(self, *, stop: bool = False) -> int:
        """Close git's stdin, which aborts a transaction not ended, as git does at the end of
        its input, and wait for it to exit (with `stop`, stopping it first), stopping it past
        EXIT_WAIT: its exit status. Stopped, not killed, git removes its lock files."""
        assert self._process.stdin is not None
        with contextlib.suppress(OSError):
            self._process.stdin.close()
        if stop:
            self._process.terminate()
        try:
            returncode = self._process.wait(EXIT_WAIT)
        except subprocess.TimeoutExpired:
            self._process.kill()
            returncode = self._process.wait()
        self._output.close()
        return returncode

    def _said(self) -> str:
        """What git wrote to its stderr file."""
        self._stderr.seek(0)
        return decode(self._stderr.read()).strip()


def ref_transaction(git: Git, *, message: str | None = None, deref: bool = True) -> RefTransaction:
    """A ref transaction, for a `with` block, which starts it over one `git update-ref
    --stdin` (with `-m <message>`, the message its changes leave in the reflogs): git makes
    its changes as it's committed, and none if the block ends first (or raises), or if git
    can't make one. The reference-transaction hook runs as git prepares, commits or aborts it.
    With `deref` False (`--no-deref`), a change to a symbolic ref changes that ref itself,
    not the one it points at.

        with ref_transaction(git, message="publish") as transaction:
            transaction.update("refs/heads/a", new, old)
            transaction.verify("refs/heads/b", b_tip)
            transaction.prepare()  # GitError if git can't make them; then locked
            ...  # e.g. push, then commit only if that worked
            transaction.commit()

    Raises GitTimeoutError if git doesn't answer within the client's timeout (it's then
    stopped, which aborts the transaction, unless it was committed already), and GitError if git fails other than for a
    change (e.g. not a repository). `FakeGit` can't start it (it needs `popen`)."""
    args = ("update-ref", *(["-m", message] if message else []))
    return RefTransaction(git, (*args, *([] if deref else ["--no-deref"]), "--stdin", "-z"))
