import copy
import pickle
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from git_client import (
    CatFile,
    Git,
    GitError,
    NotStartedError,
    PerformanceRecorder,
    ReadOnlyGit,
    config,
    git_paths,
)
from git_client.testing import FakeGit


def test_records_writes_and_backend_reads_but_omits_a_cache_hit(repo: Git) -> None:
    recorder = PerformanceRecorder()
    git = Git(repo.directory, performance=recorder)
    reader = git.read_only()
    git.run("config", "test.value", "value")
    assert reader.prefix() == ""
    assert reader.cdup() == ""
    assert config(git=reader)["test.value"] == ["value"]

    assert [(event.kind, event.name) for event in recorder.events] == [
        ("process", "git"),
        ("read", "ReadOnlyGit.prefix"),
        ("process", "git"),
        ("read", "config"),
        ("process", "git"),
    ]
    for event in recorder.events:
        assert event.end is not None and event.start <= event.end
    assert recorder.events[2].args[0] == "rev-parse"
    assert recorder.events[1].start <= recorder.events[2].start
    read_end, process_end = recorder.events[1].end, recorder.events[2].end
    assert read_end is not None and process_end is not None and process_end <= read_end


def test_cat_file_lifetime_is_separate_from_work(repo: Git) -> None:
    recorder = PerformanceRecorder()
    with CatFile(ReadOnlyGit(repo.directory, performance=recorder)) as objects:
        assert objects.resolve("HEAD") is not None
        (opened,) = [event for event in recorder.events if event.kind == "process"]
        assert opened.pid is not None and opened.end is None
        commit = objects.verify("HEAD")
        assert objects.read(commit)[0] == "commit"

    events = recorder.events
    assert [event.name for event in events if event.kind == "read"] == [
        "CatFile.resolve",
        "CatFile.verify",
        "CatFile.read",
    ]
    (process,) = [event for event in events if event.kind == "process"]
    assert process.persistent and process.pid is not None and process.returncode == 0
    assert process.args == ("cat-file", "--batch-command")
    work = [event for event in events if event.kind == "process_work"]
    assert [event.name for event in work] == [
        "CatFile.request",
        "CatFile.request",
        "CatFile.request",
        "CatFile.contents",
        "CatFile.shutdown",
    ]
    assert work[0].start <= process.start
    assert process.end is not None and work[-1].end is not None
    assert process.end <= work[-1].end
    assert opened.end is None  # snapshots are immutable


def test_records_process_failures_and_observes_exit_only_once(repo: Git, tmp_path: Path) -> None:
    recorder = PerformanceRecorder()
    git = Git(repo.directory, performance=recorder)
    with pytest.raises(GitError):
        git.run("rev-parse", "--verify", "missing")
    with git.popen("version") as process:
        process.communicate()
        assert process.poll() == process.wait() == 0
    broken = Git(repo.directory, program=str(tmp_path / "missing"), performance=recorder)
    with pytest.raises(FileNotFoundError):
        broken.run("version")
    failed, exited, unstarted = recorder.events
    assert failed.pid is not None and failed.returncode != 0
    assert exited.pid is not None and exited.returncode == 0
    assert unstarted.pid is None and unstarted.returncode is None


def test_shared_recorder_records_each_thread(repo: Git) -> None:
    recorder = PerformanceRecorder()

    def resolve(_: int) -> str | None:
        return Git(repo.directory, performance=recorder).resolve("HEAD")

    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(resolve, range(8)))
    assert answers[0] is not None and len(set(answers)) == 1
    assert sum(event.kind == "read" for event in recorder.events) == 8
    assert sum(event.kind == "process" for event in recorder.events) == 8


def test_fake_does_not_record_backend_work() -> None:
    recorder = PerformanceRecorder()
    fake = FakeGit(performance=recorder)
    fake.respond(["config"], stdout=b"test.value\nvalue\x00")
    assert config(fake.read_only()) == {"test.value": ["value"]}
    assert recorder.events == ()


def test_read_stopped_before_launch_records_no_events(repo: Git) -> None:
    recorder = PerformanceRecorder()
    reader = ReadOnlyGit(repo.directory, stopping=lambda: True, performance=recorder)
    with pytest.raises(NotStartedError):
        reader.resolve("HEAD")
    assert recorder.events == ()


def test_copy_keeps_recorder_but_unpickling_starts_a_new_one(repo: Git) -> None:
    recorder = PerformanceRecorder()
    reader = ReadOnlyGit(repo.directory, performance=recorder)
    copy.deepcopy(reader).resolve("HEAD")
    assert [event.kind for event in recorder.events] == ["read", "process"]
    restored = pickle.loads(pickle.dumps(reader))
    assert isinstance(restored, ReadOnlyGit)
    assert restored.performance is not None and restored.performance.events == ()


def test_fully_cached_paths_do_not_count_but_partial_cache_misses_do(repo: Git) -> None:
    recorder = PerformanceRecorder()
    reader = ReadOnlyGit(repo.directory, performance=recorder)

    assert git_paths(reader) == []
    (head,) = git_paths(reader, "HEAD")
    assert git_paths(reader, "HEAD") == [head]
    assert reader.object_id_length() == len(repo.run("rev-parse", "HEAD"))
    assert len(git_paths(reader, "HEAD", "index")) == 2
    reader.forget()
    assert git_paths(reader, "HEAD") == [head]

    assert [event.name for event in recorder.events if event.kind == "read"] == ["git_paths"] * 3
    assert sum(event.kind == "process" for event in recorder.events) == 3
