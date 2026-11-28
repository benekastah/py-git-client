from collections.abc import Callable
from pathlib import Path

import pytest

import git_client.diffs
from git_client import (
    Git,
    GitError,
    LineChange,
    ReadOnlyGit,
    TreeChange,
    TreeEntry,
    UnknownRevisionError,
    WrongTypeError,
    changed_paths,
    hash_blobs,
    line_changes,
    tree_entries,
)
from git_client.testing import FakeGit, GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]

KEEP = b"".join(b"line %d\n" % i for i in range(10))
ZEROS = "0" * 40


def make_diffs(git: Git) -> None:
    """Commits made of trees git builds, so paths no filesystem takes can be in them: `p0`, a
    root commit, and `p1` on it, which changes `a` (3 lines added, 1 removed), renames `keep`
    to `kept` (changing a line), makes `run` executable, turns the files `link` and `gitl`
    into a symlink and a submodule, the file `fd` into a directory and the directory `df` into
    a file, and adds a binary file, `*`, a path with a newline, one that isn't UTF-8, `:x`,
    `dir/f` and a submodule, `sub` (its message "add dir"); `m`, a merge of p1 and p0 with
    p1's tree; and `ann`, an annotated tag of p1."""
    a0, a1, keep, kept, run, binary, other, link, gitl, fd, dx, target = hash_blobs(
        git,
        [b"a\n", b"x\ny\nz\n", KEEP, KEEP.replace(b"9", b"nine"), b"r\n",
         b"\0\1", b"o\n", b"l\n", b"g\n", b"fd\n", b"dx\n", b"target"],
    )  # fmt: skip

    def tree(*entries: tuple[str, str, bytes]) -> str:
        listing = b"".join(
            b"%s %s %s\t%s\0" % (mode.encode(), kind.encode(), id.encode(), path)
            for mode, id, path in entries
            for kind in ["tree" if mode == "040000" else "commit" if mode == "160000" else "blob"]
        )
        return git.run_bytes("mktree", "-z", input=listing).decode().strip()

    def commit(tree: str, *parents: str, message: str = "x") -> str:
        options = [arg for parent in parents for arg in ("-p", parent)]
        return git.run("commit-tree", tree, *options, "-m", message)

    p0 = commit(
        tree(
            ("100644", a0, b"a"),
            ("100644", keep, b"keep"),
            ("100644", run, b"run"),
            ("100644", link, b"link"),
            ("100644", gitl, b"gitl"),
            ("100644", fd, b"fd"),
            ("040000", tree(("100644", dx, b"x")), b"df"),
        )
    )
    t1 = tree(
        ("100644", a1, b"a"),
        ("100644", kept, b"kept"),
        ("100755", run, b"run"),
        ("100644", binary, b"bin"),
        ("100644", other, b"*"),
        ("100644", other, b":x"),
        ("100644", other, b"new\nline"),
        ("100644", other, b"caf\xe9"),
        ("040000", tree(("100644", other, b"f")), b"dir"),
        ("160000", p0, b"sub"),
        ("120000", target, b"link"),
        ("160000", p0, b"gitl"),
        ("040000", tree(("100644", other, b"y")), b"fd"),
        ("100644", other, b"df"),
    )
    p1 = commit(t1, p0, message="add dir")
    m = commit(t1, p1, p0)
    for name, id in (("p0", p0), ("p1", p1), ("m", m)):
        git.run("tag", name, id)
    git.run("tag", "--annotate", "--message=x", "ann", p1)


@pytest.fixture
def diffs(repo_from: RepoFrom) -> Git:
    return repo_from("diffs", make_diffs)


def statuses(changes: list[TreeChange]) -> list[tuple[str, str]]:
    return [(change.status, change.path) for change in changes]


P1_PATHS = [
    "*", ":x", "a", "bin", "caf\udce9", "df", "df/x", "dir/f", "fd", "fd/y", "gitl", "kept",
    "link", "new\nline", "run", "sub",
]  # fmt: skip


def test_changed_paths_lists_what_a_commit_changes_in_gits_order(diffs: Git) -> None:
    [changes] = changed_paths(diffs.read_only(), ["p1"])

    assert [change.path for change in changes] == P1_PATHS
    by_path = {change.path: change for change in changes}
    a0, a1, run, link, target = diffs.run(
        "rev-parse", "p0:a", "p1:a", "p0:run", "p0:link", "p1:link"
    ).split()
    assert by_path["a"] == TreeChange("M", "a", "100644", "100644", a0, a1)
    assert by_path["run"] == TreeChange("M", "run", "100644", "100755", run, run)
    # A rename, found by default, as porcelain finds them.
    assert by_path["kept"].status == "R"
    assert (by_path["kept"].old_path, by_path["kept"].similarity) == ("keep", 86)
    assert (by_path["sub"].status, by_path["sub"].new_mode) == ("A", "160000")
    assert (by_path["sub"].old_mode, by_path["sub"].old_id) == ("000000", ZEROS)
    # A file that becomes a symlink or a submodule changes type; one that becomes a
    # directory, or a directory that becomes a file, is deleted, and its new form added.
    assert by_path["link"] == TreeChange("T", "link", "100644", "120000", link, target)
    assert (by_path["gitl"].status, by_path["gitl"].new_mode) == ("T", "160000")
    assert [by_path[p].status for p in ("df", "df/x", "fd", "fd/y")] == ["A", "D", "D", "A"]


