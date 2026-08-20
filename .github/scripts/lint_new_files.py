#!/usr/bin/env python3
"""Lint the files a PR touches — never the tree (#1481, #1849).

Two lint gates cover this repository and an error that arrives *already dead*
is invisible to both, because the hole is where they meet.

`.github/workflows/tests.yml` runs no `ruff check` step, deliberately, and says
why in its own words: a tree-wide gate reds an unrelated contributor's PR the
first time the linter ships a new rule in a selected category, and that cost is
paid by whoever happens to push next rather than by whoever caused it. That
trade is sound and this script does not reverse it.

The supertool `ruff` validator covers the other side — but only for errors an
edit *introduces*, measured against the file's own pre-edit baseline. A file
split, a rename, a new module: every line arrives at once, none of it is new
relative to a baseline that does not exist, and each subsequent edit truthfully
reports `ruff : ok (no new errors)`. Measured on PR #1473: three unreachable
imports in `presets/github/_release_gate.py`, 22/22 green, every edit `ok`.

So the gate that should own an already-dead error is neither of those. It is
this one, and its scope is the **diff**, in two halves. A path a PR adds, copies
or renames has no history, so "pre-existing" is not a category it can be in and
it is checked whole. A path a PR *modifies* has a history, so it is checked only
against its own merge-base revision. Either way a new ruff release only reds a
PR that is already touching the file.

Why the second half is a baseline and not touched lines (#1849)
---------------------------------------------------------------

The `ACR` scope alone left a hole neither gate could see: the tree-wide ignore
hides an F401 in a modified file locally, and this gate was not opening modified
files at all. Measured on PR #1843 — one finding reported, in the only file that
PR adds; five found by the same rule set over the whole diff. The other four
were orphaned `import pytest` lines in modified files and would have shipped.
`lint-new : ok` there is not a false negative anybody can see: it renders
identically to a clean diff, which is this repository's own named defect class
landing on its own tooling.

The obvious scope for the second half is touched lines — ruff's line numbers
intersected with `git diff --unified=0` hunk ranges — and it **cannot see the
case that motivated the issue**. Measured rather than reasoned: at `c974d493`
all four of those hunks are pure deletions (`+N,0`), so the touched-line set in
those files is empty and the orphaned import sits at a line the PR never typed.
A touched-lines gate reports 0 of the 4. That is not a corner case either —
deleting a fixture orphans whatever only it used, and a refactor that removes
code is by construction a diff with little or nothing added.

So the comparison is against the file's own merge-base content, through
`_introduced`: same rule set both sides, same configuration both sides, and a
head finding is reported only when its `(code, message)` is not already
accounted for at base.

Whole-file over modified paths was rejected outright, and the number is the
argument: it re-surfaces the 263 findings #797 declined to make anybody pay down
and the leg is permanently red. Re-measured over 200 commits of merged master
(443 changed `.py` files, 231 of them modified), the baseline scope adds ~7s and
reports 11 files carrying findings where whole-file would report 141.

Three states, and the third is the reason this file exists
----------------------------------------------------------

`docs/validators.md` §"Declining instead of guessing". `ok`, a `finding`, and
`declined` — a checker that could not answer. An unresolvable base ref yields an
empty file list, an empty file list is one branch away from "nothing to check",
and "nothing to check" prints exactly like "I checked and it was clean". That
equivalence *is* #1481, so it must not be reproduced by the fix for it.

`skipped` is the fourth word on the report and is not a fourth state: it is the
`ok` arm for an event that has no PR scope at all (a push to master). Named
rather than left silent, for the same reason as everything else here.

Exit codes: 0 ok or skipped, 1 a finding, 2 declined.

Why it re-enables three rules the tree ignores
----------------------------------------------

`pyproject.toml` carries `ignore = ["F401", "F841", "F541"]` — unused imports,
unused locals, f-strings with no placeholder — added by #797 with its reason
recorded: 263 pre-existing occurrences across ~120 files, and the cleanup is a
`--fix` sweep across half the repo that nobody can review line by line.

That reason is a statement about the **existing tree**. A path with no history
has no share of it at all; a path that has one keeps its share, which is why a
modified file is measured against its own base rather than checked whole. Either
way the three come back on for the files this gate checks, and the debt stops
growing without anybody having to pay it down. Without them
the gate is theatre for the case it was written for: measured 2026-08-13, a
plain `ruff check` of `presets/github/_release_gate.py` at `ffe47ed` — the
commit carrying the three dead imports #1481 names — returns `All checks
passed!`, and with them it reports all three.

The cost, measured rather than asserted: 141 of 902 tracked `.py` files carry at
least one of the three today (293 findings). A PR that *renames* one of those
reds until its author deletes the dead line. That is a cost paid by the person
moving the file, which is the test `tests.yml` sets for a lint gate.

Renames are in scope on purpose: `presets/github/_release_gate.py` is `R065` in
`git diff --name-status -M`, not `A`, so a gate scoped to additions alone would
have missed the instance it was filed for.

Usage:

    python3 .github/scripts/lint_new_files.py             # resolve from the event
    python3 .github/scripts/lint_new_files.py --base main # explicit
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

#: The four words a run can end on. Read by `tests/test_lint_new_files_1481.py`
#: and printed verbatim, so the report cannot say one thing and exit another.
STATE_OK = "lint-new    : ok"
STATE_FINDING = "lint-new    : finding"
STATE_DECLINED = "lint-new    : declined"
STATE_SKIPPED = "lint-new    : skipped"

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_DECLINED = 2

#: `A`dded, `C`opied, `R`enamed — the paths with no history, checked whole,
#: because "pre-existing" is not a category a path with no history can be in.
DIFF_FILTER = "ACR"

#: `M`odified — checked too, but never whole (#1849). A modified path has a
#: history and a share of the 263 findings the tree-wide ignore holds back, so
#: what it is checked against is its own base revision: a finding is reported
#: only when this PR introduced it. Whole-file here would re-surface the debt
#: #797 declined to make anybody pay, and the leg would be permanently red.
MODIFIED_FILTER = "M"

#: On for the checked files only. See the module docstring for why the global
#: `ignore` that switches them off does not reach a path with no history.
EXTRA_RULES = ("F401", "F841", "F541")

#: Long enough that a cold ruff on a contended runner cannot trip it, short
#: enough that a hang is still bounded. Named so the decline can quote it: a
#: reader who sees `timeout` cannot tell a hung linter from a busy machine.
TIMEOUT_S = 120


#: One ruff finding in `--output-format concise`: `path:line:col: CODE text`.
#: Non-greedy up to the first `line:col` pair, so a Windows drive letter stays
#: inside the path instead of ending it.
_FINDING_LINE = re.compile(r"^(.+?):[0-9]+:[0-9]+:")


#: The same line, split into the part that moves and the part that does not.
#: `path:line:col:` is position; `CODE [*] message` is identity.
#: No trailing `$` (#1188, and here it would be a correctness bug rather than a
#: style one): `$` matches before a final newline, so a line that somehow still
#: carried one would fail `(.*)$` and fall through to the `line.strip()` key
#: below -- a *different* key on one side of the comparison and not the other,
#: which reports a pre-existing finding as new. Unanchored, `(.*)` stops at the
#: newline and the key is the same either way.
_FINDING_KEY = re.compile(r"^(.+?):[0-9]+:[0-9]+: (.*)")


def _introduced(head_out: str, base_out: str) -> List[str]:
    """The head finding lines this diff introduced, in head's own order.

    Keyed on `CODE [*] message` -- which carries the offending symbol, e.g.
    ``F401 [*] `pytest` imported but unused`` -- and deliberately **not** on
    `path:line:col`. Position is the part that moves: inserting two lines above
    a pre-existing dead import would otherwise make it a new finding, and every
    untouched finding below any addition would be reported. That is the
    "report everything" failure with a plausible mechanism bolted to it, and it
    deletes the tree-wide ignore #797 added while looking like a narrow fix.

    A **multiset**, not a set. Two `F541`s at base and three at head is one new
    finding; a set difference calls it zero, which is the third occurrence of
    the same message going unreported forever once the first one exists.

    An empty `base_out` means "the base revision had no findings", never "the
    base could not be read" -- the caller declines before reaching here rather
    than passing an unknown in as a clean baseline (#1481's own shape).
    """
    remaining = {}  # type: dict
    for line in base_out.strip().splitlines():
        match = _FINDING_KEY.match(line)
        key = match.group(2) if match else line.strip()
        remaining[key] = remaining.get(key, 0) + 1
    new = []  # type: List[str]
    for line in head_out.strip().splitlines():
        if not line.strip():
            continue
        match = _FINDING_KEY.match(line)
        key = match.group(2) if match else line.strip()
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
        else:
            new.append(line)
    return new


def _files_with_findings(out: str) -> Tuple[List[str], List[str]]:
    """`(the distinct paths ruff named, the lines that named none)`.

    The finding arm used to report `len(files)` -- the set the gate *checked*
    -- as the set carrying findings (#1629). Those are the same number only
    when every checked file is dirty, which is the single-file diff that ships
    most often, so the wrong one read correctly nearly every time. On a 46-file
    diff with one dirty path it printed `46 file(s) ... carry lint findings`
    above a single listed line, and that sentence became a brief telling an
    agent to go looking for findings that did not exist.

    Returns a pair rather than an int because a line this cannot attribute is
    not zero files, it is an unknown -- and rounding it down would put a
    smaller-than-true number under a `finding` heading, which is the same
    defect one layer in.
    """
    seen = []  # type: List[str]
    unattributed = []  # type: List[str]
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        match = _FINDING_LINE.match(line)
        if not match:
            unattributed.append(line)
        elif match.group(1) not in seen:
            seen.append(match.group(1))
    return seen, unattributed


def _run(argv: Sequence[str], cwd: str,
         stdin: "Optional[str]" = None) -> Tuple[Optional[int], str, str]:
    """`(returncode, stdout, stderr)`, or `(None, "", reason)` if it never ran.

    The `None` is the whole point. A spawn that failed — no binary, no
    permission, a Windows `FileNotFoundError [WinError 2]` — must not be
    reachable from any arm that goes on to print a verdict.

    `stdin` feeds the baseline lint of a modified file (#1849): the base
    revision of a path is a blob, not a file on disk, and writing it to a
    temporary one would resolve ruff's configuration from the temporary
    directory rather than from the repository. Piping it under
    `--stdin-filename` keeps both sides of the comparison on one configuration,
    which is the only thing that makes the difference between them mean
    anything.
    """
    try:
        r = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True,
                           input=stdin, timeout=TIMEOUT_S,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        return None, "", "could not start {0!r}: {1}".format(argv[0], exc)
    except OSError as exc:
        return None, "", "could not run {0!r}: {1}".format(argv[0], exc)
    except subprocess.TimeoutExpired:
        return None, "", ("{0!r} did not return within {1}s; nothing was "
                          "checked".format(argv[0], TIMEOUT_S))
    return r.returncode, r.stdout or "", r.stderr or ""


def resolve_base(env) -> Optional[str]:
    """The ref this PR is proposed against, or `None` for a non-PR event.

    `GITHUB_BASE_REF` is a bare branch name and is set only on `pull_request`.
    It is resolved through `origin/` because `actions/checkout` leaves the base
    branch as a remote-tracking ref and never as a local one.
    """
    base = (env.get("GITHUB_BASE_REF") or "").strip()
    return "origin/" + base if base else None


def _changed_paths(out: str) -> List[str]:
    """Destination paths out of `git diff --name-status -M`.

    A rename row is `R065<TAB>old<TAB>new` and the *new* path is the one with no
    history; every other row is `X<TAB>path`. Taking field 1 unconditionally —
    the obvious reading — would hand ruff the path the file used to have, which
    on a rename no longer exists and on which ruff reports `E902`. That failure
    would at least be loud; the quiet version is a rename inside a directory
    that still holds a same-named file.
    """
    paths: List[str] = []
    for line in out.splitlines():
        fields = line.split(chr(9))
        if len(fields) < 2:
            continue
        paths.append(fields[-1])
    return paths


def _python(paths: Iterable[str]) -> List[str]:
    return sorted(p for p in paths if p.endswith(".py"))


def _report(state: str, *lines: str) -> Tuple[str, str]:
    return state, chr(10).join((state,) + lines)


def _scope_lines(added: Sequence[str], modified: Sequence[str]) -> List[str]:
    """What this run looked at, and what each half was measured against.

    The receipt used to read `1 of 1 file(s) this PR adds, copies or renames`.
    That sentence is accurate and it was read, by two different people in one
    session, as `1 of 1 file(s) in this PR` -- so the leg reported a whole-diff
    verdict it had never formed, in the one repository whose named defect class
    is an absence produced by a checker being read as an absence in the world.

    Two scopes now, each stated in full on every run, pass or fail. A reader
    who has to infer which files a number covers will infer the wider one.
    """
    lines = []  # type: List[str]
    if added:
        lines.append("  scope     {0} .py file(s) this PR adds, copies or "
                     "renames -- checked whole, because a path with no history "
                     "cannot carry a pre-existing finding:".format(len(added)))
        lines += ["    " + f for f in added]
    else:
        lines.append("  scope     no .py file this PR adds, copies or renames.")
    if modified:
        lines.append("  scope     {0} .py file(s) this PR modifies -- checked "
                     "only for findings this PR introduces, measured against "
                     "the merge base; one already present there is not "
                     "reported (#1849):".format(len(modified)))
        lines += ["    " + f for f in modified]
    else:
        lines.append("  scope     no .py file this PR modifies.")
    return lines


def _lint_argv(binary: str, tail: Sequence[str]) -> List[str]:
    # No `--force-exclude`, deliberately, and this is not the copy-paste of
    # `validators/ruff/ruff.py` it looks like it should be. That flag makes ruff
    # apply `[tool.ruff] exclude` to paths handed to it EXPLICITLY, rather than
    # only to its own directory walk. This file list is not a walk — it is built
    # from the diff — so with the flag on, a new file under any exclude pattern
    # is dropped from the invocation, ruff still exits 0, and the report below
    # lists it under "all clean" having never opened it. That is #1481's own
    # failure mode reproduced inside the gate written to close it, and it routes
    # around the `declined` state built for exactly this doubt. Inert on today's
    # config, which sets no `exclude` at all — which is the reason it would have
    # shipped and stayed invisible until somebody added one for an unrelated
    # reason. Without the flag an explicitly-named path is always checked, so
    # the file count this run reports is a count it earned.
    # `--quiet` is load-bearing beyond terseness: without it ruff appends a
    # `Found N error(s).` summary, which names no path, and the finding arm
    # below would then decline to state its count on every run that has a
    # finding at all. `test_the_finding_count_is_the_files_carrying_findings_
    # not_the_files_checked` runs real ruff and asserts the headline verbatim,
    # so dropping the flag reds that test rather than degrading in silence.
    return [binary, "check", "--no-cache", "--quiet",
            "--output-format", "concise",
            "--extend-select", ",".join(EXTRA_RULES)] + list(tail)


def _lint(binary: str, cwd: str, paths: Sequence[str]):
    # `--` because these paths were picked up out of `git diff --name-status`,
    # never passed as arguments by anybody, and a path that begins with `-`
    # reaches ruff's option parser instead of its file list. `_python` filtering
    # to `.py` is not a guard: `--stdin-filename=x.py` ends in `.py`, is
    # consumed as an option, leaves ruff with no positional path at all, and
    # ruff then exits 0 with an empty stdout -- which arrives at the `all clean`
    # arm listing a file nothing opened. Measured with ruff 0.16.1.
    return _run(_lint_argv(binary, ["--"] + list(paths)), cwd)


def _lint_content(binary: str, cwd: str, path: str, content: str):
    """Lint one blob as if it were `path`, so config resolves the same way.

    `--stdin-filename=PATH`, the `=` form, and that is measured rather than
    stylistic: with the space form, a path beginning with `-` is eaten by
    ruff's option parser, which answers `a value is required for
    '--stdin-filename'` and exits 2 (ruff 0.16.1). The separate `--` before the
    `-` covers the positional the same way `_lint` does.
    """
    return _run(_lint_argv(binary, ["--stdin-filename=" + path, "--", "-"]),
                cwd, stdin=content)


def _ruff_problem(rc: "Optional[int]", out: str, err: str,
                  paths: Sequence[str]) -> "Optional[List[str]]":
    """The decline lines for a ruff run that did not produce a verdict, or None.

    `rc in (0, 1)` is ruff answering. Anything else -- a spawn that never
    started, a timeout, an exit on its own configuration -- is ruff failing,
    and a failure that reaches the reporting arms would print an absence of
    findings from a checker that never looked.
    """
    if rc is None:
        return ["  " + err,
                "  The {0} file(s) below were NOT checked:".format(len(paths))
                ] + ["    " + f for f in paths]
    if rc not in (0, 1):
        return ["  ruff exited {0}, which is ruff failing rather than a "
                "finding about the files:".format(rc),
                "  " + ((err or out).strip().replace(chr(10), chr(10) + "  ")
                        or "no output"),
                "  The {0} file(s) below were NOT checked:".format(len(paths))
                ] + ["    " + f for f in paths]
    return None


def run(base: str, head: str = "HEAD", cwd: str = ".",
        ruff: "Optional[str]" = "", git: str = "git") -> Tuple[int, str]:
    """One gate run. `(exit_code, report)`.

    `ruff=""` means "find it on PATH"; `ruff=None` means "there is none", which
    is a state a caller has to be able to construct in order to test that it
    declines rather than passing.
    """
    rc, out, err = _run([git, "rev-parse", "--verify", "--quiet",
                         base + "^{commit}"], cwd)
    if rc is None:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  could not run git, so no file list was built: " + err,
            "  Nothing was checked. This is not a clean result.")[1]
    if rc != 0:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  base ref {0!r} does not resolve in this checkout.".format(base),
            "  An unresolvable base yields an empty file list, and an empty "
            "file list reads exactly like a clean one (#1481).",
            "  fix       give the job `fetch-depth: 0`, or fetch the base "
            "branch before this step.")[1]

    rc, out, err = _run([git, "diff", "--name-status", "-M",
                         "--diff-filter=" + DIFF_FILTER, base + "..." + head],
                        cwd)
    if rc is None or rc != 0:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  could not read the diff against {0!r}: {1}".format(
                base, (err or out).strip() or "unknown error"),
            "  Nothing was checked. This is not a clean result.")[1]

    added = _python(_changed_paths(out))

    # The second scope, and the whole of #1849. A modified path is checked too
    # -- against its own base revision, never whole. See `_scope_lines`.
    rc, out, err = _run([git, "diff", "--name-status", "-M",
                         "--diff-filter=" + MODIFIED_FILTER,
                         base + "..." + head], cwd)
    if rc is None or rc != 0:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  could not read the modified-file diff against {0!r}: {1}".format(
                base, (err or out).strip() or "unknown error"),
            "  Nothing was checked. This is not a clean result.")[1]
    modified = _python(_changed_paths(out))

    scope = _scope_lines(added, modified)
    files = added + modified
    if not files:
        return EXIT_OK, _report(
            STATE_OK,
            "  0 file(s) checked -- this diff contains no .py file at all.",
            *scope)[1]

    binary = shutil.which("ruff") if ruff == "" else ruff
    if not binary:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  ruff is not on PATH, so the {0} file(s) below were NOT "
            "checked:".format(len(files)),
            *["    " + f for f in files]
            + ["  This job installs ruff itself via the `dev` extra, so absent "
               "here means the job is broken rather than that a contributor "
               "lacks a tool. `skipped`, never `ok` -- same rule as "
               "validators/ruff/ruff.py."])[1]

    # The flags this gate does and does not pass, and why each one is
    # load-bearing, are on `_lint_argv` and `_lint` — where the argv is now
    # built, so the reasoning cannot drift from the call it describes.
    rules = ("  {0} are re-enabled here and ignored tree-wide: the ignore is "
             "about 263 pre-existing findings in files that have a history "
             "(#1481, #797). An added path has no share of that debt, so it is "
             "checked whole; a modified path has one, so it is checked only "
             "against its own base revision (#1849).".format(
                 ", ".join(EXTRA_RULES)))
    findings = []  # type: List[str]

    if added:
        rc, out, err = _lint(binary, cwd, added)
        problem = _ruff_problem(rc, out, err, added)
        if problem is not None:
            return EXIT_DECLINED, _report(STATE_DECLINED, *problem)[1]
        findings += [ln for ln in out.strip().splitlines() if ln.strip()]

    if modified:
        # Eagerly, and before any finding is known. A merge base that does not
        # resolve means no modified path can be measured at all -- reporting
        # that only when a finding happens to exist is a gate that goes quiet
        # exactly when it matters, which is #1481 rebuilt inside the fix for
        # #1849.
        rc, out, err = _run([git, "merge-base", base, head], cwd)
        merge_base = out.strip()
        if rc is None or rc != 0 or not merge_base:
            return EXIT_DECLINED, _report(
                STATE_DECLINED,
                "  no merge base between {0!r} and {1!r}: {2}".format(
                    base, head, (err or out).strip() or "git named no commit"),
                "  Without one there is no baseline, and without a baseline a "
                "finding in a modified file cannot be told from one somebody "
                "else left there.",
                "  The {0} modified file(s) below were NOT checked:".format(
                    len(modified)),
                *["    " + f for f in modified]
                + ["  This is not a clean result."])[1]

        rc, out, err = _lint(binary, cwd, modified)
        problem = _ruff_problem(rc, out, err, modified)
        if problem is not None:
            return EXIT_DECLINED, _report(STATE_DECLINED, *problem)[1]

        by_file = {}  # type: dict
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            match = _FINDING_KEY.match(line)
            # A line naming no path cannot be compared against a baseline, so
            # it is carried through rather than dropped. Loud and unattributed
            # beats silent: `_files_with_findings` then declines to state the
            # count instead of rounding it down (#1629).
            path = match.group(1) if match else ""
            by_file.setdefault(path, []).append(line)

        findings += by_file.pop("", [])
        for path in [p for p in modified if p in by_file]:
            rc, base_src, err = _run([git, "show", merge_base + ":" + path], cwd)
            if rc is None or rc != 0:
                return EXIT_DECLINED, _report(
                    STATE_DECLINED,
                    "  could not read {0!r} at the merge base {1}: {2}".format(
                        path, merge_base[:12],
                        (err or base_src).strip() or "unknown error"),
                    "  Its head findings are therefore neither new nor "
                    "pre-existing, and guessing either way is a verdict this "
                    "run did not earn.",
                    "  The file below was NOT checked:",
                    "    " + path,
                    "  This is not a clean result.")[1]
            rc, base_out, err = _lint_content(binary, cwd, path, base_src)
            problem = _ruff_problem(rc, base_out, err, [path])
            if problem is not None:
                return EXIT_DECLINED, _report(
                    STATE_DECLINED,
                    "  the base revision of {0!r} could not be linted, so its "
                    "findings cannot be told from pre-existing ones:".format(
                        path),
                    *problem
                    + ["  This is not a clean result."])[1]
            findings += _introduced(chr(10).join(by_file[path]), base_out)

    if findings:
        # Both numbers, always, and in that order: `N of M` is the only wording
        # that cannot be read as either of the two questions it is not
        # answering -- how many files were checked, and how many findings there
        # are. A bare count was read as the wrong one of the three (#1629).
        carrying, unattributed = _files_with_findings(chr(10).join(findings))
        if unattributed:
            headline = ("  {0} file(s) checked; {1} finding line(s) name no "
                        "file, so the number of files carrying findings is "
                        "not reported:".format(len(files), len(unattributed)))
        else:
            headline = ("  {0} of {1} .py file(s) in this PR's diff carry lint "
                        "findings:".format(len(carrying), len(files)))
        return EXIT_FINDING, _report(
            STATE_FINDING,
            headline,
            *["    " + line for line in findings] + scope + [rules])[1]
    return EXIT_OK, _report(
        STATE_OK,
        "  {0} .py file(s) checked in this PR's diff, all clean:".format(
            len(files)),
        *scope + [rules])[1]


def main(argv: "Optional[Sequence[str]]" = None, env=None,
         cwd: str = ".") -> Tuple[int, str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None,
                        help="ref to diff against (default: the PR's base)")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(list(argv) if argv is not None else None)

    env = os.environ if env is None else env
    base = args.base or resolve_base(env)
    if not base:
        return EXIT_OK, _report(
            STATE_SKIPPED,
            "  not a pull_request event and no --base given, so there is no "
            "diff scope to check.",
            "  This gate is scoped to the files a PR adds; a push has no such "
            "scope and inventing one would report on a population nobody "
            "chose. Named rather than left silent (#1481).")[1]
    return run(base=base, head=args.head, cwd=cwd)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    _code, _report_text = main()
    print(_report_text)
    sys.exit(_code)
