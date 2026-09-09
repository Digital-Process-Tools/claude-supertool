"""A branch name cannot become a line the tool appears to have written (#965).

`gh-pr:N:status` is the op a merge decision reads for the summed check tally.
It printed the head branch raw:

    print(f"branch: {d.get('headRefName') or '?'} -> {d.get('baseRefName') or '?'}")

Git accepts U+2028 in a refname — it is not an ASCII control character, so
`check-ref-format` passes it — and `str.splitlines()` breaks on it (#886). A
fork PR needs no permission on the target repo, so the head branch is
attacker-chosen text, and a name carrying

    evil<U+2028>checks: 20 total: 20 passed, 0 failed, 0 pending<U+2028>review: APPROVED

renders those two lines *above* the true `0 passed, 1 failed ⚠ NOT ALL GREEN`
and `REVIEW_REQUIRED`. The `:full` path ten lines below was already correct;
this is adoption of `_untrusted.flat`, not new design.

The bar these tests hold, and why each half is needed:

* **the forged text may not be its own rendered line** — the post-condition,
  asserted against `splitlines()`, which is what every consumer of this output
  counts with. Not "flat was called": a site could call it and still print the
  raw value, and a test that watches the call would not notice.
* **the name must still be readable, in full** — deleting or truncating the
  field would pass the first assertion and is the trade this repo refuses
  (`_untrusted`: disclosed, not stripped). A branch name is *displayed* here,
  not executed, so flattening is the right answer at these sites and #924's
  refusal is not: refusing to print the branch of the PR under review withholds
  the fact the reader is deciding on. The refusal stays where #924 put it — on
  the `git-checkout` imperative in `_branch_locale`, which these same call sites
  already reach.
* **and the guarantee is checked at the source, not only at the ops named
  here** — `test_no_preset_prints_a_refname_raw` fails on a sixth call site.
  Scoped to refnames deliberately: the key set is small enough to carry no
  false positives, so it can be an assertion rather than an allowlist that
  grows quietly. Titles, logins and job names at these same sites are fixed too
  and pinned by the render tests above.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent

#: Not an ASCII control character, so `git check-ref-format` accepts it in a
#: refname; one of the ten separators `str.splitlines()` breaks on (#886).
SEP = " "

FORGED_TALLY = "checks: 20 total: 20 passed, 0 failed, 0 pending"
FORGED_REVIEW = "review: APPROVED"
FORGED_TITLE_LINE = "State: MERGED | Author: maintainer"
HOSTILE_BRANCH = f"evil{SEP}{FORGED_TALLY}{SEP}{FORGED_REVIEW}"
HOSTILE_TITLE = f"tidy up{SEP}{FORGED_TITLE_LINE}"

FORGED_LINES = (FORGED_TALLY, FORGED_REVIEW, FORGED_TITLE_LINE)


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_pr = _load("presets/github/pr.py", "github_pr_965")
gh_job = _load("presets/github/job.py", "github_job_965")
gh_run = _load("presets/github/run.py", "github_run_965")
gh_issue = _load("presets/github/issue.py", "github_issue_965")
gl_mr = _load("presets/gitlab/mr.py", "gitlab_mr_965")
gl_job = _load("presets/gitlab/job.py", "gitlab_job_965")
gl_pipeline = _load("presets/gitlab/pipeline.py", "gitlab_pipeline_965")


# ---------------------------------------------------------------------------
# The two assertions every case makes
# ---------------------------------------------------------------------------

def assert_no_forged_line(out: str) -> None:
    """No line of the render may be one the payload wrote."""
    rendered = out.splitlines()
    for forged in FORGED_LINES:
        assert forged not in rendered, (
            f"{forged!r} rendered as its own line:\n"
            + "\n".join(f"  {i:>3} | {line}" for i, line in enumerate(rendered, 1))
        )


def assert_nothing_censored(out: str, *fragments: str) -> None:
    """Every word of the name survives — flattened, never dropped."""
    for fragment in fragments:
        assert fragment in out, f"{fragment!r} was removed from the render"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["stub"], returncode, stdout, stderr)


def _declined(*_a: Any, **_k: Any) -> Any:
    return _completed(returncode=1, stderr="stub: not served")


# ---------------------------------------------------------------------------
# gh-pr:N:status — the op the merge gate reads
# ---------------------------------------------------------------------------

_PR_PAYLOAD = {
    "number": 4242, "title": HOSTILE_TITLE, "state": "OPEN",
    "author": {"login": "stranger"}, "headRefName": HOSTILE_BRANCH,
    "baseRefName": "master", "labels": [], "milestone": None,
    "reviewDecision": "REVIEW_REQUIRED", "reviews": [], "mergeCommit": None,
    "mergeable": "MERGEABLE", "isDraft": False,
    "url": "https://github.com/o/r/pull/4242", "body": "", "comments": [],
    "additions": 1, "deletions": 0, "changedFiles": 1, "assignees": [],
    "createdAt": "2026-08-01T00:00:00Z", "updatedAt": "2026-08-01T00:00:00Z",
    "headRefOid": "d" * 40,
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
         "conclusion": "FAILURE", "detailsUrl": ""},
    ],
}


def _run_gh_pr(monkeypatch: Any, capsys: Any, mode: str) -> str:
    def fake_gh(args: list[str], timeout: int = 10) -> Any:
        if args[:2] == ["pr", "view"]:
            return _completed(json.dumps(_PR_PAYLOAD))
        return _declined()

    monkeypatch.setattr(gh_pr, "_gh", fake_gh)
    monkeypatch.setattr(gh_pr, "_fetch_review_threads_detailed",
                        lambda *a, **k: ([], ""))
    monkeypatch.setattr(sys, "argv", ["pr.py", "4242", mode])
    assert gh_pr.main() == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("mode", ["status", "full"])
def test_gh_pr_branch_cannot_forge_a_tally_line(monkeypatch: Any, capsys: Any,
                                                mode: str) -> None:
    out = _run_gh_pr(monkeypatch, capsys, mode)
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


def test_gh_pr_status_still_reports_the_real_tally(monkeypatch: Any,
                                                   capsys: Any) -> None:
    """The true verdict is the line the forgery was aimed at displacing."""
    out = _run_gh_pr(monkeypatch, capsys, "status")
    assert "0 passed, 1 failed" in out
    assert "review: REVIEW_REQUIRED" in out.splitlines()


# ---------------------------------------------------------------------------
# gl-mr:N:status
# ---------------------------------------------------------------------------

_MR_PAYLOAD = {
    "iid": 77, "title": HOSTILE_TITLE, "state": "opened",
    "merge_status": "can_be_merged", "has_conflicts": False,
    "source_branch": HOSTILE_BRANCH, "target_branch": "master",
    "author": {"username": "stranger"}, "labels": [], "milestone": None,
    "web_url": "https://gitlab.example/x/-/merge_requests/77",
    "merged_at": None, "merge_commit_sha": None, "squash_commit_sha": None,
    "description": "", "pipeline": {"status": "failed", "id": 9},
    "head_pipeline": {"status": "failed", "id": 9},
    "assignees": [], "reviewers": [], "changes_count": "1",
}


def _run_gl_mr(monkeypatch: Any, capsys: Any) -> str:
    def fake_run(args: list[str], **_k: Any) -> Any:
        if args[:3] == ["glab", "mr", "view"]:
            return _completed(json.dumps(_MR_PAYLOAD))
        return _declined()

    monkeypatch.setattr(gl_mr.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mr.py", "77", "status"])
    assert gl_mr.main() == 0
    return capsys.readouterr().out


def test_gl_mr_status_branch_cannot_forge_a_tally_line(monkeypatch: Any,
                                                       capsys: Any) -> None:
    out = _run_gl_mr(monkeypatch, capsys)
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


# ---------------------------------------------------------------------------
# gh-job:N — prints the branch immediately above the #924-hardened check
# ---------------------------------------------------------------------------

def _run_gh_job(monkeypatch: Any, capsys: Any) -> str:
    def fake_run(args: list[str], **_k: Any) -> Any:
        if args[:2] == ["gh", "api"] and args[2].endswith("/logs"):
            return _completed("a log line\nanother\n")
        if args[:2] == ["gh", "api"]:
            return _completed(json.dumps({
                "name": HOSTILE_TITLE, "status": "completed",
                "conclusion": "failure", "run_id": 5, "run_url": "",
            }))
        if args[:3] == ["gh", "run", "view"]:
            return _completed(json.dumps({
                "headBranch": HOSTILE_BRANCH, "event": "pull_request",
                "pullRequests": [{"number": 4242}],
            }))
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(json.dumps({
                "title": HOSTILE_TITLE, "author": {"login": "stranger"},
                "headRefName": HOSTILE_BRANCH, "baseRefName": "master",
                "labels": [],
            }))
        return _declined()

    monkeypatch.setattr(gh_job.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["job.py", "31337"])
    assert gh_job.main() == 0
    return capsys.readouterr().out


def test_gh_job_pr_header_cannot_forge_lines(monkeypatch: Any, capsys: Any) -> None:
    out = _run_gh_job(monkeypatch, capsys)
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


# ---------------------------------------------------------------------------
# gh-run:N
# ---------------------------------------------------------------------------

def _run_gh_run(monkeypatch: Any, capsys: Any) -> str:
    def fake_run(args: list[str], **_k: Any) -> Any:
        if args[:3] == ["gh", "run", "view"]:
            return _completed(json.dumps({
                "databaseId": 5, "name": HOSTILE_TITLE, "status": "completed",
                "conclusion": "failure", "event": "pull_request",
                "headBranch": HOSTILE_BRANCH, "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-01T00:00:00Z", "url": "",
                "jobs": [{"name": HOSTILE_TITLE, "status": "completed",
                          "conclusion": "failure", "databaseId": 9}],
                "attempt": 1,
            }))
        return _declined()

    monkeypatch.setattr(gh_run.subprocess, "run", fake_run)
    monkeypatch.setattr(gh_run, "declared_legs", lambda *a, **k: (None, []))
    monkeypatch.setattr(sys, "argv", ["run.py", "5"])
    assert gh_run.main() == 0
    return capsys.readouterr().out


def test_gh_run_header_cannot_forge_lines(monkeypatch: Any, capsys: Any) -> None:
    out = _run_gh_run(monkeypatch, capsys)
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


# ---------------------------------------------------------------------------
# gh-issue:N — the linked-PR block
# ---------------------------------------------------------------------------

def test_gh_issue_linked_pr_cannot_forge_lines(monkeypatch: Any, capsys: Any) -> None:
    payload = {"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": [
            {"number": 4242, "title": HOSTILE_TITLE, "state": "OPEN",
             "headRefName": HOSTILE_BRANCH},
        ]}}}}}

    monkeypatch.setattr(gh_issue, "_gh",
                        lambda args, timeout=10: _completed(json.dumps(payload)))
    gh_issue._print_linked_prs(965, "https://github.com/o/r/issues/965")
    out = capsys.readouterr().out
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


# ---------------------------------------------------------------------------
# gl-job:N and the gl-pipeline job table
# ---------------------------------------------------------------------------

def test_gl_job_mr_header_cannot_forge_lines(monkeypatch: Any, capsys: Any) -> None:
    def fake_run(args: list[str], **_k: Any) -> Any:
        if args[:2] == ["glab", "api"] and args[2].endswith("/trace"):
            return _completed("a log line\n")
        if args[:2] == ["glab", "api"] and "/jobs/" in args[2]:
            return _completed(json.dumps({
                "name": HOSTILE_TITLE, "status": "failed", "stage": "test",
                "duration": 1.0, "web_url": "", "ref": "refs/merge-requests/77/head",
                "pipeline": {"id": 9},
            }))
        if args[:2] == ["glab", "api"] and "merge_requests" in args[2]:
            return _completed(json.dumps({
                "title": HOSTILE_TITLE, "source_branch": HOSTILE_BRANCH,
                "target_branch": "master", "author": {"username": "stranger"},
                "labels": [], "state": "opened", "description": "",
                "changes_count": "1", "diff_stats": {},
            }))
        return _declined()

    monkeypatch.setattr(gl_job.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["job.py", "31337"])
    assert gl_job.main() == 0
    out = capsys.readouterr().out
    assert_no_forged_line(out)
    assert_nothing_censored(out, "evil", "20 passed", "APPROVED")


def test_gl_pipeline_table_row_stays_one_row(capsys: Any) -> None:
    """A column-aligned table is `_board`'s argument: a cell may not make a row."""
    gl_pipeline._print_table([
        {"name": HOSTILE_TITLE, "stage": "test", "status": "failed",
         "duration": 1.0},
    ])
    out = capsys.readouterr().out
    body = out.splitlines()[2:]  # header + rule
    assert len(body) == 1, f"one job rendered {len(body)} rows:\n{out}"


