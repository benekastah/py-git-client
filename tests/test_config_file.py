"""The config file editor behind Git.write_config, checked against `git config`'s own
edits of a copy of the same file."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from git_client import Git, decode
from git_client.config_file import ConfigChange, ConfigResult, ConfigSet, ConfigUnset, edit

type Case = tuple[bytes | None, list[ConfigChange]]

MAIN = b'[core]\n\tbare = false\n[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
VALUES = [
    *("", " lead", "trail ", "  ", "a b", "h#sh", "s;c", 'q"uote', '"', "back\\slash", "end\\"),
    *("new\nline", "\n", "t\tab", "x = y", "[x]", "ü", "a # b ; c", "\\n"),
]
NAMES = ["fix/a.b", 'q"uote', "back\\slash", "Üñí", "UPPER", "a.b.c", " sp ace ", "#;"]
TWO = b'[branch "m"]\n\tx = 1\n\tx = 2\n'

# Files git can't read.
UNREADABLE = [
    *(b"[branch", b"[a/b]", b'[branch\n"a"]', b"[branch a]", b'[branch "a\n"]', b'[branch "a"x'),
    *(b'[branch "a\\\n"]', b'[branch "a"\n', b'[branch "a"]\n\tx y\n', b"!"),
]
CASES: dict[str, Case] = {
    "a new key": (MAIN, [ConfigSet("branch.main.base", "x")]),
    "a new value": (MAIN, [ConfigSet("branch.main.remote", "upstream")]),
    "a new section": (MAIN, [ConfigSet("branch.topic.base", "main")]),
    "an unset": (MAIN, [ConfigUnset("branch.main.merge")]),
    "the last unsets": (
        MAIN,
        [ConfigUnset("branch.main.remote"), ConfigUnset("branch.main.merge")],
    ),
    "the last unset, mid-file": (
        b'[branch "a"]\n\tx = 1\n\n[core]\n\tbare = false\n',
        [ConfigUnset("branch.a.x")],
    ),
    "nothing to unset": (MAIN, [ConfigUnset("branch.main.no"), ConfigUnset("branch.no.x", "1")]),
    "keys typed in any case": (
        MAIN,
        [
            ConfigSet("Branch.main.Remote", "u"),
            ConfigSet("BRANCH.New.Key", "v"),
            ConfigUnset("branch.main.MERGE"),
        ],
    ),
    "a subsection's case counts": (
        MAIN,
        [ConfigSet("branch.Main.remote", "u"), ConfigUnset("branch.MAIN.merge")],
    ),
    "several values": (
        TWO,
        [
            ConfigUnset("branch.m.x"),
            ConfigUnset("branch.m.x", "3"),
            ConfigUnset("branch.m.x", "2"),
            ConfigSet("branch.m.x", "4"),
            ConfigUnset("branch.m.x", "4"),
        ],
    ),
    "several equal values": (b'[branch "m"]\n\tx = 1\n\tx = 1\n', [ConfigUnset("branch.m.x", "1")]),
    "a key with no value": (
        b'[branch "v"]\n\tflag\n\tx = 2\n\tother\n',
        [
            ConfigUnset("branch.v.x", "2"),
            ConfigUnset("branch.v.other", "any"),
            ConfigUnset("branch.v.flag"),
        ],
    ),
    "comments": (
        b'# top\n[branch "c"]\n\tx = 1 ; trailing\n# inside\n\ty = 2\n',
        [ConfigUnset("branch.c.x"), ConfigUnset("branch.c.y"), ConfigSet("branch.c.z", "3")],
    ),
    "a comment before the section": (
        b'[core]\n\tbare = false\n# about c\n[branch "c"]\n\tx = 1\n',
        [ConfigUnset("branch.c.x")],
    ),
    "a comment after the section": (
        b'[branch "c"]\n\tx = 1\n; about core\n[core]\n',
        [ConfigUnset("branch.c.x")],
    ),
    "odd spacing": (
        b'\n  [branch  "s"]  \n\n   x=1\n\n\ty   =   2   \n\n[core]\n',
        [ConfigSet("branch.s.x", "one"), ConfigUnset("branch.s.y"), ConfigSet("branch.s.z", "3")],
    ),
    "a section in two places": (
        b'[branch "d"]\n\tx = 1\n[core]\n\tbare = false\n[branch "d"]\n\ty = 2\n[branch "e"]\n',
        [
            ConfigSet("branch.d.z", "3"),
            ConfigUnset("branch.d.x"),
            ConfigUnset("branch.d.y"),
            ConfigUnset("branch.d.z"),
        ],
    ),
    "continued and quoted values": (
        b'[branch "l"]\n\tx = one \\\n two\n\ty = "a ; b" # c\n\tz = 3\n',
        [
            ConfigSet("branch.l.x", "1"),
            ConfigUnset("branch.l.y", "a ; b"),
            ConfigUnset("branch.l.z"),
        ],
    ),
    "no newline at the end": (
        b'[branch "a"]\n\tx = 1',
        [ConfigSet("branch.a.y", "2"), ConfigSet("branch.b.x", "3")],
    ),
    "a header at the end": (b'[core]\n[branch "e"]', [ConfigSet("branch.e.x", "1")]),
    "an empty section": (
        b'[branch "e"]\n[core]\n',
        [ConfigSet("branch.e.x", "1"), ConfigUnset("branch.e.x")],
    ),
    "an empty file": (b"", [ConfigUnset("branch.a.x"), ConfigSet("branch.a.x", "1")]),
    "no file": (
        None,
        [ConfigUnset("branch.a.x"), ConfigSet("Branch.a.X", "1"), ConfigSet("branch.b.y", "2")],
    ),
    "no file, nothing set": (None, [ConfigUnset("branch.a.x")]),
    "mixed line endings": (
        b'[branch "a"]\r\n\tx = 1\r\n\r\n\ty = 2\n',
        [ConfigSet("branch.a.z", "3"), ConfigUnset("branch.a.y")],
    ),
    "an include": (b"[include]\n\tpath = other\n", [ConfigSet("branch.i.x", "1")]),
    "a byte order mark": (
        b'\xef\xbb\xbf[branch "b"]\n\tx = 1\n',
        [ConfigSet("branch.b.y", "2"), ConfigUnset("branch.b.x")],
    ),
    "headers written otherwise": (
        b'[Branch "a"]\n\tx = 1\n[branch\t"q\\"x"]\n\tx = 1\n[branch.other]\n\tx = 1\n',
        [ConfigSet("branch.a.y", "2"), ConfigSet('branch.q"x.y', "2"), ConfigUnset('branch.q"x.x')],
    ),
    "tricky names": (
        MAIN,
        [ConfigSet(f"branch.{name}.{key}", name) for name in NAMES for key in ("x", "y")]
        + [ConfigUnset(f"branch.{name}.x") for name in NAMES],
    ),
    "tricky values": (MAIN, [ConfigSet(f"branch.v.k{i}", value) for i, value in enumerate(VALUES)]),
    "tricky values replaced": (
        MAIN,
        [ConfigSet("branch.v.k", value) for value in VALUES]
        + [ConfigUnset("branch.v.k", VALUES[-1])],
    ),
}


def git_edits(
    directory: Path, contents: bytes | None, changes: Sequence[ConfigChange]
) -> tuple[bytes | None, list[ConfigResult]]:
    """What sequential `git config --file` calls make of a file, and what each did."""
    path = directory / "config-by-git"
    if contents is not None:
        path.write_bytes(contents)
    results: list[ConfigResult] = []
    for change in changes:
        if isinstance(change, ConfigSet):
            args = [change.key, change.value]
        else:
            args = ["--unset", *(["--fixed-value"] if change.value is not None else [])]
            args += [change.key] + ([change.value] if change.value is not None else [])
        done = Git(directory).process("config", "--file", str(path), *args, capture_output=True)
        if done.returncode == 0:
            results.append(ConfigResult.CHANGED)
        else:
            assert done.returncode == 5, decode(done.stderr)
            several = b"has multiple values" in done.stderr
            results.append(ConfigResult.SEVERAL_VALUES if several else ConfigResult.NOT_FOUND)
    return (path.read_bytes() if path.exists() else None), results


@pytest.mark.parametrize(("contents", "changes"), CASES.values(), ids=CASES.keys())
def test_edit_makes_the_file_git_config_makes(
    tmp_path: Path, contents: bytes | None, changes: list[ConfigChange]
) -> None:
    assert edit(contents, changes) == git_edits(tmp_path, contents, changes)


def test_edit_keeps_a_files_crlf_line_endings_where_git_writes_lf(tmp_path: Path) -> None:
    contents = b'[core]\r\n\tbare = false\r\n\r\n[branch "w"]\r\n\tx = 1\r\n\r\n\ty = 2'
    changes: list[ConfigChange] = [
        ConfigSet("branch.w.x", "one"),
        ConfigSet("branch.w.z", "3"),
        ConfigUnset("branch.w.y"),
        ConfigSet("branch.new.x", "cr\r"),
    ]
    by_git, git_results = git_edits(tmp_path, contents.replace(b"\r\n", b"\n"), changes)
    assert by_git is not None

    assert edit(contents, changes) == (by_git.replace(b"\n", b"\r\n"), git_results)


def test_edit_writes_values_git_reads_back(tmp_path: Path) -> None:
    values = [*VALUES, "cr\rx", "cr at the end\r"]
    edited = edit(MAIN, [ConfigSet(f"branch.v.k{i}", value) for i, value in enumerate(values)])
    assert edited is not None
    (tmp_path / "config").write_bytes(edited[0] or b"")

    listed = Git(tmp_path).run_bytes("config", "--file", "config", "--list", "-z")

    assert [decode(entry).partition("\n")[2] for entry in listed.split(b"\0")[3:-1]] == values


@pytest.mark.parametrize(
    ("contents", "change"),
    [
        (MAIN, ConfigSet("core.bare", "true")),
        (MAIN, ConfigUnset("branch.main")),
        (MAIN, ConfigSet("branch.main.1x", "v")),
        (MAIN, ConfigSet("branch..x", "v")),
        (MAIN, ConfigSet("branch.a\nb.x", "v")),
        (MAIN, ConfigSet("branch.a.x", "v\0")),
        # git reads a value only up to a NUL.
        (b'[branch "a"]\n\tx = a\0b\n', ConfigUnset("branch.a.x", "a")),
        *((bad, ConfigSet("branch.a.x", "v")) for bad in UNREADABLE),
        (b'[branch "a"]\n\tx = "open\n', ConfigSet("branch.a.x", "v")),
        (b'[branch "a"]\n\tx = \\q\n', ConfigSet("branch.b.x", "v")),
        # git puts a key in a [section.subsection] header whatever its subsection's case.
        (b"[branch.a]\n\tx = 1\n", ConfigSet("branch.a.y", "v")),
        (b"[branch.a]\n\tx = 1\n", ConfigSet("branch.A.y", "v")),
        (b'[branch.a "b"]\n\tx = 1\n', ConfigSet("branch.a.b.y", "v")),
        (b'[branch "a"] x = 1\n', ConfigSet("branch.a.y", "v")),
        (b'[core] [branch "a"]\n\tx = 1\n', ConfigSet("branch.a.y", "v")),
        (b'[branch "a"] # c\n\tx = 1\n', ConfigUnset("branch.a.x")),
        (TWO, ConfigSet("branch.m.x", "v")),
        # git before 2.45 reads a tab inside a value as a space.
        (b'[branch "a"]\n\tx = a\tb\n', ConfigUnset("branch.a.x", "a b")),
    ],
)
def test_edit_leaves_to_git_what_it_cant_be_sure_of(contents: bytes, change: ConfigChange) -> None:
    assert edit(contents, [ConfigSet("branch.ok.x", "1"), change]) is None
