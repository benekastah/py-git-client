"""dev_project.py's `release`, against a local bare repository standing in for origin."""

import json
import re
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
CHANGELOG = """\
# Changelog

## 0.0.2 (unreleased)

- Next.

## 0.0.1 (2026-01-02)

### Added

- First.

## 0.0.0 (2026-01-01)

- Before.
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


def test_changelog_notes_are_the_versions_section() -> None:
    assert dev_project.changelog_notes(CHANGELOG, "0.0.1") == "### Added\n\n- First."
    assert dev_project.changelog_notes(CHANGELOG, "0.0.0") == "- Before."


def test_changelog_notes_refuse_an_undated_section() -> None:
    with pytest.raises(ReleaseError, match=r"0\.0\.2's section .* unreleased.*date it"):
        dev_project.changelog_notes(CHANGELOG, "0.0.2")


@pytest.mark.parametrize("changelog", [CHANGELOG.replace("## 0.0.1 ", "## 0.0.10 "), ""])
def test_changelog_notes_refuse_a_missing_section(changelog: str) -> None:
    with pytest.raises(ReleaseError, match=r"CHANGELOG\.md has no section for 0\.0\.1"):
        dev_project.changelog_notes(changelog, "0.0.1")


def test_changelog_notes_refuse_an_empty_section() -> None:
    with pytest.raises(ReleaseError, match=r"0\.0\.1's section .* is empty"):
        dev_project.changelog_notes("## 0.0.1 (2026-01-02)\n\n## 0.0.0 (2026-01-01)\n", "0.0.1")


def make_origin_and_clone(directory: Path) -> None:
    """origin.git, a bare repository whose main has pyproject.toml (version 0.0.1) and uv.lock,
    and clone, a clone of it."""
    Git(directory).run("init", "--quiet", "--bare", str(directory / "origin.git"))
    Git(directory).run("clone", "--quiet", str(directory / "origin.git"), str(directory / "clone"))
    clone = Git(directory / "clone")
    (directory / "clone" / "pyproject.toml").write_bytes(PYPROJECT.encode())
    (directory / "clone" / "uv.lock").write_bytes(b'version = "0.0.1"\n')
    (directory / "clone" / "CHANGELOG.md").write_bytes(CHANGELOG.encode())
    clone.run("add", "pyproject.toml", "uv.lock", "CHANGELOG.md")
    clone.run("commit", "--quiet", "-m", "first")
    clone.run("push", "--quiet", "origin", "main")


GITHUB_ORIGIN = "https://github.com/example/repo.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/repo.git",
        "https://github.com/example/repo",
        "git@github.com:example/repo.git",
        "git@github.com:example/repo",
        "ssh://git@github.com/example/repo.git",
        "ssh://git@github.com:22/example/repo.git",
        "https://GitHub.com/example/repo",
        "https://user@github.com/example/repo.git",
        "https://github.com:443/example/repo",
    ],
)
def test_github_repo_reads_owner_and_name(url: str) -> None:
    assert dev_project.github_repo(url) == "example/repo"


@pytest.mark.parametrize(
    "url",
    [
        "/srv/repo.git",
        "https://gitlab.com/example/repo.git",
        "https://github.example.com/example/repo.git",
        "ssh://git@github.com:2222/example/repo.git",
        "https://github.com/example",
    ],
)
def test_github_repo_refuses_other_urls(url: str) -> None:
    with pytest.raises(ReleaseError, match="only github.com is supported"):
        dev_project.github_repo(url)


class FakeRun:
    """Stands in for dev_project._run, to test what's asked of gh, and how its answers are
    read, without the network: records each command and answers with `output`."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.commands: list[list[str]] = []
        self.stdins: list[str | None] = []

    def __call__(self, args: list[str], cwd: Path, stdin: str | None = None) -> str:
        self.commands.append(args)
        self.stdins.append(stdin)
        return self.output


def check_runs(*runs: tuple[str, str | None]) -> str:
    """gh api's answer listing check runs, each a (status, conclusion)."""
    listed = [{"status": status, "conclusion": conclusion} for status, conclusion in runs]
    return json.dumps({"total_count": len(listed), "check_runs": listed})


