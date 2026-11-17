"""The files of git's messages, one per language: each kind of message matches what any
language's file says of it."""

from pathlib import Path

import pytest

from git_client.messages import KINDS, load


def test_a_kind_of_message_matches_what_any_language_says(tmp_path: Path) -> None:
    (tmp_path / "en.toml").write_text("not_a_repository = ['not a git repository']\n", "utf-8")
    (tmp_path / "xx.toml").write_text("not_a_repository = ['kein Repository']\n", "utf-8")

    said = load([tmp_path / "en.toml", tmp_path / "xx.toml"])["not_a_repository"]

    assert said.search("fatal: not a git repository")
    assert said.search("Schwerwiegend: kein Repository")
    assert not said.search("fatal: something else")


def test_a_file_naming_no_kind_of_message_is_refused(tmp_path: Path) -> None:
    (tmp_path / "xx.toml").write_text("not_a_repo = ['kein Repository']\n", "utf-8")

    with pytest.raises(ValueError, match="xx.toml: not_a_repo"):
        load([tmp_path / "xx.toml"])


def test_english_says_every_kind_of_message() -> None:
    files = Path(__file__).parents[1] / "src" / "git_client" / "messages"

    english = load([files / "en.toml"])

    assert set(english) == set(KINDS)
    assert all(said.pattern for said in english.values())
