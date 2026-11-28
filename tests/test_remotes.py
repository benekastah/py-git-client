import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import git_client.remotes
from git_client import (
    Arg,
    FetchRef,
    FetchRefspec,
    FetchStatus,
    Git,
    GitError,
    RemoteRefs,
    fetch,
    refs,
    remote_refs,
    remote_urls,
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
        f"* {OID} {OID} refs/heads/a b\n",  # too many fields
        f"* {OID} {OID[1:]} refs/heads/a\n",  # not an object id
        f"* {OID} {OID}\n",  # no ref
    ],
)
def test_a_porcelain_line_git_never_gives_raises(stdout: str) -> None:
    git = FakeGit()
    git.respond(["fetch", "--porcelain"], stdout=stdout, stderr="what git said\n")

    with pytest.raises(GitError, match="fetch printed what it can't read") as raised:
        fetch(git, "origin")

    assert raised.value.stderr == "what git said\n"


@pytest.mark.parametrize(
    "line", [f"* {OID} refs/heads/a", f"*{OID} {OID} refs/heads/a", "a message", ""]
)
def test_a_line_not_shaped_as_porcelain_is_skipped(line: str) -> None:
    git = FakeGit()
    ref = f"* {'0' * 40} {OID} refs/heads/b\n"
    git.respond(["fetch", "--porcelain"], stdout=f"{ref}{line}\n{ref}")

    assert fetch(git, "origin").refs == (FetchRef("refs/heads/b", FetchStatus.NEW, None, OID),) * 2


@pytest.mark.needs_git(2, 48, reason="for followRemoteHEAD")
def test_gits_warning_among_the_porcelain_lines_is_skipped(
    remote: tuple[Git, dict[str, str]],
) -> None:
    git, ids = remote
    # git prints its warning that origin's HEAD isn't what origin/HEAD says to stdout, among
    # the refs, even with --porcelain.
    origin(git).run("symbolic-ref", "HEAD", "refs/heads/kept")
    git.run("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/moved")
    git.run("config", "remote.origin.followRemoteHEAD", "warn")

    result = fetch(git, "origin", tags=False)

    assert "'HEAD' at 'origin' is 'kept', but we have 'moved' locally." in result.stdout
    assert result.returncode == 0
    assert result.refs is not None
    assert FetchRef("refs/remotes/origin/new", FetchStatus.NEW, None, ids["next"]) in result.refs
    assert len(result.refs) == 3  # moved, forced and new


