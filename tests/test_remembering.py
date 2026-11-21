import copy
import pickle
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from git_client import Arg, Git, git_common_dir, git_dir, git_paths
from git_client.testing import FakeGit, GitCall


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The time a client remembers answers by (seconds, as time.monotonic counts them): set
    `clock[0]`, rather than sleep."""
    now = [1000.0]
    monkeypatch.setattr("git_client.client._clock", lambda: now[0])
    return now


def test_an_answer_is_kept_for_remember_for_seconds(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git.prefix()
    clock[0] += 9.9

    assert git.prefix() == ""
    assert len(git_calls) == 1


def test_an_answer_is_asked_again_once_remember_for_is_past(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git.prefix()
    clock[0] += 10

    assert git.prefix() == ""
    assert len(git_calls) == 2
    git.prefix()  # and the new answer is kept
    assert len(git_calls) == 2


def test_the_answers_of_one_rev_parse_are_asked_again_together(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git_paths(git, "HEAD")
    clock[0] += 10

    assert git.prefix() == ""
    assert git_calls[-1] == [
        "rev-parse",
        "--path-format=absolute",
        "--show-prefix",
        "--show-cdup",
        "--is-inside-work-tree",
        "--show-object-format",
        "--git-dir",
        "--git-common-dir",
    ]
    assert (git.cdup(), git.object_id_length(), git_dir(git)) == ("", 40, git_common_dir(git))
    assert len(git_calls) == 2
    # A path is asked again when it's asked for.
    assert git_paths(git, "HEAD") == [Path(repo.directory) / ".git" / "HEAD"]
    assert git_calls[-1] == ["rev-parse", "--path-format=absolute", "--git-path", "HEAD"]


def test_object_id_length_asked_alone_expires_too(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git.object_id_length()
    clock[0] += 10

    assert git.object_id_length() == 40
    assert git_calls == [["rev-parse", "--show-object-format"]] * 2


def test_a_given_object_id_length_never_expires(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10, object_id_length=40)
    git.prefix()
    clock[0] += 10

    assert git.object_id_length() == 40
    git.prefix()
    assert all("--show-object-format" not in call for call in git_calls)


def test_by_default_an_answer_never_expires(
    repo: Git, clock: list[float], git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory)
    git_paths(git, "HEAD")
    clock[0] += 1e9

    git.prefix()
    git.object_id_length()
    git_dir(git)
    git_paths(git, "HEAD")
    assert len(git_calls) == 1


def test_remember_for_0_asks_each_time(repo: Git, git_calls: list[GitCall]) -> None:
    git = Git(repo.directory, remember_for=0)
    git.prefix()
    git.prefix()

    assert len(git_calls) == 2


def test_remember_for_isnt_negative() -> None:
    with pytest.raises(ValueError):
        Git(remember_for=-1)


def test_forget_drops_every_answer(repo: Git, git_calls: list[GitCall]) -> None:
    git = Git(repo.directory)
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
    git = Git(repo.directory, remember_for=10)
    reader = git.read_only()
    git.prefix()
    (git if forgets == "client" else reader).forget()

    reader.prefix()
    git.prefix()  # the reader's answer
    assert len(git_calls) == 2
    assert reader.remember_for == 10


def test_an_answer_git_gave_before_forget_is_not_kept(repo: Git, git_calls: list[GitCall]) -> None:
    class ForgettingGit(Git):
        """Forgets while git answers the first command, as another thread could."""

        def process(self, *args: Arg, **kwargs: Any) -> Any:
            result = super().process(*args, **kwargs)
            if len(git_calls) == 1:
                self.forget()
            return result

    git = ForgettingGit(repo.directory)

    assert git.prefix() == ""  # git's answer, for the caller who asked before forget
    git.prefix()
    assert len(git_calls) == 2


def test_threads_get_gits_answers_while_others_ask_again(
    repo: Git, git_calls: list[GitCall]
) -> None:
    (Path(repo.directory) / "sub").mkdir()
    git = Git(Path(repo.directory) / "sub", remember_for=0)  # each call asks again
    start = threading.Barrier(4)

    def ask(_: int) -> list[tuple[str, str]]:
        start.wait()
        return [(git.prefix(), git.cdup()) for _ in range(2)]

    with ThreadPoolExecutor(4) as pool:
        answers = [answer for answers in pool.map(ask, range(4)) for answer in answers]

    assert set(answers) == {("sub/", "../")}
    assert len(git_calls) <= 16  # at most one process a call


def test_a_fake_remembers_for_remember_for(clock: list[float]) -> None:
    git = FakeGit(remember_for=10, object_id_length=40)
    git.respond(["rev-parse"], stdout="\n\ntrue\n/g\n/g\n")
    git.prefix()
    clock[0] += 10

    git.prefix()
    assert len(git.calls) == 2


def test_a_deep_copy_remembers_nothing_and_forgets_alone(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git.prefix()
    twin = copy.deepcopy(git)

    assert twin.remember_for == 10
    twin.prefix()  # its own question
    twin.forget()
    git.prefix()  # still the client's answer
    assert len(git_calls) == 2


def test_a_pickled_client_comes_back_remembering_nothing(
    repo: Git, git_calls: list[GitCall]
) -> None:
    git = Git(repo.directory, remember_for=10)
    git_paths(git, "HEAD")
    back = pickle.loads(pickle.dumps(git))

    assert back.remember_for == 10
    assert back.prefix() == ""
    assert len(git_calls) == 2


def prefix_of(git: Git) -> str:
    """For a process pool, which pickles what it's given."""
    return git.prefix()


def test_a_client_with_answers_goes_to_a_process_pool(repo: Git) -> None:
    git = Git(repo.directory)
    git.prefix()

    with ProcessPoolExecutor(1) as pool:
        assert pool.submit(prefix_of, git).result() == ""
