from collections.abc import Callable

import pytest

from git_client import (
    Git,
    GitError,
    ReadOnlyGit,
    UnknownRevisionError,
    WrongTypeError,
    fork_point,
    independent,
    is_ancestor,
    merge_base,
    merge_bases,
)
from git_client.testing import FakeGit, GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


def make_graph(git: Git) -> None:
    """On make_repo's first commit (main): `a` (a1, a2) and `b` (b1); `p` and `q`, merges of
    a1 and b1 each way (a criss-cross, so they have two merge bases); `o`, with no history in
    common; `up`, moved from u1 to u2, each on main, and `topic`, t1, made on u1."""
    commits: dict[str, str] = {}
    for name, parents in [
        ("a1", ["main"]),
        ("a2", ["a1"]),
        ("b1", ["main"]),
        ("p", ["a1", "b1"]),
        ("q", ["b1", "a1"]),
        ("o", []),
        ("u1", ["main"]),
        ("t1", ["u1"]),
        ("u2", ["main"]),
    ]:
        flags = [arg for parent in parents for arg in ("-p", commits.get(parent, parent))]
        commits[name] = git.run("commit-tree", *flags, "-m", name, "main^{tree}")
    refs = {"a": "a2", "b": "b1", "p": "p", "q": "q", "o": "o", "up": "u1", "topic": "t1"}
    for ref, commit in refs.items():
        git.run("update-ref", f"refs/heads/{ref}", commits[commit])
    git.run("update-ref", "refs/heads/up", commits["u2"])


@pytest.fixture
def graph(repo_from: RepoFrom) -> Git:
    return repo_from("graph", make_graph)


def message(git: Git, name: str) -> str:
    """The subject of the commit `name` names."""
    return git.run("log", "-1", "--format=%s", name)


def test_is_ancestor_asks_one_merge_base_per_pair(graph: Git, git_calls: list[GitCall]) -> None:
    reader = graph.read_only()

    assert is_ancestor(reader, "main", "a")
    assert not is_ancestor(reader, "a", "main")  # asymmetric
    assert is_ancestor(graph, "a", "a")  # a commit is its own ancestor
    assert not is_ancestor(graph, "a", "o")  # nothing in common
    assert [call[:2] for call in git_calls] == [["merge-base", "--is-ancestor"]] * 4


def test_merge_base_and_merge_bases(graph: Git) -> None:
    assert message(graph, merge_base(graph.read_only(), "a", "b") or "") == "first"
    assert message(graph, merge_base(graph, "a", "p") or "") == "a1"
    assert merge_base(graph, "a", "o") is None  # nothing in common
    assert sorted(message(graph, found) for found in merge_bases(graph, "p", "q")) == ["a1", "b1"]
    assert [message(graph, found) for found in merge_bases(graph, "a", "p")] == ["a1"]
    assert merge_bases(graph, "a", "o") == []


def test_independent_keeps_what_no_other_reaches_with_one_process(
    graph: Git, git_calls: list[GitCall]
) -> None:
    found = independent(graph.read_only(), "main", "a", "p", "b", "o")

    assert [call[:2] for call in git_calls] == [["merge-base", "--independent"]]
    assert sorted(message(graph, commit) for commit in found) == ["a2", "o", "p"]


def test_independent_of_none_starts_no_git(git_calls: list[GitCall]) -> None:
    assert independent(FakeGit()) == []
    assert git_calls == []


def test_fork_point_reads_the_upstreams_reflog(graph: Git) -> None:
    # Where topic forked from up, before up moved, not their merge base.
    assert message(graph, fork_point(graph.read_only(), "up", "topic") or "") == "u1"
    assert message(graph, merge_base(graph, "up", "topic") or "") == "first"
    assert fork_point(graph, "refs/heads/up", "o") is None  # nothing in common


# Each function, given a client and two revisions.
type Ask = Callable[[ReadOnlyGit, str, str], object]
ASKS: list[Ask] = [is_ancestor, merge_base, merge_bases, independent, fork_point]


@pytest.mark.parametrize(
    ("ask", "first", "second"),
    [
        (is_ancestor, "nothing", "a"),
        (is_ancestor, "a", "nothing"),
        (merge_base, "a", "nothing"),
        (merge_bases, "nothing", "a"),
        (independent, "a", "nothing"),
        (fork_point, "nothing", "a"),  # No such ref
        (fork_point, "up", "nothing"),
    ],
)
def test_a_revision_that_names_nothing_raises(
    graph: Git, ask: Ask, first: str, second: str
) -> None:
    with pytest.raises(UnknownRevisionError):
        ask(graph, first, second)


def test_a_tree_is_no_commit(graph: Git) -> None:
    with pytest.raises(WrongTypeError):
        is_ancestor(graph, "main^{tree}", "a")


def test_fork_point_raises_where_git_says_it_failed_though_it_exits_1(graph: Git) -> None:
    # Where --fork-point exits 1, its no, but prints an error: a commit that's a tree or a
    # blob, and an upstream that's an annotated tag with no reflog (no commit, as it reads it).
    blob = graph.run("hash-object", "-w", "--stdin", input="x\n")
    graph.run("tag", "-a", "-m", "t", "tg", "main")
    for commit in ("main^{tree}", blob):
        with pytest.raises(WrongTypeError):
            fork_point(graph, "up", commit)
    with pytest.raises(GitError, match="not a commit"):
        fork_point(graph, "tg", "a")


def test_a_name_starting_with_a_dash_is_a_revision(graph: Git) -> None:
    graph.run("update-ref", "refs/heads/-x", "a")

    assert is_ancestor(graph, "main", "-x")
    assert message(graph, merge_base(graph, "-x", "b") or "") == "first"
    assert len(merge_bases(graph, "-x", "b")) == 1
    assert len(independent(graph, "-x", "main")) == 1
    assert fork_point(graph, "-x", "a") == graph.run("rev-parse", "a")


@pytest.mark.parametrize("ask", ASKS)
def test_a_nul_is_refused(ask: Ask) -> None:
    with pytest.raises(ValueError, match="not a revision"):
        ask(FakeGit(), "a", "a\0b")
    with pytest.raises(ValueError, match="not a revision"):
        ask(FakeGit(), "a\0b", "a")


# Any exit status but 0 (yes, or found) and 1 (no, or none found: which --independent never
# says) is a failure, even with nothing on stderr.
@pytest.mark.parametrize(("ask", "returncode"), [*((ask, 2) for ask in ASKS), (independent, 1)])
def test_another_exit_status_raises(ask: Ask, returncode: int) -> None:
    git = FakeGit()
    git.respond(["merge-base"], returncode=returncode)

    with pytest.raises(GitError):
        ask(git, "a", "b")


TWO = b"1" * 40 + b"\n" + b"2" * 40 + b"\n"


@pytest.mark.parametrize(
    ("ask", "output"),
    [
        (merge_base, b""),
        (merge_base, TWO),
        (fork_point, b""),
        (fork_point, TWO),
        (merge_bases, b""),
        (independent, b""),
    ],
)
def test_output_git_doesnt_document_raises(ask: Ask, output: bytes) -> None:
    git = FakeGit()
    git.respond(["merge-base"], output)

    with pytest.raises(GitError, match="merge-base printed"):
        ask(git, "a", "b")
