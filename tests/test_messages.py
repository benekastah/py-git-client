"""The files of git's messages, one per language: each kind of message matches what any
language's file says of it."""

from pathlib import Path

import pytest

from git_client.messages import KINDS, SAID, load


def test_a_kind_of_message_matches_what_any_language_says(tmp_path: Path) -> None:
    (tmp_path / "en.toml").write_text("not_a_repository = ['not a git repository']\n", "utf-8")
    (tmp_path / "xx.toml").write_text("not_a_repository = ['kein Repository']\n", "utf-8")

    said = load([tmp_path / "en.toml", tmp_path / "xx.toml"])["not_a_repository"]

    assert said.search("fatal: not a git repository")
    assert said.search("Schwerwiegend: kein Repository")
    assert not said.search("fatal: something else")


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("not_a_repo = ['x']", "not_a_repo isn't a kind of message"),
        ("conflict = '^CONFLICT'", "conflict: not a list of patterns"),
        ("[conflict]\nx = 1", "conflict: not a list of patterns"),
        ("conflict = []", "conflict: not a list of patterns"),
        ("conflict = ['x', 1]", "conflict: not a list of patterns"),
        ("conflict = ['a)|(b']", "conflict: a\\)\\|\\(b: unbalanced parenthesis"),
        ("conflict = ['(?i)x']", "conflict: \\(\\?i\\)x: sets a flag for every pattern"),
        ("conflict = ['x'", "isn't TOML"),
    ],
)
def test_a_file_that_isnt_a_list_of_patterns_for_each_kind_is_refused(
    tmp_path: Path, text: str, refused: str
) -> None:
    (tmp_path / "xx.toml").write_text(text + "\n", "utf-8")

    with pytest.raises(ValueError, match=f"^xx.toml: {refused}"):
        load([tmp_path / "xx.toml"])


def test_the_library_has_every_kind_of_message() -> None:
    assert set(SAID) == set(KINDS)
