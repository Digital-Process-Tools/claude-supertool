"""@file route must take precedence over colon-CLI args.

Bug: `./supertool 'paste:@-:::path'` silently created a file literally
named `@-` instead of reading from stdin. The @-route detection skipped
because `len(parts) != 2`, then the path argument fell into op_paste.

Fix: if parts[1] starts with '@' AND the op supports @file, either load
from the @file (ignoring trailing parts) or emit a clear error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"


def _run(args: list[str], stdin: str = "") -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_paste_at_dash_with_trailing_colon_args_does_not_create_at_dash_file(
    tmp_path: Path,
) -> None:
    """`paste:@-:::path` must NOT create a file literally named `@-`."""
    out_path = tmp_path / "real_target.txt"
    # Stdin payload includes the path field that should be used.
    payload = json.dumps({"path": str(out_path), "content": "hello\n"})
    code, stdout, stderr = _run(
        [f"paste:@-:::{out_path}"], stdin=payload,
    )
    at_dash = REPO / "@-"
    try:
        # The bug created REPO/@-; the fix must NOT create that file.
        assert not at_dash.exists(), (
            f"Bug regression: a file literally named '@-' was created at "
            f"{at_dash}. Stdout: {stdout}. Stderr: {stderr}"
        )
    finally:
        if at_dash.exists():
            at_dash.unlink()


def test_paste_at_file_with_trailing_args_either_errors_or_uses_payload(
    tmp_path: Path,
) -> None:
    """`paste:@-:::path` should either error clearly or honor the @file payload."""
    out_path = tmp_path / "target.txt"
    payload = json.dumps({"path": str(out_path), "content": "X\n"})
    code, stdout, stderr = _run(
        [f"paste:@-:::{out_path}"], stdin=payload,
    )
    combined = stdout + stderr
    # Acceptable outcomes:
    # 1. ERROR mentioning @file/path/stdin so the user knows what went wrong
    # 2. paste succeeded into the real path (no stray @- file)
    accepted = (
        "ERROR" in combined
        or (out_path.exists() and out_path.read_text(encoding="utf-8") == "X\n")
    )
    at_dash = REPO / "@-"
    if at_dash.exists():
        at_dash.unlink()
    assert accepted, (
        f"Expected error or successful paste into the real path. "
        f"Got: {combined!r}"
    )
