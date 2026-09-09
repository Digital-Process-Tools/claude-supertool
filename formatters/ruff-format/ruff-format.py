#!/usr/bin/env python3
"""ruff format adapter. Emits SCHEMA.md JSON.

Runs `ruff format` on the target file and computes before/after line diff to
populate metrics.lines_added / lines_removed -- #2085.

`ruff format` rather than `black`: this repository's own CI lint leg already
depends on ruff (see .github/workflows/tests.yml), so the toolchain this
adapter dispatches to is already installed wherever the tests run, and the
absent-tool arm below rarely fires. A repo that prefers black can still wire
it in directly (see docs/formatters.md, "Adding your own") -- this adapter
only closes the gap that no *shipped* Python formatter existed at all.

Usage: ruff-format.py <file>

Env vars:
  RUFF_BIN     ruff binary (default: ruff)
  RUFF_CONFIG  --config path (optional)
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent
                       / "validators" / "common"))
from refusal import guard_main  # noqa: E402
from bin_resolve import describe_unresolved, resolve_bin_cmd  # noqa: E402


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _line_diff(before: str, after: str):
    """Return (lines_added, lines_removed, (first_changed_line, last_changed_line)).

    The line-number pair is 1-indexed and refers to `before` -- the file a
    same-session `around_line` read would have seen moments earlier -- so a
    caller can tell whether a formatter's own post-write rewrite reached
    into the region an already-captured read is about to reuse as an
    `edit`'s `old` string, rather than learning only *that* something
    changed (#2405). `(None, None)` when nothing changed at all, never a
    fabricated `(1, 1)` -- the same "must still work when nothing changed"
    control every "must not silently X" claim here needs.

    `SequenceMatcher.get_opcodes()` rather than `unified_diff(..., n=0)`:
    the unified-diff hunk header would need re-parsing to recover line
    numbers, where `get_opcodes()` hands them over as `i1`/`i2` directly.

    Multiple disjoint hunks are reported as ONE merged span (min start,
    max end), not as a list of ranges -- two small, far-apart changes read
    as one range covering everything between them, including untouched
    lines. That over-reports rather than under-reports (a caller re-reading
    the whole span will not miss a touched line), and is the documented
    behaviour, not a bug: see `test_disjoint_hunks_report_one_merged_span_documented_over_approximation`.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    added = removed = 0
    first = last = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        # A pure insertion (i1 == i2) has no before-side span of its own --
        # anchor it to the nearest existing before-line so the range still
        # names a line number a caller's earlier read would recognise. When
        # `before_lines` is empty there IS no existing before-line to anchor
        # to at all (the whole file is new content) -- the loop below still
        # runs, but its result is discarded after the loop (#2405 self-review:
        # `max(i1, 1) == 1` used to fabricate `(1, 1)` for this case, exactly
        # the value the return-type note below disclaims).
        start = i1 + 1 if i2 > i1 else max(i1, 1)
        end = i2 if i2 > i1 else max(i1, 1)
        if first is None or start < first:
            first = start
        if last is None or end > last:
            last = end
    if not before_lines:
        # Nothing existed before this write -- there is no before-line for
        # any earlier same-session read to have gone stale against, so the
        # touched-range fields must say "nothing to report", not "line 1".
        # This is deliberately a single merged span, not a list of hunks:
        # two small, far-apart changes (e.g. before-lines 2 and 10 of a
        # 10-line file) are reported as one span covering everything
        # between them. That over-reports rather than under-reports -- a
        # caller re-reading the whole span will not miss a touched line --
        # and a list-of-ranges return shape is more machinery than #2405's
        # own repro (one contiguous collapsed block) calls for.
        first = last = None
    return added, removed, (first, last)


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "ruff-format", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    file = sys.argv[1]
    start = time.time()
    ruff_bin_cmd_str = os.environ.get("RUFF_BIN", "ruff")
    # Accept either a single binary path (may contain a space, e.g. the
    # default Windows install location "C:\\Program Files\\ruff\\ruff.exe")
    # or a shlex-quoted command line. Cross-platform test stubs pass e.g.
    # "python /path/stub.py" (each token shlex.quote'd) so the stub runs on
    # Windows too (no #!/usr/bin/env bash dependency). resolve_bin_cmd()
    # tries the whole string as one path first, and only falls back to
    # shlex.split when that does not resolve to a real executable (#2176).
    bin_cmd = resolve_bin_cmd(ruff_bin_cmd_str, "ruff")
    ruff_bin = bin_cmd[0]
    ruff_config = os.environ.get("RUFF_CONFIG", "")

    if not shutil.which(ruff_bin) and not (
        os.path.isfile(ruff_bin) and os.access(ruff_bin, os.X_OK)
    ):
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"RUFF_BIN not found: {describe_unresolved(ruff_bin_cmd_str, ruff_bin)}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            before = f.read()
    except OSError as e:
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    cmd = [*bin_cmd, "format"]
    if ruff_config:
        cmd += ["--config", ruff_config]
    cmd.append(file)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 30s"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "ruff-format", "msg": msg}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    verify_failed = None
    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            after = f.read()
    except OSError as e:
        # ruff exited 0 -- the format ran -- but the file could not be
        # re-read to compute what changed. `after = before` would report
        # `lines_added: 0, lines_removed: 0`: identical to a genuine no-op
        # (#2162). `verify_failed` says the 0/0 is "could not measure",
        # never "nothing changed".
        after = before
        verify_failed = f"could not re-read file to verify changes: {e}"

    added, removed, (first, last) = _line_diff(before, after)

    payload = {
        "tool": "ruff-format",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {
            "lines_added": added, "lines_removed": removed,
            "first_changed_line": first, "last_changed_line": last,
        },
    }
    if verify_failed:
        payload["verify_failed"] = verify_failed
    emit(payload)


if __name__ == "__main__":
    guard_main("ruff-format", main)
