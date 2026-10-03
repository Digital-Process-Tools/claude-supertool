#!/usr/bin/env python3








from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _git_common import (  
    NOT_A_REPO, _git, _list_conflicts, probe_repo, unanswered_repo_lines,
    use_utf8_stdout,
)
import _secrets  
import _untrusted  






_MARKER_RE = re.compile(r"^(<{7,}|>{7,})(\s|$)")















_SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".php", ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".scala", ".dart",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".sh", ".bash", ".zsh", ".pl", ".lua", ".sql",
})


def _shown(path: str) -> str:





















    return _untrusted.flat(path, disclose_newline=True)


def _is_source_path(path: str) -> bool:

    return os.path.splitext(path)[1].lower() in _SOURCE_EXTS









_MD_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd"})

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S")


















_SETEXT_UNDERLINE_RE = re.compile(r"^[ ]{0,3}(=+|-{2,})[ \t]*\Z")


















_BLOCK_INTERRUPT_RE = re.compile(r"^[ ]{0,3}(>|[-*+][ \t]|1[.)][ \t])")
_THEMATIC_BREAK_STAR_UNDERSCORE_RE = re.compile(r"^[ ]{0,3}([*_])(?:[ \t]*\1){2,}[ \t]*\Z")


def _is_markdown_path(path: str) -> bool:

    return os.path.splitext(path)[1].lower() in _MD_EXTS


def _union_lines(text: str, selected: Optional[set[int]] = None) -> list[tuple[str, bool]]:
















    out: list[tuple[str, bool]] = []
    state = "normal"  
    block_idx = 0
    take_ours = take_theirs = tag = True
    for line in text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if state == "normal":
            if s.startswith("<<<<<<<"):
                block_idx += 1
                tag = selected is None or block_idx in selected
                take_ours, take_theirs = True, tag
                state = "ours"
                continue
            out.append((line, False))
        elif state == "ours":
            if s.startswith("|||||||"):
                state = "base"
            elif s.startswith("======="):
                state = "theirs"
            elif s.startswith(">>>>>>>"):  
                state = "normal"
            elif take_ours:
                out.append((line, tag))
        elif state == "base":
            if s.startswith("======="):
                state = "theirs"
        elif state == "theirs":
            if s.startswith(">>>>>>>"):
                state = "normal"
            elif take_theirs:
                out.append((line, tag))
    return out


def _heading_paths(lines: list[tuple[str, bool]]) -> list[tuple[tuple[str, ...], bool]]:







































    paths: list[tuple[tuple[str, ...], bool]] = []
    stack: list[tuple[int, str]] = []
    fence = ""
    buffer: list[str] = []
    buffer_tagged = False
    for raw, tagged in lines:
        s = raw.strip()
        stripped_line = raw.rstrip("\r\n")
        if fence:
            if s.startswith(fence):
                fence = ""
            buffer, buffer_tagged = [], False
            continue
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            buffer, buffer_tagged = [], False
            continue
        if _HEADING_RE.match(stripped_line):
            level = len(s) - len(s.lstrip("#"))
            while stack and stack[-1][0] >= level:
                stack.pop()
            paths.append((tuple(t for _, t in stack) + (s,), tagged))
            stack.append((level, s))
            buffer, buffer_tagged = [], False
            continue
        underline = _SETEXT_UNDERLINE_RE.match(stripped_line)
        if underline:
            if buffer:
                title = " ".join(buffer)
                level = 1 if underline.group(1)[0] == "=" else 2
                while stack and stack[-1][0] >= level:
                    stack.pop()
                paths.append((tuple(t for _, t in stack) + (title,), tagged or buffer_tagged))
                stack.append((level, title))
            buffer, buffer_tagged = [], False
            continue
        if not s or _BLOCK_INTERRUPT_RE.match(stripped_line) or _THEMATIC_BREAK_STAR_UNDERSCORE_RE.match(stripped_line):
            buffer, buffer_tagged = [], False
            continue
        buffer.append(stripped_line.strip())
        buffer_tagged = buffer_tagged or tagged
    return paths


