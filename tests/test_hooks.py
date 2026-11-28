import sys
from pathlib import Path

import pytest

from git_client import Git, GitError, HookResult, decode, encode, run_hook, stripspace
from git_client.testing import FakeGit, GitCall


def hook(git: Git, name: str, script: str) -> None:
    """A hook, `name`, in git's sh, which runs it on every system."""
    path = Path(git.directory) / ".git" / "hooks" / name
    path.write_text(f"#!/bin/sh\n{script}", encoding="utf-8")
    path.chmod(0o755)


def test_run_hook_gives_the_hooks_exit_status_and_output(
    repo: Git, git_calls: list[GitCall]
) -> None:
    # git sends a hook's stdout to its stderr, all of it in HookResult.stderr.
    hook(repo, "commit-msg", 'echo "out $*"; echo err >&2; exit 3\n')

    result = run_hook(repo, "commit-msg", "-x", b"second arg")

    assert result == HookResult(3, "out -x second arg\nerr\n")
    assert [call[call.index("hook") :] for call in git_calls] == [
        ["hook", "run", "--ignore-missing", "commit-msg", "--", "-x", "second arg"]
    ]


def test_run_hook_lets_the_hook_change_a_file_it_was_given(repo: Git, tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_bytes(b"subject\n")
    hook(repo, "commit-msg", 'printf "\\nSigned-off-by: Hook\\n" >> "$1"\n')

    assert run_hook(repo, "commit-msg", str(message)).returncode == 0
    assert message.read_bytes() == b"subject\n\nSigned-off-by: Hook\n"


def test_run_hook_follows_git_config_to_the_hook(repo: Git, tmp_path: Path) -> None:
    hooks = tmp_path / "elsewhere"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 5\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    repo.run("config", "core.hooksPath", hooks.as_posix())

    assert run_hook(repo, "pre-commit").returncode == 5


def test_run_hook_without_a_hook(repo: Git) -> None:
    assert run_hook(repo, "commit-msg") == HookResult(0, "")
    with pytest.raises(GitError, match="cannot find a hook named commit-msg"):
        run_hook(repo, "commit-msg", ignore_missing=False)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit to leave off")
def test_run_hook_raises_without_a_hook_git_would_run(repo: Git) -> None:
    # git ignores a hook that isn't executable, and says so in hints before its refusal.
    hook(repo, "commit-msg", "exit 0\n")
    (Path(repo.directory) / ".git" / "hooks" / "commit-msg").chmod(0o644)

    assert run_hook(repo, "commit-msg").returncode == 0
    with pytest.raises(GitError, match="cannot find a hook named commit-msg"):
        run_hook(repo, "commit-msg", ignore_missing=False)


def test_run_hook_and_a_name_git_doesnt_know(repo: Git, git_version: tuple[int, int, int]) -> None:
    if git_version >= (2, 56):  # git refuses it, rather than run a hook
        with pytest.raises(GitError, match="unknown hook event 'mine'"):
            run_hook(repo, "mine")
    elif git_version < (2, 37):  # git runs it, as any other, and there is none
        assert run_hook(repo, "mine") == HookResult(0, "")
    else:
        pytest.skip("which git between 2.37 and 2.56 began to refuse it isn't pinned here")


def test_run_hook_raises_when_git_refuses_the_name() -> None:
    # As git 2.56 says, with its hints first.
    git = FakeGit()
    git.respond(
        ["hook"],
        stderr="hint: a hint\nerror: unknown hook event 'mine';\n"
        "use --allow-unknown-hook-name to allow non-native hook names\n",
        returncode=1,
    )
    with pytest.raises(GitError, match="unknown hook event"):
        run_hook(git, "mine")


def test_run_hook_refuses_a_name_git_would_read_as_an_option(git_calls: list[GitCall]) -> None:
    with pytest.raises(ValueError, match="option"):
        run_hook(FakeGit(), "--ignore-missing")
    assert git_calls == []


def test_run_hook_passes_env_to_the_hook(repo: Git) -> None:
    hook(repo, "pre-commit", 'echo "$MY_VALUE" >&2\n')
    assert run_hook(repo, "pre-commit", env={"MY_VALUE": "given"}).stderr == "given\n"


@pytest.mark.parametrize(
    ("text", "strip_comments", "comment_char", "cleaned"),
    [
        ("  a  \n\n\n\n# c\nb\n\n", False, None, "  a\n\n# c\nb\n"),
        ("a\n# c\n; d\nb", True, None, "a\n; d\nb\n"),
        ("a\n# c\n; d\n", True, ";", "a\n# c\n"),
        ("a  \r\n\r\n\r\nb\r\n", False, None, "a\n\nb\n"),
        ("\n\n", False, None, ""),
    ],
)
def test_stripspace_cleans_text_as_git_does(
    repo: Git, text: str, strip_comments: bool, comment_char: str | None, cleaned: str
) -> None:
    found = stripspace(
        repo.read_only(), text, strip_comments=strip_comments, comment_char=comment_char
    )
    assert found == cleaned


def test_stripspace_takes_the_comment_character_from_config(repo: Git) -> None:
    repo.run("config", "core.commentChar", "%")
    assert stripspace(repo, "a\n% c\n# d\n", strip_comments=True) == "a\n# d\n"


def test_stripspace_runs_one_git(repo: Git, git_calls: list[GitCall]) -> None:
    stripspace(repo, "a\n", strip_comments=True, comment_char=";")
    assert [call[call.index("-c") :] for call in git_calls] == [
        ["-c", "core.commentChar=;", "stripspace", "--strip-comments"]
    ]


def test_stripspace_keeps_bytes_that_arent_utf8(repo: Git) -> None:
    # Compared as bytes: a failure's report can't hold a surrogate.
    assert encode(stripspace(repo, decode(b"caf\xe9\n"))) == b"caf\xe9\n"


def test_stripspace_raises_what_git_refuses(repo: Git) -> None:
    with pytest.raises(GitError, match="(?i)commentchar"):
        stripspace(repo, "a\n", strip_comments=True, comment_char="")
