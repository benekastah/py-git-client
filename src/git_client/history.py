"""Commits git lists by a revision walk, with one `git rev-list` for each question."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from git_client.client import CLIENT_TIMEOUT, ReadOnlyGit, Timeout, decode, encode, read_output
from git_client.performance import record_read

# Each commit's fields, each ended by a NUL, which none of them can hold (git stops a message at
# one); rev-list ends each commit's with a newline.
_FIELDS = ("%H", "%P", "%an", "%ae", "%ad", "%cn", "%ce", "%cd", "%s")
_FORMAT = "".join(f"{field}%x00" for field in _FIELDS)


@dataclass(frozen=True)
class Signature:
    """Who authored or committed a commit, and when, as the commit says: `date` is aware, in
    the commit's own time zone. Like any datetime's, its `==` compares instants, so the same
    moment in two zones is equal: `date.utcoffset()` tells them apart."""

    name: str
    email: str
    date: datetime


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
      git: by the committer's date, not the author's).
    - `author`: only commits whose author, as `Name <email>`, contains this text (a fixed
      string, not a pattern: `--fixed-strings --author`).
    - `paths`: only commits that change one of these (and history simplified as git's
      pathspec limiting does), each a path from the top of the worktree, as git reports one,
      taken as it is, even if it holds `*` or `:`.

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
    """A Signature from git's fields, the date raw: seconds since the epoch and a zone."""
    seconds, zone = date.split()
    sign = -1 if zone.startswith("-") else 1
    offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5])) * sign
    # From the epoch, which, unlike fromtimestamp, takes a date before 1970 on Windows too.
    instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=int(seconds))
    return Signature(name, email, instant.astimezone(timezone(offset)))


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
        options += (f"--max-age={int(revisions.since.timestamp())}",)
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