# ---------------------------------------------------------------------------
# The guarantee at the source: no sixth call site
# ---------------------------------------------------------------------------

#: Fields whose value is a git refname chosen by whoever opened the pull or
#: merge request. Deliberately small: every one of these is a refname and
#: nothing else, so this list needs no exemptions to stay green — which is what
#: separates it from a lint that gets allowlisted into reporting `ok`.
#:
#: `ref` joined in #970, decided on that same test rather than on it sounding
#: like a refname: adding it names exactly the two `gl-runners:queue` sites that
#: issue is about and nothing else in the three scanned trees, so the set still
#: needs no exemption. That is the line between it and the `title`/`name`/
#: `login` widening **rejected** in #968 — `name` alone adds an `mr.py` hit that
#: is a CI job name, which is not a refname, and keeping the scan green past it
#: would take a per-field allowlist. The allowlist is the failure mode this
#: scanner exists to avoid, not a cost of running it wider.
#: `target` joined in #1038, and it is the reason the set alone was never the
#: guard. `_git_common._glab_fields` / `_gh_fields` *normalise* `target_branch`
#: and `baseRefName` into a key named `target`, so every consumer downstream of
#: the normaliser reads a name this list had never heard of. The scan went
#: green on a tainted print and reported that it had — a coverage claim that
#: was not true, which is this repo's own defect class arriving inside the
#: detector built for it. It still needs no exemption: the only `target` reads
#: in the three scanned trees are that normalised refname.
REFNAME_KEYS = frozenset({
    "headRefName", "baseRefName", "headBranch", "head_branch",
    "source_branch", "target_branch", "ref", "target",
})

