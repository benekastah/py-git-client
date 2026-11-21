"""Reads and writes that answer many questions, or make many changes, with one git process."""

import os
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
    known_git_dirs,
    known_git_paths,
    learn_where,
    location,
    rev_parse_lines,
    where_questions,
)
from git_client.errors import GitError

# What patch_ids gives every command, against the user's config, which would drop patches
# (diff.relative, submodule.<name>.ignore, diff.submodule) or change them (color, an external
# diff, textconv, diff.noprefix, diff.submodule, diff.renames, diff.context,
# diff.interHunkContext, diff.algorithm), and so that `diff-tree -p`, which reads none of that
# config, gives what `log -p` and `diff` give by default.
_PATCH_SETTINGS = (
    "-M",
    "-U3",
    "--inter-hunk-context=0",
    "--diff-algorithm=myers",
    "--no-color",
    "--no-ext-diff",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--no-relative",
    "--ignore-submodules=none",
    "--submodule=short",
    "--no-textconv",
)

# The commands that print commits, and what patch_ids gives them besides: no gpg run on each
# (log.showSignature), each commit's full id at the start of a line, where patch-id looks for
# it (log.abbrevCommit, format.pretty), and a root commit's patch (log.showRoot). `git diff`
# takes none of these.
_SHOWING_COMMITS = {"log", "show"}
_COMMIT_SETTINGS = ("--no-show-signature", "--no-abbrev-commit", "--pretty=medium", "--root")

# The highest exit status of `git merge-file` that counts conflicts (git holds the count to
# it); above it, git failed.
_MOST_CONFLICTS = 127

# The patch that patch_ids puts after each command's output, to mark where it ends.
_END_MARKER_DIFF = "diff --git a/end b/end\n--- a/end\n+++ b/end\n@@ -0,0 +1 @@\n+end\n"


def config(git: ReadOnlyGit) -> dict[str, list[str | None]]:
    """Every config entry git sees for the repository (system, global, local, worktree and
    included files), from one `git config --list -z`, with each key's values in the order git
    reads them. A key with no value at all (`[core]` then `bare`, which git reads as true) has
    None, unlike one set to nothing (`bare =`, which git reads as false), which has "".

    The keys are exactly what git prints: section and name in lower case, the subsection as
    written. So a lookup uses that form: `config(git).get("init.defaultbranch")`, not
    `"init.defaultBranch"`, which finds nothing, with no error; `branch.Topic.pushremote`
    keeps `Topic`. For a key a user typed, a tool runs `git config --get <key>` (one process),
    so git reads it and says what's wrong with it."""
    entries: dict[str, list[str | None]] = {}
    for entry in decode(git.run_bytes("config", "--list", "-z")).split("\0"):
        if entry:
            # A key, then its value after a newline; a key with no value has no newline.
            key, newline, value = entry.partition("\n")
            entries.setdefault(key, []).append(value if newline else None)
    return entries


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
    process tells the client's `prefix()` and `cdup()`, and `object_id_length()`, if they
    aren't known yet."""
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
        questions = where_questions(git, where)
        args = [arg for name in new for arg in ("--git-path", name)]
        output = git.run_bytes("rev-parse", *questions, "--path-format=absolute", *args)
        lines = learn_where(git, where, questions, rev_parse_lines(output), len(new))
        for name, line in zip(new, lines, strict=True):
            known[where, name] = Path(os.fsdecode(line))
    return [known[where, name] for name in names]


@dataclass(frozen=True)
class Head:
    """What HEAD points at. `branch` is the full name of the ref it names (`refs/heads/main`,
    or another ref if HEAD was pointed there), or None when that isn't known; `detached` is
    True when it names no ref at all. Neither: git couldn't say, as with an unborn branch (a
    repository with no commits yet) or a branch named `HEAD`, which makes "HEAD" ambiguous."""

    branch: str | None = None
    detached: bool = False


