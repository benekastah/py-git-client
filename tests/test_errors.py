"""Which GitError each failure raises, from git's real messages: if git rewords one, its test
fails."""

import http.server
import socket
import sys
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


# A full object id, of an object the repository doesn't have.
MISSING = "0123" * 10


@pytest.mark.parametrize(
    "args",
    [
        ("log", "no-such-branch"),  # ambiguous argument: unknown revision or path
        ("log", "no-such-branch", "--"),  # bad revision
        ("rev-parse", "--verify", "no-such-branch"),  # Needed a single revision
        ("merge-base", "no-such-branch", "HEAD"),  # Not a valid object name
        ("log", MISSING),  # bad object
        ("rev-list", MISSING),
        ("show", MISSING),
        ("diff", MISSING),
        ("branch", "new", "no-such-branch"),  # not a valid object name: '...'
        ("commit-tree", "no-such-branch"),  # not a valid object name
        ("switch", "no-such-branch"),  # invalid reference
        ("worktree", "add", "../worktree", "no-such-branch"),
        ("rebase", "no-such-branch"),  # invalid upstream
        ("merge", "no-such-branch"),  # not something we can merge
        ("merge-tree", "--write-tree", "main", "no-such-branch"),
        ("tag", "new", "no-such-branch"),  # Failed to resolve '...' as a valid ref
        ("merge-base", "HEAD", MISSING),  # Not a valid commit name
        ("merge-base", "--is-ancestor", MISSING, "HEAD"),
        ("log", f"HEAD..{MISSING}"),  # Invalid revision range
        ("rebase", "--onto", MISSING, "HEAD"),  # Does not point to a valid commit
        ("commit-tree", "-p", MISSING, "HEAD^{tree}"),  # <id> is not a valid object
        ("reset", MISSING),  # Could not parse object
        ("cherry", "HEAD", MISSING),  # unknown commit
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


@pytest.mark.parametrize(
    "args",
    [
        ("merge-base", "HEAD", "HEAD^{tree}"),  # Not a valid commit name
        ("cherry", "HEAD", "HEAD^{tree}"),  # unknown commit
        ("reset", "HEAD^{tree}"),  # Could not parse object
        ("rebase", "--onto", "HEAD^{tree}", "HEAD"),  # Does not point to a valid commit
        # "expected commit type, but the object dereferences to tree type", then "not
        # something we can merge"
        ("merge", "HEAD^{tree}"),
    ],
)
def test_a_name_of_the_wrong_type_is_another_failure(repo: Git, args: tuple[str, ...]) -> None:
    # "object <id> is a tree, not a commit", then what git says of a name that names nothing.
    with pytest.raises(GitError) as raised:
        repo.run(*args)

    assert type(raised.value) is GitError


def test_a_locked_config_file(repo: Git) -> None:
    (Path(repo.directory) / ".git" / "config.lock").write_bytes(b"")

    with pytest.raises(ConfigLockedError):
        # Without git's retries for a second first.
        repo.run("-c", "core.configLockTimeout=0", "config", "x.y", "z")


def test_a_local_file_git_cant_read_is_no_remote(repo: Git, tmp_path: Path) -> None:
    # "unable to access '<path>'", as git says of a remote's URL it can't reach.
    (tmp_path / "a-directory").mkdir()

    with pytest.raises(GitError) as raised:
        repo.run("status", env={"GIT_CONFIG_GLOBAL": str(tmp_path / "a-directory")})

    assert type(raised.value) is GitError


def test_a_remote_that_isnt_a_repository(repo: Git, tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()

    with pytest.raises(RemoteError):
        repo.run("ls-remote", str(tmp_path / "plain"))


def test_a_remote_that_cant_be_read_from(repo: Git) -> None:
    # An ssh that fails, as one that can't connect does: git says only that it couldn't read.
    ssh = f'"{Path(sys.executable).as_posix()}" -c "import sys; sys.exit(255)"'

    with pytest.raises(RemoteError):
        repo.run(
            "ls-remote",
            "ssh://example.invalid/repo.git",
            env={"GIT_SSH_COMMAND": ssh, "GIT_ALLOW_PROTOCOL": "ssh"},
        )


@pytest.fixture
def not_found() -> Iterator[str]:
    """The URL of a server on this machine that has no repositories."""

    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), NotFound) as server:
        # Checking often whether it's been shut down, so the test ends soon after.
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
        finally:
            server.shutdown()
            thread.join()


# For a server on this machine: over HTTP, which the tests' git doesn't allow by default, and
# with no proxy, which would answer for it.
LOCAL_HTTP = {"GIT_ALLOW_PROTOCOL": "http", "no_proxy": "*", "NO_PROXY": "*"}


def test_a_remote_with_no_such_repository(repo: Git, not_found: str) -> None:
    with pytest.raises(RemoteError):
        repo.run("ls-remote", not_found, env=LOCAL_HTTP)


def test_a_remote_that_cant_be_reached(repo: Git) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]

    with pytest.raises(RemoteError):
        repo.run("ls-remote", f"http://127.0.0.1:{port}/repo.git", env=LOCAL_HTTP)


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


def test_a_push_the_remote_refuses(repo: Git, tmp_path: Path) -> None:
    # A push to the branch checked out in a repository with a worktree: " ! [remote rejected]".
    other = Git(tmp_path / "other")
    repo.run("clone", "--quiet", repo.directory, other.directory)
    other.run("commit", "--quiet", "--allow-empty", "-m", "theirs")

    with pytest.raises(PushRejectedError):
        other.run("push", "--quiet", "origin", "main")


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