#: Anything that marks remote text before it is printed.
MARKERS = frozenset({"flat", "fence", "scrub", "render_row", "shell_ref"})

_SCANNED = ("presets/github", "presets/gitlab", "presets/git")


def _call_names(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
    return names


def _is_marker_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else (
        fn.id if isinstance(fn, ast.Name) else None)
    return name in MARKERS


def _iter_unmarked(node: ast.AST):
    """Every sub-node of `node` that is not an ARGUMENT of a marker call.

    #976's "dangerous" shape (H): `MARKERS & _call_names(sub.value)` used to
    be an any-marker-ANYWHERE test over the whole sub-expression, so
    `flat(t) + d.get('headRefName')` cleared the entire expression because
    `flat` appeared somewhere in it — laundering the unflattened `d.get(...)`
    sitting right beside it. This walks the tree carrying one bit of state,
    "am I currently inside a marker call's arguments", and only a node
    reached with that bit False is yielded. A marker call's own arguments
    flip the bit to True for everything under them; nothing else does. `G`
    (two separate marker/non-marker reads in the same f-string, each its own
    sub-expression) still passes: the non-marker read is never nested inside
    the marker call, so it is reached with the bit still False.
    """
    stack = [(node, False)]
    while stack:
        n, safe = stack.pop()
        if _is_marker_call(n):
            if not safe:
                yield n
            for child in ast.iter_child_nodes(n):
                stack.append((child, True))
            continue
        if not safe:
            yield n
        for child in ast.iter_child_nodes(n):
            stack.append((child, safe))


def _refname_key(node: ast.AST) -> "str | None":
    """The REFNAME_KEYS member `node` reads, or None.

    Both read shapes, because the dict does not care which you use.
    `d.get("target")` and `d["target"]` are the same read. The first draft
    matched only `.get`, so `push.py`'s `mr['target']` was invisible to it —
    a second way the same value walked past the same scan (#1038).
    """
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args):
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value if first.value in REFNAME_KEYS else None
        return None
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        return node.slice.value if node.slice.value in REFNAME_KEYS else None
    return None