def git_paths_and_head(git: ReadOnlyGit, *names: str) -> tuple[list[Path], Head]:
    """`git_paths(git, *names)`, and what HEAD points at, from the same one `git rev-parse`
    (so a tool that wants both pays for one process). The paths are as `git_paths` gives
    them, and remembered for `git_paths` (which asks git again for hooks, whatever is kept),
    `git.prefix()`, `git.cdup()` and `git.object_id_length()`; HEAD is read afresh each
    time, since it moves. Raises what `git_paths` does: ValueError for a name with a line ending, and
    GitError if git fails before it has printed the paths (e.g. NotARepositoryError).

    A branch with no commits yet (unborn) has no commit for git to resolve: git prints the
    paths and exits 1 without a word (it's asked to `--verify` HEAD), and the Head is the empty
    one, `Head()`, which isn't `detached`."""
    if any(c in name for name in names for c in "\n\r"):
        raise ValueError(f"git_paths_and_head can't take a name with a line ending: {names}")
    unique = list(dict.fromkeys(names))
    where = location(git)
    questions = where_questions(git, where)
    args = [arg for name in unique for arg in ("--git-path", name)]
    # --verify, so that an unborn HEAD fails (exit 1, quietly) after the paths, rather than being
    # taken for a path and printed as "HEAD", which a detached HEAD prints too.
    command = ["rev-parse", *questions, "--path-format=absolute", *args]
    command += ["--verify", "--quiet", "--symbolic-full-name", "HEAD"]
    try:
        output = rev_parse_lines(git.run_bytes(*command))
        # No HEAD line where git can't say (e.g. a branch named HEAD)
        lines = learn_where(git, where, questions, output, len(unique), len(unique) + 1)
    except GitError as error:
        if error.returncode != 1 or error.stderr.strip():
            raise  # not an unborn HEAD: git said why
        try:
            output = rev_parse_lines(encode(error.stdout))
            lines = learn_where(git, where, questions, output, len(unique))
        except ValueError:
            raise error from None  # it didn't print what was asked
    found = {
        name: Path(os.fsdecode(line))
        for name, line in zip(unique, lines[: len(unique)], strict=True)
    }
    known = known_git_paths.setdefault(git, {})
    for name, path in found.items():
        known[where, name] = path  # git_paths asks again for hooks, whatever is kept
    head = Head()
    if len(lines) > len(unique):
        named = os.fsdecode(lines[len(unique)])
        head = Head(detached=True) if named == "HEAD" else Head(branch=named)
    return [found[name] for name in names], head


def git_dir(git: ReadOnlyGit) -> Path:
    """The client's own git directory, as `git rev-parse --path-format=absolute --git-dir`
    prints it: a checkout's (from any directory in it), a git directory itself, or, for a
    linked worktree, its own (its HEAD, index and rebase state), not the main repository's
    (see `git_common_dir`). Raises GitError if git fails (e.g. NotARepositoryError for a
    directory in no repository).

    One `git rev-parse` answers both this and `git_common_dir`, asked of git once per client
    and location (its directory and `env`) and remembered."""
    return _git_dirs(git)[0]


def git_common_dir(git: ReadOnlyGit) -> Path:
    """The directory with the client's repository's refs, objects and config, as `git
    rev-parse --path-format=absolute --git-common-dir` prints it: `git_dir`'s, but for a
    linked worktree, whose common directory is the main repository's. Raises what `git_dir`
    does, and is asked of git with it, in the same one process."""
    return _git_dirs(git)[1]


def _git_dirs(git: ReadOnlyGit) -> tuple[Path, Path]:
    """The client's own and common git directories (see git_dir)."""
    known = known_git_dirs.setdefault(git, {})
    where = location(git)
    if where not in known:
        output = git.run_bytes(
            "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"
        )
        own, common = (Path(os.fsdecode(line)) for line in rev_parse_lines(output))
        known[where] = own, common
    return known[where]


def _moved_by_config(name: str) -> bool:
    """Whether config (core.hooksPath) can move where git keeps name."""
    return name == "hooks" or name.startswith("hooks/")


def patch_ids(
    git: ReadOnlyGit, *commands: Sequence[str], paths_from_top: bool = False
) -> list[list[tuple[str, str]]]:
    """For each command (git's arguments for one that prints patches, e.g. `["log", "-p",
    "main..topic"]` or `["diff", "a", "b"]`), the (patch id, commit) of each patch it prints, in
    order, as `git patch-id --stable` gives them. A diff's commit is all zeros. A diff of two
    revisions (two arguments before `--`, and no options, which are taken as revisions: name
    paths after `--`) runs as `git diff-tree -p`, which a ReadOnlyGit runs and which writes no
    index. Any other runs as `git diff`, which a ReadOnlyGit doesn't run (see there): of the
    index or the worktree, of one argument (`a..b`), or of a revision holding a `:`: a blob's
    name (`a:path`), which diff-tree doesn't take, but also `:/text`, a tree's `a:` and
    `@{<date> <time>}`, which it would. Whatever the user's config says, each command shows
    changes in the whole repository (not only under `git.directory`, as diff.relative would) and
    in every submodule (each as the commit it moves from and to, as diff.submodule's default
    shows it), none through a textconv, with git's usual a/ and b/ prefixes, renames, 3 lines
    of context (and none more between hunks) and the myers algorithm, and checks no
    signatures; and `log` and `show` print each commit's full id, as patch-id needs (a
    command's own `--format` still wins), and a root commit's patch too.

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
            # Two revisions and nothing else: an option's value could look like one. Not a
            # blob's name (`<rev>:<path>`), which diff-tree doesn't take.
            revisions = rest[: rest.index("--")] if "--" in rest else rest
            if len(revisions) == 2 and not any(
                arg.startswith("-") or ":" in arg for arg in revisions
            ):
                command, rest = "diff-tree", ["-p", *rest]
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
    import tempfile  # here: at the top, it slows every tool's start

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
    top (`git.cdup()`, asked of git once per client) and down to the path, taking it as it is,
    with no magic."""
    if paths_from_top:
        path = os.path.join(encode(git.cdup()), os.fsencode(path))
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
    import tempfile  # here: at the top, it slows every tool's start

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
