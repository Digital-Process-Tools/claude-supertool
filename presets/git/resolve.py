#!/usr/bin/env python3
"""Git resolve — pick ours/theirs/both for a conflicted PATH (or all) + stage.

ours/theirs: checkout --SIDE PATH + git add PATH (atomic).
both: union — strip conflict markers, keep both sides, write back + git add.
Receipt shows which files were resolved and how many conflicts remain.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Unambiguous conflict markers — `<<<<<<<` / `>>>>>>>` at line start. A bare row
# of `=======` is intentionally NOT matched: it is legitimate decoration (RST/MD
# underlines, comment rules) and a real leftover always carries the angle markers
# too, so the angle scan never misses an actual unresolved hunk.
_MARKER_RE = re.compile(r"^(<{7,}|>{7,})(\s|$)")


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _list_conflicts() -> list[str]:
    res = _git(["diff", "--name-only", "--diff-filter=U"])
    if res.returncode != 0:
        return []
    return [l for l in res.stdout.splitlines() if l.strip()]


def _union_file(path: str) -> tuple[bool, str]:
    """Strip conflict markers from PATH, keeping both sides (ours then theirs).

    Mirrors git's ``merge=union`` driver: for every conflict hunk, drop the
    ``<<<<<<<``, ``=======`` and ``>>>>>>>`` marker lines and concatenate both
    content blocks. diff3 ``|||||||`` base sections are dropped. Writes the
    result back to PATH atomically (single rewrite — no formatter runs between
    marker removals). Returns (ok, error_message).
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return False, f"cannot read: {e}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "not a UTF-8 text file (binary conflict?)"

    out: list[str] = []
    state = "normal"  # normal | ours | base | theirs
    saw_conflict = False
    for line in text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if state == "normal":
            if s.startswith("<<<<<<<"):
                state, saw_conflict = "ours", True
                continue
            out.append(line)
        elif state == "ours":
            if s.startswith("|||||||"):
                state = "base"
            elif s.startswith("======="):
                state = "theirs"
            elif s.startswith(">>>>>>>"):  # malformed — recover, keep nothing
                state = "normal"
            else:
                out.append(line)
        elif state == "base":
            if s.startswith("======="):
                state = "theirs"
            # else: drop base content
        elif state == "theirs":
            if s.startswith(">>>>>>>"):
                state = "normal"
            else:
                out.append(line)

    if state != "normal":
        return False, "unterminated conflict marker (file unchanged)"
    if not saw_conflict:
        return False, "no conflict markers found (file unchanged)"

    try:
        with open(path, "wb") as fh:
            fh.write("".join(out).encode("utf-8"))
    except OSError as e:
        return False, f"cannot write: {e}"
    return True, ""


def _count_blocks(path: str) -> int:
    """Number of conflict blocks in PATH, counted exactly as `git-conflicts`
    numbers them: one per ``<<<<<<<`` marker line, in file order.
    """
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("<<<<<<<"))


