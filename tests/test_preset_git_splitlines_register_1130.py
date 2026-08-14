"""#1130 - the `presets/git/` audit table, as a build gate instead of a paragraph.

Third and largest of the forged-boundary audits: #1105 read `presets/github`,
#1119 read `presets/gitlab`, and this one reads `presets/git` - 23
`str.splitlines()` call sites across 23 enclosing functions in 8 files.

Those numbers were 27 / 24 / 9 until #1693, which narrowed the four sites in
`investigate.py::main` and so emptied the file: the log render and the two diff
renders are #1681's shape one function further on, and the fourth is the blame
parse this register carried as its only open finding. That one did not take
`_untrusted.split_lines` — see the entry that used to be here, and
`test_the_narrowed_readers_did_not_quietly_revert`, which now names it.

They were 38 / 27 / 11 until #1681, which narrowed the eleven sites in
six functions that render EVERY line of a log stream, counted. That is a
different question from the one this register asks, and the register could not
have answered it: the deciding rule below asks whether a forged row is
survivable, and at an every-line render it is - a `NOT QUOTED, harmless` entry
saying "a forged split can only inflate the count" is a correct answer to the
question it was asked. What made those eleven worth narrowing anyway is that the
inflated count IS the product there, and that narrowing the split alone would
have left the separator live in a rendered row; the repair is `split_lines`
plus `visible()`, and it is not a repair this table's third arm reaches for.

Same construction as `test_preset_twin_splitlines_register_1119.py`, and the
same reason: a table in a changelog fragment is a statement about the day it was
written, so adding a `str.splitlines()` to this tree is a red build until
someone writes down which kind it is.

**This register published one deciding rule, and #1654 measured it.** The rule
was: `core.quotePath` defaults ON, git octal-quotes every byte above 0x7F
before a split sees it, only `git-diff` turns quoting off, therefore every
other reader here is safe for free. The premise is true and its scope is
narrower than the conclusion. `core.quotePath` quotes **pathnames**. On git
2.46.2, in one repository holding a file whose name carries an e-acute, one
branch and one commit subject each carrying a U+2028:

* `ls-files` -> the name comes back double-quoted with one octal escape per
  byte above 0x7F. Quoted, as advertised.
* `log --format=%s` -> the U+2028, raw.
* `for-each-ref --format=%(refname)` -> raw.
* `branch -vv` -> raw, in both the refname and the subject.
* `show` content lines -> raw.
* `blame --line-porcelain` -> raw, in `summary` and in the content line.
* stderr -> never a pathname, so never in scope at all.

So the rule answered for **3 of the 27 entries then below**, and was quoted at
all 27. Those three are the readers that read a path and nothing else - six,
now that #1681 has narrowed away every other split in three of those functions.
The rest are
not defects - most are safe on grounds that are just as good and are not this
one - but "safe by quoting" was the wrong sentence in twenty-four places, and a
wrong reason is what a later reader extends to the next call site.

**So every entry now opens with the ground it actually rests on**, one of
three, and `test_every_register_entry_names_the_ground_it_rests_on` refuses an
entry that names none:

* ``QUOTED PATH`` - the reader reads a pathname and git quoted it first. The
  rule as published, applied where it holds. Six entries: three, plus the three
  functions #1681 emptied of everything else (`checkout.py::main`,
  `diverge.py::main`, `status.py::main` are each down to their one
  `--porcelain` / `--name-status` read).
* ``NOT QUOTED, harmless`` - quoting does not reach this stream, and the entry
  says what does: the harm is fail-safe (a forged row can only refuse), or
  fail-closed, or inflates a count that is loud in the direction it inflates,
  or the text's author is the local operator rather than a stranger.
  Seventeen entries.
* ``NOT QUOTED, open`` - quoting does not reach it and the harm is real.
  **Zero since #1693**, and the ground is kept rather than deleted because a
  register whose only unsafe classification has no spelling is one that cannot
  record the next one. Its single entry was `investigate.py`'s blame parse:
  `blame --line-porcelain` interleaves porcelain headers with the blamed file's
  OWN lines, so a source line spelling `<U+2028>author X<U+2028><TAB>text`
  added a row to `## Blame hotspots` carrying an author, a date and a line
  number no commit had. `split_lines` was not the fix there and is why that
  site left this register entirely: the repair is git's own separator, LF,
  which a file line cannot contain by definition - AND a second reader nobody
  had counted, `_git_common._git`, which runs `subprocess.run(text=True)` and
  so rewrote a lone CR into LF before any splitter could decline to honour it.
  A bare CR in a source file forged the same row over plain ASCII, and no
  choice of splitter closes that. `_git_verbatim` does.

**What #1652 retired, and what this register must no longer say.** Five
`presets/github/` entries used to be justified by "`str.splitlines()` CONSUMES
an exotic separator, so narrowing would leave a forged U+2028 inside the
extracted string". Half an argument: consuming the separator also discards
everything on the other side of it, so the writer still chose which segment
became the message. Four entries here leaned on it under the name "the
extraction kind"; #1654 narrowed the two whose stream a stranger writes
(`merge.py::_fresh_merge_ref`, `worktrees.py::remote_branch_names`) and
restated the two whose stream this tool's own child writes.
`test_no_entry_rests_on_the_retired_reasoning` keeps it retired.

A fourth group is named rather than hidden: `resolve.py`'s conflict state
machines split file content and key on `<<<<<<<` at column 0. A forged
separator grants an attacker nothing a plain newline does not already grant
them, because a contributor can simply put a marker-shaped line at column 0 in
their own file. Narrowing there would be motion, not defence.

Keyed on `path::enclosing function`, not on line numbers, because a register
that goes stale on every unrelated edit teaches people to regenerate it without
reading it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parent.parent
TREE = "presets/git"

#: The three grounds an entry may rest on. An entry opening with none of them
#: is one nobody partitioned - which is the state this register was in before
#: #1654, when one sentence stood in for all three.
GROUNDS = ("QUOTED PATH - ", "NOT QUOTED, harmless - ", "NOT QUOTED, open - ")

#: How many entries rest on each, published so a drift is one visible line in a
#: diff rather than a re-derivation. Asserted exact in both directions.
GROUND_TALLY = {"QUOTED PATH - ": 6,
                "NOT QUOTED, harmless - ": 17,
                "NOT QUOTED, open - ": 0}

#: Phrases that state the argument #1652 retired. An entry may describe it in
#: the past tense; the check is on the register's reasons, not on this file.
RETIRED = ("the extraction kind", "consumes the separator",
           "CONSUMES the separator")

#: Every `str.splitlines()` in `presets/git/`, with the judgment recorded when
#: it was audited. `_untrusted.split_lines` sites are absent by construction -
#: they are not `str.splitlines()` calls.
REGISTER: dict[str, str] = {
    # -- _git_common.py -----------------------------------------------------
    # This entry is also the ground for `resolve.py`'s five `✓`/`⊘`/`✗` receipt
    # rows, which interpolate `path` with no `_untrusted.flat` (#1693). Every
    # such `path` is a member of THIS function's result: `resolve.py::main`
    # takes `targets` from `_list_conflicts()` directly, or from a
    # comma-separated argv list it first refuses unless every element is
    # already in that set. So argv is a FILTER over the quoted set, not a
    # second source — which is a stronger ground than "argv is the caller's
    # own", and it is the one that would be lost if that refusal were relaxed.
    # `tests/test_git_investigate_and_resolve_relay_grounds_1693.py` pins it.
    "presets/git/_git_common.py::_list_conflicts":
        "QUOTED PATH - `git diff --name-only --diff-filter=U`, and a pathname "
        "is exactly what core.quotePath octal-quotes, so a byte above 0x7F "
        "never reaches this split as a separator. A forged row could only ADD "
        "a conflict, whose effect is to refuse to proceed.",
    # `_first_error_line` was here, and its entry argued that narrowing the
    # split "would leave a forged break INSIDE the reported string instead of
    # consuming it". That was a choice between two bad options because neither
    # end was flattened. #1475 flattened the return, so the break inside is now
    # disclosed as `[U+2028]` rather than left live - and narrowing became
    # strictly better, since consuming it dropped the hidden tail out of the
    # receipt entirely. The site now uses `_untrusted.split_lines`.
    "presets/git/_git_common.py::_remotes_could_host_a_request":
        "NOT QUOTED, harmless - `git remote -v` prints URLs, not pathnames, so "
        "quoting was never in this. A URL carrying a separator could forge a "
        "row and flip 'every remote is a local path' to 'one names a host', "
        "silencing the #948 disclosure - but the author of that value is "
        "whoever ran `git remote add` in this clone, not the remote and not a "
        "contributor.",

    # -- checkout.py --------------------------------------------------------
    "presets/git/checkout.py::main":
        "QUOTED PATH - one site now. `status --porcelain=v1` is quoted and is "
        "counted into staged/unstaged/untracked, never rendered per line, so a "
        "forged row could only inflate a count - and a separator cannot reach "
        "it anyway. The `log -3` render beside it WAS the second site and is "
        "on `_untrusted.split_lines` + `visible()` since #1681.",

    # -- commit.py ----------------------------------------------------------
    "presets/git/commit.py::_with_coauthor":
        "NOT QUOTED, harmless - not git output at all: the commit message the "
        "CALLER typed, scanned for an existing Co-Authored-By trailer. A "
        "forged break suppresses the auto-trailer in a message its own author "
        "wrote - self-inflicted, and git's trailer parser would disagree about "
        "the line either way.",
    "presets/git/commit.py::main":
        "NOT QUOTED, harmless - `show --shortstat` last line. Not a path, so "
        "quoting does not apply; safe because it is git's own generated "
        "summary, which carries no pathname and no author-controlled text.",

    # -- diverge.py ---------------------------------------------------------
    "presets/git/diverge.py::main":
        "QUOTED PATH - one site now: `diff --name-status`, whose every field "
        "is a pathname and is octal-quoted before this split sees it. The log "
        "read beside it was the second site and is on "
        "`_untrusted.split_lines` + `visible()` since #1681 - a subject is not "
        "a pathname, and `len(shown)` is printed next to the rows.",

    # -- investigate.py -----------------------------------------------------
    # `main` was here and was this register's only `NOT QUOTED, open`. Its four
    # sites are all narrowed since #1693, so the file is out of the register
    # entirely: the log render and the two diff renders take `split_lines` +
    # `visible()` (#1681's shape), and the blame parse takes git's own LF,
    # which `split_lines` is too wide for. The old entry said the site was
    # "left alone because git-investigate gates nothing, writes nothing, and
    # the hotspot list is advisory" — true, and beside the point once the
    # question is who the reader believes wrote a line.

    # -- merge.py -----------------------------------------------------------
    # `_fresh_merge_ref` was here, on "the extraction kind", citing
    # `_first_error_line` - which #1475 had already narrowed. It read a fetch's
    # stderr, which a remote writes `remote:` lines onto, took the LAST line,
    # and put the result unflattened into a WARN the caller acts on. #1654
    # narrowed it.

    # -- push.py ------------------------------------------------------------
    "presets/git/push.py::_remote_names":
        "NOT QUOTED, harmless - `git remote` names are not pathnames. Git "
        "refuses to create a remote whose name carries an ASCII control "
        "character, U+2028 is not one, and a forged extra name only produces a "
        "lookup that misses.",
    "presets/git/push.py::_ref_line":
        "NOT QUOTED, harmless - git's per-ref PORCELAIN push status, three "
        "TAB-separated fields, and a refname is not quoted. A refname cannot "
        "carry a TAB - check-ref-format rejects every ASCII control character "
        "- so a forged fragment can never present three fields, is dropped by "
        "the `len(parts) != 3` test, and the caller gets the ('', '') that "
        "#641/#661 already render as 'git reported no line', never as a no.",
    "presets/git/push.py::_spawn_watch":
        "NOT QUOTED, harmless - not git output: the watch child's own first "
        "non-empty line, as a decline detail. The loss half of #1652 applies "
        "and the writer is this tool's own subprocess, so there is no stranger "
        "to choose the segment.",
    "presets/git/push.py::_uncommitted_leftovers":
        "QUOTED PATH - `status --porcelain`, printed as a 'you forgot to "
        "commit this' list rather than parsed. Quoting applies and is on; a "
        "forged row only adds to a warning that exists to be noticed.",
    # `_discarded_by_force` and `_incoming_commits` were here, both on the
    # "inflates a count, in the loud direction" ground - which is true and was
    # not enough (#1681). The inflated count IS the receipt at both: one is the
    # only statement `git-push` makes about what a force-push destroyed, the
    # other is `behind`. Both are on `_untrusted.split_lines` + `visible()`.
    "presets/git/push.py::_recover_by_rebase":
        "QUOTED PATH - `diff --name-only --diff-filter=U` after a failed "
        "rebase. A non-empty list selects the conservative arm - leave the "
        "rebase paused - so even a forged row cannot produce the destructive "
        "outcome.",

    # -- resolve.py ---------------------------------------------------------
    "presets/git/resolve.py::_union_lines":
        "NOT QUOTED, harmless - conflicted file CONTENT, which git never "
        "quotes, keyed on `<<<<<<<` at column 0 and computing the union the "
        "markdown heading guard reads. A forged separator grants nothing a "
        "plain newline does not: a contributor can already put a marker-shaped "
        "line at column 0. It must also stay byte-identical to `_union_file`'s "
        "parse, or the guard stops describing the union it guards - so "
        "narrowing one alone would be a new defect.",
    "presets/git/resolve.py::_union_file":
        "NOT QUOTED, harmless - the same state machine over the same unquoted "
        "content, writing the result back. Same reason, and `keepends=True` "
        "means a split with no forged marker after it reassembles "
        "byte-identically.",
    "presets/git/resolve.py::_resolve_blocks":
        "NOT QUOTED, harmless - the per-block variant of the same machine over "
        "the same content. Same reason; the `_scan_markers` hard gate refuses "
        "to stage whatever it produces.",
    "presets/git/resolve.py::_count_blocks":
        "NOT QUOTED, harmless - counts `<<<<<<<` lines in file content. "
        "Self-consistent with `_resolve_blocks`, which splits identically, and "
        "a real marker at a real column 0 survives any splitting - so this can "
        "over-count and never under-count.",
    "presets/git/resolve.py::_scan_markers":
        "NOT QUOTED, harmless - the hard gate before staging, over the same "
        "content. Over-reporting refuses a stage; under-reporting is what "
        "would be dangerous, and is impossible here for the reason above.",
    "presets/git/resolve.py::_union_attr_paths":
        "NOT QUOTED, harmless - `check-attr merge -- PATHS` prints the path "
        "back, and this matches on a `: merge: union` suffix. Fail-closed by "
        "construction: the result is SUBTRACTED from the candidate set, so a "
        "forged fragment is not a candidate and removes nothing. A split can "
        "only fail to whitelist, never wrongly whitelist.",
    "presets/git/resolve.py::_digest_block":
        "NOT QUOTED, harmless - one file's validator rows, written by this "
        "tool's own child. `_RESULT_ROW` and `_SKIPPED_ROW` both anchor at "
        "`^`, so a fragment can add a row but never destroy the head of a real "
        "one - a genuine `1 err` row keeps matching, and a forged `ok` cannot "
        "turn the digest from warn back into ok.",
    "presets/git/resolve.py::_child_failed":
        "NOT QUOTED, harmless - the validate child's stderr, first non-empty "
        "line, and the value is already `_untrusted.flat`-ed on the way out. "
        "The loss half of #1652 does apply: a U+2028 in that first line "
        "discards its tail. Kept because the writer is this tool's own "
        "validator over the caller's own files, and the digest is warn-only.",
    "presets/git/resolve.py::_validate_paths":
        "NOT QUOTED, harmless - splits the child's combined output on "
        "`validate: ` headers. A forged header can only ADD a block, and "
        "`len(blocks) != len(files)` then declines for the whole batch. #886 "
        "reached exactly this with a filename separated by U+2028, and that "
        "guard is why it was a denial rather than a second forged clean bill.",

    # -- status.py ----------------------------------------------------------
    "presets/git/status.py::main":
        "QUOTED PATH - one site now, of the five this entry used to cover: "
        "`status --porcelain=v1`. The `branch -vv` split was DEAD and is "
        "deleted; `for-each-ref`, the `log -5` render and `stash list` are on "
        "`_untrusted.split_lines` + `visible()` since #1681. The `for-each-ref` "
        "one is why the old entry was wrong rather than merely incomplete: it "
        "argued a fragment is dropped by the `'ahead' in track` test, which is "
        "true and means the SURVIVING row is rendered under a truncated "
        "refname no branch here has.",

    # -- trail.py -----------------------------------------------------------
    # `trail.py::main` was here with three sites, on the ground that its output
    # is a search render rather than a review gate. #1681 narrowed all three,
    # and one of them was never `forges` at all: the pickaxe render feeds
    # `c.split()[0]` back to `git show` as argv, and `git show --output=<file>`
    # writes that file - so a commit subject chose a path on the reader's disk.

    # -- worktrees.py -------------------------------------------------------
    "presets/git/worktrees.py::parse_worktree_list":
        "NOT QUOTED, harmless - `worktree list --porcelain` does not quote "
        "paths (that is what its `-z` form is for), so this is a path read "
        "with quoting genuinely absent. A worktree path carrying a separator "
        "forges an entry - but that path is the local user's own choice, the "
        "op is inspection-only, and a fabricated tree can only add a row to an "
        "inventory that never removes anything.",
    "presets/git/worktrees.py::_read_cwd_table_uncached":
        "NOT QUOTED, harmless - `lsof -F pn` records, not git output at all. A "
        "forged row makes a process appear to hold a tree, which pushes the "
        "verdict toward `occupied` - the conservative direction this op is "
        "built on, where `idle` has to be earned.",
    # `remote_branch_names` was here with two sites and both are gone (#1654).
    # Its stderr decline took the FIRST line of an unquoted, unmarked stream;
    # its `for-each-ref` read let ONE published ref become TWO records, which
    # is the pushed/unpushed forgery this entry itself registered as open and
    # nobody had closed. `check-ref-format` exits 0 on a refname whose middle
    # component ends in U+2028 followed by a second `refs/remotes/origin/...`.
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
    """Everything this tree has narrowed, named. Two audits, four readers."""
    sites = _call_sites()
    for gone, why in (
        ("presets/git/diff.py::_changed_files",
         "it runs with core.quotepath=false, so nothing upstream is quoting "
         "the separator for it (#1130)"),
        ("presets/git/diff.py::_scan_red_flags",
         "it runs with core.quotepath=false, so nothing upstream is quoting "
         "the separator for it (#1130)"),
        ("presets/git/merge.py::_fresh_merge_ref",
         "a fetch's stderr is written partly by the remote and is not a "
         "pathname, so quoting reaches none of it (#1654)"),
        ("presets/git/worktrees.py::remote_branch_names",
         "a refname is not a pathname and check-ref-format accepts U+2028, so "
         "one published ref could become two records (#1654)"),
        ("presets/git/push.py::_discarded_by_force",
         "the list length is the only statement git-push makes about what a "
         "force-push destroyed, and a discarded commit's own subject chose it "
         "(#1681)"),
        ("presets/git/push.py::_incoming_commits",
         "`behind` is the length of this list, and an incoming commit's "
         "subject chose it (#1681)"),
        ("presets/git/trail.py::main",
         "the pickaxe render hands `c.split()[0]` to `git show` as argv, so a "
         "forged line put an option there - `git show --output=<file>` writes "
         "that file (#1681)"),
        ("presets/git/investigate.py::main",
         "`blame --line-porcelain` interleaves porcelain headers with the "
         "blamed file's OWN lines, so a source line spelling "
         "`<U+2028>author X<U+2028><TAB>text` added a blame row carrying an "
         "author, a date and a line number no commit had. `split_lines` is NOT "
         "the fix and would not close it: `_git` runs "
         "`subprocess.run(text=True)`, which rewrites a lone CR into LF before "
         "any splitter sees it, so a bare CR in a source file forged the same "
         "row over plain ASCII. The reader is `_git_verbatim` plus a split on "
         "git's own LF, which a file line cannot contain by definition "
         "(#1693)"),
    ):
        assert gone not in sites, f"{gone} is on str.splitlines() again: {why}"


def test_every_register_entry_states_a_reason() -> None:
    for key, reason in REGISTER.items():
        assert len(reason) > 40, (key, reason)


def test_every_register_entry_names_the_ground_it_rests_on() -> None:
    """The partition #1654 asked for, as a gate rather than a paragraph.

    One deciding rule was published for this whole register and held for three
    of its entries. An entry that names no ground is one nobody partitioned.
    """
    unpartitioned = sorted(k for k, r in REGISTER.items()
                           if not r.startswith(GROUNDS))
    assert not unpartitioned, (
        "REGISTER entries that name no ground. Open the reason with one of "
        f"{GROUNDS}: {unpartitioned}"
    )


def test_the_published_partition_is_the_one_in_the_register() -> None:
    """Exact in both directions, so a reclassification is a visible line."""
    got = {g: sum(1 for r in REGISTER.values() if r.startswith(g))
           for g in GROUNDS}
    assert got == GROUND_TALLY, (
        "the partition moved. Write the new counts down in GROUND_TALLY, and "
        f"say in the docstring why: {GROUND_TALLY} -> {got}"
    )
    assert sum(got.values()) == len(REGISTER), "an entry counted twice"


def test_no_entry_rests_on_the_retired_reasoning() -> None:
    """#1652 retired 'splitlines consumes the separator, so it is safer'.

    It is half true: consuming the separator also discards what is on the
    other side of it, so the writer still chose which segment survived. Four
    entries here rested on it under the name 'the extraction kind'.
    """
    for key, reason in REGISTER.items():
        for phrase in RETIRED:
            assert phrase not in reason, (
                f"{key} rests on the argument #1652 retired ({phrase!r}). "
                "Consuming the separator discards the other half; say what "
                "the site is actually safe by, or narrow it."
            )
