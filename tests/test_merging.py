import re
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import git_client.merging
from git_client import (
    CorruptObjectError,
    Git,
    GitError,
    MergeResult,
    MissingObjectError,
    NotARepositoryError,
    ReadOnlyError,
    UnknownRevisionError,
    WrongTypeError,
    hash_blobs,
    merge_trees,
    refs,
)
from git_client.testing import FakeGit, GitCall

Changes = Callable[[Git], None]


def _git_version() -> tuple[int, int]:
    version = Git(".").run("--version")
    major, minor = re.findall(r"\d+", version)[:2]
    return int(major), int(minor)


GIT_VERSION = _git_version()
NEEDS_WRITE_TREE = pytest.mark.skipif(
    GIT_VERSION < (2, 38), reason="merge-tree --write-tree is git 2.38's"
)
NEEDS_STDIN = pytest.mark.skipif(GIT_VERSION < (2, 39), reason="merge-tree --stdin is git 2.39's")


def commit(git: Git, parent: str | None, changes: Mapping[bytes, bytes | None]) -> str:
    """A commit on parent (or a root commit) with changes made to its files (None deletes
    one), made without the worktree, so a path can hold what no filesystem takes."""
    index = Path(git.directory) / ".git" / "scratch-index"
    env = {"GIT_INDEX_FILE": str(index)}
    git.run("read-tree", *([parent] if parent else ["--empty"]), env=env)
    contents = [content for content in changes.values() if content is not None]
    blobs = iter(hash_blobs(git, contents))
    entries = b"".join(
        (b"0 " + b"0" * 40 if content is None else b"100644 " + next(blobs).encode())
        + b"\t"
        + path
        + b"\0"
        for path, content in changes.items()
    )
    git.run_bytes("update-index", "-z", "--index-info", input=entries, env=env)
    tree = git.run("write-tree", env=env)
    index.unlink()
    return git.run("commit-tree", *(["-p", parent] if parent else []), "-m", "c", tree)


def make_branches(git: Git) -> None:
    """Branches, each a commit on main (whose file is `a`), for each kind of merge."""
    # Git for Windows otherwise refuses paths NTFS can't hold (e.g. with a newline).
    git.run("config", "core.protectNTFS", "false")
    main = git.run("rev-parse", "main")
    topic = commit(git, main, {b"x": b"x\n"})
    branches = {
        "ours": commit(git, main, {b"a": b"ours\n"}),
        "theirs": commit(git, main, {b"a": b"theirs\n"}),
        "x": topic,
        "y": commit(git, main, {b"y": b"y\n"}),
        "renamed": commit(git, main, {b"a": None, b"b": b"a\n"}),
        "deleted": commit(git, main, {b"a": None}),
        "added-1": commit(git, main, {b"n": b"1\n"}),
        "added-2": commit(git, main, {b"n": b"2\n"}),
        "file": commit(git, main, {b"d": b"file\n"}),
        "directory": commit(git, main, {b"d/e": b"directory\n"}),
        # topic's change, made again on another commit: it has landed there.
        "landed": commit(git, commit(git, main, {b"y": b"y\n"}), {b"x": b"x\n"}),
        "unrelated": commit(git, None, {b"u": b"u\n"}),
        # Each adds the same odd paths, differently.
        "odd-1": commit(git, main, {b"new\nline": b"1\n", b"sp ace\xff": b"1\n"}),
        "odd-2": commit(git, main, {b"new\nline": b"2\n", b"sp ace\xff": b"2\n"}),
    }
    # Each adds the file `lost` differently, but one's blob is lost (as from a damaged object
    # store), which merging the two has to read.
    branches["lost-blob-1"] = commit(git, main, {b"lost": b"lost 1\n"})
    branches["lost-blob-2"] = commit(git, main, {b"lost": b"lost 2\n"})
    # A commit whose tree is lost.
    branches["lost-tree"] = commit(git, main, {b"t": b"lost\n"})
    # Each adds a file to the directory `lost-dir`, so merging them reads both its trees, but
    # one is lost.
    branches["lost-subtree-1"] = commit(git, main, {b"lost-dir/1": b"lost subtree\n"})
    branches["lost-subtree-2"] = commit(git, main, {b"lost-dir/2": b"lost subtree\n"})
    # A file renamed and changed, and changed where it was: finding the rename reads the
    # renamed file's blob, which is lost.
    lines = b"".join(b"line %d\n" % i for i in range(50))
    renaming = commit(git, main, {b"r": lines})
    branches["lost-renamed"] = commit(git, renaming, {b"r": None, b"r2": b"renamed\n" + lines})
    branches["edited"] = commit(git, renaming, {b"r": lines + b"edited\n"})
    # A commit whose parent is lost: finding the merge base reads it.
    branches["lost-parent"] = commit(git, commit(git, main, {b"p": b"lost parent\n"}), {})
    lost = [
        f"{branches['lost-blob-1']}:lost",
        f"{branches['lost-tree']}^{{tree}}",
        f"{branches['lost-subtree-1']}:lost-dir",
        f"{branches['lost-renamed']}:r2",
        f"{branches['lost-parent']}^",
    ]
    # Each adds the file `corrupt` differently, but one's blob is damaged.
    branches["corrupt-1"] = commit(git, main, {b"corrupt": b"corrupt 1\n"})
    branches["corrupt-2"] = commit(git, main, {b"corrupt": b"corrupt 2\n"})
    damaged = git.run("rev-parse", f"{branches['corrupt-1']}:corrupt")
    for object_id in [*git.run("rev-parse", *lost).split(), damaged]:
        path = Path(git.directory) / ".git" / "objects" / object_id[:2] / object_id[2:]
        path.chmod(0o644)  # git makes it read-only
        if object_id == damaged:
            path.write_bytes(b"garbage")
        else:
            path.unlink()
    for name, object_id in branches.items():
        git.run("update-ref", f"refs/heads/{name}", object_id)


