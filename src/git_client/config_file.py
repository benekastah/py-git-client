"""The changes `Git.write_config` makes to a repository's config, what each did, and
how to make them as `git config` does, so that a batch takes no git process: a port of git's
own reader and writer of config files (config.c, the same from git 2.36 to 2.56 but where
said). It edits only keys of `[branch "<name>"]` sections, and leaves to git (`edit` gives
None) what it can't be sure it edits as git would.

On two points, deliberately, the file isn't byte for byte what `git config` would write: in
a file whose every line ends in CRLF, the lines written end in CRLF too (git writes LF); and
a value with a carriage return is quoted, as git quotes it only from 2.51 (older git can't
read back the value it writes)."""

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSet:
    """For `Git.write_config`: set `key` to `value`, as `git config <key> <value>`."""

    key: str
    value: str


@dataclass(frozen=True)
class ConfigUnset:
    """For `Git.write_config`: remove `key`, as `git config --unset <key>`; with a
    `value`, only that value of it, as `git config --unset --fixed-value <key> <value>` (for
    a key with several values)."""

    key: str
    value: str | None = None


type ConfigChange = ConfigSet | ConfigUnset


class ConfigResult(enum.Enum):
    """What one change of `Git.write_config` did."""

    CHANGED = "changed"
    """It was made. A set always is, even to the value the key had, which git writes again."""
    NOT_FOUND = "not found"
    """An unset of a key, or of a value, that isn't set: nothing to remove."""
    SEVERAL_VALUES = "several values"
    """An unset of a key with several values (or of a value it has more than once), which git
    refuses, leaving them all: give the value to remove."""


# The keys edited here: a branch's, `branch.<name>.<key>`, as git takes them: a name with no
# line ending, and a key of a letter, then letters, digits and dashes.
_BRANCH_KEY = re.compile(r"[Bb][Rr][Aa][Nn][Cc][Hh]\.[^\n]+\.[A-Za-z][A-Za-z0-9-]*")
_BOM = b"\xef\xbb\xbf"
_NL, _CR, _TAB, _SP = 0x0A, 0x0D, 0x09, 0x20
# git's own ctype (ctype.c), not the locale's.
_SPACE = frozenset(b" \t\n\r")
_ALPHA = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_KEYCHAR = _ALPHA | frozenset(b"0123456789-")
_ESCAPES = {ord("t"): _TAB, ord("b"): 0x08, ord("n"): _NL, ord("\\"): ord("\\"), ord('"'): ord('"')}


class _Kind(enum.Enum):
    WHITESPACE = enum.auto()
    COMMENT = enum.auto()
    SECTION = enum.auto()
    ENTRY = enum.auto()


@dataclass
class _Event:
    """What git's parser reports of a file (`git_parse_source`'s events): from `begin` to
    where the next starts. A section's `name` is its section and subsection as git names them
    (`branch.Topic`), `odd` unless written `[branch "<name>"]`. An entry's `name` is its key
    as git names it, its `value` None for a key with no `=`, and `odd` if its value has a tab
    or a carriage return inside it, which git before 2.45 reads as a space."""

    kind: _Kind
    begin: int
    end: int = 0
    name: bytes = b""
    value: bytes | None = None
    odd: bool = False


class _Reader:
    """A config file, read a character at a time as git reads it (`get_next_char`): CRLF as
    LF, and the end as an LF, setting `eof`."""

    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos
        self.eof = False

    def getc(self) -> int:
        data, pos = self.data, self.pos
        if pos >= len(data):
            self.eof = True
            return _NL
        c = data[pos]
        pos += 1
        if c == _CR and pos < len(data) and data[pos] == _NL:
            c = _NL
            pos += 1
        self.pos = pos
        return c


