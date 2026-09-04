#!/usr/bin/env python3
"""php-cs-fixer formatter adapter. Emits SCHEMA.md JSON.

Runs php-cs-fixer fix on the target file and computes before/after line diff
to populate metrics.lines_added / lines_removed.

Usage: php-cs-fixer.py <file>

Env vars:
  PHPCSFIXER_BIN     php-cs-fixer binary (default: php-cs-fixer)
  PHPCSFIXER_CONFIG  --config path (optional)
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from difflib import unified_diff

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent
                       / "validators" / "common"))
from refusal import guard_main  # noqa: E402
from bin_resolve import describe_unresolved, resolve_bin_cmd  # noqa: E402


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _line_diff(before: str, after: str) -> tuple[int, int]:
    """Return (lines_added, lines_removed) between two file contents."""
    added = removed = 0
    for line in unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        n=0,
    ):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "php-cs-fixer", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    file = sys.argv[1]
    start = time.time()
    phpcsfixer_bin_cmd_str = os.environ.get("PHPCSFIXER_BIN", "php-cs-fixer")
    # Accept either a single binary path (may contain a space, e.g.
    # the default Windows install location under "C:\\Program Files")
    # or a shlex-quoted command line. Cross-platform test stubs pass
    # e.g. "python /path/stub.py" (each token shlex.quote'd) so the
    # stub runs on Windows too (no #!/usr/bin/env bash dependency).
    # resolve_bin_cmd() tries the whole string as one path first, and
    # only falls back to shlex.split when that does not resolve to a
    # real executable (#2176, #2191).
    bin_cmd = resolve_bin_cmd(phpcsfixer_bin_cmd_str, "php-cs-fixer")
    phpcsfixer_bin = bin_cmd[0]
    phpcsfixer_config = os.environ.get("PHPCSFIXER_CONFIG", "")

    if not shutil.which(phpcsfixer_bin) and not (
        os.path.isfile(phpcsfixer_bin) and os.access(phpcsfixer_bin, os.X_OK)
    ):
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PHPCSFIXER_BIN not found: {describe_unresolved(phpcsfixer_bin_cmd_str, phpcsfixer_bin)}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    cmd = [*bin_cmd, "fix", "--allow-risky=yes"]
    if phpcsfixer_config:
        cmd += ["--config", phpcsfixer_config]
    cmd.append(file)

    env = os.environ.copy()

    try:
        # php-cs-fixer exits 0 when no fixes, 1 when fixes applied, 16+ on error
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 60s"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    dur = int((time.time() - start) * 1000)

    # php-cs-fixer: exit 0 = no changes, exit 1 = fixes applied, exit >=16 = error
    if r.returncode >= 16:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "php-cs-fixer", "msg": msg}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    verify_failed = None
    try:
        after = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        # php-cs-fixer ran -- exit < 16 -- but the file could not be re-read
        # to compute what changed. `after = before` would report
        # `lines_added: 0, lines_removed: 0`: identical to a genuine no-op
        # (#2162). `verify_failed` says the 0/0 is "could not measure",
        # never "nothing changed".
        after = before
        verify_failed = f"could not re-read file to verify changes: {e}"

    added, removed = _line_diff(before, after)

    payload = {
        "tool": "php-cs-fixer",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed},
    }
    if verify_failed:
        payload["verify_failed"] = verify_failed
    emit(payload)


if __name__ == "__main__":
    guard_main("php-cs-fixer", main)
