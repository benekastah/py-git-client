# Changelog

What each version of py-git-client changes for a tool that uses it. Versions are released
as git tags (`vX.Y.Z`), each with a GitHub release of its section; see the README's
"Releasing". The version `main` carries, not yet released, says "(unreleased)".

## 0.0.3 (unreleased)

### What a tool may need to change

- `ReadOnlyGit.prefix()` and `git_paths` (except "hooks" and what's under it) ask git once
  per client, directory and `env`, and remember the answer: a tool that cached them itself
  can drop its cache (and its `cdup` is `"../" * prefix().count("/")`). A repository moved
  or replaced under a client that keeps its directory isn't noticed; make a new client.

### Added

- `Git(object_id_length=)` (40 or 64), and `ReadOnlyGit`'s and `FakeGit`'s: a tool that
  already knows the length, e.g. from an id it has read, gives it, and `object_id_length()`
  starts no `git rev-parse --show-object-format` (#50).
- `hash_blob_at(..., paths_from_top=True)`, for a tool whose client runs in a subdirectory
  but holds paths from the top of the worktree, as git reports them, as `patch_ids` has.

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