def _duplicated_headings(path: str, selected: Optional[set[int]] = None) -> list[str]:

























    if not _is_markdown_path(path):
        return []
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    headings = _heading_paths(_union_lines(text, selected))
    if not headings:
        return []
    emitted = Counter(p for p, _ in headings)
    from_a_hunk = {p for p, tagged in headings if tagged}

    dups: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for p, _tagged in headings:
        if emitted[p] < 2 or p not in from_a_hunk or p in seen:
            continue
        seen.add(p)
        dups.append(f"{p[-1]} (under {p[-2]})" if len(p) > 1 else p[-1])
    return dups


def _union_attr_paths(paths: list[str]) -> set[str]:







    if not paths:
        return set()
    res = _git(["check-attr", "merge", "--", *paths])
    if res.returncode != 0:
        return set()
    out: set[str] = set()
    for line in res.stdout.splitlines():

        if line.endswith(": merge: union"):
            out.add(line[: -len(": merge: union")])
    return out


_REFUSAL = (
    "source file — 'both' concatenates both versions (the result parses; the code "
    "runs twice); refused"
)


def _guarded_paths(paths: list[str], side: str, force: bool) -> set[str]:

    if side != "both" or force:
        return set()
    candidates = [p for p in paths if _is_source_path(p)]
    if not candidates:
        return set()
    return set(candidates) - _union_attr_paths(candidates)


def _heading_refusal(dups: list[str]) -> str:





    shown = "; ".join(repr(h) for h in dups[:3])
    more = f" (+{len(dups) - 3} more)" if len(dups) > 3 else ""
    return (f"structured document — union would emit {len(dups)} heading(s) twice "
            f"under one section: {shown}{more}, reparenting the lines between the "
            f"two copies under the first; refused")


def _print_refusal_help() -> None:
    print("Next: resolve these by hand — ./supertool 'git-conflicts' to inspect, "
          "./supertool 'git-resolve:::ours:::PATH' / ':::theirs:::PATH' to take one side,")
    print("      or append 'force' (git-resolve:::both:::PATH:::force) to union anyway "
          "and verify the result yourself.")


def _union_file(path: str) -> tuple[bool, str]:








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
    state = "normal"  
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
            elif s.startswith(">>>>>>>"):  
                state = "normal"
            else:
                out.append(line)
        elif state == "base":
            if s.startswith("======="):
                state = "theirs"

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



    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("<<<<<<<"))


def _read_text_for_hunks(path: str) -> Optional[str]:






    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _block_ranges(lines: list[str]) -> list[tuple[int, int]]:







    ranges: list[tuple[int, int]] = []
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.startswith("<<<<<<<") and start is None:
            start = i
        elif line.startswith(">>>>>>>") and start is not None:
            ranges.append((start, i))
            start = None
    if start is not None:
        ranges.append((start, len(lines) - 1))
    return ranges


