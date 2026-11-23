import copy
import pickle
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import git_client.client
from git_client import Arg, Git, GitCache, ReadOnlyGit, git_dir, git_paths
from git_client.testing import FakeGit, GitCall


def test_metadata_is_read_fresh_without_a_cache(repo: Git, git_calls: list[GitCall]) -> None:
    git = Git(repo.directory)
    assert git.prefix() == git.prefix() == ""
    assert len(git_calls) == 2

    git_calls.clear()
    assert git_paths(git, "HEAD") == git_paths(git, "HEAD")
    assert git_calls == [["rev-parse", "--path-format=absolute", "--git-path", "HEAD"]] * 2


def test_discarding_a_cache_reads_a_replaced_repository_afresh(
    repo: Git, tmp_path: Path, git_calls: list[GitCall]
) -> None:
    directory = Path(repo.directory)
    cache = GitCache()
    assert git_dir(Git(directory, cache=cache)) == directory / ".git"

    # Replace the checkout with a bare repository at the same pathname.
    directory.rename(tmp_path / "old")
    Git(tmp_path).run("init", "--bare", "--quiet", str(directory))
    git_calls.clear()

    assert git_dir(Git(directory, cache=cache)) == directory / ".git"
    assert git_dir(Git(directory, cache=GitCache())) == directory
    assert git_dir(Git(directory)) == directory
    assert len(git_calls) == 2


def test_shared_cache_reuses_answers_across_fresh_clients(
    repo: Git, git_calls: list[GitCall]
) -> None:
    cache = GitCache()
    first = Git(repo.directory, cache=cache)
    second = ReadOnlyGit(repo.directory, cache=cache)
    assert git_paths(first, "HEAD") == git_paths(second, "HEAD")
    assert second.prefix() == "" and second.object_id_length() == 40
    assert len(git_calls) == 1
    cache.clear()
    assert second.prefix() == ""
    assert len(git_calls) == 2


def test_shared_cache_keeps_answers_from_distinct_git_programs_apart() -> None:
    cache = GitCache()
    sha1 = FakeGit(program="git-sha1", cache=cache)
    sha256 = FakeGit(program="git-sha256", cache=cache)
    sha1.respond(["rev-parse", "--show-object-format"], stdout="sha1")
    sha256.respond(["rev-parse", "--show-object-format"], stdout="sha256")

    assert sha1.object_id_length() == 40
    assert sha256.object_id_length() == 64


def test_shared_cache_distinguishes_relative_programs_after_chdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = GitCache()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    repository = tmp_path / "repo"
    first = FakeGit(repository, program="./git", cache=cache)
    second = FakeGit(repository, program="./git", cache=cache)
    first.respond(["rev-parse", "--show-object-format"], stdout="sha1")
    second.respond(["rev-parse", "--show-object-format"], stdout="sha256")

    monkeypatch.chdir(first_dir)
    assert first.object_id_length() == 40
    monkeypatch.chdir(second_dir)
    assert second.object_id_length() == 64


@pytest.mark.skipif(
    sys.platform == "win32", reason="directory symlinks may require elevated privileges"
)
def test_shared_cache_distinguishes_symlink_parent_traversal(repo: Git) -> None:
    root = Path(repo.directory)
    (root / "sub").mkdir()
    (root / "sub" / "inside").mkdir()
    (root / "link").symlink_to(root / "sub" / "inside", target_is_directory=True)

    cache = GitCache()
    assert Git(root, cache=cache).prefix() == ""
    assert Git(root / "link" / "..", cache=cache).prefix() == "sub/"


def test_forget_drops_every_answer(repo: Git, git_calls: list[GitCall]) -> None:
    git = Git(repo.directory, cache=GitCache())
    git_paths(git, "HEAD")
    git.object_id_length()
    git.forget()

    git_paths(git, "HEAD")
    assert len(git_calls) == 2
    assert "--show-prefix" in git_calls[-1]
    assert "--show-object-format" in git_calls[-1]


@pytest.mark.parametrize("forgets", ["client", "reader"])
def test_a_client_and_its_read_only_copy_forget_together(
    repo: Git, git_calls: list[GitCall], forgets: str
) -> None:
    git = Git(repo.directory, cache=GitCache())
    reader = git.read_only()
    git.prefix()
    (git if forgets == "client" else reader).forget()

    reader.prefix()
    git.prefix()  # the reader's answer
    assert len(git_calls) == 2
    assert reader.cache is git.cache


def test_an_answer_git_gave_before_forget_is_not_kept(repo: Git, git_calls: list[GitCall]) -> None:
    class ForgettingGit(Git):
        """Forgets while git answers the first command, as another thread could."""

        def process(self, *args: Arg, **kwargs: Any) -> Any:
            result = super().process(*args, **kwargs)
            if len(git_calls) == 1:
                self.forget()
            return result

    git = ForgettingGit(repo.directory, cache=GitCache())

    assert git.prefix() == ""  # git's answer, for the caller who asked before forget
    git.prefix()
    assert len(git_calls) == 2


