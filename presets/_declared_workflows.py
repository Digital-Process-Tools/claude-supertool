#!/usr/bin/env python3
"""The workflows a commit *declares*, as opposed to the runs it produced (#846).

`_declared_legs` is the second source for "how many legs should this run have".
This is the second source one scope out: **how many workflows should this commit
have.** They are different questions with different failure modes, and #804/#837
only closed the first.

Everything in the leg reconciliation is sourced from the runs on the commit. A
workflow that produced *no run at all* therefore contributes nothing to either
side of that arithmetic, cancels out exactly, and leaves a tally that sums
correctly while describing a strictly smaller universe than the reader believes
they are looking at. Live, on the morning of the v0.27.0 tag::

    Verdict: GREEN — every workflow on dcb574e concluded and every leg passed
             (19 legs across 3 workflows).

`slow tests` was the fourth, `schedule`-triggered, declared in
`.github/workflows`, never dispatched on that commit. The sentence was true. A
release was tagged on it.

**Read at the commit, never off the checkout.** `.github/workflows/*` in the
working tree is the set on *some* ref, and `gh-branch` answers about a named
SHA — often one the caller does not have. So the directory and every file in it
are fetched with `?ref=<sha>`, which makes the declared set a property of the
same commit as the runs it is compared against.

**What this module refuses to do.** It does not decide that a workflow "should"
have run. `paths`, `paths-ignore`, `branches`, `branches-ignore`, a job-level
`if:`, a workflow disabled in repository settings and a `fromJSON` matrix
computed at runtime are each a legitimate reason for a declared workflow to
produce no run, and every one of them is either invisible from here or costs
more calls than the answer is worth. Concluding a shortfall from an absence
would trade a silent miss for a false alarm **on a merge gate**, which is the
worse trade and which #846 says in its own words. So this module reports the
*triggers a workflow declares* and lets the caller state scope; it never states
a verdict.

Three states throughout, never two:

* a parsed workflow with a trigger list — established;
* a parsed workflow whose `on:` block could not be read — `triggers is None`,
  which is "I could not tell", not "nothing triggers it";
* the whole directory unreadable, or wider than `MAX_DECLARED_WORKFLOWS` —
  `(None, reason)`, so the caller says the declared set is unestablished rather
  than claiming complete coverage over a list it does not have.
"""
from __future__ import annotations

import base64
import binascii
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time

# Every other presets-local sibling this module could import already carries
# its own path insert before importing one; this module never needed a
# sibling before #1864, and relying on the caller (branch.py) to have gone
# first is how `import _status_probe` above raised `ModuleNotFoundError` for
# any future caller or test that loads this file on its own -- found in
# review, reproduced with a standalone `importlib` load.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _status_probe  # noqa: E402  (does this stderr *state* the target is missing? - #1864)

WORKFLOW_DIR = ".github/workflows"

# Extra `gh api` calls one render will pay for: one for the directory, one per
# workflow file. Bounded for `_declared_legs.MAX_RECONCILED_RUNS`' reason — an
# op answering a status question must not turn into a fan-out — and past it the
# answer is "unestablished", which is honest and bounded rather than slow and
# correct. Twelve covers every repo either of these tools has been pointed at
# with room over.
MAX_DECLARED_WORKFLOWS = 12

# Files GitHub will read as a workflow. `.yaml` as well as `.yml`: leaving it
# out would make one spelling of the same file silently unreachable, which is
# this module's own defect one layer down.
_WORKFLOW_SUFFIXES = (".yml", ".yaml")

# The top-level `on:` key, at column 0 and nowhere else. Quoted spellings are
# real: YAML 1.1 reads a bare `on` as the boolean true, so repos that lint
# their workflows against a 1.1 parser write `"on":`.
_ON_KEY = re.compile(r"""^(?:on|"on"|'on')\s*:\s*(?P<rest>.*?)\s*$""")  # anchored-ok: matched per line of a workflow file

# `name:` at column 0. Absent, GitHub displays the file path as the workflow
# name, and the run list carries that path in `workflowName` — so the fallback
# is not a placeholder, it is what the other side of the comparison will say.
_NAME_KEY = re.compile(r"""^name\s*:\s*(?P<rest>.*?)\s*$""")  # anchored-ok: matched per line of a workflow file

_MAP_KEY = re.compile(r"^(?P<indent>\s+)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:")
_SEQ_ITEM = re.compile(r"^(?P<indent>\s+)-\s*(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*$")  # anchored-ok: matched per line of a workflow file

# Triggers that fire on a push to a branch. Used only to say which absences are
# expected and which are not — never to conclude one. `workflow_run` and
# `workflow_call` are deliberately here: both are reached *by* a push-triggered
# run, so their absence on a commit that had pushes is the same question.
PUSH_TRIGGERS = frozenset({"push", "workflow_run", "workflow_call"})


# A YAML comment starts at a `#` that follows whitespace (or opens the line),
# not at any `#` at all: `name: build#3` is the literal name `build#3`. Getting
# that wrong in either direction misnames a workflow, and a misnamed workflow
# never matches the run that produced it.
_COMMENT = re.compile(r"(?:^|\s)#")


