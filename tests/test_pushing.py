from pathlib import Path
from unittest.mock import ANY

import pytest

from git_client import Git, PushStatus, PushUpdate, push
from git_client.pushing import _destinations  # pyright: ignore[reportPrivateUsage]
from git_client.testing import FakeGit, GitCall


def test_push_reports_partial_publication(repo: Git, tmp_path: Path) -> None:
    first = tmp_path / "first git"
    second = tmp_path / "second git"
    earlier = repo.run("rev-parse", "HEAD")
    repo.run("commit", "--allow-empty", "-qm", "old")
    old = repo.run("rev-parse", "HEAD")
    repo.run("remote", "add", "mirrors", str(first))
    for destination in (first, second):
        repo.run("clone", "--quiet", "--bare", ".", str(destination))
        repo.run("config", "--add", "remote.mirrors.pushurl", str(destination))
    Git(second).run("update-ref", "refs/heads/main", earlier, old)
    repo.run("commit", "--allow-empty", "-qm", "publish")
    published = repo.run("rev-parse", "HEAD")

    result = push(
        repo,
        "mirrors",
        [
            PushUpdate("refs/heads/main", published, lease=old),
            PushUpdate("refs/heads/new", published),
        ],
        atomic=True,
    )

    assert result.returncode != 0
    assert [
        (destination.url, [(ref.source, ref.target, ref.status) for ref in destination.refs])
        for destination in result.destinations
    ] == [
        (
            str(first),
            [
                (published, "refs/heads/main", PushStatus.FAST_FORWARD),
                (published, "refs/heads/new", PushStatus.CREATED),
            ],
        ),
        (
            str(second),
            [
                (published, "refs/heads/main", PushStatus.REJECTED),
                (published, "refs/heads/new", PushStatus.REJECTED),
            ],
        ),
    ]
    assert Git(first).run("rev-parse", "refs/heads/main") == published
    assert Git(second).run("rev-parse", "refs/heads/main") == earlier
    assert result.destinations[0].refs[0].old_oid == old
    assert result.destinations[0].refs[0].new_oid == published
    assert "stale info" in result.stdout


def test_push_reports_noop_and_deletion(repo: Git, tmp_path: Path) -> None:
    destination = tmp_path / "remote"
    repo.run("branch", "obsolete")
    repo.run("clone", "--quiet", "--bare", ".", str(destination))

    result = push(
        repo,
        str(destination),
        [PushUpdate("refs/heads/main", "refs/heads/main"), PushUpdate("refs/heads/obsolete", None)],
    )

    assert result.returncode == 0
    assert len(result.destinations) == 1
    assert {ref.target: ref.status for ref in result.destinations[0].refs} == {
        "refs/heads/main": PushStatus.UP_TO_DATE,
        "refs/heads/obsolete": PushStatus.DELETED,
    }


def test_push_reports_a_destination_that_failed_outright(repo: Git, tmp_path: Path) -> None:
    # Git ends the report of a destination whose push failed (here its unpacker refused the
    # pack) without "Done", and goes on to the next push URL.
    first, small, last = (tmp_path / name for name in ("first", "small", "last"))
    repo.run("remote", "add", "mirrors", str(first))
    for destination in (first, small, last):
        repo.run("clone", "--quiet", "--bare", ".", str(destination))
        repo.run("config", "--add", "remote.mirrors.pushurl", str(destination))
    Git(small).run("config", "receive.maxInputSize", "1")
    repo.run("commit", "--allow-empty", "-qm", "publish")

    result = push(repo, "mirrors", [PushUpdate("refs/heads/main", "refs/heads/main")])

    assert result.returncode == 1
    assert [
        (destination.url, destination.done, [(r.status, r.summary) for r in destination.refs])
        for destination in result.destinations
    ] == [
        (str(first), True, [(PushStatus.FAST_FORWARD, ANY)]),
        (str(small), False, [(PushStatus.REJECTED, "[remote rejected] (unpacker error)")]),
        (str(last), True, [(PushStatus.FAST_FORWARD, ANY)]),
    ]
    assert "remote unpack failed" in result.stderr


def test_push_leaves_out_a_destination_refused_before_any_ref(repo: Git, tmp_path: Path) -> None:
    # The pre-push hook refuses the middle push URL: git prints nothing for it on stdout, says
    # so on stderr, and goes on to the next. Hooks run in git's sh on every system.
    first, middle, last = (tmp_path / name for name in ("first", "middle", "last"))
    repo.run("remote", "add", "mirrors", str(first))
    for destination in (first, middle, last):
        repo.run("clone", "--quiet", "--bare", ".", str(destination))
        repo.run("config", "--add", "remote.mirrors.pushurl", str(destination))
    hook = Path(repo.directory) / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\ncase "$2" in *middle) exit 1;; esac\n', encoding="utf-8")
    hook.chmod(0o755)
    repo.run("commit", "--allow-empty", "-qm", "publish")

    result = push(repo, "mirrors", [PushUpdate("refs/heads/main", "refs/heads/main")])

    assert result.returncode == 1
    assert [(d.url, d.done, [r.status for r in d.refs]) for d in result.destinations] == [
        (str(first), True, [PushStatus.FAST_FORWARD]),
        (str(last), True, [PushStatus.FAST_FORWARD]),
    ]
    assert f"error: failed to push some refs to '{middle}'" in result.stderr


