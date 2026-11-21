import os
import pickle
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from git_client import (
    Arg,
    CatFile,
    ConfigLockedError,
    ConfigResult,
    ConfigSet,
    ConfigUnset,
    CorruptObjectError,
    Git,
    GitError,
    GitTimeoutError,
    Head,
    NoAnswerError,
    NotARepositoryError,
    NotStartedError,
    ReadOnlyError,
    ReadOnlyGit,
    UnknownRevisionError,
    decode,
    find_git,
    git_paths,
    git_paths_and_head,
    printable,
)
from git_client.testing import FakeGit, GitCall, RepoTemplates


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


def test_prefix_is_asked_of_git_once_per_directory_and_environment(
    repo: Git, git_calls: list[list[str]]
) -> None:
    below = Path(repo.directory) / "sub"
    below.mkdir()
    git = Git(repo.directory)

    assert git.prefix() == ""
    assert git.prefix(timeout=5) == ""
    assert len(git_calls) == 1

    # Another directory, or another environment, is another answer.
    git.directory = str(below)
    assert git.prefix() == "sub/"
    git.env["GIT_WORK_TREE"] = str(below)
    git.prefix()
    assert len(git_calls) == 3


def add_places(repo: Git) -> None:
    """Changes for repo_from: a deep subdirectory, a linked worktree (`linked`, beside the
    repository) and a submodule (`mod`) with a subdirectory, and a bare clone beside it
    (`bare.git`)."""
    top = Path(repo.directory)
    (top / "sub" / "deep" / "er").mkdir(parents=True)
    repo.run("worktree", "add", "--quiet", str(top.parent / "linked"))
    (top.parent / "linked" / "sub").mkdir()
    repo.run("-c", "protocol.file.allow=always", "submodule", "--quiet", "add", "./", "mod")
    (top / "mod" / "sub").mkdir()
    Git(top.parent).run("clone", "--quiet", "--bare", "repo", "bare.git")


@pytest.mark.parametrize(
    "where",
    [
        "repo",
        "repo/sub",
        "repo/sub/deep/er",
        "linked",
        "linked/sub",
        "repo/mod/sub",
        "bare.git",
        "repo/.git",
        "repo/.git/objects",
    ],
)
def test_cdup_and_prefix_are_what_git_prints_wherever_the_client_is(
    where: str, repo_from: Callable[[str, Callable[[Git], None]], Git], git_calls: list[list[str]]
) -> None:
    copy = Path(repo_from("places", add_places).directory).parent
    # Outside a worktree whose top it knows, git prints no cdup at all, not an empty line.
    cdup = Git(copy / where).run_bytes("rev-parse", "--show-cdup").removesuffix(b"\n")
    prefix = Git(copy / where).run_bytes("rev-parse", "--show-prefix").removesuffix(b"\n")
    git = Git(copy / where)
    started = len(git_calls)

    assert (git.cdup(), git.prefix()) == (decode(cdup), decode(prefix))
    assert len(git_calls) == started + 1


def test_cdup_outside_the_worktree_is_its_top(repo: Git, tmp_path: Path) -> None:
    git = Git(
        tmp_path,
        env={"GIT_DIR": str(Path(repo.directory) / ".git"), "GIT_WORK_TREE": repo.directory},
    )

    assert Path(git.cdup()) == Path(repo.directory)
    assert git.prefix() == ""


def test_prefix_tells_the_object_id_length_in_the_same_process(
    tmp_path: Path, git_calls: list[list[str]]
) -> None:
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", "sha256")
    git = Git(tmp_path / "sha256")
    started = len(git_calls)

    git.prefix()
    assert git.object_id_length() == 64
    assert len(git_calls) == started + 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' file names can't hold a newline")
