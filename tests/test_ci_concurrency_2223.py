"""Every event-driven workflow declares a `concurrency` group that cancels (#2223).

Measured on 2026-09-03, mid-tick, after eight squash merges landed on `master`
in about twenty minutes: `gh run list` showed six `tests` runs queued at once,
one per squash, each a sixteen-leg matrix. The run for the head anyone actually
wanted an answer about sat behind five obsolete ones, and `gh-branch` on that
head read `4 passed, 0 failed, 17 pending` seventeen minutes after the merge --
the matrix had not started a leg. That is roughly eighty legs of runner time
spent concluding about commits nobody will ever read, and the maintainer tick
that produced the queue then blocked on the CI wait it had created for itself.

A merge train is the *normal* shape of a tick, not an exception:
`commands/tick.md` merges the ready board one pull request at a time and each
squash is a push to `master`. So the queue depth is a function of how well the
tick did.

**The `pull_request` half is free.** A force-push makes the older run worthless
by construction, and this repo force-pushes on every rebase-and-resolve.

**The `push: master` half is a trade, taken deliberately.** Cancelling means a
squashed `master` commit can end with no run of its own, which costs the ability
to bisect a red to the merge that caused it. Against that: nothing in this repo
reads a per-commit historical green -- `gh-branch` asks whether `master` is green
*now* -- and #1257 already establishes that a tally predating later merges is not
trustworthy, so those intermediate greens were carrying less than they appeared
to. The group keys on `github.ref`, so a `master` push cancels only an earlier
`master` push, never a pull request's run.

The guard is phrased against the parsed key rather than against the text, per
#731: `tests.yml` is mostly comments explaining its own decisions, so any
`"concurrency" in text` needle is satisfied by the prose describing it -- the
exact defect that kept `assert "oven-sh/setup-bun" in text` green for months
after the action was dropped.

PyYAML is deliberately not imported, for the reason `tests/_workflow_parse.py`
gives at length: a guard over CI policy that can be skipped by a third-party
parser going missing has the shape of the defect it exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

#: A key at column zero. `on:`, `concurrency:`, `jobs:` and friends.
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?\s*$")

#: A key nested one level under a top-level mapping, at exactly two spaces.
_NESTED_KEY_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?\s*$")

#: The events that queue a run behind its predecessor on the same ref. A
#: `schedule` fires on a cadence slower than a run takes, and a
#: `workflow_dispatch` is somebody asking for that exact run -- neither one
#: is what #2223 measured, and cancelling either would be wrong.
_QUEUEING_EVENTS = ("push", "pull_request")


def _workflow_files() -> list[Path]:
    """Both extensions, for the reason `test_ci_action_pinning_925.py` gives:
    a `*.yml` glob checks every workflow today and none of a `.yaml` one added
    tomorrow, and a guard that reports `ok` while checking nothing is this
    repo's own defect class."""
    return sorted(
        p for p in WORKFLOWS.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml"))


def _top_level_block(text: str, key: str) -> str | None:
    """The body under a column-zero `key:`, or None when it declares none.

    None and `""` are different answers: a key with an empty body is declared
    and says nothing, a key that is absent was never written. Only the second
    is what this file fails on, so they must not collapse.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _TOP_KEY_RE.match(line)
        if match and match.group(1) == key:
            inline = (match.group(2) or "").strip()
            if inline and not inline.startswith("#"):
                return inline
            start = index + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        body.append(line)
    return "\n".join(body)


def _nested_value(block: str, key: str) -> str | None:
    for line in block.splitlines():
        match = _NESTED_KEY_RE.match(line)
        if match and match.group(1) == key:
            return (match.group(2) or "").strip()
    return None


def _declared_events(text: str) -> list[str]:
    """Which of `_QUEUEING_EVENTS` this workflow's `on:` declares.

    `on:` is the one key here that YAML itself will mangle -- unquoted `on` is
    the boolean true in YAML 1.1 -- which is a second reason this reads the
    lines rather than a parser's mapping keys.
    """
    block = _top_level_block(text, "on")
    if block is None:
        return []
    found = []
    for event in _QUEUEING_EVENTS:
        if re.search(rf"^  {event}:", block, re.M):
            found.append(event)
        elif re.search(rf"(^|[\[,]\s*){event}(\s*[\],]|$)", block):
            # `on: [push, pull_request]`, the flow-sequence spelling.
            found.append(event)
    return found


def test_the_discovery_actually_finds_the_workflows() -> None:
    """A parser that finds nothing renders every guard below green while
    checking no workflow at all -- the #557 shape, and the only way this file
    can be wrong in the direction that matters."""
    files = _workflow_files()
    assert len(files) >= 3, (
        f"expected at least three workflow files under {WORKFLOWS}, found "
        f"{[p.name for p in files]} -- the layout changed and this file is "
        "now checking nothing")
    queueing = {p.name: _declared_events(p.read_text(encoding="utf-8"))
                for p in files}
    matched = {name: events for name, events in queueing.items() if events}
    assert len(matched) >= 2, (
        f"expected at least two workflows triggered by {list(_QUEUEING_EVENTS)}, "
        f"found {matched} out of {queueing} -- the `on:` parser stopped "
        "matching and every assertion below is now vacuous")


def test_every_queueing_workflow_declares_a_concurrency_group() -> None:
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        events = _declared_events(text)
        if not events:
            continue
        block = _top_level_block(text, "concurrency")
        assert block is not None, (
            f"{path.name}: triggered by {events} and declares no top-level "
            "`concurrency:`. Eight squash merges queued six obsolete "
            "sixteen-leg runs on master and the current head's run started no "
            "leg for seventeen minutes (#2223).")
        group = _nested_value(block, "group")
        assert group, f"{path.name}: `concurrency:` declares no `group:`"
        assert "github.ref" in group, (
            f"{path.name}: concurrency group {group!r} does not key on "
            "`github.ref`, so runs for unrelated branches share one group and "
            "cancel each other. A pull request would kill master's run.")


def test_every_concurrency_group_actually_cancels() -> None:
    """A group with `cancel-in-progress: false` serialises the queue instead of
    draining it -- the obsolete runs still execute, one at a time, and the head
    still waits behind all of them. That is the measured symptom unchanged, so
    the group alone is not the fix."""
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        if not _declared_events(text):
            continue
        block = _top_level_block(text, "concurrency")
        assert block is not None  # the test above is what reports this
        cancel = _nested_value(block, "cancel-in-progress")
        assert cancel is not None, (
            f"{path.name}: `concurrency:` declares no `cancel-in-progress:`. "
            "It defaults to false, which serialises the queue rather than "
            "draining it -- every obsolete run still executes.")
        assert cancel.split("#", 1)[0].strip() == "true", (
            f"{path.name}: `cancel-in-progress: {cancel}` -- obsolete runs "
            "still execute and the current head still waits behind them.")