def _resolve_blocks(path: str, side: str, selected: set[int]) -> tuple[bool, str, int, int]:
    """Resolve only the SELECTED conflict blocks of PATH; leave the rest verbatim.

    Blocks are 1-indexed in file order — the same numbering `git-conflicts`
    prints. For each selected block, keep the chosen side (``ours``/``theirs``)
    or, for ``both``, the union (ours then theirs, diff3 base dropped) and drop
    that block's markers. Unselected blocks — including their markers — are
    written back untouched, so the file stays conflicted and the caller's hard
    gate refuses to stage it.

    Returns ``(ok, error, num_resolved, num_total)``: ``num_total`` is every
    conflict block in the file, ``num_resolved`` how many the selector matched.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return False, f"cannot read: {e}", 0, 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "not a UTF-8 text file (binary conflict?)", 0, 0

    out: list[str] = []
    state = "normal"  # normal | ours | base | theirs
    block_idx = 0
    keep = False  # is the current block selected for resolution?
    total = 0
    resolved = 0
    for line in text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if state == "normal":
            if s.startswith("<<<<<<<"):
                block_idx += 1
                total += 1
                keep = block_idx in selected
                if keep:
                    resolved += 1
                    state = "ours"
                    continue
                state = "passthrough"
                out.append(line)  # keep the marker verbatim
            else:
                out.append(line)
        elif state == "passthrough":
            out.append(line)
            if s.startswith(">>>>>>>"):
                state = "normal"
        elif state == "ours":
            if s.startswith("|||||||"):
                state = "base"
            elif s.startswith("======="):
                state = "theirs"
            elif s.startswith(">>>>>>>"):  # malformed — recover
                state = "normal"
            elif side in ("ours", "both"):
                out.append(line)
        elif state == "base":
            if s.startswith("======="):
                state = "theirs"
            # else: drop diff3 base content
        elif state == "theirs":
            if s.startswith(">>>>>>>"):
                state = "normal"
            elif side in ("theirs", "both"):
                out.append(line)

    if state not in ("normal", "passthrough"):
        return False, "unterminated conflict marker (file unchanged)", 0, total
    if total == 0:
        return False, "no conflict markers found (file unchanged)", 0, 0
    unknown = sorted(b for b in selected if b > total)
    if unknown:
        nums = ", ".join(str(b) for b in unknown)
        return False, f"block(s) {nums} out of range — file has {total} block(s)", 0, total

    try:
        with open(path, "wb") as fh:
            fh.write("".join(out).encode("utf-8"))
    except OSError as e:
        return False, f"cannot write: {e}", 0, total
    return True, "", resolved, total


def _scan_markers(path: str) -> list[int]:
    """1-indexed line numbers carrying a leftover conflict marker, else [].

    Reads bytes and decodes UTF-8 — a binary (non-decodable) file scans clean
    (a text marker can't live in it). Used as a hard gate before staging:
    `checkout --ours/theirs` cannot leave markers, but `both`/union and any
    future per-hunk path can, and a staged marker is a broken commit.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [i for i, line in enumerate(text.splitlines(), 1) if _MARKER_RE.match(line)]


# Parser/compiler validators only — the class of check a side-pick can actually
# break. Semantic/diagnostic/style validators (lsp-diag, pyright, psr, tsc-check,
# prettier-check, git-status, …) are deliberately excluded: they report the
# file's pre-existing state, not what the resolve introduced, so they'd cry wolf
# on every resolve. The stack's normal before/after diffing can't filter that
# here — the "before" is a marker-filled conflicted file — so we scope by name.
# A name not present in the live config is simply skipped (safe degradation: no
# digest line rather than a false "ok").
_SYNTAX_VALIDATORS = (
    "phplint", "xmllint", "jsonlint", "node-check", "py-compile",
    "bash-check", "yaml-check", "yaml-check-yaml", "inilint", "tomllint",
    "ruby-check", "terraform-check", "gofmt-check",
)


# Declarative syntax-scope sentinel: supertool's `validate` op selects validators
# that set `"syntax": true` in their spec. Preferred over the hardcoded name list
# above so the scope lives in config; the list remains the fallback for configs
# that predate the flag (see _validate_paths).
_SYNTAX_FILTER = "@syntax"


def _digest_block(block: str) -> Optional[str]:
    """Condense one file's validator rows into a single receipt line.

    Returns ``None`` when no syntax validator ran for this file type.
    """
    fails: list[str] = []
    ran = False
    for line in block.splitlines():
        m = re.match(r"^([\w-]+)\s*:\s*(ok|(\d+) err)\b", line)
        if not m:
            continue
        ran = True
        if m.group(3):
            fails.append(f"{m.group(1)} {m.group(3)} err")
    if not ran:
        return None
    if fails:
        return "validate: ⚠ " + ", ".join(fails)
    return "validate: ok"


def _validate_paths(paths: list[str]) -> dict[str, Optional[str]]:
    """Warn-only post-resolve syntax digest for every resolved file, in ONE call.

    Shells back into supertool's `validate` op (the single source of truth for
    which validator runs on which file type) using the list form
    ``validate:f1,f2,…:FILTER`` — one subprocess for the whole batch instead of
    one per file. Scoped to syntax/parser validators via the declarative
    ``@syntax`` filter, falling back to the hardcoded name list for older configs.
    Output blocks are split on ``validate: PATH`` headers and folded back to each
    path. Returns ``{path: digest_or_None}``; advisory only — never blocks the
    resolve. Missing/timeout → every path maps to ``None``.
    """
    files = [p for p in paths if os.path.isfile(p)]
    digests: dict[str, Optional[str]] = {p: None for p in paths}
    if not files:
        return digests
    st = Path(__file__).resolve().parents[2] / "supertool.py"
    if not st.is_file():
        return digests
    # Prefer the declarative @syntax scope; fall back to the name allowlist so a
    # config that hasn't adopted the flag still gets the same parser-only digest.
    for tool_filter in (_SYNTAX_FILTER, ",".join(_SYNTAX_VALIDATORS)):
        try:
            res = subprocess.run(
                [sys.executable, str(st), f"validate:{','.join(files)}:{tool_filter}"],
                capture_output=True, text=True, timeout=90,
            )
        except (subprocess.TimeoutExpired, OSError):
            return digests
        out = res.stdout
        if "no validators matched filter" in out:
            # @syntax selected nothing (older config) → retry with the name list.
            continue
        if "no validators" in out:
            return digests
        # Split the combined output into per-file blocks on the header lines.
        # Blocks are emitted in the same order as `files` (op_validate_multi
        # guarantees input order), so fold them back positionally — robust to any
        # path normalization the echoed header might apply.
        blocks: list[str] = []
        buf: list[str] = []
        started = False
        for line in out.splitlines():
            if re.match(r"^validate:\s+", line):
                if started:
                    blocks.append("\n".join(buf))
                buf = []
                started = True
            elif started:
                buf.append(line)
        if started:
            blocks.append("\n".join(buf))
        for path, block in zip(files, blocks):
            digests[path] = _digest_block(block)
        return digests
    return digests


def _resolve_partial(path: str, side: str, selected: set[int]) -> int:
    """Resolve only the selected blocks of one file (issue #305).

    A partial resolve always leaves the unselected blocks' markers in place, so
    the file stays conflicted by design — it is NEVER staged. The receipt reports
    "N of M blocks resolved, file still conflicted" and points back at the
    remaining work, honoring the marker hard-gate rather than fighting it.
    """
    blocks_label = ", ".join(str(b) for b in sorted(selected))
    print(f"# git-resolve: {side} block(s) {blocks_label} in {path}")

    ok, err, resolved, total = _resolve_blocks(path, side, selected)
    if not ok:
        print(f"  ✗ {path}: {err}")
        return 1

    remaining_blocks = total - resolved

    # HARD GATE — only a file with no leftover markers may be staged. If the
    # selector happened to cover every block, the file is clean: stage it and
    # report a full resolve. Otherwise the markers stay and we never stage.
    marker_lines = _scan_markers(path)
    if marker_lines:
        print(f"  ~ {path}: {resolved} of {total} block(s) resolved, file still conflicted")
        digest = _validate_paths([path]).get(path)
        if digest:
            print(f"      {digest}")
        print(f"\nResolved blocks: {resolved} | Remaining blocks: {remaining_blocks} | Not staged (still conflicted).")
        print("Next: resolve the remaining block(s), then ./supertool 'git-resolve:::SIDE:::PATH' (whole file) or git add once clean.")
        print("Inspect: ./supertool 'git-conflicts'")
        return 0

    add = _git(["add", "--", path])
    if add.returncode != 0:
        print(f"  ✗ {path}: {add.stderr.strip() or add.stdout.strip()}")
        return 1
    digest = _validate_paths([path]).get(path)
    print(f"  ✓ {path}: {resolved} of {total} block(s) resolved — all blocks clean, staged")
    print(f"      markers: clean | {digest}" if digest else "      markers: clean")
    print(f"\nResolved blocks: {resolved} | Remaining blocks: {remaining_blocks}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("ERROR: usage: resolve.py SIDE PATH[,PATH...] [BLOCKS]")
        print("  SIDE — 'ours', 'theirs', or 'both' (union: keep both sides)")
        print("  PATH — conflicted file path, comma-separated list, or 'all' for every UU file")
        print("  BLOCKS — optional 1-indexed block list (e.g. '1,3') — per-file, as git-conflicts numbers them")
        return 1

    side = sys.argv[1].lower()
    target = sys.argv[2]
    blocks_arg = sys.argv[3] if len(sys.argv) > 3 else ""

    if side not in ("ours", "theirs", "both"):
        print(f"ERROR: SIDE must be 'ours', 'theirs', or 'both', got {side!r}")
        return 1

    selected: set[int] = set()
    if blocks_arg:
        for tok in blocks_arg.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if not tok.isdigit() or int(tok) < 1:
                print(f"ERROR: BLOCKS must be 1-indexed positive integers, got {tok!r}")
                return 1
            selected.add(int(tok))
        if not selected:
            print(f"ERROR: BLOCKS list is empty, got {blocks_arg!r}")
            return 1

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    all_conflicts = _list_conflicts()
    if not all_conflicts:
        print("# git-resolve")
        print("No conflicted files. Nothing to resolve.")
        return 0

    if target == "all":
        targets = all_conflicts
    else:
        # Comma-separated list supported — multi-file resolves in one call
        requested = [p.strip() for p in target.split(",") if p.strip()]
        unknown = [p for p in requested if p not in all_conflicts]
        if unknown:
            print(f"ERROR: not conflicted: {', '.join(repr(p) for p in unknown)}")
            print(f"Conflicts: {', '.join(all_conflicts) or '(none)'}")
            return 1
        targets = requested

    # Block selector is per-file numbered — only meaningful for a single file.
    if selected and (target == "all" or len(targets) != 1):
        print("ERROR: BLOCKS selector requires exactly one PATH (block numbers are per-file).")
        return 1

    if selected:
        return _resolve_partial(targets[0], side, selected)

    print(f"# git-resolve: {side} ({len(targets)} file(s))")

    resolved: list[str] = []
    failed: list[tuple[str, str]] = []
    digests: dict[str, Optional[str]] = {}
    for path in targets:
        if side == "both":
            ok, err = _union_file(path)
            if not ok:
                failed.append((path, err))
                continue
        else:
            co = _git(["checkout", f"--{side}", "--", path])
            if co.returncode != 0:
                failed.append((path, co.stderr.strip() or co.stdout.strip()))
                continue
        # HARD GATE — never stage a file that still carries a conflict marker.
        marker_lines = _scan_markers(path)
        if marker_lines:
            shown = ", ".join(str(n) for n in marker_lines[:5])
            more = f" (+{len(marker_lines) - 5} more)" if len(marker_lines) > 5 else ""
            failed.append((path, f"conflict markers remain at line(s) {shown}{more} — not staged"))
            continue
        add = _git(["add", "--", path])
        if add.returncode != 0:
            failed.append((path, add.stderr.strip() or add.stdout.strip()))
            continue
        resolved.append(path)

    # Syntax digest for the whole batch in ONE supertool call (folded per-file).
    if resolved:
        digests = _validate_paths(resolved)

    for path in resolved:
        print(f"  ✓ {path}")
        digest = digests.get(path)
        print(f"      markers: clean | {digest}" if digest else "      markers: clean")
    for path, err in failed:
        print(f"  ✗ {path}: {err}")

    remaining = _list_conflicts()
    print(f"\nResolved: {len(resolved)} | Failed: {len(failed)} | Remaining: {len(remaining)}")
    if remaining:
        print("Still conflicted:")
        for p in remaining:
            print(f"  {p}")
        print("Next: ./supertool 'git-conflicts' to inspect, or rerun git-resolve.")
    elif resolved:
        # Detect state to give the right continue command
        gd = _git(["rev-parse", "--git-dir"]).stdout.strip()
        from os.path import exists, join
        if exists(join(gd, "MERGE_HEAD")):
            print("Next: ./supertool 'git-commit:::Merge resolved' (or git merge --continue)")
        elif exists(join(gd, "rebase-merge")) or exists(join(gd, "rebase-apply")):
            print("Next: git rebase --continue")
        elif exists(join(gd, "CHERRY_PICK_HEAD")):
            print("Next: git cherry-pick --continue")
        else:
            print("Next: ./supertool 'git-commit:::MSG' to commit the resolution.")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
