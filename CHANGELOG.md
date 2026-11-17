# Changelog

What each version of py-git-client changes for a tool that uses it. Versions are released
as git tags (`vX.Y.Z`); see the README's "Releasing".

## Unreleased

## 0.0.1 (2026-11-16)

The first release: what git-stack, git-recommit and move-forward share for running git.

### Running git

- `Git(directory, program=, env_var=, env=, timeout=)` runs git as `git -C <directory>`,
  starting it the quickest way each system allows (no working directory, `posix_spawn` where
  Python can use it). `find_git(env_var)` finds git on PATH, or where the tool's own
  variable says, skipping relative PATH entries and, on Windows, refusing a batch file.
- `run` (text, stripped), `run_bytes`, `ok` (exit 0 or git's no, 1), `passthrough` (git's
  output is the user's), `process` and `popen` (for a process that answers requests);
  `resolve` (an object id, or None) and `var` (git's variable, or None with no identity).
- Text passes to git as UTF-8 and paths as bytes, whatever the locale; output decodes with
  surrogateescape, so `encode` gives back git's bytes. Commands whose output the library
  reads run with `LC_ALL=C`.
- A timeout per client or per command (`CLIENT_TIMEOUT`, `Timeout`). Past it, or on
  Ctrl-C, git is asked to stop so it can remove its lock files, then killed; without a
  terminal, a command with a timeout runs in a process group of its own, so what git started
  (hooks, ssh) stops with it. `GitTimeoutError` says it timed out.

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
- Fixtures `git_calls` (each git process a test starts), `fresh_find_git` and
  `outside_any_repository`; `FakeGit`, a `Git` that starts no process, for unit tests of
  the commands a tool runs.