def _parse(data: bytes) -> list[_Event] | None:
    """The events git's parser reports of a file, or None where git can't read it."""
    events: list[_Event] = []
    reader = _Reader(data, len(_BOM) if data.startswith(_BOM) else 0)
    current: _Event | None = None
    section = b""
    comment = False

    def start(kind: _Kind) -> _Event:
        """git's do_event: end the event before, where this one starts."""
        if current is not None:
            current.end = reader.pos - 1
            events.append(current)
        return _Event(kind, reader.pos - 1)

    while True:
        c = reader.getc()
        if c == _NL and reader.eof:
            if current is not None:
                current.end = reader.pos
                events.append(current)
            return events
        if c == _NL:
            comment = False
        if comment:
            continue
        if c in _SPACE:
            if current is None or current.kind is not _Kind.WHITESPACE:
                current = start(_Kind.WHITESPACE)
        elif c in b"#;":
            current = start(_Kind.COMMENT)
            comment = True
        elif c == ord("["):
            current = start(_Kind.SECTION)
            header = _header(reader)
            if header is None:
                return None
            section, current.odd = header
            current.name = section
        elif c in _ALPHA:
            current = start(_Kind.ENTRY)
            entry = _entry(reader, c)
            if entry is None:
                return None
            name, current.value, current.odd = entry
            current.name = section + b"." + name
        else:
            return None


def _header(reader: _Reader) -> tuple[bytes, bool] | None:
    """A section header's name, after its `[`, and whether it's `odd` (see _Event); None if
    git can't read it (`get_base_var`)."""
    base = bytearray()
    while True:
        c = reader.getc()
        if reader.eof:
            return None
        if c == ord("]"):
            return bytes(base).lower(), True
        if c in _SPACE:
            break
        if c not in _KEYCHAR and c != ord("."):
            return None
        base.append(c)
    while c in _SPACE:
        if c == _NL:
            return None
        c = reader.getc()
    if c != ord('"'):
        return None
    subsection = bytearray()
    while (c := reader.getc()) != ord('"'):
        if c == ord("\\"):
            c = reader.getc()
        if c == _NL:
            return None
        subsection.append(c)
    if reader.getc() != ord("]"):
        return None
    name = bytes(base).lower()
    return name + b"." + bytes(subsection), name != b"branch"


def _entry(reader: _Reader, first: int) -> tuple[bytes, bytes | None, bool] | None:
    """An entry's name, value, and whether it's `odd` (see _Event), from its name's first
    character; None if git can't read it (`get_value` and `parse_value`)."""
    name = bytearray([first])
    while True:
        c = reader.getc()
        if reader.eof or c not in _KEYCHAR:
            break
        name.append(c)
    while c in (_SP, _TAB):
        c = reader.getc()
    if c == _NL:
        return bytes(name).lower(), None, False
    if c != ord("="):
        return None
    value = bytearray()
    quote = comment = tab = odd = False
    trim = 0
    while True:
        c = reader.getc()
        if c == _NL:
            if quote:
                return None
            if trim:
                del value[trim:]
            return bytes(name).lower(), bytes(value), odd
        if comment:
            continue
        if c in _SPACE and not quote:
            if not trim:
                trim = len(value)
            if value:
                value.append(c)
                tab = tab or c != _SP
            continue
        if not quote and c in b"#;":
            comment = True
            continue
        trim = 0
        odd = odd or tab
        tab = False
        if c == ord("\\"):
            c = reader.getc()
            if c == _NL:
                continue  # a line continued
            if c not in _ESCAPES:
                return None
            value.append(_ESCAPES[c])
        elif c == ord('"'):
            quote = not quote
        else:
            value.append(c)


