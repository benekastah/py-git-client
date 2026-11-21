# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

- **`config_key` ports a rule of git's.** It lower-cases a key's section and name in Python, as
  git does, so a tool can look a key up in what `config()` read; the rule in
  `PROJECT_AGENTS.md` leaves that to git: a tool names keys as git prints them, or asks git
  (`git config --get <key>`).
- **`dev.py coverage` warns that a data file "isn't a coverage data file".** With
  `patch = ["subprocess"]`, each Python process the tests start is measured, and the tests in
  `tests/test_timeouts.py` kill some (the tools and hooks under a timeout or a signal) before
  they write their data, leaving an empty `.coverage.*` file. `coverage combine` skips it, so
  the report and the check are right, but the warning is noise in the output and CI's log.