def _hunk_note(pre_text: Optional[str], post_text: Optional[str]) -> str:
























































    if pre_text is None or post_text is None:
        return "outside-conflict check: not available (could not read the pre/post-resolution snapshot)"











    pre_lines = _untrusted.split_lines(pre_text)
    post_lines = _untrusted.split_lines(post_text)
    blocks = _block_ranges(pre_lines)
    if not blocks:
        return ("outside-conflict check: not available (the pre-checkout "
                 "snapshot carried no conflict markers to compare against — "
                 "e.g. a modify/delete conflict, where the surviving side's "
                 "content is already what git left in the working tree)")

    matcher = difflib.SequenceMatcher(None, pre_lines, post_lines, autojunk=False)
    ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]




    hunks: list[tuple[int, int]] = []
    for _tag, a1, a2, _b1, _b2 in ops:
        if hunks and hunks[-1][1] == a1:
            hunks[-1] = (hunks[-1][0], a2)
        else:
            hunks.append((a1, a2))

    def _outside_spans(a1: int, a2: int) -> list[tuple[int, int]]:




        if a2 <= a1:




            for bstart, bend in blocks:
                if bstart <= a1 <= bend + 1:
                    return []
            return [(a1, a1)]
        covered = sorted(
            (max(a1, bstart), min(a2, bend + 1))
            for bstart, bend in blocks
            if max(a1, bstart) < min(a2, bend + 1)
        )
        merged: list[tuple[int, int]] = []
        for s, e in covered:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        spans: list[tuple[int, int]] = []
        cur = a1
        for s, e in merged:
            if cur < s:
                spans.append((cur, s))
            cur = max(cur, e)
        if cur < a2:
            spans.append((cur, a2))
        return spans

    outside: list[tuple[int, int]] = []
    for a1, a2 in hunks:
        outside.extend(_outside_spans(a1, a2))
    base = f"{len(blocks)} conflict block(s), {len(hunks)} hunk(s) changed"
    if not outside:
        return base

    def _fmt(a1: int, a2: int) -> str:
        if a2 > a1:
            return f"lines {a1 + 1}-{a2}"
        return "before line 1" if a1 == 0 else f"after line {a1}"

    shown = ", ".join(_fmt(a1, a2) for a1, a2 in outside[:5])
    more = f" (+{len(outside) - 5} more)" if len(outside) > 5 else ""
    return f"{base} — {len(outside)} outside any conflict ({shown}{more})"


def _resolve_blocks(path: str, side: str, selected: set[int]) -> tuple[bool, str, int, int]:












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
    state = "normal"  
    block_idx = 0
    keep = False  
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
                out.append(line)  
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
            elif s.startswith(">>>>>>>"):  
                state = "normal"
            elif side in ("ours", "both"):
                out.append(line)
        elif state == "base":
            if s.startswith("======="):
                state = "theirs"

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










_SYNTAX_VALIDATORS = (
    "phplint", "xmllint", "jsonlint", "node-check", "py-compile",
    "bash-check", "yaml-check", "yaml-check-yaml", "inilint", "tomllint",
    "ruby-check", "terraform-check", "gofmt-check",
)






_SYNTAX_FILTER = "@syntax"





_SKIPPED_ROW = re.compile(r"^([\w-]+)\s*:\s*skipped\b\s*[—-]*\s*(.*)$")  
_RESULT_ROW = re.compile(r"^([\w-]+)\s*:\s*(ok|(\d+) err)\b")



_CALL_FOOTER = re.compile(r"^\[(result|branch)\b")


def _skip_summary(skipped: list) -> str:











    parts = [f"{tool} ({_untrusted.flat(_redacted(why))})" if why else tool
             for tool, why in skipped]
    text = ", ".join(parts)
    if len(text) > _CHILD_DETAIL_MAX:
        text = text[:_CHILD_DETAIL_MAX - 1] + "…"
    return text


def _digest_block(block: str) -> Optional[str]:






















    fails: list[str] = []
    skipped: list = []
    ran = False
    for line in block.splitlines():
        s = _SKIPPED_ROW.match(line)
        if s:
            skipped.append((s.group(1), s.group(2).strip()))
            continue
        m = _RESULT_ROW.match(line)
        if not m:
            continue
        ran = True
        if m.group(3):
            fails.append(f"{m.group(1)} {m.group(3)} err")
    if not ran:
        if skipped:
            return _not_checked(_skip_summary(skipped))
        return None
    base = ("validate: ⚠ " + ", ".join(fails)) if fails else "validate: ok"
    if skipped:
        base += f" | ⚠ not checked by {_skip_summary(skipped)}"
    return base


def _not_checked(reason: str) -> str:









    return f"validate: ⚠ not checked ({reason})"







