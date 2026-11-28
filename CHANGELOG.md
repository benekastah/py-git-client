# Changelog

What each version of py-git-client changes for a tool that uses it. Versions are released
as git tags (`vX.Y.Z`), each with a GitHub release of its section; see the README's
"Releasing". The version `main` carries, not yet released, says "(unreleased)".

## 0.0.3 (unreleased)

### What a tool may need to change

- `refs()` no longer takes `fields` (raw `--format` names), and `Ref` has no `fields`: a tool
  asks for the typed attributes instead. move-forward's `push_target`
  (`src/move_forward/plugins/git/client.py:289`, `push:remotename`, `upstream:remotename` and
  `upstream:remoteref`) passes `remotes=True` and reads `push_remote`, `upstream_remote` and
  `upstream_remote_ref`. A tool (or test) that built a `Ref` with `fields` as its sixth
  argument drops it.
- All Git subprocesses close extra inheritable file descriptors or handles. Remove the
  `close_fds` keyword from calls to `Git`, `ReadOnlyGit` and `FakeGit`, and remove imports
  of `CLOSE_FDS`.
- A `ReadOnlyGit` offers only the library's reads, each a command the library fixes:
  `resolve`, `var`, `prefix()`, `cdup()` and `object_id_length()`, and `refs`, `config`,
  `git_paths`, `git_paths_and_head`, `git_dir`, `git_common_dir` and `CatFile`, which take one.
  `run`, `run_bytes`, `ok`, `succeeds`, `passthrough`, `process`, `popen`, `command` and
  `environment` are `Git`'s alone: a tool that ran a command through a `ReadOnlyGit` (e.g.
  `reader.run("log", ...)`) runs it through a `Git`. A type checker flags each such call (#70).
- `ReadOnlyError` is gone, as nothing is refused: a tool drops its `except ReadOnlyError`. A
  `ReadOnlyGit` takes any `env`, as a `Git` does (#70).
- A tool (or its tests) that patched `ReadOnlyGit.process` or `popen` to count or time git's
  processes, as git-stack's tests do, patches `Git.process` and `Git.popen`, or in tests uses
  `git_calls`, which sees every client's. Every process of a `Git` starts through them, the
  library's reads' too (`config`, `refs`, `git_paths`, `prefix()`, `object_id_length()`, ...),
  which went through `run` or `run_bytes`: a subclass that overrode those to see the reads
  overrides `process` and `popen` instead. A `ReadOnlyGit`'s processes (a `read_only()`'s too)
  go through neither (#70).
- `patch_ids` takes a `Git`, not a `ReadOnlyGit`, as it runs whatever commands it's given.
  Given a `ReadOnlyGit` (e.g. a `Git`'s `read_only()`), it raises `TypeError`, running nothing
  (#68).
- `patch_ids` gives every command `-M -U3 --inter-hunk-context=0 --diff-algorithm=myers
  --submodule=short`, and `log` and `show` `--root`, so `diff.renames`, `diff.context`,
  `diff.interHunkContext`, `diff.algorithm`, `diff.submodule` and `log.showRoot` no longer
  change its ids: under such config, its ids become those git gives by default (a commit that
  only moves a submodule, or a root commit, is no longer dropped). A `FakeGit` test that checks
  its exact commands sees the new options (#52, #55).
- `git_common_dir` takes a client, not a directory, and asks git, where it read the
  repository's files: `git_common_dir("path/to/repo")` becomes
  `git_common_dir(Git("path/to/repo"))`. It raises `NotARepositoryError` where it returned
  None, finds a subdirectory's repository, and follows `GIT_DIR`, `GIT_COMMON_DIR` and
  `GIT_WORK_TREE` (the client's `env`'s and `os.environ`'s), as git does: a tool run from a git
  hook (whose environment can name the hook's repository) that asks of another repository
  removes them from `os.environ` first (#65, #69).
- `config_key` is gone. `config()`'s keys are exactly what `git config --list -z` prints:
  section and name in lower case, the subsection as written. So `config(git).get(
  "init.defaultBranch")` finds nothing, with no error: a tool looks up `"init.defaultbranch"`
  (`branch.Topic.pushremote` keeps `Topic`), lower-casing the section and name of each key it
  looks up, or writing its constant keys that way. git-stack (`config_key` in
  `git_stack/git.py`, for `stack.perfLog`, `stack.perfLogMaxBytes` and `core.sshCommand`) and
  move-forward (`RepoConfig.get`, given camelCase keys) must. For a key a user typed, a tool
  runs `git config --get <key>`, which takes it in any case; it exits 1 both for a key that
  isn't set and for one that isn't valid, which it says only on stderr (#67).
- `Git.write_config` is `Git.write_configs`, which takes changes, not `git config`'s
  arguments, and returns a `ConfigResult` for each, not git's output:
  `git.write_config("k", "v")` becomes `git.write_configs(ConfigSet("k", "v"))`, and
  `git.write_config("--unset", "k")` becomes `git.write_configs(ConfigUnset("k"))`, which
  returns `NOT_FOUND` (or `SEVERAL_VALUES`) where git exits 5, rather than raise. Raw
  arguments raise `TypeError`, making no change. Other writes (`--add`, `--rename-section`,
  ...) go through `git.run("config", ...)`, which doesn't try again while the file is locked
  (#61, #64).
- Repository metadata caching is opt-in. On `Git`, `ReadOnlyGit`, and `FakeGit`,
  pass `cache=GitCache()` to reuse answers. Callers pinned to a development commit
  that used `remember_for=` should switch to the same cache argument.
  A cache keeps answers until it is cleared or discarded. Give a long-lived service
  a fresh cache per refresh and share it among clients within that refresh.
  `read_only()` shares the original client's cache. `cache.clear()` or `git.forget()`
  invalidates the answers for every client sharing it.
- The cache covers `prefix()`, `cdup()`, `object_id_length()`, `git_paths`,
  `git_dir`, and `git_common_dir`. An explicit `object_id_length=` still takes
  precedence. HEAD, refs, config, object lookups, and the configured hooks path are read afresh.
- Without a cache, metadata calls ask Git for the requested values without warming
  unrelated metadata. Tests that script `FakeGit` must match the new arguments and
  output. For example, `git_paths(git, "HEAD")` asks only
  `rev-parse --path-format=absolute --git-path HEAD` and expects one path line.
  Pass `cache=GitCache()` in tests that exercise the batched cache warm-up.
- `resolve` (a `ReadOnlyGit`'s too) sends cat-file a name of all zeros first, as `CatFile`
  does: a `FakeGit` answering `cat-file --batch-check` prints
  `0000000000000000000000000000000000000000 missing` as its first line, or `resolve` raises
  `GitError` (#54).
- `GitTimeoutError` and `NotStartedError` are `NoAnswerError`s, no longer `GitError`s, so
  `except GitError` no longer takes "git didn't answer" for "git said no" (e.g. a timed-out
  `rev-parse @{upstream}` for "no upstream"). A tool that catches `GitError` to report any
  failure (e.g. at its top level) catches `(GitError, NoAnswerError)`; one that catches them by
  name needs no change (#54).
- A name of the wrong type (e.g. a tree where git wants a commit) raises `WrongTypeError`, a
  `GitError`, from `run` and the like and from `CatFile.verify` and `read`, where it raised a
  plain `GitError`: `type(error) is GitError` no longer holds for it (#50).
- An object git finds damaged (e.g. a corrupt loose object) raises `CorruptObjectError`, a
  `GitError`: from `CatFile` and `resolve`, where its name resolved to None (or `verify` and
  `read` raised `UnknownRevisionError`), and from `run` and the like, where it raised a plain
  `GitError` or `UnknownRevisionError`. A missing name still resolves to None (#50, #54).
- An object a partial clone lacks and couldn't fetch (its promisor remote unreachable, say)
  raises `MissingObjectError`, a `GitError` (with git 2.39 and newer, which names the object),
  from a `Git`'s `run` and the like, `resolve` and `CatFile`: where `run` raised `RemoteError`,
  `resolve` raised it for a name git reads an object for (e.g. `HEAD:path`, `HEAD^{tree}`) but
  gave None for a bare full id, `CatFile`'s `info` and `resolve` gave None, its `verify` raised
  `UnknownRevisionError` and its `read` a plain `GitError`. A tool catches `MissingObjectError`
  around those calls too (it caught `RemoteError` around `run`), and in a partial clone, its
  `is None` checks of a `Git`'s `resolve` and `CatFile` no longer see None for such a name:
  they get the error (#52, #55).
- For a name cat-file says only "<name> missing" of, `CatFile.verify` and `read` raise an
  `UnknownRevisionError` whose `stderr` is rev-parse's `fatal: bad revision '<name>'` (with
  ": git exited without saying why" where cat-file exited), not "<name> missing": a tool that
  compared `stderr` with `f"{name} missing"` (as git-recommit does) tests `reason == ""` (#53).
- A `CatFile` whose client has a `timeout` holds each request to it: one not answered in time
  (e.g. waiting on a lazy fetch in a partial clone) kills the process and raises
  `GitTimeoutError`, where it waited as long as it took. A tool whose requests may take longer
  gives its CatFile a client with a longer timeout, or none (#53).
- `ObjectInfo` has `warnings`, which its equality includes: `info(name) == ObjectInfo(id,
  type, size)` is False when git warned of the name (e.g. an ambiguous one); compare `id`,
  `type` and `size` (#53).
- A command whose output the library reads (`run`, `run_bytes`, `ok`, `succeeds`,
  `process(capture_output=True)`) reads nothing from stdin unless given `input`, where it read
  the tool's: a tool that had git read its own stdin (e.g. `hash-object --stdin`) passes
  `input=sys.stdin.buffer.read()`, or runs git with `passthrough` (#52).

### Added

- `status(git, *, untracked=, ignored=, ignore_submodules=, renames=, branch=)` reads a
  worktree's status from one `git --no-optional-locks status --porcelain=v2 -z`, which
  doesn't refresh the index, so a `ReadOnlyGit` offers it: typed entries (`Changed`,
  `Renamed`, `Unmerged`, `Untracked`, `Ignored`, with a `SubmoduleState`), and with
  `branch`, HEAD's commit, branch, upstream and ahead/behind counts. `worktrees(git)` lists
  the repository's worktrees from one `git worktree list --porcelain -z` (`Worktree`: path,
  HEAD, branch, bare, detached, and the reasons it's locked or prunable). A tool replaces its
  `git status --porcelain` runs and parsers (emptiness becomes `not status(...).entries`),
  and its `git worktree list --porcelain` runs and parsers.
- `is_ancestor(git, ancestor, descendant)`, `merge_base(git, a, b)`, `merge_bases(git, a, b)`,
  `independent(git, *commits)` and `fork_point(git, upstream, commit)` ask `git merge-base`
  (`--is-ancestor`, plain, `--all`, `--independent`, `--fork-point`) with typed results: a
  bool, an id or None, a list of ids, a list of ids, an id or None. git's no (exit 1) is
  False, None or []; any other failure raises (`UnknownRevisionError` for a name that names
  nothing, `WrongTypeError` for one that names no commit). They replace a tool's
  `ok`/`succeeds("merge-base", "--is-ancestor", ...)` (git-stack `git_stack/git.py:679`,
  `700`, `705`; git-recommit `git_recommit/git.py:141`; move-forward
  `src/move_forward/plugins/git/actions.py:275`), `run("merge-base", ...)` (git-stack
  `core.py:1087`, `stack.py:3915`; git-recommit `revisions.py:35`), `--all` (git-stack
  `core.py:723`), `--independent` (`stack.py:4005`) and `--fork-point` (`stack.py:567`). A
  tool whose wrapper answered False or "" for a name that names nothing (git-recommit's
  `succeeds`; git-stack's `ok`, `git_stack/git.py:306`, which wraps `succeeds`, so its
  `is_ancestor` at `git.py:679` and `700`; and its `check=False`) catches
  `UnknownRevisionError` where it relied on that. `fork_point` gives None for a full id the
  repository lacks, of which git says nothing, and raises for an error git reports reading
  upstream's reflog. git-stack's `core.py:723` and `check_ancestry`, which pass `env=` for
  one call, use a client made with that `env`.
- A failure whose stderr has git's wrong-type message (`error: object <id> is a <type>, not a
  <type>`, or `...: expected <type> type, but the object dereferences to <type> type`) and no
  other message the library knows raises `WrongTypeError` (a `GitError`) where it raised
  `GitError`, e.g. `merge-base --fork-point` of a tree, or `branch <name> <tree>`.
- `commits(git, revisions, *, max_count=None, reverse=False, topo_order=False, messages=False)`
  lists the commits a typed `Revisions` describes (includes, excludes, `merges`,
  `first_parent`, `cherry_pick`, `since`, `author`, `paths`), each a `Commit` (id, parents,
  author and committer `Signature`s with aware dates, subject, and the whole message if asked;
  a tool handles a `date` of None, for a commit that records no date or one a datetime can't
  hold), from one `git rev-list` given the revisions on stdin. `count` and `any_commit` ask the
  same with `--count` and `--max-count=1`. A tool replaces its raw `rev-list` and `log` calls
  that list, count or test ranges: e.g. `git.run("rev-list", "--max-count=1", "--merges",
  f"{base}..{tip}")` becomes `any_commit(git, Revisions(tip, exclude=base, merges=True))`,
  `--format=%H%x00%an...` with `--date=raw` becomes the `Commit`'s fields, and `--cherry-pick
  --right-only a...b` becomes `Revisions(b, exclude=a, cherry_pick=True)`. `author` matches
  the author the commit records, with no mailmap, where `git log --author` reads one by default
  (log.mailmap); and the top of the worktree is the path `""`: `"."` matches nothing, so
  move-forward's derive.py, whose `PurePosixPath(path).parent` gives `"."` for top-level
  files, passes `""` for them.
- `refs()` takes for-each-ref's filters, `contains`, `no_contains`, `merged`, `no_merged` and
  `points_at`, and its order, `sort` (`SortKey`s, the first deciding) and `count`; and fills
  in typed `Ref` attributes when asked, in the same one process: `track=True` gives `track`,
  a `Track(ahead, behind, gone)` as git says it (`%(upstream:track)`); `push_track=True`,
  `push_track` (`%(push:track)`) and `push`; `remotes=True`, `upstream_remote`,
  `upstream_remote_ref`, `push` and `push_remote`; `dates=True`, `committer_date`, a
  `datetime`; and `ahead_behind="<base>"`, `ahead_behind`, `(ahead, behind)`, from git 2.41's
  `%(ahead-behind:<base>)` (before 2.41, one `git rev-list --left-right --count` per ref).
  Calls without them run the same `git for-each-ref` as before. This replaces raw
  `for-each-ref` runs in tools: move-forward's branch listing and its `_TRACK_RE`
  (`src/move_forward/plugins/git/client.py:200`, which `track=True, remotes=True, dates=True`
  cover); git-stack's `local_branches` (`git.py:901`), per-branch `%(upstream:track)`
  (`git.py:1003`, `track=True` for all branches at once), `chain_to_restack`'s `--contains
  --merged` (`core.py:809`), `find_parent`'s `--merged --no-merged` (`stack.py:3992`), and
  its `rev-parse --abbrev-ref <b>@{upstream}` (`core.py:1153`, `1275`: `upstream`, which
  `refs()` always reads). An `ahead_behind` base that names nothing raises
  `UnknownRevisionError` on every git. A filter given None filters nothing: a tool whose base
  can be None (git-stack's `chain_to_restack`, `core.py:809`, whose `base` comes from config)
  handles that itself, rather than list every branch.

- `config_values(git, *keys, type=None)` reads keys' values as git reads them as a type
  (`ConfigType`: `git config --type`'s), the last of each, or None where a key isn't set,
  with one `git config --get-regexp` per call, so one per type. It replaces a tool's
  `git.run("config", "--type=bool", "--get", key, check=False)` and the like, which took a
  process per key, in git-recommit (`diff.renames`, `commit.gpgSign`, `commit.cleanup`,
  `core.commentChar`), git-stack (`merge.verifySignatures`, `stack.perfLog`,
  `stack.perfLogMaxBytes`, its GitHub stacks setting) and move-forward
  (`branch.<name>.remote`, `user.email`). Keys are as `config()` gives them: a tool writes
  `"commit.gpgsign"`, not `"commit.gpgSign"`, which finds nothing. A value git can't read as
  the type raises `GitError`, where `check=False` gave "".
- `color_enabled(git, key, *, stdout_is_tty)`: whether to color output, as `git config
  --get-colorbool` decides, replacing git-stack's `git.run("config", "--get-colorbool",
  "color.stack", tty, check=False)`.
- `push(git, remote, updates, *, atomic=False, set_upstream=False, no_verify=False)` makes
  many `PushUpdate`s (a target ref, a source or None to delete, per-ref `force` and `lease`)
  with one `git push`, and reports each destination and ref's status, including partial
  success and full old and new object IDs for ref updates when Git reports them. A
  destination whose push failed outright (e.g. an unpacker error) is reported with `done`
  False; a nonzero `returncode` while every listed destination is done and accepted means
  one failed before git reported any ref (e.g. a pre-push hook refused it), as `stderr`
  says. A `PushUpdate` whose source starts with `+` or is `""` raises ValueError: only
  `force` forces, and only None deletes.

- Optional native object reads through the `pygit2` extra and
  `ReadOnlyGit(..., native=True)` or `Git.read_only(native=True)`. Full SHA-1
  object IDs can be resolved and read in-process. `Git` uses subprocesses when
  it needs to consult Git.
- Pass `performance=PerformanceRecorder()` to a client to collect in-memory events
  with monotonic start and end times. Share a recorder across clients or threads.
  `read_only()` carries it forward, and `recorder.events` gives an immutable snapshot
  in start order. A typed read that consults Git or pygit2 records a `read` event.
  A fully cached answer records nothing.
- A `process` event records a Git launch, with a PID for a successful launch and
  `end=None` until the exit is observed. A native pygit2 read has no subprocess
  event. CatFile records active startup, exchanges, and shutdown as `process_work`
  intervals. Use those intervals for CatFile wall time so idle gaps in the persistent
  process's lifetime stay out of the total. Take the union of overlapping intervals
  when combining read and subprocess times.
- `ReadOnlyGit.cdup()`, as `git rev-parse --show-cdup` gives it, asked with `prefix()` (#67).
- `git_dir`, the client's own git directory (a linked worktree's own, not the main one), asked
  with `git_common_dir` (#50, #65, #69).
- `git_paths_and_head`: `git_paths`, and what HEAD points at (`Head`: a ref's full name,
  detached, or unknown, as for an unborn branch), from one `git rev-parse` (#58).
- `merge_trees` and `MergeResult`: many pairs of commits (full ids) merged as `git merge-tree
  --write-tree` merges each (git 2.38 or newer, else `GitError`), with one `git merge-tree
  --stdin` from 2.39 (on 2.38, one process each): whether each is clean, its tree and its
  conflicted paths, or why git couldn't merge that pair. It takes a `Git`, as merge-tree
  writes objects (#59).
- `Git.write_configs`'s changes: `ConfigSet`, `ConfigUnset` (with a `value`, only that value,
  as `--unset --fixed-value`) and their union `ConfigChange`, and `ConfigResult` (`CHANGED`,
  `NOT_FOUND`, `SEVERAL_VALUES`) (#61, #64).
- `Git.read_only()`: a `ReadOnlyGit` (not of the client's class, so a subclass's `process`
  doesn't see its processes) with the client's settings, sharing what git told the client of
  where things are; a `ReadOnlyGit`'s gives itself (#55, #70).
- `FakeGit().read_only()`: a `ReadOnlyGit` that answers from the fake's responses and records
  in its `calls` (through `FakeGit`'s own `process`, not a faked subclass's) (#55, #70).
- `object_id_length=` (40 or 64) on `Git`, `ReadOnlyGit` and `FakeGit`: a tool that knows the
  length gives it, and `object_id_length()` starts no git (#50).
- `process_groups=False` on `Git`, `ReadOnlyGit` and `FakeGit`: git never gets a process group
  of its own, so a command with a timeout starts faster without a terminal, but what git
  started isn't stopped with it (#52).
- `hash_blob_at(..., paths_from_top=True)`, for a path from the top of the worktree, as git
  reports one, from a client in a subdirectory (going up by `cdup()`), as `patch_ids` has
  (#50, #67).
- `NoAnswerError`, the base of `GitTimeoutError` and `NotStartedError`: git gave no answer.
  It isn't a `GitError` (#54).
- `WrongTypeError`, a `GitError` whose `expected` and `found` say which type git wanted and
  which it found (#50).
- `CorruptObjectError`, a `GitError` for an object git found damaged as it read it (#50).
- `MissingObjectError`, a `GitError` for an object a partial clone lacks and couldn't fetch,
  from git 2.39 (#52, #55).
- `UnknownRevisionError.reason`: from `CatFile`, why the name names nothing, as git said it
  (without its warnings), or "" when it gave no reason (as for most names); from other
  commands, their `stderr` (#53).
- `ObjectInfo.warnings`, from `CatFile.info`: what git warned of the name, e.g. "warning:
  refname 'main' is ambiguous." (#53).
- `ObjectType`, the types `CatFile.read` takes, exported from `git_client` (#50).

### Changed

- A `ReadOnlyGit`'s reads but `config()` run with `-c core.fsmonitor=false`, so no
  `core.fsmonitor` hook runs as git reads the index for a name such as `:path` (in `resolve`
  and `CatFile`); `git_calls` records the command without it (#55, #70).
- The subprocess path of `resolve` takes one process per lookup, where a name cat-file
  exits on (e.g. `main@{upstream}` with no upstream) took a second (#54).
- With a shared cache, threads that ask `object_id_length()` at the same time
  can reuse one Git process's answer (#54).
- Starting git costs less, a command with a timeout most: the environment built for git is
  kept until `os.environ`, the client's `env` or the command's changes, and whether the tool
  has a terminal is asked once per process and session (#52).

### Fixed

- Config writes recognize a busy lock on paths containing newlines, including with Git's
  performance or Trace2 logging enabled. Retries depend on the reason after the path, which
  can contain text shaped like an exit trace line. Bare performance traces and aliases that
  forward the config arguments also preserve the lock error type, including when Git logs
  fsync or Windows memory counters at exit.

- Cache keys preserve `..` after symlinks in repository paths, so a lookup through
  `link/..` cannot reuse the answer for its lexically normalized path.
- `repo_templates` copies repoint every remote URL as git reads it (`git config
  --get-regexp`, once per repository as the template is built), where the plugin's own
  reading of the config file missed an entry on its section's line and misread a value
  continued on the next line. A repository git can't read is copied as it is (#67).
- A `CatFile` request interrupted once sent (e.g. by Ctrl-C's KeyboardInterrupt) left its
  answer unread, to answer the next request: `resolve("HEAD")` could give another name's id.
  The next request now kills that process, and starts another (#53).
- `CatFile.close()`, and a CatFile's stopping when it's no longer used, could wait forever for
  a process that doesn't exit when its stdin closes (e.g. one waiting on a lazy fetch). It now
  closes the process's stdout too, and kills it if it hasn't exited within a second (#53).
- On Windows, an interrupt (e.g. Ctrl-C) gives git a quarter of a second to handle the Ctrl-C
  it got too, and remove its lock files (e.g. index.lock), before git and what it started are
  killed; a timeout still kills them at once (#52).
- A second interrupt while git cleans up after the first (e.g. Ctrl-C twice) kills git (and
  its group or tree) at once, where it skipped the kill and left them running (#52).

## 0.0.2 (2026-11-17)

### What a tool may need to change

- Python 3.15 or newer is required, where 0.0.1 took 3.11: a tool's own `requires-python`
  must be `>=3.15` for uv to resolve this version.
- `Arg`, `EnvValue` and `Timeout` are `type` aliases: annotations take them as before, but
  they aren't the unions at runtime (no `isinstance(x, Arg)`; `Arg.__value__` is the union).
- A `Git` subclass that overrides `run` takes its new `strip` keyword (#21), and one that
  overrides `prefix` takes `timeout` (#23); a type checker flags the old signatures.
- `git_calls` gives `GitCall`s (lists of the arguments, with the `program`): an annotation
  of `list[list[str]]` becomes `list[GitCall]` (#26).
- `ConfigLockedError` is raised only while another process holds the lock (git's "File
  exists"). A lock file git can't make for another reason (a missing directory, no
  permission), or a section's rename or removal under a held lock, is a plain `GitError`
  (#34).
- A `Git` subclass that overrides `popen` takes its new `stderr` keyword and passes it on to
  `super().popen`: CatFile reads git's reasons there (#32).
- Captured commands no longer run with `LC_ALL=C`: git's own messages are still English
  (through `GIT_TEXTDOMAINDIR`), but the hooks, filters, gpg and ssh git starts keep the
  user's locale, and from git 2.39 `log --grep`, `-G`, `-i` and `git grep` match in the
  user's `LC_CTYPE`. A tool that overrode `environment` to keep its users' locale can drop
  the override (#28).
- A tool that overrode `process` and `popen` to start no git at shutdown can pass
  `stopping=` instead, and catch `NotStartedError` (#37).
- A tool that keeps a `CatFile` of its own client can make it with `weak=True`, so the two
  make no reference cycle; `CatFile.git` is now read-only (#31).
- Damage git finds only as it reads a name (e.g. a corrupt object) makes that name resolve
  to None in `CatFile`, as a name git exits on does, where a new process's first request
  raised `GitError` (#31).

### Added

- `ReadOnlyGit`, a client that runs only commands that read, refusing any other command,
  option or environment variable (`ReadOnlyError`) before git starts. `Git` extends it, and
  `CatFile`, `config`, `refs` and `patch_ids` take one (#19).
- `run(..., strip=False)`, git's output as it wrote it, and `printable`, for text a tool
  stores or shows (#21).
- `succeeds`, whether git exited 0, where `ok` raises for a failure other than git's no
  (#22).
- `git_common_dir`, read without running git; `git_paths`, many `rev-parse --git-path`s in
  one process; and `ReadOnlyGit.prefix` (#23).
- `patch_ids(..., paths_from_top=True)`, for paths as git reports them (#24).
- Testing: `git_calls` records the program each process ran, and `FakeGit` takes `Git`'s
  keywords, so it can stand in for a tool's own `Git` subclass (#26).
- `Git.write_config(...)`: `git config`, tried again while another process holds the
  config's lock (#30).
- `merge_file`, a three-way merge of contents, and `hash_blob_at`, a blob as `git add`
  would store it at a path, through its filters (#29).
- `stopping=`, a client option (e.g. `event.is_set`): once it says so, no git starts and
  each command raises `NotStartedError` (#37).
- `CatFile(git, weak=True)`, holding its client by a weak reference (#31).
- `CatFile.verify(name)`: the id, or `UnknownRevisionError` with git's reason (e.g. no
  upstream configured), without another process; and `CatFile.read(object_id, type)`, an
  object of that type, peeled as `<id>^{type}` is, else a `GitError` (#32).

### Fixed

- A `GitError` with no exit status says "no exit status", not "timed out", which only a
  `GitTimeoutError` says (#20).
- Testing: tests' git no longer finds a repository above `tempfile`'s temporary directory,
  as it couldn't above pytest's (#25).
- `CatFile` starts no extra cat-file or rev-parse when git exits on a name (`@{u}` with no
  upstream, say), and its process stops when it's no longer used, rather than when the
  garbage collector runs (#31).
- `patch_ids(..., paths_from_top=True)` matches paths case-sensitively, even with
  `GIT_ICASE_PATHSPECS` set (#35).

## 0.0.1 (2026-11-16)

The first release: what git-stack, git-recommit and move-forward share for running git. It
has no dependencies, and needs Python 3.11 or newer and git 2.36 or newer.

### Running git

- `Git(directory, program=, env_var=, env=, timeout=)` runs git as `git -C <directory>`,
  starting it the quickest way each system allows (no working directory, `posix_spawn` where
  Python can use it). `find_git(env_var)` finds git on PATH, or where the tool's own
  variable says, skipping relative PATH entries and, on Windows, refusing a batch file.
- `run` (text, stripped), `run_bytes`, `ok` (exit 0 or git's no, 1), `passthrough` (git's
  output is the user's), `process` and `popen` (for a process that answers requests);
  `resolve` (an object id, or None) and `var` (git's variable, or None with no identity).
- Text passes to git as UTF-8 and paths as bytes, whatever the locale; output decodes with
  surrogateescape (`decode`), so `encode` gives back git's bytes. Commands whose output the
  library reads run with `LC_ALL=C`. `object_id_length` says whether ids are SHA-1's or
  SHA-256's.
- A timeout per client or per command (`CLIENT_TIMEOUT`, `Timeout`). Past it, or on
  Ctrl-C, git is stopped. On Linux and macOS it's first asked to, so it can remove its lock
  files, and without a terminal a command with a timeout runs in a process group of its own,
  so what git started (hooks, ssh) stops with it; on Windows, git and what it started are
  killed (`taskkill /T`). `GitTimeoutError` says it timed out.
- Every git process starts in `process` or `popen`, which a subclass can override.

### Errors

- `GitError`, with the command, exit status, stderr and stdout, and a subclass for each
  failure a tool may handle, read from git's message: `NotARepositoryError`,
  `UnknownRevisionError`, `ConfigLockedError`, `RemoteError`, `PushRejectedError` and
  `ConflictError`.

### Many answers, few processes

- `CatFile`: one long-lived `git cat-file --batch-command` for every lookup (`info`,
  `resolve`, `read`), started again if it exits.
- One process each: `config` (every entry, with `config_key` for git's key names), `refs`
  (refs with their upstreams, symbolic refs and any `for-each-ref` fields), `patch_ids` (for
  many commands, whatever the user's log and diff config), `hash_blobs`, and `update_refs`
  (one transaction, all or none).

### Testing a tool that runs git

- `git_client.testing`, a pytest plugin: every test's git gets a config of its own and is
  cut off from the user's (no global or system config, no network, no background
  maintenance), and runs the real git (`real_git`).
- `repo_templates`: repositories built once per session and copied for each test, with
  their remotes repointed.
- Fixtures `git_config` and `git_config_text` (the tests' git config), `git_calls` (each
  git process a test starts), `fresh_find_git` and `outside_any_repository`; `FakeGit`, a `Git` that starts no process, for unit tests of
  the commands a tool runs.
