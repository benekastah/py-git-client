"""This project's additions to dev.py (see its docstring), held to the same rules:

python dev.py release X.Y.Z      # tag origin/main as vX.Y.Z, publish its GitHub release,
                                 # and open the next version's PR
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CI_CHECK = "CI passed"
"""The check main's ruleset requires, which says every CI job passed."""
GITHUB_ACTIONS_APP = 15368
"""GitHub Actions' app id, which reports CI_CHECK."""
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
GITHUB_URL = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com(?::443)?/"
    r"|ssh://(?:[^@/]+@)?github\.com(?::22)?/"
    r"|(?:[^@/:]+@)?github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)
PROJECT_VERSION = re.compile(r'^(version\s*=\s*")[^"]*(")', re.MULTILINE)
CHANGELOG_SECTION = re.compile(r"^## (\S+) \(([^)]*)\)[ \t]*$", re.MULTILINE)
"""A version's heading in CHANGELOG.md: `## X.Y.Z (date)`, or `(unreleased)`."""
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NEW_BLOCK = re.compile(r"\s*(?:#|[-*+] |[0-9]+\. )")
"""A Markdown line that starts a heading or a list item, rather than continuing a line."""


class ReleaseError(Exception):
    """Why a release stopped, for the user."""


def parse_version(text: str) -> tuple[int, int, int]:
    """A version's three integers, from "X.Y.Z"."""
    match = VERSION.fullmatch(text)
    if match is None:
        raise ReleaseError(f"{text!r} isn't a version (three integers, X.Y.Z)")
    major, minor, patch = map(int, match.groups())
    return major, minor, patch


def next_version(version: str) -> str:
    """The version after `version`: its patch version, incremented."""
    major, minor, patch = parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


def github_repo(url: str) -> str:
    """OWNER/REPO, from the URL of a repository on github.com (https or ssh)."""
    match = GITHUB_URL.fullmatch(url)
    if match is None:
        raise ReleaseError(f"origin ({url}) isn't on github.com; only github.com is supported")
    return "/".join(match.groups())


def origin_urls(repo: Path) -> list[str]:
    """Every URL git fetches origin from or pushes it to (it pushes to all of them), after
    its URL rewriting (insteadOf, pushInsteadOf). The tests replace it: their origin is a
    local repository standing in for GitHub."""
    fetch = _git(repo, "remote", "get-url", "--all", "origin").splitlines()
    return fetch + _git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()


def origin_github_repo(repo: Path) -> str:
    """OWNER/REPO of `repo`'s origin on github.com, which gh is told (left to itself, it may
    pick another remote, e.g. upstream). Refuses unless every URL git fetches from or pushes
    to names that one repository, so that CI is checked where the tag goes."""
    named = [github_repo(url) for url in origin_urls(repo)]
    others = sorted({name for name in named if name.lower() != named[0].lower()})
    if others:
        listed = ", ".join([named[0], *others])
        raise ReleaseError(f"origin's URLs name more than one repository: {listed}")
    return named[0]


def pyproject_version(pyproject: str) -> str:
    """`[project]`'s `version`, from pyproject.toml's text."""
    return tomllib.loads(pyproject)["project"]["version"]


def set_pyproject_version(pyproject: str, version: str) -> str:
    """pyproject.toml's text with `[project]`'s `version` set to `version`, and nothing else
    changed (comments, order and line endings included)."""
    start = re.search(r"^\[project\]\s*$", pyproject, re.MULTILINE)
    if start is not None:
        following = re.compile(r"^\[", re.MULTILINE).search(pyproject, start.end())
        end = following.start() if following else len(pyproject)
        table = pyproject[start.end() : end]
        changed, count = PROJECT_VERSION.subn(rf"\g<1>{version}\g<2>", table, count=1)
        if count:
            return pyproject[: start.end()] + changed + pyproject[end:]
    raise ReleaseError("pyproject.toml's [project] has no version")


def changelog_notes(changelog: str, version: str) -> str:
    """What CHANGELOG.md's text says under `version`'s heading, up to the next version's, its
    wrapped lines joined: its release notes. Refuses unless the section is there, dated
    (YYYY-MM-DD) and not empty."""
    headings = list(CHANGELOG_SECTION.finditer(changelog))
    for index, heading in enumerate(headings):
        if heading.group(1) == version:
            date = heading.group(2)
            if not _is_date(date):
                raise ReleaseError(
                    f"{version}'s section of CHANGELOG.md says ({date}), not a date (YYYY-MM-DD);"
                    f" date it in a PR first"
                )
            end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
            notes = _unwrapped(changelog[heading.end() : end].strip())
            if not notes:
                raise ReleaseError(f"{version}'s section of CHANGELOG.md is empty")
            return notes
    raise ReleaseError(f"CHANGELOG.md has no section for {version}")


