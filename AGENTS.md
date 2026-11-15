# Working on py-git-client

Notes for agents (and people) changing this repo: what's true of this project. How to work
(PRs, reviews, tests first, worktrees, git stack) is in your global agent instructions. The
README is the user documentation; its Development section says how to set up a clone and run
the checks. Keep this file up to date when the shape of the code or a rule here changes.

## Shape of the code

- `src/git_client/client.py`: `Git`, which runs every git process, and `find_git`.
- `src/git_client/cat_file.py`: `CatFile`, the long-lived `git cat-file --batch-command`.
- `src/git_client/batch.py`: reads and writes that each take one git process.
- `src/git_client/__init__.py`: the public API; anything not exported there is private.
- `tests/`: the tests, against real repositories (see `tests/conftest.py`).
- `dev.py`: setting up a clone and running the checks, on every system (see the README).

## Rules for this project

- **No dependencies.** git-stack and move-forward allow no runtime dependencies but this
  library, which they could only take because it has none of its own.
- **The API is a contract.** git-stack, git-recommit and move-forward build on it: a change
  that could break one is a new major version, and the README's examples stay true.
- **Every git process starts through `Git`** (`process`, or `popen` for one that answers
  requests), so the way git is started stays the quickest each system allows. Never pass a
  working directory (`cwd=`): it keeps Python from using `posix_spawn`; use `-C`.
- **Real git, not mocks, in the tests:** what this library promises is how git behaves.

## Tests and checks

- **Before committing, run `python3 dev.py check`**: Ruff's formatter and linter, basedpyright
  in strict mode, then the tests. `python3 dev.py coverage` shows what the tests don't run.
  The pre-commit hook formats and lint-fixes staged files; the pre-push hook runs every
  check before a push to `main`. PRs are squash-merged, so the check must pass on a PR's
  last commit.
- **Tests keep to themselves**: no network, and not the user's real config, home directory
  or other state; use `tmp_path`, and give programs the tests start a config that reaches
  nothing.
- **Keep the tests fast.** Starting a process costs about 24ms on GitHub's Windows runners
  and 4–7ms on macOS, against under 1ms on Linux, and how Python starts it makes no
  difference, so on those systems the number of processes is what makes a suite slow. Call
  the code in the test's process rather than through its command line, except in the few
  tests of the command line itself; build expensive fixtures (e.g. git repositories) once per
  session and copy them for each test; and prefer one long-lived process (e.g.
  `git cat-file --batch`) over one per request.
- **Each CI job has a time limit** (`timeout-minutes` in `.github/workflows/check.yml`),
  about 20% over how long it takes. If a job runs into its limit, speed it up if you can
  (the tests, or the code itself); only if you can't, raise the limit, saying why.

## Linux, macOS and Windows

py-git-client runs on all three, and CI runs the tests on each. Keep it that way, with
every change:

- Use `pathlib.Path` for paths, not strings joined with `/`, and compare them as `Path`s.
  Write paths into TOML or URLs with `as_posix()`.
- Read and write text as UTF-8 (`encoding="utf-8"`, not `text=True`): Windows' default
  encoding is its code page. Don't assume `\n` line endings in files you didn't write, or a
  case-sensitive filesystem (macOS and Windows usually aren't).
- Run programs with their arguments as a list, never through a shell, and don't call Unix
  tools (`sed`, `grep`, ...) from Python.
- Branch on `sys.platform` rather than assuming POSIX (`fcntl`, `os.fork`, signals, modes).
  When a system needs its own way, give it one rather than skipping it there.
- In tests, make a fake command as a Python script with a launcher, not a shell script or a
  symlink, which Windows can't run or usually can't make. A test that can only run on one
  system says why in its `skipif`, and the other systems get a test of their own.
- Dev tooling goes in `dev.py`, not shell scripts.
