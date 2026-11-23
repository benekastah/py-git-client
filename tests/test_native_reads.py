import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pygit2
import pytest
from pygit2.enums import ConfigLevel

from git_client import CatFile, Git, ReadOnlyGit
from git_client.testing import GitCall


@pytest.fixture(autouse=True)
def native_config_paths(tmp_path: Path) -> Iterator[None]:
    """Keep libgit2's config lookups inside the test directory too."""
    paths = pygit2.settings.search_path
    levels = (ConfigLevel.SYSTEM, ConfigLevel.GLOBAL, ConfigLevel.XDG)
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
