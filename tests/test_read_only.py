import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from git_client import (
    Arg,
    CatFile,
    EnvValue,
    Git,
    ReadOnlyError,
    ReadOnlyGit,
    RefSnapshot,
    config,
    git_paths,
    patch_ids,
    refs,
)
from git_client.snapshot import ref_changes

# A day well before any test runs, for a file's mtime: not racily clean (as a file changed in
# the second the index was written would be), so git sees it changed since the index has it.
_OLD = 1_600_000_000


def no_git(tmp_path: Path) -> str:
    """A git that isn't there: a client given it that tries to start git raises
    FileNotFoundError, so one that raises ReadOnlyError started none."""
    return str(tmp_path / "no-git")


def files(directory: Path) -> dict[Path, bytes]:
    """Every file under `directory` (the repository's and its worktree's), with its content."""
    return {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}


@pytest.fixture
def stale(repo: Git) -> Path:
    """make_repo's repository, with its file's stat information out of date in the index:
    one a command that refreshes the index (e.g. `status`, without GIT_OPTIONAL_LOCKS=0)
    writes it."""
    directory = Path(repo.directory)
    repo.run("update-index", "--refresh")
    os.utime(directory / "a", (_OLD, _OLD))
    return directory


@pytest.mark.parametrize(
    "args",
    [
        ("blame", "a"),
        ("cat-file", "-p", "HEAD"),
        ("check-attr", "-a", "a"),
        ("check-ignore", "--no-index", "a"),
        ("check-ref-format", "refs/heads/main"),
        ("config", "--get", "user.name"),
        ("config", "--list"),
        ("count-objects",),
        ("describe", "--always"),
        ("diff-files",),
        ("diff-index", "HEAD"),
        ("diff-tree", "-p", "HEAD"),
        ("for-each-ref",),
        ("log", "-p", "--stat"),
        ("ls-files", "-m"),
        ("ls-tree", "HEAD"),
        ("merge-base", "HEAD", "HEAD"),
        ("name-rev", "HEAD"),
        ("rev-list", "HEAD"),
        ("rev-parse", "HEAD"),
        ("shortlog", "-s", "HEAD"),
        ("show", "HEAD"),
        ("show-ref",),
        ("status",),
        ("var", "GIT_AUTHOR_IDENT"),
        ("worktree", "list"),
        ("--no-pager", "--literal-pathspecs", "log", "--", "a"),
        # config's options before its action, and a lone key, which it gets.
        ("config", "--type=bool", "core.bare"),
        ("config", "--type", "bool", "--get", "core.bare"),
        ("config", "--bool", "--local", "--get", "core.bare"),
        ("config", "--int", "--default", "0", "--get", "no.such"),
        ("config", "--default=x", "no.such"),
        ("config", "--global", "--list"),
        ("config", "-f", ".git/config", "--list"),
        ("config", "--file=.git/config", "-z", "core.bare"),
        ("config", "--show-origin", "--show-scope", "--get-regexp", "core"),
        ("branch", "--show-current"),
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        ("remote",),
        ("remote", "-v"),
        ("remote", "get-url", "--push", "origin"),
    ],
)
def test_a_read_only_client_runs_what_only_reads_and_writes_nothing(
    stale: Path, args: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever the user's environment says: status writes the index unless this is 0.
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    before = files(stale.parent)
    # What only reads keeps refs, as RefSnapshot counts them.
    snapshot = RefSnapshot(lambda: ref_changes.count())
    kept = snapshot.get()

    ReadOnlyGit(stale).process(*args, capture_output=True)  # e.g. check-ignore exits 1

    assert files(stale.parent) == before
    assert snapshot.get() == kept


def test_status_writes_a_stale_index_without_a_read_only_clients_environment(
    stale: Path,
) -> None:
    before = files(stale.parent)

    Git(stale).run("status")

    assert files(stale.parent) != before


@pytest.mark.parametrize(
    "args",
    [
        ("diff",),  # refreshes the index, whatever GIT_OPTIONAL_LOCKS says
        ("describe", "--always", "--dirty"),  # likewise
        ("describe", "--always", "--dirt"),  # git takes an unambiguous abbreviation
        ("log", "-1", "--output=out"),
        # Each of these ends what git takes as options only as another option's value.
        ("log", "-1", "--grep", "--end-of-options", "--output=out"),
        ("describe", "--always", "--match", "--", "--dirty"),
        ("config", "--file", "--list", "a.b", "c"),  # writes a file named --list
        ("config", "-f=x", "a.b", "c.d"),  # writes a.b to a file named =x
        ("config", "-f", "x", "a.b", "c"),
        ("config", "--type=bool", "a.b", "true"),
    ],
)
def test_what_writes_from_a_reading_command_a_read_only_client_refuses(
    stale: Path, args: tuple[str, ...], tmp_path: Path
) -> None:
    before = files(stale.parent)
    Git(stale).run(*args)
    assert files(stale.parent) != before

    with pytest.raises(ReadOnlyError):
        ReadOnlyGit(stale, program=no_git(tmp_path)).run(*args)


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("commit", "--allow-empty", "-m", "x"),
        ("gc",),
        ("-c", "core.fsmonitor=x", "status"),
        ("--git-dir=elsewhere", "log"),
        ("diff-tree", "--output", "out", "HEAD"),
        ("describe", "--broken"),
        ("config", "a.b", "c"),
        ("config", "--unset", "user.name"),
        ("config", "--get", "user.name", "--ad"),
        ("config", "--list", "-ze"),  # -e, bundled
        ("config", "set", "a.b", "c"),
        ("worktree", "add", "x"),
        ("config", "--global", "a.b", "c"),
        ("config", "--global", "--add", "a.b", "c"),
        ("config", "--type=bool", "--global", "--edit"),
        ("config", "edit"),  # not a key: config's own action
        ("config", "--no-such-option", "--list"),
        ("branch", "topic"),
        ("branch", "--show-current", "topic"),
        ("symbolic-ref", "HEAD", "refs/heads/topic"),  # two names: it points HEAD at topic
        ("symbolic-ref", "--delete", "HEAD"),
        ("symbolic-ref", "-m", "why", "HEAD"),
        ("remote", "add", "upstream", "url"),
        ("remote", "set-url", "origin", "url"),
        ("ls-remote", "."),  # may run credential helpers, which store what they're told
        # Each writes a textconv's cache (see test_a_read_only_client_caches_no_textconv).
        ("cat-file", "--textconv", "HEAD:a"),
        ("cat-file", "--batch", "--textc"),
        ("diff-tree", "-p", "--textconv", "HEAD"),
        ("log", "-p", "--textconv"),
        ("status", "-v"),
        ("status", "-sv"),
        ("status", "--verb"),
        ("range-diff", "HEAD~1..HEAD", "HEAD~1..HEAD"),  # its log takes no --no-textconv
        ("log", "-p", "--ext-diff"),  # runs GIT_EXTERNAL_DIFF, or a diff driver's command
    ],
)
def test_a_read_only_client_refuses_any_other_command_without_starting_git(
    repo: Git, args: tuple[str, ...], tmp_path: Path
) -> None:
    with pytest.raises(ReadOnlyError):
        ReadOnlyGit(repo.directory, program=no_git(tmp_path)).run(*args)


