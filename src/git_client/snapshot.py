"""What a tool reads about refs, kept until a command it runs may change them."""

import os
import threading
from collections.abc import Callable, Sequence

from git_client.reading import READING_COMMANDS, reading_form

# Options to git itself, before the command, whose value is the next argument.
_GIT_OPTIONS_WITH_VALUES = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"}
)

# Commands that change no ref (HEAD included), and no setting that says what a ref's upstream
# is, beyond those a ReadOnlyGit runs (see reading.py), whatever their options (an option of
# theirs that writes writes a file or the index, not a ref): those that write only objects, the
# index or the worktree.
_KEEPING_REFS = frozenset(
    {
        "apply",
        "commit-tree",
        "diff",
        "hash-object",
        "ls-remote",
        "merge-file",
        "merge-tree",
        "mktree",
        "read-tree",
        "update-index",
        "write-tree",
    }
)

# `git config`'s options that a write keeping refs may have: where it writes (but not another
# file, which the repository's config could include) and how.
_CONFIG_WRITE_OPTIONS = frozenset(
    {"--local", "--worktree", "--add", "--replace-all", "--unset", "--unset-all", "--fixed-value"}
)

# The settings of a branch's (`branch.<name>.<setting>`, in lower case) that git lists its
# upstream (and push ref) by.
_UPSTREAM_SETTINGS = frozenset({"remote", "merge", "pushremote"})


def _changes_refs(args: Sequence[str | bytes]) -> bool:
    """Whether `git <args>` may change a ref (HEAD included), or a branch's upstream. Only
    commands known not to don't: anything else (another command, an alias) may."""
    texts = [os.fsdecode(arg) for arg in args]
    start = 0
    while start < len(texts) and texts[start].startswith("-"):
        start += 2 if texts[start] in _GIT_OPTIONS_WITH_VALUES else 1
    command, rest = (texts[start], texts[start + 1 :]) if start < len(texts) else ("", [])
    if command in READING_COMMANDS or command in _KEEPING_REFS or reading_form(command, rest):
        return False
    return command != "config" or _config_writes_upstream(rest)


def _config_writes_upstream(rest: list[str]) -> bool:
    """Whether `git config <rest>`, which writes, may change a branch's upstream: unless it
    writes one of a branch's settings that git doesn't list refs or upstreams by (e.g. a
    tool's own, `branch.<name>.toolParent`)."""
    if rest[:1] in (["set"], ["unset"]):
        rest = rest[1:]
    options = [arg for arg in rest if arg.startswith("-")]
    names = [arg for arg in rest if not arg.startswith("-")]
    if not names or not _CONFIG_WRITE_OPTIONS.issuperset(options):
        return True
    section, _, rest_of_key = names[0].partition(".")
    # A branch's name, which can hold dots, then the setting, which can't.
    branch, _, setting = rest_of_key.rpartition(".")
    return section.lower() != "branch" or not branch or setting.lower() in _UPSTREAM_SETTINGS


class _RefChanges:
    """How many git commands that may change refs (see _changes_refs) any client in this
    process has run."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def count(self) -> int:
        with self._lock:
            return self._count

    def ran(self, args: Sequence[str | bytes]) -> None:
        """Count `git <args>` if it may have changed refs."""
        if _changes_refs(args):
            with self._lock:
                self._count += 1


ref_changes = _RefChanges()
"""What every git process a client starts is counted in: after it ends, or for a long-lived
one, when it starts (see RefSnapshot). FakeGit counts what it's asked to run too."""


class RefSnapshot[T]:
    """What `read` gives (e.g. `lambda: refs(git, "refs/heads/")`), read when first asked
    for, and kept until a git command that may change a ref (HEAD included) or a branch's
    upstream runs through any `Git` in this process: then read again when next asked for.

    A tool asks about the same few refs over and over, and each git process is slow enough to
    add up: a snapshot reads them with one, for as long as nothing changes them. Anything
    computed from refs can be kept this way (the worktrees' branches, a reflog), and a dict
    (`RefSnapshot(dict)`) holds answers found one at a time, e.g. each branch's reflog, all
    forgotten together.

    Only commands known not to change refs keep it: those a ReadOnlyGit runs, those that write
    only objects, the index or the worktree (`diff`, `merge-tree`, `hash-object`, `read-tree`,
    `update-index`, `apply`, `commit-tree`, ...), `ls-remote`, the reading forms of `branch`
    (`--show-current`), `symbolic-ref`, `remote` and `worktree`, and a `git config` that reads,
    or writes one key, `branch.<name>.<setting>`, that isn't the branch's `remote`, `merge` or
    `pushRemote` (e.g. a tool's own settings for the branch). Anything else may change refs,
    an alias or a command that failed included, as may what it runs (e.g. its hooks).

    Changes it doesn't see: those made by anything but this process's clients (another
    process, the user, a git that a tool starts some other way), and those a long-lived
    process (`popen`) makes after it starts, which is when it's counted. So keep a snapshot
    for no longer than one of the tool's operations, under the tool's lock if it has one, and
    make a new one to read afresh. Every client's commands count, whichever repository they
    run in: a command in another repository costs a read, but one in a worktree of the same
    repository, which shares its refs, isn't missed.

    `get` may be called from several threads: `read` runs in one at a time, and what it gives
    is kept as of the count of changes before it started, so after a change counted while it
    ran (in another thread, or by `read` itself), the next `get` reads again. `read` mustn't
    call the same snapshot's `get`, which would wait for itself.
    """

    def __init__(self, read: Callable[[], T]) -> None:
        self._read = read
        self._lock = threading.Lock()
        # What read gave, and the count of changes before it started; None until it's read.
        self._kept: tuple[T, int] | None = None

    def get(self) -> T:
        """What `read` gives, as read since refs last changed, or read now."""
        with self._lock:
            changes = ref_changes.count()
            if self._kept is not None and self._kept[1] == changes:
                return self._kept[0]
            value = self._read()
            self._kept = (value, changes)
            return value
