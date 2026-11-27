from collections.abc import Callable

import pytest

from git_client import (
    Git,
    GitError,
    LineChange,
    TreeChange,
    UnknownRevisionError,
    WrongTypeError,
    changed_paths,
    hash_blobs,
    line_changes,
)
from git_client.testing import FakeGit, GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]

KEEP = b"".join(b"line %d\n" % i for i in range(10))
ZEROS = "0" * 40


def make_diffs(git: Git) -> None:
    """Commits made of trees git builds, so paths no filesystem takes can be in them: `p0`, a
    root commit, and `p1` on it, which changes `a` (3 lines added, 1 removed), renames `keep`
    to `kept` (changing a line), makes `run` executable, and adds a binary file, `*`, a path
    with a newline, one that isn't UTF-8, `dir/f` and a submodule, `sub`; and `m`, a merge
    of p1 and p0 with p1's tree."""
    a0, a1, keep, kept, run, binary, other = hash_blobs(
        git,
        [b"a\n", b"x\ny\nz\n", KEEP, KEEP.replace(b"9", b"nine"), b"r\n",
         b"\0\1", b"o\n"],
    )  # fmt: skip

    def tree(*entries: tuple[str, str, bytes]) -> str:
        listing = b"".join(
            b"%s %s %s\t%s\0" % (mode.encode(), kind.encode(), id.encode(), path)
            for mode, id, path in entries
            for kind in ["tree" if mode == "040000" else "commit" if mode == "160000" else "blob"]
        )
        return git.run_bytes("mktree", "-z", input=listing).decode().strip()

    def commit(tree: str, *parents: str) -> str:
        options = [arg for parent in parents for arg in ("-p", parent)]
        return git.run("commit-tree", tree, *options, "-m", "x")

    p0 = commit(tree(("100644", a0, b"a"), ("100644", keep, b"keep"), ("100644", run, b"run")))
    t1 = tree(
        ("100644", a1, b"a"),
        ("100644", kept, b"kept"),
        ("100755", run, b"run"),
        ("100644", binary, b"bin"),
        ("100644", other, b"*"),
        ("100644", other, b"new\nline"),
        ("100644", other, b"caf\xe9"),
        ("040000", tree(("100644", other, b"f")), b"dir"),
        ("160000", p0, b"sub"),
    )
    p1 = commit(t1, p0)
    m = commit(t1, p1, p0)
    for name, id in (("p0", p0), ("p1", p1), ("m", m)):
        git.run("tag", name, id)


@pytest.fixture
def diffs(repo_from: RepoFrom) -> Git:
    return repo_from("diffs", make_diffs)


def statuses(changes: list[TreeChange]) -> list[tuple[str, str]]:
    return [(change.status, change.path) for change in changes]


P1_PATHS = ["*", "a", "bin", "caf\udce9", "dir/f", "keep", "kept", "new\nline", "run", "sub"]


def test_changed_paths_lists_what_a_commit_changes_in_gits_order(diffs: Git) -> None:
    [changes] = changed_paths(diffs.read_only(), ["p1"])

    assert [change.path for change in changes] == P1_PATHS
    by_path = {change.path: change for change in changes}
    a0, a1, run = diffs.run("rev-parse", "p0:a", "p1:a", "p0:run").split()
    assert by_path["a"] == TreeChange("M", "a", "100644", "100644", a0, a1)
    assert by_path["run"] == TreeChange("M", "run", "100644", "100755", run, run)
    assert by_path["keep"].status == "D"
    assert (by_path["keep"].new_mode, by_path["keep"].new_id) == ("000000", ZEROS)
    assert (by_path["sub"].status, by_path["sub"].new_mode) == ("A", "160000")
    assert (by_path["kept"].status, by_path["kept"].old_path) == ("A", None)


def test_changed_paths_finds_renames_if_asked(diffs: Git) -> None:
    [changes] = changed_paths(diffs, ["p1"], renames=True)

    [renamed] = [change for change in changes if change.status == "R"]
    assert (renamed.old_path, renamed.path, renamed.similarity) == ("keep", "kept", 86)
    assert [change.path for change in changes] == [p for p in P1_PATHS if p != "keep"]


def test_changed_paths_answers_each_diff_in_order(diffs: Git, git_calls: list[GitCall]) -> None:
    root, backwards, same, merge = changed_paths(
        diffs, ["p0", ("p1", "p0^{tree}"), ("p1", "m"), "m"]
    )

    assert statuses(root) == [("A", "a"), ("A", "keep"), ("A", "run")]
    assert set(statuses(backwards)) >= {("D", "kept"), ("A", "keep"), ("D", "sub")}
    [a] = [change for change in backwards if change.path == "a"]
    assert (a.old_id, a.new_id) == tuple(diffs.run("rev-parse", "p1:a", "p0:a").split())
    assert same == []
    assert merge == []  # as git diffs a merge by default: not at all
    assert [call[0] for call in git_calls[:2]] == ["cat-file", "diff-tree"]


