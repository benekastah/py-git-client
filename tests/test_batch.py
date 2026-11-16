from pathlib import Path

import pytest

from git_client import Git, GitError, RefUpdate, config, hash_blobs, patch_ids, refs, update_refs


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


def test_patch_ids_of_no_commands_is_nothing(repo: Git) -> None:
    assert patch_ids(repo) == []


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


def test_hash_blobs_writes_each_as_it_is(repo: Git) -> None:
    contents = [b"one\r\n", b"", b"three\n"]

    ids = hash_blobs(repo, contents)

    assert [repo.run_bytes("cat-file", "blob", blob) for blob in ids] == contents


def test_hash_blobs_of_nothing_is_nothing(repo: Git) -> None:
    assert hash_blobs(repo, []) == []


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