@pytest.mark.parametrize(
    ("client_env", "env"),
    [
        ({"GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='x'"}, {}),
        ({}, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}),
        ({}, {"GIT_EXTERNAL_DIFF": "x"}),
        ({}, {"GIT_DIR": "elsewhere"}),
        ({}, {"HOME": "elsewhere"}),  # a global config of its own
        ({}, {"GIT_OPTIONAL_LOCKS": "1"}),
    ],
)
def test_a_read_only_client_refuses_an_environment_that_could_make_git_write(
    repo: Git,
    client_env: dict[str, EnvValue],
    env: dict[str, EnvValue],
    tmp_path: Path,
) -> None:
    git = ReadOnlyGit(repo.directory, program=no_git(tmp_path), env=client_env)

    with pytest.raises(ReadOnlyError):
        git.run("status", env=env)


def test_a_read_only_client_takes_an_environment_that_only_says_what_to_read(repo: Git) -> None:
    git = ReadOnlyGit(repo.directory, env={"GIT_LITERAL_PATHSPECS": "1"})

    assert git.run("log", "-1", "--format=%s", env={"TZ": "UTC", "LC_TIME": "C"}) == "first"


def test_the_librarys_reads_take_a_read_only_client(repo: Git) -> None:
    git = ReadOnlyGit(repo.directory)
    head = git.run("rev-parse", "HEAD")

    with CatFile(git) as cat_file:
        assert cat_file.resolve("HEAD") == head
    assert refs(git)["refs/heads/main"].object == head
    assert config(git)["user.name"]
    assert len(patch_ids(git, ["log", "-p", "HEAD"])[0]) == 1
    assert git.resolve("HEAD") == head
    assert git.prefix() == ""
    assert git_paths(git, "HEAD")[0].name == "HEAD"