def test_prefix_and_cdup_take_a_path_with_a_newline(repo: Git, tmp_path: Path) -> None:
    below = Path(repo.directory) / "a\nb"
    below.mkdir()
    git = Git(below)
    assert (git.prefix(), git.cdup()) == ("a\nb/", "../")
    assert git_paths(git, "HEAD") == [Path(repo.directory) / ".git" / "HEAD"]

    # A worktree whose top's path holds one, from outside it.
    top = tmp_path / "work\ntree"
    top.mkdir()
    env = {"GIT_DIR": str(Path(repo.directory) / ".git"), "GIT_WORK_TREE": str(top)}
    outside = Git(tmp_path, env=env)
    assert git_paths_and_head(outside, "HEAD")[1] == Head(branch="refs/heads/main")
    assert (outside.prefix(), Path(outside.cdup())) == ("", top)


def test_a_failed_prefix_is_not_remembered(tmp_path: Path) -> None:
    git = Git(tmp_path / "later")
    (tmp_path / "later").mkdir()
    with pytest.raises(GitError):
        git.prefix()

    Git(tmp_path).run("init", "--quiet", "later")
    assert git.prefix() == ""


def test_object_id_length_follows_a_change_of_directory(repo: Git, tmp_path: Path) -> None:
    Git(tmp_path).run("init", "--quiet", "--object-format=sha256", "sha256")
    git = Git(repo.directory)
    assert git.object_id_length() == 40

    git.directory = str(tmp_path / "sha256")
    assert git.object_id_length() == 64


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
    # A tool may raise a GitError with no exit status for a command it refused.
    refused = GitError(["fetch"], None, "refused: an untrusted remote")
    timed_out = GitTimeoutError(["fetch"], None, "")

    assert str(refused) == "git fetch failed (no exit status):\nrefused: an untrusted remote"
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


@pytest.mark.parametrize("cause", ["timeout", "stopping"])
def test_no_answer_from_git_isnt_a_git_error(waiting: Git, cause: str) -> None:
    # So `except GitError`, for git's no, doesn't take "git didn't answer" for it.
    if cause == "timeout":
        git = Git(waiting.directory, timeout=0.2)
    else:
        git = Git(waiting.directory, stopping=lambda: True)

    with pytest.raises(NoAnswerError):
        try:
            git.ok("wait")
        except GitError:
            pytest.fail("raised a GitError")


def test_once_stopping_says_so_no_git_starts(repo: Git) -> None:
    stop = threading.Event()
    git = Git(repo.directory, stopping=stop.is_set)
    git.run("branch", "before")
    stop.set()

    with pytest.raises(NotStartedError) as raised:
        git.run("branch", "after")
    with pytest.raises(NotStartedError):
        git.passthrough("branch", "after")

    assert str(raised.value) == "git branch after failed (not started):\nthe tool is stopping"
    assert raised.value.returncode is None
    assert repo.resolve("before") is not None
    assert repo.resolve("after") is None


def test_stopping_starts_no_cat_file_but_leaves_a_running_one(repo: Git) -> None:
    stop = threading.Event()
    git = Git(repo.directory, stopping=stop.is_set)
    with CatFile(git) as running:
        head = running.resolve("HEAD")
        stop.set()

        assert running.resolve("HEAD") == head
        with CatFile(git) as new, pytest.raises(NotStartedError, match="cat-file"):
            new.resolve("HEAD")


def test_a_stopping_read_only_git_still_refuses_what_it_wont_run(repo: Git) -> None:
    # A programming error, which a tool's shutdown mustn't pass off as stopping.
    git = ReadOnlyGit(repo.directory, stopping=lambda: True)

    with pytest.raises(ReadOnlyError):
        git.run("commit", "--allow-empty", "-m", "x")
    with pytest.raises(ReadOnlyError):
        git.popen("commit", "--allow-empty", "-m", "x")


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


def test_resolve_gives_none_for_a_name_git_exits_on_in_one_process(
    repo: Git, git_calls: list[GitCall]
) -> None:
    # main has no upstream: git cat-file exits rather than saying @{u} names nothing.
    assert repo.resolve("main@{u}") is None
    assert len(git_calls) == 1


def test_resolve_reads_a_fakes_answer_after_its_answer_for_the_name_sent_first() -> None:
    git = FakeGit()
    git.respond(["cat-file", "--batch-check"], f"{'0' * 40} missing\n{'a' * 40} commit 100\n")

    assert git.resolve("HEAD") == "a" * 40
    assert git.calls[0].input == b"0" * 40 + b"\nHEAD\n"


