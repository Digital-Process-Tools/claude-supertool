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


def _run(args: list[str], stdin: str = "", *, cwd: Path) -> tuple[int, str, str]:
    """Spawn supertool from `cwd`, which must be a directory the test owns.

    `cwd` is required rather than defaulted (#1656). The bug under test creates
    a file named `@-` relative to the process working directory, so where that
    directory is *is* the subject: these tests used to run in the live checkout
    root and create and unlink `REPO/'@-'` there — a path two concurrent runs
    in one checkout race on, in a tree that is typically symlinked as the
    operator's `supertool` binary. Spawning from `tmp_path` asserts exactly the
    same thing about a directory nothing else is using.
    """
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=10,
        cwd=str(cwd), encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_paste_at_dash_with_trailing_colon_args_does_not_create_at_dash_file(
    tmp_path: Path,
) -> None:
    """`paste:@-:::path` must NOT create a file literally named `@-`."""
    out_path = tmp_path / "real_target.txt"
    # Stdin payload includes the path field that should be used.
    payload = json.dumps({"path": str(out_path), "content": "hello\n"})
    spawn_dir = tmp_path / "cwd"
    spawn_dir.mkdir()
    code, stdout, stderr = _run(
        [f"paste:@-:::{out_path}"], stdin=payload, cwd=spawn_dir,
    )
    # The bug created `@-` in the process working directory; the fix must NOT
    # create that file. Asserted against the spawn directory rather than the
    # checkout root, which is not scratch space (#1656).
    at_dash = spawn_dir / "@-"
    assert not at_dash.exists(), (
        f"Bug regression: a file literally named '@-' was created at "
        f"{at_dash}. Stdout: {stdout}. Stderr: {stderr}"
    )


def test_paste_at_file_with_trailing_args_either_errors_or_uses_payload(
    tmp_path: Path,
) -> None:
    """`paste:@-:::path` should either error clearly or honor the @file payload."""
    out_path = tmp_path / "target.txt"
    payload = json.dumps({"path": str(out_path), "content": "X\n"})
    spawn_dir = tmp_path / "cwd"
    spawn_dir.mkdir()
    code, stdout, stderr = _run(
        [f"paste:@-:::{out_path}"], stdin=payload, cwd=spawn_dir,
    )
    combined = stdout + stderr
    # Acceptable outcomes:
    # 1. ERROR mentioning @file/path/stdin so the user knows what went wrong
    # 2. paste succeeded into the real path (no stray @- file)
    accepted = (
        "ERROR" in combined
        or (out_path.exists() and out_path.read_text(encoding="utf-8") == "X\n")
    )
    assert not (spawn_dir / "@-").exists(), (
        f"a file literally named '@-' was created at {spawn_dir / '@-'}")
    assert accepted, (
        f"Expected error or successful paste into the real path. "
        f"Got: {combined!r}"
    )
