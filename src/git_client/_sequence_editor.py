"""The sequence editor `rebase(stop_at=...)` gives `git rebase --interactive`: it marks each
given commit's pick line "edit", so the rebase stops there. It runs as a script, apart from
the library, so it imports nothing of it.

Usage: python _sequence_editor.py <full commit id>... <todo file>

It exits 1, changing nothing, if a commit has no pick line, and git then stops the rebase."""

import sys
from collections.abc import Sequence


def mark_edit(todo: bytes, commits: Sequence[str]) -> tuple[bytes, set[str]]:
    """`todo` with each commit's pick line ("pick" or "p", then its abbreviated id, which is
    unique in the repository, so the one full id it begins is its commit's) made an edit
    line, and the commits it found none for."""
    missing = set(commits)
    lines = todo.splitlines(keepends=True)
    for index, line in enumerate(lines):
        words = line.split(maxsplit=2)
        if len(words) < 2 or words[0] not in (b"pick", b"p"):
            continue
        if found := {commit for commit in missing if commit.encode().startswith(words[1])}:
            lines[index] = b"edit" + line[line.index(words[0]) + len(words[0]) :]
            missing -= found
    return b"".join(lines), missing


def main(argv: Sequence[str]) -> int:
    *commits, path = argv
    with open(path, "rb") as todo:
        marked, missing = mark_edit(todo.read(), commits)
    if missing:
        print(f"no pick line for {', '.join(sorted(missing))} in the rebase", file=sys.stderr)
        return 1
    with open(path, "wb") as todo:
        todo.write(marked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
