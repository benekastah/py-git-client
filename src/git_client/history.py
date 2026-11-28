"""Commits git lists by a revision walk, with one `git rev-list` for each question, and where
refs have been, from their reflogs, with one `git log --walk-reflogs`."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from git_client.client import CLIENT_TIMEOUT, ReadOnlyGit, Timeout, decode, encode, read_output
from git_client.errors import GitError
from git_client.performance import record_read

# Each commit's fields, each ended by a NUL, which none of them can hold (git stops a message at
# one); rev-list ends each commit's with a newline.
_FIELDS = ("%H", "%P", "%an", "%ae", "%ad", "%cn", "%ce", "%cd", "%s")
_FORMAT = "".join(f"{field}%x00" for field in _FIELDS)

# A date as `--date=raw` prints it, with a zone a datetime can hold: git prints what the commit
# records, which can be anything (fsck only warns of a bad zone, or no date at all).
_RAW_DATE = re.compile(r"(\d+) ([+-])([01]\d|2[0-3])([0-5]\d)")

# Each reflog entry's fields, apart by a NUL, and each entry ended by one (-z): its selector
# (`<the ref as given>@{<index>}`), the commit, who moved the ref, and why. None can hold a
# NUL, and git writes a reflog's message on one line.
_REFLOG_FIELDS = ("%gD", "%H", "%gn", "%ge", "%gs")
_SELECTOR = re.compile(r"(.*)@\{(\d+)\}", re.ASCII | re.DOTALL)


@dataclass(frozen=True)
class Signature:
    """Who authored or committed a commit, and when, as the commit says: `date` is aware, in
    the commit's own time zone. Like any datetime's, its `==` compares instants, so the same
    moment in two zones is equal: `date.utcoffset()` tells them apart. It's None where the
    commit records no date, or one a datetime can't hold (a zone that isn't `±HHMM` of a
    real offset, a year past 9999), which git takes, and fsck only warns of."""

    name: str
    email: str
    date: datetime | None


@dataclass(frozen=True)
class Commit:
    """A commit, as git reads it: its full id, its parents' (in order: the first is the one
    a merge was made on), its author and committer, its subject (git's: the first paragraph
    of its message, on one line), and its whole message, as written, if `commits` was asked
    for it (else None). Text is UTF-8, which git reencodes each message to; bytes that
    aren't are kept as surrogates (see `decode`)."""

    id: str
    parents: tuple[str, ...]
    author: Signature
    committer: Signature
    subject: str
    message: str | None = None


