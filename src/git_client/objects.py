"""Objects written from what they hold: trees with one `git mktree --batch`, and commits with
`git commit-tree`."""

from collections.abc import Sequence

from git_client.client import CLIENT_TIMEOUT, OBJECT_ID, Git, Timeout, decode, encode, stdout_of
from git_client.diffs import TreeEntry
from git_client.errors import GitError
from git_client.history import Signature


def _tree_input(entries: Sequence[TreeEntry]) -> bytes:
    """mktree -z's input for one tree: each entry as ls-tree prints it, ended by a NUL, then
    the empty entry that ends a tree in --batch."""
    names: set[str] = set()
    lines: list[bytes] = []
    for entry in entries:
        for field, value in (("mode", entry.mode), ("id", entry.id)):
            if not value or any(c in value for c in " \t\n\0"):
                raise ValueError(f"tree entry {entry.path!r}: {field} {value!r} isn't one")
        if not entry.path or "\0" in entry.path:
            # An empty one would end the tree; a NUL, the entry.
            raise ValueError(f"tree entry path {entry.path!r} is empty or holds a NUL")
        if entry.path in names:
            raise ValueError(f"tree holds {entry.path!r} twice")
        names.add(entry.path)
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
    for any number of commits. git checks that each object exists, and is of the type the
    mode says, unless `missing` (git's `--missing`), which leaves it to the caller; a
    submodule's commit (mode 160000) isn't checked either way. mktree writes the trees it can
    before one git refuses, which stay in the object database, unreferenced.

    Runs no hook, and changes no index or ref. Raises ValueError for an entry git would read
    otherwise (a path that is empty or holds a NUL, a mode or id with a space, tab, newline or
    NUL) and for a name given twice in one tree (mktree would write it, and `git fsck`
    report it), and GitError for what git refuses (e.g. a path with a "/", an object that
    isn't there, or of another type). A FakeGit test responds to `["mktree", "-z",
    "--batch"]` with an id on a line for each tree."""
    if not trees:
        return []
    args = ("mktree", "-z", *(["--missing"] if missing else []), "--batch")
    stdin = b"".join(_tree_input(entries) for entries in trees)
    output = stdout_of(args, git.process(*args, capture_output=True, input=stdin, timeout=timeout))
    ids = decode(output).split("\n")
    if len(ids) != len(trees) + 1 or ids[-1] or not all(map(OBJECT_ID.fullmatch, ids[:-1])):
        raise GitError(args, 0, f"mktree printed what it doesn't document: {output!r}")
    return ids[:-1]


def _identity(role: str, who: Signature) -> dict[str, str]:
    """The variables that give git `who` as the commit's `role` ("AUTHOR" or "COMMITTER"),
    each set, so none of the user's environment's is used: an empty date is now."""
    date = ""
    if who.date is not None:
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

    The message is stored as it is (a str as UTF-8): no newline is added and no cleanup made.
    As commit-tree does, git takes bytes that aren't UTF-8 as Latin-1 and stores them as UTF-8,
    warning, unless `i18n.commitEncoding` names another encoding, which git then records in
    the commit's `encoding` header.

    `author` and `committer` default to git's own (user.name, GIT_AUTHOR_NAME and the like,
    and now); one given is git's for this commit alone (its variables are set for this
    process, not the client's), whole: its date, or now if it's None, whatever the environment
    says. A date is stored to the second, in its own time zone. git takes the name and email
    as they are, but for what it strips (e.g. "<", ">" and newlines), and refuses an empty
    name.

    `sign` signs it as git's `-S` does (True: with the default key, user.signingKey; or a key
    given), through the program git's config names (gpg.format, gpg.program and the like).
    commit-tree doesn't read commit.gpgSign: a tool that follows it reads it (see
    `config_values`) and passes `sign=True`.

    Runs no hook, and changes no index or ref. Raises ValueError for a tree that starts with
    "-" (git would read an option), a date with no time zone or one that isn't whole minutes,
    and `sign=""` (give True for the default key); UnknownRevisionError for a tree or parent
    that names nothing, and GitError for any other failure (e.g. one that names another type
    of object, or signing failed). A FakeGit test responds to `["commit-tree"]` with an id
    and a newline."""
    if tree.startswith("-"):
        raise ValueError(f"tree {tree!r} would be read as an option")
    if sign == "":
        raise ValueError("sign takes a key, or True for the default one, not ''")
    env: dict[str, str] = {}
    for role, who in (("AUTHOR", author), ("COMMITTER", committer)):
        if who is not None:
            env |= _identity(role, who)
    args = ["commit-tree", tree, *(arg for parent in parents for arg in ("-p", parent))]
    if sign is not False:
        args.append("-S" if sign is True else f"-S{sign}")
    args += ["-F", "-"]
    body = encode(message) if isinstance(message, str) else message
    result = git.process(*args, capture_output=True, input=body, env=env, timeout=timeout)
    output = decode(stdout_of(args, result))
    if not output.endswith("\n") or not OBJECT_ID.fullmatch(output[:-1]):
        raise GitError(args, 0, f"commit-tree printed what it doesn't document: {output!r}")
    return output[:-1]
