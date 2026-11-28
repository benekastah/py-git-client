import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import IO

import pytest

from git_client import (
    EnvValue,
    Git,
    GitError,
    GitTimeoutError,
    NotARepositoryError,
    NotStartedError,
    ref_transaction,
)
from git_client.client import Arg
from git_client.testing import GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def make_refs(git: Git) -> None:
    """On make_repo's main: `a` at main, `b` a commit past it, and `sym`, a symbolic ref to
    `a`."""
    git.run("branch", "a")
    git.run("branch", "b", git.run("commit-tree", "-p", "main", "-m", "b", "main^{tree}"))
    git.run("symbolic-ref", "refs/heads/sym", "refs/heads/a")


@pytest.fixture
def refs(repo_from: RepoFrom) -> Git:
    return repo_from("refs", make_refs)


def tip(git: Git, branch: str) -> str | None:
    return git.resolve(f"refs/heads/{branch}")


def test_commit_makes_every_change_with_one_process(refs: Git, git_calls: list[GitCall]) -> None:
    a, b = tip(refs, "a"), tip(refs, "b")
    assert a is not None and b is not None

    with ref_transaction(refs, message="moved by a test") as transaction:
        transaction.verify("refs/heads/main", a)
        transaction.update("refs/heads/a", b, a)
        transaction.update("refs/heads/new", a, "0" * len(a))
        transaction.delete("refs/heads/b", b)
        transaction.verify("refs/heads/gone", None)
        transaction.prepare()
        transaction.commit()

    assert [tip(refs, name) for name in ("a", "new", "b")] == [b, a, None]
    updates = [call for call in git_calls if call[0] == "update-ref"]
    assert updates == [["update-ref", "-m", "moved by a test", "--stdin", "-z"]]
    assert refs.run("reflog", "-1", "--format=%gs", "refs/heads/a") == "moved by a test"


def test_a_transaction_not_committed_changes_nothing(refs: Git) -> None:
    a, b = tip(refs, "a"), tip(refs, "b")
    assert a is not None and b is not None

    with ref_transaction(refs) as transaction:
        transaction.update("refs/heads/a", b)
        transaction.prepare()
        # Prepared, the ref is locked: nothing else can change it.
        assert not refs.succeeds("update-ref", "refs/heads/a", b)
        transaction.abort()
    with ref_transaction(refs) as transaction:
        transaction.update("refs/heads/a", b)
    with pytest.raises(KeyError), ref_transaction(refs) as transaction:
        transaction.update("refs/heads/a", b)
        transaction.prepare()
        raise KeyError

    assert tip(refs, "a") == a
    assert refs.succeeds("update-ref", "refs/heads/a", b)  # no lock left


def test_prepare_raises_when_a_ref_isnt_where_its_said_to_be(refs: Git) -> None:
    a, b = tip(refs, "a"), tip(refs, "b")
    assert a is not None and b is not None

    with ref_transaction(refs) as transaction:
        transaction.update("refs/heads/main", b)
        transaction.verify("refs/heads/a", b)
        with pytest.raises(GitError, match="refs/heads/a"):
            transaction.prepare()

    assert tip(refs, "main") == a


def test_git_refuses_what_it_reads_as_wrong(refs: Git) -> None:
    b = tip(refs, "b")
    assert b is not None
    with ref_transaction(refs) as transaction:
        transaction.update("refs/heads/a..b", b)
        with pytest.raises(GitError, match="a..b"):
            transaction.commit()
    assert tip(refs, "a") != b


def test_deref_false_changes_a_symbolic_ref_itself(refs: Git) -> None:
    a, b = tip(refs, "a"), tip(refs, "b")
    assert a is not None and b is not None

    with ref_transaction(refs, deref=False) as transaction:
        transaction.update("refs/heads/sym", b)
        transaction.commit()

    assert tip(refs, "sym") == b and tip(refs, "a") == a
    assert not refs.succeeds("symbolic-ref", "refs/heads/sym")


def test_deref_moves_where_a_symbolic_ref_points(refs: Git) -> None:
    b = tip(refs, "b")
    assert b is not None

    with ref_transaction(refs) as transaction:
        transaction.update("refs/heads/sym", b)
        transaction.commit()

    assert tip(refs, "a") == b
    assert refs.run("symbolic-ref", "refs/heads/sym") == "refs/heads/a"


@pytest.mark.parametrize(
    ("ref", "value"), [("refs/heads/a\0b", "1" * 40), ("refs/heads/a", "1\0"), ("refs/heads/a", "")]
)
def test_refuses_what_git_would_read_differently(refs: Git, ref: str, value: str) -> None:
    with ref_transaction(refs) as transaction:
        with pytest.raises(ValueError):
            transaction.update(ref, value)
        with pytest.raises(ValueError):
            transaction.delete(ref, value)
        with pytest.raises(ValueError):
            transaction.verify(ref, value)


def test_a_closed_transaction_takes_nothing_more(refs: Git) -> None:
    with ref_transaction(refs) as transaction:
        transaction.commit()
        with pytest.raises(ValueError, match="closed"):
            transaction.update("refs/heads/a", "1" * 40)
        with pytest.raises(ValueError, match="closed"):
            transaction.prepare()


@pytest.mark.usefixtures("outside_any_repository")
def test_raises_as_git_fails_to_start_it() -> None:
    with pytest.raises(NotARepositoryError), ref_transaction(Git(".")):
        pass


@pytest.mark.usefixtures("outside_any_repository")
def test_starts_nothing_once_the_client_is_stopping() -> None:
    with pytest.raises(NotStartedError), ref_transaction(Git(".", stopping=lambda: True)):
        pass


class ScriptedGit(Git):
    """Runs `script`, Python, in place of git, keeping the process in `started`."""

    script: str
    started: list[subprocess.Popen[bytes]]

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", self.script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        self.started.append(process)
        return process


def scripted(repo: Git, script: str, timeout: float | None = None) -> ScriptedGit:
    git = ScriptedGit(repo.directory, timeout=timeout)
    git.script, git.started = "import sys, time\n" + script, []
    return git


# Answers start, then exits on the end of its input.
_STARTS = "sys.stdin.buffer.read(6); print('start: ok', flush=True); sys.stdin.buffer.read()\n"


def test_raises_and_stops_git_past_the_timeout(repo: Git) -> None:
    git = scripted(repo, "time.sleep(60)", timeout=0.2)
    with pytest.raises(GitTimeoutError), ref_transaction(git):
        pass
    (process,) = git.started
    if sys.platform != "win32":  # where terminating is killing
        # Terminated, as git removes its lock files then, not killed.
        assert process.returncode == -signal.SIGTERM


def test_kills_git_that_doesnt_exit_once_its_input_ends(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("git_client.transaction.EXIT_WAIT", 0.01)
    git = scripted(repo, _STARTS + "time.sleep(60)")
    with pytest.raises(GitError), ref_transaction(git):
        pass
    if sys.platform != "win32":
        assert git.started[0].returncode == -signal.SIGKILL


def test_raises_when_git_fails_as_its_input_ends(repo: Git) -> None:
    git = scripted(repo, _STARTS + "sys.exit('fatal: gave up')")
    with pytest.raises(GitError, match="gave up"), ref_transaction(git):
        pass


def test_raises_on_an_answer_git_doesnt_give(repo: Git) -> None:
    git = scripted(repo, "print('what')")
    with pytest.raises(GitError, match="answered start with b'what"), ref_transaction(git):
        pass
