"""Which commits are ancestors of which: many pairs answered through one git process."""

import heapq
import re

from git_client.batch import git_paths
from git_client.cat_file import CatFile
from git_client.client import OBJECT_ID, ReadOnlyGit
from git_client.errors import (
    CorruptObjectError,
    MissingObjectError,
    UnknownRevisionError,
    WrongTypeError,
)

_WALK_LIMIT = 500
"""How many commits a pair's walk may take from its queue before the pair is left to `git
merge-base --is-ancestor`: at about 20µs a commit, about 10ms, a few of that process's starts
on Linux and less than one on Windows; and far more than a stack's branches are apart."""

# The marks a pair's walk paints commits with: reachable from the ancestor, reachable from the
# descendant, and stale (reachable from both, so nothing below it can answer the question).
_FROM_ANCESTOR, _FROM_DESCENDANT, _STALE = 1, 2, 4
_FROM_BOTH = _FROM_ANCESTOR | _FROM_DESCENDANT

# What reading a commit can raise of that commit (missing, damaged, or not a commit), rather
# than of the repository or git.
_OBJECT_ID_BYTES = re.compile(OBJECT_ID.pattern.encode())

_UNREADABLE = (UnknownRevisionError, CorruptObjectError, MissingObjectError, WrongTypeError)


class _TooFar(Exception):
    """A pair's walk took more than _WALK_LIMIT commits."""


class _OddCommit(Exception):
    """A commit the walk can't follow as git would: of another type than its parent line
    says, or with a parent line git reads as no id (whose commit git fails to parse)."""


def is_ancestor(
    git: ReadOnlyGit, *pairs: tuple[str, str], cat_file: CatFile | None = None
) -> list[bool]:
    """For each (ancestor, descendant) pair of full object ids (one or more; none starts no git
    and gives []), whether `git merge-base --is-ancestor <ancestor> <descendant>` succeeds:
    whether the ancestor is reachable from the descendant (a commit is its own ancestor), as
    git sees history, through replace refs, grafts and a shallow clone's cut. A tag's id
    stands for its commit, as git peels it. A pair `merge-base` would fail on is False: an id
    of no object, or of one that peels to no commit (a tree, say), and anything but a full
    object id of the repository's, in lower case (a name, or what would be an option, is
    neither resolved nor passed to git). It raises only for what fails the whole call:
    GitError if git can't read the repository (e.g. NotARepositoryError), GitTimeoutError if
    a request takes longer than the client's `timeout`, and NotStartedError once its
    `stopping` says so.

    One `git cat-file --batch-command` reads every commit the pairs need, each once: the
    tool's own `cat_file`, if it gives one (it must be for `git`'s repository, which isn't
    checked: another repository's gives wrong answers), else one started for the call. Where
    the shallow and grafts files are is asked of `git_paths(git, "shallow", "info/grafts")`,
    which asks git once per client (one `rev-parse`) and remembers: a tool that has its own
    `git_paths` or `git_paths_and_head` ask for them too, on the same client, starts no git
    for them here. With both, a call starts no process (but the fallbacks below).

    A shared `cat_file` can serve other threads at once: the walk asks it for one commit at a
    time, each request under the CatFile's lock, so other threads' requests go between them;
    holding it for a whole walk would save nothing worth having (an uncontended lock costs far
    less than a request) and keep the other threads waiting. What a call learns of the
    commits is its own, so calls in different threads don't share or race on it.

    A pair whose walk is long (see _WALK_LIMIT) or reads a commit git can't (damaged or
    missing, or with a parent line git can't follow), or with a full id cat-file can't peel to
    a commit (a missing object, a tree), is asked of `git merge-base --is-ancestor`, a process
    each, as is every pair while the repository has a grafts file (deprecated in git), which
    cat-file doesn't apply, or while git's environment sets GIT_SHALLOW_FILE.
    FakeGit can't run it (it starts no CatFile): a tool's unit tests replace it.

    Each pair's walk is git's own for `merge-base` (paint_down_to_common), in Python: from
    both commits, newest committer date first, it paints commits reachable from the ancestor
    A, those from the descendant D, and those painted both stale, which spreads to their
    parents. It's True as soon as the ancestor is painted D, and False once every commit
    queued is stale. So it reads the commits between the two, and down to where their
    histories meet, give or take those dated in between: for a stack's pairs, a few each,
    however long the history.

    Why it's right whatever the dates, which only order it: paint D spreads only from the
    descendant down to parents, so True is right. If the ancestor is reachable, each commit
    on the path to it is reachable from the descendant but not from the ancestor (that would
    make a cycle), so never painted A nor stale; a commit leaves the queue only to paint its
    parents with what it has, and is queued again when it gains a paint, and the walk ends
    only when what's queued is stale, so it can't end before each commit on the path has
    painted the next D, up to the ancestor: ending without that, False is right.

    Unrelated histories, or an ancestor the descendant's history meets far below, walk to
    the limit and then cost a `merge-base`, which walks the descendant's history down to the
    ancestor's generation where there's a commit-graph, and to where they meet (or all of it,
    if they don't) where there's none.
    """
    if not pairs:
        return []
    shallow_file, grafts_file = git_paths(git, "shallow", "info/grafts")
    # GIT_SHALLOW_FILE (mostly git's own, for receive-pack and its hooks) moves the shallow
    # list where `--git-path shallow` doesn't look.
    if grafts_file.exists() or "GIT_SHALLOW_FILE" in git.environment():
        return [_merge_base_says(git, *pair) for pair in pairs]
    shallow = frozenset[str]()
    if shallow_file.exists():
        shallow = frozenset(shallow_file.read_text(encoding="utf-8").split())
    if cat_file is not None:
        return _Walker(git, cat_file, shallow).answer_all(pairs)
    with CatFile(git) as own:
        return _Walker(git, own, shallow).answer_all(pairs)


