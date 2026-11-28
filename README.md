# py-git-client

Run git from Python with as few processes as possible. Starting a process costs well under a
millisecond on Linux, but several on macOS and tens on Windows, so a tool that runs git many
times a command is only as fast as the number of git processes it starts. py-git-client is
what git-stack, git-recommit and move-forward share for that:

- **One way to start git** (`Git`), the quickest each system allows: by its full path, with
  `-C` rather than a working directory. Text and paths go to git as git expects them
  (see `Git.command`).
- **One long-lived `git cat-file --batch-command`** (`CatFile`) for resolving names and
  reading objects, which keeps answering after git exits on a name (e.g. `@{u}` without an
  upstream).
- **Batched reads and writes**, each one process: every config entry (`config`), refs with
  their upstreams, symbolic refs, HEAD and typed tracking fields (`refs`), patch ids for many
  commands (`patch_ids`), where git keeps many files in the git directory (`git_paths`), the
  git directory and the common one (`git_dir`, `git_common_dir`), blobs as they are
  (`hash_blobs`), and ref updates in one transaction (`update_refs`).
- **Remotes**: a fetch, and what it did to each ref (`fetch`), the refs a remote has
  (`remote_refs`), and the URLs git fetches it from and pushes it to (`remote_urls`).
- **Other writes and merges**, each one process: a blob as `git add` would store it at a path,
  through its filters (`hash_blob_at`), and a three-way merge of contents (`merge_file`).