def edit(
    contents: bytes | None, changes: Sequence[ConfigChange]
) -> tuple[bytes | None, list[ConfigResult]] | None:
    """A config file's `contents` (None: no file) with `changes` made in order, and what each
    did, as `git config` makes them: the same bytes, but for a file whose every line ends in
    CRLF (with no other carriage return), which is edited as git edits it with LF endings,
    then given CRLF back (git writes LF lines into it). None, for git to make them, if any
    change is of anything but a valid `branch.<name>.<key>`, or has a NUL; if git can't read
    the file, or it has a NUL; for a set of a key with several values (which git refuses);
    and where git would edit a branch's section otherwise than here: if a header of the
    branch's has more than whitespace beside it on its line, or is written otherwise than
    `[branch "<name>"]` (e.g. `[branch.<name>]`, which git takes for the branch whatever the
    case of its name), or for an unset of a value that git before 2.45 reads otherwise (see
    _Event)."""
    crlf = False
    if contents is not None:
        if b"\0" in contents:
            return None
        lines = contents.count(b"\n")
        crlf = lines > 0 and contents.count(b"\r\n") == contents.count(b"\r") == lines
        contents = contents.replace(b"\r\n", b"\n") if crlf else contents
    events = None if contents is None else _parse(contents)
    if contents is not None and events is None:
        return None
    results: list[ConfigResult] = []
    for change in changes:
        value = change.value
        if not _BRANCH_KEY.fullmatch(change.key) or "\0" in change.key + (value or ""):
            return None
        made = _change(
            contents,
            events,
            change.key.encode("utf-8", "surrogateescape"),
            None if value is None else value.encode("utf-8", "surrogateescape"),
            isinstance(change, ConfigSet),
        )
        if made is None:
            return None
        contents, events, result = made
        results.append(result)
    return (contents.replace(b"\n", b"\r\n") if crlf and contents else contents), results


def _change(
    contents: bytes | None,
    events: list[_Event] | None,
    key: bytes,
    value: bytes | None,
    setting: bool,
) -> tuple[bytes | None, list[_Event] | None, ConfigResult] | None:
    """`contents` (and its `events`) with one change made, as git's
    `repo_config_set_multivar_in_file_gently` makes it: a set of `key` to `value` if
    `setting`, else an unset of it (of `value` only, if given). None if it's git's to make
    (see edit)."""
    dot, baselen = key.index(b"."), key.rindex(b".")
    section = key[:dot].lower() + key[dot:baselen]
    wanted = section + key[baselen:].lower()
    if contents is None or events is None:
        if value is None or not setting:
            return None, None, ConfigResult.NOT_FOUND
        written = _section_header(key, baselen) + _pair(key, baselen, value)
        return written, _parse(written), ConfigResult.CHANGED
    seen: list[int] = []  # the key's entries (git's store.seen)
    last = None  # the last of the section's headers, and entries before the key's
    keys_sections: set[int] = set()
    in_section = False
    for i, event in enumerate(events):
        if event.kind is _Kind.SECTION:
            if event.odd and event.name.lower() == section.lower():
                return None
            in_section = event.name == section
            if in_section:
                if not _alone(contents, events, i):
                    return None
                keys_sections.add(i)
                last = i
        elif event.kind is _Kind.ENTRY:
            ours = event.name == wanted
            if ours and value is not None and not setting and event.odd:
                return None
            if in_section and not seen:
                last = i
            # A value given to unset matches it, and a key with no value.
            matches = setting or value is None or event.value in (None, value)
            if ours and matches and (seen or in_section):
                seen.append(i)
    if not seen and not setting:
        return contents, events, ConfigResult.NOT_FOUND
    if len(seen) > 1:
        return None if setting else (contents, events, ConfigResult.SEVERAL_VALUES)
    if seen:
        copy_end, replace_end = events[seen[0]].begin, events[seen[0]].end
        if not setting:
            copy_end, replace_end = _section_removed(
                events, seen[0], keys_sections, copy_end, replace_end
            )
        # The whitespace before it on its line.
        while copy_end > 0 and contents[copy_end - 1] in (_SP, _TAB, _CR):
            copy_end -= 1
    else:
        j = last if last is not None else len(events) - 1
        copy_end = events[j].end if j >= 0 else 0
        # A header's line ending.
        if (
            0 < copy_end < len(contents)
            and contents[copy_end - 1] != _NL
            and contents[copy_end] == _NL
        ):
            copy_end += 1
        replace_end = copy_end
    written = contents[:copy_end]
    if copy_end > 0 and contents[copy_end - 1] != _NL:
        written += b"\n"
    if setting:
        assert value is not None
        if last is None:
            written += _section_header(key, baselen)
        written += _pair(key, baselen, value)
    written += contents[replace_end:]
    spliced = _spliced(events, len(contents), copy_end, replace_end, written)
    return None if spliced is None else (written, spliced, ConfigResult.CHANGED)


