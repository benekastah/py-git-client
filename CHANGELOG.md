# Changelog

What each version of py-git-client changes for a tool that uses it. Versions are released
as git tags (`vX.Y.Z`), each with a GitHub release of its section; see the README's
"Releasing". The version `main` carries, not yet released, says "(unreleased)".

## 0.0.3 (unreleased)

### What a tool may need to change

- A git that doesn't exit when asked to stop (e.g. one waiting for an editor) is killed after
  5 seconds, not a quarter of a second (see "Fixed"). So, with such a git, a call with a
  timeout raises `GitTimeoutError` up to the timeout plus 6 seconds after it started (it was
  plus 1.25) on Linux and macOS, and an interrupt takes up to 5 seconds longer to raise
  `KeyboardInterrupt`, on every system. A tool with its own deadline or watchdog around a
  call, or tests that time how soon a timeout returns, may need to allow for that.
- `Git.run` (with `strip`, the default) strips only ASCII space, tab, CR and newline from the
  ends of git's output, no longer U+0085, U+2028, U+2029 or other characters `str.strip()`
  takes: a ref name or path printed last keeps such a final character. A tool that relied on
  those being stripped strips them itself.
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

- `write_trees(git, trees, *, missing=False)` writes many trees, each a list of `TreeEntry`s
  (one name each, in any order), with one `git mktree -z --batch`, and `write_commit(git, tree,
  parents, message, *, author=None, committer=None, sign=False)` a commit with one `git
  commit-tree` (git has no form that writes many), its identities typed (`Signature`, its
  date required) and set for that one process, its message bytes (or a str, as UTF-8)
  stored as they are, `sign` giving `-S` (True) or `-S<key>`. Neither runs a hook nor touches
  an index or ref. git-recommit replaces its temporary-index route, `read-tree`, then
  `update-index -z --index-info`, `write-tree` and `commit-tree -F -` with `GIT_AUTHOR_*` for
  each new commit (`git_recommit/session.py:433-447`), with one `write_trees` for each depth of
  the trees of all the new commits and one `write_commit` each, and no `post-index-change`
  hook. It then builds each rebuilt directory's entries, and the nesting, itself, reading the
  base's entries for those directories (one `tree_entries(..., recursive=True)`, or one per
  directory), where `read-tree` read them all into the index. It also replaces `commit-tree`
  of each later commit (`session.py:556`: `author=` the `Signature` `commits()` read, whose
  `raw_date` keeps the date as its `%ad --date=raw` copy does), `mktree` of no entries for the
  empty tree (`revisions.py:60`: `write_trees(git, [[]])`), and `commit-tree -m` of its
  worktree commit (`revisions.py:151`, whose tree still comes from `add` on a copied index,
  `:147-150`): `-m` ends the message with a newline, which `write_commit` doesn't add, so it
  passes `"Uncommitted changes\n"`.
- `Signature.raw_date`: the date as git's `--date=raw` prints it, which `commits()` sets, and
  `write_commit` writes, so a date git prints raw but a datetime can't hold (`date` None,
  e.g. past the year 9999) survives a copy; one git can't read (no raw date) doesn't. `==`
  leaves it out, and it defaults to None, so a tool's own `Signature(name, email, date)` is
  unchanged.
- A tree given where git wants a commit, or a commit where it wants a tree, in `commit-tree`
  (`... is not a valid 'commit' object`) raises `WrongTypeError`, not a plain `GitError`.
- `run_hook(git, name, *args, ignore_missing=True, env=None)` runs a hook with one `git hook run`
  (git 2.36), wherever git finds it, and returns a `HookResult` (exit status, and stderr, which
  holds all the hook printed: git sends its stdout there), raising `GitError` only where git ran no
  hook and said why. git-recommit replaces its `process("hook", "run", "--ignore-missing",
  "commit-msg", "--", path)` (`git_recommit/session.py:495-497`).
- `stripspace(git, text, *, strip_comments=False, comment_char=None)`, one `git stripspace`,
  which a `ReadOnlyGit` offers. git-recommit replaces its `run("-c",
  "core.commentChar=...", "stripspace", ...)` (`git_recommit/editor.py:89`). It still chooses
  the character for core.commentChar=auto in Python (`editor.py:63-66`): stripspace takes
  `auto` as "#", and git has no command that chooses as `git commit` does.
