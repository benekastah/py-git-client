"""Git.write_config, and the config file editor behind it, checked against `git
config`'s own edits of a copy of the same file."""

import errno
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from git_client import ConfigLockedError, Git, GitError, NotStartedError, config, decode, git_paths
from git_client.config_file import ConfigChange, ConfigResult, ConfigSet, ConfigUnset, edit
from git_client.testing import FakeGit, GitCall

type Case = tuple[bytes | None, list[ConfigChange]]

MAIN = b'[core]\n\tbare = false\n[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
VALUES = [
    *("", " lead", "trail ", "  ", "a b", "h#sh", "s;c", 'q"uote', '"', "back\\slash", "end\\"),
    *("new\nline", "\n", "t\tab", "x = y", "[x]", "ü", "a # b ; c", "\\n"),
]
NAMES = ["fix/a.b", 'q"uote', "back\\slash", "Üñí", "UPPER", "a.b.c", " sp ace ", "#;"]
TWO = b'[branch "m"]\n\tx = 1\n\tx = 2\n'

# Files git can't read.
UNREADABLE = [
    *(b"[branch", b"[a/b]", b'[branch\n"a"]', b"[branch a]", b'[branch "a\n"]', b'[branch "a"x'),
    *(b'[branch "a\\\n"]', b'[branch "a"\n', b'[branch "a"]\n\tx y\n', b"!", b"# c\n [] # c\n"),
]
CASES: dict[str, Case] = {
    "a new key": (MAIN, [ConfigSet("branch.main.base", "x")]),
    "a new value": (MAIN, [ConfigSet("branch.main.remote", "upstream")]),
    "a new section": (MAIN, [ConfigSet("branch.topic.base", "main")]),
    "an unset": (MAIN, [ConfigUnset("branch.main.merge")]),
    "the last unsets": (
        MAIN,
        [ConfigUnset("branch.main.remote"), ConfigUnset("branch.main.merge")],
    ),
    "the last unset, mid-file": (
        b'[branch "a"]\n\tx = 1\n\n[core]\n\tbare = false\n',
        [ConfigUnset("branch.a.x")],
    ),
    "nothing to unset": (MAIN, [ConfigUnset("branch.main.no"), ConfigUnset("branch.no.x", "1")]),
    "keys typed in any case": (
        MAIN,
        [
            ConfigSet("Branch.main.Remote", "u"),
            ConfigSet("BRANCH.New.Key", "v"),
            ConfigUnset("branch.main.MERGE"),
        ],
    ),
    "a subsection's case counts": (
        MAIN,
        [ConfigSet("branch.Main.remote", "u"), ConfigUnset("branch.MAIN.merge")],
    ),
    "several values": (
        TWO,
        [
            ConfigUnset("branch.m.x"),
            ConfigUnset("branch.m.x", "3"),
            ConfigUnset("branch.m.x", "2"),
            ConfigSet("branch.m.x", "4"),
            ConfigUnset("branch.m.x", "4"),
        ],
    ),
    "several equal values": (b'[branch "m"]\n\tx = 1\n\tx = 1\n', [ConfigUnset("branch.m.x", "1")]),
    "a key with no value": (
        b'[branch "v"]\n\tflag\n\tx = 2\n\tother\n',
        [
            ConfigUnset("branch.v.x", "2"),
            ConfigUnset("branch.v.other", "any"),
            ConfigUnset("branch.v.flag"),
        ],
    ),
    "comments": (
        b'# top\n[branch "c"]\n\tx = 1 ; trailing\n# inside\n\ty = 2\n',
        [ConfigUnset("branch.c.x"), ConfigUnset("branch.c.y"), ConfigSet("branch.c.z", "3")],
    ),
    "a comment before the section": (
        b'[core]\n\tbare = false\n# about c\n[branch "c"]\n\tx = 1\n',
        [ConfigUnset("branch.c.x")],
    ),
    "a comment after the section": (
        b'[branch "c"]\n\tx = 1\n; about core\n[core]\n',
        [ConfigUnset("branch.c.x")],
    ),
    "odd spacing": (
        b'\n  [branch  "s"]  \n\n   x=1\n\n\ty   =   2   \n\n[core]\n',
        [ConfigSet("branch.s.x", "one"), ConfigUnset("branch.s.y"), ConfigSet("branch.s.z", "3")],
    ),
    "a section in two places": (
        b'[branch "d"]\n\tx = 1\n[core]\n\tbare = false\n[branch "d"]\n\ty = 2\n[branch "e"]\n',
        [
            ConfigSet("branch.d.z", "3"),
            ConfigUnset("branch.d.x"),
            ConfigUnset("branch.d.y"),
            ConfigUnset("branch.d.z"),
        ],
    ),
    "continued and quoted values": (
        b'[branch "l"]\n\tx = one \\\n two\n\ty = "a ; b" # c\n\tz = 3\n',
        [
            ConfigSet("branch.l.x", "1"),
            ConfigUnset("branch.l.y", "a ; b"),
            ConfigUnset("branch.l.z"),
        ],
    ),
    "no newline at the end": (
        b'[branch "a"]\n\tx = 1',
        [ConfigSet("branch.a.y", "2"), ConfigSet("branch.b.x", "3")],
    ),
    "a header at the end": (b'[core]\n[branch "e"]', [ConfigSet("branch.e.x", "1")]),
    "an empty section": (
        b'[branch "e"]\n[core]\n',
        [ConfigSet("branch.e.x", "1"), ConfigUnset("branch.e.x")],
    ),
    "an empty file": (b"", [ConfigUnset("branch.a.x"), ConfigSet("branch.a.x", "1")]),
    "no file": (
        None,
        [ConfigUnset("branch.a.x"), ConfigSet("Branch.a.X", "1"), ConfigSet("branch.b.y", "2")],
    ),
    "no file, nothing set": (None, [ConfigUnset("branch.a.x")]),
    "mixed line endings": (
        b'[branch "a"]\r\n\tx = 1\r\n\r\n\ty = 2\n',
        [ConfigSet("branch.a.z", "3"), ConfigUnset("branch.a.y")],
    ),
    "headers git reads, oddly": (b'[ "x"]\n[.]\n[branch.]\n', [ConfigSet("branch.a.x", "1")]),
    "an include": (b"[include]\n\tpath = other\n", [ConfigSet("branch.i.x", "1")]),
    "a byte order mark": (
        b'\xef\xbb\xbf[branch "b"]\n\tx = 1\n',
        [ConfigSet("branch.b.y", "2"), ConfigUnset("branch.b.x")],
    ),
    "headers written otherwise": (
        b'[Branch "a"]\n\tx = 1\n[branch\t"q\\"x"]\n\tx = 1\n[branch.other]\n\tx = 1\n',
        [ConfigSet("branch.a.y", "2"), ConfigSet('branch.q"x.y', "2"), ConfigUnset('branch.q"x.x')],
    ),
    "tricky names": (
        MAIN,
        [ConfigSet(f"branch.{name}.{key}", name) for name in NAMES for key in ("x", "y")]
        + [ConfigUnset(f"branch.{name}.x") for name in NAMES],
    ),
    "tricky values": (MAIN, [ConfigSet(f"branch.v.k{i}", value) for i, value in enumerate(VALUES)]),
    "tricky values replaced": (
        MAIN,
        [ConfigSet("branch.v.k", value) for value in VALUES]
        + [ConfigUnset("branch.v.k", VALUES[-1])],
    ),
}


