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
from git_client import CatFile, Git, RefUpdate, config, config_key, refs, update_refs

# The git on PATH (wrappers such as git-ai included), unless $MY_TOOL_GIT names another:
# a tool names its own variable so its users can choose, e.g. git itself, for speed.
# Raises FileNotFoundError if there's neither, and, on Windows, ValueError if the variable
# names a batch file (.bat, .cmd), which cmd.exe runs, reading &, | and % in arguments.
git = Git("path/to/repo", env_var="MY_TOOL_GIT", timeout=120)  # seconds; None (the default): none

git.run("rev-parse", "--abbrev-ref", "HEAD")  # "main"; raises GitError on failure
git.ok("merge-base", "--is-ancestor", "a", "b")  # True for exit 0, False for 1 (git's no)
git.resolve("main@{upstream}")  # an object id, or None if it names nothing
git.var("GIT_AUTHOR_IDENT")  # git's value, or None if no identity is set
git.passthrough("rebase", "-i", "main", timeout=None)  # a command's own timeout; past one,
# git is stopped and GitTimeoutError raised

with CatFile(git) as cat_file:  # one process for every lookup in the block
    head = cat_file.resolve("HEAD")  # an object id, or None if it names nothing
    readme = cat_file.resolve("HEAD:README.md")
    if readme is not None:
        kind, content = cat_file.read(readme)

remotes = config(git)["remote.origin.url"]  # every value of each key
push_remote = config(git).get(config_key("branch.Topic.pushRemote"))  # keys as git names them
for ref in refs(git, "refs/heads/", fields=["upstream:track"]).values():
    print(ref.name, ref.object, ref.upstream, ref.head, ref.fields["upstream:track"])

