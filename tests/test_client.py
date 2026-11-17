import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from git_client import (
    CatFile,
    Git,
    GitError,
    GitTimeoutError,
    NotARepositoryError,
    ReadOnlyGit,
    UnknownRevisionError,
    decode,
    find_git,
    printable,
)


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


def test_run_can_leave_the_output_as_git_wrote_it(repo: Git) -> None:
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    assert repo.run("status", "--porcelain", strip=False) == " M a\n"
    assert repo.run("status", "--porcelain") == "M a"


def test_prefix_is_the_path_from_the_top_of_the_worktree_down_to_the_directory(
    repo: Git,
) -> None:
    # With a space at its start, which stripping git's output would lose.
    below = Path(repo.directory) / " a" / "b"
    below.mkdir(parents=True)

    assert Git(below).prefix() == " a/b/"
    assert repo.prefix() == ""
    assert Git(Path(repo.directory) / ".git").prefix() == ""


def test_a_failure_raises_with_what_git_said(repo: Git) -> None:
    with pytest.raises(GitError) as raised:
        repo.run("switch", "no-such-branch")

    assert raised.value.command == ["switch", "no-such-branch"]
    assert raised.value.returncode != 0
    assert "no-such-branch" in raised.value.stderr


def test_a_failures_message_cuts_a_long_command_short() -> None:
    error = GitError(["log", "--", *(f"file{i}" for i in range(100))], 1, "boom")

    first_line = str(error).splitlines()[0]
    assert first_line.startswith("git log -- file0 file1 ")
    assert first_line.endswith("… failed (1):")
    assert len(first_line) == len("git ") + 200 + len(" failed (1):")
    assert len(error.command) == 102


def test_a_failures_message_says_timed_out_only_of_a_timeout() -> None:
    # A tool may raise a GitError with no exit status for a command it never ran.
    not_run = GitError(["fetch"], None, "not run: the tool is shutting down")
    timed_out = GitTimeoutError(["fetch"], None, "")

    assert str(not_run) == "git fetch failed (no exit status):\nnot run: the tool is shutting down"
    assert str(timed_out) == "git fetch failed (timed out):\n"
    assert str(pickle.loads(pickle.dumps(timed_out))) == str(timed_out)


def test_a_failures_message_can_be_written_whatever_bytes_git_was_given() -> None:
    # A path that isn't UTF-8, as decode gives it: its byte as a surrogate, which UTF-8 can't
    # write.
    error = GitError(["add", "caf\udce9"], 128, "fatal: pathspec 'caf\udce9' did not match\n")

    assert str(error).encode("utf-8") == (
        b"git add caf\\xe9 failed (128):\nfatal: pathspec 'caf\\xe9' did not match"
    )
    assert (error.command, error.stderr) == (
        ["add", "caf\udce9"],
        "fatal: pathspec 'caf\udce9' did not match\n",
    )
    assert str(GitError(["add", "\ud800"], 128, "")).startswith("git add \\ud800 failed")


def test_a_failure_keeps_what_git_printed_to_stdout(repo: Git) -> None:
    with pytest.raises(GitError) as raised:
        repo.run("show-ref", "--verify", "refs/heads/main", "refs/heads/nope")

    assert raised.value.stdout.startswith(repo.run("rev-parse", "main"))


@pytest.fixture
def waiting(repo: Git) -> Git:
    """repo, with aliases that run past a timeout of 0.2s: `git wait`, for 10s, and `git
    pause`, for 0.3s."""
    for alias, seconds in (("wait", 10), ("pause", 0.3)):
        sleep = f'"{Path(sys.executable).as_posix()}" -c "import time; time.sleep({seconds})"'
        repo.run("config", f"alias.{alias}", f"!{sleep}")
    return repo


def test_a_command_past_the_clients_timeout_raises(waiting: Git) -> None:
    with pytest.raises(GitTimeoutError) as raised:
        Git(waiting.directory, timeout=0.2).run("wait")

    assert raised.value.command == ["wait"]
    assert raised.value.returncode is None


