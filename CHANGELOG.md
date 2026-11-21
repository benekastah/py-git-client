# Changelog

What each version of py-git-client changes for a tool that uses it. Versions are released
as git tags (`vX.Y.Z`), each with a GitHub release of its section; see the README's
"Releasing". The version `main` carries, not yet released, says "(unreleased)".

## 0.0.3 (unreleased)

### What a tool may need to change

- `git_common_dir(directory)` takes a client (`Git` or `ReadOnlyGit`), not a directory, and
  runs git, where it read the repository's files: `git_common_dir("path/to/repo")` becomes
  `git_common_dir(git)`, from one `git rev-parse --path-format=absolute --git-dir
  --git-common-dir` (a `FakeGit` answering it gives two lines: the git directory, then the
  common one), remembered per client and location, as `git_paths` and `prefix()` are. It
  raises `NotARepositoryError` where it returned None, and gives a subdirectory's repository,
  as git does, where it gave None. As git does, it follows `GIT_DIR`, `GIT_COMMON_DIR` and
  `GIT_WORK_TREE`, from the client's `env` and the process's environment, which it ignored:
  a tool run from a git hook (where git sets `GIT_DIR`) that asks of another repository
  removes them from `os.environ` first (a client's `env` can set a variable, not remove
  one), or gets the hook's repository (#65).
