"""A server pending approval is not an established subscription either (#2247).

`claude mcp get <name>` exits **0** for a third status this reader did not
have a case for: a project-scope `.mcp.json` server nobody has approved yet.

    oss-channel:
      Scope: Project config (shared via .mcp.json)
      Status: ⏸ Pending approval (run `claude` to approve)

`_configured` only ever distinguished two exit-0 answers -- `Rejected` reads
as `False` (#2208), everything else reads as `True`. A pending-approval server
is not loaded any more than a rejected one is: the harness has not started it,
it is one exit-0 status short of the claim #2209 stopped this file from
making, and `_configured`'s own docstring already frames the test -- "a claim
about a load rather than about a connection." So this must read as `False`,
the same as `Rejected`, not as `True`.

Measured against real `claude` 2.1.258 in a scratch dir with only that
`.mcp.json` (from the issue): `Rejected -> (False, '')`, the control case that
proves the branch can fire; `Pending approval -> (True, '')` before this fix,
which is the defect.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

PENDING_APPROVAL = (
    "oss-channel:\n"
    "  Scope: Project config (shared via .mcp.json)\n"
    "  Status: ⏸ Pending approval (run `claude` to approve)\n"
)

REJECTED = (
    "claude-channel:\n"
    "  Scope: Project config (shared via .mcp.json)\n"
    "  Status: ✘ Rejected (see disabledMcpjsonServers in settings)\n"
)


def _mcp_get(monkeypatch, stdout: str, returncode: int = 0):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout.encode("utf-8"))
    monkeypatch.setattr(channel.subprocess, "run", run)


def test_a_rejected_server_still_reads_as_not_configured(monkeypatch):
    """Control case: proves the branch this test lives next to can fire at
    all, on the fixture #2208 already pinned."""
    _mcp_get(monkeypatch, REJECTED)
    answer, why = channel._configured("claude-channel")
    assert answer is False, (answer, why)


def test_a_pending_approval_server_is_not_configured(monkeypatch):
    """The defect: a server nobody has approved must not read as True."""
    _mcp_get(monkeypatch, PENDING_APPROVAL)
    answer, why = channel._configured("oss-channel")
    assert answer is False, (answer, why)


def test_change_is_findable():
    assert_change_is_findable(2247)