def test_resolve_gives_the_id_git_found_even_if_it_found_a_damaged_copy_first() -> None:
    # As after a repack: git reads the object from another pack than the one it found damaged.
    # Real git's words for a damaged object (see the test of a corrupt one), on a fake's answer:
    # which pack git finds an object in can't be chosen.
    git = FakeGit()
    git.respond(
        ["cat-file", "--batch-check"],
        f"{'0' * 40} missing\n{'a' * 40} commit 100\n",
        f"error: unable to unpack {'a' * 40} header\n",
    )

    assert git.resolve("HEAD") == "a" * 40


@pytest.mark.parametrize("stdout", [f"{'a' * 40} commit 100\n", ""])
def test_resolve_raises_for_a_fake_answer_without_one_for_the_name_sent_first(
    stdout: str,
) -> None:
    # As a FakeGit answered cat-file before resolve sent a name of all zeros first: rather
    # than take the id for that name's answer, and say HEAD names nothing.
    git = FakeGit()
    git.respond(["cat-file", "--batch-check"], stdout)

    with pytest.raises(GitError, match="all zeros"):
        git.resolve("HEAD")


def test_resolve_raises_for_a_corrupt_object_rather_than_say_it_names_nothing(
    damaged_repo: Git,
) -> None:
    # Loose, cat-file says the name is missing; packed, it exits.
    with pytest.raises(CorruptObjectError):
        damaged_repo.resolve("HEAD~1")
    with pytest.raises(CorruptObjectError):
        damaged_repo.resolve("main^{tree}")
    assert damaged_repo.resolve("main@{u}") is None


def test_resolve_gives_none_for_a_name_that_isnt_one_line(repo: Git) -> None:
    assert repo.resolve("HEAD\nHEAD") is None
    assert repo.resolve("HEAD\r") is None
    assert repo.resolve("HEAD\0") is None
    assert repo.resolve("") is None


def test_resolve_takes_a_name_that_looks_like_an_option_as_a_name(repo: Git) -> None:
    repo.run("update-ref", "refs/tags/-x", "HEAD")

    assert repo.resolve("-x") == repo.run("rev-parse", "HEAD")


def test_resolve_raises_for_what_isnt_a_name_naming_nothing(
    tmp_path: Path, git_calls: list[GitCall]
) -> None:
    (tmp_path / "plain").mkdir()
    git = Git(tmp_path / "plain", env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})

    with pytest.raises(NotARepositoryError):
        git.resolve("HEAD")
    assert len(git_calls) == 1


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


def locked_config(repo: Git) -> Path:
    """Lock repo's config file, as a process writing it does, and have git fail at once on
    the lock, as git before 2.56 does; the lock file, to remove to release it."""
    repo.run("config", "core.configLockTimeout", "0")
    lock = Path(repo.directory) / ".git" / "config.lock"
    lock.write_bytes(b"")
    return lock


def test_write_configs_waits_while_the_config_file_is_locked(repo: Git) -> None:
    lock = locked_config(repo)
    timer = threading.Timer(0.2, lock.unlink)
    start = time.monotonic()
    timer.start()
    try:
        repo.write_configs(ConfigSet("x.y", "z"))
    finally:
        timer.join()

    assert time.monotonic() - start >= 0.2
    assert repo.run("config", "x.y") == "z"


def test_write_configs_gives_up_once_its_wait_is_over(repo: Git) -> None:
    locked_config(repo)
    start = time.monotonic()

    with pytest.raises(ConfigLockedError):
        repo.write_configs(ConfigSet("x.y", "z"), wait=0.2)

    assert time.monotonic() - start >= 0.2


def test_write_configs_tries_another_failure_once(repo: Git, git_calls: list[list[str]]) -> None:
    with pytest.raises(GitError) as raised:
        repo.write_configs(ConfigSet("no-section", "x"))

    assert raised.value.returncode == 2  # git config's for a key without a section
    assert git_calls == [["config", "no-section", "x"]]


