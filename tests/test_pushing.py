from pathlib import Path
from unittest.mock import ANY

import pytest

from git_client import Git, PushStatus, push
from git_client.pushing import _destinations  # pyright: ignore[reportPrivateUsage]
from git_client.testing import FakeGit


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
        "--atomic",
        f"--force-with-lease=refs/heads/main:{old}",
        "mirrors",
        f"{published}:refs/heads/main",
        f"{published}:refs/heads/new",
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

    result = push(repo, "--atomic", str(destination), "refs/heads/main", ":refs/heads/obsolete")

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

    result = push(repo, "mirrors", "refs/heads/main")

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


def test_push_stops_at_a_destination_it_cannot_reach(repo: Git, tmp_path: Path) -> None:
    # Git stops the whole push there: that destination and the ones after it aren't tried.
    first, last = tmp_path / "first", tmp_path / "last"
    repo.run("remote", "add", "mirrors", str(first))
    for destination in (first, tmp_path / "missing", last):
        repo.run("config", "--add", "remote.mirrors.pushurl", str(destination))
    for destination in (first, last):
        repo.run("clone", "--quiet", "--bare", ".", str(destination))
    repo.run("commit", "--allow-empty", "-qm", "publish")

    result = push(repo, "mirrors", "refs/heads/main")

    assert result.returncode == 128
    assert [(d.url, d.done) for d in result.destinations] == [(str(first), True)]
    assert "does not appear to be a git repository" in result.stderr
    assert Git(last).run("rev-parse", "main") != repo.run("rev-parse", "main")


def test_push_reports_unreachable_destination(repo: Git, tmp_path: Path) -> None:
    result = push(repo, str(tmp_path / "missing"), "refs/heads/main")

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
        repo, "--set-upstream", f"--force-with-lease=refs/heads/work:{old}", "origin", "work"
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

    result = push(repo, str(destination), "HEAD:a:refs/tags/blob")

    assert result.returncode == 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert (ref.source, ref.target) == ("HEAD:a", "refs/tags/blob")


def test_push_reports_dry_run_with_upstream(repo: Git, tmp_path: Path) -> None:
    destination = tmp_path / "remote"
    repo.run("clone", "--quiet", "--bare", ".", str(destination))
    repo.run("remote", "add", "origin", str(destination))
    repo.run("branch", "work")

    result = push(repo, "--dry-run", "--set-upstream", "origin", "work")

    assert result.returncode == 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert ref.status is PushStatus.CREATED
    assert Git(destination).resolve("refs/heads/work") is None


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

    result = push(
        repo,
        f"--force-with-lease=refs/heads/obsolete:{stale}",
        str(destination),
        ":refs/heads/obsolete",
    )

    assert result.returncode != 0
    (reported,) = result.destinations
    (ref,) = reported.refs
    assert (ref.source, ref.target, ref.status) == ("", "refs/heads/obsolete", PushStatus.REJECTED)
    assert "(delete):refs/heads/obsolete" in result.stdout


def test_fake_git_answers_push_by_its_documented_command() -> None:
    # The README tells FakeGit users to respond to exactly this command.
    fake = FakeGit()
    fake.respond(["-c", "core.abbrev=no", "push", "--porcelain"], stdout=f"To A\n{OK_ROW}\nDone\n")

    result = push(fake, "origin")

    assert [(d.url, [r.status for r in d.refs]) for d in result.destinations] == [
        ("A", [PushStatus.UP_TO_DATE])
    ]
    assert fake.commands == [["-c", "core.abbrev=no", "push", "--porcelain", "origin"]]
