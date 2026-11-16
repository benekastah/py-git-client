import subprocess
import sys
from pathlib import Path

import pytest

from git_client import CatFile, Git, RefUpdate, config, update_refs
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
    template = tmp_path / "template"
    template.mkdir()
    (template / "from-the-users-template").write_text("", encoding="utf-8")
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("ignored-by-user\n", encoding="utf-8")
    pytester.makepyfile(
        """
        import os
        from pathlib import Path

        import pytest

        from git_client import Git, GitError


        def test_inside(tmp_path: Path) -> None:
            git = Git(tmp_path)
            git.run("init", "--quiet")
            (tmp_path / "ignored-by-user").write_text("x", encoding="utf-8")
            assert git.run("status", "--porcelain") == "?? ignored-by-user"
            git.run("add", "ignored-by-user")
            assert (tmp_path / ".git" / "index").is_file()
            assert not (tmp_path / ".git" / "from-the-users-template").exists()
            # An editor that leaves the message as it is.
            git.run("commit", "--quiet", "--allow-empty", "--edit", "-m", "first")
            assert git.run("log", "-1", "--format=%an %cn") == "Test Test"
            git.run("rebase", "--quiet", "--interactive", "HEAD")
            # No network, and no asking for a password.
            with pytest.raises(GitError, match="transport 'https' not allowed"):
                git.run("ls-remote", "https://example.invalid/repo.git")
            assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
        """
    )
    (tmp_path / "system-config").write_text("[user]\n\tname = From The System\n", encoding="utf-8")
    Git(tmp_path).run("init", "--quiet", "hooks-repository")
    for name, value in {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_DIR": str(tmp_path / "hooks-repository" / ".git"),
        "GIT_AUTHOR_NAME": "From A Hook",
        "GIT_COMMITTER_NAME": "From A Hook",
        "GIT_INDEX_FILE": str(tmp_path / "hooks-index"),
        "GIT_TEMPLATE_DIR": str(template),
        "GIT_CONFIG_PARAMETERS": "'user.name'='From A Hook'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "From A Hook",
        "GIT_CONFIG_SYSTEM": str(tmp_path / "system-config"),
        "GIT_EDITOR": "false",
        "GIT_SEQUENCE_EDITOR": "false",
        "GIT_ALLOW_PROTOCOL": "file:https",
        "GIT_TERMINAL_PROMPT": "1",
    }.items():
        monkeypatch.setenv(name, value)

    result = pytester.runpytest_subprocess("-p", "git_client.testing", "-p", "no:xdist")

    result.assert_outcomes(passed=1)


def test_a_projects_own_git_config_fixture_keeps_the_isolation(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest(
        """
        from pathlib import Path

        import pytest

        from git_client.testing import TEST_GIT_CONFIG


        @pytest.fixture(scope="session")
        def git_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
            config = tmp_path_factory.mktemp("own") / "config"
            config.write_text(TEST_GIT_CONFIG, encoding="utf-8")
            return config
        """
    )
    pytester.makepyfile(
        """
        import os
        from pathlib import Path

        from git_client import Git


        def test_inside(tmp_path: Path, git_config: Path) -> None:
            assert "GIT_DIR" not in os.environ
            assert os.environ["GIT_CONFIG_GLOBAL"] == str(git_config)
            Git(tmp_path).run("init", "--quiet")
        """
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))

    result = pytester.runpytest_subprocess("-p", "git_client.testing", "-p", "no:xdist")

    result.assert_outcomes(passed=1)


def test_each_test_has_a_config_of_its_own_with_the_projects_entries(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
        import pytest

        from git_client.testing import TEST_GIT_CONFIG


        @pytest.fixture(scope="session")
        def git_config_text() -> str:
            return TEST_GIT_CONFIG + "[commit]\\n\\tgpgSign = false\\n"
        """
    )
    pytester.makepyfile(
        """
        from pathlib import Path

        from git_client import Git, config


        def test_first(tmp_path: Path) -> None:
            Git(tmp_path).run("config", "--global", "leak.x", "1")


        def test_second(tmp_path: Path) -> None:
            Git(tmp_path).run("init", "--quiet")
            entries = config(Git(tmp_path))
            assert "leak.x" not in entries
            assert entries["commit.gpgsign"] == ["false"]
            assert entries["user.name"] == ["Test"]
        """
    )

    result = pytester.runpytest_subprocess("-p", "git_client.testing", "-p", "no:xdist")

    result.assert_outcomes(passed=2)


def test_templates_build_under_the_sessions_config(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    def make(directory: Path) -> None:
        Git(directory).run("init", "--quiet", "repo")
        Git(directory / "repo").run("commit", "--quiet", "--allow-empty", "-m", "first")

    Git(tmp_path).run("config", "--global", "user.name", "Only This Test")
    repo_templates.build("under-session-config", make)

    copy = repo_templates.copy("under-session-config", tmp_path / "copy") / "repo"
    assert Git(copy).run("log", "-1", "--format=%an") == "Test"


def test_git_calls_records_each_git_process_and_lets_it_run(
    repo: Git, git_calls: list[list[str]]
) -> None:
    repo.run("commit", "--quiet", "--allow-empty", "-m", "second")
    with CatFile(repo) as cat_file:
        cat_file.resolve("HEAD")

    assert repo.run("log", "-1", "--format=%s") == "second"
    assert git_calls == [
        ["commit", "--quiet", "--allow-empty", "-m", "second"],
        ["cat-file", "--batch-command"],
        ["log", "-1", "--format=%s"],
    ]


def test_real_git_is_the_git_in_gits_exec_path() -> None:
    exec_path = Path(Git(".", program=real_git()).run("--exec-path"))
    beside = exec_path / ("git.exe" if sys.platform == "win32" else "git")

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


def make_symlink(directory: Path) -> None:
    Git(directory).run("init", "--quiet", "repo")
    (directory / "repo" / "a").write_bytes(b"a\n")
    (directory / "repo" / "link").symlink_to("a")
    (directory / "repo" / "dangling").symlink_to("no-such-file")
    Git(directory / "repo").run("add", ".")
    Git(directory / "repo").run("commit", "--quiet", "-m", "links")


def test_a_copy_keeps_symlinks_as_they_are(repo_templates: RepoTemplates, tmp_path: Path) -> None:
    try:
        (tmp_path / "link").symlink_to("target")
    except OSError:
        pytest.skip("symlinks can't be made here (Windows without developer mode)")
    repo_templates.build("symlink", make_symlink)

    copy = repo_templates.copy("symlink", tmp_path / "copy") / "repo"

    assert Git(copy).run("status", "--porcelain") == ""
    assert (copy / "link").readlink() == Path("a")


def test_a_template_is_built_once(repo_templates: RepoTemplates) -> None:
    builds: list[Path] = []

    for _ in range(2):
        repo_templates.build("counted", builds.append)

    assert len(builds) == 1


def test_a_build_that_fails_leaves_nothing_behind(repo_templates: RepoTemplates) -> None:
    def fails(directory: Path) -> None:
        # A commit, so there are read-only objects to delete, which on Windows takes more.
        Git(directory).run("init", "--quiet", "repo")
        Git(directory / "repo").run("commit", "--quiet", "--allow-empty", "-m", "first")
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
