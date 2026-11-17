"""What a command's timeout, or an interrupt while it runs, stops, and how git gets to clean up
first (e.g. remove index.lock). Whether the tool has a controlling terminal changes what's
stopped (see Git), so on Linux and macOS these tests run a tool, TOOL, in a process of its own:
in a session of its own, which has no terminal, or with a pty as its terminal."""

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from git_client import Git, GitTimeoutError
from git_client.client import (
    _has_terminal,  # pyright: ignore[reportPrivateUsage]
    _own_process_group,  # pyright: ignore[reportPrivateUsage]
    _stop,  # pyright: ignore[reportPrivateUsage]
)

NO_TERMINAL_ON_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no controlling terminal, pty or process group for git to be in or not;"
    " its test is test_a_timeout_stops_git_and_what_it_started_on_windows",
)

HOOK = """\
import pathlib, sys, time
started, release, finished = map(pathlib.Path, sys.argv[1:])
started.touch()
deadline = time.monotonic() + 10
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
finished.touch()
"""


@dataclass(frozen=True)
class Hook:
    """A pre-commit hook's files: it makes `started`, then waits for `release` (at most 10s),
    then makes `finished`."""

    started: Path
    release: Path
    finished: Path

    def wait_until_started(self) -> None:
        deadline = time.monotonic() + 10
        while not self.started.exists():
            assert time.monotonic() < deadline, "the hook didn't start"
            time.sleep(0.01)

    def outlived_git(self) -> bool:
        """Whether the hook still runs, once released."""
        self.release.touch()
        time.sleep(0.3)
        return self.finished.exists()


