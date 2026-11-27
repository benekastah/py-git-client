"""Reads and writes that answer many questions, or make many changes, with one git process."""

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from git_client.client import (
    CLIENT_TIMEOUT,
    DIR_QUESTIONS,
    Arg,
    EnvValue,
    Git,
    LinesUnfit,
    ReadOnlyGit,
    ask_where,
    ask_where_alone,
    asking,
    decode,
    encode,
    error_from,
    git_path_question,
    location,
    read_output,
    read_process,
    read_where,
    recall_where,
    remember_where,
    rev_parse_lines,
    where_command,
    where_questions,
)
from git_client.errors import GitError, WrongTypeError
from git_client.performance import record_read

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


@record_read("git")
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
    for entry in decode(read_output(git, "config", "--list", "-z")).split("\0"):
        if entry:
            # A key, then its value after a newline; a key with no value has no newline.
            key, newline, value = entry.partition("\n")
            entries.setdefault(key, []).append(value if newline else None)
    return entries


@dataclass(frozen=True)
class Track:
    """How a branch stands against the ref it tracks (its upstream, or where it pushes), as
    git says it (`%(upstream:track)`, `%(push:track)`): how many commits it has that the ref
    hasn't (`ahead`) and the ref has that it hasn't (`behind`), or that the ref doesn't exist
    (`gone`)."""

    ahead: int = 0
    behind: int = 0
    gone: bool = False


# A key `refs` sorts by, as `git for-each-ref --sort` takes it: "-" sorts it in reverse.
type SortKey = Literal[
    "refname",
    "-refname",
    "version:refname",
    "-version:refname",
    "committerdate",
    "-committerdate",
    "creatordate",
    "-creatordate",
]


@dataclass(frozen=True)
class Ref:
    """A ref, from `refs`: its full name, the object it points to, the full name of its
    upstream (or ""), the full name of the ref it's a symbolic ref to (or ""), whether HEAD
    is on it, and the other fields `refs` was asked for, by name.

    The rest are None unless `refs` was asked for them. With `remotes`: the remote its
    upstream is on ("." for a local branch, "" with no upstream) and the upstream's name
    there; and the full name of the remote-tracking ref git would push it to (`%(push)`, ""
    where git would push it nowhere it tracks), and that remote (`%(push:remotename)`). With
    `track` and `push_track`: how it stands against its upstream, or what it pushes to (None
    where it has none). With `dates`: its commit's committer date (None for a ref to another
    object, e.g. an annotated tag). With `ahead_behind`: how many commits it has that the base
    hasn't and the base has that it hasn't (None for a ref to no commit, e.g. a tree)."""

    name: str
    object: str
    upstream: str
    symref: str
    head: bool
    fields: dict[str, str] = field(default_factory=dict[str, str], hash=False)
    upstream_remote: str | None = None
    upstream_remote_ref: str | None = None
    push: str | None = None
    push_remote: str | None = None
    track: Track | None = None
    push_track: Track | None = None
    committer_date: datetime | None = None
    ahead_behind: tuple[int, int] | None = None


# What git before 2.41 says of %(ahead-behind:...), which it doesn't know.
_NO_AHEAD_BEHIND = re.compile(r"^fatal: unknown field name: ahead-behind:", re.MULTILINE)


