from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from git_client import (
    Git,
    GitError,
    Revisions,
    Signature,
    TreeEntry,
    UnknownRevisionError,
    WrongTypeError,
    commits,
    hash_blobs,
    tree_entries,
    write_commit,
    write_trees,
)
from git_client.testing import FakeGit, GitCall

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MISSING = "1" * 40


def blob(git: Git, content: bytes) -> str:
    [found] = hash_blobs(git, [content])
    return found


def test_write_trees_writes_every_tree_with_one_mktree(repo: Git, git_calls: list[GitCall]) -> None:
    x, y = blob(repo, b"x\n"), blob(repo, b"y\n")
    [inner] = write_trees(repo, [[TreeEntry("100755", "blob", y, "tool")]])
    git_calls.clear()

    trees = [
        # Not in git's order, which mktree sorts them into; a name with a newline (-z).
        [TreeEntry("100644", "blob", x, "z\nnewline"), TreeEntry("040000", "tree", inner, "d")],
        [],
        [TreeEntry("120000", "blob", x, "link"), TreeEntry("160000", "commit", MISSING, "sub")],
    ]
    first, empty, last = write_trees(repo, trees)

    assert [call[call.index("mktree") :] for call in git_calls] == [["mktree", "-z", "--batch"]]
    assert tree_entries(repo, first) == sorted(trees[0], key=lambda e: e.path)
    assert empty == EMPTY_TREE
    assert tree_entries(repo, last) == trees[2]
    assert tree_entries(repo, first, recursive=True)[0] == TreeEntry("100755", "blob", y, "d/tool")


def test_write_trees_of_nothing_starts_no_git(repo: Git, git_calls: list[GitCall]) -> None:
    assert write_trees(repo, []) == []
    assert git_calls == []


def test_write_trees_writes_an_empty_tree_last(repo: Git) -> None:
    x = blob(repo, b"x\n")
    assert write_trees(repo, [[TreeEntry("100644", "blob", x, "a")], []])[1] == EMPTY_TREE


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        (TreeEntry("100644", "blob", "x", ""), "empty"),
        (TreeEntry("100644", "blob", "x", "a\0b"), "NUL"),
        (TreeEntry("100644 ", "blob", "x", "a"), "mode"),
        (TreeEntry("100644", "blob", "x\tb", "a"), "id"),
        (TreeEntry("10\x000644", "blob", "x", "a"), "mode"),
    ],
)
def test_write_trees_refuses_what_mktree_would_read_otherwise(
    repo: Git, git_calls: list[GitCall], entry: TreeEntry, says: str
) -> None:
    with pytest.raises(ValueError, match=says):
        write_trees(repo, [[entry]])
    assert git_calls == []


def test_write_trees_raises_what_git_refuses(repo: Git) -> None:
    x = blob(repo, b"x\n")
    with pytest.raises(GitError, match="contains slash"):
        write_trees(repo, [[TreeEntry("100644", "blob", x, "a/b")]])
    with pytest.raises(GitError, match="doesn't match mode type"):
        write_trees(repo, [[TreeEntry("040000", "blob", x, "a")]])
    with pytest.raises(GitError, match="is unavailable"):
        write_trees(
            repo,
            [[TreeEntry("100644", "blob", x, "ok")], [TreeEntry("100644", "blob", MISSING, "a")]],
        )


def test_write_trees_takes_missing_objects_when_asked(repo: Git) -> None:
    [tree] = write_trees(repo, [[TreeEntry("100644", "blob", MISSING, "a")]], missing=True)
    assert repo.run("cat-file", "-p", tree) == f"100644 blob {MISSING}\ta"


@pytest.mark.parametrize("stdout", [b"", f"{EMPTY_TREE}\n{EMPTY_TREE}\n".encode(), b"not an id\n"])
def test_write_trees_raises_for_output_mktree_doesnt_document(stdout: bytes) -> None:
    git = FakeGit()
    git.respond(["mktree"], stdout)
    with pytest.raises(GitError, match="mktree printed"):
        write_trees(git, [[TreeEntry("100644", "blob", MISSING, "a")]])


AUTHOR = Signature(
    "Ann Author",
    "ann@example.com",
    datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=5, minutes=30))),
)
COMMITTER = Signature(
    "Cy Committer",
    "cy@example.com",
    datetime(2022, 1, 2, 3, 4, 5, tzinfo=timezone(-timedelta(hours=7))),
)


def headers(git: Git, commit: str) -> tuple[list[str], bytes]:
    """The commit's header lines and its message, as git stores them."""
    raw = git.run_bytes("cat-file", "commit", commit)
    head, _, message = raw.partition(b"\n\n")
    return head.decode().split("\n"), message


