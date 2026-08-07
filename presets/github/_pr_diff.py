#!/usr/bin/env python3
"""A PR's diff in the shape a reviewer walks it (#875).

`gh pr diff N` returns the whole unified diff or nothing. That is the wrong
granularity for the one read the merge gate cannot skip: an eighty-file
mechanical sweep carries four files of judgment, and reading it whole is not a
review, it is a context burn. So this renders the same two-step `gh-job`
already models with `:fail` / `:raw:-N` / `:grep:PATTERN` —

    gh-pr:N:diff        the file list, per-file +/-, heaviest first
    gh-pr:N:diff:PATH   that one file's hunks

**Heaviest first, not grouped by kind.** `git-diff` groups its file list by a
path classifier (src/test/i18n/…). That classifier lives in `presets/git/diff.py`
and is not shared; copying it here would create the second definition that lets
the two drift, which is the failure `_checks` and `_board` exist to prevent. A
review order that needs no shared vocabulary is churn-descending, and it is the
order a reviewer actually takes — with `mechanical_note()` covering the case
where the biggest file is also the emptiest.

**The mechanical note is a note.** A file whose every hunk is byte-identical
after stripping is flagged as a repeated edit so attention goes elsewhere. It
never removes a file from the list and it never shortens one: a wrong
"mechanical" verdict is an invitation to skim the file that needed reading, so
the test is exact equality and under-flagging is the deliberate direction.

**Three states, because this renders inside the merge gate.** A diff that could
not be fetched (`files is None`) prints a named refusal and exits 1 — never an
empty file list, which reads as "this PR changes nothing" at the moment someone
is deciding whether to merge it. A path that is not in the diff is a refusal
too, naming the paths that are, because "not in this PR" and "in this PR and
unchanged" are the same silence otherwise. Both caps — files and bytes —
disclose exactly what they withheld, in the render they truncated.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _untrusted  # noqa: E402  (hunk bodies are a stranger's text — #694)
from _env import env_int  # noqa: E402  (the one knob reader — #654)

# The file list is one line per file; 60 matches git-diff's MAX_FILES so the
# two review renders cut at the same place.
MAX_FILES = 60
# One file's hunks. Same budget as gh-job's grep emission — the point of the
# per-file route is that it is small, and a file that blows this is itself the
# finding.
MAX_BYTES = 65536


def _path_from_header(line: str) -> str:
    """`diff --git a/x b/x` → `x`. Falls back to the raw remainder."""
    rest = line[len("diff --git "):].strip()
    half = len(rest) // 2
    if rest[half:half + 1] == " ":
        left, right = rest[:half], rest[half + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
    parts = rest.split(" b/", 1)
    if len(parts) == 2:
        return parts[1]
    return rest


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse(patch: str) -> list[dict]:
    """A unified diff → one record per file.

    Keys: path, old_path, status (A/M/D/R), added, removed, hunks, binary.
    Counting `+`/`-` inside hunks only — the `+++`/`---` header lines are not
    changed lines and counting them inflates every file by one each way.
    """
    files: list[dict] = []
    current: dict | None = None
    hunk: list[str] | None = None

    def close_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk:
            current["hunks"].append("\n".join(hunk))
        hunk = None

    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            close_hunk()
            current = {
                "path": _path_from_header(line),
                "old_path": None,
                "status": "M",
                "added": 0,
                "removed": 0,
                "hunks": [],
                "binary": False,
            }
            files.append(current)
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current["status"] = "A"
                continue
            if line.startswith("deleted file mode"):
                current["status"] = "D"
                continue
            if line.startswith("rename from "):
                current["status"] = "R"
                current["old_path"] = line[len("rename from "):].strip()
                continue
            if line.startswith("rename to "):
                current["status"] = "R"
                current["path"] = line[len("rename to "):].strip()
                continue
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current["binary"] = True
                continue
            if line.startswith("--- "):
                target = line[4:].strip()
                if target != "/dev/null":
                    current["old_path"] = _strip_prefix(target)
                continue
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target != "/dev/null":
                    current["path"] = _strip_prefix(target)
                continue
            if not line.startswith("@@"):
                continue
        if line.startswith("@@"):
            close_hunk()
            hunk = [line]
            continue
        if hunk is not None:
            hunk.append(line)
            if line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["removed"] += 1
    close_hunk()
    return files


def _hunk_signature(hunk: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A hunk reduced to what it actually changes, whitespace-normalised.

    The `@@ -a,b +c,d @@` header and the context lines are excluded: the same
    edit applied at line 3 and at line 40 has different headers and different
    neighbours and is still the same edit.
    """
    removed = tuple(l[1:].strip() for l in hunk.splitlines()[1:] if l.startswith("-"))
    added = tuple(l[1:].strip() for l in hunk.splitlines()[1:] if l.startswith("+"))
    return removed, added


def mechanical_note(entry: dict) -> str | None:
    """`same edit ×N` when every hunk in the file is the identical change.

    `None` for everything else. This is the heuristic the issue asked for and
    it is deliberately the strictest reading of "mechanical": exact equality
    after stripping. A looser rule would flag files that merely look alike, and
    the cost of that is a reviewer skipping the hunk that mattered.
    """
    hunks = entry.get("hunks") or []
    if len(hunks) < 2:
        return None
    signatures = {_hunk_signature(h) for h in hunks}
    if len(signatures) != 1:
        return None
    only = next(iter(signatures))
    if not only[0] and not only[1]:
        return None
    return f"same edit x{len(hunks)}"