@dataclass(frozen=True, init=False)
class Revisions:
    """Which commits a revision walk lists, as `git rev-list` reads its arguments: those
    reachable from any of `include` and from none of `exclude` (each a revision git resolves,
    e.g. a ref, an object id, "main~2"; one string or several). So `Revisions("topic",
    exclude="main")` is git's `main..topic`. Then, of those:

    - `merges`: True, only merges (`--merges`); False, none (`--no-merges`).
    - `first_parent`: follow only each merge's first parent (`--first-parent`).
    - `cherry_pick`: leave out each commit whose patch (as `git patch-id` sees it) a commit
      reachable from the exclude and not the include has: git's `--cherry-pick --right-only
      <exclude>...<include>`, which takes one of each (ValueError otherwise).
    - `since`: only commits committed at or after it (`--max-age`, which `--since` gives
      git: by the committer's date, not the author's). An aware datetime: a naive one raises
      ValueError, rather than be taken for local time. One before 1970 keeps every commit,
      as git's dates start then.
    - `author`: only commits whose author, as `Name <email>`, contains this text (a fixed
      string, not a pattern: `--fixed-strings --author`), as the commit records it: the
      mailmap, which `git log --author` reads by default, maps no one.
    - `paths`: only commits that change one of these (and history simplified as git's
      pathspec limiting does), each a path from the top of the worktree, as git reports one,
      taken as it is, even if it holds `*` or `:`. The top itself is `""` (all of it): a path
      with a `.` or `..` part matches nothing, as git reports none, so for top-level files
      pass `""`, not `"."` (as `PurePosixPath("b").parent` gives).

    Raises ValueError for a revision that is empty, starts with "-", or holds a newline, a
    carriage return or a NUL, which git would read as something else.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...]
    merges: bool | None
    first_parent: bool
    cherry_pick: bool
    since: datetime | None
    author: str | None
    paths: tuple[str, ...]

    def __init__(
        self,
        include: str | Sequence[str],
        exclude: str | Sequence[str] = (),
        *,
        merges: bool | None = None,
        first_parent: bool = False,
        cherry_pick: bool = False,
        since: datetime | None = None,
        author: str | None = None,
        paths: Sequence[str] = (),
    ) -> None:
        include = (include,) if isinstance(include, str) else tuple(include)
        exclude = (exclude,) if isinstance(exclude, str) else tuple(exclude)
        if cherry_pick and (len(include), len(exclude)) != (1, 1):
            raise ValueError("cherry_pick takes one include and one exclude")
        if since is not None and since.utcoffset() is None:
            raise ValueError(f"since is naive, so its zone is unknown: {since!r}")
        # Given to git a line each, where an empty one ends the revisions (dropping the rest),
        # a NUL ends the line, a "\r" ending it is dropped, and one starting with "-" is an
        # option ("--not" would flip the rest): none of which is a revision.
        for name in (*include, *exclude):
            if not name or name.startswith("-") or any(c in name for c in "\n\r\0"):
                raise ValueError(f"not a revision: {name!r}")
        values = {
            "include": include,
            "exclude": exclude,
            "merges": merges,
            "first_parent": first_parent,
            "cherry_pick": cherry_pick,
            "since": since,
            "author": author,
            "paths": tuple(paths),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)


@record_read("git")
def commits(
    git: ReadOnlyGit,
    revisions: Revisions,
    *,
    max_count: int | None = None,
    reverse: bool = False,
    topo_order: bool = False,
    messages: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> list[Commit]:
    """The commits `revisions` describes, newest first (by committer date, as git walks them),
    or, with `topo_order`, with no line of history's commits shown between another's, and
    never a parent before its children (`--topo-order`). `max_count` keeps the first so many;
    `reverse` then gives them oldest first, as git does (so with both, the newest
    `max_count`, oldest first). `messages` reads each commit's whole message too.

    One `git rev-list`, given the revisions on its stdin, so there can be as many as git
    takes. Raises UnknownRevisionError for a revision git can't resolve."""
    options = [f"--max-count={max_count}"] if max_count is not None else []
    options += [
        switch for switch, on in (("--reverse", reverse), ("--topo-order", topo_order)) if on
    ]
    format = _FORMAT + ("%B%x00" if messages else "")
    output = _rev_list(
        git,
        revisions,
        "--no-commit-header",
        "--encoding=UTF-8",
        "--date=raw",
        f"--format={format}",
        *options,
        timeout=timeout,
    )
    fields = decode(output).split("\0")
    per_commit = len(_FIELDS) + messages
    found: list[Commit] = []
    for start in range(0, len(fields) - 1, per_commit):
        commit_id, parents, *signatures, subject = fields[start : start + len(_FIELDS)]
        found.append(
            Commit(
                commit_id.removeprefix("\n"),  # ending the commit before
                tuple(parents.split()),
                _signature(*signatures[:3]),
                _signature(*signatures[3:]),
                subject,
                fields[start + len(_FIELDS)] if messages else None,
            )
        )
    return found


def _signature(name: str, email: str, date: str) -> Signature:
    """A Signature from git's fields, the date raw: seconds since the epoch and a zone, or
    None where it isn't one a datetime can hold."""
    found = _RAW_DATE.fullmatch(date)
    if found is None:
        return Signature(name, email, None)
    seconds, sign, hours, minutes = found.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes)) * (-1 if sign == "-" else 1)
    try:
        # From the epoch, which, unlike fromtimestamp, takes a date before 1970 on Windows too.
        instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=int(seconds))
        return Signature(name, email, instant.astimezone(timezone(offset)))
    except OverflowError:  # past the year 9999
        return Signature(name, email, None)


def _rev_list(git: ReadOnlyGit, revisions: Revisions, *options: str, timeout: Timeout) -> bytes:
    """What `git rev-list` printed for `revisions`, with `options`."""
    if revisions.cherry_pick:
        lines = [f"{revisions.exclude[0]}...{revisions.include[0]}"]
        options = ("--cherry-pick", "--right-only", *options)
    else:
        lines = [*revisions.include, *(f"^{name}" for name in revisions.exclude)]
    switches = {
        "--merges": revisions.merges is True,
        "--no-merges": revisions.merges is False,
        "--first-parent": revisions.first_parent,
    }
    options += tuple(switch for switch, on in switches.items() if on)
    if revisions.since is not None:
        # git's whole seconds, rounded up: so none before `since` is kept. Not below 0, which
        # git would read as unsigned: no commit is dated before 1970, so 0 keeps them all.
        after = revisions.since - datetime(1970, 1, 1, tzinfo=UTC)
        seconds = max(0, after.days * 86400 + after.seconds + (after.microseconds > 0))
        options += (f"--max-age={seconds}",)
    if revisions.author is not None:
        options += ("--fixed-strings", f"--author={revisions.author}")
    # Paths after "--", each marked to be taken from the top and literally, with what would
    # otherwise take the marks as part of the name, or match other cases, turned off.
    paths = [f":(top,literal){path}" for path in revisions.paths]
    env = {"GIT_LITERAL_PATHSPECS": "0", "GIT_ICASE_PATHSPECS": "0"} if paths else None
    return read_output(
        git,
        "rev-list",
        "--stdin",
        *options,
        "--",
        *paths,
        input=b"".join(encode(line) + b"\n" for line in lines),
        env=env,
        timeout=timeout,
    )


