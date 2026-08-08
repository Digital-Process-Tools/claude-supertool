"""#1119 - the audit table, as a build gate instead of a paragraph.

The two job presets are deliberate private twins: a preset runs with
`presets/` on `sys.path` and cannot import the core, so `gap_marker`,
`split_lines` and the whole trace reader are duplicated on purpose. The cost
of that decision is that a defect found in one does not reach the other unless
someone files it - #1050 was #409 found again 640 issues later, and #444/#445
reached `gl-job` and never reached `gh-job`, both times because the usage that
surfaced them happened to be on one forge.

#1105 and #1119 are the same pair one more time. Each shipped a table in its
changelog fragment saying, per call site, fixed or left alone and why. A table
in a fragment is a statement about the day it was written: the next call site
lands in a file nobody rereads, and the reader after that believes the
question was answered everywhere.

So the table lives here, over BOTH twins at once, and adding a
`str.splitlines()` to either one is a red build until someone writes down
which kind it is. That does not make a defect in one twin automatically fix
the other - nothing can, for defects nobody has named yet. It does the one
thing that is cheap and checkable: for this class, which is the class that has
now recurred four times, the two twins are read in a single list, so the
question "and the other one?" is asked by the build rather than remembered by
a person.

Keyed on `path::enclosing function`, not on line numbers, because a register
that goes stale on every unrelated edit teaches people to regenerate it
without reading it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parent.parent
TWINS = ("presets/github", "presets/gitlab")

# Every `str.splitlines()` in the two twin preset trees, with the judgment
# recorded when it was audited. `_untrusted.split_lines` sites are absent by
# construction - they are not `str.splitlines()` calls.
REGISTER: dict[str, str] = {
    # -- presets/github, audited by #1105 -----------------------------------
    "presets/github/check.py::_annotation_line":
        "a check annotation's message. A render, not a parse: no column-0 "
        "anchor, every part flat()ed, every emitted line carries the indent.",
    "presets/github/issue.py::_print_linked_prs":
        "gh's stderr, last line as an error message. Narrowing would be worse "
        "- it would leave a forged break inside the extracted string instead "
        "of consuming it.",
    "presets/github/pr_create.py::_gh_json":
        "gh's stderr, last line as an error message. Same as above.",
    "presets/github/pr_merge.py::_gh_json":
        "gh's stderr, last line as an error message. Same as above.",
    "presets/github/pr_create.py::main":
        "gh's stdout, scanned for a URL. The extracted value is printed, not "
        "parsed.",
    "presets/github/issue_create.py::main":
        "gh's stdout, scanned for a URL. Same as above.",
    "presets/github/batch_follow.py::main":
        "a local file the caller passed in. A stray separator yields a "
        "username that 404s visibly rather than a forged record.",
    "presets/github/batch_star.py::main":
        "a local file the caller passed in. Same as above.",

    # -- presets/gitlab, audited by #1119 -----------------------------------
    "presets/gitlab/issue.py::_print_related_mrs":
        "glab's stderr, first line as a decline reason. Narrowing would be "
        "worse, for the reason its GitHub siblings are left alone.",
    "presets/gitlab/mr.py::_glab_fail_detail":
        "glab's stderr, first non-empty line as a decline detail. Same.",
    "presets/gitlab/issue_create.py::main":
        "glab's stdout, scanned for an issue URL, then its last line as a "
        "fallback. The extracted value is printed, not parsed.",
    "presets/gitlab/mr.py::_get_conflicting_files":
        "git merge-tree --name-only stdout - PATHS, not content. Left alone "
        "on evidence rather than on reasoning: git octal-quotes every "
        "non-ASCII byte in a path it prints, so a filename cannot carry a "
        "separator into this split. Its sibling in the same file reads blob "
        "content, which is not quoted, and that one was narrowed.",
    "presets/gitlab/mr.py::_get_conflict_hunks":
        "git's stderr, first line as a decline detail. The hunk-content split "
        "in this same function was narrowed by #1119; this one is the "
        "stderr-extraction kind and stays.",
}


class _Visitor(ast.NodeVisitor):
    """Collect `x.splitlines()` calls in one file, keyed by enclosing def."""

    def __init__(self, rel: str, found: dict[str, list[int]]) -> None:
        self._rel = rel
        self._found = found
        self._stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "splitlines":
            key = f"{self._rel}::{'.'.join(self._stack) or '<module>'}"
            self._found.setdefault(key, []).append(node.lineno)
        self.generic_visit(node)


def _call_sites() -> dict[str, list[int]]:
    """`path::function` -> line numbers, for every `x.splitlines()` call."""
    found: dict[str, list[int]] = {}
    for tree in TWINS:
        for path in sorted((REPO / tree).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            module = ast.parse(path.read_text(encoding="utf-8"))
            _Visitor(rel, found).visit(module)
    return found


def test_every_splitlines_in_either_twin_is_registered() -> None:
    sites = _call_sites()
    unregistered = {k: v for k, v in sites.items() if k not in REGISTER}
    assert not unregistered, (
        "New str.splitlines() in a twin preset. Decide which kind it is and "
        "add it to REGISTER, or use _untrusted.split_lines: "
        f"{unregistered}"
    )


def test_the_register_names_no_site_that_is_gone() -> None:
    """A register listing sites that no longer exist is not being read."""
    sites = _call_sites()
    stale = sorted(k for k in REGISTER if k not in sites)
    assert not stale, f"REGISTER entries with no call site left: {stale}"


def test_the_narrowed_readers_did_not_quietly_revert() -> None:
    """The two structural parses #1105 and #1119 fixed, named explicitly."""
    sites = _call_sites()
    for gone in (
        "presets/github/job.py::main",
        "presets/gitlab/job.py::main",
    ):
        assert gone not in sites, (
            f"{gone} is splitting a CI log with str.splitlines() again "
            "(#1105 / #1119)"
        )


def test_every_register_entry_states_a_reason() -> None:
    for key, reason in REGISTER.items():
        assert len(reason) > 40, (key, reason)
