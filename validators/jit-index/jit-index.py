#!/usr/bin/env python3
r"""jit-index validator adapter — a jit-context rule that can never fire (#1254).

A `.claude/jit-context/**/00-index.tsv` row is written by an ordinary `paste`
or `edit`, so nothing in the write path ever looked at its `match` column. That
column is compiled by **awk**, not PCRE:

    pre-tool-hook.sh:80   if (match(tolower(full_command), substr(r_match, 2)) == 0) continue
    pre-path-hook.sh:105  if (match(all_paths[pi], pattern)) { path_matched = 1; break }

macOS ships the one-true-awk, whose regex lexer drops an escape it does not
define. `gh\s+pr` compiles to `ghs+pr` and matches nothing. On 2026-08-10 two
`block` rules were dead exactly this way, one of which had never fired since the
day it was written. (Counting the denominator is a moving target and not the
point: the file held five rules that morning and holds seven now.) A rule that never
matches and a rule that never runs render identically — in the index, in a
directory listing, and in the hook's own log, which shows `(none) [shown:0]`
for both.

**Two checks, answering different questions.**

*Structural, and deliberately awk-independent.* awk does **not** fail to
compile `\s`; it compiles it, silently, into the wrong thing and exits 0.
Handing each pattern to the local awk and reading the exit status therefore
returns a clean verdict on the very row that produced this issue. So the
load-bearing check is a property of the pattern text: an escaped ASCII letter
or digit that awk does not define is dropped, and the pattern matches the bare
character. That rule catches `\s`, `\d`, `\w` and equally the next construct
nobody has thought of yet, and it gives the same answer on every platform —
which is the point. A pattern has to survive the most conservative POSIX awk,
because compiling under gawk is not a licence to write `\s` for a hook that
fires on somebody's Mac.

*Compile, against the awk that is actually installed.* This answers the other
failure: a genuinely malformed pattern (`gh[a`) is a **fatal** awk error, and
both hooks are invoked as `bash "$S" || true`, so one unbalanced bracket does
not disable one row — it silences every rule in the file, quietly. Only a real
engine can tell you that, so this half runs when awk is present and is declined
when it is not.

Three states, per `docs/validators.md` §"Declining instead of guessing":

- `ok` — every regex column in the index survives both checks
- a finding — a pattern that will silently never match, or that aborts the hook.
  A finding-carrying result may also hold one `code: "adapter"` row naming the
  patterns awk never reached, so a partial run is not published as a complete
  one (#1714). See the caveat on that arm in `main`: `count` is what
  `_validator_regressed` subtracts, and this adapter has `rollback_on_fail`.
- `skipped` — the file is not a jit-context index, or awk is absent and the
  structural half found nothing. **A structural pass alone is not a clean
  result**, because half the check did not run; reporting `ok` there would be
  this issue's own defect wearing the validator's clothes. A structural
  *finding* is still published without awk — declining to answer must never
  suppress an answer already in hand.

Nothing here is keyed on a hardcoded path: scope is the `match` glob in the
project's `.supertool.json`, and each row is classified by its own shape.

Usage:  jit-index.py <file>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main, skipped  # noqa: E402
from linebreaks import split_lines  # noqa: E402

TOOL = "jit-index"

AWK_ABSENT = ("awk is not on PATH, so no pattern in this index could be "
              "compiled against the engine the hooks actually use")

#: Escapes awk defines that a rule author plausibly means, and that survive.
INTENDED = set("ntr")

#: Escapes awk defines whose PCRE meaning is a different thing entirely. The
#: pattern compiles, so nothing complains, and then it matches a control
#: character no command line contains.
CONTROL = {
    "b": ("use (^|[^[:alnum:]_]) for a word boundary",
          "awk defines it as a backspace (0x08), so it is not dropped — it is "
          "simply not PCRE's word boundary"),
    "v": ("use [[:space:]]",
          "awk defines it as a vertical tab (0x0b), not PCRE's vertical "
          "whitespace"),
    "a": ("remove it", "awk defines it as a bell (0x07)"),
    "f": ("remove it", "awk defines it as a form feed (0x0c)"),
}

#: The PCRE classes the hooks' own rules reached for. A construct outside this
#: map is still refused — by the structural rule, not by being listed. The map
#: only decides whether the finding can name the replacement.
POSIX_EQUIVALENT = {
    "s": "[[:space:]]", "S": "[^[:space:]]",
    "d": "[[:digit:]]", "D": "[^[:digit:]]",
    "w": "[[:alnum:]_]", "W": "[^[:alnum:]_]",
}

BS = "\\"
TAB = "\t"

#: Budget for one awk spawn. awk compiling a handful of short regexes is
#: microseconds; anything near this is a machine in trouble, not a slow check.
TIMEOUT_S = 10


def emit(payload):
    print(json.dumps(payload))


def _ms(start):
    return int((time.time() - start) * 1000)


def _err(line, msg, code):
    return {"line": line, "col": None, "severity": "error", "code": code, "msg": msg}


def _rows(text):
    """Classify each row by its own shape, not by the directory it sits in.

    A tools row is `tool<TAB>match<TAB>file<TAB>mode<TAB>require<TAB>forbid`,
    and only its second column is a regex, and only when it opens with `~`.
    A paths row is `pattern<TAB>file`, and its first column is *always* handed
    to `match()` — calling it a "prefix" is what made that easy to miss.

    Returns (patterns, shape_errors, parsed_row_count, tabbed_row_count); each
    pattern is (line, text, family).

    `tabbed_row_count` is what separates "this file is not an index" from "this
    index has a broken row". Both leave `parsed` at zero, and collapsing them
    would drop a real finding into a skip.
    """
    patterns = []
    shape_errors = []
    parsed = 0
    tabbed = 0
    for line, raw in enumerate(split_lines(text), 1):
        if not raw.strip():
            continue
        fields = raw.split(TAB)
        if len(fields) > 1:
            tabbed += 1
        if len(fields) >= 4:
            parsed += 1
            match = fields[1]
            if match.startswith("~"):
                patterns.append((line, match[1:], "tools"))
        elif len(fields) == 2 and fields[0].strip() and fields[1].strip():
            parsed += 1
            patterns.append((line, fields[0], "paths"))
        elif len(fields) == 2:
            shape_errors.append(_err(
                line,
                "row has 2 tab-separated fields but one of them is empty: a "
                "paths row is `pattern<TAB>file` and the hook needs both — an "
                "empty pattern is handed to match() and matches everything",
                "shape"))
        else:
            shape_errors.append(_err(
                line,
                "row has {0} tab-separated field(s): a tools row has 6 and a "
                "paths row has 2, so the hook will read this row's columns as "
                "something other than what is written here".format(len(fields)),
                "shape"))
    return patterns, shape_errors, parsed, tabbed


def _escapes(pattern):
    """Every backslash escape as (offset, char); a trailing backslash gives None."""
    found = []
    i = 0
    while i < len(pattern):
        if pattern[i] == BS:
            if i + 1 >= len(pattern):
                found.append((i, None))
                break
            found.append((i, pattern[i + 1]))
            i += 2
            continue
        i += 1
    return found


def _escape_findings(line, pattern):
    out = []
    for offset, ch in _escapes(pattern):
        if ch is None:
            out.append(_err(line, "pattern ends in a lone backslash, which awk "
                                  "will not compile as written", "escape"))
            continue
        if not (ch.isascii() and ch.isalnum()):
            continue          # escaped punctuation: a real escape, and meant
        if ch in INTENDED:
            continue
        if ch in CONTROL:
            fix, why = CONTROL[ch]
            out.append(_err(
                line,
                "{0}{1} (offset {2}): {3}. {4}, so this pattern can never match "
                "a command line.".format(BS, ch, offset, fix, why),
                "escape"))
            continue
        if ch.isdigit():
            out.append(_err(
                line,
                "{0}{1} (offset {2}): an ERE has no backreferences — repeat the "
                "group instead. awk reads this as an octal escape, so it "
                "matches a control character.".format(BS, ch, offset),
                "escape"))
            continue
        hint = POSIX_EQUIVALENT.get(ch)
        out.append(_err(
            line,
            "{0}{1} (offset {2}): {3}. awk does not define this escape, so the "
            "backslash is dropped before the pattern is compiled and it matches "
            "a bare '{1}'.".format(
                BS, ch, offset,
                "use {0}".format(hint) if hint
                else "write it as a POSIX class, e.g. [[:space:]] or [[:digit:]]"),
            "escape"))
    return out


def _uppercase_outside_brackets(pattern):
    hits = []
    i = 0
    bracket = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == BS:
            i += 2
            continue
        if not bracket and ch == "[":
            bracket = True
        elif bracket and ch == "]":
            bracket = False
        elif not bracket and "A" <= ch <= "Z":
            hits.append(ch)
        i += 1
    return hits


def _case_findings(line, pattern):
    """Tools family only: pre-tool-hook.sh:80 lowercases the subject.

    A bracket expression is left alone — `[A-Za-z]` still matches through its
    lower half, so refusing it would be a false alarm at write time.
    """
    hits = _uppercase_outside_brackets(pattern)
    if not hits:
        return []
    return [_err(
        line,
        "uppercase {0}: write the pattern in lowercase. The matcher lowercases "
        "the command before matching (pre-tool-hook.sh:80), so an uppercase "
        "literal can never match.".format(
            ", ".join("'{0}'".format(c) for c in sorted(set(hits)))),
        "case")]


def _awk_version(awk):
    try:
        proc = subprocess.run([awk, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return "awk"          # TimeoutExpired included: it subclasses SubprocessError
    blob = (proc.stdout or proc.stderr or "").strip().splitlines()
    return blob[0].strip() if blob else "awk"


#: One awk process for a whole index, so a seven-rule file costs one spawn.
#: The hook reads its pattern with `getline`, so the string reaches `match()`
#: with its backslashes intact and the *regex* compiler — not awk's string
#: lexer — is what processes them. Feeding patterns on stdin reproduces that
#: exactly, and keeps them out of the program text, where they could not be
#: quoted safely. One pattern per line is sound because a TSV row cannot carry
#: a raw newline: it would be two rows.
AWK_PROGRAM = 'BEGIN { while ((getline p) > 0) { if (match("", p)) x = 1 } }'


def _awk_run(awk, patterns):
    """(returncode, stderr) — or (None, why) when awk could not be run at all."""
    try:
        proc = subprocess.run(
            [awk, AWK_PROGRAM],
            input="".join(p + "\n" for p in patterns),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "awk did not answer within {0}s".format(TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "awk could not be run: {0}".format(exc)
    return proc.returncode, (proc.stderr or "").strip()


def _compile_findings(awk, patterns):
    """(errors, could_not_run_reason, lines_never_compiled).

    The whole set goes through one process first: if every pattern compiles,
    awk exits 0 and there is nothing more to do. Only a non-zero exit buys a
    second pass, one process per pattern — awk aborts at the *first* bad regex,
    so the batch run knows that something is wrong and cannot say which row.

    The third element is the half that was missing (#1714). A stall is not a
    property of the run as a whole: the batch call losing awk means *nothing*
    was compiled, while the loop losing it at index `i` means everything from
    `i` on was not. Returning the reason alone let the caller say "something
    stalled" and never "and these are the rows nobody looked at".
    """
    code, stderr = _awk_run(awk, [p for _line, p, _family in patterns])
    if code is None:
        return [], stderr, [line for line, _p, _f in patterns]
    if code == 0:
        return [], None, []

    version = _awk_version(awk)
    out = []
    for at, (line, pattern, _family) in enumerate(patterns):
        one_code, one_stderr = _awk_run(awk, [pattern])
        if one_code is None:
            return out, one_stderr, [l for l, _p, _f in patterns[at:]]
        if one_code == 0:
            continue
        detail = one_stderr.splitlines()
        out.append(_err(
            line,
            "awk cannot compile this pattern, and a fatal regex aborts the "
            "hook — every rule in this file stops firing, not just this row. "
            "{0} says: {1}".format(
                version,
                detail[0].strip() if detail else "exit {0}".format(one_code)),
            "compile"))
    if not out:
        out.append(_err(
            None,
            "{0} rejected this index but no single pattern reproduced it, so "
            "the offending row cannot be named: {1}".format(
                version, stderr or "no diagnostic"),
            "compile"))
    return out, None, []


def _unrun_error(reason, unchecked, total):
    """The `adapter` row that says which patterns were never compiled (#1714).

    `code: "adapter"` is SCHEMA.md's channel for the adapter talking about
    itself rather than about the file, and it is the right one here even though
    real findings sit beside it: `_supertool.py:_validator_not_checked` asks
    whether **every** error is `adapter` before it declares a file unmeasured,
    so a mixed payload keeps rendering as the finding count it is. That test is
    documented in the core as deliberate, and this is the shape it was written
    for.

    `line: None` because the row is about a set of lines, not one of them, and
    the caller's sort puts it last — after the findings that do have a location.
    """
    where = ", ".join(str(line) for line in unchecked)
    return _err(
        None,
        "{0}, so {1} of {2} pattern{3} in this index {4} never compiled "
        "(line{5} {6}). The findings above are the complete answer for the "
        "other rows and say NOTHING about {7} — this run is not a clean bill "
        "for {7}.".format(
            reason, len(unchecked), total,
            "" if total == 1 else "s",
            "was" if len(unchecked) == 1 else "were",
            "" if len(unchecked) == 1 else "s",
            where,
            "it" if len(unchecked) == 1 else "them"),
        "adapter")


def main():
    start = time.time()
    if len(sys.argv) < 2:
        emit(skipped(TOOL, "", "no file argument", _ms(start)))
        return
    target = sys.argv[1]
    path = Path(target)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
              "errors": [_err(None, "could not read the index: {0}".format(exc),
                              "adapter")],
              "duration_ms": _ms(start)})
        return

    patterns, errors, parsed, tabbed = _rows(text)
    if parsed == 0 and tabbed == 0:
        # Nothing here is even tabular, so this is prose sitting at an index's
        # path rather than a broken index. Declining is the honest answer; a
        # *broken* row falls through to the finding below instead, because a
        # skip there would drop an answer already in hand.
        emit(skipped(TOOL, target,
                     "no row here has the shape of a jit-context index row "
                     "(6 tab-separated fields for tools, 2 for paths)",
                     _ms(start)))
        return

    for line, pattern, family in patterns:
        errors.extend(_escape_findings(line, pattern))
        if family == "tools":
            errors.extend(_case_findings(line, pattern))

    awk = shutil.which("awk")
    unrun = None
    unchecked = []
    if awk and patterns:
        compile_errors, unrun, unchecked = _compile_findings(awk, patterns)
        errors.extend(compile_errors)

    if errors and unrun:
        # The stall has to travel WITH the findings, not instead of them
        # (#1714). Publishing the findings alone made a run that compiled 2 of
        # 20 patterns render as `2 err` — byte-identical to a complete run that
        # found two things, which is this repo's house defect: an absence
        # produced by the tool, read as an absence in the world.
        #
        # Loud here and quiet in the `absent()` arm below is the same cost
        # calculation reaching two answers. This adapter carries
        # `rollback_on_fail`, so the reason that arm must not raise an error is
        # that a stalled machine would otherwise revert a correct edit.
        #
        # **The equivalent claim about THIS arm is not yet true, and the gap is
        # in the core, not here.** `validators/SCHEMA.md` §`adapter` states the
        # guarantee unconditionally — "the result never triggers rollback,
        # whatever `rollback_on_fail` says ... the core never subtracts it from
        # a baseline in either direction". `_supertool.py:_validator_regressed`
        # honours that only when **every** error is `adapter`
        # (`_validator_not_checked`), so in a mixed payload this row is
        # subtracted like a finding. Measured against the core's own function:
        #
        #     before 1 finding, after 1 finding            -> regressed False
        #     before 1 finding, after 1 finding + this row -> regressed True
        #
        # So a pre-existing finding plus a transient stall reverts a correct
        # edit. `cargo-check` already emits mixed payloads (`_parse_errors`,
        # #754) and is exposed the same way, which makes this a core gap this
        # arm joins rather than one it invents — but it is a gap, and the fix
        # is for `_validator_regressed` to subtract only non-`adapter` rows.
        errors.append(_unrun_error(unrun, unchecked, len(patterns)))

    # A finding already in hand is published whatever happened to the other
    # half. Declining to answer must never suppress an answer.
    if errors:
        errors.sort(key=lambda e: (e["line"] is None, e["line"] or 0))
        emit({"tool": TOOL, "file": target, "ok": False, "count": len(errors),
              "errors": errors, "duration_ms": _ms(start),
              "metrics": {"patterns_checked": len(patterns)}})
        return

    if patterns and not awk:
        # An absent tool, so `$SUPERTOOL_REQUIRE_VALIDATORS` can escalate it.
        emit(absent(TOOL, target, AWK_ABSENT, _ms(start)))
        return

    if patterns and unrun:
        # awk is installed and never completed — a hang or a failed exec, not a
        # non-zero exit (that is a *finding* above, and it is loud).
        #
        # Not `tool_fault()`, which is for a tool that exited non-zero with no
        # verdict and stays `ok: false`. Here the structural half — the half
        # that catches the construct this validator exists for — did run and did
        # pass, and this adapter carries `rollback_on_fail`, so a hung awk would
        # revert a correct edit because a machine stalled.
        #
        # It still goes through `absent()` rather than a bare `skipped()`, so
        # `$SUPERTOOL_REQUIRE_VALIDATORS` escalates it exactly as it escalates a
        # missing awk. Both are "this file was NOT fully checked", and #1202's
        # lesson is that the decision belongs in one place, not in each adapter.
        emit(absent(TOOL, target,
                    "the structural check passed but {0}, so these patterns "
                    "were never compiled".format(unrun), _ms(start)))
        return

    emit({"tool": TOOL, "file": target, "ok": True, "count": 0, "errors": [],
          "duration_ms": _ms(start),
          "metrics": {"patterns_checked": len(patterns)}})


if __name__ == "__main__":
    guard_main(TOOL, main)
