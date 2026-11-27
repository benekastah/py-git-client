# Roadmap

What's left to build in py-git-client. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

- **A typed config reader.** git-recommit makes 2–4 `git config` calls per subcommand to read
  keys as git's bool and int (git-stack and move-forward may want it too). `config()` already
  reads every entry in one process, as strings; what's missing is git's own reading of a
  value as a bool or an int (`git config --type=bool` or `--type=int`, never parsed in
  Python: see PROJECT_AGENTS.md), for many keys in as few processes as git allows.
- **A template that stands in for an existing repository.** `RepoTemplates.copy` copies only
  what `build` made, so a test module that wants its own template on top of a `repo` already
  made (e.g. a per-module fixture) needs extra plumbing; git-recommit's tests hit this.
- **Batched ahead/behind counts of pairs.** `refs(ahead_behind=)` counts many refs against one
  base, and `push_track` against where git would push each; move-forward still runs one `git
  rev-list --left-right --count` per branch that tracks a local branch (rare), each against
  its own base (`<remote>/<branch>`, which git's `%(push)` names only under some
  `push.default`s); one process for several pairs would replace them.