def git_edits(
    directory: Path, contents: bytes | None, changes: Sequence[ConfigChange]
) -> tuple[bytes | None, list[ConfigResult]]:
    """What sequential `git config --file` calls make of a file, and what each did."""
    path = directory / "config-by-git"
    if contents is not None:
        path.write_bytes(contents)
    results: list[ConfigResult] = []
    for change in changes:
        if isinstance(change, ConfigSet):
            args = [change.key, change.value]
        else:
            args = ["--unset", *(["--fixed-value"] if change.value is not None else [])]
            args += [change.key] + ([change.value] if change.value is not None else [])
        done = Git(directory).process("config", "--file", str(path), *args, capture_output=True)
        if done.returncode == 0:
            results.append(ConfigResult.CHANGED)
        else:
            assert done.returncode == 5, decode(done.stderr)
            several = b"has multiple values" in done.stderr
            results.append(ConfigResult.SEVERAL_VALUES if several else ConfigResult.NOT_FOUND)
    return (path.read_bytes() if path.exists() else None), results


# What older git can't do: before 2.47, `git config --unset --fixed-value` of a key with no
# value crashes (config.c's matches() compares the value it lacks).
CASES_NEED_GIT = {"a key with no value": pytest.mark.needs_git(2, 47, reason="git crashes before")}


