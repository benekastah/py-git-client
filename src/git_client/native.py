"""Optional libgit2 reads for local objects addressed by full SHA-1 ids."""

import os
import re
from collections.abc import Mapping
from typing import NamedTuple

_OBJECT_NAME = re.compile(r"([0-9a-fA-F]{40})(?:\^\{(blob|commit|tag|tree)\})?")
# These can change repository discovery, the object database, or revision interpretation.
_GIT_OVERRIDES = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
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
        or "GIT_CEILING_DIRECTORIES" in env
        or not _GIT_OVERRIDES.isdisjoint(os.environ)
        or not _GIT_OVERRIDES.isdisjoint(env)
    ):
        return None
    try:
        import pygit2
    except ImportError:
        return None

    object_id, requested_type = match.groups()
    try:
        path = pygit2.discover_repository(
            directory, False, os.environ.get("GIT_CEILING_DIRECTORIES", "")
        )
        if path is None:
            return None
        repository = pygit2.Repository(path)
        config = repository.config
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
                entry.name.startswith("remote.") and entry.name.endswith(".promisor")
                for entry in config
            )
        ):
            return None
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
