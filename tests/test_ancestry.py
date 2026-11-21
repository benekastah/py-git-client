import itertools
import random
import stat
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from git_client import CatFile, Git, NotARepositoryError, git_paths_and_head, is_ancestor, refs
from git_client import ancestry as ancestry_module
from git_client.testing import GitCall

# A commit: its parents (earlier commits' indices) and, optionally, its committer date.
Commits = Sequence[Sequence[int]]


def make_commits(directory: Path, parents: Commits, dates: Sequence[int] = ()) -> list[str]:
    """A repository at `directory` holding commit i (in the order given) on refs/heads/c<i>,
    with the parents given, and the date given (else one a second after the last), made with
    one `git fast-import`. Returns their ids, in order."""
    Git(directory.parent).run("init", "--quiet", str(directory))
    git = Git(directory)
    commits = [(f"c{i}", commit_parents) for i, commit_parents in enumerate(parents)]
    git.run_bytes("fast-import", "--quiet", input=fast_import_stream(commits, dates))
    found = refs(git, "refs/heads/")
    return [found[f"refs/heads/c{i}"].object for i in range(len(parents))]


def fast_import_stream(
    commits: Sequence[tuple[str, Sequence[int]]], dates: Sequence[int] = ()
) -> bytes:
    """What fast-import makes the commits of: each (on refs/heads/<its branch>, which a later
    commit can move on) with its parents (earlier commits' indices), and its date (or one a
    second after the last)."""
    stream: list[bytes] = []
    for i, (branch, parents) in enumerate(commits):
        date = dates[i] if dates else 1_000_000_000 + i
        message = f"c{i}\n".encode()
        stream.append(
            b"commit refs/heads/%s\nmark :%d\ncommitter T <t@example.com> %d +0000\ndata %d\n%s"
            % (branch.encode(), i + 1, date, len(message), message)
        )
        for which, parent in enumerate(parents):
            stream.append(b"%s :%d\n" % (b"merge" if which else b"from", parent + 1))
        stream.append(b"\n")
    return b"".join(stream)


def merge_base_says(git: Git, ancestor: str, descendant: str) -> bool:
    """What git-stack takes `merge-base --is-ancestor` to say: any failure is no."""
    return git.succeeds("merge-base", "--is-ancestor", ancestor, descendant)


def random_dag(rng: random.Random, size: int) -> tuple[list[list[int]], list[int]]:
    """A random history of `size` commits: roots now and then, forks (any earlier commit can
    be a parent), merges of two or three parents, and dates that skew (a commit can be older
    than its parents), which a walk ordered by date mustn't take as true."""
    parents: list[list[int]] = []
    for i in range(size):
        kind = rng.random()
        if i == 0 or kind < 0.08:
            parents.append([])
        elif kind < 0.35 and i > 2:
            parents.append(rng.sample(range(i), rng.choice([2, 2, 3])))
        else:
            # Mostly a recent commit, as most histories are, sometimes an old one.
            parents.append([max(0, i - 1 - int(rng.expovariate(0.5)))])
    dates = [1_000_000_000 + i * 10 + rng.choice([0, 0, 0, -500, 500]) for i in range(size)]
    return parents, dates


def reachable(parents: Commits) -> list[set[int]]:
    """Each commit's ancestors, itself included, by index: the oracle the random histories'
    pairs are checked against, as `merge-base` agrees on a sample of them."""
    reach: list[set[int]] = []
    for i, commit_parents in enumerate(parents):
        reach.append({i}.union(*(reach[p] for p in commit_parents)))
    return reach


