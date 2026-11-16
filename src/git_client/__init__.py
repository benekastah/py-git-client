"""Run git from Python with as few processes as possible: one way to start git, a long-lived
cat-file, and batched reads and writes."""

from git_client.batch import (
    Ref,
    RefUpdate,
    config,
    config_key,
    hash_blobs,
    object_id_length,
    patch_ids,
    refs,
    update_refs,
)
from git_client.cat_file import CatFile, ObjectInfo
from git_client.client import (
    CLIENT_TIMEOUT,
    CLOSE_FDS,
    Arg,
    EnvValue,
    Git,
    GitError,
    GitTimeoutError,
    Timeout,
    decode,
    encode,
    find_git,
)

__all__ = [
    "CLIENT_TIMEOUT",
    "CLOSE_FDS",
    "Arg",
    "CatFile",
    "EnvValue",
    "Git",
    "GitError",
    "GitTimeoutError",
    "ObjectInfo",
    "Ref",
    "RefUpdate",
    "Timeout",
    "config",
    "config_key",
    "decode",
    "encode",
    "find_git",
    "hash_blobs",
    "object_id_length",
    "patch_ids",
    "refs",
    "update_refs",
]
