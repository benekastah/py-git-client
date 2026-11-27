# Roadmap

What's left to build in py-git-client. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

- **A template that stands in for an existing repository.** `RepoTemplates.copy` copies only
  what `build` made, so a test module that wants its own template on top of a `repo` already
  made (e.g. a per-module fixture) needs extra plumbing; git-recommit's tests hit this.
- **Batched ahead/behind counts.** move-forward runs one `git rev-list --left-right --count`
  per branch that tracks a local branch (rare), each against its own base; one process for
  several pairs would replace them.
