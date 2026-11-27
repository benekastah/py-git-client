from pathlib import Path

import pytest

from git_client import ConfigType, Git, GitError, color_enabled, config_values
from git_client.testing import GitCall


def _set(git: Git, text: str) -> None:
    with (Path(git.directory) / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write(text)


@pytest.mark.parametrize(
    ("type", "written", "read"),
    [
        (None, "Hello", "Hello"),
        ("bool", "yes", True),
        ("bool", "off", False),
        ("int", "7k", 7168),
        ("bool-or-int", "on", True),
        ("bool-or-int", "3", 3),
        ("bool-or-str", "no", False),
        ("bool-or-str", "copies", "copies"),
        ("expiry-date", "never", 0),
        ("color", "red", "\x1b[31m"),
    ],
)
def test_config_values_reads_each_value_as_git_reads_its_type(
    repo: Git, type: ConfigType | None, written: str, read: object
) -> None:
    repo.run("config", "tool.key", written)

    assert config_values(repo.read_only(), "tool.key", type=type) == {"tool.key": read}


def test_config_values_reads_a_path_as_git_expands_it(repo: Git, tmp_path: Path) -> None:
    git = Git(repo.directory, env={"HOME": str(tmp_path)})
    git.run("config", "tool.log", "~/logs")

    assert config_values(git, "tool.log", type="path") == {"tool.log": tmp_path / "logs"}


def test_config_values_reads_many_keys_with_one_git(repo: Git, git_calls: list[GitCall]) -> None:
    _set(repo, "[tool]\n\tlimit = 1\n\tlimit = 2k\n\tother = 3\n")
    git_calls.clear()

    values = config_values(repo, "tool.limit", "tool.missing", "tool.other", type="int")

    # A key's last value, the one git uses.
    assert values == {"tool.limit": 2048, "tool.missing": None, "tool.other": 3}
    assert len(git_calls) == 1


def test_config_values_reads_a_key_with_no_value_as_git_does(repo: Git) -> None:
    _set(repo, "[tool]\n\tflag\n")

    assert config_values(repo, "tool.flag", type="bool") == {"tool.flag": True}
    assert config_values(repo, "tool.flag") == {"tool.flag": ""}


def test_config_values_finds_keys_only_as_git_prints_them(repo: Git) -> None:
    repo.run("config", "Commit.gpgSign", "true")
    repo.run("config", "branch.Topic.remote", "origin")

    # Alone, first, in the middle or last: not one finds a key git prints otherwise.
    for keys in [("commit.gpgSign",), ("commit.gpgSign", "a.b", "Commit.GPGSIGN")]:
        assert config_values(repo, *keys, type="bool") == dict.fromkeys(keys)
    assert config_values(repo, "commit.gpgsign", "branch.Topic.remote") == {
        "commit.gpgsign": "true",
        "branch.Topic.remote": "origin",
    }


def test_config_values_takes_a_key_as_it_is_not_as_a_pattern(repo: Git) -> None:
    repo.run("config", "branch.x+y.remote", "plus")
    repo.run("config", "branch.xxy.remote", "letters")

    assert config_values(repo, "branch.x+y.remote", "branch.x.y.remote") == {
        "branch.x+y.remote": "plus",
        "branch.x.y.remote": None,
    }


def test_config_values_raises_for_a_value_git_cant_read_as_the_type(repo: Git) -> None:
    repo.run("config", "tool.flag", "maybe")

    with pytest.raises(GitError, match="bad boolean config value 'maybe' for 'tool.flag'"):
        config_values(repo, "tool.flag", type="bool")


def test_config_values_reads_the_users_config_outside_a_repository(tmp_path: Path) -> None:
    assert config_values(Git(tmp_path), "user.name") == {"user.name": "Test"}


def test_config_values_of_no_keys_starts_no_git(repo: Git, git_calls: list[GitCall]) -> None:
    assert config_values(repo, type="bool") == {}
    assert git_calls == []


@pytest.mark.parametrize(
    ("settings", "stdout_is_tty", "enabled"),
    [
        ("", True, True),  # auto, by default
        ("", False, False),
        ("[color]\n\tstack = always\n", False, True),
        ("[color]\n\tstack = false\n", True, False),
        ("[color]\n\tui = never\n", True, False),  # color.ui, where color.stack isn't set
        ("[color]\n\tui = never\n\tstack = auto\n", True, True),
    ],
)
def test_color_enabled_says_what_git_decides(
    repo: Git, settings: str, stdout_is_tty: bool, enabled: bool
) -> None:
    _set(repo, settings)
    git = Git(repo.directory, env={"TERM": "xterm"})

    assert color_enabled(git.read_only(), "color.stack", stdout_is_tty=stdout_is_tty) is enabled


def test_color_enabled_is_off_on_a_dumb_terminal_as_git_decides(repo: Git) -> None:
    git = Git(repo.directory, env={"TERM": "dumb"})

    assert color_enabled(git, "color.stack", stdout_is_tty=True) is False


def test_color_enabled_raises_for_a_value_git_cant_read(repo: Git) -> None:
    repo.run("config", "color.stack", "maybe")

    with pytest.raises(GitError, match="bad boolean config value 'maybe' for 'color.stack'"):
        color_enabled(repo, "color.stack", stdout_is_tty=True)
