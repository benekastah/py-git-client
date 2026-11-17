"""The files of git's messages, one per language, and which of them is git's."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from git_client import messages
from git_client.messages import KINDS, language, load, said


def test_a_files_patterns_match_what_it_says(tmp_path: Path) -> None:
    (tmp_path / "xx.toml").write_text("not_a_repository = ['kein', 'nicht']\n", "utf-8")

    found = load(tmp_path / "xx.toml")["not_a_repository"]

    assert found.search("Schwerwiegend: kein Repository")
    assert found.search("nicht gefunden")
    assert not found.search("fatal: something else")


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("not_a_repo = ['x']", "not_a_repo isn't a kind of message"),
        ("conflict = '^CONFLICT'", "conflict: not a list of patterns"),
        ("[conflict]\nx = 1", "conflict: not a list of patterns"),
        ("conflict = []", "conflict: not a list of patterns"),
        ("conflict = ['x', 1]", "conflict: not a list of patterns"),
        ("conflict = ['a)|(b']", "conflict: 'a\\)\\|\\(b': unbalanced parenthesis"),
        ("conflict = ['(?i)x']", "conflict: '\\(\\?i\\)x': global flags not at the start"),
        ("conflict = ['(?u)x']", "conflict: '\\(\\?u\\)x': global flags not at the start"),
        ("conflict = ['']", "conflict: '': matches any message"),
        ("conflict = ['x?']", "conflict: 'x\\?': matches any message"),
        ("conflict = ['(?P<n>a)']", "conflict: '\\(\\?P<n>a\\)': has a capturing group"),
        ("conflict = ['(x)\\1']", "conflict: '\\(x\\)\\\\\\\\1': has a capturing group"),
        ("conflict = ['x'", "isn't TOML"),
    ],
)
def test_a_file_that_isnt_a_list_of_patterns_for_each_kind_is_refused(
    tmp_path: Path, text: str, refused: str
) -> None:
    (tmp_path / "xx.toml").write_text(text + "\n", "utf-8")

    with pytest.raises(ValueError, match=f"^xx.toml: {refused}"):
        load(tmp_path / "xx.toml")


def test_english_has_every_kind_of_message() -> None:
    assert set(said({"LANG": "C"})) == set(KINDS)


def test_a_language_with_no_file_has_no_message() -> None:
    assert said({"LANG": "en_US.UTF-8", "LANGUAGE": "xx"}) == {}


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files for English, German and Brazilian Portuguese."""
    for name in ("en", "de", "pt_BR"):
        (tmp_path / f"{name}.toml").write_text("conflict = ['x']\n", "utf-8")
    monkeypatch.setattr(messages, "_FILES", tmp_path)


@pytest.mark.usefixtures("files")
@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        # gettext's C locale (glibc's C.UTF-8 too), which ignores LANGUAGE.
        ({"LANG": "C", "LANGUAGE": "de"}, "en"),
        ({"LANG": "POSIX", "LANGUAGE": "de"}, "en"),
        ({"LANG": "C.UTF-8", "LANGUAGE": "de"}, "en"),
        # The locale: LC_ALL, else LC_MESSAGES, else LANG (an empty one is unset).
        ({"LANG": "de_DE.UTF-8"}, "de"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LC_ALL": "C", "LC_MESSAGES": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}, "en"),
        ({"LC_ALL": "", "LC_MESSAGES": "C", "LANG": "de_DE.UTF-8"}, "en"),
        ({"LC_MESSAGES": "de_DE.UTF-8", "LANG": "C"}, "de"),
        # A territory's file, else its language's; codeset and modifier don't count.
        ({"LANG": "pt_BR.UTF-8"}, "pt_BR"),
        ({"LANG": "de_AT.ISO-8859-1@euro"}, "de"),
        ({"LANG": "pt_PT.UTF-8"}, None),
        ({"LANG": "es_ES.UTF-8"}, None),
        # LANGUAGE, past entries git has no catalog for (English: git's own words)...
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "de"}, "de"),
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "en_GB:en"}, "en"),
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "en:de"}, "de"),
        ({"LANG": "de_DE.UTF-8", "LANGUAGE": ""}, "de"),
        ({"LANG": "de_DE.UTF-8", "LANGUAGE": "C:de"}, "en"),
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "de_DE:de"}, "de"),
        # ... but not past one git may have, whose language the library can't read.
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "es:de"}, None),
        ({"LANG": "en_US.UTF-8", "LANGUAGE": "de:pt_BR"}, None),
        # No translation at all.
        ({"GIT_TEXTDOMAINDIR": os.devnull, "LANG": "de_DE.UTF-8", "LANGUAGE": "de"}, "en"),
    ],
)
def test_gits_language_is_the_one_gettext_chooses(
    environ: Mapping[str, str], expected: str | None
) -> None:
    assert language(environ) == expected


@pytest.mark.usefixtures("files")
def test_a_directory_of_translations_leaves_the_language_to_the_locale(tmp_path: Path) -> None:
    assert language({"GIT_TEXTDOMAINDIR": str(tmp_path), "LANG": "de_DE.UTF-8"}) == "de"


@pytest.mark.parametrize(
    ("platform", "expected"), [("linux", "en"), ("darwin", None), ("win32", None)]
)
def test_with_no_locale_git_speaks_english_only_where_the_system_isnt_asked(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: str | None
) -> None:
    # Off Linux, gettext asks the system for its language (macOS's or Windows' settings).
    monkeypatch.setattr(sys, "platform", platform)

    assert language({"LANGUAGE": "de"}) == expected
