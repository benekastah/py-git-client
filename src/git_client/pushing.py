"""Structured results from a single git push."""

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass

from git_client.client import CLIENT_TIMEOUT, Arg, EnvValue, Git, Timeout, decode


class PushStatus(enum.StrEnum):
    FAST_FORWARD = " "
    FORCED = "+"
    CREATED = "*"
    DELETED = "-"
    REJECTED = "!"
    UP_TO_DATE = "="
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushRef:
    source: str
    target: str
    status: PushStatus
    summary: str
    old_oid: str | None
    new_oid: str | None


@dataclass(frozen=True)
class PushDestination:
    """A destination Git reported, with the URL as displayed by Git."""

    url: str
    refs: tuple[PushRef, ...]


@dataclass(frozen=True)
class PushResult:
    returncode: int
    stdout: str
    stderr: str
    destinations: tuple[PushDestination, ...]


_OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_RANGE = re.compile(rf"({_OID})\.{{2,3}}({_OID})(?: \(forced update\))?$")


def _parse_ref(line: str) -> PushRef | None:
    columns = line.split("\t", 2)
    if len(columns) != 3:
        return None
    flag, refspec, summary = columns
    source, separator, target = refspec.rpartition(":")
    if not separator or not target:
        return None
    try:
        status = PushStatus(flag)
    except ValueError:
        return None
    if status is PushStatus.REJECTED and not summary.startswith(
        ("[rejected]", "[remote rejected]")
    ):
        status = PushStatus.UNKNOWN
    tips = (
        _RANGE.fullmatch(summary)
        if status in (PushStatus.FORCED, PushStatus.FAST_FORWARD)
        else None
    )
    return PushRef(
        source, target, status, summary, tips[1] if tips else None, tips[2] if tips else None
    )


def _destinations(stdout: str) -> tuple[PushDestination, ...]:
    done: list[PushDestination] = []
    url: str | None = None
    refs: list[PushRef] = []
    for line in stdout.split("\n"):
        if line.startswith("To "):
            url = line.removeprefix("To ")
            refs = []
        elif line == "Done":
            if url and refs:
                done.append(PushDestination(url, tuple(refs)))
            url = None
        elif url is not None and not line.startswith(("branch ", "Would set upstream ")):
            ref = _parse_ref(line)
            if ref is None:
                url = None
            else:
                refs.append(ref)
    return tuple(done)


def push(
    git: Git,
    *args: Arg,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> PushResult:
    """Run one git push and return the completed destination reports even on failure.

    A missing report leaves that destination's outcome unknown. A reported ref
    update or rejection is from Git's machine-readable output. Fast-forwards and
    forced updates report the full old and new OIDs when Git supplies them.
    Other statuses may omit one or both OIDs. An unacknowledged update has
    UNKNOWN status. Dry-run statuses describe projected changes, and quiet
    mode can suppress reports. Git's output is captured and launch errors
    and timeouts are raised as usual.
    """
    result = git.process(
        "-c",
        "core.abbrev=no",
        "push",
        "--porcelain",
        *args,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    stdout = decode(result.stdout)
    return PushResult(result.returncode, stdout, decode(result.stderr), _destinations(stdout))