def test_changed_paths_finds_no_renames_if_asked(diffs: Git) -> None:
    [changes] = changed_paths(diffs, ["p1"], renames=False)

    assert ("D", "keep") in statuses(changes)
    assert ("A", "kept") in statuses(changes)
    assert all(change.old_path is None for change in changes)


def test_changed_paths_answers_each_diff_in_order(diffs: Git, git_calls: list[GitCall]) -> None:
    root, backwards, same, merge = changed_paths(
        diffs, ["p0", ("p1", "p0^{tree}"), ("p1", "m"), "m"]
    )

    assert statuses(root) == [
        ("A", "a"), ("A", "df/x"), ("A", "fd"), ("A", "gitl"), ("A", "keep"), ("A", "link"),
        ("A", "run"),
    ]  # fmt: skip
    assert set(statuses(backwards)) >= {("R", "keep"), ("D", "sub"), ("T", "link")}
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
    assert statuses(matched) == [("R", "kept")]


def test_changed_paths_can_ignore_submodules(diffs: Git) -> None:
    [changes] = changed_paths(diffs, ["p1"], ignore_submodules=True)

    assert "sub" not in [change.path for change in changes]
    assert "dir/f" in [change.path for change in changes]


def test_changed_paths_takes_any_revision(diffs: Git) -> None:
    # A path's tree, a commit found by its message, an annotated tag, a tree.
    [subtree, found, tagged, tree] = changed_paths(
        diffs, [("p0:df", "p1:fd"), ":/add dir", "ann", ("p1^{tree}", "ann")]
    )

    assert statuses(subtree) == [("D", "x"), ("A", "y")]
    [p1] = changed_paths(diffs, ["p1"])
    assert found == p1
    assert tagged == p1
    assert tree == []


def test_changed_paths_raises_for_a_name_that_names_nothing(diffs: Git) -> None:
    with pytest.raises(UnknownRevisionError, match="nope"):
        changed_paths(diffs, ["p0", ("p0", "nope")])
    with pytest.raises(UnknownRevisionError, match="nope"):
        changed_paths(diffs, ["nope", "p0^{tree}"])  # not the tree's error, which comes after
    with pytest.raises(UnknownRevisionError, match="no upstream"):
        changed_paths(diffs, ["@{upstream}"])  # which cat-file exits on
    with pytest.raises(WrongTypeError, match="p0:a"):
        changed_paths(diffs, [("p0", "p0:a")])  # a blob, where a tree is diffed
    with pytest.raises(WrongTypeError):
        changed_paths(diffs, ["p0^{tree}"])  # a tree, where a commit is diffed


@pytest.mark.parametrize("name", ["", "p0\np1", "p0\r", "p0\0"])
def test_changed_paths_refuses_a_name_git_would_read_as_another(name: str) -> None:
    with pytest.raises(ValueError, match="not a revision"):
        changed_paths(FakeGit(), [("p0", name)])


@pytest.fixture
def fake_diff(monkeypatch: pytest.MonkeyPatch) -> Callable[[bytes], FakeGit]:
    """A FakeGit whose diff-tree prints the output given, every name naming ZEROS (in place
    of the CatFile, which a FakeGit can't start)."""

    def resolve(git: ReadOnlyGit, names: list[tuple[str, str]]) -> list[bytes]:
        return [ZEROS.encode()] * len(names)

    monkeypatch.setattr(git_client.diffs, "_resolve", resolve)

    def fake(output: bytes) -> FakeGit:
        git = FakeGit()
        git.respond(["diff-tree"], output)
        return git

    return fake


HEADER = ZEROS.encode() + b"\0"


@pytest.mark.parametrize(
    "output",
    [
        b"",  # nothing for the commit, as git prints for one it can't read
        HEADER + b":100644 100644 0 1 Q\0p\0",  # a status git doesn't document
        HEADER + b":100644 100644 0 1\0p\0",  # too few fields
        HEADER + b":100644 100644 0 1 M\0",  # no path
        HEADER + b":100644 100644 0 1 M\0\0",  # an empty one
        HEADER + b":100644 100644 0 1 R90\0p\0",  # a rename with one path
        HEADER + b":100644 100644 0 1 R\0p\0q\0",  # a rename with no score
        HEADER + b":100644 100644 0 1 M90\0p\0",  # a score for what isn't a rename
        HEADER + b"stray",
    ],
)
def test_changed_paths_raises_for_output_git_doesnt_document(
    fake_diff: Callable[[bytes], FakeGit], output: bytes
) -> None:
    with pytest.raises(GitError, match="diff-tree printed"):
        changed_paths(fake_diff(output), ["p0"])