- **Merges of many pairs of commits in one process** (`merge_trees`, git 2.39's `git
  merge-tree --stdin`; on git 2.38, one each), each with a merge base of its own if given
  (git 2.40), as for a cherry-pick: each merge's tree and conflicted paths, or why git
  couldn't merge that pair.

It has no required dependencies (`pygit2` is an optional extra), and needs Python 3.15 or
newer and git 2.36 or newer (for `git cat-file --batch-command`; 2.38 for `merge_trees`).

## Installing

```sh
uv add git+https://github.com/benekastah/py-git-client
```

or `pip install git+https://github.com/benekastah/py-git-client`.

## Using it

```python
from git_client import (
    CatFile,
    ConfigSet,
    ConfigUnset,
    Git,
    ObjectType,
    RefUpdate,
    color_enabled,
    config,
    config_values,
    git_common_dir,
    git_dir,
    git_paths,
    git_paths_and_head,
    hash_blob_at,
    hash_blobs,
    merge_file,
    refs,
    update_refs,
)

# The git on PATH (wrappers such as git-ai included), unless $MY_TOOL_GIT names another:
# a tool names its own variable so its users can choose, e.g. git itself, for speed. Only
# PATH's absolute directories count: an empty or relative entry (e.g. ".") is skipped, on
# purpose, so a git that a repository holds is never run from it.
# Raises FileNotFoundError if there's neither, and, on Windows, ValueError if the variable
# names a batch file (.bat, .cmd), which cmd.exe runs, reading &, | and % in arguments.
git = Git("path/to/repo", env_var="MY_TOOL_GIT", timeout=120)  # seconds; None (the default): none

git.run("rev-parse", "--abbrev-ref", "HEAD")  # "main"; raises GitError on failure
git.run("status", "--porcelain", strip=False)  # " M file\n": as git wrote it, not stripped
git.ok("merge-base", "--is-ancestor", "a", "b")  # True for exit 0, False for 1 (git's no)
git.succeeds("merge-base", "--is-ancestor", "a", "b")  # the same, but False also if a or b
# names nothing: True for exit 0, False for any other
git.resolve("main@{upstream}")  # an object id, or None if it names nothing
git.var("GIT_AUTHOR_IDENT")  # git's value, or None if no identity is set
git.prefix()  # "src/" in path/to/repo/src: from the top of the worktree; "" at the top;
git.cdup()  # "../" there: the way up to the top, as git prints it, asked with prefix()
# git config, tried again while locked, for each change; what each did: an unset that removes
# nothing is NOT_FOUND, or SEVERAL_VALUES (for a key with several values: give the one to
# remove), not an error. Other config writes (--add, ...): git.run("config", ...).
# ConfigUnset(key, value) removes only that value, or a key with no value whatever value
# says (as git from 2.46.1; older git crashes on it).
git.write_configs(ConfigSet("branch.a.base", "main"), ConfigUnset("branch.a.old"))
git.passthrough("rebase", "-i", "main", timeout=None)  # a command's own timeout; past one,
# git is stopped (see below) and GitTimeoutError raised; only passthrough gives git the
# tool's stdin: the others give it their `input`, or nothing

with CatFile(git) as cat_file:  # one process for every lookup in the block
    head = cat_file.resolve("HEAD")  # an object id, or None if it names nothing
    main = cat_file.info("main")  # its id, type, size and git's warnings (e.g. ambiguous), or None
    upstream = cat_file.verify("main@{u}")  # an object id, or UnknownRevisionError: git's reason
    readme = cat_file.resolve("HEAD:README.md")
    if readme is not None:
        kind, content = cat_file.read(readme)
    wanted: ObjectType = "tree"  # or "blob", "commit", "tag"
    _, tree = cat_file.read(upstream, wanted)  # its tree; WrongTypeError if it has none

remotes = config(git)["remote.origin.url"]  # every value of each key
# Keys exactly as `git config --list -z` prints them: section and name in lower case, the
# subsection as written. config(git).get("init.defaultBranch") finds nothing, with no error.
# For a key a user typed, run `git config --get <key>` (one process).
default_branch = config(git).get("init.defaultbranch")
push_remote = config(git).get("branch.Topic.pushremote")
# Values read by git as a type ("bool", "int", "bool-or-int", "bool-or-str", "path",
# "expiry-date", "color"; none: as written), the last of each key's, or None if it isn't set:
# {"commit.gpgsign": True, "merge.verifysignatures": None}. One git per call, so one per type;
# keys as config() gives them. GitError for a value git can't read as the type.
signing = config_values(git, "commit.gpgsign", "merge.verifysignatures", type="bool")
# Whether to color output, as git decides for a setting, color.ui, and "auto" on a terminal.
colored = color_enabled(git, "color.stack", stdout_is_tty=sys.stdout.isatty())
for ref in refs(git, "refs/heads/").values():
    print(ref.name, ref.object, ref.upstream, ref.head)
# Typed attributes, each read only if asked (None otherwise), in the same one process: how
# each branch stands against its upstream and what it pushes to, as git says it (a Track:
# ahead, behind, or gone; None with none), the upstream's remote ("." for a local branch) and
# name there, git's push destination and its remote, the committer date, and commits ahead of
# and behind a base (git 2.41's %(ahead-behind:...); before, one more process per ref).
both = refs(git, "refs/heads/", track=True, push_track=True, remotes=True, dates=True)
counted = refs(git, "refs/heads/", ahead_behind="refs/heads/main")
print(both["refs/heads/main"].track, counted["refs/heads/a"].ahead_behind)  # Track(), (0, 0)
# for-each-ref's filters (contains, no_contains, merged, no_merged, points_at) and order:
# the branches main has, newest first, then by name; the first 10.
done = refs(git, "refs/heads/", merged="main", sort=["-committerdate", "refname"], count=10)

# Give git full ref names: a short one is ambiguous when a tag has the same name.
old = refs(git, "refs/heads/old")["refs/heads/old"].object
update_refs(git, [RefUpdate("refs/heads/old", None, old=old)])  # all or none; only if unmoved

# Absolute Paths, where git itself looks: in a linked worktree, HEAD is its own and refs the
# main repository's; config such as core.hooksPath moves hooks.
head_file, hooks = git_paths(git, "HEAD", "hooks")
# The same, and what HEAD points at, from the same one git: Head(branch="refs/heads/main"),
# Head(detached=True), or Head() when git can't say (a branch with no commits yet).
(head_file, hooks), head = git_paths_and_head(git, "HEAD", "hooks")
# The client's git directory (in a linked worktree, its own: its HEAD, index and rebase state),
# and the common one, with the refs, objects and config. Both can be cached (see "Caching").
# NotARepositoryError if there's no repository.
own, common = git_dir(git), git_common_dir(git)

# Blobs: as they are (no filters or line-ending conversion), written, with one process...
[raw] = hash_blobs(git, [b"one\r\n"])
# ...or as `git add` would store one at a path (its clean filter, `text`, core.autocrlf), and
# written only if asked.
cleaned = hash_blob_at(git, "notes.txt", b"one\r\n", write=True)
# From a subdirectory, for a path as git reports it (from the top): paths_from_top=True.

# A three-way merge of contents, writing nothing to the repository: the result, with conflict
# markers, and how many conflicts it has (at most 127). GitError if git can't merge them.
merged, conflicts = merge_file(
    git, ours=b"ours\n", base=b"base\n", theirs=b"theirs\n", options=["--diff3"]
)
```

```python
# Needs git 2.38.
from git_client import Git, merge_trees, refs

# Merges of commits, by their full ids (each pair as `git merge-tree --write-tree` merges it),
# with one git for all of them: whether each is clean, its tree and its conflicted paths (from
# git.directory), or, if git couldn't merge a pair (clean is None), why.
git = Git("path/to/repo")
heads = refs(git, "refs/heads/")
a, b = heads["refs/heads/a"].object, heads["refs/heads/b"].object
for merge in merge_trees(git, [(a, b), (b, a)]):
    if merge.clean is None:
        print(merge.error)  # e.g. UnknownRevisionError, or histories that share nothing
    else:
        print(merge.clean, merge.tree, merge.conflicts)  # e.g. True, "<a tree id>", ()
```

```python
# Needs git 2.40.
from git_client import Git, Merge, merge_trees, refs

# A Merge gives the merge base (pairs and Merges mix, still in one git). Cherry-picking a
# commit onto a is merging it into a with its parent as the base: Merge(a, commit,
# base=<its parent>). Here, b's changes since main, onto a. From git 2.45, all three can be
# trees. merge-ort's result can differ from `git apply --3way` of `git show`'s patch: it finds
# renames, and for a merge commit brings in its second parent's changes.
git = Git("path/to/repo")
heads = refs(git, "refs/heads/")
a, b, main = (heads[f"refs/heads/{name}"].object for name in ("a", "b", "main"))
[picked] = merge_trees(git, [Merge(a, b, base=main)])
print(picked.clean, picked.tree)  # e.g. True, "<a tree id>"
```

A CatFile's process stops once nothing uses it. With a client's `timeout`, a request it
doesn't answer in time raises `GitTimeoutError`, and the next request starts another process.
A `Git` subclass that keeps a CatFile of
itself makes it with `CatFile(self, weak=True)`, so the two make no reference cycle (which
would keep the client and its process until the garbage collector ran). Don't pass
`weak=True` for a client nothing else holds: it would be freed at once.

Paths a tool got from the filesystem (rather than from git) can be given as bytes, in the
filesystem's encoding; text, as str, which reaches git as UTF-8 whatever the locale's
encoding. Both go for arguments and for values in `env`. Output is decoded as UTF-8 with
surrogateescape, so bytes that aren't UTF-8 survive a round trip through `encode`. Those
surrogates can't be written as UTF-8 (to a file, a web page, JSON), so for a tool that stores
or shows text, `printable(text)`, for what `run`, `config` or `refs` gave, replaces each with
U+FFFD. Commands whose output the library reads run with `GIT_TEXTDOMAINDIR` naming no
directory, so git finds no translation and its messages are in English whatever the user's
language, which is how the library tells its failures apart. The user's locale (`LC_ALL`,
`LANG`, `LC_CTYPE`, ...) is left as it is, so what git runs (hooks, filters, credential
helpers, gpg, ssh) runs in it; only a git that those start themselves (e.g. a hook's `git`)
speaks English too, as git takes its language from its environment alone, which they inherit.
git's own pattern matching (`log --grep`, `--author`, `-G`, `-i`, `git grep`) follows the
user's `LC_CTYPE` from git 2.39, and C's before; `-P` patterns are UTF-8 on every version.
`passthrough`, whose output is the user's, keeps the user's language for git's messages as
well.

Past its timeout, or when the tool is interrupted (`KeyboardInterrupt`) while it runs, git is
first asked to stop (on Linux and macOS with SIGINT, as Ctrl-C asks it), so it can remove its
lock files, and killed a quarter of a second later (at once, if the tool is interrupted again
meanwhile); the call then waits at most a second more for its output. When the tool has no
terminal (CI, a service), a command with a timeout runs in a process group of its own on Linux
and macOS, so what git started (hooks, ssh, credential helpers) is stopped with it. With a
terminal, git stays in its foreground, as from a shell, so it can ask for a password or open an
editor whatever its timeout, and only git is stopped. On Windows, git and what it started are
killed: past a timeout at once, with no chance to clean up, and on an interrupt once git has
had a quarter of a second to handle the Ctrl-C that reached it too. What outlives git's own
exit then (e.g. a hook that ignores Ctrl-C) runs on: Windows finds what git started through
git.

A git in a group of its own doesn't get a signal sent to the tool's group (e.g. SIGTERM from
`timeout`, or a CI runner cancelling a job), and Python's default SIGTERM ends the tool without
stopping it. So a tool should turn SIGTERM into an exception in its main thread; any exception
that reaches a running command stops git as an interrupt does:

```python
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # 128 + SIGTERM, as a shell reports it
```

Like Ctrl-C's `KeyboardInterrupt`, that exception is raised in the main thread alone, so it
stops only a git the main thread runs. A git another thread runs goes on until it finishes or
times out, and is orphaned if the tool is killed first; to have it stopped with the tool, give
those commands no timeout, so git stays in the tool's group and gets the signal itself.

`Git(..., process_groups=False)` keeps git in the tool's group whatever its timeout: past one,
or on an interrupt, only git is stopped, and what it started runs on, as with a terminal. In
exchange each command starts faster without a terminal: by about 50 µs on Linux, and more on
macOS, where a group of its own keeps Python from using posix_spawn. It suits a tool that runs
many short commands, whose git starts nothing that could outlive it.

Git processes start with only their standard streams. Launches close extra inheritable file
descriptors or handles.

A tool that's shutting down can have its clients start no more git, whichever thread asks,
with `stopping`, which each client asks before it starts git:

```python
stop = threading.Event()
git = Git("path/to/repo", stopping=stop.is_set)
stop.set()  # e.g. on Ctrl-C, from any thread
try:
    git.run("fetch")
except NotStartedError:  # each command from now on, which starts no git
    pass
```

It doesn't stop a git that's already running: a command runs on until it finishes or times
out (or its thread is interrupted, as above), and a `CatFile`'s process keeps answering until
it's closed, though one that has to start (again) raises. `FakeGit` takes it too.

### Commits

`commits` lists the commits git's revision walk finds, with what each says of itself, from one
`git rev-list`; `count` and `any_commit` ask the same of git without listing them:

```python
from datetime import UTC, datetime

from git_client import Git, Revisions, any_commit, commits, count

git = Git("path/to/repo")
# git's main..a: reachable from a (or each of several) and from none of the excludes.
for commit in commits(git, Revisions("a", exclude="main"), reverse=True):  # oldest first
    commit.id, commit.parents, commit.subject  # full ids; a merge's first parent first
    commit.author.name, commit.author.email, commit.author.date  # an aware datetime, or None
    commit.committer  # a Signature too; commit.message: None, unless messages=True
mine = Revisions(
    ["a", "b"],
    exclude="main",
    merges=False,  # True: only merges
    since=datetime(2026, 1, 1, tzinfo=UTC),  # by the committer's date
    author="<me@example.com>",  # a fixed string in "Name <email>", with no mailmap
    paths=["src"],  # changing these paths, from the top of the worktree ("": all of it), literally
)
count(git, mine)  # how many
any_commit(git, Revisions("a", exclude="main", merges=True))  # whether any: stops at the first
# Leaving out each commit whose patch is already in main (git's --cherry-pick --right-only).
commits(git, Revisions("a", exclude="main", cherry_pick=True), topo_order=True, max_count=10)
```

The revisions reach git on its stdin, as many as it takes, so none is read as an option: a
`Revisions` refuses one that is empty, starts with `-`, or holds a newline, a carriage return
or a NUL (ValueError), which git would read as something else. Each message is read as UTF-8
whatever `i18n.logOutputEncoding` says. A date is None where the commit records none, or one a
datetime can't hold (a bad zone, a year past 9999), which git takes and fsck only warns of.
`first_parent=True` follows only each merge's first parent.

### Reflogs

`reflogs` reads where refs have been, from their reflogs, with one `git log --walk-reflogs`
for all of them:

```python
from git_client import Git, reflogs

git = Git("path/to/repo")
for ref, entries in reflogs(git, "refs/heads/b", "HEAD").items():  # by each ref as given
    for entry in entries:  # newest first; [] for a ref with no reflog
        entry.index  # its place: the ref's @{index}, 0 the newest
        entry.id, entry.message  # the commit the ref moved to, and why, as git wrote it
        entry.name, entry.email  # who moved it
```

Each ref is a full name: `HEAD` or one starting with `refs/`. A shorter one (`origin/main`)
raises ValueError, because git names its entries by whichever ref it finds the reflog of, so
they might not match the name given. So do names holding `~`, `^` or `:` (no ref's name can,
so git reads a revision, e.g. `refs/heads/main~1`, and prints nothing), `..` (a range), `@{`
or a NUL. A ref that names nothing raises
`UnknownRevisionError`, for the whole call. git prints neither an
entry's old id nor, beside its index, its date, so an entry has neither. `reflogs` runs `git
log --walk-reflogs -z --no-show-signature --format=<fields> --end-of-options <refs> --`, so a
`FakeGit` test responds to `["log", "--walk-reflogs"]` with each entry's selector, id, name,
email and message, each ended by a NUL.

### Ancestry

How commits are related, as `git merge-base` answers, each a typed function:

```python
from git_client import Git, fork_point, independent, is_ancestor, merge_base, merge_bases

git = Git("path/to/repo")
is_ancestor(git, "main", "a")  # whether main is in a's history (or is a)
merge_base(git, "main", "a")  # a best common ancestor's id, or None with no history in common
merge_bases(git, "main", "a")  # all of them (more than one after criss-cross merges), or []
independent(git, "main", "a", "b")  # the ids of those none of the others reaches
# Where a forked from main, looking in main's reflog too, for a commit main was rewritten
# from; None if git finds none. main must be a ref: a name that names none raises.
fork_point(git, "refs/heads/main", "a")
```

Each runs one `git merge-base`, so `is_ancestor` takes one process a pair: git has no form for
many pairs (to test many refs against one commit, `refs(git, merged=...)` asks once).
`independent` takes as many commits as fit on a command line. A name that names nothing
raises `UnknownRevisionError`, and one that names no commit (e.g. a tree) `WrongTypeError`.
`fork_point` gives None for a full id the repository lacks, as git says nothing of it, as when
it finds no fork point.
In a `FakeGit` test, respond to `["merge-base", "--is-ancestor"]` (or `--all`,
`--independent`, `--fork-point`; `["merge-base", "--end-of-options"]` for `merge_base`) with
exit status 0 and the ids, one a line, or 1 for git's no.

### Diffs and trees

`changed_paths` lists the paths each of many diffs changes, and `line_changes` how many lines
each adds and removes there, with a `CatFile` to resolve the names and one `git diff-tree --stdin`
for all the diffs:

```python
from git_client import ReadOnlyGit, changed_paths, line_changes

reader = ReadOnlyGit("path/to/repo")
# A commit, against its parent (a root commit against nothing; a merge isn't diffed, as git
# does by default: give (merge.parents[0], merge.id) for its first parent's), or a pair,
# (old, new), of commits or trees: any revision, e.g. commit.id from commits(), "main",
# "a^{tree}", "main:src", ":/fix the bug".
for changes in changed_paths(reader, ["main", ("main", "a")]):  # one list each
    for change in changes:  # in git's order, recursing into directories; [] if none
        change.status, change.path  # "A", "D", "M", "R", "T", ...; from the top
        change.old_mode, change.new_mode, change.old_id, change.new_id  # "000000", zeros: none
        change.old_path, change.similarity  # a rename's; None otherwise
[counts] = line_changes(reader, [("a", "main")], paths=["src", "README.md"])  # literally
for count in counts:
    count.path, count.added, count.removed  # both None for a binary file
```

Both find renames, as `git diff` and `git log` do by default (so a rename is one path, its new
one); `renames=False` gives a deletion and an addition, as diff-tree, git's plumbing, does by
default. Past `diff.renameLimit`, git finds only exact renames, warning only on its stderr,
which is dropped. Both also take `ignore_submodules=True`, to leave out submodules' changes, and
`pathspecs`, git's pathspecs as a user types them (from the client's directory, with wildcards
and magic). A file that becomes a symlink or a submodule, or the reverse, has status `T`; one
that becomes a directory, or the reverse, is deleted and its new form added. No other diff
config changes what git reports. A name that is empty or holds a newline, a carriage return or a
NUL raises ValueError; one that names nothing, `UnknownRevisionError`; one that names what isn't
a commit (in a pair, a tree), `WrongTypeError`. As they use a `CatFile`, a `FakeGit` can't
answer them (it raises `UnexpectedCommand`): a project tests the code that calls them on a real
repository (e.g. one of the plugin's repository templates), and checks the processes it starts
with `git_calls`. Merges' combined diffs (`-c`, `--cc`, `-m`), copies (`-C`) and patches aren't
supported.

`tree_entries` lists a tree's entries, from one `git ls-tree -z` (which takes one tree, so it's
one process for each):

```python
from git_client import ReadOnlyGit, tree_entries

reader = ReadOnlyGit("path/to/repo")
for entry in tree_entries(reader, "main"):  # a tree, or a commit's; its top, in git's order
    entry.mode, entry.type, entry.id, entry.path  # "100644", "blob" ("tree", "commit"), ...
tree_entries(reader, "main", recursive=True)  # every file, in every directory (none of those)
# Only these paths, from the top, literally: a directory's own entry, or with recursive=True
# or a path ending in "/", its files.
tree_entries(reader, "main", ["README.md", "src/"])
```

A name that names nothing raises `UnknownRevisionError`, and one of a blob `WrongTypeError`. A
`FakeGit` test responds to `["ls-tree", "-z", "--full-tree"]` with git's output.

### Push results

`push` makes many changes to a remote's refs with one `git push`, and reads what git says
it did at each destination (each push URL), even when some failed:

```python
git = Git("path/to/repo")
result = push(
    git,
    "origin",  # a remote's name, or a URL
    [
        PushUpdate("refs/heads/a", "a"),  # origin's a to our a, if that's a fast-forward
        PushUpdate("refs/heads/b", "b", force=True),  # even if it isn't
        # Only if origin's main is still at that object ("": only if it has no main).
        PushUpdate("refs/heads/main", "main", lease=git.run("rev-parse", "origin/main")),
        # PushUpdate("refs/heads/old", None) would delete origin's old.
    ],
    atomic=True,  # all or none, at each destination; also set_upstream=True, no_verify=True
)
result.returncode  # git's exit status; result.stdout and result.stderr, what it printed
for destination in result.destinations:  # in order, as git printed them
    destination.url  # as git printed it
    destination.done  # False: the push there failed outright (e.g. an unpacker error)
    for ref in destination.refs:  # each ref git reported there
        ref.target, ref.source, ref.status  # e.g. PushStatus.FAST_FORWARD, REJECTED
        ref.old_oid, ref.new_oid  # full object ids, for a fast-forward or forced update
```

A `PushUpdate` whose source starts with `+` or is `""` raises ValueError: only `force`
forces, and only None deletes.

Without `atomic`, git makes the changes it can and rejects the others. A ref git didn't hear
back about has status UNKNOWN. A destination whose push failed outright has `done` False,
and git goes on to the next push URL. One whose push failed before git reported any ref
(e.g. the pre-push hook refused it) has no report, and git goes on too: a nonzero
`returncode` while every destination listed is done and accepted means an unlisted one
failed, as `stderr` says. One git can't reach (e.g. not a repository) stops the push, so it
and the push URLs after it have no report. Git's push config still applies (e.g.
`push.followTags`, whose tags it reports too). Other switches (`--dry-run`, push options,
...) aren't supported. `push` runs `git -c core.abbrev=no push --porcelain [options] --
<remote> <refspecs>`, so a `FakeGit` test responds to `["-c", "core.abbrev=no", "push",
"--porcelain"]` with porcelain output.

### Remotes

`fetch` fetches with one `git fetch`, and reads what it did to each ref, even when some
failed:

```python
git = Git("path/to/repo")
result = fetch(
    git,
    "origin",  # a remote's name, a URL, or a path ("." for this repository)
    [
        # origin's main into origin/main, only if that's a fast-forward.
        FetchRefspec("refs/heads/main", "refs/remotes/origin/main"),
        FetchRefspec("refs/heads/a", "refs/heads/a", force=True),  # even if it isn't
        FetchRefspec("refs/heads/b"),  # into FETCH_HEAD only
    ],  # none: the remote's configured refspecs
    prune=True,  # delete remote-tracking refs origin no longer has
    tags=False,  # no tags (True: every tag; None, the default: as the config says)
)  # also write_fetch_head=False
result.returncode  # 1 if a ref was rejected, the others fetched; 128 if nothing was
for ref in result.refs or ():  # None before git 2.41
    ref.ref, ref.status  # e.g. "refs/heads/a", FetchStatus.FORCED; "FETCH_HEAD", NEW
    ref.old_oid, ref.new_oid  # full object ids, or None for none (new, or pruned)
```

A `FetchRefspec` whose source is empty, starts with `+` or `^`, or holds a `:`, or whose
destination is `""`, raises ValueError: only `force` forces, and only None leaves out the
destination. Fetching from a remote's name, git also updates (and reports) the
remote-tracking refs its config maps what's fetched to. `fetch` runs `git fetch --porcelain
[options] -- <remote> <refspecs>`, so a `FakeGit` test responds to `["fetch", "--porcelain"]`
with porcelain output; lines of other shapes (git prints some messages to stdout) are
skipped. Git before 2.41 has no `--porcelain`: fetch runs again without it, and `refs` is None.
A ref already up to date isn't in `refs` (git lists those only with `--verbose`). Its output
is captured, so nothing shows while git fetches; with `--porcelain` git prints no summary
(`From ...`, `[new branch]`), and older git's goes to `stderr`.

`fetch` fetches no submodules, on any git: git takes no `--recurse-submodules` with
`--porcelain`, so it ignores `fetch.recurseSubmodules` (whose default fetches the submodule
commits a fetched superproject commit needs), and on older git `fetch` passes
`--no-recurse-submodules` to match. A tool that wants them runs `git submodule update`, or
fetches in each submodule.

`remote_refs` asks a remote what refs it has, with one `git ls-remote` (a remote each), and
raises GitError if git can't (e.g. it can't reach the remote):

```python
git = Git("path/to/repo")  # or a ReadOnlyGit
found = remote_refs(git, "origin", symrefs=True)  # every ref, and where HEAD points
found.refs  # {"HEAD": "<id>", "refs/heads/main": "<id>", ...}
found.symrefs  # {"HEAD": "refs/heads/main"}
# Branches that end with one of the patterns, at a "/" ("main" matches refs/heads/main).
remote_refs(git, "origin", ["refs/heads/main", "refs/heads/gone"], heads=True).refs
```

`env` (e.g. a `GIT_CONFIG_COUNT` config change) is added to git's environment. A `FakeGit`
test responds to `["ls-remote"]`.

`remote_urls` gives the URLs git fetches a remote from, or pushes it to, as git rewrites them
(`url.<base>.insteadOf`, and for pushes `pushInsteadOf`), from one `git remote get-url
--all`:

```python
git = Git("path/to/repo")  # or a ReadOnlyGit
remote_urls(git, "origin")  # e.g. ["git@github.com:me/repo.git"]
remote_urls(git, "origin", push=True)  # each push URL: its pushurls, else its URLs
```

git prints a URL a line each, as it is, so one with a newline in it reads as two. A remote the
repository's own config doesn't name raises GitError: git ignores one set only in the global or
system config, by `-c` or by the environment (which `fetch` and `remote_refs` take). A
`FakeGit` test responds to `["remote", "get-url"]`.

### Status and worktrees

`status` reads a worktree's status from one `git status --porcelain=v2 -z`, and `worktrees`
lists a repository's worktrees from one `git worktree list --porcelain -z`. Both take a
`ReadOnlyGit`:

```python
reader = ReadOnlyGit("path/to/repo")
found = status(
    reader,
    untracked="all",  # git's --untracked-files: "no", "normal" or "all"; None: its config
    ignored="no",  # or "traditional" or "matching", as git's --ignored
    ignore_submodules=None,  # or "none", "untracked", "dirty" or "all"; None: its config
    renames=None,  # True or False: --renames or --no-renames; None: its config
    branch=True,  # also read where HEAD is, into found.branch
)
for entry in found.entries:  # paths from the worktree's top, as git gives them
    match entry:
        case Renamed():  # a Changed, with entry.original and entry.similarity
            entry.index, entry.worktree  # e.g. "R" and "."
        case Changed() | Unmerged():  # Unmerged: a conflict
            entry.index, entry.worktree, entry.submodule  # e.g. "." and "M", None
        case Untracked() | Ignored():
            entry.path
assert found.branch is not None
found.branch.head, found.branch.upstream  # "main", "origin/main"; None if detached, or none
found.branch.ahead_behind  # (0, 0); None without an upstream

for worktree in worktrees(reader):  # the main one first
    worktree.path, worktree.head, worktree.branch  # branch: "refs/heads/main"; None if detached
    worktree.locked, worktree.prunable  # git's reason ("" if none), or None
```

git runs `status` with `--no-optional-locks`, so it doesn't refresh the index, as a plain `git
status` would, writing the stat information it found stale. That makes it a read, but a
repository whose index is stale pays for that at each call. A `ReadOnlyGit`'s
`-c core.fsmonitor=false` also keeps an fsmonitor from speeding it up; a `Git` keeps it.
A `FakeGit` test responds to `["--no-optional-locks", "status", "--porcelain=v2", "-z"]` and
to `["worktree", "list", "--porcelain", "-z"]` with git's output.

`add_worktree`, `remove_worktree` and `prune_worktrees` are `git worktree`'s writes, one process
each, which take a `Git`:

```python
git = Git("path/to/repo")
# A path from the client's directory; a commit, or a branch, checked out there unless detach.
add_worktree(git, "../scratch", "main", detach=True)  # git runs the post-checkout hook
remove_worktree(git, "../scratch", force=True)  # even with changes (not if it's locked)
prune_worktrees(git)  # forget those git would prune, e.g. whose directory is gone
```

Each raises `GitError` for what git refuses (`UnknownRevisionError` for a commit that names
nothing), e.g. a branch checked out in another worktree, or changes without `force`.

### Hooks and messages

`run_hook` runs one of the repository's hooks with one `git hook run`, and `stripspace` cleans a
message up as git does, with one `git stripspace`:

```python
git = Git("path/to/repo")
# Wherever git finds it (core.hooksPath, ...); none: success, unless ignore_missing=False.
result = run_hook(git, "commit-msg", "path/to/message", ignore_missing=True, env=None)
result.returncode, result.stderr  # its answer and its output (git sends its stdout there too)
stripspace(git.read_only(), "Subject  \n\n\n# a comment\n", strip_comments=True)  # "Subject\n"
stripspace(git, "Subject\n; a comment\n", strip_comments=True, comment_char=";")
```

`run_hook` returns a hook's failure rather than raise: it's the hook's answer. It raises
`GitError` when git runs no hook and says why: without one and `ignore_missing=False`, or for a
name newer git doesn't know as a hook's. git's other failures (e.g. 2.36's outside a
repository) come back as a result, as their exit status can't be told from a hook's. A
`FakeGit` test responds to `["hook", "run"]`. `stripspace` takes the comment character from
core.commentChar unless given one; for `auto`, it takes "#" (only `git commit` chooses one from
the message). It reads nothing, so a `ReadOnlyGit` offers it.

### Caching

By default, a client asks Git for repository metadata on each call. Pass `cache=GitCache()`
to reuse `prefix()`, `cdup()`, `object_id_length()`, `git_paths`, `git_dir` and
`git_common_dir`. A cache miss can fill in other metadata in the same `git rev-parse`.
`git_paths_and_head` can fill the cache and always reads HEAD afresh.

```python
from git_client import Git, GitCache

cache = GitCache()
git = Git("path/to/repo", cache=cache)
git.prefix()  # asks Git and caches the metadata
git.read_only().object_id_length()  # uses the same cache
cache.clear()  # invalidate all clients sharing the cache
```

A cache keeps answers until it is cleared or discarded. A long-lived service can make a
fresh cache for each refresh and share it across the clients used during that refresh.
`GitCache.clear()` or `git.forget()` invalidates it during the refresh if a repository
or worktree moves. Entries are separated by the Git program, directory, and client
environment. Keep the process environment stable for the cache's lifetime.

`read_only()` and shallow copies share the cache. Deep copies and unpickled clients get
an empty cache if the original client had one. An explicit `object_id_length=` always
takes precedence over cached data.

### Reading only

A tool that only reads can show that it does with a `ReadOnlyGit`, which takes the same
arguments as `Git` and offers the library's fixed read operations: `resolve`, `var`, `prefix()`,
`cdup()` and `object_id_length()`, and the functions that take one: `refs`, `config`,
`config_values`, `color_enabled`, `git_paths`, `git_paths_and_head`, `git_dir`, `stripspace`,
`git_common_dir`, `commits`, `count`, `any_commit`, `reflogs`, `changed_paths`, `line_changes`,
`tree_entries`, `status`, `worktrees` and `CatFile`. It has no public way to run a command of
the caller's: no `run`, `run_bytes`, `ok`, `succeeds`, `passthrough`, `process` or `popen`,
which are `Git`'s. So it can't be asked to write: the library doesn't try to tell from a command
line whether git would write (that's git's to decide, and it has no read-only mode to ask), it
just doesn't take one. `Git` extends it, so a function that only reads takes a `ReadOnlyGit` and
is given either; a type checker flags a call of `run` on one. A tool that reads with other
commands (`log`, `diff`, ...) runs them with a `Git`. `patch_ids` runs the commands it's given,
so it takes a `Git`, and raises `TypeError` for a `ReadOnlyGit`.

```python
reader = ReadOnlyGit("path/to/repo", env_var="MY_TOOL_GIT")
reader.resolve("HEAD")
refs(reader, "refs/heads/")
with CatFile(reader) as cat_file:
    cat_file.resolve("main@{upstream}")
```

A tool with a `Git` for the commands that write gives the paths that only read its
`read_only()`: a `ReadOnlyGit` (not a `Git`) with the client's settings as they are then,
which shares what git told the client about where things are (`prefix()`, `cdup()`,
`object_id_length()`, `git_paths`, `git_dir` and `git_common_dir`; see "Caching"). A `Git` subclass that overrides `process` and
`popen` (e.g. to time each) sees every process the client starts, the library's reads' too; but `read_only()` gives a plain
`ReadOnlyGit`, not of the client's class, whose processes it doesn't see: a tool that times
every process gives its reads the `Git` itself.

```python
git = Git("path/to/repo")
refs(git.read_only(), "refs/heads/")
```

A `ReadOnlyGit`'s git gets `GIT_NO_LAZY_FETCH=1`, so in a partial clone a missing object isn't
fetched (git before 2.45 ignores it, but for the security releases 2.39.4 to 2.44.1):
`resolve` and `CatFile` take it as a name that names nothing (None, and `verify` and `read`
raise `UnknownRevisionError`). It also gets `-c core.fsmonitor=false` before each command but
`config --list`, so no `core.fsmonitor` hook runs as git reads the index for a name such as `:path`.
They keep its reads from writing; they aren't a promise that git writes nothing: the tool's
environment reaches git as it is (e.g. `GIT_TRACE*`, which write where they name).

#### Native object reads

Install the `pygit2` extra (`pip install 'py-git-client[pygit2]'`) and opt in with
`ReadOnlyGit(directory, native=True)` or `git.read_only(native=True)`.
`resolve` and `CatFile` can then use libgit2 for full SHA-1 object IDs, including
typed peeling such as `<id>^{tree}`. The reader opens the repository afresh for each
native read. Once a `CatFile` starts a subprocess, it uses that process until it
closes or exits. For bulk lookups, a subprocess-backed `CatFile` reuses its
repository setup across requests.

Named revisions, config, refs, repository paths, requests with a timeout, and
repositories with shallow history or partial clone metadata use Git subprocesses.
Git also handles replacement refs and explicit overrides to repository or config
discovery. The reader falls back to Git if pygit2 is unavailable or can't answer a
request. Native reads validate the configuration libgit2 needs to read an object.
Use a subprocess-backed client when you need Git's diagnostics for invalid config
values. `Git` uses subprocesses for every operation.
`git_calls` sees fallback subprocesses and all operations that start Git.

### Performance recording

Give a `Git` or `ReadOnlyGit` a `PerformanceRecorder` to collect a timeline in memory.
Share it with other clients to include work across repositories or threads. `read_only()`
carries the recorder forward.

```python
from git_client import Git, PerformanceRecorder

recording = PerformanceRecorder()
git = Git("path/to/repo", performance=recording)
reader = git.read_only()
reader.resolve("HEAD")
git.run("status", "--short")

for event in recording.events:
    if event.end is not None:
        print(event.kind, event.name, event.end - event.start)
```

Each snapshot is ordered by start time. Timestamps are in monotonic seconds on the same
clock as `time.monotonic()`. Events cover three kinds of work:

- `read` records a public typed read that consults Git or pygit2. Cache hits and
  other answers computed entirely in memory produce no events. A read that invokes Git also
  produces separate subprocess or CatFile work events.
- `process` records a Git launch with its command arguments and PID. A failed launch has
  no PID. The end time and return code are filled in when the client observes its exit,
  such as by waiting or polling. Subprocess input, output and environment data are excluded.
- `process_work` records CatFile's request, content transfer and shutdown intervals. Use
  these intervals for CatFile wall time, since its persistent process can wait idle between
  requests.

Count process events with a PID to count Git launches. When totaling elapsed time, take the
union of intervals so overlapping processes or a read and its subprocess count the shared
time once. Copies of a client within a process share its recorder. Unpickling one starts
with an empty recorder.

### Errors

`run` and `run_bytes` raise a `GitError` when git fails, with its `command`, `returncode`,
`stderr` and `stdout`. For the failures a tool may want to handle, the library reads what git
said and raises a subclass:

| Error | When |
| --- | --- |
| `NotARepositoryError` | There's no repository at the client's directory, or no such directory. |
| `UnknownRevisionError` | A revision (a commit, ref or object id) names nothing (from `CatFile`, its `reason` is git's, or "" if it gave none). |
| `WrongTypeError` | A name names an object of the wrong type, e.g. a tree where git wants a commit (its `expected` and `found` types). |
| `CorruptObjectError` | An object git read is damaged (e.g. a corrupt loose object): not taken for one that isn't there. |
| `ConfigLockedError` | Another process has the config file locked. `Git.write_configs` retries when Git gives `File exists` as the reason. |
| `MissingObjectError` | A partial clone lacks an object git needs, and couldn't fetch it. The promisor remote couldn't be reached or lacks it (its `stderr` says why): where that raised `RemoteError` before, so catch both. From git 2.39: older git doesn't say which object it couldn't fetch, so its failure raises what it says instead (e.g. `RemoteError`). |
| `RemoteError` | A remote can't be reached or read from, or has no such repository. |
| `PushRejectedError` | A push was (or, with `--dry-run`, would be) rejected, e.g. not a fast-forward. |
| `ConflictError` | A rebase, merge or cherry-pick stopped on a conflict. |

Any other failure is a plain `GitError`. `ok` raises them too, for any exit status but 0
(True) and 1 (False, git's no for `merge-base --is-ancestor`, `diff --quiet` and the like).
`succeeds` and `passthrough` say whether git succeeded instead (exit status 0, not any
other). They still raise when git gives no answer: `GitTimeoutError` past a timeout,
`NotStartedError` once `stopping` says so, and an `OSError` if git can't be started. Use
`succeeds` where any failure of git's means no, and `ok` where a failure other than git's no
should be reported.

When git gives no answer, every call raises a `NoAnswerError`, which isn't a `GitError`, so
`except GitError` never takes "git didn't answer" for "git said no" (catch
`(GitError, NoAnswerError)` for both):

| Error | When |
| --- | --- |
| `GitTimeoutError` | git ran past its timeout, and was stopped. |
| `NotStartedError` | git wasn't started: the client's `stopping` said so. |

It has a `GitError`'s attributes, its `returncode` None. An error's message starts
`git <command> failed (<status>):`, then its `stderr`. The status is git's exit status or,
without one, `timed out` for a `GitTimeoutError`, `not started` for a `NotStartedError` and
`no exit status` otherwise (e.g. a tool's own `GitError` for a command it refused).

## Testing a tool that uses it

`git_client.testing` is a pytest plugin. Load it in your top `conftest.py`
(`pytest_plugins = ["git_client.testing"]`) or with `-p git_client.testing`:

- **Every test's git gets a config of its own** (automatic; `git_config` is its file): an
  identity, `main` as the first branch, no background maintenance (which would otherwise start
  a process after each commit), and no global ignore or attributes file. What a test writes
  there (`git config --global`) no other test sees. The user's and the system's config don't
  reach it, nor do git's variables that would win over it (e.g. `GIT_DIR`, `GIT_INDEX_FILE`,
  `GIT_AUTHOR_NAME` and `GIT_TEMPLATE_DIR`, which a git hook running the tests would pass on,
  or `GIT_DEFAULT_HASH`, which would make sha256 repositories; see
  `GIT_ENVIRONMENT_VARIABLES`), for the whole session, session fixtures included. git gets
  `GIT_EDITOR=true` instead of the user's editor (`GIT_SEQUENCE_EDITOR` is cleared, so
  `rebase -i` uses it too), `GIT_ALLOW_PROTOCOL=file` (no network) and
  `GIT_TERMINAL_PROMPT=0` (no asking for a password; `GIT_ASKPASS` and `SSH_ASKPASS` are
  cleared, so no program asks either): see `GIT_SETTINGS`. And git looks for a repository no
  higher than a directory made in pytest's temporary directory or Python's
  (`tempfile.gettempdir()`), even if one of them is in a repository (e.g. a `--basetemp` or
  `TMPDIR` in your project), so a test that runs git in a directory that isn't a repository
  gets `NotARepositoryError`, rather than the repository around it. A test that needs
  another protocol widens `GIT_ALLOW_PROTOCOL` in a fixture of its own:

  ```python
  @pytest.fixture
  def ssh_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
      monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh")  # and GIT_SSH_COMMAND, a fake ssh
  ```

  To add config entries, override the session-scoped `git_config_text` fixture:

  ```python
  @pytest.fixture(scope="session")
  def git_config_text() -> str:
      return TEST_GIT_CONFIG + "[commit]\n\tgpgSign = false\n"
  ```
- **Repositories built once per session, copied for each test** (`repo_templates`), under
  the session's config rather than the config of the test that asks first, and without
  git's sample hooks (`hooks/*.sample`), which every copy would copy: building takes many
  git processes, and one more per repository, to read its remotes' URLs (`git config
  --get-regexp`); copying takes none for a repository without remotes. One with remotes
  takes a `git config` for each remote's `url` or `pushurl` that points inside the template
  (a path, or a `file://` URL), to point it at the copy's (for a key with several values,
  one to clear it and one per value). Submodules and linked worktrees aren't repointed, nor a
  repository git can't read (e.g. a bad config line), which is copied as it is. A copy's
  index keeps the template's stat data: the first command that refreshes the index (`git
  status`, `git diff`) makes it match the copy's files, but until then plumbing that doesn't
  (`git diff-index HEAD`, `git diff-files`) reports every file as modified, so a test of a
  tool that uses it runs `git update-index -q --refresh` first.

  ```python
  def make_origin_and_clone(directory: Path) -> None:
      # e.g. git init --bare origin.git, git clone origin.git clone, a first commit
      ...


  @pytest.fixture
  def clone(repo_templates: RepoTemplates, tmp_path: Path) -> Git:
      repo_templates.build("origin-and-clone", make_origin_and_clone)
      return Git(repo_templates.copy("origin-and-clone", tmp_path / "repos") / "clone")
  ```
- **`real_git()`**: git itself, from `git --exec-path`, rather than a wrapper or launcher that
  starts another process for each. The plugin runs it for its own git (repointing a copy's
  remotes), but your tool runs `find_git`'s: point your tool's variable at it in your top
  `conftest.py`, at import, before anything calls `find_git`, which caches its answer:

  ```python
  os.environ["MY_TOOL_GIT"] = real_git()
  ```
- **`git_calls`**: each git process any client (a `Git`, a `ReadOnlyGit`, or a subclass of
  either) starts during the test, while real git runs, to count a tool's processes or check
  the ones it starts. It leaves out the plugin's own, building and copying templates, whatever
  order the fixtures come in. Each is a `GitCall`: git's arguments as the client was given
  them, as a list (equal to a plain list of them), and in `program` the git it ran, e.g. to
  check that your tool's variable is honored:

  ```python
  def test_status_takes_one_process(clone: Git, git_calls: list[GitCall]) -> None:
      status(clone)  # your code
      assert git_calls == [["status", "--porcelain=v2"]]


  def test_status_runs_my_tools_git(clone: Git, git_calls: list[GitCall]) -> None:
      status(Git(clone.directory, env_var="MY_TOOL_GIT"))  # as your tool makes its client
      assert git_calls[0].program == os.environ["MY_TOOL_GIT"]
  ```
- **`outside_any_repository`**: runs a test from an empty temporary directory, where git
  finds no repository, so a git run without a directory (a `Git()` your code forgot to give
  one) fails rather than change the repository the tests were started in. To run every test
  that way, ask for it from an autouse fixture in your top `conftest.py`:

  ```python
  @pytest.fixture(autouse=True)
  def _outside_any_repository(outside_any_repository: Path) -> None:
      pass
  ```
- **`fresh_find_git`**: `find_git` caches its answer for the whole process; a test that
  changes what it would find (PATH, or your tool's variable) asks for this fixture, which
  clears the cache before and after it.
- **`FakeGit`**, for unit tests of the git commands your code runs, without a process: give it
  responses, run your code with it, and check what it ran. Assert the commands where they're
  the behavior (what's pushed, deleted or rewritten); otherwise give realistic output and
  assert the result. Leave what a command does end to end to tests on real git.

  ```python
  git = FakeGit()
  git.respond(["rev-parse", "--abbrev-ref", "HEAD"], stdout="main\n")
  git.respond(["push"])
  publish(git)  # your code
  assert git.commands[-1] == ["push", "origin", "main"]
  ```

  For code that only reads, give it the fake's `read_only()`: a `ReadOnlyGit` that answers
  from the fake's responses and records in its `calls`, and offers no public way to run a
  command of the caller's. As with a real `Git`, your subclass's own `process` (below) doesn't
  see its reads.

  If your code takes your own subclass of `Git`, fake it with a class that puts yours first,
  so `FakeGit` stands where `Git` was beneath it: your methods, and your own `process` (which
  calls `super().process`), run over its fake processes. Your constructor's arguments reach
  `FakeGit`'s, which are `Git`'s, but it runs `program`, or `"git"` if that's `None`,
  without looking for one (it ignores `env_var`):

  ```python
  class MyGit(Git):  # your tool's
      def output(self, *args: str) -> str:
          return self.run(*args)


  class FakeMyGit(MyGit, FakeGit):
      pass


  git = FakeMyGit()
  git.respond(["rev-parse"], stdout="main\n")
  assert git.output("rev-parse", "--abbrev-ref", "HEAD") == "main"
  ```

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the repository,
then run `dev.py install`:

```sh
git clone https://github.com/benekastah/py-git-client
cd py-git-client
python3 dev.py install
```

It sets up `.venv` with the package (editable) and the dev tools, and makes the clone use the
git hooks in `git_hooks/`. On Windows, run `dev.py` with `py` (`py dev.py install`).

It won't install a clone in a temporary directory (the system's, `/tmp` or `/var/tmp`): there,
run `python3 dev.py venv` and `git config core.hooksPath git_hooks` instead.

`dev.py` runs the same way on Linux, macOS and Windows:

```sh
python3 dev.py check       # every check: static, then tests (even if static failed)
python3 dev.py static      # Ruff's formatter and linter, and basedpyright in strict mode
python3 dev.py tests       # the tests, in parallel (arguments go to pytest, e.g. -k name)
python3 dev.py coverage    # the tests under coverage.py, and the lines they don't run
```

If the formatting check fails, `.venv/bin/ruff format` formats the code;
`.venv/bin/ruff check --fix` fixes what it can of the linter's findings. The pre-commit hook
does both for the files you commit, and the pre-push hook runs the static checks
(`dev.py static`) before every push.

Dependencies are locked in `uv.lock`. `dev.py` installs from it with uv, and updates it when
`pyproject.toml`'s dependencies change, and says so when it does; commit both. Without uv,
`dev.py` installs with pip, which doesn't read the lock.

### Releasing

Versions are 0.0.N. `main` always carries the next version to release, in `pyproject.toml`;
PRs never change it themselves. Release whenever a change should reach git-stack,
git-recommit or move-forward (a change that could break one is simply released as the next
patch version; see PROJECT_AGENTS.md). Once CI has passed on `main`'s latest commit, release its
version:

```sh
python3 dev.py release 0.0.1
```

It needs git, uv and gh (logged in). Every one of origin's fetch and push URLs, after git's
URL rewriting (`insteadOf`, `pushInsteadOf`), must name the same github.com repository. After fetching
origin, it refuses unless the version is `pyproject.toml`'s on `origin/main`, neither its tag nor the
next version's branch (`start-X.Y.Z+1`) exists yet, locally or on origin, the version's
section of CHANGELOG.md on `origin/main` is dated and not empty, and every GitHub
Actions run of the "CI passed" check succeeded on `origin/main`'s latest commit (if one is
still running, wait and run it again). Then it tags that commit (not your checkout's)
`vX.Y.Z`, pushes the tag, publishes it as a GitHub release whose notes are that
CHANGELOG.md section, and, in a temporary worktree, opens a PR moving `main` to the next
patch version (`start-X.Y.Z+1`). That PR merges like any other, once CI passes. If anything
stops it after the tag is pushed, the command says how to finish by hand.

So before releasing, date the version's section of CHANGELOG.md (`## X.Y.Z (unreleased)`)
in a PR of its own; and add the next version's section to the PR the command opens.

### CI and dependencies

CI (`.github/workflows/ci.yml`) runs the static checks once, on Linux, and the tests on Linux,
macOS and Windows, with the oldest and newest Python py-git-client supports, for every pull
request into `main` and every push to it (a stacked pull request gets CI once it targets
`main` and is pushed), along with this project's own jobs, in
`.github/workflows/project.yml`: the tests on Linux with git 2.36, the oldest supported, which
it builds once and caches. Each job has a time limit of at most 5 minutes: on a system
where the tests take longer, they run in several jobs (shards), as many as the Copier answers'
`test_shards_linux`, `test_shards_macos` or `test_shards_windows` say (AGENTS.md says how to
change one). It installs from `uv.lock` and fails if it's out of date. A coverage job puts the
lines and branches the tests don't run in its summary; set `fail_under` in
`[tool.coverage.report]` to make CI fail below a total. Dependabot proposes new versions of the
dependencies and of the actions the workflows and the setup action use, weekly, each a week
after its release.

Coverage runs only on Linux, so code that only runs on macOS or Windows counts as missed there.
Once `fail_under` is set, mark such a branch `# pragma: no cover`, and give it a test that runs on
its own system.

### Updating from the template

This project was made from [python-template](https://github.com/benekastah/python-template)
with [Copier](https://copier.readthedocs.io/). To bring in the template's later changes:

```sh
uvx copier update --defaults
```

It merges them with your changes, marking conflicts as git does. It never changes this
README, PROJECT_AGENTS.md, `docs/issues.md`, `docs/roadmap.md` or
`.github/workflows/project.yml` once written (though it writes one again if it's gone):
they're this project's own. `dev.py`, AGENTS.md, CLAUDE.md, `.github/workflows/ci.yml`,
`.github/workflows/cancel.yml` and `.github/actions/setup` are the template's: add commands
and install steps in `dev_project.py` instead (see `dev.py`'s docstring), this project's own
rules for agents in PROJECT_AGENTS.md, and CI jobs in `project.yml`. A Dependabot bump to an
action in `ci.yml` or `.github/actions/setup` can conflict with the template's: keep the
newer version.
