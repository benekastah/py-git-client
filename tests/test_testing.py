import sys
from pathlib import Path

import pytest

from git_client import CatFile, Git, ReadOnlyError, ReadOnlyGit, config, find_git
from git_client.testing import FakeGit, RepoTemplates, UnexpectedCommand, real_git

pytest_plugins = ["pytester"]


def test_git_sees_the_tests_config_not_the_users(repo: Git, git_config: Path) -> None:
    assert config(repo)["user.name"] == ["Test"]
    assert git_config.is_file()


def test_git_starts_no_background_maintenance(repo: Git, tmp_path: Path) -> None:
    traced = Git(repo.directory, env={"GIT_TRACE": str(tmp_path / "trace")})

    traced.run("commit", "--quiet", "--allow-empty", "-m", "second")

    assert "maintenance" not in (tmp_path / "trace").read_text(encoding="utf-8")


def test_a_session_is_cut_off_from_the_users_git_setup(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    template = tmp_path / "template"
    template.mkdir()
    (template / "from-the-users-template").write_text("", encoding="utf-8")
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("ignored-by-user\n", encoding="utf-8")
    (home / ".config" / "git" / "attributes").write_text("* diff=mine\n", encoding="utf-8")
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
            assert git.run("check-attr", "diff", "--", "a") == "a: diff: unspecified"
            git.run("add", "ignored-by-user")
            assert (tmp_path / ".git" / "index").is_file()
            assert not (tmp_path / ".git" / "from-the-users-template").exists()
            assert git.run("rev-parse", "--show-object-format") == "sha1"
            assert not (tmp_path / ".git" / "reftable").exists()
            # An editor that leaves the message as it is.
            git.run("commit", "--quiet", "--allow-empty", "--edit", "-m", "first")
            assert git.run("log", "-1", "--format=%an %cn") == "Test Test"
            git.run("rebase", "--quiet", "--interactive", "HEAD")
            # No network, and no asking for a password.
            with pytest.raises(GitError, match="transport 'https' not allowed"):
                git.run("ls-remote", "https://example.invalid/repo.git")
            assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
            with pytest.raises(GitError, match="terminal prompts disabled"):
                git.run("credential", "fill", input="protocol=https\\nhost=example.invalid\\n\\n")
        """
    )
    # Which the tests' config doesn't set: commits would fail.
    (tmp_path / "system-config").write_text(
        "[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = no-such-gpg\n", encoding="utf-8"
    )
    # A program that answers a password prompt, as a desktop's (e.g. KDE's ksshaskpass) does.
    askpass = tmp_path / "askpass"
    askpass.write_text(f"#!{sys.executable}\nprint('secret')\n", encoding="utf-8")
    askpass.chmod(0o755)
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
        "GIT_DEFAULT_HASH": "sha256",
        "GIT_DEFAULT_REF_FORMAT": "reftable",
        "GIT_ASKPASS": str(askpass),
        "SSH_ASKPASS": str(askpass),
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


def test_outside_any_repository_runs_a_test_where_git_finds_none(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        from pathlib import Path

        import pytest

        from git_client import Git, NotARepositoryError


        @pytest.mark.usefixtures("outside_any_repository")
        def test_inside() -> None:
            assert list(Path.cwd().iterdir()) == []
            with pytest.raises(NotARepositoryError):
                Git().run("rev-parse", "--git-dir")
        """
    )
    # Its temporary directories in a repository, as they are with --basetemp in a project.
    Git(pytester.path).run("init", "--quiet", "project")

    result = pytester.runpytest_subprocess(
        "-p",
        "git_client.testing",
        "-p",
        "no:xdist",
        f"--basetemp={pytester.path / 'project' / 'tmp'}",
    )

    result.assert_outcomes(passed=1)