def test_threads_get_gits_answers_while_another_forgets(
    repo: Git, git_calls: list[GitCall]
) -> None:
    (Path(repo.directory) / "sub").mkdir()
    git = Git(Path(repo.directory) / "sub", cache=GitCache())
    asking = threading.Barrier(4)
    done = threading.Event()

    def forget() -> None:
        asking.wait()
        while not done.is_set():
            git.forget()

    def ask() -> list[tuple[str, str, Path]]:
        asking.wait()
        return [(git.prefix(), git.cdup(), git_dir(git)) for _ in range(3)]

    with ThreadPoolExecutor(4) as pool:
        forgetting = pool.submit(forget)
        askers = [pool.submit(ask) for _ in range(3)]
        try:
            answers = {answer for asker in askers for answer in asker.result()}
        finally:
            done.set()  # whatever the askers raised, so the forgetter stops
        forgetting.result()

    assert answers == {("sub/", "../", Path(repo.directory) / ".git")}
    assert len(git_calls) <= 27  # at most one process a call


def test_a_fake_can_share_an_explicit_cache() -> None:
    git = FakeGit(cache=GitCache(), object_id_length=40)
    git.respond(["rev-parse"], stdout="\n\ntrue\n/g\n/g\n")
    assert git.prefix() == git.read_only().cdup() == ""
    assert len(git.calls) == 1


def test_a_deep_copy_remembers_nothing_and_forgets_alone(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    git.prefix()
    twin = copy.deepcopy(git)

    assert isinstance(twin.cache, GitCache) and twin.cache is not git.cache
    twin.prefix()  # its own question
    assert len(git_calls) == 2
    twin.forget()
    git.prefix()  # still the client's answer
    assert len(git_calls) == 2


def test_a_pickled_client_comes_back_remembering_nothing(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    git_paths(git, "HEAD")
    back = pickle.loads(pickle.dumps(git))

    assert isinstance(back, Git) and isinstance(back.cache, GitCache)
    assert back.prefix() == ""
    assert len(git_calls) == 2


def prefix_of(git: Git) -> str:
    """For a process pool, which pickles what it's given."""
    return git.prefix()


def test_a_client_with_answers_goes_to_a_process_pool(repo: Git) -> None:
    git = Git(repo.directory, cache=GitCache())
    git.prefix()

    with ProcessPoolExecutor(1) as pool:
        assert pool.submit(prefix_of, git).result() == ""


def test_threads_asking_object_id_length_at_once_take_one_gits_answer(
    repo: Git, git_calls: list[GitCall]
) -> None:
    class SlowGit(Git):
        """Takes long enough to answer that the threads all ask before it does."""

        def process(self, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            time.sleep(0.05)
            return super().process(*args, **kwargs)

    git = SlowGit(repo.directory, cache=GitCache())
    asking = threading.Barrier(4)

    def length(_: int) -> int:
        asking.wait()
        return git.object_id_length()

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(length, range(4))) == [40] * 4
    assert git_calls == [["rev-parse", "--show-object-format"]]


@pytest.mark.parametrize("ask", [git_dir, Git.prefix])
def test_what_another_thread_learns_mid_call_still_answers_the_caller(
    repo: Git, monkeypatch: pytest.MonkeyPatch, ask: Callable[[Git], object]
) -> None:
    git = Git(repo.directory, cache=GitCache())
    asking = git_client.client.asking

    def others_learn_it_meanwhile(client: ReadOnlyGit) -> int:
        """As another thread would, between the caller's look at memory and its question."""
        asked = asking(client)
        git_paths(git.read_only(), "HEAD")  # which asks everything (batch's own `asking`)
        return asked

    monkeypatch.setattr("git_client.client.asking", others_learn_it_meanwhile)

    assert ask(git) == (Path(repo.directory) / ".git" if ask is git_dir else "")


def test_clearing_cache_does_not_block_new_reads_on_an_earlier_query(
    repo: Git, git_calls: list[GitCall]
) -> None:
    old_query_answered = threading.Event()
    release_old_query = threading.Event()

    class SlowGit(Git):
        """Hold the first answer after Git has returned it."""

        def process(self, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            output = super().process(*args, **kwargs)
            old_query_answered.set()
            assert release_old_query.wait(10)
            return output

    cache = GitCache()
    first = SlowGit(repo.directory, cache=cache)
    second = Git(repo.directory, cache=cache)
    with ThreadPoolExecutor(2) as pool:
        earlier = pool.submit(first.object_id_length)
        try:
            assert old_query_answered.wait(5)
            cache.clear()
            assert pool.submit(second.object_id_length).result(timeout=2) == 40
            assert not earlier.done()
        finally:
            release_old_query.set()
        assert earlier.result() == 40

    assert git_calls == [["rev-parse", "--show-object-format"]] * 2
