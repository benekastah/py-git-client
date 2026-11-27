"""Structured results from a single git push."""

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass

from git_client.client import CLIENT_TIMEOUT, Arg, EnvValue, Git, Timeout, decode


class PushStatus(enum.StrEnum):
    """A ref's outcome: Git's porcelain flag, except UNKNOWN.

    Git prints UNKNOWN as `!` too, for a ref it didn't say was rejected (e.g. one the
    remote failed to report).
    """

    FAST_FORWARD = " "
    FORCED = "+"
    CREATED = "*"
    DELETED = "-"
    REJECTED = "!"
    UP_TO_DATE = "="
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushRef:
    """One ref Git reported for a destination.

    `source` is `""` for every deletion, even one Git prints as `(delete)`. `summary` is
    Git's, with any parenthesized reason (e.g. `[rejected] (stale info)`). `old_oid` and
    `new_oid` are set only for FAST_FORWARD and FORCED, when Git prints both; otherwise
    they're None.
    """

    source: str
    target: str
    status: PushStatus
    summary: str
    old_oid: str | None
    new_oid: str | None


@dataclass(frozen=True)
class PushDestination:
    """A destination Git reported, with the URL as displayed by Git.

    `done` is whether Git ended its report with `Done`, which it leaves out when the push to
    that destination failed outright (e.g. an unpacker error): its refs are then the rows Git
    printed before it went on, which may not be all of them.
    """

    url: str
    refs: tuple[PushRef, ...]
    done: bool


@dataclass(frozen=True)
class PushResult:
    """What one push did: its exit status, captured output and each destination's report.

    `destinations` holds each destination Git began a report for, in order, finished or not
    (see `PushDestination.done`).
    """

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
    if source == "(delete)":
        # Git names a deletion it rejects itself "(delete)" and reports others with an empty
        # source. A local ref named (delete) prints as refs/heads/(delete), so a bare
        # "(delete)" means a deletion.
        source = ""
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
    reported: list[PushDestination] = []
    url: str | None = None
    refs: list[PushRef] = []

    def end_report(done: bool) -> None:
        if url and refs:
            reported.append(PushDestination(url, tuple(refs), done))

    for line in stdout.removesuffix("\n").split("\n"):
        if line.startswith("To "):
            end_report(done=False)  # Git began the next report without finishing this one.
            url = line.removeprefix("To ")
            refs = []
        elif line == "Done":
            end_report(done=True)
            url = None
        elif url is not None and not line.startswith(("branch ", "Would set upstream ")):
            ref = _parse_ref(line)
            if ref is None:
                url = None
            else:
                refs.append(ref)
    end_report(done=False)
    return tuple(reported)


def push(
    git: Git,
    *args: Arg,
    env: Mapping[str, EnvValue] | None = None,
    timeout: Timeout = CLIENT_TIMEOUT,
) -> PushResult:
    """Run one git push and return each destination's report even on failure.

    It runs `git -c core.abbrev=no push --porcelain *args`, so a `FakeGit` test
    responds to `["-c", "core.abbrev=no", "push", "--porcelain"]` with porcelain output.

    A reported ref update or rejection is from Git's machine-readable output.
    Fast-forwards and forced updates report the full old and new OIDs when Git
    supplies them. Other statuses may omit one or both OIDs. An unacknowledged
    update has UNKNOWN status. Dry-run statuses describe projected changes. A
    destination whose push failed outright (e.g. an unpacker error) has `done`
    False, and Git goes on to the next push URL. One Git can't reach (e.g. not a
    repository) stops the push: it and the push URLs after it have no report.
    Don't pass `--quiet`: Git then reports a destination only when a ref fails,
    so the ones that succeed are left out, and the `Done` of one that succeeds
    can finish the report of an earlier one that failed outright.
    Git's output is captured and launch errors and timeouts are raised as usual.
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