def test_write_configs_of_no_change_starts_no_process(
    repo: Git, git_calls: list[list[str]]
) -> None:
    assert repo.write_configs() == []
    assert git_calls == []


def test_write_configs_says_what_each_change_did(repo: Git) -> None:
    repo.run("config", "--add", "branch.m.x", "1")
    repo.run("config", "--add", "branch.m.x", "2")
    repo.run("config", "branch.m.y", "1")

    results = repo.write_configs(
        ConfigSet("branch.m.z", "new"),
        ConfigUnset("branch.m.y"),
        ConfigUnset("branch.m.y"),  # no longer set
        ConfigUnset("branch.m.x"),  # git won't choose one of its two values
        ConfigUnset("branch.m.x", "3"),
        ConfigUnset("branch.m.x", "2"),
    )

    assert results == [
        ConfigResult.CHANGED,
        ConfigResult.CHANGED,
        ConfigResult.NOT_FOUND,
        ConfigResult.SEVERAL_VALUES,
        ConfigResult.NOT_FOUND,
        ConfigResult.CHANGED,
    ]
    assert repo.run("config", "--get-regexp", "^branch[.]m[.]") == "branch.m.x 1\nbranch.m.z new"


def test_write_configs_raises_what_git_rejects_keeping_the_changes_before(repo: Git) -> None:
    repo.run("config", "--add", "branch.m.x", "1")
    repo.run("config", "--add", "branch.m.x", "2")

    with pytest.raises(GitError) as raised:
        repo.write_configs(ConfigSet("branch.m.y", "1"), ConfigSet("branch.m.x", "3"))

    assert raised.value.returncode == 5  # git config's for a key with several values
    assert repo.run("config", "branch.m.y") == "1"


@pytest.mark.parametrize(("key", "returncode"), [("no-section", 2), ("branch.a.1x", 1)])
def test_write_configs_raises_for_an_unset_git_rejects(
    repo: Git, key: str, returncode: int
) -> None:
    # A key git can't take, not one that isn't set.
    with pytest.raises(GitError) as raised:
        repo.write_configs(ConfigUnset(key))

    assert raised.value.returncode == returncode


def test_write_configs_tells_an_empty_value_to_unset_from_none(repo: Git) -> None:
    repo.run("config", "--add", "branch.m.x", "")
    repo.run("config", "--add", "branch.m.x", "2")
    repo.run("config", "--add", "branch.m.y", "1")
    repo.run("config", "--add", "branch.m.y", "1")

    results = repo.write_configs(ConfigUnset("branch.m.x", ""), ConfigUnset("branch.m.y", "1"))

    # The empty value only; then both of y's values are 1, which git won't choose between.
    assert results == [ConfigResult.CHANGED, ConfigResult.SEVERAL_VALUES]
    assert repo.run("config", "--get-all", "branch.m.x") == "2"


def test_write_configs_tries_an_unset_again_while_locked(repo: Git) -> None:
    repo.run("config", "branch.m.x", "1")
    lock = locked_config(repo)
    with pytest.raises(ConfigLockedError):
        repo.write_configs(ConfigUnset("branch.m.x"), wait=0.2)
    timer = threading.Timer(0.2, lock.unlink)

    timer.start()
    try:
        assert repo.write_configs(ConfigUnset("branch.m.x")) == [ConfigResult.CHANGED]
    finally:
        timer.join()


GOOD = (ConfigSet("branch.m.x", "1"), ConfigUnset("branch.m.y"))


@pytest.mark.parametrize(
    "changes",
    [
        ("--unset", "branch.m.x"),
        (*GOOD, [ConfigSet("branch.m.x", "1")]),
        (*GOOD, "a str"),
        (*GOOD, ("branch.m.x", "1")),
    ],
    ids=["the old form", "a list after changes", "a str after changes", "a tuple after changes"],
)
def test_write_configs_refuses_what_isnt_a_change_before_making_any(
    repo: Git, git_calls: list[list[str]], changes: tuple[object, ...]
) -> None:
    with pytest.raises(TypeError, match="ConfigSet or ConfigUnset"):
        repo.write_configs(*changes)  # pyright: ignore[reportArgumentType]

    assert git_calls == []


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


