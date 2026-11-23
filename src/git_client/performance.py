"""In-memory timing for public reads, Git subprocesses, and active CatFile work."""

import os
import subprocess
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from threading import Lock
from time import monotonic
from typing import IO, Literal, Protocol, Self, TypedDict, Unpack, cast

type EventKind = Literal["read", "process", "process_work"]


@dataclass(frozen=True)
class PerformanceEvent:
    """An interval in monotonic seconds, with process details when applicable.

    A launch that raises has neither a pid nor a returncode. A process whose exit has
    yet to be observed has an open interval (its end is None). A persistent process's
    lifetime includes idle time. CatFile records actual exchanges and shutdown separately.
    """

    kind: EventKind
    name: str
    start: float
    end: float | None
    args: tuple[str, ...] = ()
    pid: int | None = None
    returncode: int | None = None
    persistent: bool = False


class PerformanceRecorder:
    """A caller-owned recorder that can be shared across clients and threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[PerformanceEvent] = []

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        return self

    def __reduce__(self) -> tuple[type[Self], tuple[()]]:
        return type(self), ()

    @property
    def events(self) -> tuple[PerformanceEvent, ...]:
        """An immutable snapshot ordered by start time, including still-open processes."""
        with self._lock:
            return tuple(sorted(self._events, key=lambda event: event.start))

    @contextmanager
    def _measure(self, kind: EventKind, name: str, read: _Read | None) -> Generator[None]:
        start = monotonic()
        try:
            yield
        finally:
            if read is None or read.consulted_backend:
                event = PerformanceEvent(kind, name, start, monotonic())
                with self._lock:
                    self._events.append(event)

    def _register_process(self, process: _RecordedProcess) -> int:
        event = PerformanceEvent(
            "process",
            "git",
            process.started_at,
            None,
            process.git_args,
            pid=process.pid,
            persistent=process.persistent,
        )
        with self._lock:
            index = len(self._events)
            self._events.append(event)
            return index

    def _finish_process(self, process: _RecordedProcess, returncode: int | None) -> None:
        end = monotonic()
        with self._lock:
            if process.recorded:
                return
            index = process.event_index
            if index is None:
                self._events.append(
                    PerformanceEvent(
                        "process",
                        "git",
                        process.started_at,
                        end,
                        process.git_args,
                        persistent=process.persistent,
                    )
                )
            elif self._events[index].end is None:
                self._events[index] = replace(self._events[index], end=end, returncode=returncode)
            process.recorded = True


def measure(
    recorder: PerformanceRecorder | None, kind: EventKind, name: str, *, read: _Read | None = None
) -> AbstractContextManager[None]:
    """Time an operation when recording is enabled."""
    if recorder is None:
        return nullcontext()
    return recorder._measure(kind, name, read)  # pyright: ignore[reportPrivateUsage]


class _Measured(Protocol):
    @property
    def performance(self) -> PerformanceRecorder | None: ...


@dataclass
class _Read:
    owner: _Measured
    consulted_backend: bool = False


_current_read: ContextVar[_Read | None] = ContextVar("git_client_current_read", default=None)


def note_backend_read() -> None:
    """Keep the current read's timing now that it has consulted a backend."""
    if (read := _current_read.get()) is not None:
        read.consulted_backend = True


def record_read[**P, R](
    parameter: str = "self",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Time a public read that needs a backend, including same-client calls it delegates."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        name = function.__qualname__

        @wraps(function)
        def recorded(*args: P.args, **kwargs: P.kwargs) -> R:
            if not args and parameter not in kwargs:
                return function(*args, **kwargs)
            owner = cast(_Measured, args[0] if args else kwargs[parameter])
            recorder = owner.performance
            current = _current_read.get()
            if recorder is None or (current is not None and current.owner is owner):
                return function(*args, **kwargs)
            read = _Read(owner)
            token = _current_read.set(read)
            try:
                with measure(recorder, "read", name, read=read):
                    return function(*args, **kwargs)
            finally:
                _current_read.reset(token)

        return recorded

    return decorate


class _ProcessOptions(TypedDict, total=False):
    stdin: int | None
    stdout: int | IO[bytes] | IO[str] | None
    stderr: int | IO[bytes] | None
    env: dict[str, str]
    close_fds: bool
    process_group: int | None


class _RecordedProcess(subprocess.Popen[bytes]):
    """A Popen that records its exit once, whenever a caller first observes it."""

    def __init__(
        self,
        command: list[str] | list[bytes],
        recorder: PerformanceRecorder,
        args: Sequence[str | bytes],
        persistent: bool,
        **options: Unpack[_ProcessOptions],
    ) -> None:
        self.recorder = recorder
        self.git_args = tuple(map(os.fsdecode, args))
        self.persistent = persistent
        self.recorded = False
        self.event_index: int | None = None
        self.started_at = monotonic()
        try:
            super().__init__(command, **options)
        except BaseException:
            self._finish(None)
            raise
        self.event_index = recorder._register_process(self)  # pyright: ignore[reportPrivateUsage]

    def _finish(self, returncode: int | None) -> None:
        self.recorder._finish_process(self, returncode)  # pyright: ignore[reportPrivateUsage]

    def poll(self) -> int | None:
        returncode = super().poll()
        if returncode is not None and not self.recorded:
            self._finish(returncode)
        return returncode

    def wait(self, timeout: float | None = None) -> int:
        returncode = super().wait(timeout)
        if not self.recorded:
            self._finish(returncode)
        return returncode


def open_git_process(
    command: list[str] | list[bytes],
    recorder: PerformanceRecorder | None,
    args: Sequence[str | bytes],
    *,
    persistent: bool,
    **options: Unpack[_ProcessOptions],
) -> subprocess.Popen[bytes]:
    """Start a Git process, tracking launch and observed exit when requested."""
    if recorder is None:
        return subprocess.Popen(command, **options)
    note_backend_read()
    return _RecordedProcess(command, recorder, args, persistent, **options)
