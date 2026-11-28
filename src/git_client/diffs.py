"""What changed between trees, with one `git diff-tree --stdin` for many diffs, and what a tree
holds."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NoReturn, cast

from git_client.cat_file import CatFile
from git_client.client import (
    CLIENT_TIMEOUT,
    ReadOnlyGit,
    Timeout,
    decode,
    encode,
    read_output,
    read_process,
    stdout_of,
)
from git_client.errors import GitError, WrongTypeError
from git_client.performance import record_read

type ChangeStatus = Literal["A", "C", "D", "M", "R", "T", "U", "X"]

type Diff = str | tuple[str, str]
"""What to diff: a commit, against its parent (a root commit against the empty tree, a merge
not at all, as git diffs one by default), or a pair, (old, new), each a commit or a tree. Each
is a revision that names one (e.g. a ref, an object id, "main~2", "main:src", ":/fix")."""

# A raw entry's fields, before its path: ":<old mode> <new mode> <old id> <new id> <status>",
# the status a letter, and, for a rename or copy, its score.
_RAW = re.compile(rb":([0-7]+) ([0-7]+) ([0-9a-f]+) ([0-9a-f]+) ([ACDMRTUX])([0-9]*)")
# An ls-tree entry: "<mode> <type> <id>\t<path>".
_TREE_ENTRY = re.compile(rb"([0-7]+) (blob|tree|commit) ([0-9a-f]+)\t(.+)", re.DOTALL)
# What ls-tree says of a name of a blob.
_NOT_A_TREE = re.compile(r"^fatal: not a tree object$", re.MULTILINE)
# A numstat entry's counts, before its path ("-" for a binary file's).
_NUMSTAT = re.compile(rb"([0-9]+|-)\t([0-9]+|-)\t")


@dataclass(frozen=True)
class TreeChange:
    """A path a diff changes, as git's raw diff gives it: its status (git's `--diff-filter`
    letters: "A" added, "D" deleted, "M" modified, its mode or its content, "R" renamed, "T"
    a file that became a symlink or a submodule, or the reverse; a file that became a
    directory, or the reverse, is deleted and its new form added), its path (the new
    one, for a rename), its old and new mode ("100644", "100755", "120000" a symlink,
    "160000" a submodule; "000000" where it isn't there) and object id (all zeros where it
    isn't there), and, for a rename, the path it had and how alike the two are, in percent."""

    status: ChangeStatus
    path: str
    old_mode: str
    new_mode: str
    old_id: str
    new_id: str
    old_path: str | None = None
    similarity: int | None = None


@dataclass(frozen=True)
class LineChange:
    """How many lines a diff adds to a path and removes from it (its new path, for a rename),
    as git's numstat counts them; both None for a binary file, as git tells one. A path that
    is a file on one side and a directory on the other has an entry for each."""

    path: str
    added: int | None
    removed: int | None
    old_path: str | None = None


@record_read("git")
def changed_paths(
    git: ReadOnlyGit,
    diffs: Sequence[Diff],
    *,
    renames: bool = True,
    ignore_submodules: bool = False,
    paths: Sequence[str] = (),
    pathspecs: Sequence[str] = (),
    timeout: Timeout = CLIENT_TIMEOUT,
) -> list[list[TreeChange]]:
    """The paths each of `diffs` changes, in order, each in git's order, recursing into
    directories (`-r`): an empty list where nothing changed. See `Diff` for what each diff
    compares, and `line_changes` for its switches. A merge's changes against its first
    parent are the pair (first parent, merge).

    Two processes for all of them: a CatFile (`git cat-file --batch-command`) to resolve the
    names and peel each to a commit (or, in a pair, a tree), within the client's timeout, then
    one `git diff-tree --stdin`, within `timeout`. Raises ValueError for a name git would read
    as another (empty, or with a newline, a carriage return or a NUL), UnknownRevisionError
    for one that names nothing, and WrongTypeError for one that names what isn't a commit (or
    in a pair, a tree): the first such name's. As it uses a CatFile, a FakeGit can't answer
    it."""
    output = _diff_tree(git, diffs, "--raw", renames, ignore_submodules, paths, pathspecs, timeout)
    return output.entries(_raw_entry)


@record_read("git")
def line_changes(
    git: ReadOnlyGit,
    diffs: Sequence[Diff],
    *,
    renames: bool = True,
    ignore_submodules: bool = False,
    paths: Sequence[str] = (),
    pathspecs: Sequence[str] = (),
    timeout: Timeout = CLIENT_TIMEOUT,
) -> list[list[LineChange]]:
    """How many lines each of `diffs` adds and removes, path by path, as `changed_paths`
    lists them (and with its processes and errors), with git's numstat.

    - `renames`: find renames (`-M`, git's default threshold of 50% alike), as `git diff` and
      `git log` do by default (diff-tree, git's plumbing, doesn't), so a rename is one path,
      its new one, not a deletion and an addition. git's `diff.renameLimit` applies: past it,
      git finds only exact renames, saying so only on its stderr, which is dropped.
    - `ignore_submodules`: leave out changes to submodules' commits.
    - `paths`: only these paths, each from the top of the worktree, as git reports one,
      literally (`*` is a file named so), and the files in a directory of that name.
    - `pathspecs`: only what these pathspecs match, as git reads them on its command line
      (from the client's directory, with wildcards and magic): e.g. what a user typed.

    diff-tree reads no diff config but `diff.renameLimit` (not `diff.renames`, an external
    diff, textconv or `diff.relative`); a submodule's `ignore` is overridden."""
    output = _diff_tree(
        git, diffs, "--numstat", renames, ignore_submodules, paths, pathspecs, timeout
    )
    return output.entries(_numstat_entry)


@dataclass
class _Output:
    """What `git diff-tree --stdin` printed for the diffs, each starting with the header it
    prints for that diff, and the command, for errors."""

    output: bytes
    headers: list[bytes]
    args: tuple[str, ...]
    stderr: str

    def entries[T](self, entry: Callable[[bytes, int], tuple[T, int] | None]) -> list[list[T]]:
        """Each diff's entries, read with `entry`, which gives the entry at a position and
        where the next starts, or None where none starts. Raises GitError where a header
        isn't where it should be, or output is left over."""
        found: list[list[T]] = []
        at = 0
        for header in self.headers:
            if not self.output.startswith(header, at):
                self.fail(f"no header {header!r} at {self.output[at : at + 100]!r}")
            at += len(header)
            found.append([])
            while (read := entry(self.output, at)) is not None:
                item, at = read
                found[-1].append(item)
        if at != len(self.output):
            self.fail(f"what it doesn't document: {self.output[at : at + 100]!r}")
        return found

    def fail(self, what: str) -> NoReturn:
        raise GitError(self.args, 0, f"diff-tree printed {what}\n{self.stderr}".rstrip())


def _diff_tree(
    git: ReadOnlyGit,
    diffs: Sequence[Diff],
    format: str,
    renames: bool,
    ignore_submodules: bool,
    paths: Sequence[str],
    pathspecs: Sequence[str],
    timeout: Timeout,
) -> _Output:
    """What `git diff-tree --stdin` printed for `diffs`, in `format`, after resolving them."""
    if isinstance(diffs, str):  # a Sequence[str] too, but of one-letter names
        raise TypeError(f"diffs is a sequence of diffs, not one: [{diffs!r}]")
    if not diffs:
        return _Output(b"", [], (), "")
    names: list[tuple[str, str]] = []
    for diff in diffs:
        for name in [diff] if isinstance(diff, str) else diff:
            # Lines for cat-file, which would read these as another name.
            if not name or any(c in name for c in "\n\r\0"):
                raise ValueError(f"not a revision: {name!r}")
            names.append((name, "commit" if isinstance(diff, str) else "tree"))
    ids = iter(_resolve(git, names))
    lines: list[bytes] = []
    headers: list[bytes] = []
    for diff in diffs:
        if isinstance(diff, str):
            # A commit's line; its header is the commit, with --always even if it changes
            # nothing, ended by a NUL, as -z ends each field.
            lines.append(next(ids))
            headers.append(lines[-1] + b"\0")
        else:
            # Two trees' line; git prints it back as their header, ended by a newline.
            lines.append(next(ids) + b" " + next(ids))
            headers.append(lines[-1] + b"\n")
    args = (
        "diff-tree",
        "--stdin",
        "-z",
        "-r",
        "--root",
        "--always",
        "--no-abbrev",
        format,
        "-M" if renames else "--no-renames",
        f"--ignore-submodules={'all' if ignore_submodules else 'none'}",
        "--",
        *(f":(top,literal){path}" for path in paths),
        *pathspecs,
    )
    # As Revisions' paths: the marks above taken as marks, whatever the environment says.
    env = {"GIT_LITERAL_PATHSPECS": "0", "GIT_ICASE_PATHSPECS": "0"}
    input = b"".join(line + b"\n" for line in lines)
    result = read_process(git, *args, capture_output=True, input=input, env=env, timeout=timeout)
    return _Output(stdout_of(args, result), headers, args, decode(result.stderr))


def _resolve(git: ReadOnlyGit, names: list[tuple[str, str]]) -> list[bytes]:
    """The id of the object of the kind given ("commit", "tree") each name names, from one
    CatFile. Each name is resolved as it is, then peeled by its id (`<id>^{tree}`), as git
    takes a name with a path or a search (`main:src`, `:/fix`) to run to its end."""
    ids: list[bytes] = []
    with CatFile(git) as cat:
        for name, kind in names:
            found = cat.info(name)
            if found is not None and found.type == kind:
                ids.append(encode(found.id))
                continue
            # Raises UnknownRevisionError, with git's reason, if it names nothing.
            object_id = cat.verify(name) if found is None else found.id
            try:
                ids.append(encode(cat.verify(f"{object_id}^{{{kind}}}")))
            except WrongTypeError as error:
                error.add_note(f"{name!r} names {object_id}, which isn't a {kind}")
                raise
    return ids


def _raw_entry(output: bytes, at: int) -> tuple[TreeChange, int] | None:
    """The raw entry at `at` and where the next starts, or None if none starts there."""
    if output[at : at + 1] != b":":
        return None
    fields = _RAW.fullmatch(output, at, max(output.find(b"\0", at), at))
    if fields is None:
        return None
    old_mode, new_mode, old_id, new_id, status, score = (decode(f) for f in fields.groups())
    paths: list[str] = []
    at = fields.end() + 1
    for _ in range(2 if status in "RC" else 1):
        end = output.find(b"\0", at)
        if end <= at:  # no path, or an empty one
            return None
        paths.append(decode(output[at:end]))
        at = end + 1
    if (status in "RC") != bool(score):
        return None
    renamed = len(paths) == 2
    change = TreeChange(
        cast(ChangeStatus, status),
        paths[-1],
        old_mode,
        new_mode,
        old_id,
        new_id,
        paths[0] if renamed else None,
        int(score) if renamed else None,
    )
    return change, at


def _numstat_entry(output: bytes, at: int) -> tuple[LineChange, int] | None:
    """The numstat entry at `at` and where the next starts, or None if none starts there."""
    counts = _NUMSTAT.match(output, at)
    if counts is None:
        return None
    added, removed = (None if c == b"-" else int(c) for c in counts.groups())
    at = counts.end()
    # A rename's paths each end with a NUL, after an empty field; another's ends with one.
    renamed = output[at : at + 1] == b"\0"
    paths: list[str] = []
    at += renamed
    for _ in range(2 if renamed else 1):
        end = output.find(b"\0", at)
        if end <= at:
            return None
        paths.append(decode(output[at:end]))
        at = end + 1
    return LineChange(paths[-1], added, removed, paths[0] if renamed else None), at


@dataclass(frozen=True)
class TreeEntry:
    """An entry of a tree, as `git ls-tree` gives it: its mode (as TreeChange's), its type
    ("blob" a file or symlink, "tree" a directory, "commit" a submodule), its object id, and
    its path from the tree's top."""

    mode: str
    type: Literal["blob", "tree", "commit"]
    id: str
    path: str


@record_read("git")
def tree_entries(
    git: ReadOnlyGit,
    tree: str,
    paths: Sequence[str] = (),
    *,
    recursive: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> list[TreeEntry]:
    """The entries of `tree` (a revision that names a tree or a commit), in git's order, from
    one `git ls-tree -z`: those at its top, or with `recursive`, every file (`-r`, which lists
    no directories). With `paths` (each from the top, literally), only those: a directory's
    entry, or with `recursive` or a path ending in "/", its files. git's ls-tree takes one
    tree, so it's one process for each. Raises UnknownRevisionError if `tree` names nothing,
    and WrongTypeError if it names a blob."""
    args = ("ls-tree", "-z", "--full-tree", *(["-r"] if recursive else []))
    # Not "--", which after --end-of-options is a path; nothing after it is an option.
    args += ("--end-of-options", tree, *paths)
    # Paths as they are, whatever the environment says; --full-tree takes them from the top.
    env = {"GIT_LITERAL_PATHSPECS": "1", "GIT_ICASE_PATHSPECS": "0"}
    found: list[TreeEntry] = []
    try:
        output = read_output(git, *args, env=env, timeout=timeout)
    except GitError as error:
        if _NOT_A_TREE.search(error.stderr):
            raise WrongTypeError(error.command, error.returncode, error.stderr) from None
        raise
    for field in output.split(b"\0")[:-1]:
        entry = _TREE_ENTRY.fullmatch(field)
        if entry is None:
            raise GitError(args, 0, f"ls-tree printed what it doesn't document: {field!r}")
        mode, type, id, path = (decode(f) for f in entry.groups())
        found.append(TreeEntry(mode, cast(Literal["blob", "tree", "commit"], type), id, path))
    return found