@pytest.mark.parametrize(
    ("contents", "changes"),
    [
        pytest.param(*case, id=name, marks=CASES_NEED_GIT.get(name, ()))
        for name, case in CASES.items()
    ],
)
def test_edit_makes_the_file_git_config_makes(
    tmp_path: Path, contents: bytes | None, changes: list[ConfigChange]
) -> None:
    assert edit(contents, changes) == git_edits(tmp_path, contents, changes)


def test_edit_keeps_a_files_crlf_line_endings_where_git_writes_lf(tmp_path: Path) -> None:
    contents = b'[core]\r\n\tbare = false\r\n\r\n[branch "w"]\r\n\tx = 1\r\n\r\n\ty = 2'
    changes: list[ConfigChange] = [
        ConfigSet("branch.w.x", "one"),
        ConfigSet("branch.w.z", "3"),
        ConfigUnset("branch.w.y"),
        ConfigSet("branch.new.x", "1"),
    ]
    by_git, git_results = git_edits(tmp_path, contents.replace(b"\r\n", b"\n"), changes)
    assert by_git is not None

    assert edit(contents, changes) == (by_git.replace(b"\n", b"\r\n"), git_results)


def test_edit_writes_values_git_reads_back(tmp_path: Path) -> None:
    values = [*VALUES, "cr\rx", "cr at the end\r"]
    edited = edit(MAIN, [ConfigSet(f"branch.v.k{i}", value) for i, value in enumerate(values)])
    assert edited is not None
    (tmp_path / "config").write_bytes(edited[0] or b"")

    listed = Git(tmp_path).run_bytes("config", "--file", "config", "--list", "-z")

    assert [decode(entry).partition("\n")[2] for entry in listed.split(b"\0")[3:-1]] == values


@pytest.mark.parametrize(
    ("contents", "change"),
    [
        (MAIN, ConfigSet("core.bare", "true")),
        (MAIN, ConfigUnset("branch.main")),
        (MAIN, ConfigSet("branch.main.1x", "v")),
        (MAIN, ConfigSet("branch..x", "v")),
        (MAIN, ConfigSet("branch.a\nb.x", "v")),
        (MAIN, ConfigSet("branch.a.x", "v\0")),
        # git reads a value only up to a NUL.
        (b'[branch "a"]\n\tx = a\0b\n', ConfigUnset("branch.a.x", "a")),
        *((bad, ConfigSet("branch.a.x", "v")) for bad in UNREADABLE),
        (b'[branch "a"]\n\tx = "open\n', ConfigSet("branch.a.x", "v")),
        (b'[branch "a"]\n\tx = \\q\n', ConfigSet("branch.b.x", "v")),
        # git puts a key in a [section.subsection] header whatever its subsection's case.
        (b"[branch.a]\n\tx = 1\n", ConfigSet("branch.a.y", "v")),
        (b"[branch.a]\n\tx = 1\n", ConfigSet("branch.A.y", "v")),
        (b'[branch.a "b"]\n\tx = 1\n', ConfigSet("branch.a.b.y", "v")),
        (b'[branch "a"] x = 1\n', ConfigSet("branch.a.y", "v")),
        (b'[core] [branch "a"]\n\tx = 1\n', ConfigSet("branch.a.y", "v")),
        (b'[branch "a"] # c\n\tx = 1\n', ConfigUnset("branch.a.x")),
        (TWO, ConfigSet("branch.m.x", "v")),
        # git before 2.45 reads a tab inside a value as a space.
        (b'[branch "a"]\n\tx = a\tb\n', ConfigUnset("branch.a.x", "a b")),
    ],
)
def test_edit_leaves_to_git_what_it_cant_be_sure_of(contents: bytes, change: ConfigChange) -> None:
    assert edit(contents, [ConfigSet("branch.ok.x", "1"), change]) is None


