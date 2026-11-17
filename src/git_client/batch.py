"""Reads and writes that answer many questions, or make many changes, with one git process."""

import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from git_client.client import (
    Arg,
    EnvValue,
    Git,
    ReadOnlyGit,
    decode,
    encode,
    error_from,
    known_git_paths,
    learn_object_id_length,
    location,
    prefixes,
)
from git_client.errors import GitError

# What patch_ids gives every command, against the user's config, which would drop patches
# (diff.relative, submodule.<name>.ignore) or change them (color, an external diff, textconv,
# diff.noprefix).
_PATCH_SETTINGS = (
    "--no-color",
    "--no-ext-diff",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--no-relative",
    "--ignore-submodules=none",
    "--no-textconv",
)

# The commands that print commits, and what patch_ids gives them besides: no gpg run on each
# (log.showSignature), and each commit's full id at the start of a line, where patch-id looks
# for it (log.abbrevCommit, format.pretty). `git diff` takes none of these.
_SHOWING_COMMITS = {"log", "show"}
_COMMIT_SETTINGS = ("--no-show-signature", "--no-abbrev-commit", "--pretty=medium")

# The highest exit status of `git merge-file` that counts conflicts (git holds the count to
# it); above it, git failed.
_MOST_CONFLICTS = 127

# The patch that patch_ids puts after each command's output, to mark where it ends.
_END_MARKER_DIFF = "diff --git a/end b/end\n--- a/end\n+++ b/end\n@@ -0,0 +1 @@\n+end\n"


def config(git: ReadOnlyGit) -> dict[str, list[str | None]]:
    """Every config entry git sees for the repository (system, global, local, worktree and
    included files), from one `git config --list`: each key, as git prints it (section and
    name in lower case, a subsection as written), with its values in the order git reads
    them. A key with no value at all (`[core]` then `bare`, which git reads as true) has None,
    unlike one set to nothing (`bare =`, which git reads as false), which has "". Look a key
    up by `config_key(key)`, which names it as git prints it."""
    entries: dict[str, list[str | None]] = {}
    for entry in decode(git.run_bytes("config", "--list", "-z")).split("\0"):
        if entry:
            # A key, then its value after a newline; a key with no value has no newline.
            key, newline, value = entry.partition("\n")
            entries.setdefault(key, []).append(value if newline else None)
    return entries


def config_key(key: str) -> str:
    """A config key as `config` gives it, and as git reads it: its section and name in lower
    case, and a subsection (what's between them, e.g. a branch's name) as written."""
    section, dot, rest = key.partition(".")
    subsection, dot2, name = rest.rpartition(".")
    return f"{section.lower()}{dot}{subsection}{dot2}{name.lower()}"


@dataclass(frozen=True)
class Ref:
    """A ref, from `refs`: its full name, the object it points to, the full name of its
    upstream (or ""), the full name of the ref it's a symbolic ref to (or ""), whether HEAD
    is on it, and the other fields `refs` was asked for, by name."""

    name: str
    object: str
    upstream: str
    symref: str
    head: bool
    fields: dict[str, str] = field(default_factory=dict[str, str], hash=False)


def refs(git: ReadOnlyGit, *patterns: str, fields: Sequence[str] = ()) -> dict[str, Ref]:
    """The refs matching patterns (as `git for-each-ref` takes them, e.g. "refs/heads/"; all
    refs if none), by full name, from one `git for-each-ref`. `fields` are other fields to
    read, as `--format` names them without `%(...)` (e.g. "upstream:track",
    "committerdate:iso-strict", "push:remotename"), each of whose values must be one line.

    For a branch's short name, ask for "refname:lstrip=2" (or "upstream:lstrip=2"), never
    ":short", which git disambiguates when a tag has the same name ("heads/v1"); and give git
    full ref names or object ids rather than short names."""
    extra = "".join(f"%00%({name})" for name in fields)
    where = location(git)
    listing = git.run_bytes(
        "for-each-ref",
        f"--format=%(refname)%00%(objectname)%00%(upstream)%00%(symref)%00%(HEAD){extra}",
        "--end-of-options",
        *patterns,
    )
    found: dict[str, Ref] = {}
    # Only at "\n", as git ends each: a ref name can hold what splitlines and strip take as
    # line endings or space (e.g. U+2028).
    for line in decode(listing).split("\n"):
        if not line:
            continue
        name, object_id, upstream, symref, head, *values = line.split("\0")
        found[name] = Ref(
            name, object_id, upstream, symref, head == "*", dict(zip(fields, values, strict=True))
        )
    if found:
        learn_object_id_length(git, where, next(iter(found.values())).object)
    return found