# For a branch's short name, ask for the field "refname:lstrip=2", never "refname:short",
# which comes back as "heads/v1" when a tag is named v1 too; and give git full ref names.
old = refs(git, "refs/heads/old")["refs/heads/old"].object
update_refs(git, [RefUpdate("refs/heads/old", None, old=old)])  # all or none; only if unmoved
```

Paths a tool got from the filesystem (rather than from git) can be given as bytes, in the
filesystem's encoding; text, as str, which reaches git as UTF-8 whatever the locale's
encoding. Both go for arguments and for values in `env`. Output is decoded as UTF-8 with
surrogateescape, so bytes that aren't UTF-8 survive a round trip through `encode`. Commands
whose output the library reads run with `LC_ALL=C`, so git's messages are in English
whatever the user's language; `passthrough`, whose output is the user's, keeps their locale.

### Errors

`run` and `run_bytes` raise a `GitError` when git fails, with its `command`, `returncode`,
`stderr` and `stdout`. For the failures a tool may want to handle, the library reads what git
said and raises a subclass:

| Error | When |
| --- | --- |
| `NotARepositoryError` | There's no repository at the client's directory, or no such directory. |
| `UnknownRevisionError` | A revision (a commit, ref or object id) names nothing. |
| `ConfigLockedError` | Another process has the config file locked; a write can be tried again. |
| `RemoteError` | A remote can't be reached or read from, or has no such repository. |
| `PushRejectedError` | A push was (or, with `--dry-run`, would be) rejected, e.g. not a fast-forward. |
| `ConflictError` | A rebase, merge or cherry-pick stopped on a conflict. |
| `GitTimeoutError` | git ran past its timeout, and was stopped (its `returncode` is None). |

Any other failure is a plain `GitError`. `ok` raises them too, for any exit status but 0
(True) and 1 (False, git's no for `merge-base --is-ancestor`, `diff --quiet` and the like).
`passthrough` says whether git succeeded instead, but raises `GitTimeoutError` past a timeout.

## Testing a tool that uses it

`git_client.testing` is a pytest plugin. Load it in your top `conftest.py`
(`pytest_plugins = ["git_client.testing"]`) or with `-p git_client.testing`:

- **Every test's git gets a config of its own** (automatic; `git_config` is its file): an
  identity, `main` as the first branch, no background maintenance (which would otherwise start
  a process after each commit), and no global ignore or attributes file. What a test writes
  there (`git config --global`) no other test sees. The user's and the system's config don't
  reach it, nor do git's variables that would win over it (e.g. `GIT_DIR`, `GIT_INDEX_FILE`,
  `GIT_AUTHOR_NAME` and `GIT_TEMPLATE_DIR`, which a git hook running the tests would pass on,
  or `GIT_DEFAULT_HASH`, which would make sha256 repositories; see
  `GIT_ENVIRONMENT_VARIABLES`), for the whole session, session fixtures included. git gets
  `GIT_EDITOR=true` instead of the user's editor (`GIT_SEQUENCE_EDITOR` is cleared, so
  `rebase -i` uses it too), `GIT_ALLOW_PROTOCOL=file` (no network) and
  `GIT_TERMINAL_PROMPT=0` (no asking for a password; `GIT_ASKPASS` and `SSH_ASKPASS` are
  cleared, so no program asks either): see `GIT_SETTINGS`. A test that needs
  another protocol widens it in a fixture of its own:

  ```python
  @pytest.fixture
  def ssh_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
      monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh")  # and GIT_SSH_COMMAND, a fake ssh
  ```

  To add config entries, override the session-scoped `git_config_text` fixture:

  ```python
  @pytest.fixture(scope="session")
  def git_config_text() -> str:
      return TEST_GIT_CONFIG + "[commit]\n\tgpgSign = false\n"
  ```
- **Repositories built once per session, copied for each test** (`repo_templates`), under
  the session's config rather than the config of the test that asks first, and without
  git's sample hooks (`hooks/*.sample`), which every copy would copy: building takes many
  git processes, copying takes none for a repository without remotes. One with remotes takes
  a `git config` for each remote's `url` or `pushurl` that points inside the template (a
  path, or a `file://` URL), to point it at the copy's (for a key with several values, one
  to clear it and one per value). Submodules and linked worktrees aren't repointed. A copy's
  index keeps the template's stat data: the first command that refreshes the index (`git
  status`, `git diff`) makes it match the copy's files, but until then plumbing that doesn't
  (`git diff-index HEAD`, `git diff-files`) reports every file as modified, so a test of a
  tool that uses it runs `git update-index -q --refresh` first.

  ```python
  def make_origin_and_clone(directory: Path) -> None:
      # e.g. git init --bare origin.git, git clone origin.git clone, a first commit
      ...


  @pytest.fixture
  def clone(repo_templates: RepoTemplates, tmp_path: Path) -> Git:
      repo_templates.build("origin-and-clone", make_origin_and_clone)
      return Git(repo_templates.copy("origin-and-clone", tmp_path / "repos") / "clone")
  ```
- **`real_git()`**: git itself, from `git --exec-path`, rather than a wrapper or launcher that
  starts another process for each. The plugin runs it for its own git (repointing a copy's
  remotes), but your tool runs `find_git`'s: point your tool's variable at it in your top
  `conftest.py`, at import, before anything calls `find_git`, which caches its answer:

  ```python
  os.environ["MY_TOOL_GIT"] = real_git()
  ```
- **`git_calls`**: the arguments of each git process a `Git` starts during the test, while
  real git runs, to count a tool's processes or check the ones it starts. It leaves out the
  plugin's own, building and copying templates, whatever order the fixtures come in:

  ```python
  def test_status_takes_one_process(clone: Git, git_calls: list[list[str]]) -> None:
      status(clone)  # your code
      assert git_calls == [["status", "--porcelain=v2"]]
  ```
- **`outside_any_repository`**: runs a test from an empty directory where git finds no
  repository, so a git run without a directory (a `Git()` your code forgot to give one)
  fails rather than change the repository the tests were started in. To run every test that
  way, ask for it from an autouse fixture in your top `conftest.py`:

  ```python
  @pytest.fixture(autouse=True)
  def _outside_any_repository(outside_any_repository: Path) -> None:
      pass
  ```
- **`fresh_find_git`**: `find_git` caches its answer for the whole process; a test that
  changes what it would find (PATH, or your tool's variable) asks for this fixture, which
  clears the cache before and after it.
- **`FakeGit`**, for unit tests of the git commands your code runs, without a process: give it
  responses, run your code with it, and check what it ran. Assert the commands where they're
  the behavior (what's pushed, deleted or rewritten); otherwise give realistic output and
  assert the result. Leave what a command does end to end to tests on real git.

  ```python
  git = FakeGit()
  git.respond(["rev-parse", "--abbrev-ref", "HEAD"], stdout="main\n")
  git.respond(["push"])
  publish(git)  # your code
  assert git.commands[-1] == ["push", "origin", "main"]
  ```

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

### Releasing

Versions are 0.0.N. `main` always carries the next version to release, in `pyproject.toml`;
PRs never change it themselves. Release whenever a change should reach git-stack,
git-recommit or move-forward (a change that could break one is simply released as the next
patch version; see AGENTS.md). Once CI has passed on `main`'s latest commit, release its
version:

```sh
python3 dev.py release 0.0.1
```

It needs git, uv and gh (logged in). origin's fetch and push URLs, after git's URL rewriting
(`insteadOf`, `pushInsteadOf`), must both be the same github.com repository. After fetching
origin, it refuses unless the version is `pyproject.toml`'s on `origin/main`, neither its tag nor the
next version's branch (`start-X.Y.Z+1`) exists yet, locally or on origin, and every GitHub
Actions run of the "CI passed" check succeeded on `origin/main`'s latest commit (if one is
still running, wait and run it again). Then it tags that commit (not your checkout's)
`vX.Y.Z`, pushes the tag, and, in a temporary worktree, opens a PR moving `main` to the next
patch version (`start-X.Y.Z+1`). That PR merges like any other, once CI passes. If anything
stops it after the tag is pushed, the command says how to finish by hand.

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
