"""dev_project.py's `release`, against a local bare repository standing in for origin."""

from pathlib import Path

import pytest

import dev_project
from dev_project import ReleaseError
from git_client import Git
from git_client.testing import RepoTemplates

PYPROJECT = """\
[tool.example]
version = "9.9.9"

[project]
name = "example"
version = "0.0.1"

[dependency-groups]
dev = []
"""


@pytest.mark.parametrize("text", ["0.0.1", "1.20.300", "0.0.0"])
def test_parse_version_reads_three_integers(text: str) -> None:
    assert ".".join(map(str, dev_project.parse_version(text))) == text


@pytest.mark.parametrize("text", ["1.2", "1.2.3.4", "v1.2.3", "1.2.3rc1", "01.2.3", " 1.2.3", ""])
def test_parse_version_refuses_anything_else(text: str) -> None:
    with pytest.raises(ReleaseError, match="isn't a version"):
        dev_project.parse_version(text)


def test_next_version_increments_the_patch() -> None:
    assert dev_project.next_version("1.2.9") == "1.2.10"


def test_pyproject_version_is_projects() -> None:
    assert dev_project.pyproject_version(PYPROJECT) == "0.0.1"


def test_set_pyproject_version_changes_only_projects() -> None:
    changed = dev_project.set_pyproject_version(PYPROJECT, "0.0.2")
    assert changed == PYPROJECT.replace('version = "0.0.1"', 'version = "0.0.2"')


def test_set_pyproject_version_keeps_windows_line_endings() -> None:
    crlf = PYPROJECT.replace("\n", "\r\n")
    changed = dev_project.set_pyproject_version(crlf, "0.0.2")
    assert changed == crlf.replace('version = "0.0.1"', 'version = "0.0.2"')


def test_set_pyproject_version_refuses_without_one() -> None:
    with pytest.raises(ReleaseError, match="no version"):
        dev_project.set_pyproject_version('[project]\nname = "x"\n', "0.0.2")


def make_origin_and_clone(directory: Path) -> None:
    """origin.git, a bare repository whose main has pyproject.toml (version 0.0.1) and uv.lock,
    and clone, a clone of it."""
    Git(directory).run("init", "--quiet", "--bare", str(directory / "origin.git"))
    Git(directory).run("clone", "--quiet", str(directory / "origin.git"), str(directory / "clone"))
    clone = Git(directory / "clone")
    (directory / "clone" / "pyproject.toml").write_bytes(PYPROJECT.encode())
    (directory / "clone" / "uv.lock").write_bytes(b'version = "0.0.1"\n')
    clone.run("add", "pyproject.toml", "uv.lock")
    clone.run("commit", "--quiet", "-m", "first")
    clone.run("push", "--quiet", "origin", "main")


class FakeGitHub:
    """Stands in for what dev_project asks of gh and uv, which would reach the network: the CI
    status of a commit, the PRs opened, and the lock files updated."""

    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.asked: list[str] = []
        self.prs: list[tuple[str, str, str]] = []
        self.locked: list[Path] = []

    def ci_status(self, repo: Path, sha: str) -> str:
        self.asked.append(sha)
        return self.status

    def open_pr(self, worktree: Path, branch: str, title: str, body: str) -> str:
        self.prs.append((branch, title, body))
        return f"https://github.com/example/pull/{len(self.prs)}"

    def uv_lock(self, worktree: Path) -> None:
        self.locked.append(worktree)
        lock = worktree / "uv.lock"
        lock.write_bytes(lock.read_bytes().replace(b"0.0.1", b"0.0.2"))


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(dev_project, "ci_status", fake.ci_status)
    monkeypatch.setattr(dev_project, "open_pr", fake.open_pr)
    monkeypatch.setattr(dev_project, "uv_lock", fake.uv_lock)
    return fake