@pytest.mark.parametrize(
    ("runs", "status"),
    [
        ((), "missing"),
        ((("queued", None),), "pending"),
        ((("in_progress", None),), "pending"),
        ((("completed", "success"),), "success"),
        ((("completed", "failure"),), "failure"),
        ((("completed", "cancelled"),), "cancelled"),
        ((("completed", None),), "no conclusion"),
        ((("completed", "success"), ("completed", "success")), "success"),
        ((("completed", "success"), ("in_progress", None)), "pending"),
        ((("completed", "success"), ("completed", "failure")), "failure"),
    ],
)
def test_ci_status_needs_every_run_to_succeed(
    monkeypatch: pytest.MonkeyPatch, runs: tuple[tuple[str, str | None], ...], status: str
) -> None:
    run = FakeRun(check_runs(*runs))
    monkeypatch.setattr(dev_project, "_run", run)
    assert dev_project.ci_status(Path(), "example/repo", "abc123") == status
    assert run.commands == [
        [
            "gh",
            "api",
            "--method",
            "GET",
            "repos/example/repo/commits/abc123/check-runs",
            "-f",
            "check_name=CI passed",
            "-f",
            "app_id=15368",
        ]
    ]


def test_open_pr_opens_it_on_the_repository_and_returns_its_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = FakeRun("Warning: 1 uncommitted change\nhttps://github.com/example/repo/pull/7")
    monkeypatch.setattr(dev_project, "_run", run)
    url = dev_project.open_pr(Path(), "example/repo", "start-0.0.2", "Title", "Body")
    assert url == "https://github.com/example/repo/pull/7"
    assert run.commands == [
        [
            "gh",
            "pr",
            "create",
            "--repo",
            "example/repo",
            "--base",
            "main",
            "--head",
            "start-0.0.2",
            "--title",
            "Title",
            "--body",
            "Body",
        ]
    ]


def test_create_release_publishes_the_tag_with_notes_and_returns_its_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = FakeRun("https://github.com/example/repo/releases/tag/v0.0.1")
    monkeypatch.setattr(dev_project, "_run", run)
    url = dev_project.create_release(Path(), "example/repo", "v0.0.1", "- First.")
    assert url == "https://github.com/example/repo/releases/tag/v0.0.1"
    assert run.commands == [
        [
            "gh",
            "release",
            "create",
            "v0.0.1",
            "--repo",
            "example/repo",
            "--verify-tag",
            "--title",
            "v0.0.1",
            "--notes-file",
            "-",
        ]
    ]
    assert run.stdins == ["- First."]


class FakeGitHub:
    """Stands in for what dev_project asks of gh and uv, which would reach the network: the CI
    status of a commit, the PRs opened, and the lock files updated; and for origin's URLs."""

    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.asked: list[str] = []
        self.prs: list[tuple[str, str, str]] = []
        self.locked: list[Path] = []
        self.releases: list[tuple[str, str]] = []
        self.urls = [GITHUB_ORIGIN]

    def origin_urls(self, repo: Path) -> list[str]:
        """origin's fetch and push URLs: example/repo on GitHub, for which the tests' local
        bare repository stands in."""
        return self.urls

    def ci_status(self, repo: Path, github: str, sha: str) -> str:
        assert github == "example/repo"
        self.asked.append(sha)
        return self.status

    def open_pr(self, worktree: Path, github: str, branch: str, title: str, body: str) -> str:
        assert github == "example/repo"
        self.prs.append((branch, title, body))
        return f"https://github.com/example/pull/{len(self.prs)}"

    def create_release(self, repo: Path, github: str, tag: str, notes: str) -> str:
        assert github == "example/repo"
        self.releases.append((tag, notes))
        return f"https://github.com/example/repo/releases/tag/{tag}"

    def uv_lock(self, worktree: Path) -> None:
        self.locked.append(worktree)
        lock = worktree / "uv.lock"
        lock.write_bytes(lock.read_bytes().replace(b"0.0.1", b"0.0.2"))


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(dev_project, "ci_status", fake.ci_status)
    monkeypatch.setattr(dev_project, "open_pr", fake.open_pr)
    monkeypatch.setattr(dev_project, "create_release", fake.create_release)
    monkeypatch.setattr(dev_project, "uv_lock", fake.uv_lock)
    monkeypatch.setattr(dev_project, "origin_urls", fake.origin_urls)
    return fake