_CHILD_DETAIL_MAX = 120


def _redacted(text: str) -> str:



















    redacted, _ = _secrets.redact(text)
    return redacted


def _child_failed(res: "subprocess.CompletedProcess[str]") -> str:













    how = (f"killed by signal {-res.returncode}" if res.returncode < 0
           else f"exited {res.returncode}")
    first = next((ln for ln in (res.stderr or "").splitlines() if ln.strip()), "")
    if not first:
        return f"validator {how}"
    detail = _untrusted.flat(_redacted(first.strip()))
    if len(detail) > _CHILD_DETAIL_MAX:
        detail = detail[:_CHILD_DETAIL_MAX - 1] + "…"
    return f"validator {how}: {detail}"


def _validate_paths(paths: list[str]) -> dict[str, Optional[str]]:
































    digests: dict[str, Optional[str]] = {}
    files: list[str] = []
    for p in paths:
        if os.path.isfile(p):
            digests[p] = None
            files.append(p)
        else:
            digests[p] = _not_checked("file not found")
    if not files:
        return digests
    st = Path(__file__).resolve().parents[2] / "supertool.py"
    if not st.is_file():
        for p in files:
            digests[p] = _not_checked("supertool.py not found")
        return digests


    for tool_filter in (_SYNTAX_FILTER, list(_SYNTAX_VALIDATORS)):
        payload = json.dumps({"paths": files, "tools": tool_filter})
        try:
            res = subprocess.run(
                [sys.executable, str(st), "validate:@-"],
                input=payload,
                capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            for p in files:
                digests[p] = _not_checked("timed out")
            return digests
        except OSError as exc:
            for p in files:
                digests[p] = _not_checked(f"could not run: {exc.__class__.__name__}")
            return digests










        if res.returncode != 0:
            reason = _child_failed(res)
            for p in files:
                digests[p] = _not_checked(reason)
            return digests
        out = res.stdout











        blocks: list[str] = []
        buf: list[str] = []
        started = False
        for line in out.splitlines():
            if re.match(r"^validate:\s+", line):
                if started:
                    blocks.append("\n".join(buf))
                buf = []
                started = True
            elif _CALL_FOOTER.match(line):










                continue
            elif started:
                buf.append(line)
        if started:
            blocks.append("\n".join(buf))
















        if not blocks:
            if "no validators matched filter" in out:

                continue
            if "no validators configured" in out:






                for p in files:
                    digests[p] = _not_checked("no validators configured")
                return digests











        if len(blocks) != len(files):
            reason = (f"validator output had {len(blocks)} block(s) "
                      f"for {len(files)} file(s)")
            for p in files:
                digests[p] = _not_checked(reason)
            return digests
        for path, block in zip(files, blocks):
            digests[path] = _digest_block(block)
        return digests



    for p in files:
        digests[p] = _not_checked("no syntax validator selected")
    return digests


def _resolve_partial(path: str, side: str, selected: set[int], force: bool = False) -> int:







    blocks_label = ", ".join(str(b) for b in sorted(selected))
    print(f"# git-resolve: {side} block(s) {blocks_label} in {_shown(path)}")

    if _guarded_paths([path], side, force):
        print(f"  ⊘ {_shown(path)}: {_REFUSAL}")
        print("\nResolved blocks: 0 | Refused: 1 | Not staged (still conflicted).")
        _print_refusal_help()
        return 1


    dups = _duplicated_headings(path, selected) if side == "both" and not force else []
    if dups:
        print(f"  ⊘ {_shown(path)}: {_heading_refusal(dups)}")
        print("\nResolved blocks: 0 | Refused: 1 | Not staged (still conflicted).")
        _print_refusal_help()
        return 1

    ok, err, resolved, total = _resolve_blocks(path, side, selected)
    if not ok:
        print(f"  ✗ {_shown(path)}: {err}")
        return 1

    remaining_blocks = total - resolved




    marker_lines = _scan_markers(path)
    if marker_lines:
        print(f"  ~ {_shown(path)}: {resolved} of {total} block(s) resolved, "
              f"file still conflicted")
        digest = _validate_paths([path]).get(path)
        if digest:
            print(f"      {digest}")
        print(f"\nResolved blocks: {resolved} | Remaining blocks: {remaining_blocks} | Not staged (still conflicted).")
        print("Next: resolve the remaining block(s), then ./supertool 'git-resolve:::SIDE:::PATH' (whole file) or git add once clean.")
        print("Inspect: ./supertool 'git-conflicts'")
        return 0

    add = _git(["add", "--", path])
    if add.returncode != 0:

        print(f"  ✗ {_shown(path)}: "
              f"{_untrusted.flat(add.stderr.strip() or add.stdout.strip())}")
        return 1
    digest = _validate_paths([path]).get(path)
    print(f"  ✓ {_shown(path)}: {resolved} of {total} block(s) resolved — "
          f"all blocks clean, staged")
    print(f"      markers: clean | {digest}" if digest else "      markers: clean")
    print(f"\nResolved blocks: {resolved} | Remaining blocks: {remaining_blocks}")
    return 0


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 3:
        print("ERROR: usage: resolve.py SIDE PATH[,PATH...] [BLOCKS] [force]")
        print("  SIDE — 'ours', 'theirs', or 'both' (union: keep both sides)")
        print("  PATH — conflicted file path, comma-separated list, or 'all' for every UU file")
        print("  BLOCKS — optional 1-indexed block list (e.g. '1,3') — per-file, as git-conflicts numbers them")
        print("  force — union source files too ('both' is refused on them by default)")
        return 1

    side = sys.argv[1].lower()
    target = sys.argv[2]
    force = False
    blocks_arg = ""
    for tok in sys.argv[3:]:
        if tok.strip().lower() == "force":
            force = True
        elif tok.strip():
            blocks_arg = tok

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




    inside, why = probe_repo(_git)
    if inside is None:
        for line in unanswered_repo_lines(why):
            print(line)
        print("  Nothing was resolved.")
        return 1
    if not inside:
        print(NOT_A_REPO)
        return 1

    all_conflicts, unavailable = _list_conflicts()
    if unavailable:
        print("# git-resolve")
        print(f"Conflicts: UNKNOWN — `git diff --name-only --diff-filter=U` "
              f"did not answer: {unavailable}")
        print("Nothing was inspected, so nothing was resolved. Re-run.")
        return 1
    if not all_conflicts:
        print("# git-resolve")
        print("No conflicted files. Nothing to resolve.")
        return 0























    if target == "all":
        targets = all_conflicts
    else:

        requested = [p.strip() for p in target.split(",") if p.strip()]
        unknown = [p for p in requested if p not in all_conflicts]
        if unknown:
            print(f"ERROR: not conflicted: "
                  f"{', '.join(repr(_shown(p)) for p in unknown)}")
            print(f"Conflicts: "
                  f"{', '.join(_shown(p) for p in all_conflicts) or '(none)'}")
            return 1
        targets = requested


    if selected and (target == "all" or len(targets) != 1):
        print("ERROR: BLOCKS selector requires exactly one PATH (block numbers are per-file).")
        return 1

    if selected:
        return _resolve_partial(targets[0], side, selected, force)

    print(f"# git-resolve: {side} ({len(targets)} file(s))")




    guarded = _guarded_paths(targets, side, force)

    resolved: list[str] = []
    refused: list[tuple[str, str]] = []
    forced_source: list[str] = []
    forced_headings: list[str] = []
    failed: list[tuple[str, str]] = []
    digests: dict[str, Optional[str]] = {}
    hunk_notes: dict[str, str] = {}
    for path in targets:
        if path in guarded:
            refused.append((path, _REFUSAL))
            continue
        if side == "both":



            dups = _duplicated_headings(path)
            if dups and not force:
                refused.append((path, _heading_refusal(dups)))
                continue
            if force:
                if dups:
                    forced_headings.append(path)
                if _is_source_path(path):
                    forced_source.append(path)
            ok, err = _union_file(path)
            if not ok:
                failed.append((path, err))
                continue
        else:




            pre_text = _read_text_for_hunks(path)
            co = _git(["checkout", f"--{side}", "--", path])
            if co.returncode != 0:






                failed.append((path, _untrusted.flat(
                    co.stderr.strip() or co.stdout.strip())))
                continue
            hunk_notes[path] = _hunk_note(pre_text, _read_text_for_hunks(path))

        marker_lines = _scan_markers(path)
        if marker_lines:
            shown = ", ".join(str(n) for n in marker_lines[:5])
            more = f" (+{len(marker_lines) - 5} more)" if len(marker_lines) > 5 else ""
            failed.append((path, f"conflict markers remain at line(s) {shown}{more} — not staged"))
            continue
        add = _git(["add", "--", path])
        if add.returncode != 0:
            failed.append((path, _untrusted.flat(
                add.stderr.strip() or add.stdout.strip())))
            continue
        resolved.append(path)


    if resolved:
        digests = _validate_paths(resolved)

    for path in resolved:
        print(f"  ✓ {_shown(path)}")
        digest = digests.get(path)
        line = f"      markers: clean | {digest}" if digest else "      markers: clean"
        if path in forced_source:
            line += " | ⚠ source union — verify manually"
        if path in forced_headings:
            line += " | ⚠ duplicated heading(s) — verify section structure"
        print(line)







        hunk_note = hunk_notes.get(path)
        if hunk_note:
            print(f"      {hunk_note}")
    for path, reason in refused:
        print(f"  ⊘ {_shown(path)}: {reason}")
    for path, err in failed:
        print(f"  ✗ {_shown(path)}: {err}")





    notes: list[str] = []
    if forced_source:
        notes.append(f"{len(forced_source)} source file(s) unioned — 'both' "
                     f"concatenates; verify manually")
    if forced_headings:
        notes.append(f"{len(forced_headings)} file(s) with duplicated heading(s) — "
                     f"verify section structure")
    note = f" ({'; '.join(notes)})" if notes else ""
    refused_seg = f"Refused: {len(refused)} | " if refused else ""

    remaining, remaining_unavailable = _list_conflicts()
    if remaining_unavailable:
        print(f"\nResolved: {len(resolved)}{note} | {refused_seg}Failed: {len(failed)} | "
              f"Remaining: UNKNOWN — git did not answer: {remaining_unavailable}")
        print("Do NOT continue the merge on this report — re-run to confirm "
              "nothing is still conflicted.")
        return 1
    print(f"\nResolved: {len(resolved)}{note} | {refused_seg}Failed: {len(failed)} | "
          f"Remaining: {len(remaining)}")
    if refused:
        _print_refusal_help()
    if remaining:
        print("Still conflicted:")
        for p in remaining:
            print(f"  {_shown(p)}")
        print("Next: ./supertool 'git-conflicts' to inspect, or rerun git-resolve.")
    elif resolved:

        gd = _git(["rev-parse", "--git-dir"]).stdout.strip()
        from os.path import exists, join
        if exists(join(gd, "MERGE_HEAD")):
            print("Next: ./supertool 'git-commit:::Merge resolved' (or git merge --continue)")
        elif exists(join(gd, "rebase-merge")) or exists(join(gd, "rebase-apply")):
            print("Next: git rebase --continue")
        elif exists(join(gd, "CHERRY_PICK_HEAD")):
            print("Next: git cherry-pick --continue")
        else:
            print("Next: ./supertool 'git-commit:::MESSAGE' to commit the resolution.")

    return 0 if not failed and not refused else 1


if __name__ == "__main__":
    sys.exit(main())