@pytest.fixture
def repos(repo_templates: RepoTemplates, tmp_path: Path) -> Path:
    """A copy of make_origin_and_clone's repositories, with a commit in clone that origin
    hasn't got, which a release mustn't tag."""
    repo_templates.build("origin-and-clone", make_origin_and_clone)
    directory = repo_templates.copy("origin-and-clone", tmp_path / "repos")
    clone = Git(directory / "clone")
    (directory / "clone" / "local").write_bytes(b"local\n")
    clone.run("add", "local")
    clone.run("commit", "--quiet", "-m", "not pushed")
    return directory


def test_release_tags_origins_main_and_opens_the_next_versions_pr(
    repos: Path, github: FakeGitHub
) -> None:
    origin = Git(repos / "origin.git")
    main = origin.run("rev-parse", "main")

    tag, url = dev_project.publish(repos / "clone", "0.0.1")

    assert (tag, url) == ("v0.0.1", "https://github.com/example/pull/1")
    assert github.asked == [main]
    assert origin.run("cat-file", "-t", "v0.0.1") == "tag"
    assert origin.run("rev-parse", "v0.0.1^{commit}") == main
    start = origin.run("rev-parse", "start-0.0.2")
    assert origin.run("rev-parse", f"{start}^") == main
    assert origin.run("show", f"{start}:pyproject.toml") == PYPROJECT.replace(
        'version = "0.0.1"', 'version = "0.0.2"'
    ).rstrip("\n")
    assert origin.run("show", f"{start}:uv.lock") == 'version = "0.0.2"'
    assert origin.run("log", "-1", "--format=%s", start) == (
        "Start version 0.0.2, after releasing 0.0.1"
    )
    [(branch, title, body)] = github.prs
    assert (branch, title) == ("start-0.0.2", "Start version 0.0.2, after releasing 0.0.1")
    assert body.startswith("Opened by `python3 dev.py release 0.0.1`")
    clone = Git(repos / "clone")
    assert not github.locked[0].exists()
    assert len(clone.run("worktree", "list").splitlines()) == 1
    assert not clone.ok("rev-parse", "--verify", "--quiet", "refs/heads/start-0.0.2")


def test_release_refuses_another_version(repos: Path, github: FakeGitHub) -> None:
    with pytest.raises(ReleaseError, match=r"pyproject\.toml's version is 0\.0\.1"):
        dev_project.publish(repos / "clone", "0.0.2")
    assert Git(repos / "origin.git").run("tag") == ""


def test_release_refuses_a_tag_on_origin(repos: Path, github: FakeGitHub) -> None:
    Git(repos / "origin.git").run("tag", "v0.0.1", "main")
    with pytest.raises(ReleaseError, match="v0.0.1 exists on origin"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert github.prs == []


def test_release_refuses_a_local_tag(repos: Path, github: FakeGitHub) -> None:
    Git(repos / "clone").run("tag", "v0.0.1")
    with pytest.raises(ReleaseError, match="v0.0.1 exists locally"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("tag") == ""


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("pending", "hasn't finished.*wait"),
        ("missing", "hasn't reported.*wait"),
        ("failure", "concluded failure"),
    ],
)
def test_release_refuses_until_ci_passed(
    repos: Path, github: FakeGitHub, status: str, message: str
) -> None:
    github.status = status
    with pytest.raises(ReleaseError, match=message):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("tag") == ""
    assert not Git(repos / "clone").ok("rev-parse", "--verify", "--quiet", "refs/tags/v0.0.1")


def test_release_says_how_to_finish_when_the_next_version_fails(
    repos: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(worktree: Path, branch: str, title: str, body: str) -> str:
        raise ReleaseError("gh pr create failed")

    monkeypatch.setattr(dev_project, "open_pr", fail)
    with pytest.raises(ReleaseError, match=r"(?s)pushed v0\.0\.1.*gh pr create failed.*by hand"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("cat-file", "-t", "v0.0.1") == "tag"
    assert len(Git(repos / "clone").run("worktree", "list").splitlines()) == 1


def test_release_command_names_a_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        return None if name == "gh" else name

    monkeypatch.setattr(dev_project.shutil, "which", which)
    with pytest.raises(SystemExit, match="needs gh"):
        dev_project.release(["0.0.1"])
