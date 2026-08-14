#!/usr/bin/env python3
"""Git resolve — pick ours/theirs/both for a conflicted PATH (or all) + stage.

ours/theirs: checkout --SIDE PATH + git add PATH (atomic).
both: union — strip conflict markers, keep both sides, write back + git add.
     Refused per file on known source extensions (see _SOURCE_EXTS) unless the
     path declares merge=union in .gitattributes or `force` is passed.
Receipt shows which files were resolved and how many conflicts remain.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _git_common import _git, _list_conflicts, use_utf8_stdout  # noqa: E402
import _secrets  # noqa: E402  (a dying adapter puts credentials on stderr — #925)
import _untrusted  # noqa: E402  (a failed child's stderr is untrusted text — #883)


# Unambiguous conflict markers — `<<<<<<<` / `>>>>>>>` at line start. A bare row
# of `=======` is intentionally NOT matched: it is legitimate decoration (RST/MD
# underlines, comment rules) and a real leftover always carries the angle markers
# too, so the angle scan never misses an actual unresolved hunk.
_MARKER_RE = re.compile(r"^(<{7,}|>{7,})(\s|$)")


# Extensions where a union resolve is close to always wrong. `both` concatenates
# both versions of the hunk: on a changelog that is two entries and correct, on a
# function body it is the statement run twice, or two `def`s of the same name
# where the last silently wins. Neither the marker gate nor the syntax digest can
# see that — the concatenation still parses (issue #744).
#
# This list is a heuristic and it is wrong in both directions: an extensionless
# `bin/deploy` shell script is not caught, and a `.sql` file that is only INSERT
# rows would union fine. It is deliberately small — mainstream program text only,
# no structured-data formats (a unioned .json/.xml usually fails the syntax
# validator that already runs, so it is not the silent class this guards). The
# authoritative discriminator, when a repo has bothered to state one, is git's
# own `merge=union` attribute — see _union_attr_paths, which overrides this list.
_SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".php", ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".scala", ".dart",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".sh", ".bash", ".zsh", ".pl", ".lua", ".sql",
})


def _is_source_path(path: str) -> bool:
    """True when PATH's extension is program text a union would corrupt."""
    return os.path.splitext(path)[1].lower() in _SOURCE_EXTS


# Markdown is the format where a union is otherwise right — a changelog conflict is
# two entries, and unioning them is the whole point — but whose *meaning* comes from
# repeating structural headings rather than from line order alone. When the union
# emits one heading twice, every line between the two copies is reparented under the
# first (issue #839): unreleased work reads as shipped. Nothing below merges,
# reorders or de-duplicates anything — it only detects that the union came out
# structurally implausible and hands the decision back.
_MD_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd"})

# ATX headings only. Setext underlines (`---` under a title) are not matched: a bare
# rule is legitimate decoration, and the false positives would land on exactly the
# files this guard must stay quiet about.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S")


def _is_markdown_path(path: str) -> bool:
    """True when PATH's extension is Markdown, whose headings carry structure."""
    return os.path.splitext(path)[1].lower() in _MD_EXTS


