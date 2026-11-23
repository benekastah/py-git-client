"""Optional libgit2 reads for local objects addressed by full SHA-1 ids."""

import os
import re
from collections.abc import Mapping
from typing import NamedTuple

from git_client.performance import note_backend_read

_OBJECT_NAME = re.compile(r"([0-9a-fA-F]{40})(?:\^\{(blob|commit|tag|tree)\})?")
# These can change repository discovery, the object database, or revision interpretation.
_GIT_OVERRIDES = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


_CLIENT_CONFIG_OVERRIDES = {
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG_NOSYSTEM",
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
}


class NativeObject(NamedTuple):
    """An object header and, if requested, its raw contents."""

    id: str
    kind: str
    size: int
    content: bytes


def read_local_object(
    directory: str, env: Mapping[str, str | bytes], name: str, *, contents: bool
) -> NativeObject | None:
    """Return a successful native lookup, or let Git answer for all other cases."""
    match = _OBJECT_NAME.fullmatch(name)
    if (
        match is None
        or not _CLIENT_CONFIG_OVERRIDES.isdisjoint(env)
        or not _GIT_OVERRIDES.isdisjoint(os.environ)
        or not _GIT_OVERRIDES.isdisjoint(env)
    ):
        return None
    directory = directory or "."
    # libgit2 can discover a repository from a file; git -C requires a directory.
    if not os.path.isdir(directory):
        return None
    try:
        import pygit2
    except ImportError:
        return None

    object_id, requested_type = match.groups()
    note_backend_read()
    try:
        path = pygit2.discover_repository(
            directory, False, os.environ.get("GIT_CEILING_DIRECTORIES", "")
        )
        if path is None:
            return None
        repository = pygit2.Repository(path)
        config = repository.config
        skip_system = "GIT_CONFIG_NOSYSTEM" in os.environ
        if (
            repository.is_shallow
            or any(
                key in config
                for key in (
                    "extensions.objectformat",
                    "extensions.compatobjectformat",
                    "extensions.partialclone",
                    "extensions.refstorage",
                )
            )
            or any(
                (entry.name.startswith("remote.") and entry.name.endswith(".promisor"))
                or (
                    skip_system
                    and entry.level
                    in {pygit2.enums.ConfigLevel.SYSTEM, pygit2.enums.ConfigLevel.PROGRAMDATA}
                )
                for entry in config
            )
        ):
            return None
        # A replacement ref can use any hex case. Peeling can encounter a
        # replacement beyond the requested ID, for example at a commit's tree.
        if any(ref.startswith(b"refs/replace/") for ref in repository.raw_listall_references()):
            return None
        try:
            repository.lookup_reference_dwim(object_id)
        except KeyError:
            # Git can warn if a ref also has this name, even for a full object id.
            pass
        else:
            return None
        oid = pygit2.Oid(hex=object_id)
        if requested_type is not None:
            oid = repository.revparse_single(name).id
        if contents:
            kind, content = repository.odb.read(oid)
            size = len(content)
        else:
            kind, size = repository.odb.read_header(oid)
            content = b""
        object_type = kind.name.lower()
        if requested_type is not None and requested_type != object_type:
            return None
        return NativeObject(str(oid), object_type, size, content)
    except pygit2.GitError, KeyError, ValueError, OSError:
        return None