def _is_date(text: str) -> bool:
    if DATE.fullmatch(text) is None:
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:  # e.g. a 13th month
        return False
    return True


def _unwrapped(markdown: str) -> str:
    """`markdown` with each line that continues the one before it (not blank, not after a
    heading, and not starting a heading or list item) joined to it with a space: GitHub shows
    each line break in a release's notes, which would break wrapped sentences."""
    lines: list[str] = []
    for line in markdown.splitlines():
        previous = lines[-1] if lines else ""
        continues = previous.strip() and not previous.lstrip().startswith("#")
        if continues and line.strip() and NEW_BLOCK.match(line) is None:
            lines[-1] = f"{previous} {line.strip()}"
        else:
            lines.append(line)
    return "\n".join(lines)


def _run(args: list[str], cwd: Path, stdin: str | None = None) -> str:
    """A command's output, stripped; its failure raises ReleaseError with what it said."""
    result = subprocess.run(
        args, cwd=cwd, input=stdin, capture_output=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return _run(["git", "-C", str(repo), *args], repo, stdin)


def ci_status(repo: Path, github: str, sha: str) -> str:
    """How GitHub Actions' "CI passed" check ended on commit `sha` of `github` (OWNER/REPO):
    "success" if every run of it succeeded, "pending" while one runs or waits, "missing" if
    none has started, or else the first other conclusion (e.g. "failure").

    A commit can have several runs (e.g. a pull request's as well as main's push), and
    another app could report a check of the same name, so only GitHub Actions' count, all of
    them."""
    output = _run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{github}/commits/{sha}/check-runs",
            "-f",
            f"check_name={CI_CHECK}",
            "-f",
            f"app_id={GITHUB_ACTIONS_APP}",
        ],
        repo,
    )
    runs: list[dict[str, str | None]] = json.loads(output)["check_runs"]
    if not runs:
        return "missing"
    if any(run["status"] != "completed" for run in runs):
        return "pending"
    for run in runs:
        if run["conclusion"] != "success":
            return run["conclusion"] or "no conclusion"
    return "success"


def open_pr(worktree: Path, github: str, branch: str, title: str, body: str) -> str:
    """Open a PR from `branch` into main on `github` (OWNER/REPO), and return its URL."""
    args = ["gh", "pr", "create", "--repo", github, "--base", "main", "--head", branch]
    output = _run([*args, "--title", title, "--body", body], worktree)
    return output.splitlines()[-1]


def create_release(repo: Path, github: str, tag: str, notes: str) -> str:
    """Publish `tag`, already on `github` (OWNER/REPO), as a GitHub release titled with it and
    saying `notes`; returns its URL."""
    args = ["gh", "release", "create", tag, "--repo", github, "--verify-tag", "--title", tag]
    return _run([*args, "--notes-file", "-"], repo, notes).splitlines()[-1]


def uv_lock(worktree: Path) -> None:
    """Bring uv.lock up to date with pyproject.toml."""
    _run(["uv", "lock", "--quiet"], worktree)