class Recording(Git):
    """A Git that records each process's `env` and `timeout`."""

    seen: list[tuple[object, object]]

    def process(self, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.seen.append((kwargs.get("env"), kwargs.get("timeout")))
        return super().process(*args, **kwargs)


@pytest.mark.parametrize(
    "porcelain",
    [pytest.param(True, marks=NEEDS_PORCELAIN, id="porcelain"), pytest.param(False, id="without")],
)
def test_fetch_runs_git_with_the_env_and_timeout_given(
    remote: tuple[Git, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    git_version: tuple[int, int, int],
    porcelain: bool,
) -> None:
    git, ids = remote
    if not porcelain and git_version >= (2, 41):
        monkeypatch.setattr(git_client.remotes, "_PORCELAIN", "--no-such-option")
    recording = Recording(git.directory)
    recording.seen = []
    # A remote named only by this env.
    url = Path(origin(git).directory).as_posix()
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{url}.insteadOf",
           "GIT_CONFIG_VALUE_0": "probe"}  # fmt: skip

    result = fetch(recording, "probe", [FetchRefspec("refs/heads/new", "refs/heads/fetched")],
                   env=env, timeout=30.0)  # fmt: skip

    assert result.returncode == 0
    assert refs(git, "refs/heads/fetched")["refs/heads/fetched"].object == ids["next"]
    assert recording.seen == [(env, 30.0)] * (1 if porcelain else 2)


def make_superproject(git: Git) -> None:
    """make_repo's repository as a submodule, `sub`, of `super`, cloned with it as `clone`
    (which may fetch submodules from a path); then a commit of sub's, refs/ids/new, that
    super's main points to and clone hasn't fetched."""
    allow = ("-c", "protocol.file.allow=always")
    top = Path(git.directory).parent
    Git(top).run("init", "--quiet", "super")
    sup = Git(top / "super")
    sup.run(*allow, "submodule", "--quiet", "add", "../repo", "sub")
    sup.run("commit", "--quiet", "-m", "add sub")
    Git(top).run(*allow, "clone", "--quiet", "--recurse-submodules", "super", "clone")
    Git(top / "clone").run("config", "protocol.file.allow", "always")
    git.run("commit", "--quiet", "--allow-empty", "-m", "new")
    git.run("update-ref", "refs/ids/new", "HEAD")
    Git(top / "super" / "sub").run("pull", "--quiet", "origin", "main")
    sup.run("commit", "--quiet", "-am", "move sub")


@pytest.mark.parametrize(
    "porcelain",
    [pytest.param(True, marks=NEEDS_PORCELAIN, id="porcelain"), pytest.param(False, id="without")],
)
def test_fetch_fetches_no_submodules_on_any_git(
    repo_from: Callable[[str, Changes], Git],
    monkeypatch: pytest.MonkeyPatch,
    git_version: tuple[int, int, int],
    porcelain: bool,
) -> None:
    sub = repo_from("superproject", make_superproject)
    new = sub.run("rev-parse", "refs/ids/new")
    clone = Path(sub.directory).parent / "clone"
    if not porcelain and git_version >= (2, 41):
        monkeypatch.setattr(git_client.remotes, "_PORCELAIN", "--no-such-option")

    result = fetch(Git(clone), "origin")

    assert result.returncode == 0
    # `git fetch` would have fetched it, by fetch.recurseSubmodules' on-demand.
    assert Git(clone / "sub").resolve(new) is None


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


def configured(git: Git, config: str) -> Git:
    """git, with config added to its repository's config file: git takes a remote's settings
    from the repository's config, not from the environment's or the command line's."""
    with (Path(git.directory) / ".git" / "config").open("a", encoding="utf-8") as file:
        file.write(config)
    return git


# Fetch and push rewrite a remote's URLs: url.<base>.insteadOf for each, and for pushes,
# url.<base>.pushInsteadOf first, but not of a pushurl.
REWRITES = """
[remote "a"]
    url = https://host/a
[remote "b"]
    url = https://host/b
    pushurl = https://other/b
[url "ssh://fetch/"]
    insteadOf = https://
[url "ssh://push/"]
    pushInsteadOf = https://host/
"""


@pytest.mark.parametrize(
    ("remote", "push", "urls"),
    [
        ("a", False, ["ssh://fetch/host/a"]),
        ("a", True, ["ssh://push/a"]),
        ("b", False, ["ssh://fetch/host/b"]),
        ("b", True, ["ssh://fetch/other/b"]),
    ],
)
def test_remote_urls_are_what_git_fetches_from_or_pushes_to(
    repo: Git, git_calls: list[GitCall], remote: str, push: bool, urls: list[str]
) -> None:
    git = configured(repo, REWRITES)
    git_calls.clear()

    assert remote_urls(git, remote, push=push) == urls
    assert len(git_calls) == 1


@pytest.mark.parametrize(("push", "urls"), [(False, ["a 1", "a2"]), (True, ["p1", "p2", "p3"])])
def test_remote_urls_lists_every_url(repo: Git, push: bool, urls: list[str]) -> None:
    git = configured(repo, """
[remote "r"]
    url = a 1
    pushurl = p1
    url = a2
    pushurl = p2
    pushurl = p3
""")  # fmt: skip

    assert remote_urls(git.read_only(), "r", push=push) == urls


def test_remote_urls_of_no_remote_raises(repo: Git) -> None:
    with pytest.raises(GitError, match="No such remote '-r'"):
        remote_urls(repo, "-r")


def test_remote_urls_takes_no_remote_of_the_global_config(repo: Git, tmp_path: Path) -> None:
    # fetch and ls-remote take it, but git remote doesn't.
    config = tmp_path / "global"
    config.write_text('[remote "g"]\n    url = https://host/g\n', encoding="utf-8")
    git = Git(repo.directory, env={"GIT_CONFIG_GLOBAL": str(config)})

    with pytest.raises(GitError, match="No such remote 'g'"):
        remote_urls(git, "g")


# Names git takes that str.splitlines() would split: line and paragraph separators, and NEL.
ODD = ["a b", "c\u0085d", "e f"]


def add_odd_branches(git: Git, target: str) -> None:
    """Add a branch of each ODD name to make_remote's origin, at target."""
    updates = "".join(f"update refs/heads/{name} {target}\n" for name in ODD)
    origin(git).run("update-ref", "--stdin", input=updates)


@NEEDS_PORCELAIN
def test_fetch_reports_refs_whose_names_splitlines_would_split(
    remote: tuple[Git, dict[str, str]],
) -> None:
    git, ids = remote
    add_odd_branches(git, ids["next"])
    specs = [FetchRefspec(f"refs/heads/{name}", f"refs/heads/{name}") for name in ODD]

    result = fetch(git, "origin", specs, tags=False)

    assert result.refs is not None
    assert {ref.ref for ref in result.refs} >= {f"refs/heads/{name}" for name in ODD}


def test_remote_refs_lists_refs_whose_names_splitlines_would_split(
    remote: tuple[Git, dict[str, str]],
) -> None:
    git, ids = remote
    add_odd_branches(git, ids["next"])

    found = remote_refs(git, "origin", heads=True)

    assert {f"refs/heads/{name}" for name in ODD} <= found.refs.keys()
    assert found.refs[f"refs/heads/{ODD[0]}"] == ids["next"]


def test_remote_urls_keeps_a_url_that_splitlines_would_split(repo: Git) -> None:
    git = configured(repo, '[remote "r"]\n    url = https://host/a b\u0085c\n')

    assert remote_urls(git, "r") == ["https://host/a b\u0085c"]
