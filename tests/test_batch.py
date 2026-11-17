from collections.abc import Callable
from pathlib import Path

import pytest

from git_client import (
    Git,
    GitError,
    RefUpdate,
    config,
    config_key,
    git_paths,
    hash_blob_at,
    hash_blobs,
    merge_file,
    patch_ids,
    printable,
    refs,
    update_refs,
)


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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diff.noprefix", "true"),
        ("color.ui", "always"),
        ("diff.external", "no-such-external-diff"),
        ("log.abbrevCommit", "true"),
        ("format.pretty", "format:%s"),
    ],
)
def test_patch_ids_gives_the_same_ids_whatever_the_config_says(
    repo_from: RepoFrom, key: str, value: str
) -> None:
    repo = topic_changing(repo_from, "b")
    expected = patch_ids(repo, *TOPIC)
    repo.run("config", key, value)

    assert patch_ids(repo, *TOPIC) == expected


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
