"""What git says that the library reads, in each language git speaks: one file per language
(en.toml, ...), each giving regular expressions for some kinds of message (KINDS). A kind of
message matches what any language's file says of it, since git's language can't be told
reliably from the environment (LANGUAGE, a translation that isn't installed)."""

import re
import tomllib
from collections.abc import Iterable
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

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


def load(files: Iterable[Traversable | Path]) -> dict[str, re.Pattern[str]]:
    """For each kind of message that `files` give patterns for, one pattern (multiline) that
    matches what any of them says. Raises ValueError for a file that names another kind."""
    said: dict[str, list[str]] = {}
    for file in files:
        for kind, patterns in tomllib.loads(file.read_text(encoding="utf-8")).items():
            if kind not in KINDS:
                raise ValueError(f"{file.name}: {kind} isn't a kind of message (see KINDS)")
            said.setdefault(kind, []).extend(patterns)
    return {
        kind: re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.MULTILINE)
        for kind, patterns in said.items()
    }


# Every language's file is tried, which is fine for a few. With more, pick the file for the
# language git speaks instead (e.g. by asking git, once, for a message in it).
SAID = load(
    sorted(
        (file for file in resources.files(__name__).iterdir() if file.name.endswith(".toml")),
        key=lambda file: file.name,
    )
)
"""For each of KINDS, what git says of it in any language the library has a file for."""
