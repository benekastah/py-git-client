from collections.abc import Callable
from pathlib import Path

import pytest

import git_client.remotes
from git_client import (
    FetchRef,
    FetchRefspec,
    FetchStatus,
    Git,
    GitError,
    RemoteRefs,
    fetch,
    refs,
    remote_refs,
)
from git_client.testing import FakeGit, GitCall

Changes = Callable[[Git], None]

NEEDS_PORCELAIN = pytest.mark.needs_git(2, 41, reason="for fetch --porcelain")


def make_remote(git: Git) -> None:
    """make_repo's repository with a remote, origin (`../origin.git`), fetched once (with no
    FETCH_HEAD), then changed: `moved` fast-forwarded to `next`, `forced` rewritten to
    `loose`, `gone` deleted, `new` (at `next`) and the tag `late` (at `other`) added.
    refs/ids/ names the commits: `first` (main), `next` and `other` on it, and `loose`, a root
    commit. Local branches `local` and `local-forced` are at first."""
    first = git.run("rev-parse", "HEAD")
    tree = git.run("rev-parse", "HEAD^{tree}")
    ids = {
        "first": first,
        "next": git.run("commit-tree", "-p", first, "-m", "next", tree),
        "other": git.run("commit-tree", "-p", first, "-m", "other", tree),
        "loose": git.run("commit-tree", "-m", "loose", tree),
    }
    git.run("update-ref", "--stdin", input="".join(
        f"update refs/ids/{name} {object_id}\n" for name, object_id in ids.items()
    ))  # fmt: skip
    git.run("init", "--quiet", "--bare", "../origin.git")
    # Relative, so a copy of the template fetches from its own copy of origin.
    git.run("remote", "add", "origin", "../origin.git")
    branches = ("moved", "forced", "gone", "kept")
    heads = (f"{first}:refs/heads/{name}" for name in branches)
    git.run("push", "--quiet", "origin", "refs/ids/*:refs/ids/*", *heads)
    git.run("fetch", "--quiet", "--no-write-fetch-head", "origin")
    git.run("branch", "local", first)
    git.run("branch", "local-forced", first)
    changes = {"heads/moved": ids["next"], "heads/forced": ids["loose"], "heads/gone": None,
               "heads/new": ids["next"], "tags/late": ids["other"]}  # fmt: skip
    Git(Path(git.directory).parent / "origin.git").run("update-ref", "--stdin", input="".join(
        f"delete refs/{ref}\n" if to is None else f"update refs/{ref} {to}\n"
        for ref, to in changes.items()
    ))  # fmt: skip


@pytest.fixture
def remote(repo_from: Callable[[str, Changes], Git]) -> tuple[Git, dict[str, str]]:
    """make_remote's repository, and its refs/ids/ commits by name."""
    git = repo_from("remote", make_remote)
    ids = refs(git, "refs/ids/")
    return git, {name.removeprefix("refs/ids/"): ref.object for name, ref in ids.items()}


def tracking(git: Git) -> dict[str, str]:
    """The repository's remote-tracking refs and tags, and where each points."""
    found = refs(git, "refs/remotes/", "refs/tags/")
    return {name: ref.object for name, ref in found.items()}


@NEEDS_PORCELAIN
def test_fetch_updates_and_prunes_and_reports_each_ref(
    remote: tuple[Git, dict[str, str]], git_calls: list[GitCall]
) -> None:
    git, ids = remote
    first, next_, loose = ids["first"], ids["next"], ids["loose"]
    git_calls.clear()

    result = fetch(git, "origin", prune=True, tags=False)

    assert len(git_calls) == 1
    assert result.returncode == 0
    assert result.refs is not None
    assert sorted(result.refs, key=lambda ref: ref.ref) == [
        FetchRef("refs/remotes/origin/forced", FetchStatus.FORCED, first, loose),
        FetchRef("refs/remotes/origin/gone", FetchStatus.PRUNED, first, None),
        FetchRef("refs/remotes/origin/moved", FetchStatus.FAST_FORWARD, first, next_),
        FetchRef("refs/remotes/origin/new", FetchStatus.NEW, None, next_),
    ]
    assert tracking(git) == {
        "refs/remotes/origin/forced": loose,
        "refs/remotes/origin/kept": first,
        "refs/remotes/origin/moved": next_,
        "refs/remotes/origin/new": next_,
    }


@NEEDS_PORCELAIN
def test_refspecs_say_where_each_goes_and_only_force_forces(
    remote: tuple[Git, dict[str, str]],
) -> None:
    git, ids = remote

    result = fetch(
        git,
        "origin",
        [
            FetchRefspec("refs/heads/forced", "refs/heads/local"),
            FetchRefspec("refs/heads/forced", "refs/heads/local-forced", force=True),
            FetchRefspec("refs/heads/moved"),
        ],
        tags=False,
    )

    assert result.returncode == 1  # the rejection
    assert result.refs is not None
    assert set(result.refs) == {
        FetchRef("refs/heads/local", FetchStatus.REJECTED, ids["first"], ids["loose"]),
        FetchRef("refs/heads/local-forced", FetchStatus.FORCED, ids["first"], ids["loose"]),
        FetchRef("FETCH_HEAD", FetchStatus.NEW, None, ids["next"]),
        # What a named remote's config tracks, git updates too.
        FetchRef("refs/remotes/origin/forced", FetchStatus.FORCED, ids["first"], ids["loose"]),
        FetchRef("refs/remotes/origin/moved", FetchStatus.FAST_FORWARD, ids["first"], ids["next"]),
    }
    heads = refs(git, "refs/heads/")
    assert heads["refs/heads/local"].object == ids["first"]
    assert heads["refs/heads/local-forced"].object == ids["loose"]


