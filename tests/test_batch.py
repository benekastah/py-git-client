import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_client import (
    Git,
    GitCache,
    GitError,
    Head,
    NotARepositoryError,
    ReadOnlyGit,
    Ref,
    RefUpdate,
    Track,
    config,
    decode,
    git_common_dir,
    git_dir,
    git_paths,
    git_paths_and_head,
    hash_blob_at,
    hash_blobs,
    merge_file,
    patch_ids,
    printable,
    refs,
    update_refs,
)
from git_client.batch import _ahead_behind_alone  # pyright: ignore[reportPrivateUsage]
from git_client.testing import FakeGit, GitCall, RepoTemplates


def test_config_gives_every_value_of_each_key_in_order(repo: Git) -> None:
    repo.run("config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    repo.run("config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*")
    repo.run("config", "branch.main.description", "line one\nline two")

    entries = config(repo)

    assert entries["remote.origin.fetch"] == [
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/*:refs/tags/*",
    ]
    assert entries["branch.main.description"] == ["line one\nline two"]
    assert entries["user.name"] == ["Test"]


def test_config_tells_a_key_with_no_value_from_an_empty_one(repo: Git) -> None:
    with (Path(repo.directory) / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write("[flags]\n\tbare\n\tempty =\n")

    entries = config(repo)

    # git reads the first as true and the second as false.
    assert entries["flags.bare"] == [None]
    assert entries["flags.empty"] == [""]


def test_config_keeps_bytes_that_arent_utf8_for_printable_to_replace(repo: Git) -> None:
    with (Path(repo.directory) / ".git" / "config").open("ab") as f:
        f.write(b"[tool]\n\tname = caf\xe9\n")

    [name] = config(repo)["tool.name"]

    assert name == "caf\udce9"
    assert printable(name) == "caf\ufffd"


def test_config_gives_keys_as_git_prints_them(repo: Git) -> None:
    repo.run("config", "Branch.Topic.pushRemote", "origin")

    # The section and name in lower case, the subsection as written.
    assert config(repo)["branch.Topic.pushremote"] == ["origin"]


def test_patch_ids_in_a_sha256_repository(tmp_path: Path) -> None:
    git = Git(tmp_path / "repo")
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", str(tmp_path / "repo"))
    (tmp_path / "repo" / "a").write_bytes(b"a\n")
    git.run("add", "a")
    git.run("commit", "--quiet", "-m", "first")

    [patches] = patch_ids(git, ["log", "-p", "HEAD"])

    assert [commit for _, commit in patches] == [git.run("rev-parse", "HEAD")]


def test_refs_gives_each_refs_object_upstream_and_whether_head_is_on_it(
    repo: Git, tmp_path: Path
) -> None:
    origin = tmp_path / "origin.git"
    repo.run("clone", "--quiet", "--bare", repo.directory, str(origin))
    repo.run("remote", "add", "origin", str(origin))
    repo.run("fetch", "--quiet", "origin")
    repo.run("branch", "--quiet", "--set-upstream-to", "origin/main", "main")
    repo.run("branch", "topic")
    repo.run("remote", "set-head", "origin", "main")

    found = refs(repo, "refs/heads/", "refs/remotes/")

    head = repo.run("rev-parse", "HEAD")
    assert found["refs/heads/main"].object == head
    assert found["refs/heads/main"].upstream == "refs/remotes/origin/main"
    assert found["refs/heads/main"].head
    assert not found["refs/heads/topic"].head
    assert found["refs/heads/topic"].upstream == ""
    assert found["refs/remotes/origin/HEAD"].symref == "refs/remotes/origin/main"


def test_refs_names_a_branch_fully_even_when_a_tag_has_its_name(repo: Git) -> None:
    # %(refname:short) would give the branch as "heads/v1", which names no branch.
    repo.run("tag", "v1")
    repo.run("commit", "--quiet", "--allow-empty", "-m", "second")
    repo.run("branch", "--quiet", "v1")
    repo.run("branch", "--quiet", "--set-upstream-to", "main", "v1")

    branch = refs(repo, "refs/heads/")["refs/heads/v1"]

    assert branch.object == repo.run("rev-parse", "refs/heads/v1")
    assert branch.object != repo.run("rev-parse", "refs/tags/v1")
    assert branch.upstream == "refs/heads/main"


def test_refs_keeps_bytes_that_arent_utf8_for_printable_to_replace(repo: Git) -> None:
    # In an upstream's name in the config rather than a branch's name, which macOS and Windows
    # can't store: Latin-1's é, which isn't UTF-8.
    repo.run("config", "branch.main.remote", ".")
    repo.run("config", "branch.main.merge", "refs/heads/caf\udce9")

    upstream = refs(repo, "refs/heads/main")["refs/heads/main"].upstream

    assert upstream == "refs/heads/caf\udce9"
    assert printable(upstream) == "refs/heads/caf\ufffd"


def test_refs_splits_its_listing_only_where_git_does(repo: Git) -> None:
    # Python's splitlines would split this name too.
    repo.run("branch", "a\u2028b")

    assert set(refs(repo, "refs/heads/")) == {"refs/heads/main", "refs/heads/a\u2028b"}


def test_refs_takes_a_pattern_that_looks_like_an_option_as_a_pattern(repo: Git) -> None:
    repo.run("branch", "topic")

    assert refs(repo, "--points-at=topic") == {}


def _branches_that_track(git: Git) -> None:
    """main at "second" (2020-01-02); `synced`, `ahead` (2020-01-04), `behind` (at "first",
    made now) and `topic` ("first" then 2020-01-03), each tracking main; `gone`, tracking
    origin's missing `gone`; topic pushing to origin, whose `topic` is at "first"; and tags:
    `v1` (annotated, at main) and `tree` (at main's tree)."""

    def commit(message: str, day: int) -> None:
        env = {"GIT_COMMITTER_DATE": f"2020-01-0{day}T00:00:00+00:00"}
        git.run("commit", "--quiet", "--allow-empty", "-m", message, env=env)

    git.run("branch", "behind")
    git.run("branch", "topic")
    commit("second", 2)
    git.run("branch", "synced")
    git.run("branch", "gone")
    git.run("switch", "--quiet", "-c", "ahead")
    commit("ahead", 4)
    git.run("switch", "--quiet", "topic")
    commit("topic", 3)
    git.run("switch", "--quiet", "main")
    for branch in ("synced", "ahead", "behind", "topic"):
        git.run("branch", "--quiet", "--set-upstream-to", "main", branch)
    git.run("remote", "add", "origin", "https://example.invalid/repo.git")
    git.run("config", "branch.gone.remote", "origin")
    git.run("config", "branch.gone.merge", "refs/heads/gone")
    git.run("config", "push.default", "current")
    git.run("config", "branch.topic.pushRemote", "origin")
    git.run("update-ref", "refs/remotes/origin/topic", "behind")
    git.run("tag", "-a", "-m", "v1", "v1")
    git.run("tag", "tree", "main^{tree}")


@pytest.fixture
def tracking(repo_from: RepoFrom) -> Git:
    """A copy of `_branches_that_track`'s repository."""
    return repo_from("tracking", _branches_that_track)


def test_refs_reads_how_each_branch_stands_against_its_upstream_as_git_says(
    tracking: Git,
) -> None:
    found = refs(tracking, "refs/heads/", track=True)

    assert {name.removeprefix("refs/heads/"): ref.track for name, ref in found.items()} == {
        "main": None,  # no upstream
        "synced": Track(),
        "ahead": Track(ahead=1),
        "behind": Track(behind=1),
        "topic": Track(ahead=1, behind=1),
        "gone": Track(gone=True),
    }


def test_refs_reads_where_each_branch_fetches_from_and_pushes_to(tracking: Git) -> None:
    found = refs(tracking, "refs/heads/", "refs/tags/v1", remotes=True, push_track=True)

    topic, gone, tag = (found[f"refs/{name}"] for name in ("heads/topic", "heads/gone", "tags/v1"))
    assert (topic.upstream_remote, topic.upstream_remote_ref) == (".", "refs/heads/main")
    assert (topic.push, topic.push_remote) == ("refs/remotes/origin/topic", "origin")
    assert topic.push_track == Track(ahead=1)
    assert (gone.upstream_remote, gone.upstream_remote_ref) == ("origin", "refs/heads/gone")
    assert gone.push_track == Track(gone=True)  # origin has no gone
    assert (tag.upstream_remote, tag.push, tag.push_track) == ("", "", None)


def test_refs_reads_each_commits_committer_date(tracking: Git) -> None:
    found = refs(tracking, "refs/heads/main", "refs/tags/v1", dates=True)

    assert found["refs/heads/main"].committer_date == datetime(2020, 1, 2, tzinfo=UTC)
    assert found["refs/tags/v1"].committer_date is None  # a tag, not a commit


def test_refs_counts_each_refs_commits_ahead_of_and_behind_a_base(tracking: Git) -> None:
    found = refs(tracking, ahead_behind="main")

    assert found["refs/heads/topic"].ahead_behind == (1, 1)
    assert found["refs/heads/behind"].ahead_behind == (0, 1)
    assert found["refs/tags/v1"].ahead_behind == (0, 0)  # the commit it tags
    assert found["refs/tags/tree"].ahead_behind is None  # no commit


def test_refs_counts_ahead_behind_a_ref_at_a_time_as_git_before_2_41_must(
    tracking: Git,
) -> None:
    # Called directly, so that a newer git tests it too; git 2.36's CI runs it through refs.
    counted = _ahead_behind_alone(tracking, "main", refs(tracking).values())

    assert counted["refs/heads/topic"] == (1, 1)
    assert counted["refs/tags/v1"] == (0, 0)
    assert counted["refs/tags/tree"] is None


def test_refs_refuses_an_ahead_behind_base_a_format_cant_hold(repo: Git) -> None:
    with pytest.raises(ValueError, match=r"\)"):
        refs(repo, ahead_behind="a)b")


def test_refs_keeps_the_refs_git_filters_for(tracking: Git) -> None:
    def names(found: dict[str, Ref]) -> set[str]:
        return {name.removeprefix("refs/heads/") for name in found}

    main = tracking.run("rev-parse", "main")
    heads = "refs/heads/"
    assert names(refs(tracking, heads, contains="topic")) == {"topic"}
    assert names(refs(tracking, heads, no_contains="main")) == {"behind", "topic"}
    assert names(refs(tracking, heads, merged="behind")) == {"behind"}
    assert names(refs(tracking, heads, merged="ahead", no_merged="main")) == {"ahead"}
    assert names(refs(tracking, heads, points_at=main)) == {"main", "synced", "gone"}


def test_refs_sorts_by_its_first_key_first_and_keeps_count(tracking: Git) -> None:
    found = refs(tracking, "refs/heads/", sort=["-committerdate", "refname"], count=3)

    assert list(found) == ["refs/heads/behind", "refs/heads/ahead", "refs/heads/topic"]


def test_patch_ids_reads_each_commands_patches_with_one_patch_id(repo: Git) -> None:
    repo.run("switch", "--quiet", "-c", "topic")
    for name in ("b", "c"):
        (Path(repo.directory) / name).write_bytes(f"{name}\n".encode())
        repo.run("add", name)
        repo.run("commit", "--quiet", "-m", name)
    repo.run("switch", "--quiet", "main")
    # A commit of main's own first, so the cherry-pick has another parent, and so another id.
    repo.run("commit", "--quiet", "--allow-empty", "-m", "on main")
    repo.run("cherry-pick", "topic~1")

    on_topic, on_main, a_diff, nothing = patch_ids(
        repo,
        ["log", "-p", "main..topic"],
        ["log", "-p", "topic..main"],
        ["diff", "topic~2", "topic~1"],
        ["log", "-p", "main..main"],
    )

    assert [commit for _, commit in on_topic] == [
        repo.run("rev-parse", "topic"),
        repo.run("rev-parse", "topic~1"),
    ]
    # The cherry-pick is the same change as topic's first commit, and so is the diff. (The
    # empty commit has no patch.)
    assert on_main == [(on_topic[1][0], repo.run("rev-parse", "main"))]
    assert a_diff == [(on_topic[1][0], "0" * 40)]
    assert nothing == []


# The repo_from fixture's type (see conftest.py).
RepoFrom = Callable[[str, Callable[[Git], None]], Git]

# patch_ids' commands for topic_changing's topic: its log, and a diff.
TOPIC = [["log", "-p", "main..topic"], ["diff", "main", "topic"]]


def topic_changing(repo_from: RepoFrom, path: str) -> Git:
    """A repository with a commit of `path` on main, and a change to it on a new branch,
    topic (see TOPIC)."""

    def change(repo: Git) -> None:
        file = Path(repo.directory) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"before\n")
        repo.run("add", path)
        repo.run("commit", "--quiet", "-m", f"add {path}")
        repo.run("switch", "--quiet", "-c", "topic")
        file.write_bytes(b"after\n")
        repo.run("commit", "--quiet", "-am", f"change {path}")

    return repo_from(f"topic-changing-{path}", change)


def test_patch_ids_sees_the_whole_worktree_whatever_diff_relative_says(
    repo_from: RepoFrom,
) -> None:
    repo = topic_changing(repo_from, "top")
    (Path(repo.directory) / "sub").mkdir()
    expected = patch_ids(repo, *TOPIC)
    repo.run("config", "diff.relative", "true")

    assert patch_ids(Git(Path(repo.directory) / "sub"), *TOPIC) == expected
    assert all(expected)


def test_patch_ids_sees_a_submodule_the_config_ignores(repo: Git) -> None:
    (Path(repo.directory) / ".gitmodules").write_text(
        '[submodule "mod"]\n\tpath = mod\n\turl = ./mod\n', encoding="utf-8"
    )
    repo.run("add", ".gitmodules")
    repo.run("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},mod")
    repo.run("commit", "--quiet", "-m", "add mod")
    repo.run("switch", "--quiet", "-c", "topic")
    repo.run("update-index", "--cacheinfo", f"160000,{'2' * 40},mod")
    repo.run("commit", "--quiet", "-m", "move mod")
    commands = [["log", "-p", "main..topic"], ["diff", "main", "topic"]]
    expected = patch_ids(repo, *commands)
    repo.run("config", "submodule.mod.ignore", "all")

    assert patch_ids(repo, *commands) == expected
    assert all(expected)


def moving_a_submodule(repo: Git) -> None:
    """Changes for repo_from: on main, a commit adding a submodule, mod, then one moving it to
    another commit (neither of which the repository has)."""
    (Path(repo.directory) / ".gitmodules").write_text(
        '[submodule "mod"]\n\tpath = mod\n\turl = ./mod\n', encoding="utf-8"
    )
    repo.run("add", ".gitmodules")
    repo.run("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},mod")
    repo.run("commit", "--quiet", "-m", "add mod")
    repo.run("update-index", "--cacheinfo", f"160000,{'2' * 40},mod")
    repo.run("commit", "--quiet", "-m", "move mod")


# patch_ids' commands for moving_a_submodule's main: every commit, the root's too, and a diff.
MAIN = [["log", "-p", "main"], ["diff", "main~2", "main"]]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diff.noprefix", "true"),
        ("color.ui", "always"),
        ("diff.external", "no-such-external-diff"),
        ("log.abbrevCommit", "true"),
        ("format.pretty", "format:%s"),
        ("diff.submodule", "log"),
        ("diff.submodule", "diff"),
        ("log.showRoot", "false"),
    ],
)
def test_patch_ids_gives_the_same_ids_whatever_the_config_says(
    repo_from: RepoFrom, key: str, value: str
) -> None:
    repo = repo_from("moving-a-submodule", moving_a_submodule)
    expected = patch_ids(repo, *MAIN)
    repo.run("config", key, value)

    assert patch_ids(repo, *MAIN) == expected
    # Each commit's patch: the root's, adding mod, and moving it; and the diff's.
    assert [len(patches) for patches in expected] == [3, 1]


@pytest.mark.parametrize(
    "command", [["diff", "main", "topic"], ["diff", "-S", "b", "main"], ["diff", "main"]]
)
def test_patch_ids_runs_a_diff_as_diff(
    repo_from: RepoFrom, git_calls: list[GitCall], command: list[str]
) -> None:
    repo = topic_changing(repo_from, "b")

    patch_ids(repo, command)

    assert git_calls[-2][0] == "diff"


def renaming_topic(repo_from: RepoFrom) -> Git:
    """A repository whose topic renames a file and changes it: lines that myers and histogram
    diff differently, and two changes 8 lines apart, which git's default context keeps in
    hunks of their own, but diff.context or diff.interHunkContext doesn't (see TOPIC)."""

    def change(repo: Git) -> None:
        directory = Path(repo.directory)
        (directory / "old").write_bytes(b"b\na\n{\na\n}\n}\n}\n}\nx\ny\nz\n1\n2\n3\n4\n5\nq\n")
        repo.run("add", "old")
        repo.run("commit", "--quiet", "-m", "add old")
        repo.run("switch", "--quiet", "-c", "topic")
        repo.run("mv", "old", "new")
        (directory / "new").write_bytes(b"}\nb\na\n{\n}\n}\na\n}\n}\nx\ny\nz\n1\n2\n3\n4\n5\nQ\n")
        repo.run("commit", "--quiet", "-am", "rename old")

    return repo_from("renaming-topic", change)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diff.renames", "false"),
        ("diff.context", "1"),
        ("diff.algorithm", "histogram"),
        ("diff.interHunkContext", "10"),
    ],
)
def test_patch_ids_gives_a_diff_and_a_log_the_same_ids_whatever_the_config_says(
    repo_from: RepoFrom, key: str, value: str
) -> None:
    repo = renaming_topic(repo_from)
    expected = patch_ids(repo, *TOPIC)
    repo.run("config", key, value)

    [log], [diff] = patch_ids(repo, *TOPIC)

    assert [log, diff] == [patch for [patch] in expected]
    assert diff == (log[0], "0" * 40)


def test_patch_ids_runs_diff_tree_as_given(repo_from: RepoFrom) -> None:
    repo = topic_changing(repo_from, "b")

    [diff_tree, diff] = [
        patches[0] for patches in patch_ids(repo, ["diff-tree", "-p", "main", "topic"], *TOPIC[1:])
    ]

    assert diff_tree == diff
    assert diff_tree[1] == "0" * 40


def test_patch_ids_runs_a_diff_of_two_blobs_as_diff(repo_from: RepoFrom) -> None:
    # Blobs' ids, which `git diff` takes, but `git diff-tree -p` doesn't. (The patch names
    # each file by its id, so its id isn't the commit's.)
    repo = topic_changing(repo_from, "b")
    blobs = ["diff", repo.run("rev-parse", "main:b"), repo.run("rev-parse", "topic:b")]

    [[(_, commit)]] = patch_ids(repo, blobs)

    assert commit == "0" * 40


def test_patch_ids_takes_no_read_only_client(repo: Git, git_calls: list[GitCall]) -> None:
    # It runs whatever command it's given, which a function taking a read-only client mustn't.
    for reader in (ReadOnlyGit(repo.directory), repo.read_only()):
        with pytest.raises(TypeError, match="patch_ids takes a Git, not a ReadOnlyGit"):
            patch_ids(reader, ["log", "-p", "HEAD"])  # pyright: ignore[reportArgumentType]

    assert git_calls == []


def test_patch_ids_takes_pathspec_magic(repo_from: RepoFrom) -> None:
    # A path as git reports it, from the top of the worktree, given from a subdirectory.
    repo = topic_changing(repo_from, "top")
    (Path(repo.directory) / "sub").mkdir()
    sub = Git(Path(repo.directory) / "sub")

    [patches] = patch_ids(sub, [*TOPIC[0], "--", ":(top,literal)top"])

    assert [commit for _, commit in patches] == [repo.run("rev-parse", "topic")]


def test_patch_ids_takes_paths_from_the_top_as_they_are(repo_from: RepoFrom) -> None:
    repo = topic_changing(repo_from, "top")
    (Path(repo.directory) / "sub").mkdir()
    # Which would have git take ":(top,literal)top" as a name, were it not overridden.
    sub = Git(Path(repo.directory) / "sub", env={"GIT_LITERAL_PATHSPECS": "1"})

    from_top, as_pattern, no_paths = patch_ids(
        sub,
        [*TOPIC[0], "--", "no-such-path", "top"],
        [*TOPIC[0], "--", "to*"],
        TOPIC[0],
        paths_from_top=True,
    )

    topic = [repo.run("rev-parse", "topic")]
    assert [commit for _, commit in from_top] == topic
    # "to*" names no path: it's taken as a name, not a pattern.
    assert as_pattern == []
    assert [commit for _, commit in no_paths] == topic


def test_patch_ids_takes_paths_from_the_top_case_sensitively(repo_from: RepoFrom) -> None:
    repo = topic_changing(repo_from, "Ab")
    # Which would have "ab" name "Ab" too, were it not overridden.
    git = Git(repo.directory, env={"GIT_ICASE_PATHSPECS": "1"})

    [lower] = patch_ids(git, [*TOPIC[0], "--", "ab"], paths_from_top=True)

    assert lower == []


def test_patch_ids_runs_no_textconv(repo_from: RepoFrom) -> None:
    repo = topic_changing(repo_from, "b.txt")
    expected = patch_ids(repo, *TOPIC)
    (Path(repo.directory) / ".git" / "info" / "attributes").write_text(
        "*.txt diff=mine\n", encoding="utf-8"
    )
    repo.run("config", "diff.mine.textconv", "no-such-textconv")

    assert patch_ids(repo, *TOPIC) == expected


def test_patch_ids_checks_no_signature(repo: Git, tmp_path: Path) -> None:
    # A commit with a signature (not a real one: git runs gpg on it all the same).
    unsigned = repo.run_bytes("cat-file", "commit", "HEAD")
    headers, message = unsigned.split(b"\n\n", 1)
    signature = b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n -----END PGP SIGNATURE-----"
    signed = (
        repo.run_bytes(
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--stdin",
            input=headers + b"\n" + signature + b"\n\n" + message,
        )
        .decode()
        .strip()
    )
    repo.run("config", "log.showSignature", "true")
    repo.run("config", "gpg.program", "no-such-gpg")
    trace = tmp_path / "trace"
    traced = Git(repo.directory, env={"GIT_TRACE": str(trace)})

    [[(_, commit)]] = patch_ids(traced, ["log", "-p", "-1", signed])
    patch_ids(traced, ["show", signed])

    assert commit == signed
    assert "no-such-gpg" not in trace.read_text(encoding="utf-8")


def test_patch_ids_reads_the_object_format_once_per_client(
    repo: Git, git_calls: list[list[str]]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    patch_ids(git, ["log", "-p", "HEAD"])
    patch_ids(git, ["log", "-p", "HEAD"])

    assert [call[0] for call in git_calls] == [
        "rev-parse",
        "log",
        "patch-id",
        "log",
        "patch-id",
    ]


def test_hash_blobs_writes_each_as_it_is(repo: Git) -> None:
    # Which would have git write one\r\n as one\n.
    repo.run("config", "core.autocrlf", "true")
    contents = [b"one\r\n", b"", b"three\n"]

    ids = hash_blobs(repo, contents)

    assert [repo.run_bytes("cat-file", "blob", blob) for blob in ids] == contents


def test_git_paths_finds_each_name_where_git_keeps_it_with_one_git(
    repo: Git, tmp_path: Path, git_calls: list[list[str]]
) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))
    hooks = tmp_path / "hooks"
    repo.run("config", "core.hooksPath", str(hooks))
    own = Path(Git(linked).run("rev-parse", "--path-format=absolute", "--git-dir"))
    started = len(git_calls)

    found = git_paths(Git(linked), "HEAD", "refs/heads", "hooks")

    # HEAD is the linked worktree's own; refs are the main repository's.
    main = Path(repo.directory) / ".git"
    assert [path.resolve() for path in found] == [
        (own / "HEAD").resolve(),
        (main / "refs" / "heads").resolve(),
        hooks.resolve(),
    ]
    assert all(path.is_absolute() for path in found)
    assert len(git_calls) == started + 1


def test_git_paths_asks_git_once_per_name(repo: Git, git_calls: list[list[str]]) -> None:
    git = Git(repo.directory, cache=GitCache())
    first = git_paths(git, "HEAD", "index")
    assert git_paths(git, "index", "HEAD", "refs") == [
        first[1],
        first[0],
        first[1].parent / "refs",
    ]
    git_paths(git, "HEAD", "refs")

    assert [call[-2:] for call in git_calls] == [
        ["--git-path", "index"],
        ["--git-path", "refs"],
    ]
    assert len(git_calls) == 2


def test_git_paths_follows_a_change_of_directory_or_environment(repo: Git, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))
    git = Git(repo.directory, cache=GitCache())
    [main] = git_paths(git, "HEAD")

    git.directory = str(linked)
    [own] = git_paths(git, "HEAD")
    git.env["GIT_DIR"] = str(Path(repo.directory) / ".git")
    [forced] = git_paths(git, "HEAD")

    assert own != main
    assert forced == main


def test_git_paths_asks_again_for_hooks_which_a_config_change_moves(repo: Git) -> None:
    git = Git(repo.directory, cache=GitCache())
    [before] = git_paths(git, "hooks")
    [hook_before] = git_paths(git, "hooks/pre-commit")
    repo.run("config", "core.hooksPath", str(Path(repo.directory) / "my-hooks"))

    assert git_paths(git, "hooks") != [before]
    assert git_paths(git, "hooks/pre-commit") != [hook_before]


def test_git_paths_refuses_a_name_with_a_line_ending(repo: Git) -> None:
    with pytest.raises(ValueError, match="line ending"):
        git_paths(repo, "HEAD", "a\nb")


def blob_id(git: Git, content: bytes) -> str:
    """The id of content as a blob, as it is."""
    return git.run_bytes("hash-object", "--stdin", "--no-filters", input=content).decode().strip()


def has_object(git: Git, object_id: str) -> bool:
    return git.ok("cat-file", "-e", object_id)


def test_hash_blob_at_hashes_content_through_its_paths_filters(repo: Git) -> None:
    (Path(repo.directory) / ".gitattributes").write_bytes(b"*.txt text\n")

    # .txt files are text, so git stores them with LF line endings; others as they are.
    assert hash_blob_at(repo, "notes.txt", b"one\r\ntwo\r\n") == blob_id(repo, b"one\ntwo\n")
    assert hash_blob_at(repo, "data.bin", b"one\r\n") == blob_id(repo, b"one\r\n")
    # Unlike hash_blobs, which writes content as it is.
    [as_it_is] = hash_blobs(repo, [b"one\r\ntwo\r\n"])
    assert as_it_is == blob_id(repo, b"one\r\ntwo\r\n")


def test_hash_blob_at_runs_the_paths_clean_filter(repo: Git) -> None:
    # git's own program, as a filter every system can run.
    (Path(repo.directory) / ".gitattributes").write_bytes(b"*.md filter=strip\n")
    repo.run("config", "filter.strip.clean", "git stripspace")

    assert hash_blob_at(repo, "notes.md", b"a  \n\n\n") == blob_id(repo, b"a\n")


def test_hash_blob_at_takes_a_path_from_the_clients_directory(repo: Git) -> None:
    top = Path(repo.directory)
    (top / ".gitattributes").write_bytes(b"*.txt text\n")
    (top / "sub").mkdir()
    (top / "sub" / ".gitattributes").write_bytes(b"*.txt -text\n")
    as_it_is = blob_id(repo, b"one\r\n")

    # notes.txt, from sub, is sub/notes.txt, which isn't text: its CRLF stays.
    assert hash_blob_at(Git(top / "sub"), "notes.txt", b"one\r\n") == as_it_is
    # The same file, from the top, as git prints paths, to a client there.
    assert hash_blob_at(repo, "sub/notes.txt", b"one\r\n") == as_it_is
    assert hash_blob_at(repo, "notes.txt", b"one\r\n") != as_it_is


def test_hash_blob_at_takes_a_path_from_the_top_if_asked(repo: Git) -> None:
    top = Path(repo.directory)
    (top / ".gitattributes").write_bytes(b"*.txt text\n")
    (top / "sub" / "deep").mkdir(parents=True)
    (top / "sub" / ".gitattributes").write_bytes(b"*.txt -text\n")
    as_it_is = blob_id(repo, b"one\r\n")
    converted = blob_id(repo, b"one\n")

    for directory in (top / "sub", top / "sub" / "deep"):
        client = Git(directory)
        assert hash_blob_at(client, "sub/notes.txt", b"one\r\n", paths_from_top=True) == as_it_is
        assert hash_blob_at(client, "notes.txt", b"one\r\n", paths_from_top=True) == converted
        # A path as the filesystem has it, in bytes, as well.
        assert hash_blob_at(client, b"sub/notes.txt", b"one\r\n", paths_from_top=True) == as_it_is
    # At the top, from the top is from the directory.
    assert hash_blob_at(repo, "sub/notes.txt", b"one\r\n", paths_from_top=True) == as_it_is


def test_hash_blob_at_takes_a_path_from_the_top_outside_the_worktree(
    repo: Git, tmp_path: Path
) -> None:
    top = Path(repo.directory)
    (top / "sub").mkdir()
    (top / "sub" / ".gitattributes").write_bytes(b"*.txt text\n")
    # A client outside the worktree, which GIT_WORK_TREE names.
    outside = tmp_path / "outside"
    outside.mkdir()
    env = {"GIT_DIR": str(top / ".git"), "GIT_WORK_TREE": str(top)}
    client = Git(outside, env=env)

    found = hash_blob_at(client, "sub/notes.txt", b"one\r\n", paths_from_top=True)
    assert found == blob_id(repo, b"one\n")


def test_hash_blob_at_writes_the_blob_only_if_asked(repo: Git) -> None:
    (Path(repo.directory) / ".gitattributes").write_bytes(b"*.txt text\n")

    hashed = hash_blob_at(repo, "only-hashed.txt", b"only hashed\r\n")
    written = hash_blob_at(repo, "written.txt", b"written\r\n", write=True)

    assert not has_object(repo, hashed)
    assert repo.run_bytes("cat-file", "blob", written) == b"written\n"


def test_merge_file_merges_changes_to_different_lines(repo: Git) -> None:
    merged = merge_file(repo, ours=b"A\nb\nc\n", base=b"a\nb\nc\n", theirs=b"a\nb\nC\n")

    assert merged == (b"A\nb\nC\n", 0)


def test_merge_file_counts_conflicts_and_marks_them_with_its_labels(repo: Git) -> None:
    content, conflicts = merge_file(
        repo,
        ours=b"ours\nsame\nours\n",
        base=b"base\nsame\nbase\n",
        theirs=b"theirs\nsame\ntheirs\n",
        labels=("mine", "original", "yours"),
        options=["--diff3"],
    )

    assert conflicts == 2
    assert content.split(b"\n")[:5] == [
        b"<<<<<<< mine",
        b"ours",
        b"||||||| original",
        b"base",
        b"=======",
    ]


def test_merge_file_shows_conflicts_as_the_repositorys_config_says(repo: Git) -> None:
    repo.run("config", "merge.conflictStyle", "diff3")

    content, _ = merge_file(repo, ours=b"ours\n", base=b"base\n", theirs=b"theirs\n")

    assert b"||||||| base\nbase\n" in content


def test_merge_file_counts_at_most_127_conflicts(repo: Git) -> None:
    # git's exit status is the count, which it holds to 127: above that is an error.
    # 130 of them, each 4 lines from the next: git takes conflicts closer than that as one.
    def lines(word: str) -> bytes:
        return b"".join(f"{word}{i}\n".encode() + b"same\n" * 4 for i in range(130))

    _, conflicts = merge_file(repo, ours=lines("ours"), base=lines("base"), theirs=lines("theirs"))

    assert conflicts == 127


def test_merge_file_raises_if_git_cant_merge(repo: Git) -> None:
    with pytest.raises(GitError, match="binary"):
        merge_file(repo, ours=b"ours\0", base=b"base\0", theirs=b"theirs\0")


def test_nothing_to_do_starts_no_git(repo: Git, git_calls: list[list[str]]) -> None:
    assert git_paths(repo) == []
    assert hash_blobs(repo, []) == []
    assert patch_ids(repo) == []
    update_refs(repo, [])

    assert git_calls == []


def test_update_refs_makes_every_change(repo: Git) -> None:
    head = repo.run("rev-parse", "HEAD")
    repo.run("branch", "old")

    update_refs(
        repo,
        [
            RefUpdate("refs/heads/new", head, old="0" * 40),
            RefUpdate("refs/heads/old", None, old=head),
        ],
        message="tidy",
    )

    assert set(refs(repo, "refs/heads/")) == {"refs/heads/main", "refs/heads/new"}
    assert repo.run("reflog", "-1", "--format=%gs", "refs/heads/new") == "tidy"


def test_update_refs_makes_none_if_any_cant_be_made(repo: Git) -> None:
    head = repo.run("rev-parse", "HEAD")

    with pytest.raises(GitError):
        update_refs(
            repo,
            [
                RefUpdate("refs/heads/new", head),
                RefUpdate("refs/heads/main", head, old="1" * 40),
            ],
        )

    assert set(refs(repo, "refs/heads/")) == {"refs/heads/main"}


@pytest.mark.parametrize(
    ("where", "prefix"), [(".", ""), ("sub/deep", "sub/deep/"), (" a/b", " a/b/"), (".git", "")]
)
def test_git_paths_learns_the_prefix_in_the_same_process(
    repo: Git, git_calls: list[list[str]], where: str, prefix: str
) -> None:
    (Path(repo.directory) / where).mkdir(parents=True, exist_ok=True)
    git = Git(Path(repo.directory) / where, cache=GitCache())
    git_paths(git, "HEAD")
    assert git.prefix() == prefix
    assert git.cdup() == "../" * prefix.count("/")
    assert len(git_calls) == 1


@pytest.mark.parametrize("ask", [git_paths, git_paths_and_head])
def test_git_paths_learns_the_object_id_length_in_the_same_process(
    ask: Callable[[Git, str], object], tmp_path: Path, git_calls: list[list[str]]
) -> None:
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", "sha256")
    git = Git(tmp_path / "sha256", cache=GitCache())
    started = len(git_calls)
    ask(git, "HEAD")
    assert git.object_id_length() == 64
    assert len(git_calls) == started + 1


def test_git_paths_asks_no_object_id_length_it_knows(repo: Git, git_calls: list[list[str]]) -> None:
    git = Git(repo.directory, cache=GitCache())
    git.object_id_length()
    git_paths(git, "HEAD")
    assert "--show-object-format" not in git_calls[-1]


def test_git_paths_asks_no_prefix_it_knows(repo: Git, git_calls: list[list[str]]) -> None:
    git = Git(repo.directory, cache=GitCache())
    git.prefix()
    git_paths(git, "HEAD")
    assert "--show-prefix" not in git_calls[-1]


def test_git_paths_refuses_an_answer_a_line_short() -> None:
    git = FakeGit()
    git.respond(["rev-parse"], stdout="/repo/.git/HEAD\n")  # no index line
    with pytest.raises(ValueError):
        git_paths(git, "HEAD", "index")


def test_git_paths_refuses_an_empty_answer() -> None:
    git = FakeGit()
    git.respond(["rev-parse"])
    with pytest.raises(ValueError):
        git_paths(git, "HEAD")


def test_git_paths_and_head_gives_the_paths_and_the_branch_in_one_git(
    repo: Git, git_calls: list[list[str]]
) -> None:
    paths, head = git_paths_and_head(repo, "HEAD", "index")

    assert paths == [Path(repo.directory) / ".git" / name for name in ("HEAD", "index")]
    assert head == Head(branch="refs/heads/main")
    assert len(git_calls) == 1


def test_git_paths_and_head_runs_under_a_read_only_client(repo: Git) -> None:
    assert git_paths_and_head(repo.read_only(), "HEAD")[1].branch == "refs/heads/main"


def test_git_paths_and_head_follows_a_detached_head(repo: Git) -> None:
    repo.run("checkout", "--quiet", "--detach")

    assert git_paths_and_head(repo, "HEAD")[1] == Head(detached=True)


def test_git_paths_and_head_asks_git_each_time_since_head_moves(
    repo: Git, git_calls: list[list[str]]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    assert git_paths_and_head(git, "HEAD")[1].branch == "refs/heads/main"
    repo.run("switch", "--quiet", "--create", "other")
    del git_calls[:]

    assert git_paths_and_head(git, "HEAD")[1].branch == "refs/heads/other"
    assert len(git_calls) == 1


def test_git_paths_and_head_does_not_know_an_unborn_branch(tmp_path: Path) -> None:
    Git(tmp_path).run("init", "--quiet", "--initial-branch=main", "new")
    git = Git(tmp_path / "new")

    paths, head = git_paths_and_head(git, "HEAD")

    assert paths == git_paths(git, "HEAD")
    assert head == Head()


def test_git_paths_and_head_gives_a_linked_worktrees_own_paths(repo: Git, tmp_path: Path) -> None:
    linked = Git(tmp_path / "linked")
    repo.run("worktree", "add", "--quiet", "-b", "feature", str(tmp_path / "linked"))

    paths, head = git_paths_and_head(linked, "HEAD", "refs")

    assert paths == git_paths(linked, "HEAD", "refs")
    assert paths[0] != git_paths(repo, "HEAD")[0]
    assert head.branch == "refs/heads/feature"


def test_git_paths_and_head_fills_what_git_paths_remembers(
    repo: Git, git_calls: list[list[str]]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    git_paths_and_head(git, "HEAD", "refs")
    del git_calls[:]

    git_paths(git, "HEAD", "refs")
    git.prefix()

    assert git_calls == []


def test_git_paths_and_head_raises_when_git_fails_before_the_paths(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        git_paths_and_head(Git(tmp_path), "HEAD")


def test_git_paths_and_head_refuses_a_name_with_a_line_ending(repo: Git) -> None:
    with pytest.raises(ValueError, match="line ending"):
        git_paths_and_head(repo, "a\nb")


def test_git_paths_and_head_without_names_still_gives_the_head(repo: Git) -> None:
    assert git_paths_and_head(repo) == ([], Head(branch="refs/heads/main"))


@pytest.mark.parametrize(
    ("stdout", "head"),
    [
        ("/g/HEAD\nrefs/heads/main\n", Head(branch="refs/heads/main")),
        ("/g/HEAD\nHEAD\n", Head(detached=True)),
    ],
)
def test_git_paths_and_head_reads_a_fakes_answer(stdout: str, head: Head) -> None:
    git = FakeGit(object_id_length=40, cache=GitCache())
    # The prefix, cdup, inside and git directories first.
    git.respond(["rev-parse"], stdout=f"\n\ntrue\n/g\n/g\n{stdout}")

    assert git_paths_and_head(git, "HEAD") == ([Path("/g/HEAD")], head)
    assert git.prefix() == ""


def test_git_paths_and_head_does_not_know_an_unborn_branch_wherever_it_is(tmp_path: Path) -> None:
    git = Git(tmp_path)
    git.run("init", "--quiet", "--initial-branch=main", "--bare", "bare")
    git.run("init", "--quiet", "--initial-branch=main", "checkout")
    (tmp_path / "checkout" / "HEAD").write_bytes(b"")  # a file git could take for the ref

    for directory in ("bare", "checkout", "checkout/.git"):
        assert git_paths_and_head(Git(tmp_path / directory), "HEAD")[1] == Head(), directory


def test_git_paths_and_head_raises_when_git_fails_with_no_names_to_print(repo: Git) -> None:
    repo.prefix()  # known, so only the head is asked for
    (Path(repo.directory) / ".git" / "HEAD").unlink()  # no longer a repository

    with pytest.raises(NotARepositoryError):
        git_paths_and_head(repo)


def test_git_paths_and_head_follows_hooks_wherever_config_puts_them(repo: Git) -> None:
    git_paths_and_head(repo, "hooks")
    repo.run("config", "core.hooksPath", "elsewhere")

    assert git_paths(repo, "hooks") == [Path(repo.directory) / "elsewhere"]
    assert git_paths_and_head(repo, "hooks")[0] == [Path(repo.directory) / "elsewhere"]


def test_git_paths_and_head_names_any_ref_head_points_at(repo: Git) -> None:
    repo.run("update-ref", "refs/remotes/origin/x", "HEAD")
    repo.run("symbolic-ref", "HEAD", "refs/remotes/origin/x")

    assert git_paths_and_head(repo, "HEAD")[1] == Head(branch="refs/remotes/origin/x")


def test_git_paths_and_head_does_not_know_head_when_a_branch_is_named_head(repo: Git) -> None:
    repo.run("update-ref", "refs/heads/HEAD", "HEAD")  # which makes "HEAD" ambiguous

    assert git_paths_and_head(repo, "HEAD")[1] == Head()


# Names whose newline makes a path's lines look like other answers: one newline, before a
# line of "true" (the marker after no cdup); and two, around an empty line, after a space.
ADVERSARIAL_NAMES = ["w\ntrue", " a\n\nb"]
HEAD_STATES = ["main", "ambiguous", "unborn"]
LAYOUTS = ["top", "below", "git directory", "bare", "outside"]


def make_adversarial_repositories(directory: Path) -> None:
    """For each HEAD state and adversarial name: a repository whose path ends in the name,
    with a directory of that name in it, and a bare one beside it."""
    for state in HEAD_STATES:
        for i, name in enumerate(ADVERSARIAL_NAMES):
            top = directory / state / name
            Git(directory).run("init", "--quiet", str(top))
            git = Git(top)
            (top / name).mkdir()
            if state != "unborn":
                git.run("commit", "--quiet", "--allow-empty", "-m", "first")
            if state == "ambiguous":
                git.run("update-ref", "refs/heads/HEAD", "HEAD")
            bare = directory / state / f"bare{i}{name}"
            if state == "unborn":
                Git(directory).run("init", "--quiet", "--bare", str(bare))
            else:
                Git(directory).run("clone", "--quiet", "--bare", str(top), str(bare))
                if state == "ambiguous":
                    Git(bare).run("update-ref", "refs/heads/HEAD", "HEAD")


def asked_alone(git: Git) -> tuple[str, str, Path, Path, list[Path], Head]:
    """The prefix, cdup, git directory, common directory, paths of HEAD and config, and HEAD,
    each asked of git alone."""

    def answer(*args: str) -> bytes:
        return git.process("rev-parse", *args, capture_output=True).stdout.removesuffix(b"\n")

    def path(*args: str) -> Path:
        return Path(os.fsdecode(answer("--path-format=absolute", *args)))

    paths = [path("--git-path", name) for name in ("HEAD", "config")]
    named = decode(answer("--verify", "--quiet", "--symbolic-full-name", "HEAD"))
    head = Head() if not named else Head(detached=True) if named == "HEAD" else Head(branch=named)
    prefix, cdup = decode(answer("--show-prefix")), decode(answer("--show-cdup"))
    return prefix, cdup, path("--git-dir"), path("--git-common-dir"), paths, head


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' file names can't hold a newline")
@pytest.mark.parametrize("state", HEAD_STATES)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_git_paths_and_head_tells_lines_apart_as_git_asked_alone_does(
    state: str, layout: str, repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    root = repo_templates.build("adversarial", make_adversarial_repositories) / state
    # One name a case, each name in every layout and HEAD state, to keep the processes few.
    i = (HEAD_STATES.index(state) + LAYOUTS.index(layout)) % len(ADVERSARIAL_NAMES)
    name = ADVERSARIAL_NAMES[i]
    env: dict[str, str] = {}
    directory = {
        "top": root / name,
        "below": root / name / name,
        "git directory": root / name / ".git",
        "bare": root / f"bare{i}{name}",
        "outside": tmp_path,
    }[layout]
    if layout == "outside":
        top = tmp_path / f"top{name}"
        top.mkdir()
        env = {"GIT_DIR": str(root / name / ".git"), "GIT_WORK_TREE": str(top)}
    expected = asked_alone(Git(directory, env=env))
    git = Git(directory, env=env)
    # Each case's first question differs, so each function's fallback is run.
    if LAYOUTS.index(layout) % 2:
        git_dir(git)
    paths, head = git_paths_and_head(git, "HEAD", "config")

    assert (git.prefix(), git.cdup(), git_dir(git), git_common_dir(git), paths, head) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' file names can't hold a newline")
@pytest.mark.parametrize("ask", [git_paths, git_paths_and_head])
def test_a_bare_repositorys_marker_after_no_cdup_isnt_taken_for_one_line_each(
    ask: Callable[[Git, str], object], tmp_path: Path
) -> None:
    # No cdup, then "false", then as many lines as asked for: the one path with a newline (the
    # objects', which the git directories' don't hold) adds one.
    Git(tmp_path).run("init", "--quiet", "--bare", "bare")
    objects = tmp_path / "o\nz"
    objects.mkdir()
    git = Git(tmp_path / "bare", env={"GIT_OBJECT_DIRECTORY": str(objects)})

    ask(git, "objects")
    assert git_paths(git, "objects") == [objects]
    assert (git.prefix(), git.cdup(), git_dir(git)) == ("", "", tmp_path / "bare")


def test_git_paths_and_head_takes_an_unborn_branch_whatever_git_warns(tmp_path: Path) -> None:
    git = Git(tmp_path)
    git.run("init", "--quiet", "repo")
    repo = Git(tmp_path / "repo")
    # git warns of this ("core.bare and core.worktree do not make sense"), and answers.
    repo.run("config", "core.bare", "true")
    repo.run("config", "core.worktree", str(tmp_path / "repo"))

    assert git_paths_and_head(repo, "HEAD") == ([tmp_path / "repo" / ".git" / "HEAD"], Head())


def test_git_paths_and_head_refuses_an_answer_a_line_short() -> None:
    git = FakeGit()
    git.respond(["rev-parse"], stdout="/g/HEAD\nrefs/heads/main\n")  # a path too few for two

    with pytest.raises(ValueError):
        git_paths_and_head(git, "HEAD", "index")


def test_git_paths_and_head_takes_a_quiet_exit_1_with_the_paths_for_an_unborn_head() -> None:
    git = FakeGit()
    git.respond(["rev-parse"], stdout="/g/HEAD\n", returncode=1)

    assert git_paths_and_head(git, "HEAD") == ([Path("/g/HEAD")], Head())


@pytest.mark.parametrize(
    ("stderr", "returncode", "stdout"),
    [
        ("", 2, "\n\ntrue\n/g\n/g\n/g/HEAD\n"),  # another exit status
        ("", 1, "\n\ntrue\n/g\n/g\n/g/HEAD\nextra\n"),  # more than the paths
        ("", 1, "\n\ntrue\n/g\n/g\n"),  # fewer than the paths
    ],
)
def test_git_paths_and_head_raises_for_a_failure_that_is_not_an_unborn_head(
    stderr: str, returncode: int, stdout: str
) -> None:
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse"], stdout=stdout, stderr=stderr, returncode=returncode)

    with pytest.raises(GitError):
        git_paths_and_head(git, "HEAD")
