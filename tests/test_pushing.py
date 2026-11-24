from pathlib import Path

import pytest

from git_client import Git, PushStatus, push
from git_client.pushing import _destinations  # pyright: ignore[reportPrivateUsage]


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


def test_destination_without_done_is_left_out() -> None:
    # Git ends a destination's report with "Done" unless its push failed outright;
    # without it, the report may be incomplete.
    assert _destinations(f"To A\n{OK_ROW}\n") == ()


def test_unfinished_destination_is_left_out_before_a_finished_one() -> None:
    (reported,) = _destinations(f"To A\n{OK_ROW}\nTo B\n{OK_ROW}\nDone\n")
    assert reported.url == "B"


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
