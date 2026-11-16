"""Which GitError each failure raises, from git's real messages: if git rewords one, its test
fails."""

import http.server
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from git_client import (
    ConfigLockedError,
    ConflictError,
    Git,
    GitError,
    NotARepositoryError,
    PushRejectedError,
    RemoteError,
    UnknownRevisionError,
)


def test_a_directory_with_no_repository(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    git = Git(tmp_path / "plain", env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})

    with pytest.raises(NotARepositoryError):
        git.run("rev-parse", "HEAD")


def test_a_directory_that_isnt_there(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        Git(tmp_path / "missing").run("status")


@pytest.mark.parametrize(
    "args",
    [
        ("log", "no-such-branch"),  # ambiguous argument: unknown revision or path
        ("log", "no-such-branch", "--"),  # bad revision
        ("rev-parse", "--verify", "no-such-branch"),  # Needed a single revision
        ("merge-base", "no-such-branch", "HEAD"),  # Not a valid object name
    ],
)
def test_a_revision_that_names_nothing(repo: Git, args: tuple[str, ...]) -> None:
    with pytest.raises(UnknownRevisionError):
        repo.run(*args)


def test_a_name_that_is_a_revision_and_a_path_is_another_failure(repo: Git) -> None:
    # "ambiguous argument 'a': both revision and filename": it names something.
    repo.run("branch", "a")

    with pytest.raises(GitError) as raised:
        repo.run("log", "a")

    assert type(raised.value) is GitError


def test_a_locked_config_file(repo: Git) -> None:
    (Path(repo.directory) / ".git" / "config.lock").write_bytes(b"")

    with pytest.raises(ConfigLockedError):
        repo.run("config", "x.y", "z")


def test_a_remote_that_isnt_a_repository(repo: Git, tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()

    with pytest.raises(RemoteError):
        repo.run("ls-remote", str(tmp_path / "plain"))


@pytest.fixture
def not_found() -> Iterator[str]:
    """The URL of a server on this machine that has no repositories."""

    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), NotFound) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
        finally:
            server.shutdown()
            thread.join()


# A proxy would answer for the server on this machine.
NO_PROXY = {"no_proxy": "*", "NO_PROXY": "*"}


def test_a_remote_with_no_such_repository(repo: Git, not_found: str) -> None:
    with pytest.raises(RemoteError):
        repo.run("ls-remote", not_found, env=NO_PROXY)


def test_a_remote_that_cant_be_reached(repo: Git) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]

    with pytest.raises(RemoteError):
        repo.run("ls-remote", f"http://127.0.0.1:{port}/repo.git", env=NO_PROXY)


@pytest.fixture
def behind(repo: Git, tmp_path: Path) -> Git:
    """repo, with origin, a bare clone of it, to which another clone has pushed a commit."""
    origin, other = tmp_path / "origin.git", tmp_path / "other"
    repo.run("clone", "--quiet", "--bare", repo.directory, str(origin))
    repo.run("clone", "--quiet", str(origin), str(other))
    Git(other).run("commit", "--quiet", "--allow-empty", "-m", "theirs")
    Git(other).run("push", "--quiet", "origin", "main")
    repo.run("remote", "add", "origin", str(origin))
    repo.run("commit", "--quiet", "--allow-empty", "-m", "ours")
    return repo


def test_a_push_that_needs_a_fetch_first(behind: Git) -> None:
    with pytest.raises(PushRejectedError):
        behind.run("push", "--dry-run", "--no-verify", "origin", "main")


def test_a_push_that_isnt_a_fast_forward(behind: Git) -> None:
    behind.run("fetch", "--quiet", "origin")

    with pytest.raises(PushRejectedError):
        behind.run("push", "--quiet", "origin", "main")


@pytest.fixture
def diverged(repo: Git) -> Git:
    """repo, with a branch, topic, that changes `a` as main does too."""
    repo.run("switch", "--quiet", "-c", "topic")
    (Path(repo.directory) / "a").write_bytes(b"topic\n")
    repo.run("commit", "--quiet", "-am", "topic")
    repo.run("switch", "--quiet", "main")
    (Path(repo.directory) / "a").write_bytes(b"main\n")
    repo.run("commit", "--quiet", "-am", "main")
    return repo


@pytest.mark.parametrize(
    "args", [("merge", "topic"), ("cherry-pick", "topic"), ("rebase", "topic")]
)
def test_a_conflict(diverged: Git, args: tuple[str, ...]) -> None:
    with pytest.raises(ConflictError):
        diverged.run(*args)