def _strip_comment(text: str) -> str:
    m = _COMMENT.search(text)
    return text[:m.start()] if m else text


def _strip_quotes(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def _scalar(raw: str) -> str:
    """One YAML scalar off the right-hand side of a key, comment removed.

    Quoted first, because the quotes are what make a `#` ordinary text: a
    workflow legitimately called `release # 1` must survive, and a plain
    `tests # the matrix` must not. `parse_triggers` stripped comments and
    `parse_name` did not, so a commented name never matched GitHub's
    `workflowName` and the render gained a NOT-covered line about a workflow
    that had run and passed — the false alarm this module exists to refuse.
    """
    t = raw.strip()
    if t[:1] in ("'", '"'):
        quote = t[0]
        end = t.find(quote, 1)
        return t[1:end] if end > 0 else t[1:]
    return _strip_comment(t).strip()


def _lines(text: str) -> list[str]:
    # `split("\n")`, not `splitlines()`. `splitlines()` also breaks on `\r`,
    # `\x0b`, `\x1c`, ` ` and friends — which would silently truncate a
    # name carrying one instead of handing the whole thing to `_untrusted.flat`
    # where it belongs. Sanitising by accident is how a sanitiser stops being
    # tested.
    return str(text or "").split("\n")


def parse_name(text: str, path: str) -> str:
    """The workflow's `name:`, or `path` when it declares none.

    The fallback is exact rather than approximate: GitHub itself displays the
    file path for an unnamed workflow, and `gh run list --json workflowName`
    returns that same path, so the two sides of the comparison agree.
    """
    for line in _lines(text):
        m = _NAME_KEY.match(line)
        if m:
            value = _scalar(m.group("rest"))
            if value:
                return value
            break
    return path


def parse_triggers(text: str) -> list[str] | None:
    """The event names under the top-level `on:`, or `None` when unreadable.

    `None` and `[]` are different answers and the difference is the point:
    `[]` would assert that the workflow declares no trigger at all, which is
    not a thing a valid workflow can do, so an empty parse is always this
    parser failing rather than the file saying something.

    Four spellings, all live in the wild::

        on: push
        on: [push, pull_request]
        on:
          push:
            branches: [master]
        on:
          - push
    """
    lines = _lines(text)
    for i, line in enumerate(lines):
        m = _ON_KEY.match(line)
        if not m:
            continue
        # Strip a trailing comment before deciding the form — `on: push # ...`
        # is a scalar, not a scalar plus noise.
        rest = _strip_comment(m.group("rest")).strip()
        if rest.startswith("["):
            return _flow_sequence(rest, lines[i + 1:])
        if rest:
            return [_strip_quotes(rest)]
        return _block_keys(lines[i + 1:])
    return None


# How far a `[` is followed looking for its `]`. A flow sequence spanning more
# lines than this is not a workflow trigger list, and an unbounded scan over a
# file that never closes the bracket would read the whole file as one value.
_FLOW_MAX_LINES = 20


def _flow_sequence(first: str, rest: list[str]) -> list[str] | None:
    """A `[a, b]` list, however many lines it is spread over.

    Reading only the first line is not a smaller answer, it is a **wrong** one
    pointing the dangerous way::

        on: [pull_request,
             push]

    truncated to `['pull_request']`, so `is_push_triggered` answered `False` and
    a genuinely undispatched push-triggered workflow collapsed into "no push
    trigger, so no run on this commit is expected" — the loud line #846 exists
    to print, silenced by the parser.

    An unterminated bracket returns `None`, not the part that was read. A
    partial list is an assertion about which triggers a workflow declares; the
    honest output when the bracket never closes is that this parser could not
    tell, which routes the workflow to the loud group rather than the quiet one.
    """
    buf = first
    for line in rest[:_FLOW_MAX_LINES]:
        if "]" in buf:
            break
        buf += " " + _strip_comment(line).strip()
    if "]" not in buf:
        return None
    inner = buf[1:].split("]", 1)[0]
    found = [_strip_quotes(p) for p in inner.split(",")]
    found = [f for f in found if f]
    return found or None


def _block_keys(rest: list[str]) -> list[str] | None:
    """The immediate child keys of a block, mapping or sequence form."""
    found: list[str] = []
    indent: int | None = None
    for line in rest:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            break  # back to column 0 — the `on:` block is over
        m = _MAP_KEY.match(line) or _SEQ_ITEM.match(line)
        if not m:
            continue
        width = len(m.group("indent"))
        if indent is None:
            indent = width
        if width == indent and m.group("key") not in found:
            found.append(m.group("key"))
    return found or None


def _run(argv: list[str], timeout: int = 15):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


# The enrichment's cost ceiling, bounded **by construction** rather than by a
# projection. Two reviewers arrived at this from opposite directions: at 15s per
# call with 4 workers, 12 files is 1 + 3 waves = 60s, which is the whole of
# `gh-branch`'s own 60s allowance and two thirds of the 90s
# `pr_merge._default_branch_report` wrapped the script in.
#
#     1 directory read           10s
#   + ceil(12 / 6) waves x 10s   20s
#   = 30s absolute worst case
#
# against a `gh-branch` budget now raised to 90s and a `pr_merge` one raised to
# 120s. This is an annotation on the verdict and it must never cost more than
# the verdict does.
#
# A projected-cost refusal was tried first and rejected: it computed the *worst*
# case per wave and declined a ten-file repository that would in practice have
# answered in about three seconds, turning a working scope check into a
# permanent UNESTABLISHED. The bound belongs in the fan-out width and the file
# cap, which are known here, not in a guess about latency, which is not.
API_TIMEOUT = 10
BUDGET_SECS = 45
FETCH_WORKERS = 6


def _api(path: str, timeout: int = API_TIMEOUT) -> tuple[object, str]:
    """`(json, error)` for one `gh api` read. Every spawn failure is a reason.

    `FileNotFoundError` is caught explicitly: Windows raises it for a missing
    executable where POSIX shells may not fail at all (#997), and this call is
    an enrichment — one that escapes takes the whole verdict down with it.
    """
    try:
        r = _run(["gh", "api", path], timeout=timeout)
    except FileNotFoundError:
        return None, "gh not found"
    except subprocess.TimeoutExpired:
        return None, "gh timed out"
    except OSError as exc:
        return None, f"gh could not be run ({type(exc).__name__})"
    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        if _status_probe.says_not_found(stderr):
            return None, "404"
        return None, f"gh failed ({stderr[:120] or 'no stderr'})"
    try:
        return json.loads(r.stdout), ""
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"


def _decode(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("encoding") or "") != "base64":
        return None
    try:
        return base64.b64decode(
            str(payload.get("content") or "")).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None


def declared_at(owner: str, repo: str, sha: str,
                workers: int = FETCH_WORKERS,
                budget_secs: int = BUDGET_SECS) -> tuple[list[dict] | None, str]:
    """`(workflows, reason)` — what `.github/workflows` holds at `sha`.

    `workflows` is a list of ``{"name", "path", "triggers"}``; `triggers` is
    `None` for a file whose `on:` block could not be read. `(None, reason)`
    means the *set* is unestablished, and the caller must say so rather than
    treat the runs it saw as complete coverage.

    `([], "")` — an established empty set — is a real and different answer: a
    commit with no `.github/workflows` declares no Actions workflow, so every
    run on it came from somewhere this check does not cover, and there is
    nothing to disclose.
    """
    if not owner or not repo or not sha:
        return None, "the repository or commit could not be identified"

    started = time.monotonic()
    listing, err = _api(
        f"repos/{owner}/{repo}/contents/{WORKFLOW_DIR}?ref={sha}")
    if err == "404":
        return [], ""
    if err:
        return None, f"{WORKFLOW_DIR} could not be read at this commit: {err}"
    if not isinstance(listing, list):
        return None, f"{WORKFLOW_DIR} did not read as a directory"

    paths = [
        str(entry.get("path") or "")
        for entry in listing
        if isinstance(entry, dict)
        and str(entry.get("type") or "file") == "file"
        and str(entry.get("path") or "").endswith(_WORKFLOW_SUFFIXES)
    ]
    paths = [p for p in paths if p]
    if not paths:
        return [], ""
    if len(paths) > MAX_DECLARED_WORKFLOWS:
        return None, (f"{len(paths)} workflow files at this commit, over the "
                      f"{MAX_DECLARED_WORKFLOWS} this op will fetch in one "
                      f"render")

    # A directory read that itself ate the budget means the API is slow enough
    # that the file reads will not fit either. Declining here costs the caller
    # a stated non-answer; not declining risks the caller's own timeout killing
    # the subprocess, which turns a stated non-answer into no answer at all.
    elapsed = time.monotonic() - started
    if elapsed > budget_secs:
        return None, (f"reading {WORKFLOW_DIR} alone took {int(elapsed)}s of "
                      f"this op's {budget_secs}s enrichment budget, so the "
                      f"{len(paths)} workflow files were not fetched")

    def fetch(path: str) -> tuple[str, str | None]:
        payload, error = _api(f"repos/{owner}/{repo}/contents/{path}?ref={sha}")
        return path, (None if error else _decode(payload))

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(paths)))) as pool:
        bodies = dict(pool.map(fetch, paths))

    out: list[dict] = []
    for path in paths:
        text = bodies.get(path)
        if text is None:
            # The file is declared — the directory listing says so — and its
            # contents are not readable. Reporting it with unknown triggers is
            # strictly better than dropping it, which would shrink the declared
            # set by exactly the file this op could not read.
            out.append({"name": path, "path": path, "triggers": None})
            continue
        out.append({
            "name": parse_name(text, path),
            "path": path,
            "triggers": parse_triggers(text),
        })
    return out, ""


def is_push_triggered(triggers: object) -> bool | None:
    """True / False / `None` for "does a push to a branch reach this?"."""
    if triggers is None:
        return None
    return any(str(t) in PUSH_TRIGGERS for t in triggers)
