"""What git says that the library reads, in each language git speaks: one file per language
(en.toml, ...), each giving regular expressions for some kinds of message (KINDS). A kind of
message matches what any language's file says of it, since git's language can't be told
reliably from the environment (LANGUAGE, a translation that isn't installed)."""

import re
import tomllib
from collections.abc import Iterable
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


def load(files: Iterable[Path]) -> dict[str, re.Pattern[str]]:
    """For each kind of message that `files` give patterns for, one pattern (multiline) that
    matches what any of them says. Raises ValueError, naming the file, for one that isn't
    TOML, names another kind, or gives one that isn't a list of patterns, each a regular
    expression of its own (not, say, `a)|(b`, nor one with a flag such as `(?i)` for all of
    it: `(?i:...)` sets one for a part) that doesn't match an empty message and has no
    capturing group."""
    said: dict[str, list[str]] = {}
    for file in files:
        for kind, patterns in _read(file).items():
            said.setdefault(kind, []).extend(patterns)
    return {
        kind: re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.MULTILINE)
        for kind, patterns in said.items()
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


# Every language's file is tried, which is fine for a few. With more, pick the file for the
# language git speaks instead (e.g. by asking git, once, for a message in it).
SAID = load(sorted(Path(__file__).parent.glob("*.toml")))
"""For each of KINDS, what git says of it in any language the library has a file for."""