def test_git_finds_no_repository_above_any_temporary_directory(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        """
        import tempfile
        from pathlib import Path

        import pytest

        from git_client import Git, NotARepositoryError


        def test_in_pytests_temporary_directory(tmp_path: Path) -> None:
            with pytest.raises(NotARepositoryError):
                Git(tmp_path).run("rev-parse", "--git-dir")


        def test_in_pythons_temporary_directory() -> None:
            with pytest.raises(NotARepositoryError):
                Git(tempfile.mkdtemp()).run("rev-parse", "--git-dir")


        def test_below_the_users_own_ceiling() -> None:
            with pytest.raises(NotARepositoryError):
                Git(Path("project", "elsewhere", "sub")).run("rev-parse", "--git-dir")
        """
    )
    # Both kinds of temporary directory in a repository, as a project's tooling may put them.
    Git(pytester.path).run("init", "--quiet", "project")
    (pytester.path / "project" / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(pytester.path / "project" / "tmp"))
    # A ceiling of the user's own, outside both, which the tests' git keeps.
    (pytester.path / "project" / "elsewhere" / "sub").mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(pytester.path / "project" / "elsewhere"))

    result = pytester.runpytest_subprocess(
        "-p",
        "git_client.testing",
        "-p",
        "no:xdist",
        f"--basetemp={pytester.path / 'project' / 'basetemp'}",
    )

    result.assert_outcomes(passed=3)


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


def test_git_calls_records_a_read_only_clients_processes_but_not_what_it_refuses(
    repo: Git, git_calls: list[list[str]]
) -> None:
    git = ReadOnlyGit(repo.directory)
    git.run("log", "-1")
    with pytest.raises(ReadOnlyError):
        git.run("commit", "--allow-empty", "-m", "x")

    assert git_calls == [["log", "-1"]]


@pytest.fixture
def new_clone(request: pytest.FixtureRequest, repo_templates: RepoTemplates, tmp_path: Path) -> Git:
    """A copy of make_origin_and_clone's clone, from a template built for this test alone, so
    building it, and repointing the copy's remote, happen while this fixture is set up."""
    name = f"new-clone-{request.node.name}"
    repo_templates.build(name, make_origin_and_clone)
    return Git(repo_templates.copy(name, tmp_path / "copy") / "clone")


def test_git_calls_leaves_out_a_template_set_up_after_it(
    git_calls: list[list[str]], new_clone: Git
) -> None:
    new_clone.run("status", "--porcelain")

    assert git_calls == [["status", "--porcelain"]]


def test_git_calls_leaves_out_a_template_set_up_before_it(
    new_clone: Git, git_calls: list[list[str]]
) -> None:
    new_clone.run("status", "--porcelain")

    assert git_calls == [["status", "--porcelain"]]


def test_these_tests_run_the_real_git() -> None:
    assert find_git() == real_git()


def test_real_git_is_the_git_on_path_if_gits_exec_path_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path))
    real_git.cache_clear()
    try:
        assert real_git() == find_git()
    finally:
        real_git.cache_clear()


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


def traced_commands(trace: Path) -> list[str]:
    """The git commands GIT_TRACE wrote to `trace`, one per git process."""
    if not trace.exists():
        return []
    lines = trace.read_text(encoding="utf-8").splitlines()
    return [line.split("built-in: git ", 1)[1] for line in lines if "built-in: git " in line]