def write_hook(repo: Git, script: str, *args: Path) -> None:
    """Make repo's pre-commit hook run `script` with Python (through sh, as git runs hooks on
    every system), given `args`."""
    hooks = Path(repo.directory) / ".git" / "hooks"
    (hooks / "hook.py").write_text(script, encoding="utf-8")
    command = " ".join(
        f'"{path.as_posix()}"' for path in (Path(sys.executable), hooks / "hook.py", *args)
    )
    (hooks / "pre-commit").write_text(f"#!/bin/sh\n{command}\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)


@pytest.fixture
def hook(repo: Git, tmp_path: Path) -> Iterator[Hook]:
    """repo with a pre-commit hook that waits (see Hook), which `git commit -a` runs holding
    index.lock; repo's file `a` is changed, for `commit -a` to commit."""
    files = Hook(tmp_path / "started", tmp_path / "release", tmp_path / "finished")
    write_hook(repo, HOOK, files.started, files.release, files.finished)
    (Path(repo.directory) / "a").write_bytes(b"changed\n")
    yield files
    files.release.touch()


def index_lock(repo: Git) -> Path:
    return Path(repo.directory) / ".git" / "index.lock"


COMMIT = ("commit", "--quiet", "-a", "-m", "changed")

TOOL = """\
import signal, sys
from git_client import Git, GitTimeoutError
# As the README says a tool should, so that git is stopped on the way out.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
# As a tool started from a shell gets it, even if pytest was started with SIGINT ignored.
signal.signal(signal.SIGINT, signal.default_int_handler)
directory, timeout, terminal, groups, *args = sys.argv[1:]
if terminal == "terminal":
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # stdin's pty, as this session's terminal
git = Git(directory, timeout=float(timeout), process_groups=groups == "groups")
try:
    print("ok:", git.run(*args), flush=True)
except GitTimeoutError:
    print("timed out", flush=True)
except KeyboardInterrupt:
    print("interrupted", flush=True)
"""


def tool_command(
    tmp_path: Path, repo: Git, timeout: float, terminal: bool, *args: str, groups: bool = True
) -> list[str]:
    (tmp_path / "tool.py").write_text(TOOL, encoding="utf-8")
    mode = "terminal" if terminal else "none"
    tool = [sys.executable, str(tmp_path / "tool.py"), repo.directory, str(timeout), mode]
    return [*tool, "groups" if groups else "no-groups", *args]


def without_terminal(
    tmp_path: Path, repo: Git, timeout: float, *args: str, groups: bool = True
) -> subprocess.Popen[str]:
    """TOOL running `git <args>`, in a session of its own, which has no terminal (as in CI, or
    a service), with a client whose `process_groups` is `groups`."""
    return subprocess.Popen(
        tool_command(tmp_path, repo, timeout, False, *args, groups=groups),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        encoding="utf-8",
        start_new_session=True,
    )


@dataclass
class Terminal:
    """TOOL with a pty as its terminal, and the pty's other end."""

    process: subprocess.Popen[bytes]
    master: int

    def type(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def read_until(self, text: str, seconds: float = 10) -> str:
        """What the terminal shows, up to and including `text`."""
        shown = b""
        deadline = time.monotonic() + seconds
        while text.encode() not in shown:
            left = deadline - time.monotonic()
            assert left > 0, f"{text!r} not shown, only {shown!r}"
            if select.select([self.master], [], [], left)[0]:
                try:
                    shown += os.read(self.master, 1024)
                except OSError:  # Linux, once the tool has exited
                    break
        assert text.encode() in shown, f"{text!r} not shown, only {shown!r}"
        return shown.decode()


@pytest.fixture
def terminal(tmp_path: Path) -> Iterator[list[Terminal]]:
    """Gives started Terminals, and stops what's left of them after the test."""
    started: list[Terminal] = []
    yield started
    for each in started:
        each.process.kill()
        each.process.wait()
        os.close(each.master)


def with_terminal(
    started: list[Terminal], tmp_path: Path, repo: Git, timeout: float, *args: str
) -> Terminal:
    """TOOL running `git <args>` in a session of its own, with a pty as its terminal (as from
    a shell, where it's in the foreground)."""
    master, slave = os.openpty()
    process = subprocess.Popen(
        tool_command(tmp_path, repo, timeout, True, *args),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    started.append(Terminal(process, master))
    return started[-1]


@NO_TERMINAL_ON_WINDOWS
def test_without_a_terminal_a_timeout_stops_what_git_started(
    repo: Git, hook: Hook, tmp_path: Path
) -> None:
    # A hook that ignores SIGINT, so only the kill that follows stops it.
    ignoring = "import signal; signal.signal(signal.SIGINT, signal.SIG_IGN)\n" + HOOK
    write_hook(repo, ignoring, hook.started, hook.release, hook.finished)
    tool = without_terminal(tmp_path, repo, 1, *COMMIT)
    hook.wait_until_started()
    assert index_lock(repo).exists()

    output, _ = tool.communicate(timeout=10)

    assert output == "timed out\n"
    assert not index_lock(repo).exists()
    assert not hook.outlived_git()


# On Windows, an interrupt is Ctrl-C, which reaches every process on the console, git too, and
# _stop gives git _GRACE to handle it before taskkill. A test would need a console of its own
# for the tool, and a process attached to it to send Ctrl-C (sending it from the tests'
# console would interrupt them too); and whether git leaves index.lock without the wait is a
# race between its own handler and taskkill's start, which such a test couldn't lose reliably.
NO_INTERRUPT_TEST_ON_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Ctrl-C reaches every process on a Windows console, so a test would need a console"
    " of its own, and couldn't reliably show that git is given time to clean up (see above)",
)


@NO_INTERRUPT_TEST_ON_WINDOWS
def test_without_a_terminal_an_interrupt_stops_git_and_lets_it_clean_up(
    repo: Git, hook: Hook, tmp_path: Path
) -> None:
    tool = without_terminal(tmp_path, repo, 60, *COMMIT)
    hook.wait_until_started()

    tool.send_signal(signal.SIGINT)  # to the tool alone, as `kill -INT` sends it
    output, _ = tool.communicate(timeout=10)

    assert output == "interrupted\n"
    assert not index_lock(repo).exists()
    assert not hook.outlived_git()


@NO_TERMINAL_ON_WINDOWS
@pytest.mark.parametrize("groups", [True, False])
def test_without_a_terminal_process_groups_says_whether_git_gets_its_own(
    repo: Git, tmp_path: Path, groups: bool
) -> None:
    # TOOL leads its session's process group: is what git runs in another?
    own = f'"{Path(sys.executable).as_posix()}" -c "import os; print(os.getpgrp() != os.getsid(0))"'
    repo.run("config", "alias.own", f"!{own}")
    tool = without_terminal(tmp_path, repo, 60, "own", groups=groups)

    output, _ = tool.communicate(timeout=10)

    assert output == f"ok: {groups}\n"


@NO_TERMINAL_ON_WINDOWS
def test_without_a_terminal_sigterm_to_the_tools_group_stops_git_on_the_way_out(
    repo: Git, hook: Hook, tmp_path: Path
) -> None:
    tool = without_terminal(tmp_path, repo, 60, *COMMIT)
    hook.wait_until_started()

    # As `timeout` or a CI runner sends it: to the tool's group, which git, in a group of its
    # own, isn't in. TOOL turns it into SystemExit.
    os.killpg(tool.pid, signal.SIGTERM)
    tool.communicate(timeout=10)

    assert tool.returncode == 143
    assert not index_lock(repo).exists()
    assert not hook.outlived_git()


@NO_TERMINAL_ON_WINDOWS
def test_with_a_terminal_git_can_read_it_under_a_timeout(
    repo: Git, tmp_path: Path, terminal: list[Terminal]
) -> None:
    ask = f'"{Path(sys.executable).as_posix()}" -c "print(open(\'/dev/tty\').readline().strip())"'
    repo.run("config", "alias.ask", f"!{ask}")
    tool = with_terminal(terminal, tmp_path, repo, 5, "ask")

    tool.type(b"typed\n")

    assert "ok: typed" in tool.read_until("ok: ")


@NO_TERMINAL_ON_WINDOWS
def test_with_a_terminal_a_timeout_lets_git_clean_up(
    repo: Git, hook: Hook, tmp_path: Path, terminal: list[Terminal]
) -> None:
    tool = with_terminal(terminal, tmp_path, repo, 1, *COMMIT)
    hook.wait_until_started()

    tool.read_until("timed out")

    assert not index_lock(repo).exists()


@NO_TERMINAL_ON_WINDOWS
def test_with_a_terminal_ctrl_c_lets_git_clean_up(
    repo: Git, hook: Hook, tmp_path: Path, terminal: list[Terminal]
) -> None:
    tool = with_terminal(terminal, tmp_path, repo, 60, *COMMIT)
    hook.wait_until_started()

    tool.type(b"\x03")
    tool.read_until("interrupted")

    assert not index_lock(repo).exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows' own: Linux and macOS have a test with a terminal and one without",
)
def test_a_timeout_stops_git_and_what_it_started_on_windows(repo: Git, hook: Hook) -> None:
    with pytest.raises(GitTimeoutError):
        repo.run(*COMMIT, timeout=1)

    assert not hook.outlived_git()


ORPHAN = """\
import subprocess, sys, time
release = sys.argv[1]
# A process that holds git's stderr (a hook's stdout and stderr), but that stopping git's tree
# doesn't stop: its parent has exited, and on Linux and macOS it's in a session of its own, as
# a daemon would be, so it isn't in git's process group either.
orphan = (
    "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'],"
    " stderr=sys.stderr, start_new_session=sys.platform != 'win32')"
)
subprocess.run([sys.executable, "-c", orphan], stderr=sys.stderr, check=True)
deadline = time.monotonic() + 10
while not __import__("os").path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.01)
"""


def test_a_timeout_returns_soon_though_something_git_started_holds_its_output(
    repo: Git, tmp_path: Path
) -> None:
    write_hook(repo, ORPHAN, tmp_path / "release")
    (Path(repo.directory) / "a").write_bytes(b"changed\n")
    started = time.monotonic()

    try:
        with pytest.raises(GitTimeoutError):
            repo.run(*COMMIT, timeout=1)
    finally:
        (tmp_path / "release").touch()

    # The timeout, git's chance to stop, and at most a second for its output.
    assert time.monotonic() - started < 3.5


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill is Windows'")
def test_a_timeout_stops_git_when_taskkill_cant_run(
    repo: Git, hook: Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = Git(repo.directory, env={"SYSTEMROOT": os.environ["SYSTEMROOT"]})
    monkeypatch.setenv("SYSTEMROOT", str(Path(repo.directory) / "no-such-windows"))
    started = time.monotonic()

    with pytest.raises(GitTimeoutError):
        git.run(*COMMIT, timeout=1)

    # Not the hook's 10 seconds, which git, if it weren't killed, would wait out.
    assert time.monotonic() - started < 3.5


def test_a_second_interrupt_while_git_cleans_up_still_kills_it(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A git that waits for its stdin, and a second Ctrl-C as _stop waits for it to clean up.
    process = repo.popen("cat-file", "--batch")
    killed: list[bool] = []
    kill = process.kill
    monkeypatch.setattr(process, "kill", lambda: (killed.append(True), kill()))

    wait = process.wait

    def interrupted(timeout: float | None = None) -> int:
        if timeout is not None:  # _stop's wait for git, not Popen's on the way out
            raise KeyboardInterrupt
        return wait()

    monkeypatch.setattr(process, "wait", interrupted)

    with process, pytest.raises(KeyboardInterrupt):
        _stop(process, False, interrupted=True)

    assert killed


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no fork, sessions or groups")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_whether_there_is_a_terminal_is_asked_again_after_a_fork_or_a_new_session() -> None:
    def probes() -> int:
        _own_process_group(1, True)
        return _has_terminal.cache_info().misses

    first = probes()
    assert probes() == first  # asked once
    pid = os.fork()
    if pid == 0:  # the child: exits at once, with no cleanup of pytest's
        forked = probes()
        os.setsid()
        os._exit(0 if (forked, probes()) == (first + 1, first + 2) else 1)
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
