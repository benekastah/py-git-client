import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pygit2
import pytest
from pygit2.enums import ConfigLevel

from git_client import CatFile, Git, GitError, NotARepositoryError, ReadOnlyGit
from git_client.testing import GitCall


@pytest.fixture(autouse=True)
def native_config_paths(
    tmp_path: Path, git_config: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep libgit2's config lookups inside the test directory too."""
    # The Git test plugin normally isolates global config with an explicit override.
    # Use Git's default discovery with the same isolated config for native-path tests.
    (tmp_path / ".gitconfig").write_bytes(git_config.read_bytes())
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(tmp_path))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    paths = pygit2.settings.search_path
    levels = (ConfigLevel.PROGRAMDATA, ConfigLevel.SYSTEM, ConfigLevel.GLOBAL, ConfigLevel.XDG)
    original = {level: paths[level] for level in levels}
    try:
        for level in levels:
            paths[level] = os.fspath(tmp_path)
        yield
    finally:
        for level, path in original.items():
            paths[level] = path


def test_native_reader_reads_full_object_ids_without_processes(
    repo: Git, git_calls: list[GitCall]
) -> None:
    commit, tree = repo.run("rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    with CatFile(repo) as objects:
        expected_info = objects.info(commit)
        expected_tree = objects.read(tree)
    git_calls.clear()

    reader = repo.read_only(native=True)
    assert reader.resolve(commit) == commit
    with CatFile(reader) as objects:
        assert objects.info(commit) == expected_info
        assert objects.verify(f"{commit}^{{tree}}") == tree
        assert objects.read(commit, "tree") == expected_tree
    assert not git_calls


def test_writing_client_and_revision_syntax_keep_using_git(
    repo: Git, git_calls: list[GitCall]
) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    git_calls.clear()
    assert repo.resolve(commit) == commit
    assert repo.read_only(native=True).resolve("HEAD") == commit
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-check"]] * 2


def test_repository_environment_override_uses_git(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    reader = ReadOnlyGit(
        tmp_path, native=True, env={"GIT_DIR": os.fspath(Path(repo.directory) / ".git")}
    )
    git_calls.clear()
    assert reader.resolve(commit) == commit
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-check"]]


def test_replace_ref_uses_git_even_when_added_after_a_native_read(
    repo: Git, git_calls: list[GitCall]
) -> None:
    original = repo.run("hash-object", "-w", "--stdin", input="original")
    replacement = repo.run("hash-object", "-w", "--stdin", input="replacement")
    with CatFile(repo.read_only(native=True)) as objects:
        assert objects.read(original) == ("blob", b"original")
        repo.run("replace", original, replacement)
        git_calls.clear()
        assert objects.read(original) == ("blob", b"replacement")
        assert [call[:2] for call in git_calls] == [["cat-file", "--batch-command"]]
        repo.run("replace", "-d", original)
        assert objects.read(original) == ("blob", b"replacement")
        objects.close()
        assert objects.read(original) == ("blob", b"original")


def test_native_reads_fall_back_when_pygit2_is_unavailable(
    repo: Git, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    monkeypatch.setitem(sys.modules, "pygit2", None)
    git_calls.clear()
    assert repo.read_only(native=True).resolve(commit) == commit
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-check"]]


def test_object_read_with_deadline_uses_git(repo: Git, git_calls: list[GitCall]) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    git_calls.clear()
    assert repo.read_only(native=True).resolve(commit, timeout=10) == commit
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-check"]]


def test_hex_named_ref_is_left_to_git(repo: Git, git_calls: list[GitCall]) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    repo.run("tag", commit)
    with CatFile(repo) as objects:
        expected = objects.info(commit)
    assert expected is not None
    git_calls.clear()
    with CatFile(repo.read_only(native=True)) as objects:
        assert objects.info(commit) == expected
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-command"]]


def test_native_read_requires_a_directory(repo: Git) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    reader = ReadOnlyGit(Path(repo.directory) / "a", native=True)
    with pytest.raises(NotARepositoryError):
        reader.resolve(commit)


@pytest.mark.parametrize("config_in_client", [False, True])
def test_native_read_respects_alternate_global_config(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_in_client: bool
) -> None:
    commit = repo.resolve("HEAD")
    assert commit is not None
    config_path = tmp_path / "broken.gitconfig"
    config_path.write_text("[section\n", encoding="utf-8")
    env = {"GIT_CONFIG_GLOBAL": os.fspath(config_path)}
    if not config_in_client:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.fspath(config_path))
        env = {}
    reader = ReadOnlyGit(repo.directory, env=env, native=True)
    with pytest.raises(GitError, match="bad config"):
        reader.resolve(commit)


def test_peeled_tree_with_packed_replacement_uses_git(repo: Git, git_calls: list[GitCall]) -> None:
    commit, tree = repo.run("rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    empty = repo.run("mktree", input="")
    repo.run("replace", tree, empty)
    repo.run("pack-refs", "--all")
    git_calls.clear()
    with CatFile(repo.read_only(native=True)) as objects:
        assert objects.read(commit, "tree") == ("tree", b"")
    assert [call[:2] for call in git_calls] == [["cat-file", "--batch-command"]]


def test_packed_uppercase_replacement_is_left_to_git(repo: Git) -> None:
    original = repo.run("hash-object", "-w", "--stdin", input="original")
    replacement = repo.run("hash-object", "-w", "--stdin", input="replacement")
    repo.run("update-ref", f"refs/replace/{original.upper()}", replacement)
    repo.run("pack-refs", "--all")
    with CatFile(repo.read_only(native=True)) as objects:
        assert objects.read(original) == ("blob", b"replacement")


@pytest.mark.parametrize("kind", ["commit", "tag"])
def test_type_suffix_requires_a_parseable_object(repo: Git, kind: str) -> None:
    invalid = repo.run("hash-object", "--literally", "-t", kind, "-w", "--stdin", input="bad")
    name = f"{invalid}^{{{kind}}}"
    assert repo.resolve(name) is None
    assert repo.read_only(native=True).resolve(name) is None