@pytest.fixture
def repos(repo_templates: RepoTemplates, tmp_path: Path) -> Path:
    """A copy of make_origin_and_clone's repositories, with a commit in clone that origin
    hasn't got, which a release mustn't tag."""
    repo_templates.build("release-origin-and-clone", make_origin_and_clone)
    directory = repo_templates.copy("release-origin-and-clone", tmp_path / "repos")
    clone = Git(directory / "clone")
    (directory / "clone" / "local").write_bytes(b"local\n")
    clone.run("add", "local")
    clone.run("commit", "--quiet", "-m", "not pushed")
    return directory


def test_release_tags_origins_main_publishes_it_and_opens_the_next_versions_pr(
    repos: Path, github: FakeGitHub
) -> None:
    origin = Git(repos / "origin.git")
    main = origin.run("rev-parse", "main")

    tag, release, url = dev_project.publish(repos / "clone", "0.0.1")

    assert (tag, release, url) == (
        "v0.0.1",
        "https://github.com/example/repo/releases/tag/v0.0.1",
        "https://github.com/example/pull/1",
    )
    assert github.asked == [main]
    assert github.releases == [("v0.0.1", "### Added\n\n- First.")]
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


def test_release_refuses_an_undated_changelog_before_tagging(
    repos: Path, github: FakeGitHub
) -> None:
    clone = Git(repos / "clone")
    clone.run("switch", "--quiet", "-c", "undated", "origin/main")
    changelog = repos / "clone" / "CHANGELOG.md"
    changelog.write_bytes(CHANGELOG.replace("0.0.1 (2026-01-02)", "0.0.1 (unreleased)").encode())
    clone.run("commit", "--quiet", "-am", "undate")
    clone.run("push", "--quiet", "origin", "undated:main")
    with pytest.raises(ReleaseError, match="unreleased"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("tag") == ""
    assert github.releases == []


def test_release_says_how_to_finish_when_the_release_fails(
    repos: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(repo: Path, github: str, tag: str, notes: str) -> str:
        raise ReleaseError("gh release create failed")

    monkeypatch.setattr(dev_project, "create_release", fail)
    with pytest.raises(
        ReleaseError,
        match=r"(?s)pushed v0\.0\.1.*gh release create failed.*by hand.*gh release create v0\.0\.1"
        r".*start-0\.0\.2",
    ):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("cat-file", "-t", "v0.0.1") == "tag"
    assert github.prs == []


def test_release_says_how_to_finish_when_the_next_version_fails(
    repos: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(worktree: Path, github: str, branch: str, title: str, body: str) -> str:
        raise ReleaseError("gh pr create failed")

    monkeypatch.setattr(dev_project, "open_pr", fail)
    with pytest.raises(
        ReleaseError, match=r"(?s)pushed v0\.0\.1.*gh pr create failed.*by hand"
    ) as e:
        dev_project.publish(repos / "clone", "0.0.1")
    assert "gh release create" not in str(e.value)  # published already
    assert Git(repos / "origin.git").run("cat-file", "-t", "v0.0.1") == "tag"
    assert len(Git(repos / "clone").run("worktree", "list").splitlines()) == 1


def test_release_command_names_a_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        return None if name == "gh" else name

    monkeypatch.setattr(dev_project.shutil, "which", which)
    with pytest.raises(SystemExit, match="needs gh"):
        dev_project.release(["0.0.1"])


def test_release_refuses_and_keeps_a_local_next_version_branch(
    repos: Path, github: FakeGitHub
) -> None:
    clone = Git(repos / "clone")
    clone.run("branch", "start-0.0.2")
    mine = clone.run("rev-parse", "start-0.0.2")
    with pytest.raises(ReleaseError, match="start-0.0.2.* exists locally"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert clone.run("rev-parse", "start-0.0.2") == mine
    assert Git(repos / "origin.git").run("tag") == ""


def test_release_refuses_a_next_version_branch_on_origin(repos: Path, github: FakeGitHub) -> None:
    Git(repos / "origin.git").run("branch", "start-0.0.2", "main")
    with pytest.raises(ReleaseError, match="start-0.0.2.* exists on origin"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "origin.git").run("tag") == ""


def test_release_refuses_an_origin_not_on_github(repos: Path, github: FakeGitHub) -> None:
    github.urls = ["/srv/repo.git"]
    with pytest.raises(ReleaseError, match="only github.com is supported"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert github.asked == []
    assert Git(repos / "origin.git").run("tag") == ""


def test_release_says_how_to_finish_when_interrupted_after_the_tag(
    repos: Path,
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(worktree: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dev_project, "uv_lock", interrupt)
    with pytest.raises(KeyboardInterrupt):
        dev_project.publish(repos / "clone", "0.0.1")
    assert re.search(r"(?s)pushed v0\.0\.1.*by hand", capsys.readouterr().err)
    assert len(Git(repos / "clone").run("worktree", "list").splitlines()) == 1


def test_release_drops_the_tag_it_couldnt_push(repos: Path, github: FakeGitHub) -> None:
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.pushurl", (repos / "missing.git").as_posix())
    with pytest.raises(ReleaseError, match="push"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert clone.run("tag") == ""


@pytest.mark.parametrize(
    ("url", "rewrite"),
    [
        ("gh:example/repo", {"git@github.com:": "gh:"}),
        ("gh:example/repo", {"https://github.com/": "gh:"}),
        ("https://github.com/example/repo.git", {"git@github.com:": "https://github.com/"}),
    ],
)
def test_origin_github_repo_follows_insteadof(
    repos: Path, url: str, rewrite: dict[str, str]
) -> None:
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", url)
    for base, alias in rewrite.items():
        clone.run("config", f"url.{base}.insteadOf", alias)
    assert dev_project.origin_github_repo(repos / "clone") == "example/repo"


def test_origin_github_repo_refuses_a_local_origin(repos: Path) -> None:
    with pytest.raises(ReleaseError, match="only github.com is supported"):
        dev_project.origin_github_repo(repos / "clone")


def test_origin_github_repo_refuses_github_rewritten_elsewhere(repos: Path) -> None:
    # CI would be checked on GitHub, but the tag pushed to the mirror.
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", GITHUB_ORIGIN)
    clone.run("config", "url.https://mirror.example/.insteadOf", "https://github.com/")
    with pytest.raises(ReleaseError, match="only github.com is supported"):
        dev_project.origin_github_repo(repos / "clone")


def test_origin_github_repo_refuses_pushing_elsewhere(repos: Path) -> None:
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", GITHUB_ORIGIN)
    clone.run("config", "remote.origin.pushurl", "git@github.com:other/repo.git")
    with pytest.raises(ReleaseError, match="more than one repository.*other/repo"):
        dev_project.origin_github_repo(repos / "clone")


def test_origin_github_repo_follows_pushinsteadof(repos: Path) -> None:
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", "gh:example/repo")
    clone.run("config", "url.https://github.com/.insteadOf", "gh:")
    clone.run("config", "url.git@github.com:other/.pushInsteadOf", "gh:example/")
    with pytest.raises(ReleaseError, match="more than one repository.*other/repo"):
        dev_project.origin_github_repo(repos / "clone")


def test_release_keeps_a_next_version_branch_it_didnt_create(
    repos: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The branch appears between the refusals and `worktree add` (say, made in another
    # terminal), so `worktree add -b` fails; the cleanup must leave it.
    real_git = dev_project._git  # pyright: ignore[reportPrivateUsage]

    def git(repo: Path, *args: str, stdin: str | None = None) -> str:
        if args[:2] == ("worktree", "add"):
            real_git(repo, "branch", "start-0.0.2", "origin/main")
        return real_git(repo, *args, stdin=stdin)

    monkeypatch.setattr(dev_project, "_git", git)
    with pytest.raises(ReleaseError, match="already exists"):
        dev_project.publish(repos / "clone", "0.0.1")
    assert Git(repos / "clone").ok("rev-parse", "--verify", "--quiet", "refs/heads/start-0.0.2")


@pytest.mark.parametrize("key", ["remote.origin.url", "remote.origin.pushurl"])
def test_origin_github_repo_refuses_a_second_url_elsewhere(repos: Path, key: str) -> None:
    # git fetches from the first URL but pushes to every one.
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", GITHUB_ORIGIN)
    if key == "remote.origin.pushurl":
        clone.run("config", key, GITHUB_ORIGIN)
    clone.run("config", "--add", key, "git@github.com:other/repo.git")
    with pytest.raises(ReleaseError, match="more than one repository.*other/repo"):
        dev_project.origin_github_repo(repos / "clone")


def test_origin_github_repo_ignores_case(repos: Path) -> None:
    clone = Git(repos / "clone")
    clone.run("config", "remote.origin.url", "https://github.com/Example/Repo")
    clone.run("config", "remote.origin.pushurl", "git@github.com:example/repo.git")
    assert dev_project.origin_github_repo(repos / "clone") == "Example/Repo"