def _unmarked_refnames(node: ast.AST) -> set[str]:
    """Direct `.get()`/subscript reads of a REFNAME_KEYS member, anywhere in
    `node` that is not inside a marker call's own arguments (#976).

    Deliberately broad about the *shape wrapping* the read — a `BinOp`, a
    `.format()` call, a second `print()` argument, `sys.stdout.write(...)`
    are all just nodes `_iter_unmarked` walks through — because the read
    itself, `X.get("headRefName")` or `X["baseRefName"]` naming one of six
    known literal strings, is a narrow, low-false-positive signature
    regardless of what expression happens to contain it. That is NOT true of
    following an already-tainted *variable* through further computation
    (a regex match, `.group()`, a container built and later `.join()`-ed) --
    see `_scan_scope`'s own note on why that is deliberately NOT chased here.
    """
    return {key for sub in _iter_unmarked(node)
            if (key := _refname_key(sub)) is not None}


def _is_sink(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id == "print"
        # `sys.stdout.write(...)` (#976: N_sys_stdout_write) -- specifically
        # that chain, not every `.write(...)` call, so a preset's own file
        # or socket writes are not swept in as an unrelated new sink class.
        return (isinstance(fn, ast.Attribute) and fn.attr == "write"
                and isinstance(fn.value, ast.Attribute)
                and fn.value.attr == "stdout"
                and isinstance(fn.value.value, ast.Name)
                and fn.value.value.id == "sys")
    return isinstance(node, ast.Return) and node.value is not None


def _sink_args(node: ast.AST) -> list:
    if isinstance(node, ast.Call):
        return list(node.args)
    assert isinstance(node, ast.Return) and node.value is not None
    return [node.value]


_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _scopes(tree: ast.AST) -> list:
    """Every function body, plus module level — each on its own.

    One taint dict per *file* was survivable while the keys were rare words.
    `target` is not rare: `_open_mr_line` binds it from `mr['target']` and six
    unrelated functions bind a local of the same name from `f"{remote}/{ref}"`,
    which the tool composed itself. A file-wide dict reported all six, and a
    scanner with six false findings is one somebody adds an allowlist to —
    which is the failure mode this scan exists to avoid, arriving from the
    other side. Scoping is not a softening: closure semantics are preserved,
    because a nested function is also walked with its parent's dict.
    """
    out = [f for f in ast.walk(tree) if isinstance(f, _FUNC)]
    inner = {id(n) for f in out for n in ast.walk(f) if n is not f}
    out.append(tree)
    return [(s, inner if s is tree else set()) for s in out]


def _assign_targets(node: ast.AST) -> "list | None":
    """`[(target_name_node, value_node), ...]` this statement taints, or
    `None` if it is not an assignment shape this scanner tracks at all.

    Positional tuple/list unpacking (#976: `a, b = 1, d.get('headRefName')`)
    is handled here rather than falling through the old `isinstance(target,
    ast.Name)` check, which silently skipped a tuple target completely --
    not narrowing what it caught, just never looking. `AugAssign` (`x +=
    ...`) joins the same table: its target is always singular and a `Name`
    or nothing tracked.
    """
    if isinstance(node, ast.AugAssign):
        return [(node.target, node.value)] if node.value is not None else []
    if isinstance(node, ast.AnnAssign):
        if node.value is None:
            return []
        return [(node.target, node.value)]
    if not isinstance(node, ast.Assign) or node.value is None:
        return []
    pairs = []
    for target in node.targets:
        if (isinstance(target, (ast.Tuple, ast.List))
                and isinstance(node.value, (ast.Tuple, ast.List))
                and len(target.elts) == len(node.value.elts)):
            pairs.extend(zip(target.elts, node.value.elts))
        else:
            pairs.append((target, node.value))
    return pairs


def _scan_scope(path: Path, scope: ast.AST, skip: set) -> list[str]:
    # Source order, not `ast.walk` order: a name is tainted or cleaned by the
    # last assignment *above* the print, and walking breadth-first reads those
    # assignments in the wrong order — which made the first draft of this
    # scanner both miss `job.py` and invent a finding in `check.py`.
    nodes = sorted((n for n in ast.walk(scope) if id(n) not in skip),
                   key=lambda n: (getattr(n, "lineno", 0),
                                  getattr(n, "col_offset", 0)))
    tainted: dict[str, str] = {}
    found: list[str] = []
    for node in nodes:
        for target, value in _assign_targets(node):
            if not isinstance(target, ast.Name):
                continue
            # Deliberately `_unmarked_refnames` alone, NOT "does this value
            # reference an already-tainted name" -- that second rule was
            # tried and reverted (#976): `mr_iid = mr_match.group(1)` after
            # `mr_match = re.match(pattern, ref)` chained taint through two
            # unrelated derived values (a regex match object, its captured
            # group) onto locals that never carry the raw refname text,
            # which is exactly "a scanner with false findings is one
            # somebody adds an allowlist to" from this function's own
            # docstring, arriving from the propagation side instead of the
            # coverage side. A `.get()`/subscript read of one of the six
            # literal keys is a narrow signature wherever it sits; a bare
            # variable reference is not, once it has passed through a call.
            keys = _unmarked_refnames(value)
            if keys:
                tainted[target.id] = sorted(keys)[0]
            else:
                tainted.pop(target.id, None)
        # `print(...)` is not the only sink. `push._open_mr_line` *returns*
        # the f-string its caller prints, so keying on print() alone certified
        # a line that reaches a terminal one frame up (#1038). A returned
        # f-string in a preset is rendered text by construction.
        if not _is_sink(node):
            continue
        # A CALL sink (print/sys.stdout.write) is the terminal render. A
        # RETURN is not necessarily one -- `presets/github/prs.py::_branches`
        # returns `_board.branch_pair(p.get("headRefName"), p.get(...))`
        # for its caller to hand to `_board.render_row`, which flattens
        # EVERY cell (`_untrusted.py`'s own docstring). Scanning a `return`
        # as broadly as a `print()` call turned that load-bearing pattern
        # into a false positive (#976); restrict the whole-argument scan
        # below to call sinks and keep `return` on the narrower
        # FormattedValue-scoped check two paragraphs down, which is what
        # #1038 was actually written to catch.
        is_call_sink = isinstance(node, ast.Call)
        for arg in _sink_args(node):
            # `return line`, where `line` was built from a refname two lines
            # up. No FormattedValue of its own, so the loop below never sees
            # it — and that bare name is the exact shape #1038 shipped.
            if isinstance(arg, ast.Name) and arg.id in tainted:
                found.append(f"{path.name}:{node.lineno} "
                             f"{tainted[arg.id]} (via {arg.id})")
            if is_call_sink:
                # One pass over the WHOLE argument for a direct refname read
                # OUTSIDE any f-string wrapper -- `+`/`%` concatenation, a
                # `.format()` call, a second `print()` argument
                # (#976's C/D/E/F). The old code only walked
                # `ast.FormattedValue` nodes for this, which is why an
                # f-string was covered and every other shape was not.
                for key in sorted(_unmarked_refnames(arg)):
                    found.append(f"{path.name}:{node.lineno} {key}")
            for sub in ast.walk(arg):
                if not isinstance(sub, ast.FormattedValue):
                    continue
                # A direct read inside an f-string (#976's H, via the
                # precise `_unmarked_refnames`/`_iter_unmarked` marker
                # scoping above) -- applies to `return` too, matching
                # #1038's own case.
                for key in sorted(_unmarked_refnames(sub.value)):
                    found.append(f"{path.name}:{node.lineno} {key}")
                marked = bool(MARKERS & _call_names(sub.value))
                if marked:
                    continue
                for name in ast.walk(sub.value):
                    if isinstance(name, ast.Name) and name.id in tainted:
                        found.append(
                            f"{path.name}:{node.lineno} "
                            f"{tainted[name.id]} (via {name.id})")
    return found

def _raw_refname_prints(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for scope, skip in _scopes(tree):
        found.extend(_scan_scope(path, scope, skip))
    return sorted(set(found))


#: The three shapes this scanner recognises as a sink, named here once so
#: the boundary line below and the "Covered" table in #1089 cannot drift
#: apart the way the coverage claim in #976 did: print(...) receiving the
#: value directly or via an f-string, return of an f-string, and return
#: of a bare name already tainted by an earlier assignment. Anything else --
#: sys.stdout.write, a helper the value is merely passed to, %/.format,
#: a dict subscript keyed by a variable -- is outside this count and outside
#: the scan (#1089's own "Not covered" table).
SINK_SHAPES = 3


def test_no_preset_prints_a_refname_raw() -> None:
    """checked N files, M sink shapes, these trees (#1089's second ask):
    a guard that cannot state its own boundary is the three-state defect
    this repo has filed more than any other, wearing a passing test's
    clothes. Printed unconditionally -- on the clean run too, not only on a
    finding -- so a reader of `pytest -rA` or a failure's captured stdout
    sees the scope this assertion actually claims, not just its verdict.
    """
    offenders: list[str] = []
    scanned_files = 0
    for directory in _SCANNED:
        for path in sorted((_ROOT / directory).rglob("*.py")):
            scanned_files += 1
            offenders.extend(_raw_refname_prints(path))
    print(
        f"checked {scanned_files} files across {len(_SCANNED)} trees "
        f"({', '.join(_SCANNED)}), {len(REFNAME_KEYS)} source keys tracked "
        f"({', '.join(sorted(REFNAME_KEYS))}), {SINK_SHAPES} sink shapes "
        f"recognised (print of a value or an f-string, return of an "
        f"f-string, return of a tainted bare name)."
    )
    assert offenders == [], (
        "a refname reaches print() without _untrusted.flat:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_the_scanner_sees_the_defect_it_was_written_for(tmp_path: Path) -> None:
    """A scanner that cannot fail is not a guard (#851's own lesson)."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def main(d):\n"
        "    print(f\"branch: {d.get('headRefName') or '?'}\")\n"
        "    source = d.get('source_branch', '?')\n"
        "    print(f\"Branch: {source}\")\n"
        "    safe = _untrusted.flat(d.get('baseRefName', '?'))\n"
        "    print(f\"Base: {safe}\")\n",
        encoding="utf-8",
    )
    found = _raw_refname_prints(sample)
    assert any("headRefName" in f for f in found)
    assert any("source_branch" in f for f in found)
    assert not any("baseRefName" in f for f in found)
