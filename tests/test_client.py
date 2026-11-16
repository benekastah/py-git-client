import os
import pickle
import subprocess
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows passes the environment as str")
def test_text_in_env_reaches_git_as_utf8_whatever_the_locales_encoding(repo: Git) -> None:
    # In a Python of its own, since the filesystem encoding is set as Python starts: ASCII,
    # in the C locale, when Python neither coerces it nor uses UTF-8 mode. (Its command line
    # is ASCII too, so the name is escaped there.)
    script = (
        "import sys; from git_client import Git; Git(sys.argv[1]).run('commit', '--quiet',"
        " '--allow-empty', '-m', 'x', env={'GIT_AUTHOR_NAME': 'Zo\\u00eb \\u5f20'})"
    )
    subprocess.run(
        [sys.executable, "-c", script, repo.directory],
        env={**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"},
        check=True,
    )

    assert repo.run_bytes("log", "-1", "--format=%an") == "Zoë 张\n".encode()


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


@pytest.mark.usefixtures("fresh_find_git")
def test_find_git_never_takes_a_git_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "git.exe" if sys.platform == "win32" else "git"
    planted, real = tmp_path / "repo", tmp_path / "bin"
    for directory in (planted, real):
        directory.mkdir()
        (directory / name).write_bytes(b"")
        (directory / name).chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(real)]))

    assert find_git() == str(real / name)


@pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows runs a batch file by its extension"
)
@pytest.mark.usefixtures("fresh_find_git")
def test_find_git_never_takes_a_batch_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # cmd.exe would read &, | and % in git's arguments (e.g. a branch name) as its own.
    shims, real = tmp_path / "shims", tmp_path / "bin"
    shims.mkdir()
    real.mkdir()
    for name in ("git.cmd", "git.bat"):
        (shims / name).write_text("@echo off\n", encoding="utf-8")
    (real / "git.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shims), str(real)]))

    assert find_git() == str(real / "git.exe")


@pytest.mark.usefixtures("fresh_find_git")
def test_find_git_fails_rather_than_run_whatever_git_is_nearby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(["", "."]))

    with pytest.raises(FileNotFoundError):
        find_git()


def test_passthrough_sends_gits_output_where_its_told(repo: Git, tmp_path: Path) -> None:
    with (tmp_path / "out").open("wb") as out:
        assert repo.passthrough("rev-parse", "--abbrev-ref", "HEAD", stdout=out)
        assert not repo.passthrough(
            "rev-parse", "--quiet", "--verify", "no-such-branch", stdout=out
        )

    assert (tmp_path / "out").read_bytes().strip() == b"main"


def test_a_git_error_survives_pickling(repo: Git) -> None:
    with pytest.raises(GitError) as raised:
        repo.run("switch", "no-such-branch")

    copy = pickle.loads(pickle.dumps(raised.value))

    assert (copy.command, copy.returncode, copy.stderr) == (
        raised.value.command,
        raised.value.returncode,
        raised.value.stderr,
    )


class ToolError(GitError):
    pass


def test_a_subclass_of_git_error_survives_pickling_with_its_notes() -> None:
    error = ToolError(["status"], 1, "boom")
    error.add_note("while checking the stack")

    copy = pickle.loads(pickle.dumps(error))

    assert type(copy) is ToolError
    assert copy.__notes__ == ["while checking the stack"]