def _union_lines(text: str, selected: Optional[set[int]] = None) -> list[tuple[str, bool]]:
    """The union of TEXT, line by line, each tagged with "a hunk put this here".

    The union is the one `_union_file` writes — both sides concatenated, diff3
    ``|||||||`` base dropped — computed in memory so the guard can read the document
    a resolve would produce rather than infer it from the shape of the hunks.

    The tag is what replaces a second render of the file. Rendering the surrounding
    context separately and diffing the two counts looked equivalent and is not: an
    odd number of ``` fence delimiters inside a hunk closes a fence in one rendering
    and leaves it open in the other, so the two parses disagree about which later
    lines are headings at all, and a document that already repeated a heading was
    refused for it. One parse, carrying the attribution, cannot drift from itself.

    Hunks outside ``selected`` contribute their ``ours`` side untagged. They are not
    being resolved, so what they carry is not this resolve's doing.
    """
    out: list[tuple[str, bool]] = []
    state = "normal"  # normal | ours | base | theirs
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
            elif s.startswith(">>>>>>>"):  # malformed — recover, claim nothing
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
    """Every ATX heading in LINES as its ancestor path, in file order, tag carried.

    The path — enclosing headings of lower level, then the heading itself — is what
    makes `### Fixed` under `## [Unreleased]` a different thing from `### Fixed`
    under `## [0.22.0]`. Every changelog repeats section headings once per release,
    so a file-wide count of heading lines answers a question nobody asked.

    Fenced blocks are skipped. This repo's changelog quotes shell constantly and a
    `# run it` comment inside a fence is not structure; reading it as one would
    refuse ordinary entries, and a guard that fires on those trains the override.
    """
    paths: list[tuple[tuple[str, ...], bool]] = []
    stack: list[tuple[int, str]] = []
    fence = ""
    for raw, tagged in lines:
        s = raw.strip()
        if fence:
            if s.startswith(fence):
                fence = ""
            continue
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            continue
        if not _HEADING_RE.match(raw.rstrip("\r\n")):
            continue
        level = len(s) - len(s.lstrip("#"))
        while stack and stack[-1][0] >= level:
            stack.pop()
        paths.append((tuple(t for _, t in stack) + (s,), tagged))
        stack.append((level, s))
    return paths


def _duplicated_headings(path: str, selected: Optional[set[int]] = None) -> list[str]:
    """Headings a union of PATH would emit twice — in file order, deduped.

    The question is about the **resulting document**: does unioning put the same
    heading twice under the same parent, with at least one of the two copies coming
    from a hunk? So the union is rendered once, every heading is read as its ancestor
    path, and each carries whether a hunk put it there.

    #839 asked a narrower one — is the SAME heading line on BOTH sides of one hunk —
    and #911 is the arrangement that slips past it: `### Fixed` inside the hunk on one
    side only, its twin in the surrounding context git had already merged. The union
    still emits two, the hunk still looks internally clean, and the receipt still said
    `markers: clean`. A guard that reads hunk shape can only ever be right about the
    boundaries someone thought of; reading the output cannot be fooled by where the
    boundary fell.

    Requiring one copy to come from a hunk is what keeps a document that ALREADY
    repeats a heading — malformed, but not by this resolve — out of the refusal. Note
    it is deliberately not "would picking a side duplicate it too": in #911's
    arrangement taking `theirs` also lands two `### Fixed`, and excusing the union on
    that ground would excuse the exact case the issue is about.

    Non-Markdown paths return ``[]`` — the ``#`` heading grammar is Markdown's, and a
    guard should only have opinions about a format it can read. ``selected`` restricts
    the scan to those 1-indexed hunks, for a partial resolve.
    """
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
    """Subset of PATHS that .gitattributes declares as ``merge=union``.

    A repo that sets the attribute has already answered the question this guard
    asks, so those paths union without a prompt. ONE ``git check-attr`` call for
    the whole batch; any failure returns the empty set — the fallback is always
    to refuse, never to union.
    """
    if not paths:
        return set()
    res = _git(["check-attr", "merge", "--", *paths])
    if res.returncode != 0:
        return set()
    out: set[str] = set()
    for line in res.stdout.splitlines():
        # Format: "<path>: merge: <value>"
        if line.endswith(": merge: union"):
            out.add(line[: -len(": merge: union")])
    return out


_REFUSAL = (
    "source file — 'both' concatenates both versions (the result parses; the code "
    "runs twice); refused"
)


def _guarded_paths(paths: list[str], side: str, force: bool) -> set[str]:
    """Paths where `both` must not run: source text, no merge=union, not forced."""
    if side != "both" or force:
        return set()
    candidates = [p for p in paths if _is_source_path(p)]
    if not candidates:
        return set()
    return set(candidates) - _union_attr_paths(candidates)


def _heading_refusal(dups: list[str]) -> str:
    """Refusal text that names the headings it saw.

    A refusal that does not say what it found is a refusal you override blind, which
    is the same defect one layer down.
    """
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
        # NOT flattened, and that is a measured decision rather than an
        # oversight (#1638). This reason reaches the same `✗ PATH: REASON` row
        # as the three child-stream relays below, but `str(OSError)` reprs the
        # filename, so a U+2028 in a conflicted path arrives here already
        # spelled as its six-character escape — ASCII that cannot open a line.
        # Verified on CPython 3.14; pinned by the 1638 test file.
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


