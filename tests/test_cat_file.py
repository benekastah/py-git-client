import gc
import io
import os
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

import pytest

from git_client import (
    Arg,
    CatFile,
    CorruptObjectError,
    EnvValue,
    Git,
    GitError,
    GitTimeoutError,
    NotARepositoryError,
    UnknownRevisionError,
    WrongTypeError,
)
from git_client.client import object_header
from git_client.testing import real_git


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


def test_a_name_git_exits_on_starts_no_more_processes(
    repo: Git, git_calls: list[list[str]]
) -> None:
    # One process up to each name git exits on, and one more after it: no other to try the
    # name again, nor to check the repository, when the process that exited says enough.
    with CatFile(repo) as cat_file:
        assert cat_file.resolve("HEAD") is not None
        assert cat_file.resolve("main@{u}") is None
        assert cat_file.resolve("HEAD") is not None
        assert cat_file.resolve("main@{u}^{tree}") is None
        cat_file.close()
        assert cat_file.resolve("main@{u}") is None

    assert git_calls == [["cat-file", "--batch-command"]] * 3


def test_verify_gives_the_id_or_git_s_reason_without_another_process(
    repo: Git, git_calls: list[list[str]]
) -> None:
    with CatFile(repo) as cat_file:
        main = cat_file.verify("main")
        # Names cat-file says nothing of but "missing" (or "ambiguous"): in rev-parse's words.
        with pytest.raises(UnknownRevisionError) as missing:
            cat_file.verify("refs/heads/nope")
        with pytest.raises(UnknownRevisionError) as empty:
            cat_file.verify("")
        # Names git exits on, at a later request, then at a new process's first.
        with pytest.raises(UnknownRevisionError) as no_upstream:
            cat_file.verify("main@{u}")
        with pytest.raises(UnknownRevisionError, match="log for 'HEAD' only has 1 entries"):
            cat_file.verify("HEAD@{999}")

    assert git_calls == [["cat-file", "--batch-command"]] * 2
    assert main == repo.run("rev-parse", "main")
    assert (missing.value.stderr, missing.value.reason) == (
        "fatal: bad revision 'refs/heads/nope'",
        "",
    )
    assert (empty.value.stderr, empty.value.reason) == ("fatal: bad revision ''", "")
    assert no_upstream.value.stderr == "fatal: no upstream configured for branch 'main'"
    assert no_upstream.value.reason == no_upstream.value.stderr


def test_verify_says_a_name_that_names_the_wrong_type_names_something(repo: Git) -> None:
    with CatFile(repo) as cat_file, pytest.raises(WrongTypeError) as raised:
        cat_file.verify("HEAD^{tree}^{commit}")

    assert (raised.value.expected, raised.value.found) == ("commit", "tree")
    assert "expected commit type, but the object dereferences to tree type" in str(raised.value)


def tag_main(git: Git) -> None:
    """Make a tag named main, like the branch: git warns that main is ambiguous, but
    resolves it."""
    git.run("tag", "main")


def test_info_gives_what_git_warned_of_the_name(
    repo_from: Callable[[str, Callable[[Git], None]], Git],
) -> None:
    repo = repo_from("main-tag", tag_main)

    with CatFile(repo) as cat_file:
        ambiguous = cat_file.info("main")
        branch = cat_file.info("refs/heads/main")

    assert ambiguous is not None and branch is not None
    assert ambiguous.warnings == ("warning: refname 'main' is ambiguous.",)
    assert branch.warnings == ()


def warn_as_git_starts(git: Git) -> None:
    """Set a config key git warns of as it starts, as deprecated."""
    git.run("config", "core.fsyncObjectFiles", "true")


@pytest.mark.parametrize("env", [{}, {"GIT_TRACE": "2"}], ids=["config", "trace"])
def test_what_git_says_as_it_starts_is_of_no_name(
    repo_from: Callable[[str, Callable[[Git], None]], Git], env: dict[str, str]
) -> None:
    repo = repo_from("warns-as-git-starts", warn_as_git_starts)
    git = Git(repo.directory, env=env)

    with CatFile(git) as cat_file:
        head = cat_file.info("HEAD")
    with CatFile(git) as cat_file, pytest.raises(UnknownRevisionError) as raised:
        cat_file.verify("nope")

    assert head is not None and head.warnings == ()
    assert (raised.value.stderr, raised.value.reason) == ("fatal: bad revision 'nope'", "")


def break_a_ref(git: Git) -> None:
    """Make refs/heads/broken a ref file that holds no id, which git warns of and ignores."""
    (Path(git.directory) / ".git" / "refs" / "heads" / "broken").write_text("garbage\n")


