import gc
import subprocess
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from git_client import CatFile, Git, GitError
from git_client.client import Arg, EnvValue


def test_resolves_names_to_object_ids(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("HEAD") == repo.run("rev-parse", "HEAD")
        assert cat_file.resolve("main^{tree}") == repo.run("rev-parse", "main^{tree}")


def test_info_gives_the_type_and_size(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        found = cat_file.info("HEAD:a")

    assert found is not None
    assert (found.type, found.size) == ("blob", 2)


def test_a_name_that_names_nothing_resolves_to_none(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("no-such-branch") is None
        assert cat_file.resolve("") is None
        assert cat_file.resolve("HEAD\nHEAD") is None


def test_keeps_answering_after_a_name_git_exits_on(repo: Git) -> None:
    # main has no upstream: git cat-file exits rather than saying @{u} names nothing.
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("main@{u}") is None
        assert cat_file.resolve("main") == repo.run("rev-parse", "main")


def test_sees_refs_as_they_are_when_asked(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        before = cat_file.resolve("main")
        repo.run("commit", "--quiet", "--allow-empty", "-m", "second")

        assert cat_file.resolve("main") != before
        assert cat_file.resolve("main") == repo.run("rev-parse", "main")


def test_reads_an_objects_type_and_content(repo: Git) -> None:
    blob = repo.run("rev-parse", "HEAD:a")

    with CatFile(repo) as cat_file:
        assert cat_file.read(blob) == ("blob", b"a\n")
        kind, commit = cat_file.read(repo.run("rev-parse", "HEAD"))

    assert kind == "commit"
    assert commit.endswith(b"\n\nfirst\n")


def test_reading_an_object_that_isnt_there_fails(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        with pytest.raises(GitError):
            cat_file.read("0" * 40)
        with pytest.raises(ValueError):
            cat_file.read("main")
        # And it's still in step.
        assert cat_file.read(repo.run("rev-parse", "HEAD:a")) == ("blob", b"a\n")


def test_a_name_with_a_nul_names_nothing(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("HEAD\0junk") is None


def test_works_in_a_sha256_repository(tmp_path: Path) -> None:
    git = Git(tmp_path / "repo")
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", str(tmp_path / "repo"))
    blob = git.run("hash-object", "-w", "--stdin", input="content\n")

    with CatFile(git) as cat_file:
        assert len(blob) == 64
        assert cat_file.resolve(blob) == blob
        assert cat_file.read(blob) == ("blob", b"content\n")


def test_stopping_leaves_no_pipe_open(repo: Git) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        cat_file = CatFile(repo)
        cat_file.resolve("main@{u}")  # git exits, and the next request starts another
        cat_file.resolve("main")
        cat_file.close()
        del cat_file
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


class GitWithoutBatchCommand(Git):
    """A git older than 2.36, whose cat-file exits with a usage error at --batch-command."""

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(129)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )


def test_a_git_without_batch_command_is_an_error_not_an_empty_answer(repo: Git) -> None:
    with (
        CatFile(GitWithoutBatchCommand(repo.directory)) as cat_file,
        pytest.raises(GitError, match="2.36"),
    ):
        cat_file.resolve("HEAD")
