import os
from pathlib import Path

import pytest

from git_client import (
    CatFile,
    EnvValue,
    Git,
    ReadOnlyError,
    ReadOnlyGit,
    config,
    git_paths,
    patch_ids,
    refs,
)

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
        ("range-diff", "HEAD..HEAD", "HEAD..HEAD"),
        ("rev-list", "HEAD"),
        ("rev-parse", "HEAD"),
        ("shortlog", "-s", "HEAD"),
        ("show", "HEAD"),
        ("show-ref",),
        ("status",),
        ("var", "GIT_AUTHOR_IDENT"),
        ("worktree", "list"),
        ("--no-pager", "--literal-pathspecs", "log", "--", "a"),
    ],
)
def test_a_read_only_client_runs_what_only_reads_and_writes_nothing(
    stale: Path, args: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever the user's environment says: status writes the index unless this is 0.
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    before = files(stale.parent)

    ReadOnlyGit(stale).process(*args, capture_output=True)  # e.g. check-ignore exits 1

    assert files(stale.parent) == before


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