@pytest.fixture
def merges(repo_from: Callable[[str, Changes], Git]) -> tuple[Git, dict[str, str]]:
    """A repository with make_branches' branches, and each branch's commit by name."""
    git = repo_from("merges", make_branches)
    return git, {name[len("refs/heads/") :]: ref.object for name, ref in refs(git).items()}


def merged_alone(git: Git, ours: str, theirs: str) -> MergeResult:
    """What `git merge-tree --write-tree` says of one pair, run alone."""
    result = git.process(
        "merge-tree", "--write-tree", "-z", "--name-only", "--no-messages", ours, theirs,
        capture_output=True,
    )  # fmt: skip
    assert result.returncode in (0, 1), result.stderr
    tree, *paths, end = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    assert end == ""
    return MergeResult(result.returncode == 0, tree, tuple(paths))


# Pairs of make_branches' branches, of each kind of merge.
KINDS = [
    ("x", "y"),  # clean
    ("ours", "theirs"),  # a conflict in a file's content
    ("renamed", "deleted"),
    ("added-1", "added-2"),
    ("file", "directory"),
    ("landed", "x"),
    ("x", "x"),
    ("odd-1", "odd-2"),
]


@NEEDS_WRITE_TREE
def test_each_pair_gives_what_merge_tree_gives_for_it_alone(
    merges: tuple[Git, dict[str, str]],
) -> None:
    git, ids = merges
    pairs = [(ids[ours], ids[theirs]) for ours, theirs in KINDS]

    results = merge_trees(git, pairs)

    assert results == [merged_alone(git, ours, theirs) for ours, theirs in pairs]
    clean, content, rename_delete, add_add, file_directory, landed, same, odd = results
    assert clean.clean and not clean.conflicts
    assert content == MergeResult(False, content.tree, ("a",))
    assert not rename_delete.clean and rename_delete.conflicts == ("b",)
    assert add_add == MergeResult(False, add_add.tree, ("n",))
    assert not file_directory.clean and file_directory.conflicts
    assert odd.conflicts == ("new\nline", "sp ace\udcff")
    # Its changes are in its parent already: merging changes nothing there.
    assert landed == MergeResult(True, git.run("rev-parse", f"{ids['landed']}^{{tree}}"))
    assert same == MergeResult(True, git.run("rev-parse", f"{ids['x']}^{{tree}}"))