def test_a_commands_own_timeout_takes_the_clients_place(waiting: Git) -> None:
    client = Git(waiting.directory, timeout=0.2)

    with pytest.raises(GitTimeoutError):
        waiting.ok("wait", timeout=0.2)
    assert client.run("pause", timeout=None) == ""
    assert client.passthrough("pause", timeout=10)


def test_succeeds_raises_rather_than_say_no_past_a_timeout(waiting: Git) -> None:
    with pytest.raises(GitTimeoutError):
        waiting.succeeds("wait", timeout=0.2)


def test_resolve_gives_the_object_a_name_names_or_none(repo: Git) -> None:
    assert repo.resolve("main") == repo.run("rev-parse", "HEAD")
    assert repo.resolve("no-such-branch") is None


def test_resolve_gives_none_for_an_object_the_repository_lacks(repo: Git) -> None:
    # A submodule's commit, which is in the submodule's repository, not this one.
    repo.run("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},mod")
    repo.run("commit", "--quiet", "-m", "add mod")

    assert repo.resolve("0123" * 10) is None
    assert repo.resolve("HEAD:mod") is None
    assert repo.resolve("HEAD:a") == repo.run("rev-parse", "HEAD:a")


def test_resolve_gives_none_for_a_name_git_exits_on(repo: Git) -> None:
    # main has no upstream: git cat-file exits rather than saying @{u} names nothing.
    assert repo.resolve("main@{u}") is None


def test_resolve_gives_none_for_a_name_that_isnt_one_line(repo: Git) -> None:
    assert repo.resolve("HEAD\nHEAD") is None
    assert repo.resolve("HEAD\r") is None
    assert repo.resolve("HEAD\0") is None
    assert repo.resolve("") is None


def test_resolve_takes_a_name_that_looks_like_an_option_as_a_name(repo: Git) -> None:
    repo.run("update-ref", "refs/tags/-x", "HEAD")

    assert repo.resolve("-x") == repo.run("rev-parse", "HEAD")


def test_resolve_raises_for_what_isnt_a_name_naming_nothing(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    git = Git(tmp_path / "plain", env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})

    with pytest.raises(NotARepositoryError):
        git.resolve("HEAD")


def test_var_gives_gits_value_for_a_variable(repo: Git) -> None:
    ident = repo.var("GIT_AUTHOR_IDENT")

    assert ident is not None
    assert ident.startswith("Test <test@example.com> ")


