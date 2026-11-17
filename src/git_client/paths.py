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
    top = Path(directory)
    dot_git = top / ".git"
    if dot_git.is_file():
        line = dot_git.read_bytes().rstrip()
        if not line.startswith(b"gitdir: "):
            return None
        own = top / os.fsdecode(line.removeprefix(b"gitdir: "))
    elif dot_git.is_dir():
        own = dot_git
    else:
        own = top
    commondir = own / "commondir"
    common = own / os.fsdecode(commondir.read_bytes().rstrip()) if commondir.is_file() else own
    return common.resolve() if _is_git_directory(common) else None


def _is_git_directory(directory: Path) -> bool:
    """Whether `directory` looks like a common git directory, as git checks it: a HEAD file,
    and objects and refs directories (refs too in a reftable repository, which keeps its refs
    in reftable)."""
    return (
        (directory / "HEAD").is_file()
        and (directory / "objects").is_dir()
        and (directory / "refs").is_dir()
    )
