"""#1130 - the `presets/git/` audit table, as a build gate instead of a paragraph.

Third and largest of the forged-boundary audits: #1105 read `presets/github`
(12 sites, 2 narrowed), #1119 read `presets/gitlab` (9 sites, 3 narrowed), and
this one reads `presets/git` - 44 `str.splitlines()` call sites across 12 files,
2 narrowed. The issue's estimate of ~60 was high; the count is stated here so
the next reader does not re-derive it.

Same construction as `test_preset_twin_splitlines_register_1119.py`, and the
same reason: a table in a changelog fragment is a statement about the day it was
written, so adding a `str.splitlines()` to this tree is a red build until
someone writes down which kind it is.

**What the audit actually found, because the ratio is the interesting part.**
2 of 44 is lower than either prior sweep, and not because this tree was read
less carefully. `presets/git/` divides almost cleanly in three:

* **Extraction sites** - `stderr.splitlines()[0]` or `[-1]` as a decline
  reason. Narrowing these makes them WORSE (#1105's central finding, confirmed
  empirically by #1119): taking one element of the split CONSUMES the
  separator, so the split is itself the disclosure. Left alone, every time.
* **Readers that leave git's quoting ON.** `core.quotePath` defaults to true,
  and git then octal-quotes every byte above 0x7F in a path it prints - so a
  path carrying U+2028 arrives as three octal escapes and cannot reach the
  split as a separator at all. This is evidence, not reasoning, and it is the
  same argument #1119 used to leave `mr.py::_get_conflicting_files` alone.
* **`presets/git/diff.py`, which turns that quoting OFF** - deliberately and
  correctly, so an accented filename reaches the receipt as itself. Both of its
  readers were narrowed. The two facts are the same fact: `git-diff` is the one
  op in this tree that asks git not to protect it, so it is the one op that has
  to protect itself.

A fourth group exists and is named rather than hidden: `resolve.py`'s conflict
state machines split file content and key on `<<<<<<<` at column 0. They are
left alone on the strongest reason in the file - a forged separator grants an
attacker nothing a plain newline does not already grant them, because a
contributor can simply put a marker-shaped line at column 0 in their own file.
Narrowing there would be motion, not defence.

Keyed on `path::enclosing function`, not on line numbers, because a register
that goes stale on every unrelated edit teaches people to regenerate it without
reading it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parent.parent
TREE = "presets/git"

#: Every `str.splitlines()` in `presets/git/`, with the judgment recorded when
#: it was audited. `_untrusted.split_lines` sites are absent by construction -
#: they are not `str.splitlines()` calls.
REGISTER: dict[str, str] = {
    # -- _git_common.py -----------------------------------------------------
    "presets/git/_git_common.py::_list_conflicts":
        "`git diff --name-only --diff-filter=U` - PATHS, and this reader leaves "
        "core.quotePath at its default, so a byte above 0x7F is octal-quoted "
        "before the split sees it. A forged row could only ADD a conflict, "
        "whose effect is to refuse to proceed.",
    # `_first_error_line` was here, and its entry argued that narrowing the
    # split "would leave a forged break INSIDE the reported string instead of
    # consuming it". That was a choice between two bad options because neither
    # end was flattened. #1475 flattened the return, so the break inside is now
    # disclosed as `[U+2028]` rather than left live — and narrowing became
    # strictly better, since consuming it dropped the hidden tail out of the
    # receipt entirely. The site now uses `_untrusted.split_lines`.
    "presets/git/_git_common.py::_remotes_could_host_a_request":
        "`git remote -v`. A URL carrying a separator could forge a row and flip "
        "'every remote is a local path' to 'one names a host', silencing the "
        "#948 disclosure - but the author of that value is whoever ran `git "
        "remote add` in this clone, not the remote and not a contributor.",

    # -- checkout.py --------------------------------------------------------
    "presets/git/checkout.py::main":
        "`status --porcelain=v1` counted into staged/unstaged/untracked, and "
        "`log -3` subjects rendered. Default quoting is left on for the "
        "porcelain read, and a forged row can only inflate a count.",

    # -- commit.py ----------------------------------------------------------
    "presets/git/commit.py::_with_coauthor":
        "the commit message the CALLER typed, scanned for an existing "
        "Co-Authored-By trailer. A forged break suppresses the auto-trailer in "
        "a message its own author wrote - self-inflicted, and git's trailer "
        "parser would disagree about the line either way.",
    "presets/git/commit.py::main":
        "`show --shortstat` last line. Git's own generated summary; it carries "
        "no path and no author-controlled text.",

    # -- diverge.py ---------------------------------------------------------
    "presets/git/diverge.py::main":
        "log subjects and `--name-status` rows, both rendered as a capped list "
        "with a count beside it. Default quoting on, nothing parsed out of "
        "either, and a forged split can only inflate the count it prints.",

    # -- investigate.py -----------------------------------------------------
    "presets/git/investigate.py::main":
        "log lines, two diffs counted for +/- totals, and `blame "
        "--line-porcelain` parsed on a 40-hex header / `author ` / leading-tab "
        "content. The blame parse is the closest call in this tree: file "
        "CONTENT is in that stream, so a crafted line can misattribute an "
        "author or a date. Left alone because git-investigate gates nothing, "
        "writes nothing, and the hotspot list is advisory - but this is the "
        "site to revisit first if that ever stops being true.",

    # -- merge.py -----------------------------------------------------------
    "presets/git/merge.py::_fresh_merge_ref":
        "fetch's stderr, last line as a WARN detail. The extraction kind - same "
        "as `_first_error_line`, same reason for leaving it.",

    # -- push.py ------------------------------------------------------------
    "presets/git/push.py::_remote_names":
        "`git remote` names. Git refuses to create a remote whose name carries "
        "a control character, and a forged extra name only produces a lookup "
        "that misses.",
    "presets/git/push.py::_ref_line":
        "git's per-ref PORCELAIN push status, three TAB-separated fields. A "
        "refname cannot carry a TAB - check-ref-format rejects every ASCII "
        "control character - so a forged fragment can never present three "
        "fields, is dropped by the `len(parts) != 3` test, and the caller gets "
        "the ('', '') that #641/#661 already render as 'git reported no line', "
        "never as a no.",
    "presets/git/push.py::_spawn_watch":
        "the watch child's first non-empty output line, as a decline detail. "
        "The extraction kind.",
    "presets/git/push.py::_uncommitted_leftovers":
        "`status --porcelain`, printed as a 'you forgot to commit this' list "
        "rather than parsed. Default quoting on; a forged row only adds to a "
        "warning that exists to be noticed.",
    "presets/git/push.py::_discarded_by_force":
        "`log %h %an: %s` over the commits a force-push discarded. An author or "
        "subject carrying a separator INFLATES that list - the loud direction, "
        "on the one check here whose failure must never read as reassurance.",
    "presets/git/push.py::_incoming_commits":
        "the same log shape for commits the remote added. `behind` is the "
        "length of this list and drives a warning plus a cap line; a forged "
        "split can inflate it and cannot hide a commit.",
    "presets/git/push.py::_recover_by_rebase":
        "`diff --name-only --diff-filter=U` after a failed rebase. A non-empty "
        "list selects the conservative arm - leave the rebase paused - so a "
        "forged row cannot produce the destructive outcome.",

    # -- resolve.py ---------------------------------------------------------
    "presets/git/resolve.py::_union_lines":
        "conflicted file content, keyed on `<<<<<<<` at column 0, computing the "
        "union the markdown heading guard reads. A forged separator grants "
        "nothing a plain newline does not: a contributor can already put a "
        "marker-shaped line at column 0. It must also stay byte-identical to "
        "`_union_file`'s parse, or the guard stops describing the union it "
        "guards - so narrowing one alone would be a new defect.",
    "presets/git/resolve.py::_union_file":
        "the same state machine, writing the result back. Same reason, and "
        "`keepends=True` means a split with no forged marker after it "
        "reassembles byte-identically.",
    "presets/git/resolve.py::_resolve_blocks":
        "the per-block variant of the same machine. Same reason; the "
        "`_scan_markers` hard gate refuses to stage whatever it produces.",
    "presets/git/resolve.py::_count_blocks":
        "counts `<<<<<<<` lines. Self-consistent with `_resolve_blocks`, which "
        "splits identically, and a real marker at a real column 0 survives any "
        "splitting - so this can over-count and never under-count.",
    "presets/git/resolve.py::_scan_markers":
        "the hard gate before staging. Over-reporting refuses a stage; "
        "under-reporting is what would be dangerous, and is impossible here for "
        "the reason above.",
    "presets/git/resolve.py::_union_attr_paths":
        "`check-attr merge -- PATHS`, matched on a `: merge: union` suffix. "
        "Fail-closed by construction: the result is SUBTRACTED from the "
        "candidate set, so a forged fragment is not a candidate and removes "
        "nothing. A split can only fail to whitelist, never wrongly whitelist.",
    "presets/git/resolve.py::_digest_block":
        "one file's validator rows. `_RESULT_ROW` and `_SKIPPED_ROW` both anchor "
        "at `^`, so a fragment can add a row but never destroy the head of a "
        "real one - a genuine `1 err` row keeps matching, and a forged `ok` "
        "cannot turn the digest from warn back into ok.",
    "presets/git/resolve.py::_child_failed":
        "the validate child's stderr, first non-empty line. The extraction "
        "kind, and the value is already `_untrusted.flat`-ed on the way out.",
    "presets/git/resolve.py::_validate_paths":
        "splits the child's combined output on `validate: ` headers. A forged "
        "header can only ADD a block, and `len(blocks) != len(files)` then "
        "declines for the whole batch. #886 reached exactly this with a "
        "filename separated by U+2028, and that guard is why it was a denial "
        "rather than a second forged clean bill.",

    # -- status.py ----------------------------------------------------------
    "presets/git/status.py::main":
        "five sites: `branch -vv` (DEAD - its only assignment, `current_branch`, "
        "is never read; the render uses `rev-parse`), `for-each-ref` "
        "refname-TAB-track (a refname cannot carry a TAB, so a fragment has no "
        "track field and the `'ahead' in track` test drops it), log subjects "
        "rendered, `status --porcelain=v1` with default quoting left ON - "
        "unlike diff.py - and `stash list` rendered with a count.",

    # -- trail.py -----------------------------------------------------------
    "presets/git/trail.py::main":
        "two pickaxe log reads, rendered and counted, plus a `git show` hunk "
        "extractor keyed on `diff --git` / `@@`. The extractor is the same "
        "shape as diff.py's and is left alone for the opposite reason: default "
        "quoting is on here, and its output is a search render, not a review "
        "gate - the worst a forged `@@` does is drop context lines from a "
        "display the reader is already reading.",

    # -- worktrees.py -------------------------------------------------------
    "presets/git/worktrees.py::parse_worktree_list":
        "`worktree list --porcelain`, which does not quote paths (that is what "
        "its `-z` form is for). A worktree path carrying a separator forges an "
        "entry - but that path is the local user's own choice, the op is "
        "inspection-only, and a fabricated tree can only add a row to an "
        "inventory that never removes anything.",
    "presets/git/worktrees.py::_read_cwd_table_uncached":
        "`lsof -F pn` records. A forged row makes a process appear to hold a "
        "tree, which pushes the verdict toward `occupied` - the conservative "
        "direction this op is built on, where `idle` has to be earned.",
    "presets/git/worktrees.py::remote_branch_names":
        "stderr's first line as a decline reason (extraction kind), and "
        "`for-each-ref refname:strip=3` into a set answering 'has this branch "
        "been pushed'. check-ref-format ACCEPTS U+2028 (#1119), so a hostile "
        "remote could publish a ref whose tail spells your branch name and make "
        "an unpushed branch read as pushed. Left alone: it is one word in an "
        "inspection-only column and nothing acts on it - the second site to "
        "revisit, after investigate.py's blame parse.",
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
    for path in sorted((REPO / TREE).rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"))
        _Visitor(rel, found).visit(module)
    return found


def test_every_splitlines_in_presets_git_is_registered() -> None:
    sites = _call_sites()
    unregistered = {k: v for k, v in sites.items() if k not in REGISTER}
    assert not unregistered, (
        "New str.splitlines() in presets/git. Decide which kind it is and add "
        "it to REGISTER, or use _untrusted.split_lines: "
        f"{unregistered}"
    )


def test_the_register_names_no_site_that_is_gone() -> None:
    """A register listing sites that no longer exist is not being read."""
    sites = _call_sites()
    stale = sorted(k for k in REGISTER if k not in sites)
    assert not stale, f"REGISTER entries with no call site left: {stale}"


def test_the_narrowed_readers_did_not_quietly_revert() -> None:
    """The two the audit narrowed. Both are in the one op that disables quoting."""
    sites = _call_sites()
    for gone in (
        "presets/git/diff.py::_changed_files",
        "presets/git/diff.py::_scan_red_flags",
    ):
        assert gone not in sites, (
            f"{gone} is parsing a diff with str.splitlines() again, and it runs "
            "with core.quotepath=false, so nothing upstream is quoting the "
            "separator for it (#1130)"
        )


def test_every_register_entry_states_a_reason() -> None:
    for key, reason in REGISTER.items():
        assert len(reason) > 40, (key, reason)
