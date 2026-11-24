"""Which GitError each failure raises, from git's real messages: if git rewords one, its test
fails."""

import http.server
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from git_client import (
    CatFile,
    ConfigLockedError,
    ConflictError,
    CorruptObjectError,
    Git,
    GitError,
    MissingObjectError,
    NotARepositoryError,
    PushRejectedError,
    RemoteError,
    UnknownRevisionError,
    WrongTypeError,
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
        pytest.param(
            ("merge-tree", "--write-tree", "main", "no-such-branch"),
            marks=pytest.mark.needs_git(2, 38, reason="for --write-tree"),
        ),
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
    with pytest.raises(UnknownRevisionError) as raised:
        repo.run(*args)

    assert raised.value.reason == raised.value.stderr


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
def test_a_name_of_the_wrong_type(repo: Git, args: tuple[str, ...]) -> None:
    # "object <id> is a tree, not a commit", then what git says of a name that names nothing.
    with pytest.raises(WrongTypeError) as raised:
        repo.run(*args)

    assert (raised.value.expected, raised.value.found) == ("commit", "tree")


def test_a_wrong_type_git_doesnt_name_has_no_types() -> None:
    # A tool's own, say: it has nothing of git's to read them from.
    error = WrongTypeError(["log"], 1, "not a commit")

    assert (error.expected, error.found) == (None, None)


def test_a_corrupt_object(damaged_repo: Git) -> None:
    # "fatal: loose object <id> (stored in <path>) is corrupt", or "packed object".
    with pytest.raises(CorruptObjectError):
        damaged_repo.run("rev-parse", "--verify", "main^{tree}")


def test_a_locked_config_file(repo: Git) -> None:
    (Path(repo.directory) / ".git" / "config.lock").write_bytes(b"")

    with pytest.raises(ConfigLockedError):
        # Without git's retries for a second first.
        repo.run("-c", "core.configLockTimeout=0", "config", "x.y", "z")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows paths cannot contain newlines")
@pytest.mark.parametrize(
    "trace_env",
    [
        pytest.param({}, id="none"),
        pytest.param({"GIT_TRACE_PERFORMANCE": "1"}, id="performance"),
        pytest.param({"GIT_TRACE2": "1"}, id="trace2"),
        pytest.param({"GIT_TRACE2_EVENT": "1"}, id="trace2-event"),
        pytest.param({"GIT_TRACE2_PERF": "1"}, id="trace2-perf"),
        pytest.param({"GIT_TRACE2": "1", "GIT_TRACE2_BRIEF": "1"}, id="trace2-brief"),
        pytest.param(
            {"GIT_TRACE2_PERF": "1", "GIT_TRACE2_PERF_BRIEF": "1"}, id="trace2-perf-brief"
        ),
    ],
)
def test_a_locked_config_file_with_a_newline_in_its_path(
    repo: Git, tmp_path: Path, trace_env: dict[str, str]
) -> None:
    config_path = tmp_path / "a\nconfig"
    config_path.with_name(config_path.name + ".lock").write_bytes(b"")

    with pytest.raises(ConfigLockedError):
        repo.run("config", "--file", str(config_path), "x.y", "z", env=trace_env)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows paths cannot contain newlines")
@pytest.mark.parametrize("trace", ["0", "1"])
@pytest.mark.parametrize(
    "directory",
    [
        "missing: File exists\nsegment",
        "missing\nsegment: File exists\nsegment",
        "missing\nerror: could not lock config file other: File exists\nsegment",
    ],
)
def test_a_config_path_that_contains_the_lock_error_is_not_a_busy_lock(
    repo: Git, tmp_path: Path, directory: str, trace: str
) -> None:
    config_path = tmp_path / directory / "config"
    with pytest.raises(GitError) as raised:
        repo.run(
            "config", "--file", str(config_path), "x.y", "z", env={"GIT_TRACE_PERFORMANCE": trace}
        )

    assert type(raised.value) is GitError


def test_a_locked_config_file_is_another_failure_when_git_gives_no_reason(repo: Git) -> None:
    # "could not lock config file .git/config", with no reason, as for a missing directory.
    repo.run("config", "x.y", "z")
    (Path(repo.directory) / ".git" / "config.lock").write_bytes(b"")

    with pytest.raises(GitError) as raised:
        repo.run("-c", "core.configLockTimeout=0", "config", "--rename-section", "x", "renamed")

    assert type(raised.value) is GitError


def test_a_config_file_git_cant_lock_for_another_reason_is_another_failure(repo: Git) -> None:
    # "could not lock config file no-such-directory/x: No such file or directory"
    with pytest.raises(GitError) as raised:
        repo.run("config", "--file", "no-such-directory/x", "x.y", "z")

    assert type(raised.value) is GitError


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


# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def fall_behind(repo: Git) -> None:
    """Give repo origin, a bare clone of it, to which another clone has pushed a commit, and
    a commit of its own."""
    origin, other = (
        Path(repo.directory).parent / "origin.git",
        Path(repo.directory).parent / "other",
    )
    repo.run("clone", "--quiet", "--bare", repo.directory, str(origin))
    repo.run("clone", "--quiet", str(origin), str(other))
    Git(other).run("commit", "--quiet", "--allow-empty", "-m", "theirs")
    Git(other).run("push", "--quiet", "origin", "main")
    repo.run("remote", "add", "origin", str(origin))
    repo.run("commit", "--quiet", "--allow-empty", "-m", "ours")


@pytest.fixture
def behind(repo_from: RepoFrom) -> Git:
    """A repository whose origin has a commit it doesn't (see fall_behind)."""
    return repo_from("behind", fall_behind)


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


def diverge(repo: Git) -> None:
    """Give repo a branch, topic, that changes `a` as main does too."""
    repo.run("switch", "--quiet", "-c", "topic")
    (Path(repo.directory) / "a").write_bytes(b"topic\n")
    repo.run("commit", "--quiet", "-am", "topic")
    repo.run("switch", "--quiet", "main")
    (Path(repo.directory) / "a").write_bytes(b"main\n")
    repo.run("commit", "--quiet", "-am", "main")


@pytest.fixture
def diverged(repo_from: RepoFrom) -> Git:
    """A repository whose main and topic change `a` each (see diverge)."""
    return repo_from("diverged", diverge)


@pytest.mark.parametrize(
    "args", [("merge", "topic"), ("cherry-pick", "topic"), ("rebase", "topic")]
)
def test_a_conflict(diverged: Git, args: tuple[str, ...]) -> None:
    with pytest.raises(ConflictError):
        diverged.run(*args)


def make_partial_clone(origin: Git) -> None:
    """Give make_repo's repository a second commit, changing `a`, and clone it beside it, as
    `clone`, without blobs: a partial clone, whose promisor remote is the repository."""
    origin.run("config", "uploadpack.allowFilter", "true")
    (Path(origin.directory) / "a").write_bytes(b"b\n")
    origin.run("commit", "--quiet", "-am", "second")
    url = Path(origin.directory).as_uri()
    Git(Path(origin.directory).parent).run(
        "clone", "--quiet", "--no-checkout", "--filter=blob:none", url, "clone"
    )


@pytest.fixture
def partial_clone(repo_from: RepoFrom) -> Git:
    """A client of make_partial_clone's clone."""
    return Git(Path(repo_from("partial-clone", make_partial_clone).directory).parent / "clone")


@pytest.fixture
def unfetchable(partial_clone: Git) -> Git:
    """partial_clone, its promisor remote gone, so it can't fetch the blobs it lacks."""
    partial_clone.run("config", "remote.origin.url", str(Path(partial_clone.directory) / "gone"))
    return partial_clone


# Before 2.45 (but for 2.39.4 to 2.44.1), git ignores GIT_NO_LAZY_FETCH, and fetches.
NEEDS_NO_LAZY_FETCH = pytest.mark.needs_git(2, 45, reason="for GIT_NO_LAZY_FETCH")


# Older git says only why the fetch failed, not which object it couldn't fetch.
@pytest.mark.needs_git(2, 39, reason="for its 'could not fetch <id> from promisor remote'")
def test_an_object_a_partial_clone_cant_fetch(unfetchable: Git) -> None:
    blob = unfetchable.run("rev-parse", "HEAD:a")

    with pytest.raises(MissingObjectError):
        unfetchable.run("log", "-p", "-1")
    with pytest.raises(MissingObjectError):
        unfetchable.resolve(blob)
    with CatFile(unfetchable) as cat_file:
        with pytest.raises(MissingObjectError):
            cat_file.info(blob)
        with pytest.raises(MissingObjectError):
            cat_file.read(blob)


@NEEDS_NO_LAZY_FETCH
def test_a_read_only_client_takes_a_blob_a_partial_clone_lacks_as_naming_nothing(
    partial_clone: Git,
) -> None:
    # cat-file says only that it's missing, as of a name that names nothing: git doesn't say
    # why, so neither can the library, short of a process of its own per name.
    reader = partial_clone.read_only()
    blob = partial_clone.run("rev-parse", "HEAD:a")  # which reads only the tree

    assert reader.resolve(blob) is None
    with CatFile(reader) as cat_file:
        assert cat_file.info("HEAD:a") is None
        with pytest.raises(UnknownRevisionError):
            cat_file.read(blob)
