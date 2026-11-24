import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from git_client import (
    Arg,
    CatFile,
    Git,
    GitCache,
    ReadOnlyGit,
    config,
    git_common_dir,
    git_dir,
    git_paths,
    git_paths_and_head,
    refs,
)
from git_client.testing import FakeGit, GitCall

# What runs a command of the caller's, or writes: a Git's alone.
_GITS_OWN = (
    "run",
    "run_bytes",
    "ok",
    "succeeds",
    "passthrough",
    "process",
    "popen",
    "command",
    "environment",
    "write_configs",
)


def test_a_read_only_client_offers_no_command_of_the_callers(repo: Git) -> None:
    for reader in (ReadOnlyGit(repo.directory), repo.read_only(), FakeGit().read_only()):
        assert not isinstance(reader, Git)
        assert [name for name in _GITS_OWN if hasattr(reader, name)] == []


@pytest.fixture(params=["ReadOnlyGit", "read_only", "Git"])
def client(request: pytest.FixtureRequest, repo: Git) -> ReadOnlyGit:
    """Each client a function that only reads is given: a ReadOnlyGit, a Git's read_only(),
    and a Git."""
    if request.param == "ReadOnlyGit":
        return ReadOnlyGit(repo.directory)
    return repo.read_only() if request.param == "read_only" else Git(repo.directory)


def typed_reads(git: ReadOnlyGit, repo: Git) -> None:
    """Every read a ReadOnlyGit offers, through `git`, each as `repo`'s git answers it."""
    head = repo.run("rev-parse", "HEAD")
    assert git.resolve("HEAD") == head
    ident = repo.run("var", "GIT_AUTHOR_IDENT").rsplit(" ", 2)[0]  # but its time
    assert (git.var("GIT_AUTHOR_IDENT") or "").rsplit(" ", 2)[0] == ident
    assert (git.prefix(), git.cdup(), git.object_id_length()) == ("", "", 40)
    assert (git_dir(git), git_common_dir(git)) == (git_dir(repo), git_common_dir(repo))
    assert git_paths(git, "HEAD") == git_paths(repo, "HEAD")
    assert git_paths_and_head(git, "index")[1].branch == "refs/heads/main"
    assert refs(git)["refs/heads/main"].object == head
    assert config(git) == config(repo)
    with CatFile(git) as cat_file:
        assert cat_file.resolve("HEAD") == head


def test_every_typed_read_takes_any_client(client: ReadOnlyGit, repo: Git) -> None:
    typed_reads(client, repo)


class CountingGit(Git):
    """A tool's own Git, whose `process` and `popen` note each command, as one that times
    them would."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.started: list[list[str]] = []

    def process(self, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.started.append([os.fsdecode(arg) for arg in args])
        return super().process(*args, **kwargs)

    def popen(self, *args: Arg, **kwargs: Any) -> subprocess.Popen[bytes]:
        self.started.append([os.fsdecode(arg) for arg in args])
        return super().popen(*args, **kwargs)


def test_a_git_subclass_sees_every_process_its_typed_reads_start(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git = CountingGit(repo.directory)

    git.resolve("HEAD")
    git.var("GIT_AUTHOR_IDENT")
    git.prefix()
    git_dir(git)
    git_paths(git, "HEAD")
    git_paths_and_head(git, "index")
    refs(git)
    config(git)
    with CatFile(git) as cat_file:
        cat_file.resolve("HEAD")

    assert git.started == git_calls
    assert {args[0] for args in git_calls} == {
        "cat-file",
        "var",
        "rev-parse",
        "for-each-ref",
        "config",
    }


def test_a_subclass_read_only_one_is_a_plain_read_only_git(repo: Git) -> None:
    git = CountingGit(repo.directory)

    reader = git.read_only()
    reader.resolve("HEAD")

    assert type(reader) is ReadOnlyGit
    assert git.started == []  # a client of its own


def test_a_clients_read_only_one_has_its_settings_and_answers(
    repo: Git, git_calls: list[GitCall]
) -> None:
    def stopping() -> bool:
        return False

    git = Git(
        repo.directory,
        timeout=9,
        stopping=stopping,
        env={"TZ": "UTC"},
        process_groups=False,
        close_fds=False,
        cache=GitCache(),
        object_id_length=64,
    )
    git.prefix()
    started = len(git_calls)

    reader = git.read_only()
    git.env["TZ"] = "CET"  # a reader already given out stays as it was

    settings = (
        "directory",
        "program",
        "timeout",
        "stopping",
        "process_groups",
        "close_fds",
        "cache",
    )
    assert [getattr(reader, name) for name in settings] == [getattr(git, name) for name in settings]
    assert reader.env == {"TZ": "UTC"}
    assert (reader.prefix(), reader.cdup(), reader.object_id_length()) == ("", "", 64)
    assert len(git_calls) == started  # the client's answers
    assert reader.read_only() is reader


def files(directory: Path) -> dict[Path, bytes]:
    """Every file under `directory` (the repository's and its worktree's), with its content."""
    return {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def python_command(script: Path, *args: Path) -> str:
    """A command line for git to run through its shell: `script`, with Python."""
    return " ".join(f'"{path.as_posix()}"' for path in (Path(sys.executable), script, *args))


@pytest.fixture
def fsmonitor(repo: Git, tmp_path: Path) -> Path:
    """repo with a core.fsmonitor hook, a program that git runs as it reads the index, to ask
    what changed (which could write anything): it makes the file this returns, and says
    everything may have."""
    ran = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.py"
    script.write_text(
        "import sys\nopen(sys.argv[1], 'w').close()\nsys.stdout.write('\\0/\\0')\n",
        encoding="utf-8",
    )
    repo.run("config", "core.fsmonitor", python_command(script, ran))
    return ran


def test_a_read_only_clients_names_from_the_index_run_no_fsmonitor(
    repo: Git, fsmonitor: Path
) -> None:
    reader = ReadOnlyGit(repo.directory)
    blob = repo.run("rev-parse", ":a")
    fsmonitor.unlink()

    assert reader.resolve(":a") == blob
    with CatFile(reader) as cat_file:
        assert cat_file.resolve(":a") == blob
    assert not fsmonitor.exists()

    Git(repo.directory).resolve(":a")
    assert fsmonitor.exists()


# 2.45, or 2.39.4 to 2.44.1: older git ignores GIT_NO_LAZY_FETCH, and fetches.
@pytest.mark.needs_git(2, 45, reason="for GIT_NO_LAZY_FETCH")
def test_in_a_partial_clone_a_read_only_client_fetches_no_missing_object(repo: Git) -> None:
    origin = Path(repo.directory)
    repo.run("config", "uploadpack.allowFilter", "true")
    blob = repo.run("rev-parse", "HEAD:a")
    clone = origin.parent / "clone"
    url = origin.as_uri()
    Git(origin.parent).run("clone", "--quiet", "--no-checkout", "--filter=blob:none", url, "clone")
    before = files(clone)

    assert ReadOnlyGit(clone).resolve(blob) is None

    assert files(clone) == before
    assert Git(clone).resolve(blob) == blob  # which a Git fetches
