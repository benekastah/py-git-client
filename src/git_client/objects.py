"""Objects written from what they hold: trees with one `git mktree --batch`, and commits with
`git commit-tree`."""

from collections.abc import Sequence
from datetime import datetime

from git_client.client import CLIENT_TIMEOUT, OBJECT_ID, Git, Timeout, decode, encode, stdout_of
from git_client.diffs import TreeEntry
from git_client.errors import GitError
from git_client.history import Signature, raw_datetime


def _tree_input(entries: Sequence[TreeEntry]) -> bytes:
    """mktree -z's input for one tree: each entry as ls-tree prints it, ended by a NUL, then
    the empty entry that ends a tree in --batch."""
    lines: list[bytes] = []
    for entry in entries:
        for field, value in (("mode", entry.mode), ("id", entry.id)):
            if not value or any(c in value for c in " \t\n\0"):
                raise ValueError(f"tree entry {entry.path!r}: {field} {value!r} isn't one")
        if not entry.path or "\0" in entry.path:
            # An empty one would end the tree; a NUL, the entry.
            raise ValueError(f"tree entry path {entry.path!r} is empty or holds a NUL")
        lines.append(f"{entry.mode} {entry.type} {entry.id}\t".encode() + encode(entry.path))
    return b"".join(line + b"\0" for line in lines) + b"\0"


