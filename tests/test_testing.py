import os
import subprocess
from pathlib import Path

import pytest

from git_client import Git, RefUpdate, config, update_refs
from git_client.testing import FakeGit, RepoTemplates, UnexpectedCommand, real_git

pytest_plugins = ["pytester"]


def test_git_sees_the_tests_config_not_the_users(repo: Git, git_config: Path) -> None:
    entries = config(repo)

    assert entries["user.name"] == ["Test"]
    assert entries["maintenance.auto"] == ["false"]
    assert git_config.is_file()


def test_a_session_is_cut_off_from_the_users_git_setup(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("ignored-by-user\n", encoding="utf-8")
    pytester.makepyfile(
        """
        from pathlib import Path

        from git_client import Git


        def test_inside(tmp_path: Path) -> None:
            git = Git(tmp_path)
            git.run("init", "--quiet")
            (tmp_path / "ignored-by-user").write_text("x", encoding="utf-8")
            assert git.run("status", "--porcelain") == "?? ignored-by-user"
            git.run("commit", "--quiet", "--allow-empty", "-m", "first")
            assert git.run("log", "-1", "--format=%an %cn") == "Test Test"
        """
    )
    for name, value in {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_AUTHOR_NAME": "From A Hook",
        "GIT_COMMITTER_NAME": "From A Hook",
        "GIT_INDEX_FILE": str(tmp_path / "hooks-index"),
    }.items():
        monkeypatch.setenv(name, value)

    result = pytester.runpytest_subprocess("-p", "git_client.testing", "-p", "no:xdist")

    result.assert_outcomes(passed=1)


def test_real_git_is_the_git_in_gits_exec_path() -> None:
    exec_path = Path(Git(".", program=real_git()).run("--exec-path"))
    beside = exec_path / ("git.exe" if os.name == "nt" else "git")

    if beside.exists():
        assert Path(real_git()) == beside
    assert Git(".", program=real_git()).run("--version").startswith("git version")


def make_origin_and_clone(directory: Path) -> None:
    Git(directory).run("init", "--quiet", "--bare", str(directory / "origin.git"))
    Git(directory).run("clone", "--quiet", str(directory / "origin.git"), str(directory / "clone"))
    clone = Git(directory / "clone")
    clone.run("commit", "--quiet", "--allow-empty", "-m", "first")
    clone.run("push", "--quiet", "origin", "HEAD:main")
    clone.run("config", "remote.origin.pushurl", str(directory / "origin.git"))


def test_a_copy_fetches_and_pushes_its_own_copy_of_each_remote(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    repo_templates.build("origin-and-clone", make_origin_and_clone)
    first = repo_templates.copy("origin-and-clone", tmp_path / "first")
    second = repo_templates.copy("origin-and-clone", tmp_path / "second")

    Git(first / "clone").run("commit", "--quiet", "--allow-empty", "-m", "only in first")
    Git(first / "clone").run("push", "--quiet", "origin", "HEAD:main")

    assert Git(first / "origin.git").run("log", "-1", "--format=%s", "main") == "only in first"
    assert Git(second / "origin.git").run("log", "-1", "--format=%s", "main") == "first"
    assert (
        Git(repo_templates.root / "origin-and-clone" / "origin.git").run("log", "-1", "--format=%s")
        == "first"
    )


def make_nested(directory: Path) -> None:
    Git(directory).run("init", "--quiet", "--bare", str(directory / "upstream.git"))
    Git(directory).run("init", "--quiet", str(directory / "deep" / "down" / "work"))
    work = Git(directory / "deep" / "down" / "work")
    work.run("remote", "add", "origin", str(directory / "upstream.git"))
    work.run("config", "--add", "remote.origin.url", str(directory / "upstream.git"))


def test_repoints_every_url_of_a_repository_however_deep(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    repo_templates.build("nested", make_nested)
    copy = repo_templates.copy("nested", tmp_path / "copy")

    urls = (
        Git(copy / "deep" / "down" / "work")
        .run("config", "--get-all", "remote.origin.url")
        .splitlines()
    )

    assert [Path(url) for url in urls] == [copy / "upstream.git", copy / "upstream.git"]


def test_copying_a_repository_without_remotes_starts_no_git(
    repo_templates: RepoTemplates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make_plain(directory: Path) -> None:
        Git(directory).run("init", "--quiet", "repo")

    repo_templates.build("plain", make_plain)

    def no_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("started git")

    monkeypatch.setattr(Git, "process", no_git)

    assert (repo_templates.copy("plain", tmp_path / "copy") / "repo" / ".git").is_dir()


def test_a_template_is_built_once(repo_templates: RepoTemplates) -> None:
    builds: list[Path] = []

    for _ in range(2):
        repo_templates.build("counted", builds.append)

    assert len(builds) == 1


def test_a_build_that_fails_leaves_nothing_behind(repo_templates: RepoTemplates) -> None:
    def fails(directory: Path) -> None:
        (directory / "half-made").write_text("", encoding="utf-8")
        raise RuntimeError("couldn't make it")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="couldn't make it"):
            repo_templates.build("fails", fails)


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


def test_fake_git_records_what_was_sent_on_stdin() -> None:
    git = FakeGit()
    git.respond(["update-ref"])

    update_refs(git, [RefUpdate("refs/heads/old", None, old="1" * 40)])

    assert git.calls[-1].input == b"delete refs/heads/old\0" + b"1" * 40 + b"\0"


def test_fake_git_fails_on_a_command_it_has_no_response_for() -> None:
    git = FakeGit()

    with pytest.raises(UnexpectedCommand, match="git push origin main"):
        git.run("push", "origin", "main")