- `add_worktree(git, path, commit, *, detach=False)`, `remove_worktree(git, path, *,
  force=False)` and `prune_worktrees(git)`, `git worktree add`, `remove` and `prune`, one process
  each. git-recommit replaces `worktree add --quiet --detach`, `worktree remove --force` and
  `worktree prune` (`git_recommit/validate.py:35`, `50`, `52`; its `checkout --detach --force`
  in the worktree, `:37`, waits for a typed `switch`), and git-stack `worktree remove [--force]`
  and `worktree add --quiet --detach` (`git_stack/integrations.py:321`, `914`). If the
  post-checkout hook fails, `add_worktree` raises `GitError` though git added the worktree:
  git-stack (`integrations.py:914`) raises and leaves it for its next call to reopen as cached;
  a tool that doesn't want it calls `remove_worktree(..., force=True)`.
- `ref_transaction(git, *, message=None, deref=True, timeout=...)`, for a `with` block: a
  `RefTransaction` over one `git update-ref --stdin -z`, whose `update`, `delete` and `verify`
  changes git checks and locks at `prepare()`, before the tool commits (`commit()`) or aborts
  (`abort()`, or the block's end) them. git-stack replaces its hand-built `start`, changes,
  `prepare`, `commit`/`abort` input (`git_stack/git.py:314`, called from `integrations.py:1017`)
  with it, `deref=False` for its `option no-deref`, and can now decide after `prepare` rather
  than before. A step that runs past `timeout` stops git: on Windows that kills it, leaving a
  prepared transaction's lock files behind, and on any system a commit stopped partway may have
  made only some of its changes (see the README).
- Typed branch writes, each one `git branch` on a `Git`, names after `--end-of-options`:
  `delete_branches(git, names, *, force=False)` deletes many with one `git branch
  --delete`/`-D` (and their config), each git can, and returns `DeletedBranches` (each
  deleted branch's full id, git's exit status, and its reasons for the others), raising only
  for a failure not of one branch; `create_branch(git, name, start_point=None, *, force=False,
  track=None)` (`track`: True, False, "inherit", or None for git's config);
  `rename_branch(git, old, new, *, force=False)`; and `set_upstream(git, branch, upstream)`,
  None to remove it. `valid_branch_name(git, name)` (a read) asks `git check-ref-format
  --branch`, and says no to a name git expands (`@{-1}`). git-stack replaces `branch --quiet
  --delete --force` (`git_stack/core.py:1239`), `branch --quiet --track` (`stack.py:754`,
  `777`, `780`; `track=True`), `--no-track` (`stack.py:1016`; `track=False`), `--move`
  (`stack.py:934`), `--set-upstream-to=` (`stack.py:760`, `3457`), `--unset-upstream`
  (`stack.py:947`), and its two `check-ref-format` runs (`stack.py:1403`) with one
  `valid_branch_name`; move-forward replaces `branch -D`
  (`src/move_forward/plugins/git/actions.py:104`, `127`) and its one `branch -D` per line
  (`actions.py:195`) with one `delete_branches(..., force=True)` per repository, reporting
  each name not in `deleted` as failed, with `stderr`. Unlike `git.run`, `delete_branches`
  doesn't raise when a branch isn't deleted: git-stack's untrack (`core.py:1239`) checks
  `branch in result.deleted` before saving its state. move-forward passes each name once: a
  name given twice makes git delete none of that repository's branches (its loop deleted the
  first and reported the second as failed); and its per-name failure text becomes git's
  `stderr` for all of them, as git's reasons aren't one per name.
- Porcelain writes, one git each, typed: `switch(git, target, *, detach, create,
  start_point, track, force, quiet)`, `reset(git, commit=None, *, mode, quiet)`, `merge(git,
  commit, *, ff, message, edit, quiet)`, `cherry_pick(git, *commits, ff, record_origin,
  allow_empty)`, `revert(git, *commits, edit)`, `commit(git, message=None, *, all, amend,
  allow_empty, no_verify, quiet)`, `abort(git, operation)` and `skip(git, operation)`. Each
  returns a `CommandOutput` (stdout and stderr), raises `ConflictError` for a conflict that
  stopped it and `GitError` for any other failure (a hook's refusal too), and takes
  `inherit_output=True` (and `stdout=`) to leave git's input and output the tool's, as
  `passthrough` does. Hooks run; no editor opens unless `edit=True`, which needs
  `inherit_output`. They replace:
  - git-stack's `run("reset", "--quiet", "--keep", t)` (`git_stack/core.py:1112`) with
    `reset(git, t, mode="keep", quiet=True)`, and `run("merge", "--ff-only", "--quiet",
    new)` (`core.py:1185`) and move-forward's (`plugins/git/actions.py:290`) with
    `merge(git, new, ff="only", quiet=True)`.
  - git-stack's `passthrough("cherry-pick", "--ff", *commits)` (`core.py:1045`) with
    `cherry_pick(git, *commits, ff=True, inherit_output=True, stdout=...)`, and
    `passthrough("revert", "--no-edit", *commits)` (`stack.py:1025`) with `revert(git,
    *commits, inherit_output=True, stdout=...)`: catching `ConflictError`, and on any other
    `GitError` still checking `operation_in_progress()`, as now. Stops that aren't
    conflicts (a commit that's become empty; a pick of several commits stopped partway by
    an untracked file) raise a plain `GitError` with the operation still in progress.
  - The integration worktree's `run("merge", "--no-ff", "--no-edit", "-m", m, tip)`,
    `run("cherry-pick", "-x", "--allow-empty", c)` and `run("cherry-pick", "--skip")`
    (`integrations.py:882, 892, 898`) with `merge(git, tip, ff="no", message=m)`,
    `cherry_pick(git, c, record_origin=True, allow_empty=True)` and `skip(git,
    "cherry-pick")`, a `ConflictError` in place of the `ls-files -u` check; and its
    `ok(command, "--abort")` (`integrations.py:969`) with `abort(git, command)`, in a `try`:
    `abort` raises `GitError` where `ok` returned False.
  - git-stack's `switch` calls (`git.py:904`, `core.py:1237, 1321`, `stack.py:484, 1148`,
    `integrations.py:930, 992`) with `switch(git, ..., quiet=True)` (`detach=True`;
    `create=True, start_point=..., track=False`), `inherit_output=True` for the passthrough
    at `stack.py:1148`. git-stack's `-c core.warnAmbiguousRefs=false` has no typed form: with
    captured output git's warning of a name that's both a branch and a tag is only in
    `stderr`, but at `stack.py:1148`, with `inherit_output`, it reaches the user.
  - git-recommit's `run("reset", "--quiet")` (`git_recommit/session.py:527`) with
    `reset(git, quiet=True)`, and move-forward's `run("commit", "--all", "--quiet", "-m",
    WIP_MESSAGE)` (`plugins/git/actions.py:307`) with `commit(git, WIP_MESSAGE, all=True,
    quiet=True)`.
- `rebase(git, upstream, branch=None, *, onto=None, update_refs=None, stop_at=())`: one
  `git rebase`, typed like the porcelain writes (a `ConflictError` for a conflict stop,
  `inherit_output`). `update_refs` True is `--update-refs` (git 2.38; older git raises
  `GitError`), False `-c rebase.updateRefs=false` (any git), None the config; `upstream`
  None is `--root`. `stop_at` (full ids) makes it interactive (with `-c core.abbrev=no`),
  the library's sequence editor marking those commits "edit". It replaces:
  - git-stack's `passthrough("rebase", "--no-update-refs" | "--update-refs", "--onto",
    parent_tip, base, branch)` (`git_stack/core.py:672, 848`) with `rebase(git, base, branch,
    onto=parent_tip, update_refs=False | True, inherit_output=True, stdout=...)`: catching
    `ConflictError`, and on any other `GitError` still checking `operation_in_progress()`,
    as now (a stop that isn't a conflict, e.g. a commit that's become empty, is a plain
    `GitError` with the rebase still in progress).
  - git-recommit's `passthrough("rebase", "--interactive", "--no-autosquash", ...)` with its
    own `GIT_SEQUENCE_EDITOR` (`git_recommit/edit_commit.py:29`, `sequence_editor.py`) with
    `rebase(git, f"{sha}^" if <sha has a parent> else None, stop_at=[sha],
    inherit_output=True)`. git-recommit keeps its own checks that `sha` is an ancestor of
    HEAD and that no merge follows it: `rebase` flattens merges without a word, as `git
    rebase -i` does.
- `fetch(git, remote, refspecs=(), *, prune=False, tags=None, write_fetch_head=True)` fetches
  with one `git fetch --porcelain`, refspecs typed as `FetchRefspec(source, destination=None,
  *, force=False)`, and returns a `FetchResult` (exit status, output, and each `FetchRef` git
  reported, with its `FetchStatus` and old and new ids; `refs` None before git 2.41, where it
  runs fetch again without `--porcelain`). It fetches no submodules on any git (git takes no
  `--recurse-submodules` with `--porcelain`; on older git it passes `--no-recurse-submodules`),
  where `git fetch` by default fetches the commits a fetched superproject commit needs: a tool
  that wants them runs `git submodule update`. Its output is captured, so it shows no progress,
  and with `--porcelain` git prints no summary (older git's goes to `stderr`): git-stack's
  `passthrough("fetch", "--prune", remote)` (`git_stack/git.py:1223`) stays a passthrough,
  unless git-stack accepts losing them (it can print `refs` itself). git-stack replaces
  `fetch --quiet --no-write-fetch-head <remote> <refspecs>` (`git_stack/stack.py:3039`,
  `git_stack/adapters/github.py:580`); move-forward, `fetch --quiet <remote>` and `fetch --quiet
  . <commit>:refs/heads/<branch>` (`src/move_forward/plugins/git/actions.py:260`, `283`).
- `remote_refs(git, remote, patterns=(), *, heads=False, symrefs=False, env=None)`: what a
  remote has, from one `git ls-remote`, as a `RemoteRefs` (each ref's id, and with `symrefs`
  each symbolic ref's target). git-stack replaces `ls-remote --symref <remote> HEAD`
  (`git_stack/git.py:1087`, reading `symrefs["HEAD"]`), its probe at a push URL (`1202`,
  passing its `GIT_CONFIG_COUNT` rewrite as `env`) and `ls-remote --heads -- <remote> <refs>`
  (`git_stack/stack.py:3036`, `3666`).
- `remote_urls(git, remote, *, push=False)`: the URLs git fetches a remote from or pushes it
  to, rewritten as git rewrites them (`insteadOf`, `pushInsteadOf`), from one `git remote
  get-url --all [--push]`. git-stack replaces `remote get-url --push --all`
  (`git_stack/git.py:1131`) and `remote get-url` (`git_stack/adapters/github.py:1981`), and
  can drop its own reading of `insteadOf`, `pushInsteadOf` and `pushurl`
  (`git_stack/git.py:353-370`, `580-610`, `1110-1190`) for comparing what git gives with the
  configured URLs; move-forward, `remote get-url` (`src/move_forward/plugins/github/setup.py:36`;
  its `git remote` at `:34`, which lists the remotes, stays). Unlike `fetch` and `remote_refs`,
  it takes only a remote the repository's own config names.
- `status(git, *, untracked=, ignored=, ignore_submodules=, renames=, branch=)` reads a
  worktree's status from one `git --no-optional-locks status --porcelain=v2 -z`, which
  doesn't refresh the index, so a `ReadOnlyGit` offers it: typed entries (`Changed`,
  `Renamed`, `Unmerged`, `Untracked`, `Ignored`, with a `SubmoduleState`), and with
  `branch`, HEAD's commit, branch, upstream and ahead/behind counts. `worktrees(git)` lists
  the repository's worktrees from one `git worktree list --porcelain -z` (`Worktree`: path,
  HEAD, branch, bare, detached, and the reasons it's locked or prunable). A tool replaces its
  `git status --porcelain` runs and parsers (emptiness becomes `not status(...).entries`),
  and its `git worktree list --porcelain` runs and parsers.
- `reflogs(git, *refs)` reads many refs' reflogs with one `git log --walk-reflogs`: by each
  ref as given (a full name, `HEAD` or `refs/...`: ValueError otherwise), its entries newest
  first, each a `ReflogEntry` (index, the commit's id, who moved the ref, and git's message
  as written); [] for a ref with no reflog. It replaces git-stack's one `log
  --walk-reflogs` per branch in `reflog_tips` (`git_stack/git.py:1233`, `entry.id` for `%H`)
  and `finished_rebase` (`git.py:1242`, which compares `entry.message` with its `rebase
  (finish): ...` text itself), both given `refs/heads/<branch>` already, so a tool can read
  all its branches' reflogs at once. Unlike those, run with `check=False`, a ref that names
  nothing raises `UnknownRevisionError`, failing the whole call: a tool passes only refs
  that exist, or catches it.
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
- `changed_paths(git, diffs, *, renames=True, ignore_submodules=False, paths=(),
  pathspecs=())` lists the paths each of many diffs changes (`TreeChange`: status, path, old
  and new mode and object id, and a rename's old path and similarity), and `line_changes`, with
  the same arguments, how many lines each adds and removes per path (`LineChange`; None for a
  binary file). A diff is a commit, against its parent, or a pair (old, new) of commits or
  trees, each any revision. Two processes for all of them: a `CatFile` to resolve the names,
  and `git diff-tree --stdin -z`; so a `FakeGit` can't answer them. Renames are found by
  default, as porcelain `diff` and `log` find them (a rename is its new path alone), within
  `diff.renameLimit`. A tool replaces its per-diff runs: git-stack's `changes_something`
  (`git_stack/adapters/git.py:108`, `diff --quiet`: an empty list) and `changed_files`
  (`adapters/git.py:231`, `diff --name-only -z`), one per branch, by one call for all
  branches, and `batch_line_changes` (`git.py:741`) and its hand parser by `line_changes`;
  git-recommit's `match` (`git_recommit/session.py:194`, `pathspecs=`, `renames=False`) and
  `_old_commits`' `diff-tree --stdin` and parser (`oldcommits.py:138`, `renames=False`, with
  `commits()` for each commit's author); move-forward's `_my_dirs`
  (`src/move_forward/plugins/git/client.py:306`) and git-stack's `disjoint_source_paths`
  (`git_stack/integrations.py:780`, `renames=False`), `log --name-only` and `--name-status
  -z`, by `commits()` and `changed_paths` of their ids, which give the same paths (a merge's,
  which `log` doesn't list by default, aren't listed either).
- `tree_entries(git, tree, paths=(), *, recursive=False)` lists a tree's entries (`TreeEntry`:
  mode, type, object id, path from the top), from one `git ls-tree -z --full-tree`, its paths
  taken literally. A tool replaces its raw `ls-tree` runs and parsers: git-recommit's
  `add_unchanged` (`git_recommit/session.py:333`, run per file: one call with all the paths
  instead) and `_trees` (`oldcommits.py:255`, `recursive=True`), and git-stack's
  `disjoint_source_paths` listing (`git_stack/integrations.py:801`).
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
- `Merge(ours, theirs, base=None)`, which `merge_trees` takes as well as pairs, in the same
  one process: a merge with its own merge base (git 2.40's `merge-tree --stdin` line `<base>
  -- <ours> <theirs>`, else `GitError`), so a cherry-pick of commit C onto ours is
  `Merge(ours, C, base=<C's parent>)`. git-stack can replace `is_rebased_copy`
  (`git_stack/git.py:1345-1372`: `show`, `apply --3way` and `write-tree` per commit) with one
  `merge_trees` of `Merge(<the previous upstream commit, or new_base>, <commit>,
  base=<commit's parent>)` for every commit, comparing each tree with the upstream commit's.
  That is close to what it does now, not the same: for a merge commit among its commits, `git
  show` prints a combined diff (often empty, so nothing is applied) where the Merge brings in
  the second parent's changes, and merge-ort finds renames, which `apply --3way` doesn't; its
  tests should cover both. (Its other raw `merge-tree --write-tree` runs, `git_stack/git.py:1258`,
  `1294` and the one per merge from `1308`, need no base: `merge_trees` already takes them, once
  git-stack resolves their branch names to ids and gathers calls now made one at a time.)
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

- `CatFile` no longer splits what git said at U+0085, U+2028 or U+2029, which a ref name can
  hold: a warning in `ObjectInfo.warnings` that quotes such a name is one warning, and
  `UnknownRevisionError.reason` keeps the name whole.
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
- On Windows, an interrupt (e.g. Ctrl-C) gives git time to handle the Ctrl-C it got too, and
  remove its lock files (e.g. index.lock), before git and what it started are killed; a
  timeout still kills them at once (#52).
- Git, asked to stop on an interrupt, or (on Linux and macOS; on Windows a timeout still
  kills at once) past its timeout, has 5 seconds to exit before it's killed, where it had a
  quarter of a second: on a busy machine git can take longer than that
  to remove its lock files, and was killed first, leaving index.lock behind. Stopping waits
  only until git exits, usually within milliseconds; the 5 seconds are spent only on a git
  that doesn't exit when asked (e.g. waiting for an editor), and a second interrupt still
  kills it at once.
- On Windows, `CatFile.read` of a large object could hold up to about twice its size: the
  thread reading git's output read ahead of it without limit. It now holds at most 8 chunks
  (of at most 64 KiB each) that a read hasn't taken.
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