#: A validator that matched the file and then declined to run — the row
#: `_validator_render_row` emits for `{"skipped": reason}`. Its own vocabulary
#: for the third state, arriving inside a batch that otherwise succeeded.
_SKIPPED_ROW = re.compile(r"^([\w-]+)\s*:\s*skipped\b\s*[—-]*\s*(.*)$")  # anchored-ok: matched per line of a validator block
_RESULT_ROW = re.compile(r"^([\w-]+)\s*:\s*(ok|(\d+) err)\b")

#: supertool's own per-call footers, which follow the last file's block and
#: belong to no file. See the fold loop in `_validate_paths`.
_CALL_FOOTER = re.compile(r"^\[(result|branch)\b")


def _skip_summary(skipped: list) -> str:
    """`tool (why)` for each declined validator, bounded to one short cell.

    The `why` is the adapter's own text — `phpstan` declining because it could
    not authenticate names the URL it tried — so it gets the same two passes a
    dead child's stderr gets, and for the same two reasons: redaction (#925),
    and the one-line rule (#883/#895) because this cell is interpolated into
    `markers: clean | {digest}` at column 0 exactly like the other one. It had
    the first and not the second; a carriage return in a decline reason
    overwrites the line the receipt is made of, which is #851 through a second
    door. The tool name is ours and is at risk from neither.
    """
    parts = [f"{tool} ({_untrusted.flat(_redacted(why))})" if why else tool
             for tool, why in skipped]
    text = ", ".join(parts)
    if len(text) > _CHILD_DETAIL_MAX:
        text = text[:_CHILD_DETAIL_MAX - 1] + "…"
    return text


def _digest_block(block: str) -> Optional[str]:
    """Condense one file's validator rows into a single receipt line.

    Returns ``None`` when no syntax validator ran for this file type — a real
    answer about the world, which the caller deliberately renders as nothing.

    A validator that *matched* this file and then declined is a fourth row
    shape, and until #880 this function did not match it: `phplint : skipped —
    php not installed` set neither `ran` nor `fails`, so the file digested to
    the same `None`, and a `.php` conflict staged on a machine without php
    reported ``markers: clean`` with nothing beside it. Byte-identical to a
    file whose parser ran and passed.

    That route survived #883, which put every *batch*-level decline behind
    `_not_checked` — the child crashed, timed out, was never found, folded to
    the wrong block count. Here the child ran fine and emitted a well-formed
    block; the decline is one row inside it. It is also the likeliest of the
    four to be met in practice, because it needs no crash, only a missing
    interpreter.

    A skip beside a pass still costs a word: the reader takes this line as the
    verdict for the file, and half-checked may not render as checked.
    """
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
    """The digest line for a file whose syntax check never ran.

    Distinct from ``None``, which means the validators ran and none of them
    handles this file type — a real answer, and the reason the caller prints
    nothing for it. "Could not run" is not that answer, and rendering the two
    the same way is the defect class this repo is organised around: the caller
    prints ``markers: clean`` and the missing digest reads exactly like a check
    that passed (#880). So it says so, on the line, in the render.
    """
    return f"validate: ⚠ not checked ({reason})"


#: Cap on how much of a failed child's stderr reaches the receipt. This line is
#: one cell of a resolve report, not a log viewer: it has to distinguish *could
#: not run* from *nothing to say*, and the first line of stderr does that. It
#: does not have to diagnose, and a traceback pasted whole would push the
#: `markers: clean` it hangs off out of view.
_CHILD_DETAIL_MAX = 120