@NEEDS_STDIN
def test_many_pairs_take_one_process(
    merges: tuple[Git, dict[str, str]], git_calls: list[GitCall]
) -> None:
    git, ids = merges
    pairs = [(ids[ours], ids[theirs]) for ours, theirs in KINDS] * 25

    results = merge_trees(git, pairs)

    assert len(results) == 200
    assert results[:8] * 25 == results  # a pair repeated gives the same result
    assert [call[:2] for call in git_calls] == [["merge-tree", "--write-tree"]]


@NEEDS_STDIN
def test_a_pair_git_cant_merge_gets_its_error_and_the_rest_their_results(
    merges: tuple[Git, dict[str, str]], git_calls: list[GitCall]
) -> None:
    git, ids = merges
    tree = git.run("rev-parse", "main^{tree}")
    git_calls.clear()
    missing = "1" * 40
    pairs = [
        (ids["main"], missing),
        (ids["x"], ids["y"]),
        (ids["main"], ids["unrelated"]),
        (ids["ours"], ids["theirs"]),
        (tree, ids["main"]),
        (ids["landed"], ids["x"]),
    ]

    results = merge_trees(git, pairs)

    # git stops at a pair it can't merge: the rest go to another process.
    assert len(git_calls) == 4
    good = [1, 3, 5]
    assert [results[i] for i in good] == [merged_alone(git, *pairs[i]) for i in good]
    errors = [result.error for result in results]
    assert isinstance(errors[0], UnknownRevisionError)
    assert f"{missing} - not something we can merge" in errors[0].stderr
    assert isinstance(errors[2], GitError)
    assert "refusing to merge unrelated histories" in errors[2].stderr
    assert isinstance(errors[4], WrongTypeError)
    for i in (0, 2, 4):
        assert results[i] == MergeResult(None, None, (), errors[i])


@NEEDS_STDIN
@pytest.mark.parametrize("where", [0, 1])
@pytest.mark.parametrize(
    ("ours", "theirs", "error", "says"),
    [
        ("lost-blob-1", "lost-blob-2", GitError, "^fatal: unable to read blob object "),
        ("main", "lost-tree", GitError, "^error: Could not read "),
        ("lost-subtree-1", "lost-subtree-2", GitError, r"^fatal: unable to read tree \("),
        ("lost-renamed", "edited", GitError, "^fatal: unable to read [0-9a-f]+$"),
        ("x", "lost-parent", GitError, "^error: Could not read "),
        ("corrupt-1", "corrupt-2", CorruptObjectError, r"^fatal: loose object \S+ .* is corrupt$"),
    ],
)
def test_a_pair_whose_objects_git_cant_read_gets_its_error_wherever_it_is(
    merges: tuple[Git, dict[str, str]],
    git_calls: list[GitCall],
    where: int,
    ours: str,
    theirs: str,
    error: type[GitError],
    says: str,
) -> None:
    git, ids = merges
    good = [(ids["x"], ids["y"]), (ids["ours"], ids["theirs"])]
    pairs = [*good]
    pairs.insert(where, (ids[ours], ids[theirs]))

    results = merge_trees(git, pairs)

    # The process that stopped at it, and one for the pairs after it.
    assert len(git_calls) == 2
    failed = results.pop(where).error
    assert failed is not None and type(failed) is error
    assert re.search(says, failed.stderr, re.MULTILINE)
    assert results == [merged_alone(git, *pair) for pair in good]