def publish(repo: Path, version: str) -> tuple[str, str, str]:
    """Release `version` from `repo`'s origin: tag origin/main's latest commit `vX.Y.Z` and
    push the tag, publish it as a GitHub release whose notes are origin/main's CHANGELOG.md
    section for `version`, then open a PR moving main to the next version. Returns the tag, the
    release's URL and the PR's. Refuses, before changing anything, unless origin is on
    github.com, `version` is origin/main's pyproject.toml version, its CHANGELOG.md section is
    there, dated and not empty, neither its tag nor the next version's branch exists yet, and CI passed on that
    commit."""
    parse_version(version)
    github = origin_github_repo(repo)
    tag = f"v{version}"
    _git(repo, "fetch", "--quiet", "origin")
    if _git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise ReleaseError(f"{tag} exists on origin")
    if _git(repo, "tag", "--list", tag):
        raise ReleaseError(f"{tag} exists locally")
    branch = _next_version_branch(version)
    if _git(repo, "branch", "--list", branch):
        raise ReleaseError(f"{branch}, the next version's branch, exists locally")
    if _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"):
        raise ReleaseError(f"{branch}, the next version's branch, exists on origin")
    current = pyproject_version(_git(repo, "show", "origin/main:pyproject.toml"))
    if current != version:
        raise ReleaseError(f"pyproject.toml's version is {current} on origin/main, not {version}")
    notes = changelog_notes(_git(repo, "show", "origin/main:CHANGELOG.md"), version)
    sha = _git(repo, "rev-parse", "origin/main^{commit}")
    status = ci_status(repo, github, sha)
    if status == "pending":
        raise ReleaseError(f"{CI_CHECK} hasn't finished on origin/main ({sha}); wait for it")
    if status == "missing":
        raise ReleaseError(f"{CI_CHECK} hasn't reported on origin/main ({sha}); wait for it")
    if status != "success":
        raise ReleaseError(f"{CI_CHECK} concluded {status} on origin/main ({sha})")
    _git(repo, "tag", "--annotate", tag, "--message", f"Release {version}", sha)
    try:
        _git(repo, "push", "--quiet", "origin", f"refs/tags/{tag}")
    except ReleaseError:
        _git(repo, "tag", "--delete", tag)  # so that a rerun isn't refused for it
        raise
    upcoming = next_version(version)
    url = None  # the release's, once published
    try:
        url = create_release(repo, github, tag, notes)
        return tag, url, _start_next_version(repo, github, version)
    except BaseException as error:  # a Ctrl-C too: the tag is out, so say what's left
        why = str(error) or type(error).__name__  # e.g. KeyboardInterrupt, which says nothing
        if url:
            step, left = f"starting {upcoming}", ""
        else:
            kept, notes_file = tempfile.mkstemp(prefix=f"{tag}-notes-", suffix=".md")
            with os.fdopen(kept, "w", encoding="utf-8") as file:
                file.write(notes)
            step = "publishing its GitHub release"
            left = (
                f"`gh release create {tag} --repo {github} --verify-tag --title {tag}"
                f" --notes-file {notes_file}`, then "
            )
        message = (
            f"pushed {tag}, but {step} failed: {why}\n"
            f"To finish by hand: {left}on a branch from origin/main, set pyproject.toml's version"
            f" to {upcoming}, run `uv lock`, commit, push, and open a PR into main (or, if"
            f" start-{upcoming} was pushed, just open its PR)."
        )
        if isinstance(error, ReleaseError):
            raise ReleaseError(message) from error
        print(f"release: {message}", file=sys.stderr)
        raise


def _next_version_branch(released: str) -> str:
    """The branch of the PR moving main on from `released`."""
    return f"start-{next_version(released)}"


def _start_next_version(repo: Path, github: str, released: str) -> str:
    """Open a PR moving main's version on from `released`, from a temporary worktree (main's
    ruleset requires CI, so the change can't be pushed to main itself); returns its URL."""
    upcoming = next_version(released)
    branch = _next_version_branch(released)
    title = f"Start version {upcoming}, after releasing {released}"
    message = (
        f"{title}\n\nmain always carries the next version to release (see PROJECT_AGENTS.md),"
        f" so that what lands from now on is released as {upcoming}, never as {released} again.\n"
    )
    body = (
        f"Opened by `python3 dev.py release {released}`, after it tagged v{released}.\n\n"
        f"main always carries the next version to release (see PROJECT_AGENTS.md)."
        f" Merge this like any other PR once CI passes.\n"
    )
    scratch = Path(tempfile.mkdtemp(prefix="release-"))
    worktree = scratch / "worktree"
    created = False  # whether this run made the branch, and so may delete it
    try:
        _git(
            repo,
            "worktree",
            "add",
            "--quiet",
            "--no-track",
            "-b",
            branch,
            str(worktree),
            "origin/main",
        )
        created = True
        pyproject = worktree / "pyproject.toml"
        text = pyproject.read_bytes().decode("utf-8")
        pyproject.write_bytes(set_pyproject_version(text, upcoming).encode("utf-8"))
        uv_lock(worktree)
        _git(worktree, "add", "pyproject.toml", "uv.lock")
        _git(worktree, "commit", "--quiet", "--file", "-", stdin=message)
        _git(worktree, "push", "--quiet", "origin", branch)
        return open_pr(worktree, github, branch, title, body)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            check=False,
        )
        if created:  # pushed by now, or nothing of value lost
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", branch], capture_output=True, check=False
            )
        shutil.rmtree(scratch, ignore_errors=True)


def release(args: list[str]) -> None:
    """`dev.py release X.Y.Z`: publish X.Y.Z from this clone (see `publish`)."""
    if len(args) != 1:
        sys.exit("usage: python dev.py release X.Y.Z")
    missing = [name for name in ("git", "uv", "gh") if shutil.which(name) is None]
    if missing:
        sys.exit(f"release: needs {' and '.join(missing)}, which isn't on PATH; install it")
    try:
        tag, release_url, url = publish(ROOT, args[0])
    except ReleaseError as error:
        sys.exit(f"release: {error}")
    print(f"pushed {tag}")
    print(f"published {release_url}")
    print(f"opened {url}")


COMMANDS = {"release": release}
