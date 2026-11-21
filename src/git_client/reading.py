"""Which git commands only read: what a ReadOnlyGit runs."""

from collections.abc import Sequence

READING_COMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "check-ref-format",
        "count-objects",
        "describe",
        "diff-files",
        "diff-index",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "patch-id",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "var",
    }
)
"""The commands that read whatever their arguments, but for options that write (see client.py's
_WRITING_OPTIONS). Not porcelain diff, which writes the index whatever GIT_OPTIONAL_LOCKS says
when it compares the worktree: its plumbing (diff-files, diff-index, diff-tree) doesn't. Not
range-diff, whose log runs a textconv, and caches it if the driver says to, whatever its
options."""

# `git symbolic-ref`'s options that a read may have.
_SYMBOLIC_REF_READ_OPTIONS = frozenset({"-q", "--quiet", "--short", "--recurse", "--no-recurse"})

# `git remote`'s arguments that list the remotes.
_REMOTE_LISTS: tuple[list[str], ...] = ([], ["-v"], ["--verbose"])

# `git config`'s actions that read, and those that write (refused, even with one that reads).
_CONFIG_READS = frozenset(
    {
        "get",
        "list",
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "--list",
        "-l",
    }
)
_CONFIG_WRITES = (
    "--add",
    "--replace-all",
    "--unset",
    "--unset-all",
    "--rename-section",
    "--remove-section",
    "--edit",
)

# `git config`'s options that may come before an action that reads: which files it reads, and
# how it gives values. Those in _CONFIG_OPTIONS_WITH_VALUES take the next argument as their
# value, unless (a long one) it follows "=".
_CONFIG_OPTIONS = frozenset(
    {
        "--local",
        "--global",
        "--system",
        "--worktree",
        "--bool",
        "--int",
        "--bool-or-int",
        "--path",
        "-z",
        "--null",
        "--show-origin",
        "--show-scope",
        "--name-only",
    }
)
_CONFIG_OPTIONS_WITH_VALUES = frozenset({"--type", "--default", "-f", "--file"})


def names(arg: str, option: str) -> bool:
    """Whether git takes `arg` as `option`: it, or (for a long option) an abbreviation of it,
    with or without a value after "="."""
    name = arg.split("=", 1)[0]
    if name == option:
        return True
    return name.startswith("--") and len(name) > 2 and option.startswith(name)


def reading_form(command: str, rest: Sequence[str]) -> bool:
    """Whether `git <command> <rest>` is the form of a command that can also write that only
    reads: `branch --show-current`, `symbolic-ref` naming one ref (`--quiet`, `--short`),
    `remote` listing remotes or `get-url`, `worktree list`, and a `config` that only reads
    (see config_reads)."""
    match command:
        case "branch":
            return list(rest) == ["--show-current"]
        case "symbolic-ref":
            # Given a ref to point it at (two names), it writes; --delete, -m too.
            options = [arg for arg in rest if arg.startswith("-")]
            return len(rest) - len(options) == 1 and _SYMBOLIC_REF_READ_OPTIONS.issuperset(options)
        case "remote":
            return list(rest) in _REMOTE_LISTS or rest[:1] == ["get-url"]
        case "worktree":
            return rest[:1] == ["list"]
        case "config":
            return config_reads(rest)
        case _:
            return False


def config_reads(rest: Sequence[str]) -> bool:
    """Whether `git config <rest>` only reads: options that say which files it reads and how
    it gives values (e.g. `--global`, `-f <file>`, `--type=bool`, `--default <value>`), then
    an action that reads (e.g. `--get`, `get`, `--list`), or a lone key (`a.b`, a get), with
    no action that writes anywhere. The action must come first after those options: anywhere
    else, it can be another option's value (`config --file --list a.b c` writes a.b to a file
    named --list)."""
    start = 0
    while start < len(rest) and rest[start].startswith("-"):
        arg = rest[start]
        long = arg.startswith("--")
        if (arg.split("=", 1)[0] if long else arg[:2]) in _CONFIG_OPTIONS_WITH_VALUES:
            # Its value is in it (`--file=x`, `-fx`, `-f=x`, whose file is "=x") or next.
            attached = "=" in arg if long else len(arg) > 2
            start += 1 if attached else 2
        elif arg in _CONFIG_OPTIONS:
            start += 1
        else:
            break
    action = rest[start:]
    # A key has a section, so it has a dot: `config edit` edits.
    reads = action[:1] and action[0] in _CONFIG_READS or (len(action) == 1 and "." in action[0])
    writes = any(names(arg, write) for arg in rest for write in _CONFIG_WRITES)
    # -e (edit), even bundled with other short options (-ze): any short-option argument
    # holding an "e" is refused, which takes in some that don't edit (e.g. -f<path>).
    edits = any(arg[:1] == "-" and arg[1:2] != "-" and "e" in arg for arg in rest)
    return bool(reads) and not writes and not edits
