# Roadmap

What's left to build in py-git-client. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

- **A typed config reader.** git-recommit makes 2–4 `git config` calls per subcommand to read
  keys as git's bool and int (git-stack and move-forward may want it too). `config()` already
  reads every entry in one process; what's missing is git's parsing of a value as a bool
  (`yes`, `on`, `1`, an empty value, ...) and an int (`k`, `m`, `g` suffixes), so a tool can
  read many typed keys from that one process.
- **A template that stands in for an existing repository.** `RepoTemplates.copy` copies only
  what `build` made, so a test module that wants its own template on top of a `repo` already
  made (e.g. a per-module fixture) needs extra plumbing; git-recommit's tests hit this.
- **Batched ahead/behind counts.** move-forward runs one `git rev-list --left-right --count`
  per branch that tracks a local branch (rare), each against its own base; one process for
  several pairs would replace them.
