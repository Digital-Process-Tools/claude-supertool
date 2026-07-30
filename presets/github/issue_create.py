#!/usr/bin/env python3
"""Create a GitHub issue from a JSON/TOML payload file."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _remote_default as _rd  # noqa: E402


def _gh(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _load_payload(path: str) -> dict:
    if path.startswith("@"):
        path = path[1:]
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")

    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    if tomllib is None:
        raise ValueError("TOML payload requires Python 3.11+ or `pip install tomli`")
    return tomllib.loads(raw)


def _validate(payload: dict) -> str | None:
    if not payload.get("repo"):
        return "ERROR: payload missing required field: repo"
    if not payload.get("title"):
        return "ERROR: payload missing required field: title"
    if payload.get("body") and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
    return None


def main() -> int:
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path = raw_arg[1:] if raw_arg.startswith("@") else raw_arg
    if not path:
        # supertool's {arg} substitution never omits the placeholder — a
        # missing @FILE arrives here as an empty string, not a missing
        # argv slot, so this must be checked explicitly rather than relying
        # on len(sys.argv). See #620.
        print(
            "ERROR: gh-issue-create needs a payload — "
            "gh-issue-create:@FILE (JSON or TOML with title/body)."
        )
        return 1

    # Decide *before* reading rather than catching whatever the OS raises:
    # opening a directory for read raises IsADirectoryError on POSIX but
    # PermissionError on Windows (CreateFileW succeeds, the subsequent read
    # fails with ERROR_ACCESS_DENIED). Catching only IsADirectoryError left
    # #620's traceback alive on Windows — an is_dir() check gives the same
    # message on every platform without depending on which errno the OS
    # happens to pick. See #627.
    if path != "-" and Path(path).is_dir():
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1

    try:
        payload = _load_payload(raw_arg)
    except FileNotFoundError:
        print(f"ERROR: payload file not found: {path}")
        return 1
    except IsADirectoryError:
        # Belt-and-suspenders for the TOCTOU window between the is_dir()
        # check above and this read (path became a directory in between).
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1
    except PermissionError as e:
        # Deliberately a distinct message from "is a directory": a locked
        # or wrong-ownership file also raises PermissionError, and on
        # Windows it's the *only* thing a directory read raises. Reporting
        # it as "is a directory" would be a confidently wrong disclosure,
        # not just an unhelpful one.
        print(f"ERROR: permission denied reading payload: {path} — {e}")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: failed to parse payload: {e} (expected JSON or TOML with title/body)")
        return 1

    if not payload.get("repo"):
        auto = _rd.resolve("github_repo", "github.com")
        if auto:
            payload["repo"] = auto

    err = _validate(payload)
    if err:
        print(err)
        return 1

    repo = payload["repo"]
    title = payload["title"]
    body_file = payload.get("body_file")
    body = payload.get("body", "")
    labels: list[str] = payload.get("labels") or []
    assignees: list[str] = payload.get("assignees") or []
    milestone: str = payload.get("milestone", "")

    tmp_body: str | None = None
    try:
        if body_file:
            content = Path(body_file).read_text(encoding="utf-8")
        else:
            content = body

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_body = f.name

        cmd = [
            "issue", "create",
            "--repo", repo,
            "--title", title,
            "--body-file", tmp_body,
        ]
        if labels:
            cmd += ["--label", ",".join(labels)]
        if assignees:
            cmd += ["--assignee", ",".join(assignees)]
        if milestone:
            cmd += ["--milestone", milestone]

        try:
            result = _gh(cmd, timeout=30)
        except FileNotFoundError:
            print("ERROR: gh not found — install from https://cli.github.com")
            return 1
        except subprocess.TimeoutExpired:
            print("ERROR: gh timed out")
            return 1

        if result.returncode != 0:
            print(f"ERROR: gh issue create failed (exit {result.returncode})")
            print(result.stderr.strip() or result.stdout.strip())
            return 1

        match = re.search(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", result.stdout)
        if match:
            url = match.group(0)
            number = match.group(1)
        else:
            url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "?"
            number = url.rstrip("/").split("/")[-1] if "/" in url else "?"

        print(f"gh-issue-create OK number={number} url={url}")
        return 0

    finally:
        if tmp_body and os.path.exists(tmp_body):
            os.unlink(tmp_body)


if __name__ == "__main__":
    sys.exit(main())