@pytest.mark.skipif(
    tuple(int(part) for part in Git(".").run("version").split()[2].split(".")[:2]) < (2, 45),
    reason="git before 2.45 (but for 2.39.4 to 2.44.1) ignores GIT_NO_LAZY_FETCH, and fetches",
)
def test_in_a_partial_clone_a_read_only_client_fetches_no_missing_object(repo: Git) -> None:
    origin = Path(repo.directory)
    repo.run("config", "uploadpack.allowFilter", "true")
    blob = repo.run("rev-parse", "HEAD:a")
    clone = origin.parent / "clone"
    url = origin.as_uri()
    Git(origin.parent).run("clone", "--quiet", "--no-checkout", "--filter=blob:none", url, "clone")
    before = files(clone)

    result = ReadOnlyGit(clone).process("cat-file", "-p", blob, capture_output=True)

    assert result.returncode != 0
    assert files(clone) == before
    assert Git(clone).run("cat-file", "-p", blob) == "a"  # which a Git fetches


def test_a_clients_read_only_one_runs_as_it_does_but_refuses_what_writes(repo: Git) -> None:
    def stopping() -> bool:
        return False

    git = Git(repo.directory, timeout=9, stopping=stopping, env={"TZ": "UTC"})

    reader = git.read_only()

    assert (reader.directory, reader.program, reader.timeout, reader.stopping, reader.env) == (
        git.directory,
        git.program,
        9,
        stopping,
        {"TZ": "UTC"},
    )
    assert reader.run("log", "-1", "--format=%s") == "first"
    with pytest.raises(ReadOnlyError):
        reader.run("commit", "--allow-empty", "-m", "x")
    git.run("commit", "--allow-empty", "--quiet", "-m", "x")  # the client itself still writes
    assert reader.read_only() is reader


def test_a_clients_read_only_one_has_what_the_client_has_when_its_asked_for(repo: Git) -> None:
    git = LoggingGit(repo.directory)
    reader = git.read_only()

    git.log = ["rebound"]  # as a tool or a test may
    now = git.read_only()
    assert isinstance(now, LoggingGit)
    assert now.log == ["rebound"]
    git.env["TZ"] = "UTC"
    assert git.read_only().env == {"TZ": "UTC"}

    assert reader.env == {}  # a reader already given out stays as it was


def test_a_client_whose_environment_a_read_only_one_refuses_makes_none(repo: Git) -> None:
    git = Git(repo.directory, env={"GIT_DIR": "elsewhere"})

    with pytest.raises(ReadOnlyError):
        git.read_only()


def test_a_client_and_its_read_only_one_share_what_git_told_them(
    repo: Git, git_calls: list[list[str]]
) -> None:
    git = Git(repo.directory)
    git.prefix()
    git.object_id_length()
    reader = git.read_only()
    git_paths(reader, "HEAD")
    started = len(git_calls)

    assert reader.prefix() == ""
    assert reader.object_id_length() == 40
    git_paths(git, "HEAD")

    assert len(git_calls) == started