def test_push_stops_at_a_destination_it_cannot_reach(repo: Git, tmp_path: Path) -> None:
    # Git stops the whole push there: that destination and the ones after it aren't tried.
    first, last = tmp_path / "first", tmp_path / "last"
    repo.run("remote", "add", "mirrors", str(first))
    for destination in (first, tmp_path / "missing", last):
        repo.run("config", "--add", "remote.mirrors.pushurl", str(destination))
    for destination in (first, last):
        repo.run("clone", "--quiet", "--bare", ".", str(destination))
    repo.run("commit", "--allow-empty", "-qm", "publish")

    result = push(repo, "mirrors", [PushUpdate("refs/heads/main", "refs/heads/main")])

    assert result.returncode == 128
    assert [(d.url, d.done) for d in result.destinations] == [(str(first), True)]
    assert "does not appear to be a git repository" in result.stderr
    assert Git(last).run("rev-parse", "main") != repo.run("rev-parse", "main")


def test_push_reports_unreachable_destination(repo: Git, tmp_path: Path) -> None:
    result = push(repo, str(tmp_path / "missing"), [PushUpdate("refs/heads/main", "main")])

    assert result.returncode != 0
    assert not result.destinations
    assert "does not appear to be a git repository" in result.stderr


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_push_reports_full_ids_for_a_forced_update(tmp_path: Path, object_format: str) -> None:
    local = tmp_path / "work"
    Git(tmp_path).run("init", "--quiet", f"--object-format={object_format}", str(local))
    repo = Git(local)
    repo.run("commit", "--allow-empty", "-qm", "old")
    destination = tmp_path / "remote"
    repo.run("branch", "work")
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    repo.run("remote", "add", "origin", str(destination))
    repo.run("commit", "--allow-empty", "-qm", "advance")
    old = repo.run("rev-parse", "HEAD")
    new = repo.run("rev-parse", "work")
    Git(destination).run("fetch", str(repo.directory), "main:work")

    result = push(
        repo,
        "origin",
        [PushUpdate("refs/heads/work", "refs/heads/work", lease=old)],
        set_upstream=True,
    )

    assert result.returncode == 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert ref.status is PushStatus.FORCED
    assert (ref.old_oid, ref.new_oid) == (old, new)
    assert repo.run("config", "branch.work.remote") == "origin"


def test_push_reports_source_expressions_with_colons(repo: Git, tmp_path: Path) -> None:
    destination = tmp_path / "remote"
    repo.run("clone", "--quiet", "--bare", ".", str(destination))

    result = push(repo, str(destination), [PushUpdate("refs/tags/blob", "HEAD:a")])

    assert result.returncode == 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert (ref.source, ref.target) == ("HEAD:a", "refs/tags/blob")


def test_missing_remote_acknowledgement_is_not_a_rejection() -> None:
    # Git prints this row when the remote's report omits a ref after unpacking.
    (reported,) = _destinations(
        "To example\n!\trefs/heads/main:refs/heads/main\t[remote failure] (remote failed to report status)\nDone\n"
    )
    (ref,) = reported.refs
    assert ref.status is PushStatus.UNKNOWN


OK_ROW = "=\trefs/heads/main:refs/heads/main\t[up to date]"


def test_destination_without_done_is_reported_unfinished() -> None:
    # Git ends a destination's report with "Done" unless its push failed outright.
    assert [(d.url, d.done) for d in _destinations(f"To A\n{OK_ROW}\nTo B\n{OK_ROW}\nDone\n")] == [
        ("A", False),
        ("B", True),
    ]
    assert [(d.url, d.done) for d in _destinations(f"To A\n{OK_ROW}\n")] == [("A", False)]


@pytest.mark.parametrize(
    "row",
    [
        "something else",
        "=\trefs/heads/main:refs/heads/main",
        "=\trefs/heads/main\t[up to date]",
        "?\trefs/heads/main:refs/heads/main\t[up to date]",
    ],
    ids=["unrecognized", "too few columns", "no colon", "unknown flag"],
)
def test_destination_with_an_unrecognized_row_is_left_out(row: str) -> None:
    assert _destinations(f"To A\n{OK_ROW}\n{row}\n{OK_ROW}\nDone\n") == ()


def test_push_reports_a_rejected_deletion_with_an_empty_source(repo: Git, tmp_path: Path) -> None:
    # Git names the source of a deletion it rejects itself "(delete)".
    destination = tmp_path / "remote"
    repo.run("branch", "obsolete")
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    stale = repo.run("rev-parse", "HEAD")
    repo.run("commit", "--allow-empty", "-qm", "moved")
    Git(destination).run("fetch", str(repo.directory), "+main:obsolete")

    result = push(repo, str(destination), [PushUpdate("refs/heads/obsolete", None, lease=stale)])

    assert result.returncode != 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert (ref.source, ref.target, ref.status) == ("", "refs/heads/obsolete", PushStatus.REJECTED)
    assert "(delete):refs/heads/obsolete" in result.stdout


