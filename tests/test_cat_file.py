import gc
import os
import subprocess
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from git_client import CatFile, Git, GitError, NotARepositoryError, UnknownRevisionError
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
        # git would take HEAD\r as HEAD.
        assert cat_file.resolve("HEAD\r") is None
        # "<name> missing", in three parts, the last two of which aren't a type and size.
        assert cat_file.resolve("no-such branch") is None
        # And again, with a byte that isn't UTF-8 (as decode gives a path).
        assert cat_file.resolve("HEAD:\udce9 x") is None


def test_keeps_answering_after_a_name_git_exits_on(repo: Git) -> None:
    # main has no upstream: git cat-file exits rather than saying @{u} names nothing.
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("main@{u}") is None
        assert cat_file.resolve("main") == repo.run("rev-parse", "main")
        assert cat_file.resolve("main@{u}") is None
        assert cat_file.resolve("main") == repo.run("rev-parse", "main")


class StartsKeepingGit(Git):
    """A Git that keeps each long-lived process it starts."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.started: list[subprocess.Popen[bytes]] = []

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        self.started.append(super().popen(*args, env=env))
        return self.started[-1]


def test_a_process_that_stopped_between_requests_is_started_again(repo: Git) -> None:
    git = StartsKeepingGit(repo.directory)

    with CatFile(git) as cat_file:
        cat_file.resolve("main")
        git.started[-1].kill()  # by the OOM killer, say
        git.started[-1].wait()

        assert cat_file.resolve("main") == repo.run("rev-parse", "main")
        assert cat_file.read(repo.run("rev-parse", "HEAD:a")) == ("blob", b"a\n")
    assert len(git.started) == 2


def test_a_directory_that_isnt_a_repository_is_an_error_not_an_empty_answer(
    tmp_path: Path,
) -> None:
    (tmp_path / "plain").mkdir()
    git = Git(tmp_path / "plain", env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})

    with CatFile(git) as cat_file:
        with pytest.raises(NotARepositoryError):
            cat_file.resolve("HEAD")
        with pytest.raises(NotARepositoryError):
            cat_file.read("0" * 40)


def test_a_broken_config_is_an_error_not_an_empty_answer(repo: Git) -> None:
    with (Path(repo.directory) / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write("[core\n")

    with CatFile(repo) as cat_file, pytest.raises(GitError, match="bad config line"):
        cat_file.resolve("HEAD")


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
        with pytest.raises(UnknownRevisionError, match="0{40}"):
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


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 12),
    reason="os.set_blocking takes a pipe on Windows only from Python 3.12",
)
def test_its_process_holds_no_descriptor_of_ours_open(repo: Git) -> None:
    # One the tool inherited, say, or a pipe another thread was setting up for its process:
    # held by a cat-file that lives as long as the tool, it would never see EOF.
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)
    with CatFile(repo) as cat_file:
        cat_file.resolve("HEAD")
        os.close(write_end)
        os.set_blocking(read_end, False)
        try:
            assert os.read(read_end, 1) == b""
        finally:
            os.close(read_end)


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


class GitThatDiesMidObject(Git):
    """A git whose cat-file says an object has 100 bytes, then stops after 10."""

    def popen(
        self, *args: Arg, env: Mapping[str, EnvValue] | None = None
    ) -> subprocess.Popen[bytes]:
        script = (
            "import sys; sys.stdin.readline();"
            " sys.stdout.write(sys.argv[1] + ' blob 100\\n' + 'x' * 10)"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script, "1" * 40],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )


def test_an_object_cut_short_is_an_error_not_its_content(repo: Git) -> None:
    with (
        CatFile(GitThatDiesMidObject(repo.directory)) as cat_file,
        pytest.raises(GitError, match="10 of its 100 bytes"),
    ):
        cat_file.read("1" * 40)
