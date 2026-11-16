"""Reads and writes that answer many questions, or make many changes, with one git process."""

import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from git_client.client import Git, decode, encode
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

# The commands that would run gpg on every commit they print (log.showSignature), and that
# take an option against it (`git diff` doesn't).
_SHOWING_COMMITS = {"log", "show"}

# The patch that patch_ids puts after each command's output, to mark where it ends.
_END_MARKER_DIFF = "diff --git a/end b/end\n--- a/end\n+++ b/end\n@@ -0,0 +1 @@\n+end\n"


def config(git: Git) -> dict[str, list[str | None]]:
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


def refs(git: Git, *patterns: str, fields: Sequence[str] = ()) -> dict[str, Ref]:
    """The refs matching patterns (as `git for-each-ref` takes them, e.g. "refs/heads/"; all
    refs if none), by full name, from one `git for-each-ref`. `fields` are other fields to
    read, as `--format` names them without `%(...)` (e.g. "upstream:track",
    "committerdate:iso-strict", "push:remotename"), each of whose values must be one line.

    For a branch's short name, ask for "refname:lstrip=2" (or "upstream:lstrip=2"), never
    ":short", which git disambiguates when a tag has the same name ("heads/v1"); and give git
    full ref names or object ids rather than short names."""
    extra = "".join(f"%00%({name})" for name in fields)
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
    return found


def patch_ids(git: Git, *commands: Sequence[str]) -> list[list[tuple[str, str]]]:
    """For each command (git's arguments for one that prints patches, e.g. `["log", "-p",
    "main..topic"]` or `["diff", "a", "b"]`), the (patch id, commit) of each patch it prints,
    in order, as `git patch-id --stable` gives them. A diff's commit is all zeros. Whatever
    the user's config says, each command shows changes in the whole repository (not only
    under `git.directory`, as diff.relative would) and in every submodule, none through a
    textconv, with git's usual a/ and b/ prefixes, and checks no signatures.

    Paths after `--` are pathspecs, relative to `git.directory`: mark a path git reported
    (from the top of the worktree, which may hold `*` or `:`) as `":(top,literal)" + path`.

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
            (*_PATCH_SETTINGS, "--no-show-signature")
            if command in _SHOWING_COMMITS
            else _PATCH_SETTINGS
        )
        patches.append(git.run(command, *settings, *rest))
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
    line-ending conversion), with one `git hash-object`, and return their ids in order."""
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