@NEEDS_PORCELAIN
def test_tags_and_no_fetch_head(remote: tuple[Git, dict[str, str]]) -> None:
    git, ids = remote

    result = fetch(git, "origin", [FetchRefspec("refs/heads/moved")], tags=True,
                   write_fetch_head=False)  # fmt: skip

    assert result.refs is not None
    assert set(result.refs) == {
        FetchRef("refs/remotes/origin/moved", FetchStatus.FAST_FORWARD, ids["first"], ids["next"]),
        FetchRef("refs/tags/late", FetchStatus.NEW, None, ids["other"]),
    }
    assert git.resolve("FETCH_HEAD") is None
    assert tracking(git)["refs/tags/late"] == ids["other"]


def test_before_git_2_41_it_fetches_and_reports_no_refs(
    remote: tuple[Git, dict[str, str]],
    git_calls: list[GitCall],
    monkeypatch: pytest.MonkeyPatch,
    git_version: tuple[int, int, int],
) -> None:
    git, ids = remote
    if git_version >= (2, 41):
        # As git 2.40 does --porcelain, which its fetch doesn't know.
        monkeypatch.setattr(git_client.remotes, "_PORCELAIN", "--no-such-option")
    git_calls.clear()

    result = fetch(git, "origin", [FetchRefspec("refs/heads/new", "refs/remotes/origin/new")])

    assert len(git_calls) == 2  # refused, then without
    assert result.returncode == 0
    assert result.refs is None
    assert tracking(git)["refs/remotes/origin/new"] == ids["next"]


def test_a_fetch_that_fails_says_so(repo: Git, git_version: tuple[int, int, int]) -> None:
    result = fetch(repo, "no-such-remote", tags=False)

    assert result.returncode == 128
    assert "'no-such-remote' does not appear to be a git repository" in result.stderr
    assert result.refs == (() if git_version >= (2, 41) else None)


@pytest.mark.parametrize(
    ("source", "destination", "says"),
    [
        ("", None, "empty source"),
        ("+refs/heads/a", None, "would force"),
        ("^refs/heads/a", None, "negative"),
        ("refs/heads/a:refs/heads/b", None, "':'"),
        ("refs/heads/a", "", "empty destination"),
    ],
)
def test_a_refspec_git_would_read_otherwise_is_refused(
    source: str, destination: str | None, says: str
) -> None:
    with pytest.raises(ValueError, match=says):
        FetchRefspec(source, destination)


OID = "a" * 40


@pytest.mark.parametrize(
    "stdout",
    [
        f"x {OID} {OID} refs/heads/a\n",  # a flag git doesn't document
        f"* {OID} refs/heads/a\n",  # too few fields
        f"* {OID} {OID} refs/heads/a b\n",  # too many
        f"* {OID} {OID[1:]} refs/heads/a\n",  # not an object id
        f"*{OID} {OID} refs/heads/a\n",
    ],
)
def test_output_git_never_gives_raises(stdout: str) -> None:
    git = FakeGit()
    git.respond(["fetch", "--porcelain"], stdout=stdout, stderr="what git said\n")

    with pytest.raises(GitError, match="fetch printed what it can't read") as raised:
        fetch(git, "origin")

    assert raised.value.stderr == "what git said\n"


def origin(git: Git) -> Git:
    """A client for make_remote's origin."""
    return Git(Path(git.directory).parent / "origin.git")


def test_remote_refs_lists_every_ref_and_symref(
    remote: tuple[Git, dict[str, str]], git_calls: list[GitCall]
) -> None:
    git, ids = remote
    origin(git).run("symbolic-ref", "HEAD", "refs/heads/kept")
    git_calls.clear()

    found = remote_refs(git, "origin", symrefs=True)

    assert len(git_calls) == 1
    assert found == RemoteRefs(
        {
            "HEAD": ids["first"],
            "refs/heads/forced": ids["loose"],
            "refs/heads/kept": ids["first"],
            "refs/heads/moved": ids["next"],
            "refs/heads/new": ids["next"],
            **{f"refs/ids/{name}": object_id for name, object_id in ids.items()},
            "refs/tags/late": ids["other"],
        },
        {"HEAD": "refs/heads/kept"},
    )


def test_remote_refs_takes_patterns_and_heads_only(remote: tuple[Git, dict[str, str]]) -> None:
    git, ids = remote
    patterns = ["refs/heads/moved", "refs/heads/gone", "late", "new"]

    found = remote_refs(git.read_only(), "origin", patterns, heads=True)

    assert found == RemoteRefs({"refs/heads/moved": ids["next"], "refs/heads/new": ids["next"]})


def test_remote_refs_runs_with_the_env_given(remote: tuple[Git, dict[str, str]]) -> None:
    git, ids = remote
    # A URL rewritten from a name only this env gives.
    url = Path(origin(git).directory).as_posix()
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{url}.insteadOf",
           "GIT_CONFIG_VALUE_0": "probe"}  # fmt: skip

    found = remote_refs(git, "probe", ["refs/heads/kept"], env=env)

    assert found == RemoteRefs({"refs/heads/kept": ids["first"]})


def test_remote_refs_raises_what_git_says(repo: Git) -> None:
    with pytest.raises(GitError, match="'no-such-remote' does not appear to be a git repository"):
        remote_refs(repo, "no-such-remote")


@pytest.mark.parametrize(
    "stdout",
    [f"{OID[1:]}\trefs/heads/a\n", f"{OID} refs/heads/a\n", "ref: refs/heads/a HEAD\n"],
)
def test_remote_refs_raises_for_output_git_never_gives(stdout: str) -> None:
    git = FakeGit()
    git.respond(["ls-remote"], stdout=stdout)

    with pytest.raises(GitError, match="ls-remote printed what it can't read"):
        remote_refs(git, "origin")
