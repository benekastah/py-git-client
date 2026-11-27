from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from git_client import (
    Commit,
    Git,
    ReadOnlyGit,
    Revisions,
    Signature,
    UnknownRevisionError,
    any_commit,
    commits,
    count,
)
from git_client.testing import GitCall

# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]


# When make_history's commits are made: so many seconds after this.
EPOCH = 1_600_000_000


def _at(when: int) -> dict[str, str]:
    """git's environment for a commit at `when` seconds after EPOCH."""
    return {"GIT_COMMITTER_DATE": f"{EPOCH + when} +0000"}


def _commit(git: Git, message: bytes, when: int, **env: str) -> None:
    """Commit what's staged with `message`, as it is, by git's committer at `when`."""
    git.run_bytes(
        "commit", "--quiet", "--cleanup=verbatim", "-F", "-", input=message,
        env={**_at(when), **env},
    )  # fmt: skip


def make_history(git: Git) -> None:
    """On make_repo's first commit (tagged `first`): `side`'s s1 and s3, which add src/c and
    src/d; main's `second`, by Other, which adds b, then a merge of side; and `copy`, from
    first, with s1's change picked. Side's are authored in a zone west of UTC. Each committed so
    many thousand seconds after EPOCH as its subject names (the merge 4, the pick 5), so the two
    lines' dates interleave."""
    git.run("tag", "first")
    git.run("switch", "--quiet", "-c", "side")
    (Path(git.directory) / "src").mkdir()
    for when, name in ((1000, "c"), (3000, "d")):
        (Path(git.directory) / "src" / name).write_bytes(b"x\n")
        git.run("add", "src")
        _commit(git, f"s{when // 1000}\n".encode(), when, GIT_AUTHOR_DATE="1700000000 -0730")
    git.run("switch", "--quiet", "main")
    (Path(git.directory) / "b").write_bytes(b"b\n")
    git.run("add", "b")
    author = {"GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "o@e"}
    _commit(git, "second\n\nbody ü\n".encode(), 2000, GIT_AUTHOR_DATE="1700000000 +0130", **author)
    git.run("merge", "--quiet", "--no-edit", "side", env=_at(4000))
    git.run("switch", "--quiet", "-c", "copy", "first")
    git.run("cherry-pick", "side~", env=_at(5000))
    git.run("switch", "--quiet", "main")


@pytest.fixture
def history(repo_from: RepoFrom) -> Git:
    return repo_from("history", make_history)


def subjects(found: list[Commit]) -> list[str]:
    return [commit.subject for commit in found]


def test_commits_lists_a_range_newest_first_with_what_git_says_of_each(history: Git) -> None:
    merge, second = commits(history.read_only(), Revisions("main", exclude="side"))

    assert merge.subject == "Merge branch 'side'"
    assert merge.parents == (second.id, history.run("rev-parse", "side"))
    assert second.id == history.run("rev-parse", "main^")
    assert second.parents == (history.run("rev-parse", "first"),)
    zone = timezone(timedelta(hours=1, minutes=30))
    assert second.author == Signature("Other", "o@e", datetime(2023, 11, 14, 23, 43, 20, 0, zone))
    assert second.author.date
    assert second.author.date.utcoffset() == timedelta(hours=1, minutes=30)
    assert second.committer == Signature(
        "Test", "test@example.com", datetime.fromtimestamp(EPOCH + 2000, UTC)
    )
    assert second.message is None
    [_, s1] = commits(history, Revisions("side", exclude="first"))
    west = timezone(-timedelta(hours=7, minutes=30))
    assert s1.author.date
    assert s1.author.date == datetime(2023, 11, 14, 14, 43, 20, 0, west)
    assert s1.author.date.utcoffset() == -timedelta(hours=7, minutes=30)


# Dates git prints as the commit records them, which fsck only warns of, and a datetime can't
# hold: a zone out of range or not 4 digits, no date at all, a year past 9999.
@pytest.mark.parametrize(
    "date",
    ["1700000000 +9999", "1700000000 +0060", "1700000000 +123456", "", "99999999999999 +0000"],
)
def test_a_date_a_datetime_cant_hold_is_none(history: Git, date: str) -> None:
    tree, first = history.run("rev-parse", "first^{tree}", "first").split()
    ident = f"A <a@e> {date}".rstrip()
    content = f"tree {tree}\nparent {first}\nauthor {ident}\ncommitter C <c@e> {EPOCH} +0000\n\nx\n"
    made = history.run("hash-object", "-t", "commit", "--literally", "-w", "--stdin", input=content)

    [commit] = commits(history, Revisions(made, exclude="first"))

    assert (commit.author.name, commit.author.email, commit.author.date) == ("A", "a@e", None)
    assert commit.committer.date == datetime.fromtimestamp(EPOCH, UTC)


def test_commits_gives_each_message_as_it_is_when_asked_in_utf8(history: Git) -> None:
    history.run("config", "i18n.logOutputEncoding", "ISO-8859-1")

    [second] = commits(history, Revisions("main^", exclude="first"), messages=True)

    assert (second.subject, second.message) == ("second", "second\n\nbody ü\n")


def test_commits_filters_merges_and_first_parents(history: Git) -> None:
    since_first = Revisions("main", exclude="first")
    assert subjects(commits(history, since_first)) == ["Merge branch 'side'", "s3", "second", "s1"]
    merges_only = Revisions("main", exclude="first", merges=True)
    assert subjects(commits(history, merges_only)) == ["Merge branch 'side'"]
    no_merges = Revisions("main", exclude="first", merges=False)
    assert subjects(commits(history, no_merges)) == ["s3", "second", "s1"]
    first_parent = Revisions("main", exclude="first", first_parent=True)
    assert subjects(commits(history, first_parent)) == ["Merge branch 'side'", "second"]


def test_commits_limits_then_reverses_and_orders_by_topology(history: Git) -> None:
    since_first = Revisions("main", exclude="first")

    # As git does: the newest two, then oldest first.
    assert subjects(commits(history, since_first, max_count=2, reverse=True)) == [
        "s3",
        "Merge branch 'side'",
    ]
    # No line's commits between another's, whatever their dates.
    assert subjects(commits(history, since_first, topo_order=True)) in (
        ["Merge branch 'side'", "s3", "s1", "second"],
        ["Merge branch 'side'", "second", "s3", "s1"],
    )


def test_commits_leaves_out_patches_the_excluded_side_has_when_asked(history: Git) -> None:
    assert subjects(commits(history, Revisions("side", exclude="copy"))) == ["s3", "s1"]
    picked = Revisions("side", exclude="copy", cherry_pick=True)
    assert subjects(commits(history, picked)) == ["s3"]
    assert count(history, picked) == 1


def test_cherry_pick_takes_one_revision_on_each_side() -> None:
    with pytest.raises(ValueError, match="one include and one exclude"):
        Revisions(["side", "main"], exclude="copy", cherry_pick=True)


def test_commits_by_author_text_and_committer_date(history: Git) -> None:
    def found(revisions: Revisions) -> list[str]:
        return subjects(commits(history, revisions))

    assert found(Revisions("main", author="<o@e>")) == ["second"]
    # A fixed string, not a pattern, which "o.e" would be.
    assert found(Revisions("main", author="o.e")) == []
    # As the commit records it: the mailmap (which `git log` reads by default) maps no one.
    (Path(history.directory) / ".mailmap").write_text("New <n@e> <o@e>\n", encoding="utf-8")
    assert found(Revisions("main", author="<n@e>")) == []
    assert found(Revisions("main", author="<o@e>")) == ["second"]
    # By the committer's date (second's author's is later).
    since = datetime.fromtimestamp(EPOCH + 3000, UTC)
    assert found(Revisions("main", exclude="first", since=since)) == ["Merge branch 'side'", "s3"]
    # At or after it: not s3, a microsecond before.
    later = since + timedelta(microseconds=1)
    assert found(Revisions("main", exclude="first", since=later)) == ["Merge branch 'side'"]


def test_since_must_say_its_zone() -> None:
    with pytest.raises(ValueError, match="naive"):
        Revisions("main", since=datetime(2026, 1, 1))


def test_commits_that_change_paths_from_the_top_taken_literally(
    history: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Which would take the marks commits gives each path as part of its name, and match a
    # path that differs only in case.
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    monkeypatch.setenv("GIT_ICASE_PATHSPECS", "1")

    def found(git: ReadOnlyGit, *paths: str) -> list[str]:
        return subjects(commits(git, Revisions("main", exclude="first", paths=paths)))

    for git in (history, history.read_only()):
        assert found(git, "src") == ["s3", "s1"]
        assert len(found(git, "")) == 4  # the top: the whole worktree
        assert found(git, "*") == []  # no file named *
        assert found(git, "B") == []  # nor B
    assert found(Git(Path(history.directory) / "src"), "b") == ["second"]


# Taken literally, as git reports no path with them, these match nothing: the top is "".
@pytest.mark.parametrize("path", [".", "./b", "src/.", "src/../src", ".."])
def test_a_path_with_a_dot_part_matches_nothing(history: Git, path: str) -> None:
    assert commits(history, Revisions("main", paths=[path])) == []


def test_many_revisions_take_one_process(history: Git, git_calls: list[GitCall]) -> None:
    tips = ["main", "side", "copy", *(f"main~{n}" for n in range(1, 3))]

    found = commits(history, Revisions(tips, exclude=["first", "copy~"]))

    assert sorted(subjects(found)) == ["Merge branch 'side'", "s1", "s1", "s3", "second"]
    assert [call[0] for call in git_calls] == ["rev-list"]


# On git's stdin, an empty line ends the revisions, a NUL ends the line, a "\r" ending it is
# dropped, and a line starting with "-" is an option ("--not" flips the rest).
@pytest.mark.parametrize("name", ["", "--not", "-x", "main\n--all", "main\0x", "main\r"])
def test_a_revision_is_never_read_as_something_else(name: str) -> None:
    with pytest.raises(ValueError, match="not a revision"):
        Revisions("main", exclude=name)
    with pytest.raises(ValueError, match="not a revision"):
        Revisions(["main", name], exclude="other")
    with pytest.raises(ValueError, match="not a revision"):
        Revisions([name, "main"])


def test_an_unknown_revision_raises(history: Git) -> None:
    with pytest.raises(UnknownRevisionError):
        commits(history, Revisions("main", exclude="nothing"))


def test_count_and_any_commit(history: Git) -> None:
    assert count(history.read_only(), Revisions("main", exclude="first")) == 4
    assert count(history, Revisions("side", exclude="main")) == 0
    assert any_commit(history.read_only(), Revisions("main", exclude="first", merges=True))
    assert not any_commit(history, Revisions("side", exclude="first", merges=True))