def test_a_copy_takes_one_git_per_url_it_repoints(
    repo_templates: RepoTemplates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_templates.build("origin-and-clone", make_origin_and_clone)
    monkeypatch.setenv("GIT_TRACE", str(tmp_path / "trace"))

    copy = repo_templates.copy("origin-and-clone", tmp_path / "copy")

    commands = traced_commands(tmp_path / "trace")
    assert [command.split()[0] for command in commands] == ["config", "config"]  # url, pushurl
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        assert Path(Git(copy / "clone").run("config", key)) == copy / "origin.git"


@pytest.mark.usefixtures("fresh_find_git")
def test_a_copy_runs_the_real_git_whatever_git_is_on_path(
    repo_templates: RepoTemplates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_templates.build("origin-and-clone", make_origin_and_clone)
    real_git()  # found once per process, before PATH changes, as in a session
    # A git on PATH that can't run, so that only real_git()'s does.
    name = "git.exe" if sys.platform == "win32" else "git"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / name).write_bytes(b"")
    (tmp_path / "bin" / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    find_git.cache_clear()

    copy = repo_templates.copy("origin-and-clone", tmp_path / "copy")

    url = Git(copy / "clone", program=real_git()).run("config", "remote.origin.url")
    assert Path(url) == copy / "origin.git"


def make_nested(directory: Path) -> None:
    # A name git quotes in its config file, for its "#".
    Git(directory).run("init", "--quiet", "--bare", str(directory / "up #1.git"))
    Git(directory).run("init", "--quiet", str(directory / "deep" / "down" / "work"))
    work = Git(directory / "deep" / "down" / "work")
    work.run("remote", "add", "origin", str(directory / "up #1.git"))
    work.run("config", "--add", "remote.origin.url", str(directory / "up #1.git"))
    work.run("remote", "add", "elsewhere", "https://example.org/x.git")
    # As a person might write it: the old form of section, and a comment.
    upstream = (directory / "up #1.git").as_posix()
    with (directory / "deep" / "down" / "work" / ".git" / "config").open(
        "a", encoding="utf-8"
    ) as config:
        config.write(f'[remote.ByHand]\n\tURL = "{upstream}" ; written by hand\n')


def test_repoints_every_url_of_a_repository_however_deep(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    repo_templates.build("nested", make_nested)
    copy = repo_templates.copy("nested", tmp_path / "copy")

    work = Git(copy / "deep" / "down" / "work")
    urls = work.run("config", "--get-all", "remote.origin.url").splitlines()

    assert [Path(url) for url in urls] == [copy / "up #1.git", copy / "up #1.git"]
    assert work.run("config", "remote.elsewhere.url") == "https://example.org/x.git"
    assert Path(work.run("config", "remote.byhand.url")) == copy / "up #1.git"


def make_file_url_remotes(directory: Path) -> None:
    Git(directory).run("init", "--quiet", "--bare", str(directory / "origin.git"))
    Git(directory).run("init", "--quiet", str(directory / "work"))
    work = Git(directory / "work")
    work.run("remote", "add", "origin", (directory / "origin.git").as_uri())
    work.run("remote", "add", "outside", (directory.parent / "elsewhere.git").as_uri())


def test_repoints_a_file_url_into_the_template(
    repo_templates: RepoTemplates, tmp_path: Path
) -> None:
    repo_templates.build("file-urls", make_file_url_remotes)
    copy = repo_templates.copy("file-urls", tmp_path / "copy")

    work = Git(copy / "work")
    assert work.run("config", "remote.origin.url") == (copy / "origin.git").as_uri()
    outside = (repo_templates.root / "elsewhere.git").as_uri()
    assert work.run("config", "remote.outside.url") == outside


def test_a_template_keeps_no_sample_hooks(repo_templates: RepoTemplates, tmp_path: Path) -> None:
    repo_templates.build("origin-and-clone", make_origin_and_clone)

    copy = repo_templates.copy("origin-and-clone", tmp_path / "copy")

    for hooks in (copy / "clone" / ".git" / "hooks", copy / "origin.git" / "hooks"):
        assert hooks.is_dir()
        assert list(hooks.iterdir()) == []


def test_copying_a_repository_without_remotes_starts_no_git(
    repo_templates: RepoTemplates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make_plain(directory: Path) -> None:
        Git(directory).run("init", "--quiet", "repo")

    repo_templates.build("plain", make_plain)
    monkeypatch.setenv("GIT_TRACE", str(tmp_path / "trace"))

    assert (repo_templates.copy("plain", tmp_path / "copy") / "repo" / ".git").is_dir()
    assert traced_commands(tmp_path / "trace") == []


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


def test_fake_git_answers_with_the_first_response_that_matches() -> None:
    git = FakeGit()
    git.respond(["rev-parse", "--verify"], stdout="first\n")
    git.respond(["rev-parse"], stdout="second\n")

    assert git.run("rev-parse", "--verify", "HEAD") == "first"
    assert git.run("rev-parse", "HEAD") == "second"


def test_fake_git_records_what_was_sent_on_stdin() -> None:
    git = FakeGit()
    git.respond(["update-ref"])

    git.run("update-ref", "--stdin", input="delete refs/heads/old\n")

    assert git.calls[-1].input == b"delete refs/heads/old\n"


def test_fake_git_records_what_each_command_added_to_the_environment() -> None:
    git = FakeGit()
    git.env["GIT_AUTHOR_NAME"] = "Client"
    git.respond(["commit"])

    git.run("commit", "-m", "x", env={"GIT_AUTHOR_DATE": "2000-01-01T00:00:00"})

    assert git.calls[-1].env == {
        "GIT_AUTHOR_NAME": "Client",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00",
    }


def test_fake_git_cant_start_a_long_lived_git() -> None:
    with CatFile(FakeGit()) as cat_file, pytest.raises(UnexpectedCommand, match="cat-file"):
        cat_file.resolve("HEAD")


def test_fake_git_fails_on_a_command_it_has_no_response_for() -> None:
    git = FakeGit()

    with pytest.raises(UnexpectedCommand, match="git push origin main"):
        git.run("push", "origin", "main")