- `config_key` is gone: it redid in Python git's reading of a key (and gave a key that isn't
  one back unchanged, so a lookup found nothing where git says "key does not contain a
  section"). `config()`'s keys are exactly what `git config --list -z` prints: section and
  name in lower case, the subsection as written. A lookup must use that form: one written
  in camelCase, as `config(git).get("init.defaultBranch")`, now finds nothing, with no
  error; write `"init.defaultbranch"` (`branch.Topic.pushremote` keeps `Topic`). So a tool
  that dropped `config_key` lower-cases the section and name of each key (keeping the
  subsection) before it looks it up in `config()`'s result, or writes its constant keys that
  way (e.g. `f"branch.{name}.pushremote"`). Two tools must: git-stack, which calls
  `config_key` in `git_stack/git.py` and looks up `stack.perfLog`, `stack.perfLogMaxBytes`
  and `core.sshCommand`, and move-forward, whose `RepoConfig.get` is given camelCase keys.
  For a key a user typed, a tool runs `git config --get <key>` (one process; `git config
  get` from git 2.46): git takes the key however it's written and canonicalizes it itself.
  It exits 1 both for a key that isn't set and for one that isn't a key (`nosection`, `a.b
  c`), saying why only on stderr (e.g. "key does not contain a section"), so a tool that reads
  exit 1 as "not set" reads a mistyped key as unset: one that must tell them apart checks
  stderr (#67).
- `object_id_length()` is only ever git's answer (`rev-parse --show-object-format`), never
  the length of an id git printed, which in a SHA-256 repository with
  `extensions.compatObjectFormat=sha1` can be a SHA-1 id. The `rev-parse` of `prefix()`,
  `git_paths` and `git_paths_and_head` asks it too while it isn't known, so it usually costs
  no process: a `FakeGit` answering them gives the lines the `cdup()` entry below lists, or
  is made with `object_id_length=40`, which asks no format (#67).
- `ReadOnlyGit.cdup()` gives the path up to the top of the worktree, as `git rev-parse
  --show-cdup` prints it, where a tool built it from the prefix (`"../" *
  prefix().count("/")`, which outside a worktree git knows the top of is wrong). It's asked
  with `prefix()`. The `rev-parse` that `prefix()`, `git_paths` and `git_paths_and_head`
  run starts with what isn't known yet: `--show-prefix --show-cdup --is-inside-work-tree`
  while the prefix isn't, then `--show-object-format` while the object id length isn't. A
  `FakeGit` answering it gives, in this order: the prefix's line (empty at the top), the
  cdup's (none at all where git knows no worktree: a bare repository, a git directory),
  `true` or `false`, then `sha1` or `sha256`; then the paths and HEAD (none where git exits
  1 for an unborn branch, or 0 saying "refname 'HEAD' is ambiguous"). Where a path holds a
  newline, so that the lines can't be told apart, each question is asked alone. One still
  answering the old way (the prefix's line alone, or then the format's) gets too few lines:
  `ValueError: rev-parse printed N lines, too few for M` (#67).
- `Git.write_config` is now `Git.write_configs`, which takes changes, not `git config`'s
  arguments, and returns what each did (a list of `ConfigResult`), not git's output:
  `git.write_config("k", "v")` becomes `git.write_configs(ConfigSet("k", "v"))`, and
  `git.write_config("--unset", k)` becomes `git.write_configs(ConfigUnset(k))`: where git
  failed with exit 5, its result says so, rather than raise, as `NOT_FOUND` for a key that
  isn't set, and `SEVERAL_VALUES` for one with several values (`ConfigUnset(k, v)` removes
  only the value `v`, as `--unset --fixed-value`, and a key with no value whatever `v` says,
  as git does from 2.46.1, where older git crashes). Other `git config` writes (`--add`,
  `--rename-section`, `--worktree`, ...) go through `git.run("config", ...)`, which doesn't
  try again while the file is locked (#61).
- `ReadOnlyGit.prefix()`, `object_id_length()` and `git_paths` (except "hooks" and what's
  under it) ask git once per client, directory and `env`, and remember the answer: a tool
  that cached them itself can drop its cache (and its cdup is `cdup()`'s, remembered with
  the prefix). `git_paths` learns `prefix()` in its own process: a `FakeGit` answering it
  gives the lines #67's `cdup()` entry above lists. A repository moved or replaced under a
  client that keeps its directory isn't noticed; make a new client (#50).
- A name of the wrong type (e.g. a tree where git wants a commit) raises `WrongTypeError`
  where it raised a plain `GitError`, from `run` and the like, and from `CatFile.verify` and
  `CatFile.read`: `except GitError` still catches it, but a test of `type(error) is
  GitError` no longer holds, and a tool that told it apart by git's message can catch
  `WrongTypeError` instead (#50).
- An object git finds damaged as it reads a name (e.g. a corrupt loose object) raises
  `CorruptObjectError` from `CatFile`, where the name resolved to None (or `verify` and
  `read` raised `UnknownRevisionError`): a missing name is still None. `run` and the like
  raise it too, for git's own words of a corrupt object, where they raised a plain
  `GitError` (#50).
- `CatFile.verify` (and `read`) word a name cat-file says only "<name> missing" of as
  rev-parse does, `fatal: bad revision '<name>'`, adding ": git exited without saying why"
  where cat-file exited saying nothing: a tool that compared `stderr` with
  `f"{name} missing"` (as git-recommit does) tests `reason == ""` instead (#53).
- A `CatFile` whose client has a `timeout` holds each request to it: one its process doesn't
  answer in time (e.g. waiting on a lazy fetch in a partial clone) kills the process and
  raises `GitTimeoutError` (a `NoAnswerError`, which `except GitError` doesn't catch), where
  it waited as long as it took. Its `stderr` is what git said of the request, and a note
  names the request. A tool whose requests may take longer gives its CatFile a client with a
  longer timeout, or none (#53).
- `patch_ids` runs a `diff` of two revisions (two arguments before `--`, no options, and no
  `:`, so not blobs) as `git diff-tree -p`, which a `ReadOnlyGit` runs and which writes no
  index: a `FakeGit` answering it responds to `diff-tree`, and a path given as the second
  argument, without `--`, is taken as a revision (#55).
- `patch_ids` runs every command with `-M -U3 --inter-hunk-context=0 --diff-algorithm=myers`, so
  a diff and a log give a change the same id whatever `diff.renames`, `diff.context`,
  `diff.interHunkContext` or `diff.algorithm` say: under such config its ids change to the ones
  git gives by default (#55).
- A `ReadOnlyGit` keeps what the repository's config or the tool's environment tells git to run
  from writing: its `log`, `show` and `blame` run with `--no-textconv` (a textconv whose driver
  has `cachetextconv` wrote its cache), so a file a textconv showed shows as git's own diff;
  every command but `config` runs with `-c core.fsmonitor=false -c diff.submodule=short`, so
  whatever a repository's `diff.submodule` (`log` or `diff`) says, `log -p` and `show` show a
  submodule's change as its commits' ids. It refuses `range-diff`, `--textconv`, `status -v`,
  `--ext-diff` and `--submodule=diff`; a tool that needs them uses a `Git` (#55).
- An object a partial clone lacks and couldn't fetch raises `MissingObjectError` (a
  `GitError`), from `run` and the like, `resolve` and `CatFile`: a `Git` whose promisor
  remote can't be reached raised `RemoteError` from `run`, and a plain `GitError` (or None)
  from `resolve` and `CatFile`. A tool that catches `RemoteError` around a partial clone's
  commands (e.g. a `checkout` that needs a blob) catches `MissingObjectError` too (#55).
  Only with git 2.39 and newer, which says which object it couldn't fetch: older git's
  failure raises `RemoteError` from `run`, as before (#52).

- `object_id_length()`: threads that ask at once wait for one git's answer, where each could
  start its own (#54).
- `Git.resolve` takes one process for every name, where a name `git cat-file` exits on (e.g.
  `main@{upstream}` with no upstream) took a second, a `rev-parse`: it sends cat-file a name
  of all zeros first, as `CatFile` does, so a `FakeGit` answering `cat-file --batch-check`
  prints `0000000000000000000000000000000000000000 missing` as its first line (else resolve
  raises `GitError`, rather than give None). It raises `CorruptObjectError` for a damaged
  object cat-file reads for the name, as `CatFile.resolve` does, where it gave None for a
  loose one (#54).
- `GitTimeoutError` and `NotStartedError` are no longer `GitError`s, but `NoAnswerError`s,
  so `except GitError` no longer takes "git didn't answer" for "git said no" (e.g. a
  timed-out `git rev-parse @{upstream}` read as "no upstream", or a hang as defaults). Their
  attributes and messages are as before. A tool that catches `GitError` to handle git's no
  needs no change, and now lets these through; one that catches it to report any failure
  (e.g. at its top level) catches `(GitError, NoAnswerError)`; and one that caught
  `GitTimeoutError` or `NotStartedError` by name still does (#54).
- A command whose output the library reads (`run`, `run_bytes`, `ok`, `succeeds`,
  `process(capture_output=True)`) reads nothing from stdin, as from /dev/null, unless given
  `input`: it read the tool's, so `run("shortlog", "-s")` waited on it. A tool that has git
  read its own stdin (e.g. `cat file | tool` running `hash-object -w --stdin` or `apply`)
  now hashes or applies nothing, and must pass it on: `input=sys.stdin.buffer.read()` to
  `run_bytes` (or to `run`, decoded), or run git with `passthrough` (#52).

### Added

- `ReadOnlyGit.cdup()`, as `git rev-parse --show-cdup` gives it, asked with `prefix()` in
  the same process and remembered with it (#67).
- Many config changes in one `Git.write_configs` call, each a `ConfigSet` or `ConfigUnset`
  (`ConfigChange` is their union, for a tool's annotations), with a `ConfigResult` for each:
  `CHANGED`, or, for an unset that removes nothing, `NOT_FOUND` or `SEVERAL_VALUES` (a key
  with several values unset without the one to remove), which raise nothing (#61).
- `MissingObjectError`, for an object a partial clone lacks and couldn't fetch: a tool that
  reads through a `ReadOnlyGit` can catch it and try again with a `Git` (#55). It needs git
  2.39 (and a `ReadOnlyGit` fetches nothing only from 2.45, and in 2.39.4 to 2.44.1) (#52).
- `ReadOnlyGit.read_only()`: a `Git`'s copy that refuses what a `ReadOnlyGit` refuses, of the
  client's own class (a subclass's `process` still runs), sharing what git told the client
  about where things are, for a tool's paths that never write (#55).
- `FakeGit().read_only()` refuses what a `ReadOnlyGit` refuses, while sharing the fake's
  responses and `calls`, so a unit test can check that code stays read-only (#55).
- A `ReadOnlyGit` runs more reads: `config` with options before its action (`--global`,
  `-f <file>`, `--type=bool`, `--default`, `-z`, ...) or with a lone key, `branch
  --show-current`, `symbolic-ref --quiet --short <name>`, `remote` and `remote get-url`
  (#55).
- `Git(process_groups=False)`, and `ReadOnlyGit`'s and `FakeGit`'s: git never gets a
  process group of its own, so a command with a timeout starts faster without a terminal,
  but what git started isn't stopped with it (#52).
- `Git(object_id_length=)` (40 or 64), and `ReadOnlyGit`'s and `FakeGit`'s: a tool that
  knows the length some other way than asking git gives it, and `object_id_length()` starts
  no `git rev-parse --show-object-format` (#50).
- `hash_blob_at(..., paths_from_top=True)`, for a tool whose client runs in a subdirectory
  but holds paths from the top of the worktree, as git reports them, as `patch_ids` has
  (#50).
- `WrongTypeError`, a `GitError` whose `expected` and `found` say which type git wanted and
  which it found (#50).
- `CorruptObjectError`, a `GitError` for an object git found damaged as it read it (#50).
- `ObjectType`, the types `CatFile.read` takes, exported from `git_client` (#50).
- `git_paths_and_head`: `git_paths`, and what HEAD points at (`Head`: a ref's full name,
  detached, or unknown, as for an unborn branch), from the same one `git rev-parse` (#58).
- `merge_trees` and `MergeResult`: many pairs of commits (full ids) merged as `git merge-tree
  --write-tree` merges each, with one `git merge-tree --stdin` (git 2.39; on 2.38, one
  process each): whether each is clean, its tree and its conflicted paths, or why git
  couldn't merge that pair. It takes a `Git`, as merge-tree writes objects (#59).
- `git_dir`, `git_common_dir`'s counterpart: the client's own git directory (a linked
  worktree's own, not the main one) (#50, #65).
- `ObjectInfo.warnings`, from `CatFile.info`: what git warned of the name, e.g. "warning:
  refname 'main' is ambiguous.", so a tool needn't run `rev-parse` to learn it (#53).
- `UnknownRevisionError.reason`: from `CatFile`, why the name names nothing, as git said it
  (without its warnings), or "" when it gave no reason (as for most names); from other
  commands, their `stderr` (#53).

### Fixed

- `repo_templates` copies repoint every remote URL as git reads it, which git now reads
  for them (`git config --get-regexp`, once per repository as the template is built), where
  the plugin's own reading of the config file missed an entry on its section's line and
  misread a value continued on the next line. A copy still runs only the writes. A
  repository git can't read is copied as it is (#67).
- `hash_blob_at(..., paths_from_top=True)` from a client outside the worktree that git is
  told of (e.g. by GIT_WORK_TREE) takes the path from the worktree's top, as `cdup()` says,
  where it took it from the client's directory and so read the wrong attributes (#67).
- A `CatFile` request interrupted once sent (e.g. by Ctrl-C's KeyboardInterrupt) left its
  answer unread, to answer the next request: `resolve("HEAD")` could give another name's
  id. The next request now kills that process, and starts another (#53).
- `CatFile.close()`, and a CatFile's stopping when it's no longer used, could wait forever
  for a process that doesn't exit when its stdin closes (e.g. one waiting on a lazy fetch in
  a partial clone, or writing what nothing reads). It now closes the process's stdout too,
  and kills it if it hasn't exited within a second (#53).
- `patch_ids` keeps a commit that only moves a submodule, and gives a submodule's patches
  the same ids, whatever diff.submodule says, and keeps a root commit's patch whatever
  log.showRoot says (#52).
- On Windows, an interrupt (e.g. Ctrl-C) gives git a quarter of a second to handle the
  Ctrl-C it got too, and remove its lock files (e.g. index.lock), before git and what it
  started are killed; a timeout still kills them at once. What outlives git's own exit runs
  on (#52).
- A second interrupt while git cleans up after the first (e.g. Ctrl-C twice) kills git
  (and its group or tree) at once, where it skipped the kill and left them running (#52).

### Changed

- Starting git costs less, a command with a timeout most: the environment built for git is
  kept until os.environ, the client's `env` or the command's changes, rather than copied
  from os.environ every time, and whether the tool has a terminal is asked once per process
  and session (#52).
- `import git_client` takes about 3 ms less (of about 19): `tempfile` is imported only
  where it's used (#52).

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
