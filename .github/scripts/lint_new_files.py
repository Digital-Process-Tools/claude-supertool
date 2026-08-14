#!/usr/bin/env python3
"""Lint the files a PR adds, copies or renames — never the tree (#1481).

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
this one, and its scope is the **diff**: a path a PR adds, copies or renames has
no history, so "pre-existing" is not a category it can be in. A new ruff release
then only reds a PR that is already touching the file.

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
has no share of it. So the three come back on for the files this gate checks,
and the debt stops growing without anybody having to pay it down. Without them
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

#: `A`dded, `C`opied, `R`enamed. Not `M`: a modified file has a history, and
#: reporting a finding somebody else left in it is the tree-wide gate this
#: script exists instead of.
DIFF_FILTER = "ACR"

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


def _run(argv: Sequence[str], cwd: str) -> Tuple[Optional[int], str, str]:
    """`(returncode, stdout, stderr)`, or `(None, "", reason)` if it never ran.

    The `None` is the whole point. A spawn that failed — no binary, no
    permission, a Windows `FileNotFoundError [WinError 2]` — must not be
    reachable from any arm that goes on to print a verdict.
    """
    try:
        r = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
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

    files = _python(_changed_paths(out))
    if not files:
        return EXIT_OK, _report(
            STATE_OK,
            "  no added, copied or renamed .py file in this diff — 0 files "
            "checked.",
            "  Files this PR only *modifies* are out of scope on purpose: they "
            "have a history, and a finding somebody else left in one is the "
            "tree-wide gate tests.yml declines to be.")[1]

    binary = shutil.which("ruff") if ruff == "" else ruff
    if not binary:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  ruff is not on PATH, so the {0} file(s) below were NOT "
            "checked:".format(len(files)),
            *["    " + f for f in files]
            + ["  This job installs ruff itself via the `dev` extra, so absent "
               "here means the job is broken rather than that a contributor "
               "lacks a tool. `skipped`, never `ok` — same rule as "
               "validators/ruff/ruff.py."])[1]

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
    # `--` for the same reason, one layer down. These paths were picked up out
    # of `git diff --name-status`, never passed as arguments by anybody, and a
    # path that begins with `-` reaches ruff's option parser instead of its file
    # list. `_python` filtering to `.py` is not a guard: `--stdin-filename=x.py`
    # ends in `.py`, is consumed as an option, leaves ruff with no positional
    # path at all, and ruff then exits 0 with an empty stdout -- which arrives
    # at the `all clean` arm below listing a file nothing opened. Measured with
    # ruff 0.16.1; the separator is what makes the count a count it earned.
    argv = [binary, "check", "--no-cache", "--quiet",
            "--output-format", "concise",
            "--extend-select", ",".join(EXTRA_RULES), "--"] + files
    rc, out, err = _run(argv, cwd)
    listing = ["    " + f for f in files]
    rules = ("  {0} are re-enabled here and ignored tree-wide: the ignore is "
             "about 263 pre-existing findings in files that have a history, "
             "and these paths do not (#1481, #797).".format(
                 ", ".join(EXTRA_RULES)))
    if rc is None:
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  " + err,
            "  The {0} file(s) below were NOT checked:".format(len(files)),
            *listing)[1]
    if rc not in (0, 1):
        return EXIT_DECLINED, _report(
            STATE_DECLINED,
            "  ruff exited {0}, which is ruff failing rather than a finding "
            "about the files:".format(rc),
            "  " + ((err or out).strip().replace(chr(10), chr(10) + "  ")
                    or "no output"),
            "  The {0} file(s) below were NOT checked:".format(len(files)),
            *listing)[1]
    if rc == 1 or out.strip():
        # Both numbers, always, and in that order: `N of M` is the only wording
        # that cannot be read as either of the two questions it is not
        # answering -- how many files were checked, and how many findings there
        # are. A bare count was read as the wrong one of the three (#1629).
        carrying, unattributed = _files_with_findings(out)
        if unattributed:
            headline = ("  {0} file(s) checked; {1} finding line(s) name no "
                        "file, so the number of files carrying findings is "
                        "not reported:".format(len(files), len(unattributed)))
        else:
            headline = ("  {0} of {1} file(s) this PR adds, copies or renames "
                        "carry lint findings:".format(len(carrying),
                                                      len(files)))
        return EXIT_FINDING, _report(
            STATE_FINDING,
            headline,
            *["    " + line for line in out.strip().splitlines()]
            + [rules])[1]
    return EXIT_OK, _report(
        STATE_OK,
        "  {0} file(s) added, copied or renamed by this PR, all clean:".format(
            len(files)),
        *listing + [rules])[1]


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
