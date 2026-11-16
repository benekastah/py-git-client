# py-git-client's own rules

What's true of py-git-client alone. `AGENTS.md` has the rules it shares with every project
made from python-template; where the two disagree, this file wins. Keep this file current when
the shape of the code or a rule here changes.

## Shape of the code

- `src/git_client/client.py`: `ReadOnlyGit`, which runs every git process (only commands that
  read), `Git`, which extends it to run any, and `find_git`.
- `src/git_client/errors.py`: `GitError`, and the subclass each failure git describes raises.
- `src/git_client/cat_file.py`: `CatFile`, the long-lived `git cat-file --batch-command`.
- `src/git_client/batch.py`: reads and writes that each take one git process.
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
- **The API is a contract.** git-stack, git-recommit and move-forward build on it, and the
  README's examples stay true (`tests/test_readme.py` runs its Python blocks, with git 2.36
  or newer, the oldest supported). They depend on
  `git+https://github.com/benekastah/py-git-client@main` (or a tag), locked to a commit by
  their uv.lock.
- **Versions** are 0.0.N, each released as a git tag, `v0.0.N`, as often as needed, with
  `python3 dev.py release 0.0.N` (see the README). After each release, `main` carries the
  next version (the release command opens that PR), so a change that could break git-stack,
  git-recommit or move-forward is simply released as the next patch version. PRs never change
  the version themselves.
- **Every git process starts through `ReadOnlyGit`** (`process`, or `popen` for one that
  answers requests; `Git` inherits both), so the way git is started stays the quickest each
  system allows, and a ReadOnlyGit refuses what could write. Never pass a working directory
  (`cwd=`): it keeps Python from using `posix_spawn`; use `-C`.
- **Real git, not mocks, in this library's tests:** what it promises is how git behaves.
  `FakeGit` is for projects using it, to unit-test the commands they run, once this library
  has shown how git answers them.
- **Errors are the library's own**, never subprocess's: which `GitError` a failure raises is
  read from git's message (in English: commands whose output is read run with `LC_ALL=C`),
  and each message matched is one a test makes real git print.

## CI

- **main's ruleset requires the "CI passed" check.**
- **`ci.yml`'s time limits are this project's**, lower than the template's 5 minutes: static
  and coverage 1 minute, the tests 2 (they take up to about a minute, on macOS and Windows).
  Where an update changes one of those lines, keep this project's limit.
