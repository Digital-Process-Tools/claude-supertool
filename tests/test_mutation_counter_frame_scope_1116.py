"""Mutation counters need the same frame-scoping #1109 gave the list
accumulators (#1116).

`_WRITE_COUNT`, `_MUTATION_ATTEMPTS`, `_SKIP_COUNT`, `_ROLLBACK_COUNT` and
`_REAPPLY_COUNT` are `[0]` process-global counters. `_dispatch_impl` used to
snapshot each one at op entry and subtract it at op exit -- correct only
while exactly one op is mutating at once. That is the identical failure
#1109 fixed for `_NOT_CHECKED`/`_VALIDATED_FILES`, left standing here because
it was a different value and bundling it into #1109 would have widened a
validators fix into the mutation path.

It held only because every mutating op is excluded from `_PARALLEL_SAFE_OPS`
-- a reachability argument about a membership list kept for an unrelated
reason, not a design one. This test calls `supertool.dispatch()` directly
from several threads at once, bypassing `main()`'s `_is_parallel_safe` gate
entirely, because the claim under test is about the counters themselves: if
two mutating ops are ever in flight together -- today by a direct caller, or
tomorrow because that list changed -- each op's own footer must count only
its own write, never a sibling's.

A slow formatter widens the window between one op's snapshot and its own
completion, the same trick #1109's slow validator adapter used to make the
race land reliably instead of serialising by luck.
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import supertool

WORKERS = 6


def _configure_slow_formatter(tmp_path: Path, delay: float = 0.15) -> None:
    reply = (
        chr(39)
        + "{\"ok\": true, \"changes\": {\"lines_added\": 0, \"lines_removed\": 0, "
        + "\"bytes_delta\": 0}}"
        + chr(39)
    )
    helper = tmp_path / "_slow_fmt.py"
    helper.write_text(
        "import time" + chr(10)
        + f"time.sleep({delay!r})" + chr(10)
        + f"print({reply})" + chr(10),
        encoding="utf-8",
    )
    exe = sys.executable.replace(chr(92), "/")
    helper_fwd = str(helper).replace(chr(92), "/")
    supertool._CONFIG = {"formatters": {
        "slow": {
            "cmd": f"{exe} {helper_fwd} {{file}}",
            "hooks_into": ["paste"],
            "match": "*.txt",
            "requires_config": False,
        },
    }}
    supertool._CONFIG_CHECKED = True


def _result_line(out: str) -> str:
    for ln in out.splitlines():
        if ln.startswith("[result]"):
            return ln
    return ""


def _paste_one(i: int) -> str:
    return supertool.dispatch(f"paste:f{i}.txt:x = {i}" + chr(10))


def test_each_concurrently_dispatched_write_counts_only_its_own_write(
    tmp_path, monkeypatch
) -> None:
    """RED before the fix: with six writes racing the shared global, some
    footers claim more than one write and others claim none at all -- the
    total is still six, but attributed to the wrong ops.
    """
    monkeypatch.chdir(tmp_path)
    _configure_slow_formatter(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        outs = list(ex.map(_paste_one, range(WORKERS)))
    lines = [_result_line(o) for o in outs]
    assert all(ln == "[result] 1 op run, 1 write" for ln in lines), (
        "a concurrently-dispatched write reported a count it did not "
        "produce itself:" + chr(10) + chr(10).join(outs))


def test_a_sequential_run_is_unchanged(tmp_path, monkeypatch) -> None:
    """The control: one op at a time was always correct, and stays correct."""
    monkeypatch.chdir(tmp_path)
    _configure_slow_formatter(tmp_path, delay=0.0)
    outs = [_paste_one(i) for i in range(3)]
    lines = [_result_line(o) for o in outs]
    assert lines == ["[result] 1 op run, 1 write"] * 3, outs