def write_trees(
    git: Git,
    trees: Sequence[Sequence[TreeEntry]],
    *,
    missing: bool = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> list[str]:
    """Write each tree, given its entries, to the object database, and return their ids, in
    order, with one `git mktree -z --batch` for them all (none for no trees).

    Each entry is one name in its tree (`path` holds no "/"), in any order: git sorts them.
    A tree that holds another written here needs its id, so a directory's trees are written
    before it, by an earlier call: one call for each depth, each writing that depth's trees
    for any number of commits. git checks that each object exists, and that its type is the
    one the mode's kind takes (a tree for 040000, a commit for 160000, a blob for any other),
    unless `missing` (git's `--missing`), which leaves that to the caller; a submodule's
    commit isn't checked either way. Otherwise mktree writes what it's given: a name given
    twice, ".", "..", ".git", or a mode that isn't one of git's (e.g. "644", which it writes
    as it is, and `ls-tree` then reads as a submodule) give a tree that `git fsck` (and a
    server with receive.fsckObjects) reports: fsck is git's check of them. mktree writes the
    trees it can before one it refuses, which stay in the object database, unreferenced.

    Runs no hook, and changes no index or ref. Raises ValueError for an entry mktree's input
    can't hold as it is (a path that is empty or holds a NUL, a mode or id that is empty or
    holds a space, tab, newline or NUL), and GitError for what git refuses (e.g. a path with
    a "/", an object that isn't there, or of the wrong type). A FakeGit test responds to
    `["mktree"]` (git's arguments are `["mktree", "-z", "--batch"]`, with `"--missing"`
    before `"--batch"` if asked) with an id on a line for each tree."""
    if not trees:
        return []
    args = ("mktree", "-z", *(["--missing"] if missing else []), "--batch")
    stdin = b"".join(_tree_input(entries) for entries in trees)
    output = stdout_of(args, git.process(*args, capture_output=True, input=stdin, timeout=timeout))
    ids = decode(output).split("\n")
    if len(ids) != len(trees) + 1 or ids[-1] or not all(map(OBJECT_ID.fullmatch, ids[:-1])):
        raise GitError(args, 0, f"mktree printed what it doesn't document: {output!r}")
    return ids[:-1]


def _same(read: datetime | None, given: datetime | None) -> bool:
    """Whether two dates are the same instant in the same zone, or both None."""
    if read is None or given is None:
        return read is given
    return read == given and read.utcoffset() == given.utcoffset()


def _identity(role: str, who: Signature) -> dict[str, str]:
    """The variables that give git `who` as the commit's `role` ("AUTHOR" or "COMMITTER"),
    each set, so none of the user's environment's is used."""
    raw = who.raw_date
    if raw is not None and _same(raw_datetime(raw), who.date):
        date = f"@{raw}"
    elif who.date is None:
        raise ValueError(f"{role.lower()} {who.name!r} has no date: give one")
    else:
        offset = who.date.utcoffset()
        if offset is None:
            raise ValueError(f"{role.lower()}'s date {who.date} has no time zone")
        minutes, seconds = divmod(int(offset.total_seconds()), 60)
        if seconds or offset.microseconds:
            raise ValueError(f"{role.lower()}'s time zone {offset} isn't whole minutes")
        sign = "-" if minutes < 0 else "+"
        hours, minutes = divmod(abs(minutes), 60)
        seconds_since_epoch = int(who.date.replace(microsecond=0).timestamp())
        date = f"@{seconds_since_epoch} {sign}{hours:02}{minutes:02}"
    return {f"GIT_{role}_NAME": who.name, f"GIT_{role}_EMAIL": who.email, f"GIT_{role}_DATE": date}


def write_commit(
    git: Git,
    tree: str,
    parents: Sequence[str],
    message: bytes | str,
    *,
    author: Signature | None = None,
    committer: Signature | None = None,
    sign: bool | str = False,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> str:
    """Write a commit of `tree` on `parents` (in order; none for a root commit), each a
    revision that names one, to the object database, and return its id, with one `git
    commit-tree`: git has no form that writes many, so it's one process for each commit.

    The message is stored as it is: no newline is added and no cleanup made (git refuses a
    NUL in it). A str is stored as UTF-8, and labeled so whatever `i18n.commitEncoding` says
    (git runs with `-c i18n.commitEncoding=UTF-8`). Bytes are stored as given, labeled as
    the repository's `i18n.commitEncoding` says, as `git commit-tree` does: with an
    `encoding` header naming it if it isn't UTF-8; if it is (the default), git takes bytes
    that aren't UTF-8 as Latin-1 and stores them as UTF-8, warning.

    `author` and `committer` default to git's own (user.name, GIT_AUTHOR_NAME and the like,
    and now); one given is git's for this commit alone (its variables are set for this
    process, not the client's), whole, whatever the environment says. Its date is its
    `raw_date` if it has one that `date` still describes (as `commits` gives it), so a commit
    read and written again keeps a date a datetime can't hold (e.g. past the year 9999);
    else its `date`, stored to the second, in its own time zone. git writes no zone it can't
    read back as hours and minutes: "-0000", which it reads as "+0000", stays "+0000", and
    one past "+2359" is written otherwise. git takes no date before 1970. For now, give
    `datetime.now().astimezone()`. git takes the name and email as they are, but for what it
    strips (e.g. "<", ">" and newlines), and refuses an empty name.

    `sign` signs it as git's `-S` does (True: with the default key, user.signingKey; or a key
    given), through the program git's config names (gpg.format, gpg.program and the like).
    commit-tree doesn't read commit.gpgSign: a tool that follows it reads it (see
    `config_values`) and passes `sign=True`.

    Runs no hook, and changes no index or ref. Raises ValueError for a tree that starts with
    "-" (git would read an option), an identity with no date (None, and no `raw_date` that
    describes it), a date with no time zone or one that isn't whole minutes, and `sign=""`
    (give True for the default key); UnknownRevisionError for a tree or parent that names
    nothing, WrongTypeError for one that names another type of object, and GitError for any
    other failure (e.g. a date before 1970, a NUL in the message, or signing failed). A
    FakeGit test responds to `["commit-tree"]` with an id and a newline, or for a str
    message, to `["-c", "i18n.commitEncoding=UTF-8", "commit-tree"]`."""
    if tree.startswith("-"):
        raise ValueError(f"tree {tree!r} would be read as an option")
    if sign == "":
        raise ValueError("sign takes a key, or True for the default one, not ''")
    env: dict[str, str] = {}
    for role, who in (("AUTHOR", author), ("COMMITTER", committer)):
        if who is not None:
            env |= _identity(role, who)
    # git labels a message by i18n.commitEncoding: a str's bytes are UTF-8.
    args = ["-c", "i18n.commitEncoding=UTF-8"] if isinstance(message, str) else []
    args += ["commit-tree", tree, *(arg for parent in parents for arg in ("-p", parent))]
    if sign is not False:
        args.append("-S" if sign is True else f"-S{sign}")
    args += ["-F", "-"]
    body = encode(message) if isinstance(message, str) else message
    result = git.process(*args, capture_output=True, input=body, env=env, timeout=timeout)
    output = decode(stdout_of(args, result))
    if not output.endswith("\n") or not OBJECT_ID.fullmatch(output[:-1]):
        raise GitError(args, 0, f"commit-tree printed what it doesn't document: {output!r}")
    return output[:-1]
