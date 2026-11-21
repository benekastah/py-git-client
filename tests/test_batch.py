from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import (
    Git,
    GitError,
    Head,
    NotARepositoryError,
    ReadOnlyError,
    RefUpdate,
    config,
    config_key,
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
from git_client.testing import FakeGit, GitCall


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


def test_config_key_names_a_key_as_config_does(repo: Git) -> None:
    repo.run("config", "branch.Topic.pushRemote", "origin")
    repo.run("config", "url.https://Example.org/.insteadOf", "ex:")

    entries = config(repo)

    assert entries[config_key("branch.Topic.pushRemote")] == ["origin"]
    assert entries[config_key("URL.https://Example.org/.insteadOf")] == ["ex:"]
    assert entries[config_key("User.Name")] == ["Test"]


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


def test_refs_reads_the_fields_asked_for_in_the_same_process(repo: Git) -> None:
    repo.run("branch", "--quiet", "topic")
    repo.run("branch", "--quiet", "--set-upstream-to", "main", "topic")
    repo.run("commit", "--quiet", "--allow-empty", "-m", "second")

    found = refs(repo, "refs/heads/", fields=("upstream:track", "subject"))

    assert found["refs/heads/topic"].fields == {"upstream:track": "[behind 1]", "subject": "first"}
    assert found["refs/heads/main"].fields == {"upstream:track": "", "subject": "second"}


def test_refs_keeps_bytes_that_arent_utf8_for_printable_to_replace(repo: Git) -> None:
    # In a commit's subject rather than a branch's name, which macOS and Windows can't store.
    # Written with hash-object, as commit-tree would store the byte as Latin-1's é in UTF-8.
    headers = repo.run("cat-file", "commit", "HEAD").split("\n\n")[0]
    commit = repo.run(
        "hash-object", "-t", "commit", "-w", "--stdin", input=f"{headers}\n\ncaf\udce9\n"
    )
    repo.run("update-ref", "refs/heads/topic", commit)

    fields = refs(repo, "refs/heads/topic", fields=["subject"])["refs/heads/topic"].fields

    assert fields == {"subject": "caf\udce9"}
    assert printable(fields["subject"]) == "caf\ufffd"


def test_refs_splits_its_listing_only_where_git_does(repo: Git) -> None:
    # Python's splitlines would split this name too.
    repo.run("branch", "a\u2028b")

    assert set(refs(repo, "refs/heads/")) == {"refs/heads/main", "refs/heads/a\u2028b"}


def test_refs_takes_a_pattern_that_looks_like_an_option_as_a_pattern(repo: Git) -> None:
    repo.run("branch", "topic")

    assert refs(repo, "--points-at=topic") == {}


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


def test_patch_ids_reads_a_diff_of_two_revisions_with_diff_tree(
    repo_from: RepoFrom, git_calls: list[GitCall]
) -> None:
    repo = topic_changing(repo_from, "b")
    (Path(repo.directory) / "b").write_bytes(b"in the worktree\n")

    # Which a ReadOnlyGit runs, and which doesn't write the index.
    log, diff = patch_ids(repo.read_only(), *TOPIC)
    started = len(git_calls)
    [worktree] = patch_ids(repo, ["diff", "topic"])

    assert diff == [(log[0][0], "0" * 40)]
    assert [call[0] for call in git_calls[:started]] == [
        "rev-parse",
        "log",
        "diff-tree",
        "patch-id",
    ]
    assert git_calls[started][0] == "diff"
    assert worktree and worktree[0][0] != log[0][0]


@pytest.mark.parametrize("command", [["diff", "-S", "b", "main"], ["diff", "main"]])
def test_patch_ids_runs_any_other_diff_as_diff(
    repo_from: RepoFrom, git_calls: list[GitCall], command: list[str]
) -> None:
    # Each compares the worktree: the value of an option isn't a second revision.
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


def test_patch_ids_runs_a_diff_of_two_blobs_as_diff(
    repo_from: RepoFrom, git_calls: list[GitCall]
) -> None:
    # diff-tree takes only trees and commits.
    repo = topic_changing(repo_from, "b")
    blobs = ["diff", "main:b", "topic:b"]

    [[(patch_id, _)]] = patch_ids(repo, blobs)

    assert git_calls[-2][0] == "diff"
    assert patch_id == patch_ids(repo, TOPIC[0])[0][0][0]
    with pytest.raises(ReadOnlyError):
        patch_ids(repo.read_only(), blobs)


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
    patch_ids(repo, ["log", "-p", "HEAD"])
    patch_ids(repo, ["log", "-p", "HEAD"])

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
    first = git_paths(repo, "HEAD", "index")
    assert git_paths(repo, "index", "HEAD", "refs") == [
        first[1],
        first[0],
        first[1].parent / "refs",
    ]
    git_paths(repo, "HEAD", "refs")

    assert [call[-2:] for call in git_calls] == [
        ["--git-path", "index"],
        ["--git-path", "refs"],
    ]
    assert len(git_calls) == 2


def test_git_paths_follows_a_change_of_directory_or_environment(repo: Git, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    repo.run("worktree", "add", "--quiet", str(linked))
    git = Git(repo.directory)
    [main] = git_paths(git, "HEAD")

    git.directory = str(linked)
    [own] = git_paths(git, "HEAD")
    git.env["GIT_DIR"] = str(Path(repo.directory) / ".git")
    [forced] = git_paths(git, "HEAD")

    assert own != main
    assert forced == main


def test_git_paths_asks_again_for_hooks_which_a_config_change_moves(repo: Git) -> None:
    [before] = git_paths(repo, "hooks")
    [hook_before] = git_paths(repo, "hooks/pre-commit")
    repo.run("config", "core.hooksPath", str(Path(repo.directory) / "my-hooks"))

    assert git_paths(repo, "hooks") != [before]
    assert git_paths(repo, "hooks/pre-commit") != [hook_before]


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
    git = Git(Path(repo.directory) / where)
    git_paths(git, "HEAD")
    assert git.prefix() == prefix
    assert len(git_calls) == 1


@pytest.mark.parametrize("ask", [git_paths, git_paths_and_head])
def test_git_paths_learns_the_object_id_length_in_the_same_process(
    ask: Callable[[Git, str], object], tmp_path: Path, git_calls: list[list[str]]
) -> None:
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", "sha256")
    git = Git(tmp_path / "sha256")
    started = len(git_calls)
    ask(git, "HEAD")
    assert git.object_id_length() == 64
    assert len(git_calls) == started + 1


def test_git_paths_asks_no_object_id_length_it_knows(repo: Git, git_calls: list[list[str]]) -> None:
    repo.object_id_length()
    git_paths(repo, "HEAD")
    assert "--show-object-format" not in git_calls[-1]


def test_git_paths_asks_no_prefix_it_knows(repo: Git, git_calls: list[list[str]]) -> None:
    repo.prefix()
    git_paths(repo, "HEAD")
    assert "--show-prefix" not in git_calls[-1]


def test_git_paths_refuses_an_answer_a_line_short() -> None:
    git = FakeGit(object_id_length=40)  # so rev-parse is asked no object format
    git.respond(["rev-parse"], stdout="/repo/.git/HEAD\n")  # no prefix line
    with pytest.raises(ValueError):
        git_paths(git, "HEAD")


def test_git_paths_refuses_an_empty_answer() -> None:
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse", "--show-prefix"], stdout="\n")
    git.prefix()
    git.respond(["rev-parse", "--path-format=absolute"])  # nothing
    with pytest.raises(ValueError):
        git_paths(git, "HEAD")


def test_git_paths_and_head_gives_the_paths_and_the_branch_in_one_git(
    repo: Git, git_calls: list[list[str]]
) -> None:
    paths, head = git_paths_and_head(repo, "HEAD", "index")

    assert paths == git_paths(repo, "HEAD", "index")
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
    assert git_paths_and_head(repo, "HEAD")[1].branch == "refs/heads/main"
    repo.run("switch", "--quiet", "--create", "other")
    del git_calls[:]

    assert git_paths_and_head(repo, "HEAD")[1].branch == "refs/heads/other"
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
    git_paths_and_head(repo, "HEAD", "refs")
    del git_calls[:]

    git_paths(repo, "HEAD", "refs")
    repo.prefix()

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
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse"], stdout=f"\n{stdout}")  # the prefix's line first

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


def test_git_paths_and_head_refuses_an_answer_a_line_short() -> None:
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse"], stdout="\n/g/HEAD\n")  # one path too few for two names

    with pytest.raises(ValueError):
        git_paths_and_head(git, "HEAD", "index")


def test_git_paths_and_head_takes_a_quiet_exit_1_with_the_paths_for_an_unborn_head() -> None:
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse"], stdout="\n/g/HEAD\n", returncode=1)

    assert git_paths_and_head(git, "HEAD") == ([Path("/g/HEAD")], Head())


@pytest.mark.parametrize(
    ("stderr", "returncode", "stdout"),
    [
        ("", 2, "\n/g/HEAD\n"),  # another exit status
        ("fatal: it said why\n", 1, "\n/g/HEAD\n"),  # git said something
        ("", 1, "\n/g/HEAD\nextra\n"),  # more than the paths
        ("", 1, "\n"),  # fewer than the paths
    ],
)
def test_git_paths_and_head_raises_for_a_failure_that_is_not_an_unborn_head(
    stderr: str, returncode: int, stdout: str
) -> None:
    git = FakeGit(object_id_length=40)
    git.respond(["rev-parse"], stdout=stdout, stderr=stderr, returncode=returncode)

    with pytest.raises(GitError):
        git_paths_and_head(git, "HEAD")