def test_line_changes_counts_each_files_lines(diffs: Git, git_calls: list[GitCall]) -> None:
    [changes] = line_changes(diffs.read_only(), [("p0", "p1")])

    by_path = {change.path: change for change in changes}
    assert by_path["a"] == LineChange("a", 3, 1)
    assert by_path["kept"] == LineChange("kept", 1, 1, old_path="keep")
    assert by_path["bin"] == LineChange("bin", None, None)  # binary
    assert by_path["run"] == LineChange("run", 0, 0)
    assert by_path["new\nline"] == LineChange("new\nline", 1, 0)
    assert by_path["link"] == LineChange("link", 1, 1)  # a type change: one entry
    assert [change.path for change in changes].count("gitl") == 1
    assert [call[0] for call in git_calls] == ["cat-file", "diff-tree"]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        HEADER + b"1\tx\tp\0",  # a count that isn't one
        HEADER + b"1\t2\t\0p\0",  # a rename with one path
    ],
)
def test_line_changes_raises_for_output_git_doesnt_document(
    fake_diff: Callable[[bytes], FakeGit], output: bytes
) -> None:
    with pytest.raises(GitError, match="diff-tree printed"):
        line_changes(fake_diff(output), ["p0"])


def test_no_diffs_take_no_process() -> None:
    assert changed_paths(FakeGit(), []) == []  # which fails the test on any command
    assert line_changes(FakeGit(), [], renames=True) == []


def test_changed_paths_refuses_one_diff_for_many() -> None:
    with pytest.raises(TypeError, match="not one"):
        changed_paths(FakeGit(), "p1")


def test_tree_entries_lists_a_trees_entries(diffs: Git, git_calls: list[GitCall]) -> None:
    entries = tree_entries(diffs.read_only(), "p1")

    top = [
        "*", ":x", "a", "bin", "caf\udce9", "df", "dir", "fd", "gitl", "kept", "link",
        "new\nline", "run", "sub",
    ]  # fmt: skip
    assert [entry.path for entry in entries] == top
    by_path = {entry.path: entry for entry in entries}
    assert by_path["a"] == TreeEntry("100644", "blob", diffs.run("rev-parse", "p1:a"), "a")
    assert (by_path["run"].mode, by_path["run"].type) == ("100755", "blob")
    assert (by_path["dir"].mode, by_path["dir"].type) == ("040000", "tree")
    assert (by_path["sub"].mode, by_path["sub"].type) == ("160000", "commit")
    assert [call[0] for call in git_calls[:1]] == ["ls-tree"]


def test_tree_entries_recurses_if_asked(diffs: Git) -> None:
    paths = [entry.path for entry in tree_entries(diffs, "p1^{tree}", recursive=True)]

    assert "dir/f" in paths
    assert "dir" not in paths


def test_tree_entries_takes_only_the_paths_asked_for(
    diffs: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "0")  # which would take ":x" as magic

    def paths(*asked: str, recursive: bool = False) -> list[str]:
        return [e.path for e in tree_entries(diffs, "p1", asked, recursive=recursive)]

    assert paths("*", ":x", "dir", "nope", "-r") == ["*", ":x", "dir"]
    assert paths("dir", recursive=True) == ["dir/f"]
    assert paths("dir/") == ["dir/f"]
    below = Path(diffs.directory, "below")
    below.mkdir()
    assert [e.path for e in tree_entries(Git(below), "p1", ["a"])] == ["a"]  # from the top


@pytest.mark.parametrize("tree", ["nope", "-x"])
def test_tree_entries_raises_for_a_name_that_names_no_tree(diffs: Git, tree: str) -> None:
    with pytest.raises(UnknownRevisionError):
        tree_entries(diffs, tree)


def test_tree_entries_raises_for_a_name_of_a_blob(diffs: Git) -> None:
    with pytest.raises(WrongTypeError, match="not a tree object"):
        tree_entries(diffs, "p1:a")


@pytest.mark.parametrize(
    "output",
    [
        b"100644 blob 0\0",  # no path
        b"100644 blob 0\t\0",  # an empty one
        b"100644 blob\ta\0",  # too few fields
        b"100644 tag 0\ta\0",  # a type a tree can't hold
    ],
)
def test_tree_entries_raises_for_output_git_doesnt_document(output: bytes) -> None:
    git = FakeGit()
    git.respond(["ls-tree"], output)

    with pytest.raises(GitError, match="ls-tree printed"):
        tree_entries(git, "p0")