@record_read("git")
def count(git: ReadOnlyGit, revisions: Revisions, *, timeout: Timeout = CLIENT_TIMEOUT) -> int:
    """How many commits `revisions` describes, from one `git rev-list --count` (see
    `commits`)."""
    return int(_rev_list(git, revisions, "--count", timeout=timeout))


@record_read("git")
def any_commit(
    git: ReadOnlyGit, revisions: Revisions, *, timeout: Timeout = CLIENT_TIMEOUT
) -> bool:
    """Whether `revisions` describes any commit, from one `git rev-list --max-count=1`, which
    stops at the first (see `commits`)."""
    return bool(_rev_list(git, revisions, "--max-count=1", timeout=timeout).strip())


@dataclass(frozen=True)
class ReflogEntry:
    """An entry of a ref's reflog, as git reads it: `index`, its place in the reflog (0 the
    newest: the ref's `@{index}`); `id`, the commit the ref was moved to; `name` and `email`,
    who moved it (git's identity then); and `message`, why, as git wrote it (e.g. "commit:
    fix", "rebase (finish): refs/heads/a onto <id>"). Text is UTF-8, as git wrote it; bytes
    that aren't are kept as surrogates (see `decode`)."""

    index: int
    id: str
    name: str
    email: str
    message: str


@record_read("git")
def reflogs(
    git: ReadOnlyGit, *refs: str, timeout: Timeout = CLIENT_TIMEOUT
) -> dict[str, list[ReflogEntry]]:
    """Each ref's reflog, newest first, by the ref as given (each once, in the order given):
    [] for a ref with none. One `git log --walk-reflogs` for all of them (none for no refs),
    so git's rules apply: it shows only entries of commits it can read, and none past what
    `git reflog expire` removed.

    Each ref is a full ref name: "HEAD", or one starting with "refs/" (e.g. "refs/heads/main",
    "refs/remotes/origin/main", "refs/tags/v1", "refs/stash"), which git names each entry by.
    A shorter one (e.g. "origin/main"), git names by the ref it finds the reflog of, which
    can't be told from the name, so it raises ValueError. So does a name that holds what no
    ref's can and git reads as something else: "~", "^" or ":" (a revision that's no ref, e.g.
    "refs/heads/main~1", of which git prints nothing, as of a ref with no reflog), "@{" (a
    reflog selector), or a NUL; and "". Raises UnknownRevisionError if a ref names nothing
    (failing them all)."""
    names = list(dict.fromkeys(refs))
    for name in names:
        full = name == "HEAD" or name.startswith("refs/")
        if not full or any(part in name for part in ("~", "^", ":", "@{", "\0")):
            raise ValueError(f"not a full ref name: {name!r}")
    found: dict[str, list[ReflogEntry]] = {name: [] for name in names}
    if not names:
        return found
    args = (
        "log",
        "--walk-reflogs",
        "-z",
        "--no-show-signature",  # which log.showSignature would print among the entries
        f"--format={'%x00'.join(_REFLOG_FIELDS)}",
        "--end-of-options",
        *names,
        "--",
    )
    fields = decode(read_output(git, *args, timeout=timeout)).split("\0")
    per_entry = len(_REFLOG_FIELDS)
    if len(fields) % per_entry != 1 or fields[-1]:
        raise GitError(args, 0, f"log printed entries it doesn't document: {fields!r}")
    for start in range(0, len(fields) - 1, per_entry):
        selector, commit_id, name, email, message = fields[start : start + per_entry]
        parsed = _SELECTOR.fullmatch(selector)
        if parsed is None or parsed[1] not in found:
            raise GitError(args, 0, f"log printed a selector it wasn't asked for: {selector!r}")
        found[parsed[1]].append(ReflogEntry(int(parsed[2]), commit_id, name, email, message))
    return found
