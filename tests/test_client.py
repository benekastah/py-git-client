import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from git_client import Git, GitError, find_git


@pytest.fixture
def fresh_find_git() -> Iterator[None]:
    """find_git, without what earlier tests found."""
    find_git.cache_clear()
    yield
    find_git.cache_clear()


@pytest.mark.usefixtures("fresh_find_git")
def test_find_git_takes_the_projects_variable_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROJECT_GIT", "/opt/real/git")

    assert find_git("MY_PROJECT_GIT") == "/opt/real/git"


@pytest.mark.usefixtures("fresh_find_git")
def test_find_git_otherwise_finds_git_on_path_by_its_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MY_PROJECT_GIT", raising=False)

    found = Path(find_git("MY_PROJECT_GIT"))

    assert found.is_absolute()
    assert found.stem == "git"


def test_run_returns_stdout_stripped(repo: Git) -> None:
    assert repo.run("rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_failure_raises_with_what_git_said(repo: Git) -> None:
    with pytest.raises(GitError) as raised:
        repo.run("switch", "no-such-branch")

    assert raised.value.command == ["switch", "no-such-branch"]
    assert raised.value.returncode != 0
    assert "no-such-branch" in raised.value.stderr


def test_ok_says_whether_it_succeeded(repo: Git) -> None:
    assert repo.ok("rev-parse", "--verify", "main")
    assert not repo.ok("rev-parse", "--verify", "no-such-branch")


def test_input_and_output_keep_their_line_endings(repo: Git) -> None:
    # On Windows, text mode would write each \n as \r\n; hash-object would then hash those.
    blob = repo.run("hash-object", "-w", "--stdin", input="one\ntwo\n")

    assert repo.run_bytes("cat-file", "blob", blob) == b"one\ntwo\n"


def test_text_that_isnt_utf8_comes_back_as_it_was(repo: Git) -> None:
    blob = repo.run_bytes("hash-object", "-w", "--stdin", input=b"caf\xe9\n").decode().strip()

    assert repo.run("cat-file", "blob", blob) == "caf\udce9"
    assert repo.run_bytes("cat-file", "blob", blob) == b"caf\xe9\n"


def test_env_is_added_to_each_commands_environment(repo: Git) -> None:
    client = Git(repo.directory, env={"GIT_AUTHOR_NAME": "Client"})

    client.run("commit", "--quiet", "--allow-empty", "-m", "by client")
    client.run(
        "commit", "--quiet", "--allow-empty", "-m", "by command", env={"GIT_AUTHOR_NAME": "Command"}
    )

    assert client.run("log", "-2", "--format=%an") == "Command\nClient"


def test_runs_in_its_directory_wherever_we_are(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        Path(repo.run("rev-parse", "--show-toplevel")).resolve() == Path(repo.directory).resolve()
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows passes arguments as str")
def test_a_path_given_as_bytes_reaches_git_as_it_is(repo: Git) -> None:
    name = b"caf\xe9"
    (Path(repo.directory) / name.decode("utf-8", "surrogateescape")).write_bytes(b"x\n")

    repo.run_bytes("add", "--", name)

    assert repo.run_bytes("diff", "--cached", "--name-only", "-z") == name + b"\0"