def config_file(repo: Git) -> Path:
    return Path(repo.directory) / ".git" / "config"


def test_write_config_makes_a_batch_as_git_config_would_with_no_process(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    changes: list[ConfigChange] = [
        *CASES["tricky names"][1],
        ConfigSet("branch.main.remote", "origin"),
        ConfigUnset("branch.main.no-such-key"),
    ]
    before = config_file(repo).read_bytes()

    results = repo.write_config(*changes)
    repo.write_config(*changes[-2:])

    assert [call[0] for call in git_calls] == ["rev-parse"]  # git_paths, then remembered
    assert (config_file(repo).read_bytes(), results) == git_edits(tmp_path, before, changes)


UNSETS: list[tuple[ConfigChange, ConfigResult]] = [
    (ConfigUnset("branch.m.y"), ConfigResult.CHANGED),
    (ConfigUnset("branch.m.y"), ConfigResult.NOT_FOUND),
    (ConfigUnset("branch.m.x"), ConfigResult.SEVERAL_VALUES),
    (ConfigUnset("branch.m.x", "3"), ConfigResult.NOT_FOUND),
    (ConfigUnset("branch.m.x", "2"), ConfigResult.CHANGED),
    (ConfigUnset("branch.m.x", "1"), ConfigResult.CHANGED),
]


@pytest.mark.parametrize("by_git", [False, True], ids=["itself", "falling back to git"])
def test_write_config_tells_unsets_apart_as_git_does(
    repo: Git, git_calls: list[GitCall], by_git: bool
) -> None:
    config_file(repo).write_bytes(TWO + b"\ty = 1\n")
    # A key not a branch's is git's to set.
    first: list[ConfigChange] = [ConfigSet("core.forced", "1")] if by_git else []

    results = repo.write_config(*first, *(change for change, _ in UNSETS))

    assert results[len(first) :] == [result for _, result in UNSETS]
    assert any(call[0] == "config" for call in git_calls) == by_git
    assert "branch.m.x" not in config(repo)


def symlink_config(repo: Git, tmp_path: Path) -> Path:
    """Make repo's config a symbolic link to a file elsewhere, that file, or skip if this
    system can't."""
    target = tmp_path / "linked-config"
    target.write_bytes(config_file(repo).read_bytes())
    config_file(repo).unlink()
    try:
        config_file(repo).symlink_to(target)
    except OSError:  # Windows without the privilege
        pytest.skip("this system can't make a symbolic link")
    return target


def set_git_config(repo: Git, tmp_path: Path) -> None:
    repo.env["GIT_CONFIG"] = str(config_file(repo))


def symlinked_once_known(repo: Git, tmp_path: Path) -> None:
    git_paths(repo, "config")  # before git resolves the link
    symlink_config(repo, tmp_path)


def legacy_header(repo: Git, tmp_path: Path) -> None:
    config_file(repo).write_bytes(b"[branch.x]\n")


@pytest.mark.parametrize("fall_back", [set_git_config, symlinked_once_known, legacy_header])
def test_write_config_falls_back_to_git(
    repo: Git, tmp_path: Path, git_calls: list[GitCall], fall_back: Callable[[Git, Path], None]
) -> None:
    fall_back(repo, tmp_path)
    started = len(git_calls)

    results = repo.write_config(ConfigSet("branch.x.y", "1"), ConfigUnset("branch.x.z"))

    assert results == [ConfigResult.CHANGED, ConfigResult.NOT_FOUND]
    assert [call[:3] for call in git_calls[started:] if call[0] == "config"] == [
        ["config", "branch.x.y", "1"],
        ["config", "--unset", "branch.x.z"],
    ]
    assert repo.run("config", "branch.x.y") == "1"
    assert config_file(repo).is_symlink() == (fall_back is symlinked_once_known)


def test_write_config_writes_the_file_a_symbolic_link_names_as_git_does(
    repo: Git, tmp_path: Path
) -> None:
    target = symlink_config(repo, tmp_path)

    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert config_file(repo).is_symlink()
    assert b'[branch "a"]' in target.read_bytes()


def test_write_config_leaves_a_config_it_cant_read_to_git(repo: Git) -> None:
    git_paths(repo, "config")
    config_file(repo).rename(config_file(repo).with_name("elsewhere"))
    config_file(repo).mkdir()

    with pytest.raises(GitError):  # git's, not an OSError of ours
        repo.write_config(ConfigSet("branch.a.x", "1"))


def test_write_config_falling_back_keeps_the_changes_before_one_git_rejects(
    repo: Git,
) -> None:
    config_file(repo).write_bytes(TWO)

    with pytest.raises(GitError) as raised:
        repo.write_config(ConfigSet("branch.m.y", "1"), ConfigSet("branch.m.x", "3"))

    assert raised.value.returncode == 5
    assert repo.run("config", "branch.m.y") == "1"


def lock(repo: Git) -> Path:
    """Hold repo's config lock, as another process writing it does: the lock, to remove."""
    held = config_file(repo).with_name("config.lock")
    held.write_bytes(b"")
    return held


def test_write_config_waits_while_the_config_is_locked(repo: Git) -> None:
    timer = threading.Timer(0.2, lock(repo).unlink)
    start = time.monotonic()
    timer.start()
    try:
        repo.write_config(ConfigSet("branch.a.x", "1"))
    finally:
        timer.join()

    assert time.monotonic() - start >= 0.2
    assert repo.run("config", "branch.a.x") == "1"


@pytest.mark.parametrize("key", ["branch.a.x", "core.x"], ids=["itself", "falling back to git"])
def test_write_config_gives_up_once_its_wait_is_over(repo: Git, key: str) -> None:
    repo.run("config", "core.configLockTimeout", "0")  # git fails at once, as before 2.56
    before = config_file(repo).read_bytes()
    held = lock(repo)

    with pytest.raises(ConfigLockedError) as raised:
        repo.write_config(ConfigSet(key, "1"), wait=0.2)

    # git's own error, or the same.
    assert (raised.value.command, raised.value.returncode) == (["config", key, "1"], 255)
    assert held.exists()  # another's, to leave
    assert config_file(repo).read_bytes() == before


def test_write_config_and_git_config_writing_at_once_lose_nothing(repo: Git) -> None:
    def ours(i: int) -> None:
        repo.write_config(ConfigSet(f"branch.ours{i}.x", "1"), ConfigSet(f"branch.ours{i}.y", "2"))

    def gits(i: int) -> None:
        repo.write_config(ConfigSet(f"gits.n{i}.x", "1"))  # not a branch's: git config's

    with ThreadPoolExecutor(8) as pool:
        for future in [pool.submit(ours if i % 2 else gits, i) for i in range(16)]:
            future.result()

    keys = config(repo)
    assert all(f"gits.n{i}.x" in keys for i in range(0, 16, 2))
    assert all(f"branch.ours{i}.y" in keys for i in range(1, 16, 2))


@pytest.mark.parametrize(
    ("call", "error"),
    [
        ("write", OSError("disk full")),
        ("fsync", OSError("I/O error")),
        ("replace", PermissionError()),
    ],
)
def test_write_config_leaves_a_write_that_fails_midway_to_git(
    repo: Git,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_calls: list[GitCall],
    call: str,
    error: OSError,
) -> None:
    by_git, _ = git_edits(tmp_path, config_file(repo).read_bytes(), [ConfigSet("branch.a.x", "1")])
    started = len(git_calls)

    def fail(*args: object) -> None:
        raise error

    monkeypatch.setattr(os, call, fail)
    repo.write_config(ConfigSet("branch.a.x", "1"), wait=0.1)

    # Nothing of ours kept, and git's own write (or its error) instead.
    assert [call for call in git_calls[started:] if call[0] == "config"] == [
        ["config", "branch.a.x", "1"]
    ]
    assert config_file(repo).read_bytes() == by_git
    assert not config_file(repo).with_name("config.lock").exists()


def fail_one_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next os.close let go of its descriptor, as Linux does, then report EIO."""
    close = os.close
    failures = [OSError(errno.EIO, "I/O error")]

    def close_then_fail(fd: int) -> None:
        close(fd)
        if failures:
            raise failures.pop()

    monkeypatch.setattr(os, "close", close_then_fail)


def test_write_config_whose_close_fails_leaves_no_lock_and_falls_back(
    repo: Git, monkeypatch: pytest.MonkeyPatch, git_calls: list[GitCall]
) -> None:
    git_paths(repo, "config")
    fail_one_close(monkeypatch)
    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert [call[:2] for call in git_calls[1:]] == [["config", "branch.a.x"]]
    assert not config_file(repo).with_name("config.lock").exists()
    assert repo.run("config", "branch.a.x") == "1"


def test_write_config_once_written_doesnt_look_for_its_lock(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Where inode numbers don't tell files apart (0 on some filesystems, or one reused at
    # once), a lock made since would look like ours.
    lstat = os.lstat

    def no_lock(path: Path, **kwargs: bool) -> os.stat_result:
        assert Path(path).name != "config.lock"
        return lstat(path, **kwargs)

    monkeypatch.setattr(os, "lstat", no_lock)

    repo.write_config(ConfigSet("branch.a.x", "1"))


def test_write_config_whose_write_and_close_both_fail_falls_back(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_paths(repo, "config")

    def fail(*args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", fail)
    fail_one_close(monkeypatch)
    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert repo.run("config", "branch.a.x") == "1"
    assert not config_file(repo).with_name("config.lock").exists()


def test_write_config_whose_close_fails_with_nothing_to_change_says_so(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_paths(repo, "config")
    fail_one_close(monkeypatch)

    assert repo.write_config(ConfigUnset("branch.a.x")) == [ConfigResult.NOT_FOUND]
    assert not config_file(repo).with_name("config.lock").exists()


def test_write_config_stops_waiting_for_a_lock_once_the_tool_is_stopping(repo: Git) -> None:
    git_paths(repo, "config")
    before = config_file(repo).read_bytes()
    held = lock(repo)
    stopping = threading.Event()
    repo.stopping = stopping.is_set
    timer = threading.Timer(0.1, stopping.set)
    start = time.monotonic()

    timer.start()
    try:
        with pytest.raises(NotStartedError):
            repo.write_config(ConfigSet("branch.a.x", "1"), wait=5)
    finally:
        timer.join()

    assert time.monotonic() - start < 2
    assert held.exists()
    assert config_file(repo).read_bytes() == before


def test_write_config_interrupted_once_written_leaves_anothers_lock(
    repo: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = os.replace

    def replace_then_interrupt(source: Path, destination: Path) -> None:
        replace(source, destination)
        lock(repo)  # another process's, as soon as ours is gone
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", replace_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        repo.write_config(ConfigSet("branch.a.x", "1"))

    assert config_file(repo).with_name("config.lock").exists()
    assert b'[branch "a"]' in config_file(repo).read_bytes()


def test_write_config_tries_a_rename_windows_refuses_again(
    repo: Git, monkeypatch: pytest.MonkeyPatch, git_calls: list[GitCall]
) -> None:
    refusals = [PermissionError("in use")]  # as Windows refuses while the file is open
    replace = os.replace

    def refuse_once(source: Path, destination: Path) -> None:
        if refusals:
            raise refusals.pop()
        replace(source, destination)

    monkeypatch.setattr(os, "replace", refuse_once)
    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert repo.run("config", "branch.a.x") == "1"
    # Only Windows' refusal passes: elsewhere, git writes it.
    assert any(call[0] == "config" for call in git_calls[:-1]) == (sys.platform != "win32")


def test_write_config_leaves_a_lock_it_cant_make_to_git(
    repo: Git, monkeypatch: pytest.MonkeyPatch, git_calls: list[GitCall]
) -> None:
    open_file = os.open

    def refuse_the_lock(path: Path, flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "config.lock":
            raise PermissionError("denied")
        return open_file(path, flags, mode)

    monkeypatch.setattr(os, "open", refuse_the_lock)
    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert git_calls[-1][:2] == ["config", "branch.a.x"]


def test_write_config_waits_for_a_reader_that_keeps_windows_from_replacing_the_file(
    repo: Git,
) -> None:
    # Windows won't replace a file another process has open without sharing its deletion,
    # as Python opens it; elsewhere, the file is replaced at once.
    reader = config_file(repo).open("rb")
    timer = threading.Timer(0.2, reader.close)
    timer.start()
    try:
        repo.write_config(ConfigSet("branch.a.x", "1"))
    finally:
        timer.join()

    assert repo.run("config", "branch.a.x") == "1"


def test_write_config_keeps_the_files_permissions(repo: Git) -> None:
    config_file(repo).chmod(0o640)  # Windows keeps only whether it's read-only
    mode = stat.S_IMODE(config_file(repo).stat().st_mode)

    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert stat.S_IMODE(config_file(repo).stat().st_mode) == mode


def test_write_config_leaves_a_read_only_config_to_git(repo: Git, git_calls: list[GitCall]) -> None:
    # Windows won't replace a read-only file, nor remove a read-only lock; git for Windows
    # clears the flag first.
    config_file(repo).chmod(0o444)

    repo.write_config(ConfigSet("branch.a.x", "1"))

    assert repo.run("config", "branch.a.x") == "1"
    assert git_calls[-2][:2] == ["config", "branch.a.x"]
    assert not config_file(repo).with_name("config.lock").exists()


def test_write_config_writes_no_file_when_nothing_changes(repo: Git) -> None:
    before = config_file(repo).stat().st_mtime_ns
    time.sleep(0.01)

    results = repo.write_config(ConfigUnset("branch.a.x"), ConfigUnset("branch.b.y", "1"))

    assert results == [ConfigResult.NOT_FOUND, ConfigResult.NOT_FOUND]
    assert config_file(repo).stat().st_mtime_ns == before
    assert not config_file(repo).with_name("config.lock").exists()


def test_write_config_makes_a_config_file_where_there_is_none(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git_paths(repo, "config")
    config_file(repo).unlink()

    repo.write_config(ConfigUnset("branch.a.y"), ConfigSet("branch.a.x", "1"))

    assert config_file(repo).read_bytes() == b'[branch "a"]\n\tx = 1\n'  # as git makes it
    assert [call[0] for call in git_calls] == ["rev-parse"]


def test_write_config_writes_nothing_once_the_tool_is_stopping(repo: Git) -> None:
    git_paths(repo, "config")
    before = config_file(repo).read_bytes()
    repo.stopping = lambda: True

    with pytest.raises(NotStartedError):
        repo.write_config(ConfigSet("branch.a.x", "1"))

    assert config_file(repo).read_bytes() == before


def test_write_config_writes_the_common_config_from_a_linked_worktree(
    repo: Git, tmp_path: Path
) -> None:
    repo.run("worktree", "add", "--quiet", str(tmp_path / "linked"))
    repo.run("config", "extensions.worktreeConfig", "true")

    Git(tmp_path / "linked").write_config(ConfigSet("branch.a.x", "1"))

    assert b'[branch "a"]' in config_file(repo).read_bytes()


def test_fake_git_runs_git_config_for_each_change() -> None:
    git = FakeGit()
    git.respond(["config"])

    git.write_config(ConfigSet("branch.a.x", "1"), ConfigUnset("branch.a.y"))

    assert git.commands == [["config", "branch.a.x", "1"], ["config", "--unset", "branch.a.y"]]