def test_changed_paths_takes_only_the_paths_asked_for(
    diffs: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")  # which would take paths' marks as names
    [literal] = changed_paths(diffs, ["p1"], paths=["*", "dir", "new\nline"])
    [matched] = changed_paths(diffs, ["p1"], pathspecs=["k*"])

    assert statuses(literal) == [("A", "*"), ("A", "dir/f"), ("A", "new\nline")]
    assert statuses(matched) == [("D", "keep"), ("A", "kept")]


def test_changed_paths_can_ignore_submodules(diffs: Git) -> None:
    [changes] = changed_paths(diffs, ["p1"], ignore_submodules=True)

    assert "sub" not in [change.path for change in changes]
    assert "dir/f" in [change.path for change in changes]


def test_changed_paths_raises_for_a_name_that_names_nothing(diffs: Git) -> None:
    with pytest.raises(UnknownRevisionError, match="nope"):
        changed_paths(diffs, ["p0", ("p0", "nope")])
    with pytest.raises(WrongTypeError):
        changed_paths(diffs, ["p0^{tree}"])  # a tree, where a commit is diffed


@pytest.mark.parametrize("name", ["", "p0\np1", "p0\r", "p0\0"])
def test_changed_paths_refuses_a_name_git_would_read_as_another(name: str) -> None:
    with pytest.raises(ValueError, match="not a revision"):
        changed_paths(FakeGit(), [("p0", name)])


def fake_diff(output: bytes) -> FakeGit:
    """A FakeGit whose `p0` is ZEROS's commit, and whose diff-tree prints `output`."""
    git = FakeGit()
    git.respond(["cat-file", "--batch-check"], f"{ZEROS} commit 100\n")
    git.respond(["diff-tree"], output)
    return git


HEADER = ZEROS.encode() + b"\0"


@pytest.mark.parametrize(
    "output",
    [
        b"",  # nothing for the commit, as git prints for one it can't read
        HEADER + b":100644 100644 0 1 Q\0p\0",  # a status git doesn't document
        HEADER + b":100644 100644 0 1\0p\0",  # too few fields
        HEADER + b":100644 100644 0 1 M\0",  # no path
        HEADER + b":100644 100644 0 1 R90\0p\0",  # a rename with one path
        HEADER + b":100644 100644 0 1 R\0p\0q\0",  # a rename with no score
        HEADER + b":100644 100644 0 1 M90\0p\0",  # a score for what isn't a rename
        HEADER + b"stray",
    ],
)
def test_changed_paths_raises_for_output_git_doesnt_document(output: bytes) -> None:
    with pytest.raises(GitError, match="diff-tree printed"):
        changed_paths(fake_diff(output), ["p0"])


def test_line_changes_counts_each_files_lines(diffs: Git, git_calls: list[GitCall]) -> None:
    [changes] = line_changes(diffs.read_only(), [("p0", "p1")], renames=True)

    by_path = {change.path: change for change in changes}
    assert by_path["a"] == LineChange("a", 3, 1)
    assert by_path["kept"] == LineChange("kept", 1, 1, old_path="keep")
    assert by_path["bin"] == LineChange("bin", None, None)  # binary
    assert by_path["run"] == LineChange("run", 0, 0)
    assert by_path["new\nline"] == LineChange("new\nline", 1, 0)
    assert [call[0] for call in git_calls] == ["cat-file", "diff-tree"]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        HEADER + b"1\tx\tp\0",  # a count that isn't one
        HEADER + b"1\t2\t\0p\0",  # a rename with one path
    ],
)
def test_line_changes_raises_for_output_git_doesnt_document(output: bytes) -> None:
    with pytest.raises(GitError, match="diff-tree printed"):
        line_changes(fake_diff(output), ["p0"])


def test_changed_paths_raises_if_cat_file_answers_fewer_names_than_asked() -> None:
    git = FakeGit()
    git.respond(["cat-file", "--batch-check"], f"{ZEROS} commit 100\n")

    with pytest.raises(GitError, match="cat-file gave 1 answers for 2"):
        changed_paths(git, ["p0", "p1"])


def test_no_diffs_take_no_process() -> None:
    assert changed_paths(FakeGit(), []) == []  # which fails the test on any command
    assert line_changes(FakeGit(), [], renames=True) == []


def test_changed_paths_refuses_one_diff_for_many() -> None:
    with pytest.raises(TypeError, match="not one"):
        changed_paths(FakeGit(), "p1")