def test_write_commit_writes_a_root_commit_with_identities_and_message_as_given(repo: Git) -> None:
    message = b"Subject\n\n  kept as it is, without a newline at its end  "
    commit = write_commit(repo, EMPTY_TREE, [], message, author=AUTHOR, committer=COMMITTER)

    lines, stored = headers(repo, commit)
    assert lines == [
        f"tree {EMPTY_TREE}",
        "author Ann Author <ann@example.com> 1614814567 +0530",
        "committer Cy Committer <cy@example.com> 1641117845 -0700",
    ]
    assert stored == message


def test_write_commit_gives_parents_in_order(repo: Git) -> None:
    first = repo.resolve("HEAD")
    assert first is not None
    second = write_commit(repo, EMPTY_TREE, [first], "second")
    merge = write_commit(repo, "HEAD^{tree}", [second, first], "merge\n")

    lines, stored = headers(repo, merge)
    assert lines[1:3] == [f"parent {second}", f"parent {first}"]
    assert stored == b"merge\n"


def test_write_commit_takes_identities_from_one_call_alone(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user's environment names someone else and a date; an identity given replaces it
    # whole, for that call alone.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Author")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "@1000000000 +0000")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "env@example.com")
    given = write_commit(repo, EMPTY_TREE, [], "m", author=AUTHOR)
    default = write_commit(repo, EMPTY_TREE, [], "m")

    author, committer = headers(repo, given)[0][1:3]
    assert author == "author Ann Author <ann@example.com> 1614814567 +0530"
    assert committer.startswith("committer Test <env@example.com> ")  # git's, from the env
    assert headers(repo, default)[0][1] == "author Env Author <test@example.com> 1000000000 +0000"
    assert repo.env == {}


def test_write_commit_encodes_a_str_message_as_utf8_whatever_the_config(repo: Git) -> None:
    # Which would otherwise label the UTF-8 bytes as Latin-1.
    repo.run("config", "i18n.commitEncoding", "ISO-8859-1")

    commit = write_commit(repo, EMPTY_TREE, [], "café ✓\n")

    lines, stored = headers(repo, commit)
    assert stored == "café ✓\n".encode()
    assert not any(line.startswith("encoding") for line in lines)


def test_write_commit_labels_bytes_by_the_config(repo: Git) -> None:
    repo.run("config", "i18n.commitEncoding", "ISO-8859-1")

    commit = write_commit(repo, EMPTY_TREE, [], b"caf\xe9\n")

    lines, stored = headers(repo, commit)
    assert stored == b"caf\xe9\n"
    assert "encoding ISO-8859-1" in lines
    assert repo.run("log", "-1", "--encoding=UTF-8", "--format=%s", commit) == "café"


def raw_commit(git: Git, date: str) -> str:
    """A commit whose author and committer have `date`, as git stores it, which git's own
    commands can't all write."""
    text = (
        f"tree {EMPTY_TREE}\n"
        f"author A <a@example.com> {date}\ncommitter C <c@example.com> {date}\n\nm\n"
    )
    return git.run("hash-object", "--literally", "-t", "commit", "-w", "--stdin", input=text)


@pytest.mark.parametrize(
    ("stored", "written"),
    [
        ("253402300800 +0000", "253402300800 +0000"),  # the year 10000, past a datetime's
        ("99999999999999 +0100", "99999999999999 +0100"),
        ("1700000000 +0130", "1700000000 +0130"),
        ("1700000000 -0000", "1700000000 +0000"),  # git reads -0000 as +0000
    ],
)
def test_write_commit_keeps_a_read_commits_dates(repo: Git, stored: str, written: str) -> None:
    [old] = commits(repo, Revisions(raw_commit(repo, stored)), max_count=1)

    copy = write_commit(repo, EMPTY_TREE, [], "m", author=old.author, committer=old.committer)

    assert headers(repo, copy)[0][1:3] == [
        f"author A <a@example.com> {written}",
        f"committer C <c@example.com> {written}",
    ]


def test_write_commit_takes_a_new_date_over_the_one_read(repo: Git) -> None:
    [old] = commits(repo, Revisions(raw_commit(repo, "1700000000 +0130")), max_count=1)
    author = replace(old.author, date=AUTHOR.date)

    copy = write_commit(repo, EMPTY_TREE, [], "m", author=author)

    assert headers(repo, copy)[0][1] == "author A <a@example.com> 1614814567 +0530"
    # The same instant, in another zone.
    assert old.author.date is not None
    author = replace(old.author, date=old.author.date.astimezone(UTC))
    copy = write_commit(repo, EMPTY_TREE, [], "m", author=author)
    assert headers(repo, copy)[0][1] == "author A <a@example.com> 1700000000 +0000"


@pytest.mark.parametrize(
    ("sign", "switch"), [(False, []), (True, ["-S"]), ("ABC123", ["-SABC123"])]
)
def test_write_commit_signs_when_asked(sign: bool | str, switch: list[str]) -> None:
    git = FakeGit()
    git.respond(["commit-tree"], f"{'2' * 40}\n")

    assert write_commit(git, EMPTY_TREE, ["p"], b"m", sign=sign) == "2" * 40
    assert git.commands == [["commit-tree", EMPTY_TREE, "-p", "p", *switch, "-F", "-"]]


def test_write_commit_gives_git_a_str_message_as_utf8() -> None:
    git = FakeGit()
    git.respond(["-c", "i18n.commitEncoding=UTF-8", "commit-tree"], f"{'2' * 40}\n")
    assert write_commit(git, EMPTY_TREE, [], "m") == "2" * 40


def test_write_commit_signs_with_git_signing_program(repo: Git, tmp_path: Path) -> None:
    # A fake gpg, in git's sh on every system: git reads its signature, and its status line.
    gpg = tmp_path / "fake-gpg"
    gpg.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" > "$0.args"\n'
        "cat > /dev/null\n"
        'printf "\\n[GNUPG:] SIG_CREATED D 1 8 00 0 F\\n" >&2\n'
        'printf "%s\\n" "-----BEGIN PGP SIGNATURE-----" fake "-----END PGP SIGNATURE-----"\n',
        encoding="utf-8",
    )
    gpg.chmod(0o755)
    repo.run("config", "gpg.program", gpg.as_posix())

    commit = write_commit(repo, EMPTY_TREE, [], "signed\n", sign="KEY1")

    assert headers(repo, commit)[0][3:5] == ["gpgsig -----BEGIN PGP SIGNATURE-----", " fake"]
    assert Path(f"{gpg}.args").read_text(encoding="utf-8").endswith(" KEY1\n")