def git_paths(git: ReadOnlyGit, *names: str) -> list[Path]:
    """Where git keeps each of names (paths in a git directory, e.g. "HEAD", "refs/heads",
    "hooks", "rebase-merge"), as absolute paths, in order, from one `git rev-parse
    --git-path`. Each is where git itself would look: in a linked worktree, its own git
    directory for what's the worktree's (HEAD, index, rebase-merge) and the common one for the
    rest (refs, objects, config); and where config or the environment move it (e.g.
    core.hooksPath, GIT_OBJECT_DIRECTORY). Raises ValueError for a name with a line ending,
    whose path couldn't be told from the next one's, and GitError if git fails (e.g.
    NotARepositoryError).

    Each name is asked of git once per client and location (its directory and
    `env`) and remembered, except "hooks" and what's under it: core.hooksPath, which a tool
    may set, moves them. Nothing else `--git-path` answers depends on config. The same
    process tells the client's `prefix()`, if it isn't known yet."""
    if not names:
        return []
    if any(c in name for name in names for c in "\n\r"):
        raise ValueError(f"git_paths can't take a name with a line ending: {names}")
    known = known_git_paths.setdefault(git, {})
    where = location(git)
    new = [
        name
        for name in dict.fromkeys(names)
        if (where, name) not in known or _moved_by_config(name)
    ]
    if new:
        # The prefix too, if it's not known: it costs no process of its own here.
        known_prefixes = prefixes.setdefault(git, {})
        prefix = [] if where in known_prefixes else ["--show-prefix"]
        args = [arg for name in new for arg in ("--git-path", name)]
        output = git.run_bytes("rev-parse", *prefix, "--path-format=absolute", *args)
        # Not splitlines(): the prefix's line can be empty, and a path can't hold a line ending.
        lines = output.removesuffix(b"\n").split(b"\n") if output else []
        if prefix:
            known_prefixes[where] = decode(lines.pop(0))
        for name, line in zip(new, lines, strict=True):
            known[where, name] = Path(os.fsdecode(line))
    return [known[where, name] for name in names]


def _moved_by_config(name: str) -> bool:
    """Whether config (core.hooksPath) can move where git keeps name."""
    return name == "hooks" or name.startswith("hooks/")


def patch_ids(
    git: ReadOnlyGit, *commands: Sequence[str], paths_from_top: bool = False
) -> list[list[tuple[str, str]]]:
    """For each command (git's arguments for one that prints patches, e.g. `["log", "-p",
    "main..topic"]` or `["diff", "a", "b"]`), the (patch id, commit) of each patch it prints,
    in order, as `git patch-id --stable` gives them. A diff's commit is all zeros; a
    ReadOnlyGit doesn't run `diff` (see there), so give it `log` or `show`. Whatever
    the user's config says, each command shows changes in the whole repository (not only
    under `git.directory`, as diff.relative would) and in every submodule, none through a
    textconv, with git's usual a/ and b/ prefixes, and checks no signatures; and `log` and
    `show` print each commit's full id, as patch-id needs (a command's own `--format` still
    wins).

    Paths after `--` are pathspecs, relative to `git.directory`. With `paths_from_top`, each
    is instead a path from the top of the worktree, as git reports one (e.g. `git diff
    --name-only`), taken as it is, even if it holds `*` or `:`: patch_ids marks each
    `:(top,literal)`, and runs that command with GIT_LITERAL_PATHSPECS=0, which would
    otherwise take the mark as part of the name, and GIT_ICASE_PATHSPECS=0, which would
    otherwise have a path also match one that differs from it only in case.

    Each command runs, but one `git patch-id` reads them all: after each command's output
    comes a patch of ours, whose commit (1, 2, ...) can't be a real one's, marking its end.
    """
    if not commands:
        return []
    length = git.object_id_length()
    patches: list[str] = []
    for i, (command, *rest) in enumerate(commands, 1):
        if command == "diff":
            # Without a commit line, patch-id would take the diff as part of the marker
            # before it.
            patches.append(f"commit {'0' * length}")
        settings = (
            (*_PATCH_SETTINGS, *_COMMIT_SETTINGS)
            if command in _SHOWING_COMMITS
            else _PATCH_SETTINGS
        )
        env: dict[str, EnvValue] = {}
        if paths_from_top and "--" in rest:
            paths = rest.index("--") + 1
            rest = [*rest[:paths], *(f":(top,literal){path}" for path in rest[paths:])]
            # Which would take the marks as part of each name; `literal` does its work.
            env["GIT_LITERAL_PATHSPECS"] = "0"
            # Which would make `literal` match case-insensitively, taking in other files.
            env["GIT_ICASE_PATHSPECS"] = "0"
        patches.append(git.run(command, *settings, *rest, env=env))
        patches.append(f"commit {i:0{length}x}\n\n{_END_MARKER_DIFF}")
    results: list[list[tuple[str, str]]] = [[]]
    for line in git.run("patch-id", "--stable", input="\n".join(patches) + "\n").splitlines():
        patch_id, commit = line.split()
        if commit == f"{len(results):0{length}x}":
            results.append([])
        else:
            results[-1].append((patch_id, commit))
    if len(results) != len(commands) + 1:
        raise GitError(
            ["patch-id", "--stable"],
            0,
            f"printed {len(results) - 1} end markers for {len(commands)}",
        )
    return results[:-1]


