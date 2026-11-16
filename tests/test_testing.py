from pathlib import Path

import pytest

from git_client import Git, config
from git_client.testing import FakeGit, RepoTemplates, UnexpectedCommand, real_git


def test_git_sees_the_tests_config_not_the_users(repo: Git, git_config: Path) -> None:
    entries = config(repo)

    assert entries["user.name"] == ["Test"]
    assert entries["maintenance.auto"] == ["false"]
    assert git_config.is_file()


def test_real_git_is_git_itself() -> None:
    found = Path(real_git())

    assert found.is_absolute()
    assert Git(".", program=str(found)).run("--version").startswith("git version")


def make_origin_and_clone(directory: Path) -> None:
    Git(directory).run("init", "--quiet", "--bare", str(directory / "origin.git"))
    Git(directory).run("clone", "--quiet", str(directory / "origin.git"), str(directory / "clone"))
    clone = Git(directory / "clone")
    clone.run("commit", "--quiet", "--allow-empty", "-m", "first")
    clone.run("push", "--quiet", "origin", "HEAD:main")


def test_a_copy_reaches_its_own_copy_of_each_remote(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    repo_templates.build("origin-and-clone", make_origin_and_clone)
    first = repo_templates.copy("origin-and-clone", tmp_path / "first")
    second = repo_templates.copy("origin-and-clone", tmp_path / "second")

    Git(first / "clone").run("commit", "--quiet", "--allow-empty", "-m", "only in first")
    Git(first / "clone").run("push", "--quiet", "origin", "HEAD:main")

    assert Git(first / "origin.git").run("log", "-1", "--format=%s", "main") == "only in first"
    assert Git(second / "origin.git").run("log", "-1", "--format=%s", "main") == "first"


def test_a_template_is_built_once(repo_templates: RepoTemplates, tmp_path: Path) -> None:
    builds: list[Path] = []

    for _ in range(2):
        repo_templates.build("counted", builds.append)

    assert len(builds) == 1


def test_fake_git_answers_from_its_responses_and_records_each_command() -> None:
    git = FakeGit()
    git.respond(["rev-parse", "--abbrev-ref"], stdout="main\n")
    git.respond(["merge-base", "--is-ancestor"], returncode=1)

    assert git.run("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not git.ok("merge-base", "--is-ancestor", "a", "b")
    assert git.commands == [
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["merge-base", "--is-ancestor", "a", "b"],
    ]


def test_fake_git_fails_on_a_command_it_has_no_response_for() -> None:
    git = FakeGit()

    with pytest.raises(UnexpectedCommand, match="git push origin main"):
        git.run("push", "origin", "main")