def _spliced(
    events: list[_Event], size: int, start: int, end: int, contents: bytes
) -> list[_Event] | None:
    """The events of `contents`, made from a file of `size` bytes and `events` by replacing
    its bytes from `start` to `end`: parsing again only from the section header before
    `start` to the first at or after `end`, where git's parser starts afresh (None if it
    can't, as it always can here)."""
    first = 0
    last = len(events)
    for i, event in enumerate(events):
        if event.kind is _Kind.SECTION and event.begin < start:
            first = i
        elif event.kind is _Kind.SECTION and event.begin >= end:
            last = i
            break
    begin = events[first].begin if first else 0
    stop = events[last].begin if last < len(events) else size
    delta = len(contents) - size
    middle = _parse(contents[begin : stop + delta])
    if middle is None:
        return None
    for event in middle:
        event.begin += begin
        event.end += begin
    for event in events[last:]:
        event.begin += delta
        event.end += delta
    return events[:first] + middle + events[last:]


def _alone(contents: bytes, events: list[_Event], i: int) -> bool:
    """Whether the section header that's `events[i]` has only whitespace beside it on its
    line."""
    before = contents[: events[i].begin]
    after = events[i + 1] if i + 1 < len(events) else None
    return not before.rpartition(b"\n")[2].removeprefix(_BOM).strip(b" \t") and (
        after is None
        or (after.kind is _Kind.WHITESPACE and b"\n" in contents[after.begin : after.end])
    )


def _section_removed(
    events: list[_Event], i: int, keys_sections: set[int], begin: int, end: int
) -> tuple[int, int]:
    """Where an unset of the entry `events[i]` cuts the file: from `begin` to `end`, or, if
    it's its section's last entry, with no comment in or around the section, the whole
    section (git's maybe_remove_section)."""
    section_seen = False
    j = i
    while j > 0:
        kind = events[j - 1].kind
        if kind is _Kind.COMMENT or (kind is _Kind.ENTRY and not section_seen):
            return begin, end
        if kind is _Kind.ENTRY or (kind is _Kind.SECTION and j - 1 not in keys_sections):
            break
        section_seen = section_seen or kind is _Kind.SECTION
        j -= 1
    new_begin = events[j].begin
    j = i + 1
    while j < len(events):
        kind = events[j].kind
        if kind is _Kind.COMMENT or kind is _Kind.ENTRY:
            return begin, end
        if kind is _Kind.SECTION and j not in keys_sections:
            break
        j += 1
    return new_begin, events[j].begin if j < len(events) else events[-1].end


def _section_header(key: bytes, baselen: int) -> bytes:
    """A new section's header, as git's store_create_section writes it: the section as `key`
    has it, whatever its case."""
    dot = key.index(b".")
    subsection = key[dot + 1 : baselen].replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b"[" + key[:dot] + b' "' + subsection + b'"]\n'


def _pair(key: bytes, baselen: int, value: bytes) -> bytes:
    """An entry, as git's write_pair writes it: the key's name as `key` has it, and the value
    quoted where it starts or ends with a space or has a comment's character (or a carriage
    return, which git quotes from 2.51), with `\\`, `"`, newlines and tabs escaped."""
    edges = value[:1] == b" " or value[-1:] == b" "
    quote = b'"' if edges or any(c in value for c in b";#\r") else b""
    escaped = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    escaped = escaped.replace(b"\n", b"\\n").replace(b"\t", b"\\t")
    return b"\t" + key[baselen + 1 :] + b" = " + quote + escaped + quote + b"\n"
