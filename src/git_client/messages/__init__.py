"""What git says that the library reads, in each language git speaks: one file per language
(en.toml, ...), each giving regular expressions for some kinds of message (KINDS). git's
messages are read only with the file for the language git speaks, as gettext chooses it from
git's environment (see language); in another language, nothing is read."""

import functools
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

KINDS = (
    "conflict",
    "push_rejected",
    "remote",
    "config_locked",
    "not_a_repository",
    "unknown_revision",
    "wrong_type",
    "no_identity",
)
"""The kinds of message a language's file may give patterns for (en.toml says what each is)."""


def load(file: Path) -> dict[str, re.Pattern[str]]:
    """For each kind of message that `file` gives patterns for, one pattern (multiline) that
    matches what any of them says. Raises ValueError, naming the file, if it isn't TOML,
    names another kind, or gives one that isn't a list of patterns, each a regular
    expression of its own (not, say, `a)|(b`, nor one with a flag such as `(?i)` for all of
    it: `(?i:...)` sets one for a part) that doesn't match an empty message and has no
    capturing group."""
    return {
        kind: re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.MULTILINE)
        for kind, patterns in _read(file).items()
    }


def _read(file: Path) -> dict[str, list[str]]:
    """What `file` says of each kind of message, checked (see load)."""
    try:
        read = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file.name}: isn't TOML: {error}") from None
    for kind, patterns in read.items():
        if kind not in KINDS:
            raise ValueError(f"{file.name}: {kind} isn't a kind of message (see KINDS)")
        listed = cast(list[object], patterns) if isinstance(patterns, list) else []
        if not listed or not all(isinstance(pattern, str) for pattern in listed):
            raise ValueError(f"{file.name}: {kind}: not a list of patterns")
        for pattern in cast(list[str], listed):
            _check(file, kind, pattern)
    return cast(dict[str, list[str]], read)


def _check(file: Path, kind: str, pattern: str) -> None:
    """Raise ValueError unless `pattern` is one that `load` takes."""
    # On its own (refusing, say, a)|(b, which a group can't hold), then in a group, as load
    # joins it (refusing a flag such as (?i) for all of it).
    try:
        re.compile(pattern)
        compiled = re.compile(f"(?:{pattern})", re.MULTILINE)
    except re.error as error:
        problem = str(error.msg)
    else:
        if compiled.search(""):
            problem = "matches any message"
        elif compiled.groups:
            # Which joining the patterns renumbers (so \1 would mean another's), or names twice.
            problem = "has a capturing group: write (?:...)"
        else:
            return
    raise ValueError(f"{file.name}: {kind}: {pattern!r}: {problem}")


# Where the files are.
_FILES = Path(__file__).parent


def said(environ: Mapping[str, str]) -> Mapping[str, re.Pattern[str]]:
    """For each kind of message, what git says of it in the language it speaks in `environ`
    (the environment it gets): none, if that's a language the library has no file for, or
    can't tell (see language)."""
    name = language(environ)
    return {} if name is None else _said(_FILES, name)


@functools.cache
def _said(files: Path, name: str) -> Mapping[str, re.Pattern[str]]:
    return load(files / f"{name}.toml")


def language(environ: Mapping[str, str]) -> str | None:
    """The file (en, de, pt_BR, ...) for the language git speaks in `environ`, as gettext
    chooses it, or None if the library has no file for it, or can't tell. git speaks English
    with GIT_TEXTDOMAINDIR naming no directory (it then finds no translation), or in the C
    locale: the first of LC_ALL, LC_MESSAGES and LANG that's set (C, C.UTF-8 or POSIX), or,
    on Linux, none. Otherwise, LANGUAGE's first language that git has a translation for, or
    else the locale's. git has none for English, and as the library can't tell which others
    git has, LANGUAGE's other languages (up to a C) must all be one file's. Without a locale, gettext asks macOS or Windows for the system's language, which the
    library doesn't, so there it can't tell.

    It errs toward None, which reads nothing, never toward a wrong file. git without the
    translation installed speaks English while this names the locale's language: its file
    must then not read English as it reads its own words."""
    directory = environ.get("GIT_TEXTDOMAINDIR")
    if directory is not None and not os.path.isdir(directory):
        return "en"
    locale = next(
        (environ[name] for name in ("LC_ALL", "LC_MESSAGES", "LANG") if environ.get(name)), None
    )
    if locale is None:
        return "en" if sys.platform == "linux" else None
    if _is_c(locale):
        return "en"
    names: set[str | None] = set()
    for entry in [entry for entry in environ.get("LANGUAGE", "").split(":") if entry] or [locale]:
        if _is_c(entry):
            break
        if _territory(entry).split("_")[0] != "en":
            names.add(_file(entry))
    if not names:
        return "en"
    return names.pop() if len(names) == 1 else None


def _is_c(locale: str) -> bool:
    return locale in ("C", "POSIX") or locale.startswith("C.")


def _territory(locale: str) -> str:
    """`locale`'s language and territory (e.g. de_AT, from de_AT.UTF-8@euro)."""
    return re.split("[.@]", locale, maxsplit=1)[0]


def _file(locale: str) -> str | None:
    """The file for `locale`: its territory's (e.g. de_AT), else its language's (de), or
    None."""
    territory = _territory(locale)
    for name in (territory, territory.split("_")[0]):
        if (_FILES / f"{name}.toml").is_file():
            return name
    return None