def test_fake_git_answers_push_by_its_documented_command() -> None:
    # The README tells FakeGit users to respond to exactly this command.
    fake = FakeGit()
    fake.respond(["-c", "core.abbrev=no", "push", "--porcelain"], stdout=f"To A\n{OK_ROW}\nDone\n")

    result = push(fake, "origin", [PushUpdate("refs/heads/main", "main")])

    assert [(d.url, [r.status for r in d.refs]) for d in result.destinations] == [
        ("A", [PushStatus.UP_TO_DATE])
    ]
    assert fake.commands == [
        ["-c", "core.abbrev=no", "push", "--porcelain", "--", "origin", "main:refs/heads/main"]
    ]


def diverge(repo: Git, destination: Path, *branches: str) -> None:
    """Give each branch a commit locally, and the destination's branch another."""
    for branch in branches:
        repo.run("branch", branch)
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    other = Git(destination)
    tree = other.run("rev-parse", "main^{tree}")
    for branch in branches:
        commit = other.run("commit-tree", "-p", branch, "-m", "theirs", tree)
        other.run("update-ref", f"refs/heads/{branch}", commit)
        ours = repo.run("commit-tree", "-p", branch, "-m", "ours", tree)
        repo.run("update-ref", f"refs/heads/{branch}", ours)


def test_push_updates_many_refs_in_one_process(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    destination = tmp_path / "remote"
    repo.run("branch", "obsolete")
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    head = repo.run("rev-parse", "HEAD")
    git_calls.clear()

    result = push(
        repo,
        str(destination),
        [
            PushUpdate("refs/heads/new", "refs/heads/main"),
            PushUpdate("refs/tags/v1", head, lease=""),
            PushUpdate("refs/heads/obsolete", None, force=True),
        ],
    )

    assert git_calls == [
        [
            "-c",
            "core.abbrev=no",
            "push",
            "--porcelain",
            "--force-with-lease=refs/tags/v1:",
            "--",
            str(destination),
            "refs/heads/main:refs/heads/new",
            f"{head}:refs/tags/v1",
            "+:refs/heads/obsolete",
        ]
    ]
    assert result.returncode == 0
    (reported,) = result.destinations
    assert {ref.target: ref.status for ref in reported.refs} == {
        "refs/heads/new": PushStatus.CREATED,
        "refs/heads/obsolete": PushStatus.DELETED,
        "refs/tags/v1": PushStatus.CREATED,
    }


def test_push_keeps_the_updates_git_accepts_unless_atomic(repo: Git, tmp_path: Path) -> None:
    destination = tmp_path / "remote"
    diverge(repo, destination, "forced", "kept")
    theirs = Git(destination).run("rev-parse", "kept")

    result = push(
        repo,
        str(destination),
        [
            PushUpdate("refs/heads/forced", "refs/heads/forced", force=True),
            PushUpdate("refs/heads/kept", "refs/heads/kept"),
        ],
    )

    assert result.returncode == 1
    (reported,) = result.destinations
    assert [(ref.target, ref.status) for ref in reported.refs] == [
        ("refs/heads/forced", PushStatus.FORCED),
        ("refs/heads/kept", PushStatus.REJECTED),
    ]
    assert Git(destination).run("rev-parse", "forced") == repo.run("rev-parse", "forced")
    assert Git(destination).run("rev-parse", "kept") == theirs


def test_atomic_push_updates_nothing_when_git_rejects_one(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    destination = tmp_path / "remote"
    diverge(repo, destination, "forced", "kept")
    before = Git(destination).run("for-each-ref")
    git_calls.clear()

    result = push(
        repo,
        str(destination),
        [
            PushUpdate("refs/heads/forced", "refs/heads/forced", force=True),
            PushUpdate("refs/heads/kept", "refs/heads/kept"),
        ],
        atomic=True,
    )

    assert "--atomic" in git_calls[0]
    assert result.returncode == 1
    (reported,) = result.destinations
    assert {ref.status for ref in reported.refs} == {PushStatus.REJECTED}
    assert Git(destination).run("for-each-ref") == before


def test_push_skips_the_pre_push_hook_only_if_asked(repo: Git, tmp_path: Path) -> None:
    # Hooks run in git's sh on every system.
    destination = tmp_path / "remote"
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    hook = Path(repo.directory) / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    update = PushUpdate("refs/heads/new", "refs/heads/main")

    assert push(repo, str(destination), [update]).returncode == 1
    assert push(repo, str(destination), [update], no_verify=True).returncode == 0
    assert Git(destination).run("rev-parse", "new") == repo.run("rev-parse", "main")


def test_push_reads_a_remote_like_an_option_as_a_remote(repo: Git) -> None:
    result = push(repo, "--version", [PushUpdate("refs/heads/main", "main")])

    assert result.returncode == 128
    assert "'--version'" in result.stderr
    assert "git version" not in result.stdout


def test_push_needs_an_update(repo: Git) -> None:
    # Without a refspec, git would push what its config says to.
    with pytest.raises(ValueError, match="no updates"):
        push(repo, "origin", [])