@pytest.mark.parametrize(
    "batch",
    [
        pytest.param(True, marks=NEEDS_STDIN, id="batch"),
        pytest.param(False, marks=NEEDS_WRITE_TREE, id="one-at-a-time"),
    ],
)
@pytest.mark.parametrize("followed", [False, True])
@pytest.mark.parametrize(
    ("theirs", "error", "says"),
    [
        ("missing", UnknownRevisionError, " - not something we can merge"),
        ("tree", WrongTypeError, "expected commit type"),
        ("unrelated", GitError, "^fatal: refusing to merge unrelated histories$"),
    ],
)
def test_a_pair_git_cant_merge_first_gets_its_error(
    merges: tuple[Git, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    batch: bool,
    followed: bool,
    theirs: str,
    error: type[GitError],
    says: str,
) -> None:
    git, ids = merges
    ids["missing"] = "1" * 40
    ids["tree"] = git.run("rev-parse", "main^{tree}")
    if not batch:
        monkeypatch.setattr(git_client.merging, "_STDIN", "--no-such-option")
    good = [(ids["x"], ids["y"])] if followed else []

    first, *rest = merge_trees(git, [(ids["main"], ids[theirs]), *good])

    assert first.error is not None and type(first.error) is error
    assert re.search(says, first.error.stderr, re.MULTILINE)
    assert first == MergeResult(None, None, (), first.error)
    assert rest == [merged_alone(git, *pair) for pair in good]


def test_a_failure_not_about_one_pair_is_raised_even_after_a_merge() -> None:
    git = FakeGit()
    stderr = (
        "error: insufficient permission for adding an object to repository database"
        " .git/objects\nfatal: failure to merge\n"
    )  # what git says when it can't write the second merge's tree
    git.respond(["merge-tree"], stdout=f"1\0{'a' * 40}\0\0", stderr=stderr, returncode=128)

    with pytest.raises(GitError, match="insufficient permission"):
        merge_trees(git, [("b" * 40, "c" * 40)] * 3)

    assert len(git.calls) == 1


@NEEDS_WRITE_TREE
@NEEDS_STDIN
def test_an_object_a_partial_clone_cant_fetch_is_raised_at_once(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    head = repo.run("rev-parse", "HEAD")
    ids = {
        "ours": commit(repo, head, {b"a": b"ours\n"}),
        "theirs": commit(repo, head, {b"a": b"theirs\n"}),
        "x": commit(repo, head, {b"x": b"x\n"}),
        "y": commit(repo, head, {b"y": b"y\n"}),
    }
    for name, object_id in ids.items():
        repo.run("update-ref", f"refs/heads/{name}", object_id)
    repo.run("config", "uploadpack.allowFilter", "true")
    git, origin = repo, Path(repo.directory)
    clone = tmp_path / "clone"
    git.run("clone", "--quiet", "--filter=blob:none", origin.as_uri(), str(clone))
    origin.rename(tmp_path / "gone")  # as a remote that can't be reached
    git_calls.clear()
    # The second pair's merge reads blobs the clone doesn't have.
    pairs = [(ids["x"], ids["y"]), (ids["ours"], ids["theirs"]), (ids["x"], ids["y"])]

    with pytest.raises(MissingObjectError, match="could not fetch"):
        merge_trees(Git(clone), pairs)

    # Not a fetch, and its wait, for each pair left: each would fail the same way.
    assert len(git_calls) == 1


def test_without_stdin_each_pair_takes_a_process_of_its_own_and_gets_the_same_results(
    merges: tuple[Git, dict[str, str]],
    git_calls: list[GitCall],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git, ids = merges
    tree = git.run("rev-parse", "main^{tree}")
    pairs = [(ids[ours], ids[theirs]) for ours, theirs in KINDS]
    pairs += [(ids["main"], "1" * 40), (ids["main"], ids["unrelated"]), (tree, ids["main"])]
    # As git 2.38 does --stdin, which it doesn't know.
    monkeypatch.setattr(git_client.merging, "_STDIN", "--no-such-option")
    git_calls.clear()

    results = merge_trees(git, pairs)

    # The batch, refused, then one for each pair.
    assert len(git_calls) == 1 + len(pairs)
    good = len(KINDS)
    assert results[:good] == [merged_alone(git, *pair) for pair in pairs[:good]]
    missing, unrelated, wrong_type = (result.error for result in results[good:])
    assert isinstance(missing, UnknownRevisionError)
    assert unrelated is not None and "refusing to merge unrelated histories" in unrelated.stderr
    assert isinstance(wrong_type, WrongTypeError)
    assert all(result.clean is None for result in results[good:])


def test_without_write_tree_it_says_which_git_it_needs(
    merges: tuple[Git, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    git, ids = merges
    if GIT_VERSION >= (2, 38):
        # As git 2.37 does --write-tree, which its merge-tree doesn't know.
        monkeypatch.setattr(git_client.merging, "_STDIN", "--no-such-option")
        monkeypatch.setattr(git_client.merging, "_WRITE_TREE", "--no-such-option")

    with pytest.raises(GitError, match=r"merge_trees needs git 2\.38 or newer"):
        merge_trees(git, [(ids["x"], ids["y"])])


@NEEDS_WRITE_TREE
def test_conflicted_paths_are_from_the_clients_directory(
    merges: tuple[Git, dict[str, str]],
) -> None:
    git, ids = merges
    (Path(git.directory) / "sub").mkdir()

    [result] = merge_trees(Git(Path(git.directory) / "sub"), [(ids["ours"], ids["theirs"])])

    assert result.conflicts == ("../a",)


@pytest.mark.parametrize(
    "name",
    ["-x", "--stdin", "main", "a" * 39, "A" * 40, "a" * 40 + "\n", "a" * 20 + " " + "a" * 19],
)
def test_anything_but_a_full_object_id_is_refused_before_git_starts(
    repo: Git, name: str, git_calls: list[GitCall]
) -> None:
    head = repo.run("rev-parse", "HEAD")
    git_calls.clear()

    with pytest.raises(ValueError, match="full object id"):
        merge_trees(repo, [(head, head), (head, name)])

    assert git_calls == []


@pytest.mark.usefixtures("outside_any_repository")
def test_outside_a_repository_it_raises() -> None:
    with pytest.raises(NotARepositoryError):
        merge_trees(Git("."), [("a" * 40, "b" * 40)] * 3)


def test_a_read_only_client_is_refused(repo: Git, git_calls: list[GitCall]) -> None:
    head = repo.run("rev-parse", "HEAD")
    git_calls.clear()

    with pytest.raises(ReadOnlyError):
        merge_trees(repo.read_only(), [(head, head)])  # pyright: ignore[reportArgumentType]

    assert git_calls == []


def test_no_pairs_start_no_git(repo: Git, git_calls: list[GitCall]) -> None:
    assert merge_trees(repo, []) == []
    assert git_calls == []


# Output git never gives, as a FakeGit can: each is git's fault, so it raises.
TREE = "a" * 40


@pytest.mark.parametrize(
    ("stdout", "returncode", "says"),
    [
        (f"1\0{TREE}\0\0", 0, "merge-tree gave 1 results for 2 pairs"),
        (f"1\0{TREE}\0\0" + f"1\0{TREE}\0", 0, "merge-tree gave 1 results for 2 pairs"),
        (f"2\0{TREE}\0\0" * 2, 0, "merge-tree printed"),
        (f"1\0{TREE[1:]}\0\0" * 2, 0, "merge-tree printed"),
        ("1\0not a tree\0\0", 128, "merge-tree printed"),
        (f"1\0{TREE}\0\0" * 2, 128, "merge-tree gave 2 results for 2 pairs"),
    ],
    ids=[
        "too few",
        "one unfinished",
        "bad status",
        "short tree",
        "failed with garbage",
        "failed after every pair",
    ],
)
def test_output_git_never_gives_raises_with_gits_status_and_stderr(
    stdout: str, returncode: int, says: str
) -> None:
    git = FakeGit()
    git.respond(
        ["merge-tree"], stdout=stdout, stderr="warning: what git said\n", returncode=returncode
    )

    with pytest.raises(GitError, match=says) as raised:
        merge_trees(git, [("b" * 40, "c" * 40)] * 2)

    assert raised.value.returncode == returncode
    assert raised.value.stderr == "warning: what git said\n"