def test_a_warning_is_no_reason(repo_from: Callable[[str, Callable[[Git], None]], Git]) -> None:
    repo = repo_from("broken-ref", break_a_ref)

    with CatFile(repo) as cat_file, pytest.raises(UnknownRevisionError) as raised:
        cat_file.verify("broken")

    assert raised.value.stderr == (
        "warning: ignoring broken ref refs/heads/broken\nfatal: bad revision 'broken'"
    )
    assert raised.value.reason == ""


def test_verify_gives_only_what_git_said_of_that_name(
    repo_from: Callable[[str, Callable[[Git], None]], Git],
) -> None:
    repo = repo_from("main-tag", tag_main)

    with CatFile(repo) as cat_file:
        assert cat_file.verify("main") == repo.run("rev-parse", "refs/heads/main")
        with pytest.raises(UnknownRevisionError) as raised:
            cat_file.verify("no-such-branch")

    assert raised.value.stderr == "fatal: bad revision 'no-such-branch'"


class StartsKeepingGit(Git):
    """A Git that keeps each long-lived process it starts. It runs git itself: Git for
    Windows' launcher on PATH starts git as a child, which killing the launcher leaves
    running."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory, program=real_git())
        self.started: list[subprocess.Popen[bytes]] = []

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        self.started.append(super().popen(*args, env=env, stderr=stderr))
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


def test_a_cat_file_no_longer_used_stops_its_process(repo: Git) -> None:
    git = StartsKeepingGit(repo.directory)
    cat_file = CatFile(git)
    cat_file.resolve("HEAD")

    del cat_file

    assert git.started[0].poll() is not None


def test_exiting_leaves_a_cat_file_another_thread_uses_alone(repo: Git) -> None:
    # A daemon thread's requests carry on as Python exits, rather than lose the process.
    script = """
import sys, threading, time
from git_client import CatFile, Git
cat_file = CatFile(Git(sys.argv[1], program=sys.argv[2]))
def resolve():
    while True:
        cat_file.resolve("HEAD")
threading.Thread(target=resolve, daemon=True).start()
time.sleep(0.2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, repo.directory, real_git()],
        capture_output=True,
        check=True,
    )

    assert b"Exception in thread" not in result.stderr


class InterruptedPipe(io.BufferedWriter):
    """CatFile's end of its process's stdin, which raises KeyboardInterrupt from a flush (once
    it has sent what was written) while `interrupt` says so, as Ctrl-C could as CatFile waits
    for the answer."""

    def __init__(self, pipe: io.FileIO) -> None:
        super().__init__(pipe)
        self.interrupt = False

    def flush(self) -> None:
        super().flush()
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt


class InterruptibleGit(StartsKeepingGit):
    """A Git whose long-lived processes' stdin is an InterruptedPipe."""

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        process = super().popen(*args, env=env, stderr=stderr)
        assert process.stdin is not None
        pipe = io.FileIO(os.dup(process.stdin.fileno()), "w")
        process.stdin.close()
        process.stdin = InterruptedPipe(pipe)
        return process

    def interrupt(self) -> None:
        """Interrupt the next request, once it's sent."""
        stdin = self.started[-1].stdin
        assert isinstance(stdin, InterruptedPipe)
        stdin.interrupt = True


def test_a_request_interrupted_once_sent_leaves_no_answer_for_the_next(repo: Git) -> None:
    git = InterruptibleGit(repo.directory)
    commit, blob = repo.run("rev-parse", "HEAD"), repo.run("rev-parse", "HEAD:a")

    with CatFile(git) as cat_file:
        cat_file.resolve("HEAD")
        git.interrupt()
        with pytest.raises(KeyboardInterrupt):
            cat_file.resolve("HEAD^{tree}")
        assert cat_file.resolve("HEAD") == commit
        git.interrupt()
        with pytest.raises(KeyboardInterrupt):
            cat_file.read(commit)
        assert cat_file.read(blob) == ("blob", b"a\n")


