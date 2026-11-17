"""Where a repository keeps its files, read from them without running git."""

import os
from pathlib import Path


def git_common_dir(directory: str | os.PathLike[str]) -> Path | None:
    """The directory holding a repository's refs, objects and config (as `git rev-parse
    --git-common-dir` finds it), read from the files, without starting git. `directory` is the
    top of a checkout, or a git directory itself: for a checkout whose `.git` is a directory,
    that one; for one whose `.git` is a file (`gitdir: <path>`: a linked worktree's, or a
    submodule's), the git directory it names, or the main one that directory's `commondir`
    names (a linked worktree's); for a git directory (e.g. a bare repository), it, or the one
    its `commondir` names. The path is absolute, with symbolic links resolved.

    None when `directory` is none of those: not a repository, or below the top of a checkout
    (unlike git, it doesn't look in the directories above), or a `.git` file that names no
    git directory. It doesn't read git's variables (e.g. GIT_DIR, GIT_COMMON_DIR). Raises
    OSError if a file it needs can't be read (e.g. PermissionError)."""
    found = _git_directories(directory)
    return found[1].resolve() if found else None


def git_dir(directory: str | os.PathLike[str]) -> Path | None:
    """A checkout's own git directory (as `git rev-parse --absolute-git-dir` finds it), read
    from the files, without starting git: the counterpart of `git_common_dir`. They differ
    only for a linked worktree, whose own git directory (its HEAD, index and rebase state) is
    the one its `.git` file names, not the main repository's. For a checkout whose `.git` is a
    directory, that one; for a git directory, it. The path is absolute, with symbolic links
    resolved. It returns None, and raises OSError, in the same cases as `git_common_dir`."""
    found = _git_directories(directory)
    return found[0].resolve() if found else None


def _git_directories(directory: str | os.PathLike[str]) -> tuple[Path, Path] | None:
    """`directory`'s own git directory and its common one (see git_common_dir), or None."""
    top = Path(directory)
    dot_git = top / ".git"
    if dot_git.is_file():
        line = _read_path(dot_git)
        if not line.startswith(b"gitdir: "):
            return None
        own = top / os.fsdecode(line.removeprefix(b"gitdir: "))
    elif dot_git.is_dir():
        own = dot_git
    else:
        own = top
    commondir = own / "commondir"
    common = own / os.fsdecode(_read_path(commondir)) if commondir.is_file() else own
    return (own, common) if _is_git_directory(own, common) else None


def _read_path(file: Path) -> bytes:
    """A file git wrote a path in (the whole file), without its line ending: as git reads it,
    only "\\n" and "\\r" are taken off, so a path ending in a space keeps it."""
    return file.read_bytes().rstrip(b"\r\n")


def _is_git_directory(own: Path, common: Path) -> bool:
    """Whether `own` (with its common directory, `common`) is a git directory, as git checks
    it: a HEAD file in `own`, and objects and refs directories in `common` (refs too in a
    reftable repository, which keeps its refs in reftable)."""
    return (own / "HEAD").is_file() and (common / "objects").is_dir() and (common / "refs").is_dir()