def _redacted(text: str) -> str:
    """Strip credential-shaped values out of text a child process wrote (#925).

    Validator adapters shell out, and a child dying on an auth error puts the
    credential it tried on stderr: a `user:token@host` clone URL, an
    `Authorization: Bearer …`, a `GITLAB_TOKEN=…` echoed by a wrapper. That
    line then lands verbatim in the resolve receipt, which is a document people
    paste into issues and PR comments.

    `presets/_secrets` existed for exactly this shape and was wired only into
    `claude-log` / `devto` / `bluesky` — named call sites rather than output
    boundaries — so validate/resolve bypassed it.

    **Order matters and is pinned.** This runs before `_CHILD_DETAIL_MAX`
    truncation. Redacting the already-cut cell would find a token too short for
    its own rule to match and ship its head, which is a fix that reads as one
    and is not. It makes no claim of completeness — see `_secrets`' own
    docstring: detection is by shape, so a credential this module does not
    recognise still passes through here.
    """
    redacted, _ = _secrets.redact(text)
    return redacted


def _child_failed(res: "subprocess.CompletedProcess[str]") -> str:
    """Why the validate child did not answer — one bounded, one-line cell.

    A negative returncode is a POSIX signal, and that case is named separately
    because it is the one that arrives *with* a complete-looking reply on
    stdout: an OOM kill after the last block was flushed.

    stderr is a child's text and the digest is interpolated into a line this
    tool owns at column 0, so it goes through ``_untrusted.flat`` — the same
    one-line rule ``_flat_cell`` applies in ``supertool.py`` (#895), covering
    exactly the ten separators ``str.splitlines()`` splits on plus the cursor
    movement that removes a line rather than adding one (#851). This is the
    call to that rule, deliberately not a second copy of it.
    """
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
    """Warn-only post-resolve syntax digest for every resolved file, in ONE call.

    Shells back into supertool's `validate` op (the single source of truth for
    which validator runs on which file type) through its ``@payload`` route —
    one subprocess for the whole batch instead of one per file. Scoped to
    syntax/parser validators via the declarative ``@syntax`` filter, falling
    back to the hardcoded name list for older configs. Output blocks are split
    on ``validate: PATH`` headers and folded back to each path positionally.
    Returns ``{path: digest_or_None}``; advisory only — never blocks the resolve.

    **The payload rather than the colon form** ``validate:f1,f2,…:FILTER``
    (#876, #878). That form joins on ``:`` and ``,`` and has no escape for
    either, and both are characters a real filename contains: ``x:ruff``
    re-parses so the field the receiver reads as the filter is not the one this
    module chose, and ``a,b.py`` re-parses into two paths, neither of them real.
    Argv is list-form, so that was never a shell exposure — it was a scope one.

    **Filtering those paths out at this end was tried first, and the shape of
    why it failed is the lesson.** The receiver's tokenizer already reassembles
    a Windows drive letter, per comma-segment, precisely so the list form
    carries ``D:\\a\\x.php,D:\\a\\y.php``. A sender-side ``":" not in p`` is a
    second, cruder copy of that rule written at the wrong end of the pipe and
    missing its only interesting case — so it excluded exactly the paths the
    receiver handles best, on the one platform where every absolute path has a
    colon in it, and excluded them *silently*. Loud exotic bug, quiet universal
    one.

    The answer is not to re-derive the tokenizer here — that rule would have to
    stay in sync forever, and every future caller would have to re-derive it
    too. It is to hand the receiver a field it never tokenizes. Nothing is
    excluded, and every path is digestible on every platform.
    """
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
    # Prefer the declarative @syntax scope; fall back to the name allowlist so a
    # config that hasn't adopted the flag still gets the same parser-only digest.
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
        # The exit status is a fact this function already had in hand and threw
        # away (#883). Two shapes, one state. A child that died before writing
        # anything lands zero blocks, and the count guard below then reported it
        # as a *fold* problem — the wrong actor, and without the one line of
        # stderr that says what broke. A child killed *after* a complete reply
        # passes that guard entirely and digests to `validate: ok`, which is the
        # strongest claim this tool can make about a run that did not finish.
        # No retry on the second filter pass either: a non-zero exit is not
        # "that filter selected nothing", so retrying would run the failure
        # twice and report the second one.
        if res.returncode != 0:
            reason = _child_failed(res)
            for p in files:
                digests[p] = _not_checked(reason)
            return digests
        out = res.stdout
        # Split the combined output into per-file blocks on the header lines.
        # Blocks are emitted in the same order as `files` and one per file —
        # op_validate_multi guarantees both, the second one only since #881,
        # where a filename containing newlines emitted three headers for two
        # files. So fold them back positionally: robust to any path
        # normalization the echoed header might apply, and not dependent on
        # the header's *content* for anything at all. That last clause was
        # written by #884 and was false when written — the two status
        # substring tests below the split read the header's content, which is
        # #888. It is true of everything above this line, and the tests are
        # now gated on there being no header at all.
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
                # Not this file's rows. `[result] ...` and `[branch: ...]`
                # describe the whole call and print after the last block with no
                # header of their own, so the splitter above folds them into
                # whichever file sorted last. Inert today only because
                # `_RESULT_ROW` and `_SKIPPED_ROW` both anchor on a word
                # character and these open with `[` -- an accident, not a
                # guarantee, and #990 turned it from a decline-only case into
                # every run. Dropped explicitly, so the invariant this file is
                # built on -- a block holds that file's own content -- is
                # enforced rather than lucky.
                continue
            elif started:
                buf.append(line)
        if started:
            blocks.append("\n".join(buf))
        # "Did the validator run at all?" answered structurally, by whether a
        # block was emitted — not by substring-testing the stream (#888).
        # `op_validate_multi` returns "no validators configured" / "no
        # validators matched filter" *instead of* every block, never beside
        # one, so a status message is an output with no header in it. The old
        # test read the combined stdout, which carries one `validate: <path>`
        # header per file, so a file named `no validators.py` answered the
        # question for the whole batch and every neighbour came back `None` —
        # the state meaning "no validator handles this type", which the caller
        # renders as `markers: clean` with no digest line. A file with a real
        # syntax error was staged and reported clean, and no crafted character
        # was needed, only a plausible name. #881 stopped a filename adding a
        # line to this stream; it never stopped one containing a string, and a
        # longer or more specific substring would be the same bug with a
        # smaller target. Gating on "no blocks" also keeps a validator that
        # quotes file content back in a row from reaching these tests.
        if not blocks:
            if "no validators matched filter" in out:
                # @syntax selected nothing (older config) → retry the name list.
                continue
            if "no validators configured" in out:
                # Not an error — the child ran fine — but not the answer `None`
                # encodes either. `None` means the validators ran and none of
                # them handles this file type, which the render deliberately
                # prints as nothing. Here none was ever *considered*, so
                # leaving it silent lets a config with no validators at all
                # report every resolved conflict as clean.
                for p in files:
                    digests[p] = _not_checked("no validators configured")
                return digests
            # Anything else with no blocks — a crash, an ERROR line — is not a
            # clean bill. Fall through to the count check, which says so.
        # A fold that cannot account for its own inputs must say so. `zip`
        # truncates silently, and every file past the mismatch then takes some
        # other file's verdict — which is how #881 turned a syntax error into
        # `validate: ok`. Not unreachable, whatever the emitter guarantees:
        # #884 called it that and #886 reached it two code points later, with a
        # filename separated by U+2028, which the flattener did not yet cover.
        # This guard is the only reason that gap was a denial rather than a
        # second forged clean. The cost of being wrong here is a false clean
        # bill, so it stays whatever the layer above currently promises.
        if len(blocks) != len(files):
            reason = (f"validator output had {len(blocks)} block(s) "
                      f"for {len(files)} file(s)")
            for p in files:
                digests[p] = _not_checked(reason)
            return digests
        for path, block in zip(files, blocks):
            digests[path] = _digest_block(block)
        return digests
    # Both passes declined: `@syntax` selected nothing and neither did the
    # fallback name list. Same argument as "no validators configured" one branch
    # up — nothing ran, so nothing may render as a pass.
    for p in files:
        digests[p] = _not_checked("no syntax validator selected")
    return digests


