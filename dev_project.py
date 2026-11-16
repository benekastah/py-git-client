"""This project's additions to dev.py (see its docstring), held to the same rules:

python dev.py release X.Y.Z      # tag origin/main as vX.Y.Z, and open the next version's PR
"""

import json
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
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
GITHUB_URL = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
PROJECT_VERSION = re.compile(r'^(version\s*=\s*")[^"]*(")', re.MULTILINE)


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
    """ "OWNER/REPO", from a GitHub repository's URL (https or ssh)."""
    match = GITHUB_URL.fullmatch(url)
    if match is None:
        raise ReleaseError(f"origin ({url}) isn't a GitHub repository")
    return "/".join(match.groups())


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
    """How GitHub Actions' "CI passed" check ended on commit `sha` of `github` (OWNER/REPO): "success" or
    another conclusion (e.g. "failure"), "pending" while it runs or waits, "missing" if it
    hasn't started."""
    output = _run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{github}/commits/{sha}/check-runs",
            "-f",
            f"check_name={CI_CHECK}",
        ],
        repo,
    )
    runs: list[dict[str, str | None]] = json.loads(output)["check_runs"]
    if not runs:
        return "missing"
    latest = runs[0]  # GitHub lists the latest run of each check (filter=latest) first.
    if latest["status"] != "completed":
        return "pending"
    return latest["conclusion"] or "missing"


def open_pr(worktree: Path, github: str, branch: str, title: str, body: str) -> str:
    """Open a PR from `branch` into main on `github` (OWNER/REPO), and return its URL."""
    args = ["gh", "pr", "create", "--repo", github, "--base", "main", "--head", branch]
    output = _run([*args, "--title", title, "--body", body], worktree)
    return output.splitlines()[-1]


def uv_lock(worktree: Path) -> None:
    """Bring uv.lock up to date with pyproject.toml."""
    _run(["uv", "lock", "--quiet"], worktree)


def publish(repo: Path, version: str) -> tuple[str, str]:
    """Release `version` from `repo`'s origin: tag origin/main's latest commit `vX.Y.Z` and
    push the tag, then open a PR moving main to the next version. Returns the tag and the PR's
    URL. Refuses, before changing anything, unless `version` is origin/main's
    pyproject.toml version, has no tag yet, and CI passed on that commit."""
    parse_version(version)
    # gh is told the repository: left to itself, it may pick another remote (e.g. upstream).
    # The URL as configured, not as insteadOf rewrites it, names the repository.
    github = github_repo(_git(repo, "config", "--get", "remote.origin.url"))
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
    try:
        return tag, _start_next_version(repo, github, version)
    except ReleaseError as error:
        upcoming = next_version(version)
        raise ReleaseError(
            f"pushed {tag}, but starting {upcoming} failed: {error}\n"
            f"To finish by hand: on a branch from origin/main, set pyproject.toml's version to"
            f" {upcoming}, run `uv lock`, commit, push, and open a PR into main (or, if"
            f" start-{upcoming} was pushed, just open its PR)."
        ) from error


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
        f"{title}\n\nmain always carries the next version to release (see AGENTS.md), so that"
        f" what lands from now on is released as {upcoming}, never as {released} again.\n"
    )
    body = (
        f"Opened by `python3 dev.py release {released}`, after it tagged v{released}.\n\n"
        f"main always carries the next version to release (see AGENTS.md). Merge this like any"
        f" other PR once CI passes.\n"
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
        tag, url = publish(ROOT, args[0])
    except ReleaseError as error:
        sys.exit(f"release: {error}")
    print(f"pushed {tag}")
    print(f"opened {url}")


COMMANDS = {"release": release}