def _stat_cell(entry: dict) -> str:
    if entry.get("binary"):
        return "binary"
    return f"+{entry.get('added', 0)} -{entry.get('removed', 0)}"


def _churn(entry: dict) -> int:
    return int(entry.get("added", 0)) + int(entry.get("removed", 0))


def _summary(files: list[dict], header: list[str], number: str | None,
             max_files: int) -> tuple[str, int]:
    out = list(header)
    total = len(files)
    if total == 0:
        # A real, reportable state — and worded so it can never be mistaken
        # for the refusal below. A PR with no file changes is unusual and the
        # reviewer should see it said plainly rather than inferred from blank
        # output.
        out.append("No files changed in this PR (0 files) — "
                   "the diff was read and it is empty.")
        return "\n".join(out), 0

    added = sum(int(f.get("added", 0)) for f in files)
    removed = sum(int(f.get("removed", 0)) for f in files)
    out.append(f"{total} files, +{added} -{removed}")
    out.append("")
    out.append(f"## Files changed ({total})")

    ordered = sorted(files, key=lambda f: (-_churn(f), str(f.get("path", ""))))
    shown = ordered[:max_files]
    width = max((len(_stat_cell(f)) for f in shown), default=0)
    for entry in shown:
        note = mechanical_note(entry)
        suffix = f"  [{note}]" if note else ""
        path = _untrusted.flat(str(entry.get("path", "?")))
        if entry.get("status") == "R" and entry.get("old_path"):
            path = f"{_untrusted.flat(str(entry['old_path']))} -> {path}"
        out.append(f"  {entry.get('status', '?')}  "
                   f"{_stat_cell(entry):>{width}}  {path}{suffix}")
    dropped = total - len(shown)
    if dropped:
        # The count is the real one and the cut names itself. A file list
        # narrowed by the tool and rendered as complete is the defect this op
        # sits inside — here it would be a reviewer certifying files they were
        # never shown.
        out.append(f"  ... {dropped} more file(s) not shown "
                   f"(cap {max_files}) — raise with GH_PR_DIFF_MAX_FILES=N")

    out.append("")
    ref = number or "N"
    out.append(f"One file's hunks: gh-pr:{ref}:diff:PATH")
    return "\n".join(out), 0


def _one_file(files: list[dict], path: str, header: list[str],
              max_bytes: int) -> tuple[str, int]:
    match = next((f for f in files if str(f.get("path", "")) == path), None)
    if match is None:
        # Not a file with no changes. Almost always a typo or the wrong PR
        # number, and printing nothing lets the reader conclude the file is
        # clean in a PR that never touched it.
        out = list(header)
        out.append(f"Could not show {path!r}: it is not among the "
                   f"{len(files)} file(s) in this PR's diff.")
        out.append("")
        out.append("## Files in this diff")
        for entry in files[:MAX_FILES]:
            out.append(f"  {_untrusted.flat(str(entry.get('path', '?')))}")
        if len(files) > MAX_FILES:
            out.append(f"  ... {len(files) - MAX_FILES} more")
        return "\n".join(out), 1

    out = list(header)
    out.append(f"## {_untrusted.flat(path)}  "
               f"({match.get('status', '?')}, {_stat_cell(match)})")
    note = mechanical_note(match)
    if note:
        out.append(f"Note: every hunk in this file is the same edit "
                   f"({note}) — a heuristic, not a filter; all hunks follow.")
    if match.get("binary"):
        out.append("Binary file — no textual hunks to show.")
        return "\n".join(out), 0
    if not match.get("hunks"):
        out.append("No hunks in this file's entry "
                   "(mode change or rename with no content change).")
        return "\n".join(out), 0

    body = "\n".join(match["hunks"])
    total_bytes = len(body.encode("utf-8", errors="replace"))
    cut = 0
    if total_bytes > max_bytes:
        body = body.encode("utf-8", errors="replace")[:max_bytes].decode(
            "utf-8", errors="ignore")
        cut = total_bytes - len(body.encode("utf-8", errors="replace"))
        out.append(f"Showing the first {max_bytes} bytes of {total_bytes} — "
                   f"{cut} bytes withheld; raise with GH_PR_DIFF_MAX_BYTES=N")
    out.append(_untrusted.fence(body))
    if cut:
        out.append(f"{cut} bytes withheld above (cap {max_bytes} bytes of "
                   f"{total_bytes}) — this is NOT the whole file's diff.")
    return "\n".join(out), 0


def render(files: list[dict] | None, *, header: list[str],
           path: str | None = None, reason: str | None = None,
           number: str | None = None,
           max_files: int | None = None,
           max_bytes: int | None = None) -> tuple[str, int]:
    """The whole render, as (text, exit code).

    `files is None` means nobody read the diff. That is the one case where
    printing a well-formed empty result would be a lie told inside a merge
    decision, so it refuses and names the cause it was handed.
    """
    if max_files is None:
        max_files = env_int("GH_PR_DIFF_MAX_FILES", MAX_FILES, minimum=1)
    if max_bytes is None:
        max_bytes = env_int("GH_PR_DIFF_MAX_BYTES", MAX_BYTES, minimum=1)

    if files is None:
        out = list(header)
        out.append("Could not read this PR's diff — the file list below is "
                   "absent because nothing was fetched, NOT because nothing "
                   "changed.")
        out.append(f"Reason: {reason or 'unknown'}")
        out.append("Do not treat this as a reviewed diff.")
        return "\n".join(out), 1

    if path:
        return _one_file(files, path, header, max_bytes)
    return _summary(files, header, number, max_files)
