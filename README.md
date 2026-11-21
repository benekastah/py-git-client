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
- **Ancestry for many pairs of commits** through one `git cat-file` (`is_ancestor`), as
  `git merge-base --is-ancestor` answers each, reading only the commits between each pair's two.
- **Batched reads and writes**, each one process: every config entry (`config`), refs with
  their upstreams, symbolic refs, HEAD and any other fields (`refs`), patch ids for many commands (`patch_ids`),
  where git keeps many files in the git directory (`git_paths`), blobs as they are
  (`hash_blobs`), and ref updates in one transaction (`update_refs`).
- **Other writes and merges**, each one process: a blob as `git add` would store it at a path,
  through its filters (`hash_blob_at`), and a three-way merge of contents (`merge_file`).
- **Merges of many pairs of commits in one process** (`merge_trees`, git 2.39's `git
  merge-tree --stdin`; on git 2.38, one each): each merge's tree and conflicted paths, or
  why git couldn't merge that pair.
- **No process at all** to find a checkout's common git directory (`git_common_dir`), or its
  own (`git_dir`: a linked worktree's own), read from its `.git` file or directory.

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
    config_key,
    git_common_dir,
    git_dir,
    git_paths,
    git_paths_and_head,
    hash_blob_at,
    hash_blobs,
    is_ancestor,
    merge_file,
    refs,
    update_refs,
)

# The git on PATH (wrappers such as git-ai included), unless $MY_TOOL_GIT names another:
# a tool names its own variable so its users can choose, e.g. git itself, for speed.
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
# asked of git once per client (and directory), as are git_paths and object_id_length()
# (which an id read by refs, resolve or CatFile also tells)
# git config, tried again while locked, for each change; what each did: an unset that removes
# nothing is NOT_FOUND, or SEVERAL_VALUES (for a key with several values: give the one to
# remove), not an error. Other config writes (--add, ...): git.run("config", ...).
git.write_config(ConfigSet("branch.a.base", "main"), ConfigUnset("branch.a.old"))
git.passthrough("rebase", "-i", "main", timeout=None)  # a command's own timeout; past one,
# git is stopped (see below) and GitTimeoutError raised

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
push_remote = config(git).get(config_key("branch.Topic.pushRemote"))  # keys as git names them
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
# For each (ancestor, descendant) pair of full commit ids, whether `merge-base --is-ancestor`
# says yes (a commit is its own ancestor; an id of no commit, or a name: False), through one
# process however many pairs, each walk reading the commits between its pair's two.
branches = refs(git, "refs/heads/")
a, b = branches["refs/heads/a"].object, branches["refs/heads/b"].object
a_under_b, b_under_a = is_ancestor(git, (a, b), (b, a))
with CatFile(git) as cat_file:  # the tool's own, for its other reads too
    # Through it, the call starts no cat-file of its own; with "shallow" and "info/grafts" in
    # a git_paths the tool asked for already, no process at all.
    [a_under_a] = is_ancestor(git, (a, a), cat_file=cat_file)
# The directory with the refs, objects and config of a checkout (its top), or of a git
# directory, without running git: a linked worktree's or a submodule's too. None if it's
# neither (a subdirectory of a checkout included).
common = git_common_dir("path/to/repo")
own = git_dir("path/to/repo")  # its own: a linked worktree's HEAD, index and rebase state

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
helpers, gpg, ssh) runs in it; only a git that those start themselves speaks English too.
git's own pattern matching (`log --grep`, `--author`, `-G`, `-i`, `git grep`) follows the
user's `LC_CTYPE` from git 2.39, and C's before; `-P` patterns are UTF-8 on every version.
`passthrough`, whose output is the user's, keeps the user's language for git's messages as
well.

Past its timeout, or when the tool is interrupted (`KeyboardInterrupt`) while it runs, git is
first asked to stop (on Linux and macOS with SIGINT, as Ctrl-C asks it), so it can remove its
lock files, and killed a quarter of a second later; the call then waits at most a second more
for its output. When the tool has no terminal (CI, a service), a command with a timeout runs
in a process group of its own on Linux and macOS, so what git started (hooks, ssh, credential
helpers) is stopped with it. With a terminal, git stays in its foreground, as from a shell, so
it can ask for a password or open an editor whatever its timeout, and only git is stopped. On
Windows, git and what it started are killed, with no chance to clean up.

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

### Keeping what refs say

A tool that asks about the same few refs over and over can read them once with a
`RefSnapshot`, which keeps what its function read until a command that may change a ref
(HEAD included) or a branch's upstream runs through any `Git` in the process, and reads it
again when next asked:

```python
from git_client import ConfigSet, Git, RefSnapshot, refs

git = Git("path/to/repo")
branches = RefSnapshot(lambda: refs(git, "refs/heads/"))
branches.get()["refs/heads/main"].upstream  # one git for-each-ref...
branches.get()["refs/heads/a"].object  # ...for both
git.write_config(ConfigSet("branch.a.myToolParent", "main"))  # a branch's own setting: kept
git.run("branch", "c")  # may change refs: read again when next asked
reflogs = RefSnapshot(dict[str, list[str]])  # answers found one at a time, forgotten together
known = reflogs.get()
if "a" not in known:
    known["a"] = git.run("log", "-g", "--format=%H", "refs/heads/a").split()
```

Only commands known not to change refs keep it: those a `ReadOnlyGit` runs; those that write
only objects, the index or the worktree (`diff`, `merge-tree`, `hash-object`, `read-tree`,
`update-index`, `apply`, `commit-tree`, ...); `ls-remote`; the reading forms of `branch`
(`--show-current`), `symbolic-ref`, `remote` and `worktree`; and a `git config` that reads,
or writes one key, `branch.<name>.<setting>`, that isn't the branch's `remote`, `merge` or
`pushRemote`. Anything else may, an alias or a command that failed included (with what it
ran, e.g. its hooks), and is counted once it ends; a long-lived process (`popen`), when it
starts. `FakeGit`'s commands are counted too.

It doesn't see changes made any other way: by another process (the user, another tool, a
git the tool starts itself), or by a long-lived process after it starts; nor a textconv's
cache (`refs/notes/textconv/`), which a `Git`'s `log`, `show`, `blame`, `range-diff` or
`status -v` may write (a `ReadOnlyGit`'s don't). So keep one for no
longer than one of the tool's operations (under its lock, if it has one), and make a new one
to read afresh. `get` can be called from several threads; what a read that a change
overlapped gave isn't kept.

### Reading only

A tool that only reads can show that it does with a `ReadOnlyGit`, which takes the same
arguments as `Git`, and runs only commands that read. Anything else raises `ReadOnlyError`
before git starts. `Git` extends it, so a function that only reads can take a `ReadOnlyGit`,
and be given either; `CatFile`, `config`, `refs`, `patch_ids` and `git_paths` do. A parameter typed
`ReadOnlyGit` says what the function asks of git, not that the client can't write: the
promise is the client's, so make a `ReadOnlyGit` to have it.

```python
reader = ReadOnlyGit("path/to/repo", env_var="MY_TOOL_GIT")
reader.run("log", "-1", "--format=%s")
reader.run("status", "--porcelain")  # with GIT_OPTIONAL_LOCKS=0, so it doesn't write the index
refs(reader, "refs/heads/")
try:
    reader.run("commit", "-m", "never")
except ReadOnlyError:
    pass
```

A tool with a `Git` for the commands that write gives the paths that only read its
`read_only()`: a copy of the client, as it is then, that refuses what a `ReadOnlyGit` refuses.
It's of the client's own class, so a subclass's own `process` and other methods still run, and
it shares what git told the client about where things are (`prefix()`, `object_id_length()`,
`git_paths`). It raises `ReadOnlyError` if the client's `env` holds a variable a `ReadOnlyGit`
doesn't take.

```python
git = Git("path/to/repo")
refs(git.read_only(), "refs/heads/")
```

It runs `blame`, `cat-file`, `check-attr`, `check-ignore`, `check-ref-format`, `count-objects`,
`describe`, `diff-files`, `diff-index`, `diff-tree`, `for-each-ref`, `log`, `ls-files`,
`ls-tree`, `merge-base`, `name-rev`, `patch-id`, `rev-list`, `rev-parse`, `shortlog`, `show`,
`show-ref`, `status`, `var`, `config` with an action that reads (`--get`, `--list`, `get`, ...)
or a lone key, after options that say which files it reads and how it gives values (`--global`,
`-f <file>`, `--type=bool`, `--default <value>`, `-z`, ...), `branch --show-current`,
`symbolic-ref` naming one ref (`--quiet`, `--short`), `remote` (`-v`), `remote get-url` and
`worktree list`. It doesn't run `diff`, which writes the index when it compares the worktree
(`diff-index`, `diff-files` and `diff-tree` don't; `patch_ids` runs a diff of two revisions as
`diff-tree -p`), `range-diff`, whose log runs a textconv whatever it's told, `merge-tree`, which
writes the merge's objects (with `--quiet`, from git 2.50, still those of the merge it makes of
several merge bases), nor `ls-remote`, which runs ssh or credential helpers that may store what
they're given. It doesn't take options that write: `--output`, describe's `--dirty` and
`--broken`, `--textconv` and status's `-v` (a textconv's cache), `--ext-diff`, and
`--submodule=diff` (which runs a `git diff` in each submodule, which does all of that). It
checks every argument, after `--` too (which can be another option's value), and `config`'s
action must come first. Of git's options before the command, it takes only those that name no
config, repository or program (e.g. `--no-pager`, not `-c`), and `env` may set only the locale,
`TZ`, and which index and paths git reads (e.g. `GIT_INDEX_FILE`).

What it runs, it runs so that what the repository's config or the tool's environment tells
git to run doesn't write:

- `log`, `show` and `blame` get `--no-textconv`: a diff driver's textconv with
  `cachetextconv` writes its cache (`refs/notes/textconv/<driver>`). They show git's own diff
  of a file a textconv would show otherwise.
- Every command but `config` gets `-c core.fsmonitor=false -c diff.submodule=short`, which
  win over the config git inherits too, so no `core.fsmonitor` hook runs, and
  `diff.submodule` can't ask for a submodule's `git diff`: `log -p` and `show` show a
  submodule's change as its commits' ids, whatever `diff.submodule` (`log` or `diff`) says.
- `GIT_OPTIONAL_LOCKS=0`, so `status` doesn't write the index.
- `GIT_NO_LAZY_FETCH=1`, so in a partial clone a missing object isn't fetched (git before
  2.45 ignores it, but for the security releases 2.39.4 to 2.44.1), and a command that needs
  one fails. The commands that read a blob's content to diff or search it (`log -p`, `show
  <commit>`, `diff-tree -p`, `log --stat`, ...) raise `MissingObjectError`, which a tool can
  catch to try again with a `Git`. Those given a blob's name don't: `show <rev>:<path>` and
  `cat-file -p` raise `UnknownRevisionError`, `cat-file blob` and `cat-file -s` a plain
  `GitError`, and `blame` says the path isn't there (a plain `GitError`). `ls-tree -l`
  succeeds, with `BAD` as the blob's size. And cat-file's batch modes say only that the
  object is missing, as of a name that names nothing: `resolve` and `CatFile` give None for
  it (and `verify` and `read` raise `UnknownRevisionError`).

What git still runs can write: a filter (git-lfs's smudge, run by `cat-file --filters`, or its
clean, which `status` runs on a file whose stat information changed), a merge driver
(`merge.<driver>.driver`, which `log --remerge-diff` runs, as does `log -m` under
`log.diffMerges=remerge`), the program `gpg.program` or `gpg.ssh.program` names (`log
--show-signature`, or `log.showSignature`), and, through `passthrough`, the pager. The rest of
the tool's environment reaches git as it is: `GIT_DIR` (which a `Git` follows too),
`GIT_TRACE*` (which write where they name), `GIT_EXTERNAL_DIFF` (which git runs only for
`--ext-diff`, and in a submodule's `git diff`, both refused), and config from
`GIT_CONFIG_PARAMETERS` or `GIT_CONFIG_COUNT`, which can't get past the above.

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
| `ConfigLockedError` | Another process has the config file locked; a write can be tried again (`Git.write_config` does). |
| `MissingObjectError` | A partial clone lacks an object git needs, and couldn't fetch it. From a `ReadOnlyGit`, which fetches none, try again with a `Git`. From a `Git`, the promisor remote couldn't be reached or lacks it (its `stderr` says why): where that raised `RemoteError` before, so catch both. |
| `RemoteError` | A remote can't be reached or read from, or has no such repository. |
| `PushRejectedError` | A push was (or, with `--dry-run`, would be) rejected, e.g. not a fast-forward. |
| `ConflictError` | A rebase, merge or cherry-pick stopped on a conflict. |

Any other failure is a plain `GitError`. `ok` raises them too, for any exit status but 0
(True) and 1 (False, git's no for `merge-base --is-ancestor`, `diff --quiet` and the like).
`succeeds` and `passthrough` say whether git succeeded instead (exit status 0, not any
other). They still raise when git gives no answer: `GitTimeoutError` past a timeout, an
`OSError` if git can't be started, and a `ReadOnlyGit`'s `ReadOnlyError` for a command it
refuses. Use `succeeds` where any failure of git's means no, and `ok` where a failure other
than git's no should be reported.

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
  git processes, copying takes none for a repository without remotes. One with remotes takes
  a `git config` for each remote's `url` or `pushurl` that points inside the template (a
  path, or a `file://` URL), to point it at the copy's (for a key with several values, one
  to clear it and one per value). Submodules and linked worktrees aren't repointed. A copy's
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
- **`git_calls`**: each git process a `Git` or `ReadOnlyGit` starts during the test, while
  real git runs, to count a tool's processes or check the ones it starts. It leaves out the
  plugin's own, building and copying templates, whatever order the fixtures come in. Each is
  a `GitCall`: git's arguments, as a list (equal to a plain list of them), and in `program`
  the git it ran, e.g. to check that your tool's variable is honored:

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

  For code that only reads, give it the fake's `read_only()`: it shares the fake's responses
  and records in its `calls`, but raises `ReadOnlyError` for what a `ReadOnlyGit` refuses, so
  the test fails if the code writes.

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
`.github/workflows/project.yml`. Each job has a time limit of at most 5 minutes: on a system
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