def test_write_commit_leaves_signing_to_the_caller_whatever_the_config(repo: Git) -> None:
    # commit-tree doesn't read commit.gpgSign; were it to, this gpg would fail the commit.
    repo.run("config", "commit.gpgSign", "true")
    repo.run("config", "gpg.program", "no-such-gpg")
    commit = write_commit(repo, EMPTY_TREE, [], "m")
    assert not any(line.startswith("gpgsig") for line in headers(repo, commit)[0])


def test_write_commit_refuses_what_git_would_read_otherwise(
    repo: Git, git_calls: list[GitCall]
) -> None:
    naive = Signature("A", "a@example.com", datetime(2021, 1, 1))
    seconds = Signature(
        "A", "a@example.com", datetime(2021, 1, 1, tzinfo=timezone(timedelta(seconds=30)))
    )
    with pytest.raises(ValueError, match="option"):
        write_commit(repo, "-tree", [], "m")
    with pytest.raises(ValueError, match="time zone"):
        write_commit(repo, EMPTY_TREE, [], "m", author=naive)
    with pytest.raises(ValueError, match="whole minutes"):
        write_commit(repo, EMPTY_TREE, [], "m", committer=seconds)
    with pytest.raises(ValueError, match="key"):
        write_commit(repo, EMPTY_TREE, [], "m", sign="")
    with pytest.raises(ValueError, match="no date"):
        write_commit(repo, EMPTY_TREE, [], "m", author=Signature("A", "a@example.com", None))
    assert git_calls == []


def test_write_commit_raises_what_git_refuses(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        write_commit(repo, "no-such-tree", [], "m")
    with pytest.raises(UnknownRevisionError):
        write_commit(repo, EMPTY_TREE, ["no-such-parent"], "m")
    with pytest.raises(GitError, match="empty ident name"):
        write_commit(repo, EMPTY_TREE, [], "m", author=replace(AUTHOR, name=""))
    with pytest.raises(GitError, match="invalid date format"):  # git takes none before 1970
        write_commit(
            repo,
            EMPTY_TREE,
            [],
            "m",
            author=replace(AUTHOR, date=datetime(1969, 12, 31, tzinfo=UTC)),
        )
    with pytest.raises(GitError, match="NUL byte"):
        write_commit(repo, EMPTY_TREE, [], b"a\0b")
    with pytest.raises(WrongTypeError) as raised:
        write_commit(repo, "HEAD", [], "m")  # a commit for the tree
    assert raised.value.expected == "tree"
    with pytest.raises(WrongTypeError) as raised:
        write_commit(repo, EMPTY_TREE, [EMPTY_TREE], "m")  # a tree for a parent
    assert raised.value.expected == "commit"


@pytest.mark.parametrize(
    "stdout", [b"", b"not an id\n", f"{EMPTY_TREE}x".encode(), f"{EMPTY_TREE}\nmore\n".encode()]
)
def test_write_commit_raises_for_output_git_doesnt_document(stdout: bytes) -> None:
    git = FakeGit()
    git.respond(["commit-tree"], stdout)
    with pytest.raises(GitError, match="commit-tree printed"):
        write_commit(git, EMPTY_TREE, [], b"m")


def test_write_commit_refuses_a_nul_in_an_identity(repo: Git) -> None:
    # Python refuses it in a variable, which would end there.
    with pytest.raises(ValueError, match="null"):
        write_commit(repo, EMPTY_TREE, [], "m", author=replace(AUTHOR, name="A\0B"))
