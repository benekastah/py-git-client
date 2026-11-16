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
import sys
from git_client import Git, GitTimeoutError
directory, timeout, terminal, *args = sys.argv[1:]
if terminal == "terminal":
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # stdin's pty, as this session's terminal
try:
    print("ok:", Git(directory, timeout=float(timeout)).run(*args), flush=True)
except GitTimeoutError:
    print("timed out", flush=True)
except KeyboardInterrupt:
    print("interrupted", flush=True)
"""


def tool_command(
    tmp_path: Path, repo: Git, timeout: float, terminal: bool, *args: str
) -> list[str]:
    (tmp_path / "tool.py").write_text(TOOL, encoding="utf-8")
    mode = "terminal" if terminal else "none"
    return [sys.executable, str(tmp_path / "tool.py"), repo.directory, str(timeout), mode, *args]


def without_terminal(
    tmp_path: Path, repo: Git, timeout: float, *args: str
) -> subprocess.Popen[str]:
    """TOOL running `git <args>`, in a session of its own, which has no terminal (as in CI, or
    a service)."""
    return subprocess.Popen(
        tool_command(tmp_path, repo, timeout, False, *args),
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
    tool = without_terminal(tmp_path, repo, 1, *COMMIT)
    hook.wait_until_started()
    assert index_lock(repo).exists()

    output, _ = tool.communicate(timeout=10)

    assert output == "timed out\n"
    assert not index_lock(repo).exists()
    assert not hook.outlived_git()


@NO_TERMINAL_ON_WINDOWS
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
# A process that holds git's stderr (a hook's stdout and stderr), but that isn't in git's
# process tree any more: its parent has exited.
orphan = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], stderr=sys.stderr)"
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

    with pytest.raises(GitTimeoutError):
        git.run(*COMMIT, timeout=1)