@record_read("git")
def refs(
    git: ReadOnlyGit,
    *patterns: str,
    fields: Sequence[str] = (),
    contains: str | None = None,
    no_contains: str | None = None,
    merged: str | None = None,
    no_merged: str | None = None,
    points_at: str | None = None,
    sort: Sequence[SortKey] = (),
    count: int | None = None,
    remotes: bool = False,
    track: bool = False,
    push_track: bool = False,
    dates: bool = False,
    ahead_behind: str | None = None,
) -> dict[str, Ref]:
    """The refs matching patterns (as `git for-each-ref` takes them, e.g. "refs/heads/"; all
    refs if none), by full name, in git's order, from one `git for-each-ref`.

    Only those whose commit contains `contains` (or doesn't contain `no_contains`), whose
    commit `merged` contains (or `no_merged` doesn't), or that point at `points_at`, as git's
    options of those names filter them. `sort` orders them by each key in turn, the first
    deciding (git's `--sort`, whose last decides), and `count` keeps the first so many.

    `remotes`, `track`, `push_track`, `dates` and `ahead_behind` (a base: a commit, or a
    name for one) fill in the `Ref` attributes they name (see Ref). With git before 2.41,
    which can't count against a base, `ahead_behind` runs one `git rev-list` per ref
    (and one `git rev-parse` for the base), so that every count is against the same commit.
    Raises ValueError for a base with ")", which a format can't hold, and GitError if git
    fails (e.g. the base names no commit).

    `fields` are other fields to read, as `--format` names them without `%(...)` (e.g.
    "subject"), each of whose values must be one line. For a branch's short name, ask for
    "refname:lstrip=2" (or "upstream:lstrip=2"), never ":short", which git disambiguates when
    a tag has the same name ("heads/v1"); and give git full ref names or object ids rather
    than short names."""
    if ahead_behind is not None and ")" in ahead_behind:
        raise ValueError(f"refs can't count against a base with ')': {ahead_behind!r}")
    options = [
        f"--{option}={value}"
        for option, value in (
            ("contains", contains),
            ("no-contains", no_contains),
            ("merged", merged),
            ("no-merged", no_merged),
            ("points-at", points_at),
        )
        if value is not None
    ]
    # git's last key decides.
    options += [f"--sort={key}" for key in reversed(sort)]
    options += [] if count is None else [f"--count={count}"]
    # The typed attributes asked for, each with its field, in the order they're read.
    typed: list[tuple[str, str]] = []
    if remotes:
        typed += [
            ("upstream_remote", "upstream:remotename"),
            ("upstream_remote_ref", "upstream:remoteref"),
            ("push_remote", "push:remotename"),
        ]
    typed += [("push", "push")] if remotes or push_track else []
    typed += [("track", "upstream:track,nobracket")] if track else []
    typed += [("push_track", "push:track,nobracket")] if push_track else []
    typed += [("committer_date", "committerdate:iso-strict")] if dates else []
    typed += [("ahead_behind", f"ahead-behind:{ahead_behind}")] if ahead_behind is not None else []

    def listing(typed: list[tuple[str, str]]) -> str:
        atoms = ["refname", "objectname", "upstream", "symref", "HEAD", *fields]
        atoms += [atom for _, atom in typed]
        spec = "%00".join(f"%({atom})" for atom in atoms)
        command = ["for-each-ref", f"--format={spec}", *options, "--end-of-options"]
        return decode(read_output(git, *command, *patterns))

    try:
        listed, counted_alone = listing(typed), False
    except GitError as error:
        if ahead_behind is None or not _NO_AHEAD_BEHIND.search(error.stderr):
            raise
        typed.pop()  # ahead_behind, counted below
        listed, counted_alone = listing(typed), True
    found: dict[str, Ref] = {}
    # Only at "\n", as git ends each: a ref name can hold what splitlines and strip take as
    # line endings or space (e.g. U+2028).
    for line in listed.split("\n"):
        if not line:
            continue
        name, object_id, upstream, symref, head, *values = line.split("\0")
        given = dict(zip(fields, values[: len(fields)], strict=True))
        said = dict(zip((key for key, _ in typed), values[len(fields) :], strict=True))
        date = said.get("committer_date")
        found[name] = Ref(
            name,
            object_id,
            upstream,
            symref,
            head == "*",
            given,
            upstream_remote=said.get("upstream_remote"),
            upstream_remote_ref=said.get("upstream_remote_ref"),
            push=said.get("push"),
            push_remote=said.get("push_remote"),
            track=_track(said["track"], upstream) if track else None,
            push_track=_track(said["push_track"], said["push"]) if push_track else None,
            committer_date=datetime.fromisoformat(date) if date else None,
            ahead_behind=_counts(said.get("ahead_behind", "")),
        )
    if counted_alone and ahead_behind is not None:
        alone = _ahead_behind_alone(git, ahead_behind, found.values())
        found = {name: replace(ref, ahead_behind=alone[name]) for name, ref in found.items()}
    return found