class LoggingGit(Git):
    """A tool's own Git, whose `process` notes each command."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.log: list[str] = []

    def process(self, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.log.append(os.fsdecode(args[0]))
        return super().process(*args, **kwargs)


def test_a_subclass_read_only_one_keeps_its_methods(repo: Git) -> None:
    git = LoggingGit(repo.directory)

    reader = git.read_only()
    reader.run("rev-parse", "HEAD")

    assert isinstance(reader, LoggingGit)
    assert git.log == ["rev-parse"]  # its own attributes too, shared with the client's
    with pytest.raises(ReadOnlyError):
        reader.run("commit", "--allow-empty", "-m", "x")


def python_command(script: Path, *args: Path) -> str:
    """A command line for git to run through its shell: `script`, with Python."""
    return " ".join(f'"{path.as_posix()}"' for path in (Path(sys.executable), script, *args))


@pytest.fixture
def textconv_cached(repo: Git, tmp_path: Path) -> Path:
    """repo with a second commit, changing `a`, whose diff driver has a textconv that git
    caches (in refs/notes/textconv/upper), as `diff.<driver>.cachetextconv` asks."""
    directory = Path(repo.directory)
    (directory / "a").write_bytes(b"b\n")
    repo.run("commit", "--quiet", "-am", "second")
    (directory / ".git" / "info" / "attributes").write_text("a diff=upper\n", encoding="utf-8")
    script = tmp_path / "upper.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(open(sys.argv[1], 'rb').read().upper())\n",
        encoding="utf-8",
    )
    repo.run("config", "diff.upper.textconv", python_command(script))
    repo.run("config", "diff.upper.cachetextconv", "true")
    return directory


@pytest.mark.parametrize("args", [("log", "-p"), ("show",), ("blame", "a")])
def test_a_read_only_client_caches_no_textconv(
    textconv_cached: Path, args: tuple[str, ...]
) -> None:
    before = files(textconv_cached)

    output = ReadOnlyGit(textconv_cached).run(*args)

    assert files(textconv_cached) == before
    assert "B" not in output  # git's own text, not the textconv's
    Git(textconv_cached).run(*args)  # which caches it
    assert Git(textconv_cached).run("for-each-ref", "refs/notes/textconv/")


@pytest.fixture
def fsmonitor(repo: Git, tmp_path: Path) -> Path:
    """repo with a core.fsmonitor hook, a program that git runs to ask what changed (which
    could write anything): it makes the file this returns, and says everything may have."""
    ran = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.py"
    script.write_text(
        "import sys\nopen(sys.argv[1], 'w').close()\nsys.stdout.write('\\0/\\0')\n",
        encoding="utf-8",
    )
    repo.run("config", "core.fsmonitor", python_command(script, ran))
    return ran


def test_a_read_only_client_runs_no_fsmonitor(repo: Git, fsmonitor: Path) -> None:
    ReadOnlyGit(repo.directory).run("status")
    assert not fsmonitor.exists()

    Git(repo.directory).run("status")
    assert fsmonitor.exists()


def test_a_read_only_client_runs_no_fsmonitor_its_environment_sets(
    repo: Git, fsmonitor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Git(repo.directory).run("config", "core.fsmonitor")
    Git(repo.directory).run("config", "--unset", "core.fsmonitor")
    # As `git -c core.fsmonitor=... my-tool` would give it.
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor'='{command}'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)

    ReadOnlyGit(repo.directory).run("status")

    assert not fsmonitor.exists()


def test_a_read_only_client_runs_no_external_diff_its_environment_names(
    textconv_cached: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = tmp_path / "external-diff-ran"
    script = tmp_path / "external.py"
    script.write_text("import sys\nopen(sys.argv[1], 'w').close()\n", encoding="utf-8")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", python_command(script, ran))
    git = ReadOnlyGit(textconv_cached)

    for args in [
        ("log", "-p"),
        ("show",),
        ("diff-tree", "-p", "HEAD"),
        ("diff-index", "-p", "HEAD~1"),
    ]:
        git.run(*args)

    assert not ran.exists()
    Git(textconv_cached).run("log", "-p", "--ext-diff")  # which runs it
    assert ran.exists()


def test_a_read_only_clients_config_reads_have_none_of_its_own(repo: Git) -> None:
    assert ReadOnlyGit(repo.directory).run("config", "--list") == repo.run("config", "--list")