def hash_blobs(git: Git, contents: Sequence[bytes]) -> list[str]:
    """Write each of contents to the object database as a blob, as it is (no filters or
    line-ending conversion), with one `git hash-object`, and return their ids in order. For
    content as `git add` would store it at a path, through that path's filters, see
    hash_blob_at."""
    if not contents:
        return []
    with tempfile.TemporaryDirectory() as scratch:
        paths: list[str] = []
        for i, content in enumerate(contents):
            paths.append(os.path.join(scratch, str(i)))
            with open(paths[-1], "wb") as f:
                f.write(content)
        listed = b"".join(os.fsencode(path) + b"\n" for path in paths)
        return decode(
            git.run_bytes("hash-object", "-w", "--no-filters", "--stdin-paths", input=listed)
        ).split()


def hash_blob_at(
    git: Git, path: Arg, content: bytes, *, write: bool = False, paths_from_top: bool = False
) -> str:
    """The id of content as `git add` would store it at `path`, whether or not a file is
    there: through the filters that path's attributes and the config give it, e.g. a clean
    filter (such as git-lfs's), or line-ending conversion (`text`, `eol`, core.autocrlf),
    unlike hash_blobs, which stores content as it is. With `write`, the blob is written to the
    object database too. One `git hash-object --path`, which runs the filter's program, if
    any. Raises GitError if git fails (e.g. a required filter does).

    `path` is relative to `git.directory`, and takes no pathspec magic (e.g. ":(top)"). With
    `paths_from_top`, it is instead a path from the top of the worktree, as git reports one
    (e.g. `git diff --name-only`), for a client in a subdirectory: hash_blob_at goes up to the
    top (`git.prefix()`'s, asked of git once per client) and down to the path, taking it as it
    is, with no magic."""
    if paths_from_top:
        path = b"../" * git.prefix().count("/") + os.fsencode(path)
    output = git.run_bytes(
        "hash-object", *(["-w"] if write else []), "--path", path, "--stdin", input=content
    )
    return decode(output).strip()


def merge_file(
    git: Git,
    *,
    ours: bytes,
    base: bytes,
    theirs: bytes,
    labels: tuple[str, str, str] = ("ours", "base", "theirs"),
    options: Sequence[str] = (),
) -> tuple[bytes, int]:
    """Merge the changes from base to ours and from base to theirs, as `git merge-file`
    does, and return the result and how many conflicts it has (at most 127, as git counts
    them), each between conflict markers that name it by `labels` (ours, base, theirs).
    `options` are merge-file's own, e.g. "--diff3" or "--zdiff3" (otherwise, in a repository,
    its config's merge.conflictStyle says how conflicts look; outside one, git's default),
    "--union", "--marker-size=10". The three contents are keyword-only, as orders differ
    (git's is ours, base, theirs), and a swap would merge silently wrong. Nothing is
    written to the repository: the three are put in temporary files for git, which merge-file
    needs. Raises GitError if git can't merge them (e.g. binary content). It takes a Git, as
    a ReadOnlyGit doesn't run merge-file."""
    with tempfile.TemporaryDirectory() as scratch:
        paths: list[bytes] = []
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            paths.append(os.fsencode(os.path.join(scratch, name)))
            with open(paths[-1], "wb") as f:
                f.write(content)
        args = ["merge-file", "--stdout", *options]
        args += [arg for label in labels for arg in ("-L", label)]
        result = git.process(*args, *paths, capture_output=True)
    if not 0 <= result.returncode <= _MOST_CONFLICTS:
        raise error_from(args, result)
    return result.stdout, result.returncode


@dataclass(frozen=True)
class RefUpdate:
    """A change to a ref, for `update_refs`: point `ref` at `new`, or delete it if `new` is
    None; and if `old` is given, only if the ref points there now (all zeros: only if it
    doesn't exist yet)."""

    ref: str
    new: str | None
    old: str | None = None


def update_refs(git: Git, updates: Iterable[RefUpdate], message: str | None = None) -> None:
    """Make every update in one transaction (`git update-ref --stdin`): all of them, or, if
    any can't be made (e.g. a ref isn't at its `old`), none. `message` goes in the reflogs.
    Raises GitError if they weren't made."""
    commands: list[bytes] = []
    for update in updates:
        old = encode(update.old or "")
        if update.new is None:
            commands.append(b"delete " + encode(update.ref) + b"\0" + old + b"\0")
        else:
            commands.append(
                b"update " + encode(update.ref) + b"\0" + encode(update.new) + b"\0" + old + b"\0"
            )
    if commands:
        git.run_bytes(
            "update-ref",
            *(["-m", message] if message else []),
            "--stdin",
            "-z",
            input=b"".join(commands),
        )
