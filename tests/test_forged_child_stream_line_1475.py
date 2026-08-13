"""A child process's stdout may not become a line the tool wrote (#1475).

The class behind #1470, which closed it for `git-push` only. `gh` writes the
GitHub API's refusal text onto its own stderr, a commit hook writes whatever it
likes, and both reached a receipt at column 0 with no `_untrusted` call between
them. `_untrusted.split_lines` cuts on LF / CR / CRLF alone, by design (#1081),
so a U+2028 survives *inside* a relayed line and everything the reader anchors
at column 0 becomes the writer's to choose.

**The fix is at the seam, not at the sites.** Seven sites were named and the
sweep found 182 candidates in 40 files, which is what a per-site fix earns: the
same defect re-filed once per call. So

* `_git_common._first_error_line` flattens what it returns. Every caller —
  `git-commit`, `git-push`, and whatever is written next — is covered at once,
  and `_untrusted.flat` is idempotent so the callers that already flattened pay
  a no-op rather than a second substitution. It also splits with
  `_untrusted.split_lines` instead of `str.splitlines()`: it is *parsing* a
  line-oriented stream, and the parse must not fold on a separator the writer
  chose (#1081).
* `pr._format_error` is the one sink for `gh-pr`'s two error prints, and
  flattens there.
* `commit._failure_receipt` is the whole failure render, extracted so the dump
  can be relayed the way `git-push` relays its own (#1448): the child's lines
  under a `> ` prefix, disclosed with `visible(keep=tab)` rather than dropped.

The bar is the one `tests/test_forged_branch_line_965.py` set: assert on what a
consumer counts, not on `flat` having been called — a site can call it and
print the raw value anyway — and assert the forged text is still *readable*,
because disclosed-never-stripped is the trade this repo has already made.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


git_common = _load("presets/git/_git_common.py", "git_common_1475")
commit = _load("presets/git/commit.py", "git_commit_1475")
gh_pr = _load("presets/github/pr.py", "github_pr_1475")

#: Survives `_untrusted.split_lines`, breaks `str.splitlines()` (#886, #1081).
SEP = chr(0x2028)
ESC = chr(0x1b)

FORGED_RESULT = "[result] 1 op run, 1 write"
FORGED_STATUS = "Status: COMMITTED"
MARKERS = ("[result]", "Status:", "First error:", "HEAD after:")


def relayed(lines: list) -> list:
    """Only the child's own lines — everything under `--- git output ---`.

    The three lines above it are supertool's, and they legitimately open with
    the markers below. Scoping the assertion to the relay is what makes it an
    assertion about the *forgery* rather than about the render.
    """
    at = lines.index("--- git output ---")
    return lines[at + 1:]


def assert_no_forged_marker(lines: list) -> None:
    """No line the child wrote may open with a marker the tool owns."""
    for line in lines:
        for marker in MARKERS:
            assert not line.startswith(marker), (
                f"a child stream forged a column-0 {marker} line:" + chr(10)
                + chr(10).join(f"  {i:>3} | {ln}"
                               for i, ln in enumerate(lines, 1))
            )


def assert_disclosed(text: str) -> None:
    """The separator is shown, not deleted, and never survives as itself."""
    assert SEP not in text, "the raw separator reached the render"
    assert "U+2028" in text, "the separator was dropped instead of disclosed"


# ---------------------------------------------------------------------------
# the seam: _first_error_line
# ---------------------------------------------------------------------------

def test_first_error_line_returns_one_line() -> None:
    """It is printed at column 0 by two ops and interpolated into a third."""
    got = git_common._first_error_line(
        "step 1" + chr(10) + "error: refused" + SEP + FORGED_RESULT)
    assert got.splitlines() == [got], got
    assert_disclosed(got)
    assert "1 op run, 1 write" in got, "the refusal text was censored"


def test_first_error_line_discloses_an_escape_sequence() -> None:
    """`ESC [2K ESC [1A` erases a line the tool already wrote (#851)."""
    got = git_common._first_error_line("error: refused" + ESC + "[2K")
    assert ESC not in got
    assert "error: refused" in got


def test_first_error_line_does_not_fold_on_a_separator_the_writer_chose() -> None:
    """The parse is line-oriented, so it cuts on LF/CR/CRLF only (#1081).

    With `str.splitlines()` the forged tail was a *line* of its own, and a
    hostile writer could therefore choose which line the scan returned.
    """
    got = git_common._first_error_line(
        "fatal: real cause" + SEP + "pushed successfully")
    assert "real cause" in got


# ---------------------------------------------------------------------------
# gh-pr's one error sink
# ---------------------------------------------------------------------------

def test_gh_pr_format_error_is_one_line() -> None:
    """`gh` echoes the GitHub API's own message; two prints share this sink."""
    got = gh_pr._format_error("boom" + SEP + FORGED_RESULT, "PR", "42")
    assert got.splitlines() == [got], got
    assert_disclosed(got)
    assert "1 op run, 1 write" in got


# ---------------------------------------------------------------------------
# git-commit's failure receipt
# ---------------------------------------------------------------------------

def _completed(stdout: str = "", returncode: int = 1, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


def test_commit_failure_receipt_cannot_be_forged_by_a_hook() -> None:
    """A pre-commit hook chose the bytes; only supertool owns column 0."""
    hook = ("running checks" + chr(10)
            + "error: blocked" + SEP + FORGED_STATUS + SEP + FORGED_RESULT)
    lines = commit._failure_receipt(_completed(stderr=hook),
                                    head_before="a" * 7, head_after="a" * 7)
    assert_no_forged_marker(relayed(lines))
    assert_disclosed(chr(10).join(lines))
    assert "error: blocked" in chr(10).join(lines)


def test_commit_failure_receipt_still_relays_the_whole_transcript() -> None:
    """Containment may not cost the reader the hook output they came for."""
    hook = "line one" + chr(10) + "line two"
    body = chr(10).join(commit._failure_receipt(
        _completed(stderr=hook), head_before="a" * 7, head_after="b" * 7))
    assert "line one" in body and "line two" in body
    assert "Status: commit returned exit 1" in body.splitlines()


def test_a_tab_survives_the_commit_relay() -> None:
    """The other half of #1448's trade, pinned so it is not re-flattened."""
    tab = chr(9)
    body = chr(10).join(commit._failure_receipt(
        _completed(stdout="PASS" + tab + "tests/test_a.py"),
        head_before="a" * 7, head_after="a" * 7))
    assert "PASS" + tab + "tests/test_a.py" in body
    assert "0009" not in body

# ---------------------------------------------------------------------------
# the detector: nothing stopped the eighth
# ---------------------------------------------------------------------------
#
# The seven fixes above are worth less than this section. #1470 closed one op
# and the class re-arrived seven times; the issue says so in its own acceptance
# clause, and its quoted refusal explains why the obvious guard is hard: the
# taint set for a child stream is not `REFNAME_KEYS`. It is `combined`, `blob`,
# loop variables over `split_lines(...)` and helper returns, against parse-only
# consumers that must not trip it. Rushed, it "arrives as an allowlist that
# grows quietly — which is the failure mode of the thing it would be guarding".
#
# **So this is not a zero-assertion, and pretending otherwise is what would
# make it useless.** Measured on this branch: 174 candidate sites in 36 files
# across `presets/git`, `presets/github` and `presets/gitlab`. Not all are
# defects — `push._local_head` returns `r.stdout.strip()`, and that is a SHA —
# and closing them is four lanes of work this PR is not.
#
# What is enforceable today, with no exemption mechanism at all, is a **count
# per file that may only go down**. It is not an allowlist: there is nothing to
# add a site to. A new relay in any of these 36 files bumps its number and
# fails; a fixed one lowers it and fails, with the message telling you to write
# the smaller number down. Both directions are one visible line in a diff,
# which is exactly what "grows quietly" was not.
#
# What it does NOT catch, said plainly: a relay added to a file with no row
# here is caught (its count goes 0 → 1), but a relay added to `push.py` in the
# same commit that fixes another one nets to 35 and passes. That is the price
# of shipping a ratchet instead of a gate, and the gate costs 174 fixes first.

#: The two attributes a `CompletedProcess` exposes, and a parameter carrying
#: either name. Small on purpose, for the reason `REFNAME_KEYS` is: the scan is
#: only worth having while its key set needs no exemptions.
STREAM_ATTRS = frozenset({"stdout", "stderr"})

#: Anything that marks a child's text before it is rendered.
MARKS = frozenset({"flat", "fence", "scrub", "visible", "render_row",
                   "shell_ref"})

#: Calls whose result cannot be a string, so the taint stops there. A type
#: argument, not an allowlist: `json.loads(r.stdout)` yields a dict, and every
#: field read back off it is the *other* scanner's question (#965).
NOT_TEXT = frozenset({"loads", "int", "float", "len", "bool"})

_SCANNED = ("presets/github", "presets/gitlab", "presets/git")

#: file -> how many child-stream relays reach a sink unmarked. May only shrink.
CENSUS = {
    "presets/git/_git_common.py": 7,
    "presets/git/blame.py": 2,
    "presets/git/checkout.py": 10,
    "presets/git/commit.py": 9,
    "presets/git/conflicts.py": 2,
    "presets/git/diff.py": 4,
    "presets/git/diverge.py": 3,
    "presets/git/investigate.py": 4,
    "presets/git/merge.py": 10,
    "presets/git/push.py": 35,
    "presets/git/resolve.py": 2,
    "presets/git/status.py": 16,
    "presets/git/trail.py": 4,
    "presets/git/worktrees.py": 6,
    "presets/github/_release_gate.py": 2,
    "presets/github/batch_follow.py": 1,
    "presets/github/batch_star.py": 1,
    "presets/github/branch.py": 4,
    "presets/github/check.py": 1,
    "presets/github/issue.py": 3,
    "presets/github/issue_create.py": 3,
    "presets/github/issues.py": 2,
    "presets/github/job.py": 5,
    "presets/github/labels.py": 2,
    "presets/github/pr.py": 6,
    "presets/github/pr_create.py": 5,
    "presets/github/pr_merge.py": 5,
    "presets/github/prs.py": 2,
    "presets/github/run.py": 3,
    "presets/gitlab/api.py": 1,
    "presets/gitlab/issue.py": 2,
    "presets/gitlab/issue_create.py": 2,
    "presets/gitlab/job.py": 1,
    "presets/gitlab/mr.py": 3,
    "presets/gitlab/pipeline.py": 4,
    "presets/gitlab/runners.py": 2,
}

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _walk_text(node: ast.AST):
    """`ast.walk`, but never into a call whose result cannot be a string."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            fn = n.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", None))
            if name in NOT_TEXT:
                continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _call_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
    return names


def _streams_in(node: ast.AST, tainted: dict) -> set:
    keys = set()
    for sub in _walk_text(node):
        if isinstance(sub, ast.Attribute) and sub.attr in STREAM_ATTRS:
            keys.add(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in tainted:
            keys.add(tainted[sub.id])
    return keys


def _unmarked(node: ast.AST, tainted: dict) -> set:
    keys = _streams_in(node, tainted)
    return set() if MARKS & _call_names(node) else keys


def _is_sink(node: ast.AST) -> bool:
    """`print`, and a bare `return` — a returned f-string is rendered text.

    Keying on `print` alone certified `push._open_mr_line`, whose caller does
    the printing one frame up; #1038 learnt that on the sibling scanner.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id == "print"
    return isinstance(node, ast.Return) and node.value is not None


def _sink_args(node: ast.AST) -> list:
    if isinstance(node, ast.Call):
        return list(node.args)
    return [node.value]


def _scopes(tree: ast.AST) -> list:
    """Every function body, plus module level, each with its own taint dict."""
    out = [f for f in ast.walk(tree) if isinstance(f, _FUNC)]
    inner = {id(n) for f in out for n in ast.walk(f) if n is not f}
    out.append(tree)
    return [(s, inner if s is tree else set()) for s in out]


def _params(scope: ast.AST) -> dict:
    """A parameter literally named `stdout`/`stderr` arrives tainted.

    `pr._format_error(stderr, resource, identifier)` is the shape the seven
    named sites had: the attribute read happens at the caller, and the raw
    interpolation happens here. Without this the scan reported neither.
    """
    if not isinstance(scope, _FUNC):
        return {}
    a = scope.args
    names = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    return {x.arg: x.arg for x in names if x.arg in STREAM_ATTRS}


def _scan_scope(path: Path, scope: ast.AST, skip: set) -> list:
    # Source order, not `ast.walk` order: a name is tainted or cleaned by the
    # last assignment *above* the sink.
    nodes = sorted((n for n in ast.walk(scope) if id(n) not in skip),
                   key=lambda n: (getattr(n, "lineno", 0),
                                  getattr(n, "col_offset", 0)))
    tainted = _params(scope)
    found = []
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            keys = _unmarked(node.value, tainted)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if keys:
                    tainted[target.id] = sorted(keys)[0]
                else:
                    tainted.pop(target.id, None)
        if not _is_sink(node):
            continue
        for arg in _sink_args(node):
            for key in sorted(_unmarked(arg, tainted)):
                found.append(f"{path.name}:{node.lineno} {key}")
    return found


def raw_child_stream_sinks(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for scope, skip in _scopes(tree):
        found.extend(_scan_scope(path, scope, skip))
    return sorted(set(found))


def _measure() -> dict:
    counts = {}
    for directory in _SCANNED:
        for path in sorted((_ROOT / directory).rglob("*.py")):
            n = len(raw_child_stream_sinks(path))
            if n:
                counts[f"{directory}/{path.name}"] = n
    return counts


def test_no_new_child_stream_reaches_a_receipt_raw() -> None:
    """The ratchet. Every number may fall; none may rise; none may appear."""
    counts = _measure()
    grew = sorted(f"  {k}: {CENSUS.get(k, 0)} -> {v}"
                  for k, v in counts.items() if v > CENSUS.get(k, 0))
    assert not grew, (
        "a child process's stream reaches a receipt at column 0 in a place it "
        "did not before (#1475). Route it through `presets/_untrusted` — "
        "`flat` for a field on a line the tool owns, `visible(keep=tab)` per "
        "line for a relayed transcript — rather than raising the number:"
        + chr(10) + chr(10).join(grew))


def test_the_census_shrinks_rather_than_going_stale() -> None:
    """A fixed site must be written down, or the ratchet stops ratcheting.

    Split from the test above so a burn-down PR fails on *this* one, which
    says what to do, instead of on the guard that says something got worse.
    """
    counts = _measure()
    shrank = sorted(f"  {k}: {CENSUS[k]} -> {counts.get(k, 0)}"
                    for k in CENSUS if counts.get(k, 0) < CENSUS[k])
    assert not shrank, (
        "sites were fixed and CENSUS still claims the old number — write the "
        "smaller one down in tests/test_forged_child_stream_line_1475.py:"
        + chr(10) + chr(10).join(shrank))


def test_the_scanner_sees_the_defect_it_was_written_for(tmp_path: Path) -> None:
    """A scanner that cannot fail is not a guard (#851's own lesson)."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def render(r):" + chr(10)
        + "    print(f'ERROR: {r.stderr.strip()}')" + chr(10)
        + "    combined = (r.stdout or '') + (r.stderr or '')" + chr(10)
        + "    print(combined.strip())" + chr(10)
        + "    safe = _untrusted.flat(r.stderr)" + chr(10)
        + "    print(f'ok: {safe}')" + chr(10)
        + "    n = int(r.stdout)" + chr(10)
        + "    print(f'n: {n}')" + chr(10)
        + "def relay(stderr):" + chr(10)
        + "    return f'ERROR: {stderr.strip()}'" + chr(10),
        encoding="utf-8",
    )
    found = raw_child_stream_sinks(sample)
    lines = {int(f.split(":")[1].split()[0]) for f in found}
    assert lines == {2, 4, 10}, found
