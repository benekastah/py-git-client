# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

- **`RepoTemplates.build` reuses a template of the same name silently.** Two test modules
  that each build a template `name` with a different `make` share whichever ran first in that
  worker, so a test fails only under some orders (it happened with two "submodule"s). Fix:
  remember each name's `make` and raise when another asks for the name.
- **`dev.py coverage` warns that a data file "isn't a coverage data file".** With
  `patch = ["subprocess"]`, each Python process the tests start is measured, and the tests in
  `tests/test_timeouts.py` kill some (the tools and hooks under a timeout or a signal) before
  they write their data, leaving an empty `.coverage.*` file. `coverage combine` skips it, so
  the report and the check are right, but the warning is noise in the output and CI's log.
- **The testing plugin lists git's environment variables itself.** `GIT_ENVIRONMENT_VARIABLES`
  hard-codes the variables `git rev-parse --local-env-vars` lists (among others it must
  clear), so one a later git adds isn't cleared, and leaks from a hook into the tests. Fix:
  clear what that command prints, and list only the rest (identity, editor, askpass, ...).
- **The testing plugin finds repositories by their layout.** `_repositories` decides in
  Python that a directory is one (a `.git` directory, or `HEAD`, `config` and `objects` for a
  bare one), which `PROJECT_AGENTS.md` says not to do (git decides). Fix: keep the walk (git
  can't list repositories), but ask git of each candidate directory (`rev-parse --git-dir`)
  rather than guess from its files.
