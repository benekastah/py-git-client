# py-git-client's own rules

What's true of py-git-client alone. `AGENTS.md` has the rules it shares with every project
made from python-template; where the two disagree, this file wins. Keep this file current when
the shape of the code or a rule here changes.

## Shape of the code

- `src/git_client/client.py`: `ReadOnlyGit`, which offers only the library's reads (commands
  it fixes), `Git`, which extends it with the commands of the caller's (`run`, `process`,
  ...), and `find_git`; `start_process` and `start_popen`, where every git process starts,
  and `read_process`, `read_popen` and `read_output`, which the reads run through; and
  `Memory`, what git told a client (and what its `read_only()` gave) about where things are,
  which one `rev-parse` asks while any is unknown (`where_questions`, `read_where`,
  `ask_where`), with when git told it, for `remember_for` and `forget()`.
- `src/git_client/errors.py`: `GitError`, and the subclass each failure git describes raises;
  `NoAnswerError` (a timeout, or not started), which isn't a `GitError`.
- `src/git_client/cat_file.py`: `CatFile`, the long-lived `git cat-file --batch-command`.
- `src/git_client/batch.py`: reads and writes that each take one git process.
- `src/git_client/merging.py`: `merge_trees`, many `git merge-tree --write-tree` in one
  process.
- `src/git_client/config_file.py`: the changes `Git.write_configs` makes
  (`ConfigSet`, `ConfigUnset`), and what each did (`ConfigResult`).
- `src/git_client/testing.py`: a pytest plugin for projects that use the library: the tests'
  git config and isolation, the real git, repository templates, fixtures (`git_calls`,
  `fresh_find_git`, `outside_any_repository`), and `FakeGit`. It may import pytest; nothing
  else in `src/` may.
- `src/git_client/__init__.py`: the public API; anything not exported there (or in `testing`)
  is private.
- `tests/`: the tests, against real repositories, through the `git_client.testing` plugin (see
  `tests/conftest.py`).
- `dev_project.py`: this project's commands for `dev.py`: `release` (see the README).

## Rules for this project

- **No dependencies.** git-stack and move-forward allow no runtime dependencies but this
  library, which they could only take because it has none of its own.
- **A git client only: never reimplement git.** The library starts git and parses what git
  prints. It never decides in Python what git decides: no walking objects, refs or worktrees,
  no reading or editing files in a git directory, no ported algorithms or rules (merge-base,
  config syntax, ref names, ...). Where git does an action for many things in one process
  (`update-ref --stdin`, `cat-file --batch-command`, `for-each-ref`, `merge-tree --stdin`,
  `rev-parse` with many arguments, ...), use git's way; where it doesn't, run more processes:
  that's fine. Before adding a function, ask which git command answers it.
- **The API is a contract.** git-stack, git-recommit and move-forward build on it, and the
  README's examples stay true (`tests/test_readme.py` runs its Python blocks, with git 2.36
  or newer, the oldest supported; a block that starts `# Needs git X.Y.`, alone in a block of
  its own, runs only on that git or newer). They depend on
  `git+https://github.com/benekastah/py-git-client@main` (or a tag), locked to a commit by
  their uv.lock.
- **Versions** are 0.0.N, each released as a git tag, `v0.0.N`, and a GitHub release whose
  notes are its CHANGELOG.md section, as often as needed, with
  `python3 dev.py release 0.0.N` (see the README). After each release, `main` carries the
  next version (the release command opens that PR), so a change that could break git-stack,
  git-recommit or move-forward is simply released as the next patch version. PRs never change
  the version themselves.
- **CHANGELOG.md** says what each version changes for a tool that uses the library. A PR adds
  its change under the section of the version `main` carries (`## X.Y.Z (unreleased)`),
  saying what a tool must change for it (e.g. an override's new signature). Before
  `dev.py release`, a PR puts the date in place of "unreleased"; the release's PR to the next
  version adds that version's section.
- **A ReadOnlyGit offers no public way to run a command of the caller's**: it can't be asked
  to write because it takes no command line, not because the library judges one. A function that
  takes a ReadOnlyGit runs only commands the library fixes, through `read_process`, `read_popen` or
  `read_output` (a ReadOnlyGit's private `_process` and `_popen`, which on a Git call its
  public `process` and `popen`, so a subclass's override sees every process); one that runs
  a command it's given takes a Git.
- **Every git process starts in `start_process`** (or `start_popen` for one that answers
  requests), which `Git.process`, `Git.popen` and a ReadOnlyGit's reads call, so the way git
  is started stays the quickest each system allows, a client whose `stopping` says so starts
  nothing, and `git_calls` (which wraps both) sees every process (`FakeGit` replaces
  `process` and `popen`, and asks `stopping` too). Never pass a working directory (`cwd=`):
  it keeps Python from using `posix_spawn`; use `-C`.
- **Real git, not mocks, in this library's tests:** what it promises is how git behaves.
  `FakeGit` is for projects using it, to unit-test the commands they run, once this library
  has shown how git answers them.
- **Errors are the library's own**, never subprocess's: which `GitError` a failure raises is
  read from git's message (in English: commands whose output is read run with
  `GIT_TEXTDOMAINDIR` naming no directory, which leaves the user's locale to what git runs),
  and each message matched is one a test makes real git print.

## CI

- **main's ruleset requires the "CI passed" check.**
- **`ci.yml`'s time limits are this project's**, lower than the template's 5 minutes: static
  and coverage 1 minute, the tests 2 (they take up to about a minute, on macOS and Windows).
  Where an update changes one of those lines, keep this project's limit. `project.yml`'s
  `oldest-git` has 2 too: about a minute when it builds git, 15 seconds from its cache.
- **`project.yml`'s `oldest-git` job runs the tests with git 2.36**, the oldest the README
  supports, built from kernel.org's tarball and cached; CI's other jobs run the newest. A test
  of what older git can't do is marked `needs_git(major, minor)` (see `tests/conftest.py`).
  Raising the oldest supported git changes the job's `GIT_VERSION` and `GIT_SHA256`, the
  README test's `needs_git`, and the README.
