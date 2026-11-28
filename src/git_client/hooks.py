"""What git does around a commit's message: running a hook (e.g. commit-msg), and cleaning the
message up (`git stripspace`)."""

from collections.abc import Mapping
from dataclasses import dataclass

from git_client.client import (
    CLIENT_TIMEOUT,
    Arg,
    EnvValue,
    Git,
    ReadOnlyGit,
    Timeout,
    decode,
    encode,
    error_from,
    read_output,
)
from git_client.performance import record_read


@dataclass(frozen=True)
class HookResult:
    """How a hook ended: its exit status, and what it printed. git sends a hook's stdout to its
    own stderr, so `stdout` is git's, which prints nothing there: what the hook printed is
    in `stderr`."""

    returncode: int
    stdout: str
    stderr: str


def run_hook(
    git: Git,
    name: str,
    *args: Arg,
    ignore_missing: bool = True,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> HookResult:
    """Run the repository's hook `name` (e.g. "commit-msg") with `args`, as git would before
    an action it guards, with one `git hook run` (git 2.36), and return how it ended, whether
    it succeeded or not: a hook's failure is its answer (e.g. a commit-msg hook's refusal).
    Which program that is (core.hooksPath, hooks in config on newer git) is git's to find.
    Its output is captured; `env` is added to its environment.

    Without a hook of that name, it returns success, as git does, unless `ignore_missing` is
    False: then it raises GitError. It raises GitError too where git refuses the name rather
    than run a hook (newer git refuses a name it doesn't know as a hook unless given
    `--allow-unknown-hook-name`). git's other failures (e.g. outside a repository, where 2.36
    exits 128) are returned as a hook's would be: their exit status can't be told from one.
    Raises ValueError for a name that starts with "-", which git would read as an option. A
    FakeGit test responds to `["hook", "run"]`."""
    if name.startswith("-"):
        raise ValueError(f"hook name {name!r} would be read as an option")
    command = ("hook", "run", *(["--ignore-missing"] if ignore_missing else []), name, "--")
    command += args
    result = git.process(*command, capture_output=True, env=env, timeout=timeout)
    stderr = decode(result.stderr)
    # git's first words when it runs no hook (a hook that printed them first would pass
    # for git).
    first_line = stderr.partition("\n")[0]
    if result.returncode != 0 and (
        (not ignore_missing and first_line == f"error: cannot find a hook named {name}")
        or first_line.startswith(f"error: unknown hook event '{name}'")
    ):
        raise error_from(command, result)
    return HookResult(result.returncode, decode(result.stdout), stderr)


@record_read("git")
def stripspace(
    git: ReadOnlyGit,
    text: str,
    *,
    strip_comments: bool = False,
    comment_char: str | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> str:
    """`text` cleaned up as git cleans a commit message, with one `git stripspace`: no
    whitespace at the ends of lines, runs of empty lines made one, none at the start or end,
    and a newline at the end of the last line (none for text that's left empty). With
    `strip_comments`, lines starting with the comment character go too: `comment_char`, or
    else core.commentChar's (stripspace takes "auto" as "#"; only `git commit` chooses one
    from the message). Text that isn't UTF-8 (as surrogates, see `decode`) is kept as it is.
    It reads and writes nothing, so a ReadOnlyGit offers it. Raises GitError for a comment
    character git refuses."""
    config = ["-c", f"core.commentChar={comment_char}"] if comment_char is not None else []
    args = (*config, "stripspace", *(["--strip-comments"] if strip_comments else []))
    return decode(read_output(git, *args, input=encode(text), timeout=timeout))