@pytest.mark.parametrize("seed", range(6))
def test_answers_as_merge_base_does_in_random_histories(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    parents, dates = random_dag(rng, 100)
    commits = make_commits(tmp_path / "repo", parents, dates)
    git = Git(tmp_path / "repo")
    reach = reachable(parents)
    # Every pair in one call, so pairs can get in each other's way if they would.
    every_pair = [(a, d) for a in range(len(commits)) for d in range(len(commits))]

    answers = is_ancestor(git, *((commits[a], commits[d]) for a, d in every_pair))

    assert answers == [a in reach[d] for a, d in every_pair]
    # The oracle is git's: merge-base, a process each, on a sample.
    for a, d in rng.sample(every_pair, 20):
        assert merge_base_says(git, commits[a], commits[d]) == (a in reach[d])


# A history with a merge, and a criss-cross: 0-1-2-3 (main), 1-4-5 (topic), 6 merges 3 and 5;
# 7 and 8 each merge 2 and 4 (criss-cross), and 9 is an unrelated root, with 10 on it.
SHAPES: Commits = [[], [0], [1], [2], [1], [4], [3, 5], [2, 4], [4, 2], [], [9]]


@pytest.fixture
def shapes(tmp_path: Path) -> tuple[Git, list[str]]:
    commits = make_commits(tmp_path / "repo", SHAPES)
    return Git(tmp_path / "repo"), commits


def test_a_merge_reaches_both_its_parents_histories(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes

    answers = is_ancestor(git, (c[5], c[6]), (c[4], c[6]), (c[3], c[6]), (c[6], c[5]))

    assert answers == [True, True, True, False]


def test_criss_cross_merges(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes

    answers = is_ancestor(git, (c[2], c[8]), (c[4], c[7]), (c[7], c[8]), (c[8], c[7]))

    assert answers == [True, True, False, False]


def test_a_commit_is_its_own_ancestor(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes

    assert is_ancestor(git, (c[6], c[6]), (c[0], c[0])) == [True, True]


def test_unrelated_histories(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes

    answers = is_ancestor(git, (c[9], c[6]), (c[0], c[10]), (c[9], c[10]))

    assert answers == [False, False, True]


def test_the_descendant_of_a_reversed_pair_isnt_an_ancestor(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes

    assert is_ancestor(git, (c[3], c[0]), (c[0], c[3])) == [False, True]


def test_a_pair_repeated_gets_the_same_answer(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes

    answers = is_ancestor(git, *[(c[0], c[6]), (c[6], c[0])] * 3)

    assert answers == [True, False] * 3


def test_a_stack_whose_ancestors_are_each_others(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes
    # Each pair's ancestor is the next pair's descendant, as in a stack of branches.
    stack = [(c[2], c[3]), (c[1], c[2]), (c[0], c[1]), (c[0], c[3]), (c[1], c[3])]

    assert is_ancestor(git, *stack) == [True] * 5


def test_an_ancestor_below_another_pairs_ancestor(tmp_path: Path) -> None:
    # 0-1-2 (main), and 3 forks from 1: 0 is an ancestor of 3 through 1, which is an ancestor
    # of the other pair's ancestor, 2.
    git_dir = tmp_path / "repo"
    c = make_commits(git_dir, [[], [0], [1], [1]])

    answers = is_ancestor(Git(git_dir), (c[2], c[2]), (c[0], c[3]), (c[2], c[3]))

    assert answers == [True, True, False]


def test_a_missing_object_is_no_ones_ancestor_or_descendant(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes
    missing = "1" * 40

    answers = is_ancestor(git, (missing, c[6]), (c[0], missing), (missing, missing))

    assert answers == [False, False, False]


@pytest.mark.parametrize("name", ["c6", "refs/heads/c6", "HEAD", "-h", "--all", "", "a\nb"])
def test_anything_but_an_object_id_is_false_and_never_an_option(
    shapes: tuple[Git, list[str]], name: str
) -> None:
    git, c = shapes

    assert is_ancestor(git, (name, c[6]), (c[0], name)) == [False, False]


def test_a_parent_header_out_of_place_is_no_parent(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes
    tree = git.run("rev-parse", f"{c[0]}^{{tree}}")
    # git takes a commit's parents only from the lines right after its tree, as fsck would
    # insist, but a commit fetched unchecked can have another (and a second committer).
    who = "T <t@example.com> 1000000000 +0000"
    content = (
        f"tree {tree}\nparent {c[0]}\nauthor {who}\ncommitter {who}\n"
        f"parent {c[9]}\ncommitter {who}\n\nodd\n"
    )
    odd = git.run("hash-object", "--literally", "-t", "commit", "-w", "--stdin", input=content)
    # With no author, the committer gives no date: it still has its parent.
    authorless = f"tree {tree}\nparent {odd}\ncommitter {who}\n\nno author\n"
    on_odd = git.run(
        "hash-object", "--literally", "-t", "commit", "-w", "--stdin", input=authorless
    )
    pairs = [(c[9], odd), (c[0], odd), (c[0], on_odd)]

    assert is_ancestor(git, *pairs) == [merge_base_says(git, a, d) for a, d in pairs]
    assert merge_base_says(git, c[0], odd)


@pytest.mark.parametrize(
    "parent",
    ["{upper}", "{short}", "{commit} x", "\u00e9" * 40, "{tree}"],
    ids=["uppercase", "short", "trailing", "not-hex", "tree"],
)
def test_an_odd_parent_line_answers_as_merge_base_does(
    shapes: tuple[Git, list[str]], parent: str
) -> None:
    git, c = shapes
    tree = git.run("rev-parse", f"{c[0]}^{{tree}}")
    who = "T <t@example.com> 1000000000 +0000"
    # git reads an id's hex in either case, and fails a commit with a parent it can't read.
    line = parent.format(upper=c[0].upper(), short=c[0][:39], commit=c[0], tree=tree)
    content = f"tree {tree}\nparent {line}\nauthor {who}\ncommitter {who}\n\nodd\n"
    odd = git.run("hash-object", "--literally", "-t", "commit", "-w", "--stdin", input=content)
    # Read as an endpoint (which cat-file parses as it peels it) and as a parent (read as it is).
    child = git.run("commit-tree", "-p", odd, "-m", "child", tree)
    pairs = [(c[0], odd), (odd, odd), (c[9], odd), (c[0], child), (odd, child)]

    assert is_ancestor(git, *pairs) == [merge_base_says(git, a, d) for a, d in pairs]


def test_a_tag_a_corrupt_commit_names_as_parent_is_still_its_commit(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes
    git.run("tag", "-a", "-m", "tag", "t", c[1])
    tag = git.run("rev-parse", "refs/tags/t")
    tree = git.run("rev-parse", f"{c[0]}^{{tree}}")
    who = "T <t@example.com> 1000000000 +0000"
    content = f"tree {tree}\nparent {tag}\nauthor {who}\ncommitter {who}\n\nodd\n"
    odd = git.run("hash-object", "--literally", "-t", "commit", "-w", "--stdin", input=content)
    # Once cat-file has parsed odd, it takes the tag's id for a commit's, and fails to peel it.
    pairs = [(c[0], odd), (tag, c[3])]

    assert is_ancestor(git, *pairs) == [merge_base_says(git, a, d) for a, d in pairs]
    assert merge_base_says(git, tag, c[3])


def test_an_abbreviated_id_is_false_in_a_sha256_repository(tmp_path: Path) -> None:
    # 40 hex digits: an abbreviation there, which cat-file would resolve.
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", "repo")
    git = Git(tmp_path / "repo")
    git.run("commit", "--quiet", "--allow-empty", "-m", "first")
    commit = git.run("rev-parse", "HEAD")

    assert is_ancestor(git, (commit[:40], commit), (commit, commit)) == [False, True]


def test_a_tag_is_its_commit_and_a_tree_no_commit(shapes: tuple[Git, list[str]]) -> None:
    git, c = shapes
    git.run("tag", "-a", "-m", "tag", "t", c[1])
    tag = git.run("rev-parse", "refs/tags/t")
    tree = git.run("rev-parse", f"{c[1]}^{{tree}}")

    answers = is_ancestor(git, (tag, c[3]), (c[0], tag), (tree, c[3]), (c[1], tree))

    assert answers == [merge_base_says(git, a, d) for a, d in [(tag, c[3]), (c[0], tag)]] + [
        False,
        False,
    ]
    assert answers[:2] == [True, True]


def test_no_pairs_start_no_git(git_calls: list[GitCall], shapes: tuple[Git, list[str]]) -> None:
    git, _ = shapes
    started = len(git_calls)

    assert is_ancestor(git) == []
    assert git_calls[started:] == []


def test_a_hundred_pairs_take_as_many_processes_as_one(
    git_calls: list[GitCall], shapes: tuple[Git, list[str]]
) -> None:
    git, c = shapes
    git = Git(git.directory)  # nothing known of it yet
    started = len(git_calls)
    pairs = [(c[i % 11], c[(i * 7) % 11]) for i in range(100)]

    is_ancestor(git, *pairs)
    first = [call[0] for call in git_calls[started:]]
    is_ancestor(git, *pairs[:1])
    second = [call[0] for call in git_calls[started + len(first) :]]

    # Where the shallow and grafts files are, asked once per client; one cat-file per call.
    assert first == ["rev-parse", "cat-file"]
    assert second == ["cat-file"]


def test_a_tools_own_cat_file_and_paths_start_no_process(
    git_calls: list[GitCall], shapes: tuple[Git, list[str]]
) -> None:
    git, c = shapes
    git = Git(git.directory)
    # What the tool asks anyway, asking for the shallow and grafts files too.
    git_paths_and_head(git, "HEAD", "shallow", "info/grafts")
    with CatFile(git) as cat_file:
        cat_file.resolve("HEAD")
        started = len(git_calls)

        answers = is_ancestor(git, (c[0], c[6]), (c[6], c[0]), cat_file=cat_file)

    assert answers == [True, False]
    assert git_calls[started:] == []


def test_threads_can_share_a_cat_file(tmp_path: Path) -> None:
    parents, dates = random_dag(random.Random(0), 60)
    commits = make_commits(tmp_path / "repo", parents, dates)
    git = Git(tmp_path / "repo")
    reach = reachable(parents)
    pairs = [(a, d) for a in range(len(commits)) for d in range(len(commits))]
    expected = [a in reach[d] for a, d in pairs]
    results: list[list[bool]] = []
    start = threading.Barrier(2)

    def answer(cat_file: CatFile, order: list[tuple[int, int]]) -> None:
        start.wait()
        answers = is_ancestor(git, *((commits[a], commits[d]) for a, d in order), cat_file=cat_file)
        answered = dict(zip(order, answers, strict=True))
        results.append([answered[pair] for pair in pairs])

    with CatFile(git) as cat_file:
        threads = [
            threading.Thread(target=answer, args=(cat_file, order))
            for order in (pairs, pairs[::-1])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert results == [expected, expected]


def test_it_raises_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        is_ancestor(Git(tmp_path), ("1" * 40, "2" * 40))


def test_a_shallow_clones_cut_history(git_calls: list[GitCall], tmp_path: Path) -> None:
    c = make_commits(tmp_path / "origin", [[], [0], [1], [2], [3], [1]])
    origin = (tmp_path / "origin").as_uri()
    Git(tmp_path).run(
        "clone", "--quiet", "--depth=1", "--branch=c5", origin, str(tmp_path / "clone")
    )  # fmt: skip
    git = Git(tmp_path / "clone")
    git.run("fetch", "--quiet", "--depth=5", "origin", "c4")
    # c5 stays cut from its parent, c1, though the fetch of c4 brought c1.
    pairs = [(a, d) for a in c for d in c]
    started = len(git_calls)

    answers = is_ancestor(git, *pairs)

    assert [call[0] for call in git_calls[started:]] == ["rev-parse", "cat-file"]
    assert answers == [merge_base_says(git, a, d) for a, d in pairs]
    assert answers[pairs.index((c[1], c[5]))] is False
    assert answers[pairs.index((c[1], c[4]))] is True


def test_a_shallow_file_named_by_the_environment_cuts_history(
    shapes: tuple[Git, list[str]], tmp_path: Path
) -> None:
    git, c = shapes
    # Where git (receive-pack, say) keeps a shallow list elsewhere: c3 cut from c2.
    shallow = tmp_path / "shallow"
    shallow.write_text(f"{c[3]}\n", encoding="utf-8")
    git = Git(git.directory, env={"GIT_SHALLOW_FILE": str(shallow)})
    pairs = [(c[2], c[3]), (c[0], c[6]), (c[0], c[2])]

    answers = is_ancestor(git, *pairs)

    assert answers == [merge_base_says(git, a, d) for a, d in pairs]
    assert answers[0] is False


def test_replace_refs_change_history_as_they_do_for_merge_base(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes
    # c2 replaced by a root: c0 and c1 are no longer c3's ancestors.
    root = git.run("commit-tree", "-m", "root", f"{c[2]}^{{tree}}")
    git.run("replace", c[2], root)
    pairs = [(c[1], c[3]), (c[2], c[3]), (c[1], c[6]), (c[0], c[8])]

    answers = is_ancestor(git, *pairs)

    assert answers == [merge_base_says(git, a, d) for a, d in pairs]
    assert answers[0] is False


def test_grafts_change_history_as_they_do_for_merge_base(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes
    # c3 grafted onto c9, from the unrelated history.
    grafts = Path(git.directory) / ".git" / "info" / "grafts"
    grafts.parent.mkdir(exist_ok=True)
    grafts.write_text(f"{c[3]} {c[9]}\n", encoding="utf-8")
    pairs = [(c[2], c[3]), (c[9], c[3]), (c[0], c[6]), (c[9], c[6])]

    answers = is_ancestor(git, *pairs, ("c0", c[3]))

    assert answers == [merge_base_says(git, a, d) for a, d in pairs] + [False]


def test_a_walk_too_long_asks_merge_base(
    monkeypatch: pytest.MonkeyPatch, git_calls: list[GitCall], shapes: tuple[Git, list[str]]
) -> None:
    git, c = shapes
    monkeypatch.setattr(ancestry_module, "_WALK_LIMIT", 2)
    started = len(git_calls)

    answers = is_ancestor(git, (c[0], c[6]), (c[9], c[6]), (c[5], c[6]))

    assert answers == [True, False, True]
    merge_bases = [call for call in git_calls[started:] if call[0] == "merge-base"]
    assert merge_bases == [
        ["merge-base", "--is-ancestor", c[0], c[6]],
        ["merge-base", "--is-ancestor", c[9], c[6]],
    ]


def test_a_missing_commit_in_the_walk_answers_as_merge_base_does(
    shapes: tuple[Git, list[str]],
) -> None:
    git, c = shapes
    # fast-import packs them: unpack, to delete c4's object.
    unpack(git)
    remove_object(git, c[4])
    pairs = [(c[1], c[5]), (c[1], c[6]), (c[0], c[3])]

    answers = is_ancestor(git, *pairs)

    assert answers == [merge_base_says(git, a, d) for a, d in pairs]
    assert answers[2] is True


def test_a_damaged_commit_is_no_ones_ancestor_or_descendant(damaged_repo: Git) -> None:
    head = damaged_repo.run("rev-parse", "HEAD")

    assert is_ancestor(damaged_repo, (head, head)) == [False]
    assert not merge_base_says(damaged_repo, head, head)


def unpack(git: Git) -> None:
    """Make every object in the repository's packs a loose object."""
    pack_dir = Path(git.directory) / ".git" / "objects" / "pack"
    for pack in pack_dir.glob("*.pack"):
        moved = pack.parent.parent / pack.name
        pack.rename(moved)
        pack.with_suffix(".idx").unlink()
        git.run_bytes("unpack-objects", "-q", input=moved.read_bytes())
        moved.unlink()


def remove_object(git: Git, object_id: str) -> None:
    path = Path(git.directory) / ".git" / "objects" / object_id[:2] / object_id[2:]
    path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.unlink()


def make_history_with_stack(directory: Path, length: int) -> list[str]:
    """A repository at `directory` with `length` commits on main, then a stack of 50
    branches (refs/heads/s<i>) on its tip, each 3 commits past the one below: returns main's
    tip, then the stack's, from the bottom. Made with one fast-import."""
    Git(directory.parent).run("init", "--quiet", str(directory))
    git = Git(directory)
    commits = [("main", [i - 1] if i else []) for i in range(length)]
    commits += [(f"s{j // 3}", [length + j - 1]) for j in range(150)]
    git.run_bytes("fast-import", "--quiet", input=fast_import_stream(commits))
    found = refs(git, "refs/heads/")
    return [found["refs/heads/main"].object] + [found[f"refs/heads/s{i}"].object for i in range(50)]


def counting_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The object ids every CatFile reads, while `monkeypatch` holds."""
    read = CatFile.read
    reads: list[str] = []

    def counting(self: CatFile, object_id: str, *args: Any, **kwargs: Any) -> tuple[str, bytes]:
        reads.append(object_id)
        return read(self, object_id, *args, **kwargs)

    monkeypatch.setattr(CatFile, "read", counting)
    return reads


def test_the_work_is_bounded_by_the_stack_not_the_history(
    git_calls: list[GitCall], tmp_path: Path
) -> None:
    reads: dict[tuple[int, bool], int] = {}
    for length in (1_000, 10_000):
        directory = tmp_path / str(length)
        stack = make_history_with_stack(directory, length)
        git = Git(directory)
        # Each branch on the one below (main's tip first), each on main, and the first 49
        # reversed.
        pairs = list(itertools.pairwise(stack))
        pairs += [(stack[0], tip) for tip in stack[1:]]
        pairs += [(d, a) for a, d in pairs[:49]]
        for graph in (False, True):
            if graph:
                git.run("commit-graph", "write", "--reachable")
            started = len(git_calls)
            with pytest.MonkeyPatch.context() as patch:
                read = counting_reads(patch)
                answers = is_ancestor(git, *pairs)
            assert answers == [True] * 100 + [False] * 49
            # Where the shallow and grafts files are is asked only the first time.
            processes = [call[0] for call in git_calls[started:]]
            assert processes == (["cat-file"] if graph else ["rev-parse", "cat-file"])
            reads[length, graph] = len(read)
    # The same commits read, however long the history, with a commit-graph or not: the
    # stack's 150, and a few below.
    assert len(set(reads.values())) == 1, reads
    assert next(iter(reads.values())) < 300