def test_a_read_interrupted_before_its_content_leaves_none_for_the_next(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content whose first line reads as an object's header: the answer to the next request,
    # if it were left in the pipe.
    blob = repo.run("hash-object", "-w", "--stdin", input=f"{'1' * 40} commit 1\n")

    def interrupted(line: bytes) -> tuple[str, str, int] | None:
        """object_header, interrupted at the header of blob's content (by Ctrl-C, say)."""
        header = object_header(line)
        if header is not None and header[0] == blob:
            raise KeyboardInterrupt
        return header

    with CatFile(repo) as cat_file:
        cat_file.resolve("HEAD")
        with monkeypatch.context() as patched, pytest.raises(KeyboardInterrupt):
            patched.setattr("git_client.cat_file.object_header", interrupted)
            cat_file.read(blob)
        assert cat_file.resolve("HEAD") == repo.run("rev-parse", "HEAD")


def test_a_process_whose_setting_up_fails_is_stopped(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = StartsKeepingGit(repo.directory)

    def cant_read(pipe: IO[bytes]) -> None:
        raise RuntimeError("can't start new thread")  # as Windows' reading thread can

    with CatFile(git) as cat_file:
        with monkeypatch.context() as patched, pytest.raises(RuntimeError):
            patched.setattr("git_client.cat_file._Output", cant_read)
            cat_file.resolve("HEAD")
        assert git.started[0].wait(10) is not None
        assert cat_file.resolve("HEAD") == repo.run("rev-parse", "HEAD")


class KeepsItsCatFile(StartsKeepingGit):
    """A client with a CatFile of its own, which holds it weakly."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.cat_file = CatFile(self, weak=True)


def test_a_client_that_keeps_a_cat_file_stops_it_when_no_longer_used(repo: Git) -> None:
    # By its reference count, rather than whenever the garbage collector gets to it.
    git = KeepsItsCatFile(repo.directory)
    git.cat_file.resolve("HEAD")
    started = git.started
    gc.disable()
    try:
        del git
        assert started[0].poll() is not None
    finally:
        gc.enable()


def test_a_cat_file_whose_git_is_gone_raises(repo: Git) -> None:
    cat_file = CatFile(Git(repo.directory), weak=True)

    with pytest.raises(ReferenceError):
        cat_file.resolve("HEAD")


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


def test_threads_can_share_it(repo: Git) -> None:
    expected: dict[str, str | None] = {
        name: repo.run("rev-parse", name) for name in ("HEAD", "HEAD^{tree}", "HEAD:a")
    }
    expected["no-such-branch"] = None
    wrong: list[tuple[str, str | None]] = []

    def resolve_each(cat_file: CatFile) -> None:
        for _ in range(50):
            for name, object_id in expected.items():
                if (found := cat_file.resolve(name)) != object_id:
                    wrong.append((name, found))

    with CatFile(repo) as cat_file:
        threads = [
            threading.Thread(target=resolve_each, args=(cat_file,), daemon=True) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    assert not any(thread.is_alive() for thread in threads)  # out of step, they'd hang
    assert wrong == []


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


def test_reads_an_object_of_the_type_asked_for(repo: Git) -> None:
    commit, tree = repo.run("rev-parse", "HEAD"), repo.run("rev-parse", "HEAD^{tree}")
    blob = repo.run("rev-parse", "HEAD:a")

    with CatFile(repo) as cat_file:
        assert cat_file.read(blob, "blob") == ("blob", b"a\n")
        # A commit's tree, as `git cat-file tree <commit>` reads it.
        assert cat_file.read(commit, "tree") == cat_file.read(tree)
        with pytest.raises(WrongTypeError) as raised:
            cat_file.read(blob, "commit")
        with pytest.raises(UnknownRevisionError):
            cat_file.read("0" * 40, "commit")
        with pytest.raises(ValueError):  # from a caller whose types aren't checked
            cat_file.read(commit, "HEAD")  # pyright: ignore[reportArgumentType]

    assert (raised.value.expected, raised.value.found) == ("commit", "blob")
    assert "expected commit type, but the object dereferences to blob type" in str(raised.value)


def test_reading_an_object_that_isnt_there_fails(repo: Git) -> None:
    with CatFile(repo) as cat_file:
        with pytest.raises(UnknownRevisionError, match="0{40}"):
            cat_file.read("0" * 40)
        with pytest.raises(ValueError):
            cat_file.read("main")
        # And it's still in step.
        assert cat_file.read(repo.run("rev-parse", "HEAD:a")) == ("blob", b"a\n")


def test_a_corrupt_object_isnt_taken_for_a_missing_one(damaged_repo: Git) -> None:
    commit = damaged_repo.run("rev-parse", "HEAD")
    blob = damaged_repo.run("hash-object", "a")  # HEAD:a would read the commit

    with CatFile(damaged_repo) as cat_file:
        # cat-file exits on it, at a new process's first request.
        with pytest.raises(CorruptObjectError, match=f"object {commit} .* is corrupt"):
            cat_file.resolve("main^{tree}")
        # Loose, it says the name is missing; packed, it exits.
        with pytest.raises(CorruptObjectError):
            cat_file.verify("HEAD~1")
        # Loose, it says the object is missing; packed, it stops after the header.
        with pytest.raises(CorruptObjectError):
            cat_file.read(commit)
        with pytest.raises(CorruptObjectError):
            cat_file.read(commit, "tree")
        # What isn't damaged is read, and what isn't there is still missing.
        assert cat_file.read(blob) == ("blob", b"a\n")
        assert cat_file.resolve("no-such-branch") is None
        assert cat_file.resolve("main@{u}") is None
        with pytest.raises(UnknownRevisionError):
            cat_file.verify("no-such-branch")
        with pytest.raises(UnknownRevisionError):
            cat_file.read("0" * 40)


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


class GitThatCantStart(Git):
    """A git whose long-lived process can't start (e.g. a tool's that's shutting down)."""

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        raise RuntimeError("not now")


def test_a_process_that_cant_start_leaves_no_file_open(repo: Git) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        cat_file = CatFile(GitThatCantStart(repo.directory))
        with pytest.raises(RuntimeError):
            cat_file.resolve("HEAD")
        del cat_file
        gc.collect()

    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


class GitThatDropsStderr(Git):
    """A git whose popen takes `stderr`, but doesn't pass it on."""

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        return super().popen(*args, env=env)


def test_verify_says_so_when_git_said_nothing_of_the_name(repo: Git) -> None:
    with (
        CatFile(GitThatDropsStderr(repo.directory)) as cat_file,
        pytest.raises(UnknownRevisionError) as raised,
    ):
        cat_file.verify("main@{u}")

    assert raised.value.stderr == "fatal: bad revision 'main@{u}': git exited without saying why"
    assert raised.value.reason == ""


class GitWithoutBatchCommand(Git):
    """A git older than 2.36, whose cat-file exits with a usage error at --batch-command."""

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
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
    """A git whose cat-file answers the request CatFile sends first, then says an object has
    100 bytes, and stops after 10."""

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        script = (
            "import sys; sys.stdin.readline();"
            " sys.stdout.write('0' * 40 + ' missing\\n'); sys.stdout.flush();"
            " sys.stdin.readline(); sys.stdout.write(sys.argv[1] + ' blob 100\\n' + 'x' * 10)"
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


class GitThatAnswersOnce(Git):
    """A git whose cat-file answers the first request (and the one CatFile sends first, before
    it) with an object, then runs `then` (Python), whatever it's asked."""

    def __init__(self, directory: str, then: str, *, timeout: float | None = None) -> None:
        super().__init__(directory, timeout=timeout)
        self.then = then
        self.started: list[subprocess.Popen[bytes]] = []

    def popen(
        self,
        *args: Arg,
        env: Mapping[str, EnvValue] | None = None,
        stderr: int | IO[bytes] = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        script = (
            "import os, sys, time\n"
            "for _ in range(2):\n"
            "    sys.stdin.readline()\n"
            "    sys.stdout.write(sys.argv[1] + ' commit 1\\n'); sys.stdout.flush()\n" + self.then
        )
        self.started.append(
            subprocess.Popen(
                [sys.executable, "-c", script, "1" * 40],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        )
        return self.started[-1]


# As a lazy fetch in a partial clone can: git exits once it's done with what it writes.
WRITES_ON = """
try:
    while True:
        sys.stdout.write("x" * 65536)
except OSError:
    os._exit(3)
"""


def test_closing_stops_a_process_writing_what_nothing_reads(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("git_client.cat_file._EXIT_WAIT", 60)  # past which it would be killed
    git = GitThatAnswersOnce(repo.directory, WRITES_ON)
    cat_file = CatFile(git)
    cat_file.resolve("HEAD")

    cat_file.close()

    assert git.started[0].returncode == 3


def test_closing_kills_a_process_that_doesnt_exit(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("git_client.cat_file._EXIT_WAIT", 0.01)
    git = GitThatAnswersOnce(repo.directory, "time.sleep(60)")
    cat_file = CatFile(git)
    cat_file.resolve("HEAD")

    cat_file.close()

    assert git.started[0].returncode not in (None, 0)


@pytest.mark.parametrize(
    "answer", ["", f"{'1' * 40} blob 100\n" + "x" * 10], ids=["nothing", "part of an object"]
)
def test_a_request_past_the_client_s_timeout_stops_the_process(repo: Git, answer: str) -> None:
    then = f"sys.stdin.readline(); sys.stdout.write({answer!r}); sys.stdout.flush(); time.sleep(60)"
    git = GitThatAnswersOnce(repo.directory, then, timeout=0.2)

    with CatFile(git) as cat_file:
        cat_file.resolve("HEAD")
        with pytest.raises(GitTimeoutError, match=f"{'1' * 40}: no answer within 0.2 seconds"):
            cat_file.read("1" * 40)
        assert git.started[0].poll() is not None
        # Another process answers the next.
        assert cat_file.resolve("HEAD") == "1" * 40
    assert len(git.started) == 2
