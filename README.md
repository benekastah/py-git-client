# py-git-client

Run git from Python with as few processes as possible. Starting a process costs well under a
millisecond on Linux, but several on macOS and tens on Windows, so a tool that runs git many
times a command is only as fast as the number of git processes it starts. py-git-client is
what git-stack, git-recommit and move-forward share for that:

- **One way to start git** (`Git`), the quickest each system allows: by its full path, with
  `-C` rather than a working directory, and, except on Windows (or for a long-lived process),
  without `close_fds`, so Python can use `posix_spawn` (but for a command with a timeout run
  without a terminal, which gets a process group of its own: see below). Text and paths go to
  git as git expects them (see `Git.command`).
- **One long-lived `git cat-file --batch-command`** (`CatFile`) for resolving names and
  reading objects, which keeps answering after git exits on a name (e.g. `@{u}` without an
  upstream).
- **Batched reads and writes**, each one process: every config entry (`config`), refs with
  their upstreams, symbolic refs, HEAD and any other fields (`refs`), patch ids for many commands (`patch_ids`),
  where git keeps many files in the git directory (`git_paths`), the git directory and the
  common one (`git_dir`, `git_common_dir`), blobs as they are (`hash_blobs`), and ref updates
  in one transaction (`update_refs`).
- **Other writes and merges**, each one process: a blob as `git add` would store it at a path,
  through its filters (`hash_blob_at`), and a three-way merge of contents (`merge_file`).
- **Merges of many pairs of commits in one process** (`merge_trees`, git 2.39's `git
  merge-tree --stdin`; on git 2.38, one each): each merge's tree and conflicted paths, or
  why git couldn't merge that pair.

It has no dependencies of its own, and needs Python 3.15 or newer and git 2.36 or newer (for
`git cat-file --batch-command`; 2.38 for `merge_trees`).

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
    config,
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
# asked of git and remembered per client (and directory), as are git_paths, git_dir and
# object_id_length(), all in one git while any is unknown (see "What a client remembers")
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
for ref in refs(git, "refs/heads/", fields=["upstream:track"]).values():
    print(ref.name, ref.object, ref.upstream, ref.head, ref.fields["upstream:track"])

# For a branch's short name, ask for the field "refname:lstrip=2", never "refname:short",
# which comes back as "heads/v1" when a tag is named v1 too; and give git full ref names.
old = refs(git, "refs/heads/old")["refs/heads/old"].object
update_refs(git, [RefUpdate("refs/heads/old", None, old=old)])  # all or none; only if unmoved

# Absolute Paths, where git itself looks: in a linked worktree, HEAD is its own and refs the
# main repository's; config such as core.hooksPath moves hooks.
head_file, hooks = git_paths(git, "HEAD", "hooks")
# The same, and what HEAD points at, from the same one git: Head(branch="refs/heads/main"),
# Head(detached=True), or Head() when git can't say (a branch with no commits yet).
(head_file, hooks), head = git_paths_and_head(git, "HEAD", "hooks")
# The client's git directory (in a linked worktree, its own: its HEAD, index and rebase state),
# and the common one, with the refs, objects and config: asked with prefix() and git_paths, in
# the same git, and remembered per client.
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

### What a client remembers

A client asks git where things are (`prefix()`, `cdup()`, `object_id_length()`, `git_paths`,
`git_dir`, `git_common_dir`) once per directory and `env`, all in one `git rev-parse` while
any is unknown, and remembers the answers, shared with what its `read_only()` gives. By default
they're kept as long as the client, which suits a command that runs and exits, but a
repository moved or replaced under the client isn't noticed. A long-lived tool (an editor's
helper, a daemon such as move-forward) asks git, but remembers it for a while: it keeps a
client per repository, and gives it `remember_for` (seconds, from when git answered), or
calls `forget()` after each request or refresh:

```python
git = Git("path/to/repo", remember_for=5)  # asked again once 5 seconds old
git.prefix()  # one git, which tells the rest too
git.forget()  # every answer, so the next call asks git again (one process)
```

What one `rev-parse` told expires together, and is asked again together. An
`object_id_length=` given to the client never expires. Threads may share a client: each gets
git's answer, and threads that ask at once what isn't known may each start a process (all but
`object_id_length()` alone, whose threads wait for one answer). A
shallow copy of a client (`copy.copy`) shares its answers and `forget()`, as the `ReadOnlyGit` its
`read_only()` gives does; a deep copy, or a pickled client (e.g. sent to a process pool), starts
remembering nothing, with the same `remember_for`. A client can be deep-copied or pickled only if
its `stopping` (and a subclass's attributes) can be: a `threading.Event`'s `is_set` or a lambda
can't, so give a worker process a client of its own.

### Reading only

A tool that only reads can show that it does with a `ReadOnlyGit`, which takes the same
arguments as `Git` and offers the library's fixed read operations:
`resolve`, `var`, `prefix()`, `cdup()` and `object_id_length()`, and the functions that take
one: `refs`, `config`, `git_paths`, `git_paths_and_head`, `git_dir`, `git_common_dir` and
`CatFile`. It has no public way to run a command of the caller's: no `run`, `run_bytes`,
`ok`, `succeeds`, `passthrough`, `process` or `popen`, which are `Git`'s. So it can't be asked to
write: the library doesn't try to tell from a command line whether git would write (that's
git's to decide, and it has no read-only mode to ask), it just doesn't take one. `Git` extends
it, so a function that only reads takes a `ReadOnlyGit` and is given either; a type checker
flags a call of `run` on one. A tool that reads with other commands (`log`, `status`,
`diff`, ...) runs them with a `Git`. `patch_ids` runs the commands it's given, so it takes a
`Git`, and raises `TypeError` for a `ReadOnlyGit`.

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
`object_id_length()`, `git_paths`, `git_dir` and `git_common_dir`; see "What a client
remembers"). A `Git` subclass that overrides `process` and `popen` (e.g. to time each) sees
every process the client starts, the library's reads' too; but `read_only()` gives a plain
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
| `ConfigLockedError` | Another process has the config file locked; a write can be tried again (`Git.write_configs` does). |
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
