# py-git-client

Run git from Python with as few processes as possible. Starting a process costs well under a
millisecond on Linux, but several on macOS and tens on Windows, so a tool that runs git many
times a command is only as fast as the number of git processes it starts. py-git-client is
what git-stack, git-recommit and move-forward share for that:

- **One way to start git** (`Git`), the quickest each system allows: by its full path, with
  `-C` rather than a working directory, and, except on Windows (or for a long-lived process),
  without `close_fds`, so Python can use `posix_spawn`. Text and paths go to git as git
  expects them (see `Git.command`).
- **One long-lived `git cat-file --batch-command`** (`CatFile`) for resolving names and
  reading objects, which keeps answering after git exits on a name (e.g. `@{u}` without an
  upstream).
- **Batched reads and writes**, each one process: every config entry (`config`), refs with
  their upstreams, symbolic refs, HEAD and any other fields (`refs`), patch ids for many commands (`patch_ids`),
  blobs (`hash_blobs`), and ref updates in one transaction (`update_refs`).

It has no dependencies of its own, and needs Python 3.11 or newer and git 2.36 or newer (for
`git cat-file --batch-command`).

## Installing

```sh
uv add git+https://github.com/benekastah/py-git-client
```

or `pip install git+https://github.com/benekastah/py-git-client`.

## Using it

```python
from git_client import CatFile, Git, RefUpdate, config, refs, update_refs

# The git on PATH (wrappers such as git-ai included), unless $MY_TOOL_GIT names another:
# a tool names its own variable so its users can choose, e.g. git itself, for speed.
# Raises FileNotFoundError if there's neither.
git = Git("path/to/repo", env_var="MY_TOOL_GIT")

git.run("rev-parse", "--abbrev-ref", "HEAD")  # "main"; raises GitError on failure
git.ok("merge-base", "--is-ancestor", "a", "b")  # True or False

with CatFile(git) as cat_file:  # one process for every lookup in the block
    head = cat_file.resolve("HEAD")  # an object id, or None if it names nothing
    readme = cat_file.resolve("HEAD:README.md")
    if readme is not None:
        kind, content = cat_file.read(readme)

remotes = config(git)["remote.origin.url"]  # every value of each key
for ref in refs(git, "refs/heads/", fields=["upstream:track"]).values():
    print(ref.name, ref.object, ref.upstream, ref.head, ref.fields["upstream:track"])

old = refs(git, "refs/heads/old")["refs/heads/old"].object
update_refs(git, [RefUpdate("refs/heads/old", None, old=old)])  # all or none; only if unmoved
```

Paths a tool got from the filesystem (rather than from git) can be given as bytes, in the
filesystem's encoding; text, as str, which reaches git as UTF-8 whatever the locale's
encoding. Both go for arguments and for values in `env`. Output is decoded as UTF-8 with surrogateescape, so bytes
that aren't UTF-8 survive a round trip through `encode`.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the repository,
then run `dev.py install`:

```sh
git clone https://github.com/benekastah/py-git-client
cd py-git-client
python3 dev.py install
```

It sets up `.venv` with the package (editable) and the dev tools, and makes the clone use the
git hooks in `git_hooks/`. On Windows, run `dev.py` with `py` (`py dev.py install`).

`dev.py` runs the same way on Linux, macOS and Windows:

```sh
python3 dev.py check       # every check: static, then tests
python3 dev.py static      # Ruff's formatter and linter, and basedpyright in strict mode
python3 dev.py tests       # the tests, in parallel (arguments go to pytest, e.g. -k name)
python3 dev.py coverage    # the tests under coverage.py, and the lines they don't run
```

If the formatting check fails, `.venv/bin/ruff format` formats the code;
`.venv/bin/ruff check --fix` fixes what it can of the linter's findings. The pre-commit hook
does both for the files you commit, and the pre-push hook runs every check before a push to
`main`.

Dependencies are locked in `uv.lock`. `dev.py` installs from it with uv, and updates it when
`pyproject.toml`'s dependencies change; commit both. Without uv, `dev.py` installs with pip,
which doesn't read the lock.

### CI and dependencies

CI (`.github/workflows/check.yml`) runs the static checks once, on Linux, and the tests on
Linux, macOS and Windows, with the oldest and newest Python py-git-client supports, for
every pull request and every push to `main`. It installs from `uv.lock` and fails if it's out
of date. Dependabot proposes new versions of the dependencies and the workflow's actions
weekly, each a week after its release.

### Updating from the template

This project was made from [python-template](https://github.com/benekastah/python-template)
with [Copier](https://copier.readthedocs.io/). To bring in the template's later changes:

```sh
uvx copier update --defaults
```

It merges them with your changes, marking conflicts as git does. It never touches this
README, AGENTS.md or `.github/workflows/check.yml`, which are this project's own. `dev.py` is
the template's: add commands and install steps in `dev_project.py` instead (see `dev.py`'s
docstring).