# What git's %(upstream:track,nobracket) says of a ref that's ahead, behind or both.
_TRACK = re.compile(r"(?:ahead (?P<ahead>\d+))?(?:, )?(?:behind (?P<behind>\d+))?")


def _track(said: str, tracked: str) -> Track | None:
    """A Track from what git said of how a ref stands against the ref it tracks
    (%(upstream:track,nobracket) or %(push:track,nobracket)), named `tracked` ("" if none):
    nothing when they're the same, "gone" when `tracked` doesn't exist."""
    if not tracked:
        return None
    if said == "gone":
        return Track(gone=True)
    found = _TRACK.fullmatch(said)
    if found is None:
        raise ValueError(f"git said a ref's tracking is {said!r}")
    return Track(int(found["ahead"] or 0), int(found["behind"] or 0))


def _counts(said: str) -> tuple[int, int] | None:
    """Two counts from what git said, "<ahead> <behind>"; None if it said nothing."""
    if not said:
        return None
    ahead, behind = said.split()
    return int(ahead), int(behind)


def _ahead_behind_alone(
    git: ReadOnlyGit, base: str, found: Iterable[Ref]
) -> dict[str, tuple[int, int] | None]:
    """What %(ahead-behind:<base>) says of each ref, for a git before 2.41, which can't say
    it: one `git rev-list --left-right --count` per ref against the commit base names (from
    one `git rev-parse`), and None for a ref to no commit."""
    commit = decode(
        read_output(git, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    ).strip()
    counts: dict[str, tuple[int, int] | None] = {}
    for ref in found:
        args = ("rev-list", "--left-right", "--count", "--end-of-options")
        try:
            counts[ref.name] = _counts(
                decode(read_output(git, *args, f"{ref.object}^{{commit}}...{commit}"))
            )
        except WrongTypeError:  # e.g. a tag of a tree
            counts[ref.name] = None
    return counts


@record_read("git")
def git_paths(git: ReadOnlyGit, *names: str) -> list[Path]:
    """Where git keeps each of names (paths in a git directory, e.g. "HEAD", "refs/heads",
    "hooks", "rebase-merge"), as absolute paths, in order, from one `git rev-parse
    --git-path`. Each is where git itself would look: in a linked worktree, its own git
    directory for what's the worktree's (HEAD, index, rebase-merge) and the common one for the
    rest (refs, objects, config); and where config or the environment move it (e.g.
    core.hooksPath, GIT_OBJECT_DIRECTORY). Raises ValueError for a name with a line ending,
    whose path couldn't be told from the next one's, and GitError if git fails (e.g.
    NotARepositoryError).

    With a cache, names are kept for the current location. Git always answers for "hooks"
    and paths below it because core.hooksPath can move them. A path containing a newline
    can make batched output ambiguous; in that case Git answers each question separately."""
    if not names:
        return []
    if any(c in name for name in names for c in "\n\r"):
        raise ValueError(f"git_paths can't take a name with a line ending: {names}")
    where = location(git)
    found: dict[str, bytes] = {}
    for name in dict.fromkeys(names):
        question = git_path_question(name)
        known = None if _moved_by_config(name) else recall_where(git, where, question)
        if known is not None:
            found[name] = known[question]
    new = [name for name in dict.fromkeys(names) if name not in found]
    if new:
        asked = asking(git)
        questions = where_questions(git, where)
        args = [arg for name in new for arg in ("--git-path", name)]
        output = read_output(git, *where_command(questions), *args)
        try:
            answers, lines = read_where(questions, rev_parse_lines(output), len(new))
        except LinesUnfit:  # a path with a newline: each in a process of its own
            answers = ask_where_alone(git, questions)
            lines = [_git_path_alone(git, name) for name in new]
        found.update(zip(new, lines, strict=True))
        answers.update((git_path_question(name), found[name]) for name in new)
        remember_where(git, where, answers, asked)
    return [Path(os.fsdecode(found[name])) for name in names]


@dataclass(frozen=True)
class Head:
    """What HEAD points at. `branch` is the full name of the ref it names (`refs/heads/main`,
    or another ref if HEAD was pointed there), or None when that isn't known; `detached` is
    True when it names no ref at all. Neither: git couldn't say, as with an unborn branch (a
    repository with no commits yet) or a branch named `HEAD`, which makes "HEAD" ambiguous."""

    branch: str | None = None
    detached: bool = False


@record_read("git")
def git_paths_and_head(git: ReadOnlyGit, *names: str) -> tuple[list[Path], Head]:
    """`git_paths(git, *names)`, and what HEAD points at, from the same one `git rev-parse` (so
    a tool that wants both pays for one process). The paths are as `git_paths` gives them, and
    cached for `git_paths` when the client has a cache. HEAD is read afresh each time,
    since it moves. Raises what `git_paths` does: ValueError for a
    name with a line ending, and GitError if git fails before it has printed the paths (e.g.
    NotARepositoryError).

    A branch with no commits yet (unborn) has no commit for git to resolve: git prints the
    paths and exits 1 (it's asked to `--verify` HEAD, `--quiet`ly, so it says nothing of it,
    though it may warn of other things, e.g. its config), and the Head is the empty
    one, `Head()`, which isn't `detached`."""
    if any(c in name for name in names for c in "\n\r"):
        raise ValueError(f"git_paths_and_head can't take a name with a line ending: {names}")
    unique = list(dict.fromkeys(names))
    where = location(git)
    asked = asking(git)
    questions = where_questions(git, where)
    args = [arg for name in unique for arg in ("--git-path", name)]
    # --verify, so that an unborn HEAD fails (exit 1, quietly) after the paths, rather than being
    # taken for a path and printed as "HEAD", which a detached HEAD prints too.
    command = [*where_command(questions), *args]
    command += ["--verify", "--quiet", "--symbolic-full-name", "HEAD"]
    result = read_process(git, *command, capture_output=True)
    stderr = decode(result.stderr)
    if result.returncode == 0:
        # No HEAD line where git can't say which ref "HEAD" is (a branch named HEAD).
        head_lines = 0 if _AMBIGUOUS_HEAD.search(stderr) else 1
    elif result.returncode == 1:
        head_lines = 0  # unborn, whatever git warned of (e.g. its config)
    else:
        raise error_from(command, result)  # git said why
    try:
        answers, lines = read_where(
            questions, rev_parse_lines(result.stdout), len(unique) + head_lines
        )
    except LinesUnfit:  # a path with a newline: each in a process of its own
        answers = ask_where_alone(git, questions)
        lines = [_git_path_alone(git, name) for name in unique]
        lines += _head_alone(git)
    except ValueError:
        if result.returncode == 0:
            raise
        raise error_from(command, result) from None  # it didn't print what was asked
    found = dict(zip(unique, lines[: len(unique)], strict=True))
    # git_paths asks again for hooks, whatever is kept.
    answers.update((git_path_question(name), line) for name, line in found.items())
    remember_where(git, where, answers, asked)
    head = Head()
    if len(lines) > len(unique):
        named = os.fsdecode(lines[len(unique)])
        head = Head(detached=True) if named == "HEAD" else Head(branch=named)
    return [Path(os.fsdecode(found[name])) for name in names], head


@record_read("git")
def git_dir(git: ReadOnlyGit) -> Path:
    """The client's own git directory, as `git rev-parse --path-format=absolute --git-dir`
    prints it: a checkout's (from any directory in it), a git directory itself, or, for a
    linked worktree, its own (its HEAD, index and rebase state), not the main repository's
    (see `git_common_dir`). Raises GitError if git fails (e.g. NotARepositoryError for a
    directory in no repository).

    Asked of Git with `git_common_dir`. With a cache, other metadata queries can
    fill in both directories in the same process."""
    return _git_dirs(git)[0]


@record_read("git")
def git_common_dir(git: ReadOnlyGit) -> Path:
    """The directory with the client's repository's refs, objects and config, as `git
    rev-parse --path-format=absolute --git-common-dir` prints it: `git_dir`'s, but for a
    linked worktree, whose common directory is the main repository's. Raises what `git_dir`
    does, and is asked of git with it, in the same one process."""
    return _git_dirs(git)[1]


def _git_dirs(git: ReadOnlyGit) -> tuple[Path, Path]:
    """The client's own and common git directories (see git_dir)."""
    found = ask_where(git, location(git), DIR_QUESTIONS, CLIENT_TIMEOUT)
    own, common = (Path(os.fsdecode(found[question])) for question in DIR_QUESTIONS)
    return own, common


# What git says (on stderr, with exit 0) where it prints no HEAD line: a ref named HEAD (e.g.
# refs/heads/HEAD) makes the name ambiguous.
_AMBIGUOUS_HEAD = re.compile(r"refname 'HEAD' is ambiguous")


def _git_path_alone(git: ReadOnlyGit, name: str) -> bytes:
    """Where git keeps `name`, from a `rev-parse` of its own: its whole output but the newline
    that ends it, for a path with a newline in it."""
    output = read_output(git, "rev-parse", "--path-format=absolute", "--git-path", name)
    return output.removesuffix(b"\n")


def _head_alone(git: ReadOnlyGit) -> list[bytes]:
    """The full name of what HEAD points at, from a `rev-parse` of its own, as one line; none
    where git can't say (an unborn branch, a branch named HEAD). A ref's name holds no
    newline."""
    args = ("rev-parse", "--verify", "--quiet", "--symbolic-full-name", "HEAD")
    result = read_process(git, *args, capture_output=True)
    if result.returncode not in (0, 1):  # 1: unborn, whatever git warned of
        raise error_from(args, result)
    return rev_parse_lines(result.stdout)


def _moved_by_config(name: str) -> bool:
    """Whether config (core.hooksPath) can move where git keeps name."""
    return name == "hooks" or name.startswith("hooks/")


def patch_ids(
    git: Git, *commands: Sequence[str], paths_from_top: bool = False
) -> list[list[tuple[str, str]]]:
    """For each command (git's arguments for one that prints patches, e.g. `["log", "-p",
    "main..topic"]`, `["diff-tree", "-p", "a", "b"]` or `["diff", "a", "b"]`), the (patch id,
    commit) of each patch it prints, in order, as `git patch-id --stable` gives them. A diff's
    commit is all zeros. Each command runs as it's given, with the options below added: so
    patch_ids takes a Git, and raises TypeError, running nothing, for a ReadOnlyGit (e.g. a
    Git's `read_only()`), which has no public way to run a command of the caller's. Whatever
    the user's config says, each command shows changes in the whole repository (not only
    under `git.directory`, as diff.relative would) and in every submodule (each as the commit
    it moves from and to, as diff.submodule's default shows it), none through a textconv,
    with git's usual a/ and b/ prefixes, renames, 3 lines of context (and none more between
    hunks) and the myers algorithm, and checks no signatures; and `log` and `show` print each
    commit's full id, as patch-id needs (a command's own `--format` still wins), and a root
    commit's patch too.

    Paths after `--` are pathspecs, relative to `git.directory`. With `paths_from_top`, each
    is instead a path from the top of the worktree, as git reports one (e.g. `git diff
    --name-only`), taken as it is, even if it holds `*` or `:`: patch_ids marks each
    `:(top,literal)`, and runs that command with GIT_LITERAL_PATHSPECS=0, which would
    otherwise take the mark as part of the name, and GIT_ICASE_PATHSPECS=0, which would
    otherwise have a path also match one that differs from it only in case.

    Each command runs, but one `git patch-id` reads them all: after each command's output
    comes a patch of ours, whose commit (1, 2, ...) can't be a real one's, marking its end.
    """
    # Clearer than the AttributeError for run, after object_id_length() may have run git.
    if not isinstance(git, Git):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("patch_ids takes a Git, not a ReadOnlyGit")
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
    top using `git.cdup()`, then down to the path, taking it as it is."""
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
