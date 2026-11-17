from collections.abc import Callable

import pytest

from git_client import Git, RefSnapshot, refs
from git_client.testing import FakeGit, GitCall


def counting() -> tuple[Callable[[], int], list[int]]:
    """A read for a RefSnapshot that gives how many times it has been called, and that count."""
    reads = [0]

    def read() -> int:
        reads[0] += 1
        return reads[0]

    return read, reads


def answering_anything() -> FakeGit:
    git = FakeGit()
    git.respond([])
    return git


@pytest.mark.parametrize(
    "args",
    [
        # What a ReadOnlyGit runs.
        ["log", "-1"],
        ["rev-parse", "HEAD"],
        ["for-each-ref", "refs/heads/"],
        ["config", "--get", "branch.main.merge"],
        ["worktree", "list", "--porcelain"],
        ["--no-pager", "show", "HEAD"],
        # Options to git itself, before the command.
        ["-c", "core.quotePath=false", "-C", "sub", "log"],
        # Commands that write only objects, the index or the worktree.
        ["diff", "--quiet"],
        ["ls-remote", "origin"],
        ["merge-tree", "--write-tree", "a", "b"],
        ["merge-file", "-p", "a", "b", "c"],
        ["hash-object", "-w", "--stdin"],
        ["read-tree", "HEAD"],
        ["write-tree"],
        ["update-index", "--refresh"],
        ["apply", "--cached"],
        ["commit-tree", "-m", "m", "HEAD^{tree}"],
        ["mktree"],
        # The reading forms of commands that can also change refs.
        ["branch", "--show-current"],
        ["symbolic-ref", "--short", "HEAD"],
        ["remote"],
        ["remote", "get-url", "origin"],
        # A branch's settings that git doesn't list refs or upstreams by.
        ["config", "branch.topic.stackParent", "main"],
        ["config", "set", "branch.topic.description", "a topic"],
        ["config", "--unset", "--fixed-value", "branch.topic.stackBase", "abc"],
        ["config", "--local", "--replace-all", "branch.a.b.c.stackX", "1"],
    ],
)
def test_snapshot_outlives_commands_that_cant_change_refs(args: list[str]) -> None:
    git, (read, _) = answering_anything(), counting()
    snapshot = RefSnapshot(read)
    snapshot.get()

    git.run(*args)

    assert snapshot.get() == 1


@pytest.mark.parametrize(
    "args",
    [
        ["switch", "main"],
        ["-c", "core.warnAmbiguousRefs=false", "switch", "--quiet", "main"],
        ["commit", "-m", "m"],
        ["branch", "topic"],
        ["branch", "--list"],
        ["update-ref", "refs/heads/topic", "HEAD"],
        ["fetch", "origin"],
        ["push", "origin", "topic"],
        ["tag", "v1"],
        ["stash", "list"],
        ["symbolic-ref", "HEAD", "refs/heads/topic"],
        ["symbolic-ref", "--delete", "HEAD"],
        ["symbolic-ref", "-d", "HEAD"],
        ["remote", "add", "upstream", "url"],
        ["worktree", "add", "../wt"],
        ["gc"],
        # An alias, which could be anything.
        ["co", "main"],
        # Settings that say a branch's upstream, or what a remote's refs are called.
        ["config", "branch.topic.merge", "refs/heads/topic"],
        ["config", "set", "branch.topic.remote", "origin"],
        ["config", "--unset", "branch.topic.pushRemote"],
        ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        ["config", "--rename-section", "branch.a", "branch.b"],
        ["config", "core.ignoreCase", "true"],
        # Another file (which could be included), or settings that aren't one key's.
        ["config", "--file", "other", "branch.topic.stackParent", "main"],
        ["config", "--edit"],
        ["config", "branch.stackParent", "main"],
    ],
)
def test_snapshot_is_read_again_after_a_command_that_may_change_refs(args: list[str]) -> None:
    git, (read, _) = answering_anything(), counting()
    snapshot = RefSnapshot(read)
    snapshot.get()

    git.run(*args)

    assert snapshot.get() == 2


def test_snapshot_is_read_again_after_a_command_that_failed() -> None:
    # e.g. a rebase that stopped on a conflict, having moved HEAD.
    git, (read, _) = FakeGit(), counting()
    git.respond(["rebase"], returncode=1)
    snapshot = RefSnapshot(read)
    snapshot.get()

    assert not git.succeeds("rebase", "main")

    assert snapshot.get() == 2


def test_snapshot_is_read_once_until_refs_change() -> None:
    git, (read, reads) = answering_anything(), counting()
    snapshot = RefSnapshot(read)

    assert reads == [0]
    assert (snapshot.get(), snapshot.get()) == (1, 1)
    git.run("branch", "topic")
    assert (snapshot.get(), snapshot.get()) == (2, 2)


def test_a_change_while_the_snapshot_is_read_isnt_kept() -> None:
    # As another thread could make it: what was read may be from before the change.
    git, (read, _) = answering_anything(), counting()

    def read_and_change() -> int:
        value = read()
        if value == 1:
            git.run("branch", "topic")
        return value

    snapshot = RefSnapshot(read_and_change)

    assert (snapshot.get(), snapshot.get(), snapshot.get()) == (1, 2, 2)


def test_snapshot_of_refs_sees_changes_through_any_client(
    repo: Git, git_calls: list[GitCall]
) -> None:
    snapshot = RefSnapshot(lambda: set(refs(repo, "refs/heads/")))
    other = Git(repo.directory)

    assert snapshot.get() == {"refs/heads/main"}
    repo.run("log", "-1")
    assert snapshot.get() == {"refs/heads/main"}
    other.run("branch", "topic")
    assert snapshot.get() == {"refs/heads/main", "refs/heads/topic"}
    assert sum(call[0] == "for-each-ref" for call in git_calls) == 2


def test_a_long_lived_process_that_may_change_refs_counts_when_it_starts(repo: Git) -> None:
    snapshot = RefSnapshot(lambda: set(refs(repo, "refs/heads/")))
    snapshot.get()

    with repo.popen("update-ref", "--stdin") as process:
        stdin = process.stdin
        assert stdin is not None
        stdin.write(b"create refs/heads/topic HEAD\n")
        stdin.close()
        process.wait()

    assert snapshot.get() == {"refs/heads/main", "refs/heads/topic"}