def _merge_base_says(git: ReadOnlyGit, ancestor: str, descendant: str) -> bool:
    """What `git merge-base --is-ancestor` says of the pair, any failure being no. Only full
    object ids reach git, so neither can be taken for an option."""
    if not (OBJECT_ID.fullmatch(ancestor) and OBJECT_ID.fullmatch(descendant)):
        return False
    return git.succeeds("merge-base", "--is-ancestor", ancestor, descendant)


class _Walker:
    """Answers pairs through one CatFile, keeping each commit's committer date and parents,
    read once for all of them."""

    def __init__(self, git: ReadOnlyGit, cat_file: CatFile, shallow: frozenset[str]) -> None:
        self._git = git
        self._cat_file = cat_file
        # The commits a shallow clone has cut from their parents, as git treats them.
        self._shallow = shallow
        self._commits: dict[str, tuple[int, tuple[str, ...]]] = {}
        # Each id a pair gave: what _peel makes of it.
        self._peeled: dict[str, str | None] = {}

    def answer_all(self, pairs: tuple[tuple[str, str], ...]) -> list[bool]:
        """Each pair's answer, a pair repeated answered once."""
        answers: dict[tuple[str, str], bool] = {}
        for pair in pairs:
            if pair not in answers:
                answers[pair] = self.answer(*pair)
        return [answers[pair] for pair in pairs]

    def answer(self, ancestor: str, descendant: str) -> bool:
        a, d = self._peel(ancestor), self._peel(descendant)
        if a is None or d is None:
            return False
        if not (a and d):
            # A full id cat-file couldn't peel: none of a commit to git either (a tree, a
            # missing object), or one cat-file has taken for another type's, as it does a
            # tag's once it has parsed a corrupt commit naming the tag as a parent.
            return _merge_base_says(self._git, ancestor, descendant)
        try:
            return self._walk(a, d)
        except (_TooFar, _OddCommit, *_UNREADABLE):
            return _merge_base_says(self._git, a, d)

    def _peel(self, object_id: str) -> str | None:
        """The commit `object_id` is, or a tag of it points to; None if it's not a full
        object id of the repository's length, and "" if cat-file can't peel it to a commit."""
        if object_id not in self._peeled:
            found = None
            if OBJECT_ID.fullmatch(object_id):
                try:
                    found = self._cat_file.info(f"{object_id}^{{commit}}")
                except _UNREADABLE:
                    found = None
            if found is None:
                self._peeled[object_id] = "" if OBJECT_ID.fullmatch(object_id) else None
            elif len(found.id) != len(object_id):
                # An id shorter than the repository's (40 hex digits in a SHA-256 one) is an
                # abbreviation, which cat-file resolves: the commit's id has the full length.
                self._peeled[object_id] = None
            else:
                self._peeled[object_id] = found.id
        return self._peeled[object_id]

    def _commit(self, commit_id: str) -> tuple[int, tuple[str, ...]]:
        """The commit's committer date and parents (none for one a shallow clone cut).
        Raises what CatFile.read does for one it can't read, and _OddCommit."""
        if commit_id not in self._commits:
            # Not peeled (`<id>^{commit}`): a parent line naming a tag names no commit to git.
            kind, content = self._cat_file.read(commit_id)
            if kind != "commit":
                raise _OddCommit
            # Read as git's parse_commit_buffer reads it: its parents are the run of parent
            # lines right after its tree (a later one is no parent), and its date is that of
            # the committer line right after them and the author line (else 0).
            headers = content.split(b"\n\n", 1)[0].split(b"\n")
            end = 1
            while end < len(headers) and headers[end].startswith(b"parent "):
                end += 1
            # git reads each id's hex in either case; one it can't read fails the commit.
            parents = [line.removeprefix(b"parent ").lower() for line in headers[1:end]]
            if not all(
                _OBJECT_ID_BYTES.fullmatch(parent) and len(parent) == len(commit_id)
                for parent in parents
            ):
                raise _OddCommit
            date = 0
            after = headers[end : end + 2]
            if (
                len(after) == 2
                and after[0].startswith(b"author ")
                and after[1].startswith(b"committer ")
            ):
                # "committer <name> <<email>> <seconds> <zone>"; any number orders it.
                fields = after[1].rsplit(b" ", 2)
                date = int(fields[1]) if len(fields) == 3 and fields[1].isdigit() else 0
            ids = () if commit_id in self._shallow else tuple(parent.decode() for parent in parents)
            self._commits[commit_id] = (date, ids)
        return self._commits[commit_id]

    def _walk(self, ancestor: str, descendant: str) -> bool:
        """Whether ancestor is reachable from descendant (see is_ancestor). Raises
        _TooFar past _WALK_LIMIT commits, and what _commit does."""
        if ancestor == descendant:
            return True
        marks = {ancestor: _FROM_ANCESTOR, descendant: _FROM_DESCENDANT}
        # (newest first, then first queued first, commit). A commit is queued at most once at
        # a time, and painted with its marks as they are when it's taken.
        queue: list[tuple[int, int, str]] = []
        queued: set[str] = set()
        not_stale = 0  # how many queued commits aren't stale
        pushes = 0

        def push(commit: str) -> None:
            nonlocal not_stale, pushes
            heapq.heappush(queue, (-self._commit(commit)[0], pushes, commit))
            pushes += 1
            queued.add(commit)
            if not marks[commit] & _STALE:
                not_stale += 1

        push(ancestor)
        push(descendant)
        taken = 0
        while not_stale:
            taken += 1
            if taken > _WALK_LIMIT:
                raise _TooFar
            _, _, commit = heapq.heappop(queue)
            queued.remove(commit)
            mark = marks[commit]
            if not mark & _STALE:
                not_stale -= 1
                if mark & _FROM_BOTH == _FROM_BOTH:
                    mark = marks[commit] = mark | _STALE
            for parent in self._commit(commit)[1]:
                had = marks.get(parent, 0)
                now = had | mark
                if now == had:
                    continue
                if parent == ancestor and now & _FROM_DESCENDANT:
                    return True
                marks[parent] = now
                if parent not in queued:
                    push(parent)
                elif now & _STALE and not had & _STALE:
                    not_stale -= 1
        return False