def _resolve_partial(path: str, side: str, selected: set[int], force: bool = False) -> int:
    """Resolve only the selected blocks of one file (issue #305).

    A partial resolve always leaves the unselected blocks' markers in place, so
    the file stays conflicted by design — it is NEVER staged. The receipt reports
    "N of M blocks resolved, file still conflicted" and points back at the
    remaining work, honoring the marker hard-gate rather than fighting it.
    """
    blocks_label = ", ".join(str(b) for b in sorted(selected))
    print(f"# git-resolve: {side} block(s) {blocks_label} in {path}")

    if _guarded_paths([path], side, force):
        print(f"  ⊘ {path}: {_REFUSAL}")
        print("\nResolved blocks: 0 | Refused: 1 | Not staged (still conflicted).")
        _print_refusal_help()
        return 1

    # Same guard as the whole-file path, scoped to the blocks actually selected.
    dups = _duplicated_headings(path, selected) if side == "both" and not force else []
    if dups:
        print(f"  ⊘ {path}: {_heading_refusal(dups)}")
        print("\nResolved blocks: 0 | Refused: 1 | Not staged (still conflicted).")
        _print_refusal_help()
        return 1

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
        # Same relay, printed directly rather than collected (#1638).
        print(f"  ✗ {path}: "
              f"{_untrusted.flat(add.stderr.strip() or add.stdout.strip())}")
        return 1
    digest = _validate_paths([path]).get(path)
    print(f"  ✓ {path}: {resolved} of {total} block(s) resolved — all blocks clean, staged")
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

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("ERROR: not inside a git repository.")
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
        return _resolve_partial(targets[0], side, selected, force)

    print(f"# git-resolve: {side} ({len(targets)} file(s))")

    # Per-file, not per-set: a mixed `both:all` still resolves the CHANGELOG and
    # only holds back the files where a union is wrong. Refusing the whole set
    # would just train the override.
    guarded = _guarded_paths(targets, side, force)

    resolved: list[str] = []
    refused: list[tuple[str, str]] = []
    forced_source: list[str] = []
    forced_headings: list[str] = []
    failed: list[tuple[str, str]] = []
    digests: dict[str, Optional[str]] = {}
    for path in targets:
        if path in guarded:
            refused.append((path, _REFUSAL))
            continue
        if side == "both":
            # Deliberately NOT bypassed by merge=union: that attribute answers
            # "union this file", which is a different question from "this union
            # came out sound". Only `force` gets through here.
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
            co = _git(["checkout", f"--{side}", "--", path])
            if co.returncode != 0:
                # A child's stream reaching the `✗ PATH: REASON` row below
                # (#1638). `_untrusted.split_lines` cuts on LF/CR/CRLF alone by
                # design, so a U+2028 survives inside what the render treats as
                # one line and puts chosen text at column 0, where a `[result]`
                # a consumer greps for sorts first. #1622 (`4bcb1b2`) is the
                # shape.
                failed.append((path, _untrusted.flat(
                    co.stderr.strip() or co.stdout.strip())))
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
            failed.append((path, _untrusted.flat(
                add.stderr.strip() or add.stdout.strip())))
            continue
        resolved.append(path)

    # Syntax digest for the whole batch in ONE supertool call (folded per-file).
    if resolved:
        digests = _validate_paths(resolved)

    for path in resolved:
        print(f"  ✓ {path}")
        digest = digests.get(path)
        line = f"      markers: clean | {digest}" if digest else "      markers: clean"
        if path in forced_source:
            line += " | ⚠ source union — verify manually"
        if path in forced_headings:
            line += " | ⚠ duplicated heading(s) — verify section structure"
        print(line)
    for path, reason in refused:
        print(f"  ⊘ {path}: {reason}")
    for path, err in failed:
        print(f"  ✗ {path}: {err}")

    # A forced union is reported as such — the syntax digest above says
    # `validate: ok` about a file whose statements now run twice, or whose
    # unreleased entries now sit under a tagged release, so the tally has to carry
    # the doubt the validator cannot.
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
            print("Next: ./supertool 'git-commit:::MESSAGE' to commit the resolution.")

    return 0 if not failed and not refused else 1


if __name__ == "__main__":
    sys.exit(main())