def test_each_command_gets_the_environment_as_it_is_then(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library keeps the environment it built for git till ours, or the client's, changes.
    def author() -> str:
        return repo.run("var", "GIT_AUTHOR_IDENT").split(" <")[0]

    names = [author()]
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ours")
    names.append(author())
    repo.env["GIT_AUTHOR_NAME"] = "The client's"
    names.append(author())
    del repo.env["GIT_AUTHOR_NAME"]
    monkeypatch.delenv("GIT_AUTHOR_NAME")
    names.append(author())

    assert names == [names[0], "Ours", "The client's", names[0]]
    assert names[0] not in ("Ours", "The client's")


def test_each_command_gets_the_environment_of_an_os_environ_put_in_its_place(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As mock.patch("os.environ", {...}) does.
    environ = {**os.environ, "GIT_AUTHOR_NAME": "First"}
    monkeypatch.setattr(os, "environ", environ)
    names = [repo.run("var", "GIT_AUTHOR_IDENT").split(" <")[0]]
    environ["GIT_AUTHOR_NAME"] = "Second"
    names.append(repo.run("var", "GIT_AUTHOR_IDENT").split(" <")[0])

    assert names == ["First", "Second"]


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
def test_captured_messages_are_in_english_whatever_the_users_language(repo: Git) -> None:
    with pytest.raises(UnknownRevisionError) as raised:
        repo.run("rev-parse", "--verify", "no-such-branch")

    assert "Needed a single revision" in raised.value.stderr


@pytest.mark.usefixtures("german")
def test_passthrough_speaks_the_users_language(repo: Git, tmp_path: Path) -> None:
    with (tmp_path / "out").open("wb") as out:
        repo.passthrough("status", stdout=out)

    assert "Auf Branch main" in (tmp_path / "out").read_text(encoding="utf-8")


@pytest.mark.usefixtures("german")
def test_captured_and_passthrough_commands_keep_their_languages_in_turn(
    repo: Git, tmp_path: Path
) -> None:
    # The environment kept for one isn't the other's.
    said: list[str] = []
    for _ in range(2):
        with (tmp_path / "out").open("wb") as out:
            repo.passthrough("status", stdout=out)
        said.append((tmp_path / "out").read_text(encoding="utf-8"))
        with pytest.raises(UnknownRevisionError):
            repo.run("rev-parse", "--verify", "no-such-branch")

    assert all("Auf Branch main" in each for each in said)


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


@pytest.mark.usefixtures("german")
def test_a_git_that_a_hook_runs_speaks_english_too(repo: Git, tmp_path: Path) -> None:
    # Its environment is the captured git's: GIT_TEXTDOMAINDIR, which git has no other way to
    # be given, reaches what git runs.
    said = tmp_path / "said"
    hook = Path(repo.directory) / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        f'#!/bin/sh\ngit rev-parse --verify no-such-branch 2> "{said.as_posix()}"\nexit 0\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)

    repo.run("commit", "--quiet", "--allow-empty", "-m", "hooked")

    assert "Needed a single revision" in said.read_text(encoding="utf-8")


def test_only_passthrough_reads_the_tools_stdin(repo: Git) -> None:
    # In a Python of its own, given stdin: on Windows, git inherits the process's standard
    # handle, not fd 0, so the tests' process can't stand in for it.
    script = (
        "import sys; from git_client import Git; git = Git(sys.argv[1]);"
        " print(git.run('hash-object', '--stdin'), flush=True);"
        " git.passthrough('hash-object', '--stdin')"
    )
    printed = subprocess.run(
        [sys.executable, "-c", script, repo.directory],
        input=b"the tool's\n",
        capture_output=True,
        check=True,
    ).stdout.split()

    # The ids of an empty blob, and of the tool's stdin.
    assert printed == [
        b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        b"e6f364be0ca5213da8cb6a285b49e8438e386b94",
    ]


def test_importing_the_library_leaves_tempfile_out() -> None:
    # In a Python of its own, as pytest has imported it here: tempfile takes a few
    # milliseconds to import, which every tool would pay at its start.
    script = "import sys, git_client; print('tempfile' in sys.modules)"

    imported = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, check=True
    ).stdout

    assert imported.strip() == b"False"


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


def test_object_id_length_is_read_from_git_once(repo: Git, git_calls: list[GitCall]) -> None:
    assert [repo.object_id_length(), repo.object_id_length()] == [40, 40]
    assert git_calls == [["rev-parse", "--show-object-format"]]


def test_object_id_length_asks_git_once_for_threads_asking_at_once(
    repo: Git, git_calls: list[GitCall]
) -> None:
    failures: list[None] = [None]

    class SlowGit(Git):
        """Takes long enough to answer that the threads all ask before it does, and fails the
        first time."""

        def run(self, *args: Arg, **kwargs: Any) -> str:
            time.sleep(0.05)
            if failures:
                failures.pop()
                raise GitError(args, 1, "failed")
            return super().run(*args, **kwargs)

    git = SlowGit(repo.directory)

    def length(_: int) -> int | None:
        try:
            return git.object_id_length()
        except GitError:
            return None

    with ThreadPoolExecutor(4) as pool:
        lengths = list(pool.map(length, range(4)))

    # The thread that asked first failed; the others waited for it, then one asked again.
    assert sorted(lengths, key=str) == [40, 40, 40, None]
    assert git_calls == [["rev-parse", "--show-object-format"]]


def make_sha256_repo_with_sha1_ids(directory: Path) -> None:
    """A SHA-256 repository, `repo`, with one commit, that also knows each object's SHA-1 id
    (extensions.compatObjectFormat)."""
    Git(directory).run("init", "--quiet", "--object-format=sha256", "repo")
    git = Git(directory / "repo")
    git.run("config", "extensions.compatObjectFormat", "sha1")
    git.run("commit", "--quiet", "--allow-empty", "-m", "first")


def resolve_with_cat_file(git: Git, name: str) -> str | None:
    with CatFile(git) as cat_file:
        return cat_file.resolve(name)


@pytest.mark.needs_git(2, 46, reason="extensions.compatObjectFormat")
@pytest.mark.parametrize(
    "resolve", [Git.resolve, resolve_with_cat_file], ids=["resolve", "CatFile"]
)
def test_object_id_length_is_the_repositorys_whatever_ids_were_read(
    resolve: Callable[[Git, str], str | None], repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    try:
        repo_templates.build("sha256 with sha1 ids", make_sha256_repo_with_sha1_ids)
    except GitError as error:
        # As the git on macOS CI says: it's built without what makes the SHA-1 ids.
        if "compatibility hash algorithm support requires Rust" not in error.stderr:
            raise
        pytest.skip("this git can't keep SHA-1 ids beside SHA-256 ones (it's built without Rust)")
    git = Git(repo_templates.copy("sha256 with sha1 ids", tmp_path / "copy") / "repo")
    sha1 = git.run("rev-parse", "--output-object-format=sha1", "HEAD")

    assert resolve(git, sha1) == sha1  # git gives a SHA-1 id back for one
    assert git.object_id_length() == 64


def test_a_failed_object_id_length_is_not_remembered(tmp_path: Path) -> None:
    git = Git(tmp_path / "later")
    (tmp_path / "later").mkdir()
    with pytest.raises(GitError):
        git.object_id_length()

    Git(tmp_path).run("init", "--quiet", "later")
    assert git.object_id_length() == 40


@pytest.mark.parametrize("client", [Git, ReadOnlyGit, FakeGit])
def test_object_id_length_given_starts_no_git(
    client: type[ReadOnlyGit], tmp_path: Path, git_calls: list[GitCall]
) -> None:
    # Not a repository: git, if asked, would fail.
    git = client(tmp_path, object_id_length=64)
    assert git.object_id_length() == 64
    assert git_calls == []


@pytest.mark.parametrize("length", [0, 39, 41, 63])
def test_object_id_length_given_is_40_or_64(length: int) -> None:
    with pytest.raises(ValueError, match="40 or 64"):
        Git(object_id_length=length)