@pytest.mark.parametrize(
    "identity",
    [
        "",  # unable to auto-detect email address, unless this machine's name gives one
        "[user]\n\tuseConfigOnly = true\n",  # no email was given
        "[user]\n\tuseConfigOnly = true\n\temail = a@example.com\n",  # no name was given
        "[user]\n\tname =\n\temail = a@example.com\n",  # empty ident name
    ],
)
def test_var_gives_none_when_git_has_no_value_for_it(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config").write_text(identity, encoding="utf-8")
    anonymous = Git(repo.directory, env={"GIT_CONFIG_GLOBAL": str(tmp_path / "config")})
    if (
        not identity
        and anonymous.process("var", "GIT_AUTHOR_IDENT", capture_output=True).returncode == 0
    ):
        pytest.skip("git finds an email address from this machine's name")

    assert anonymous.var("GIT_AUTHOR_IDENT") is None


def test_var_raises_for_what_isnt_a_missing_value(repo: Git) -> None:
    with pytest.raises(GitError):
        repo.var("NO_SUCH_VARIABLE")
    with (Path(repo.directory) / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write("[core\n")
    with pytest.raises(GitError, match="bad config line"):
        repo.var("GIT_AUTHOR_IDENT")


def test_ok_answers_yes_for_exit_0_and_no_for_exit_1(
    repo: Git, capfd: pytest.CaptureFixture[str]
) -> None:
    repo.run("commit", "--quiet", "--allow-empty", "-m", "second")
    (Path(repo.directory) / "a").write_bytes(b"changed\n")

    assert repo.ok("merge-base", "--is-ancestor", "HEAD~1", "HEAD")
    assert not repo.ok("merge-base", "--is-ancestor", "HEAD", "HEAD~1")
    assert repo.ok("diff", "--quiet", "--cached")
    assert not repo.ok("diff", "--quiet")
    assert not repo.ok("config", "--get", "no.such-key")
    assert capfd.readouterr() == ("", "")  # git's output discarded


def test_ok_raises_for_a_failure(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError) as raised:
        repo.ok("merge-base", "--is-ancestor", "no-such-branch", "HEAD")

    assert raised.value.returncode == 128
    assert "no-such-branch" in raised.value.stderr


def test_succeeds_answers_yes_for_exit_0_and_no_for_any_other(
    repo: Git, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert repo.succeeds("rev-parse", "--verify", "HEAD")
    assert not repo.succeeds("config", "--get", "no.such-key")  # exit 1
    assert not repo.succeeds("merge-base", "--is-ancestor", "no-such-branch", "HEAD")  # 128
    assert capfd.readouterr() == ("", "")  # git's output, and its fatal: line, discarded
    assert not ReadOnlyGit(tmp_path / "no-such-directory").succeeds("status")


def test_input_and_output_keep_their_line_endings(repo: Git) -> None:
    # On Windows, text mode would write each \n as \r\n; hash-object would then hash those.
    blob = repo.run("hash-object", "-w", "--stdin", input="one\ntwo\n")

    assert repo.run_bytes("cat-file", "blob", blob) == b"one\ntwo\n"


def test_text_that_isnt_utf8_comes_back_as_it_was(repo: Git) -> None:
    blob = repo.run_bytes("hash-object", "-w", "--stdin", input=b"caf\xe9\n").decode().strip()

    assert repo.run("cat-file", "blob", blob) == "caf\udce9"
    assert repo.run_bytes("cat-file", "blob", blob) == b"caf\xe9\n"


def test_printable_replaces_the_bytes_that_arent_utf8() -> None:
    assert printable(decode(b"caf\xe9 \xc3\xa9t\xc3\xa9")) == "caf\ufffd \u00e9t\u00e9"


def test_printable_replaces_any_other_surrogate_too() -> None:
    # Not from decode: e.g. from json.loads('"\\ud800"').
    assert printable("caf\udce9 \ud800") == "caf\ufffd \ufffd"
    # A truncated sequence is one U+FFFD, as without the other surrogate.
    assert printable(decode(b"\xf0\x9f") + "\ud800") == "\ufffd\ufffd"


def test_env_is_added_to_each_commands_environment(repo: Git) -> None:
    client = Git(repo.directory, env={"GIT_AUTHOR_NAME": "Client"})

    client.run("commit", "--quiet", "--allow-empty", "-m", "by client")
    client.run(
        "commit", "--quiet", "--allow-empty", "-m", "by command", env={"GIT_AUTHOR_NAME": "Command"}
    )

    assert client.run("log", "-2", "--format=%an") == "Command\nClient"


def test_text_reaches_git_as_utf8(repo: Git) -> None:
    repo.run(
        "commit", "--quiet", "--allow-empty", "-m", "Zoë 张", env={"GIT_AUTHOR_NAME": "Zoë 张"}
    )

    assert repo.run_bytes("log", "-1", "--format=%an %s") == "Zoë 张 Zoë 张\n".encode()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows passes text to git as str")
def test_text_reaches_git_as_utf8_whatever_the_locales_encoding(repo: Git) -> None:
    # In a Python of its own, since the filesystem encoding is set as Python starts: ASCII,
    # in the C locale, when Python neither coerces it nor uses UTF-8 mode. (Its command line
    # is ASCII too, so the name is escaped there.)
    script = (
        "import sys; from git_client import Git; Git(sys.argv[1]).run('commit', '--quiet',"
        " '--allow-empty', '-m', 'Zo\\u00eb \\u5f20', env={'GIT_AUTHOR_NAME': 'Zo\\u00eb \\u5f20'})"
    )
    subprocess.run(
        [sys.executable, "-c", script, repo.directory],
        env={**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"},
        check=True,
    )

    assert repo.run_bytes("log", "-1", "--format=%an %s") == "Zoë 张 Zoë 张\n".encode()


@pytest.fixture
def german(repo: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who reads git in German, if git's translations are installed."""
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    said = subprocess.run(
        [repo.program, "-C", repo.directory, "rev-parse", "--verify", "no-such-branch"],
        capture_output=True,
        check=False,
    ).stderr
    if b"Needed a single revision" in said:
        pytest.skip("git's German translation isn't installed")


@pytest.mark.usefixtures("german")
def test_captured_messages_are_in_the_users_language(repo: Git) -> None:
    # The library has no German patterns, so it can't say which failure this is.
    with pytest.raises(GitError) as raised:
        repo.run("rev-parse", "--verify", "no-such-branch")

    assert type(raised.value) is GitError
    assert "Benötigte einen einzelnen Commit" in raised.value.stderr


@pytest.mark.usefixtures("german")
def test_answers_read_from_exit_statuses_dont_depend_on_the_users_language(repo: Git) -> None:
    assert repo.resolve("no-such-branch") is None
    assert repo.resolve("main@{u}") is None  # cat-file exits, then rev-parse --quiet checks
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("no-such-branch") is None
        assert cat_file.resolve("main@{u}") is None
    assert not repo.ok("config", "--get", "no.such")
    assert not repo.succeeds("rev-parse", "--verify", "no-such-branch")


@pytest.mark.usefixtures("german")
def test_passthrough_speaks_the_users_language(repo: Git, tmp_path: Path) -> None:
    with (tmp_path / "out").open("wb") as out:
        repo.passthrough("status", stdout=out)

    assert "Auf Branch main" in (tmp_path / "out").read_text(encoding="utf-8")


def test_what_git_runs_keeps_the_users_locale(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only recorded, so it needn't be installed. Hooks run in git's sh on every system.
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    seen = tmp_path / "seen"
    hook = Path(repo.directory) / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\necho "$LC_ALL $LANG" > "{seen.as_posix()}"\n', encoding="utf-8")
    hook.chmod(0o755)

    repo.run("commit", "--quiet", "--allow-empty", "-m", "hooked")

    assert seen.read_text(encoding="utf-8").strip() == "de_DE.UTF-8 fr_FR.UTF-8"


def test_runs_in_its_directory_wherever_we_are(
    repo: Git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        Path(repo.run("rev-parse", "--show-toplevel")).resolve() == Path(repo.directory).resolve()
    )


def test_a_path_given_as_bytes_reaches_git(repo: Git) -> None:
    name = "café".encode()
    (Path(repo.directory) / "café").write_bytes(b"x\n")

    repo.run_bytes("add", "--", name)

    assert repo.run_bytes("diff", "--cached", "--name-only", "-z") == name + b"\0"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows passes arguments as str")
@pytest.mark.skipif(sys.platform == "darwin", reason="macOS refuses a file name that isn't UTF-8")
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


@pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows runs a batch file by its extension"
)
@pytest.mark.usefixtures("fresh_find_git")
@pytest.mark.parametrize("name", ["git-ai.cmd", "GIT.BAT"])
def test_find_git_refuses_a_batch_file_the_projects_variable_names(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # As on PATH: cmd.exe would read &, | and % in git's arguments as its own.
    monkeypatch.setenv("MY_PROJECT_GIT", f"C:\\tools\\{name}")

    with pytest.raises(ValueError, match="MY_PROJECT_GIT"):
        find_git("MY_PROJECT_GIT")


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
        repo.run("show-ref", "--verify", "refs/heads/main", "refs/heads/nope")

    copy = pickle.loads(pickle.dumps(raised.value))

    assert (copy.command, copy.returncode, copy.stderr, copy.stdout) == (
        raised.value.command,
        raised.value.returncode,
        raised.value.stderr,
        raised.value.stdout,
    )


class ToolError(GitError):
    pass


def test_a_subclass_of_git_error_survives_pickling_with_its_notes() -> None:
    error = ToolError(["status"], 1, "boom")
    error.add_note("while checking the stack")

    copy = pickle.loads(pickle.dumps(error))

    assert type(copy) is ToolError
    assert copy.__notes__ == ["while checking the stack"]
