"""#905 - the raw `./supertool` register, as a build gate instead of a paragraph.

The issue's own filed count was seven. Measured on this tree at the time this
register was written: 71 line matches across 22 files under `presets/`, and by
the time the four sites the issue actually named (`_branch_locale.py`,
`_repo_target.py`, `presets/gitlab/job.py`, `presets/github/check.py`) were
routed through `_st_hint.st_hint` -- the shared helper this issue adds, moved
out of `presets/git/_git_common.py::st_hint` because that one is reachable
only from `presets/git/` -- 63 remained. `presets/_st_hint.py` itself accounts
for 3 of those: two in its own docstring, one in the literal it is the single
place allowed to spell.

This register does not close the debt; it makes the debt visible and refuses
to let it grow silently. `./supertool` is correct in exactly one place a
reader might be standing -- a clone that made itself the wrapper by hand -- and
wrong in a linked worktree, which is where this repo's own CLAUDE.md says
agents work. A printed remedy hardcoding it is not a functional defect on its
own (#905's own words), which is why this register can carry `REMEDY-DEBT`
entries rather than fixing every one in the same PR that adds the gate: the
`test_printed_invocation_worktree_1012.py` / `test_st_hint_interpreter_1017.py`
pair already pins `_git_common.st_hint`'s exact behaviour and globals identity,
and widening every one of ~26 remaining call sites through it in one pass would
be a much larger diff than the four this issue names, touching modules no
other part of #905 needs to read.

Three grounds, one of which — `DEFINITION` — this register alone has, because
unlike the `str.splitlines()` registers this one is not classifying whether a
site is *safe*; every site here is the same defect wearing a different mask,
and the third state is "this occurrence is the implementation, not a mistake".

* ``DEFINITION`` - the function whose whole job is choosing between
  `./supertool` and the interpreter fallback. It must contain the literal.
  Two: `presets/_st_hint.py::st_hint` (the shared helper #905 adds) and
  `presets/git/_git_common.py::st_hint` (the git-only original, left exactly
  as `test_printed_invocation_worktree_1012.py` pins it — see that file's
  `assert conflicts.st_hint.__globals__ is vars(git_common)`, which a "just
  re-export it" refactor breaks).
* ``PROSE`` - a docstring or module-level example naming the invocation as
  history or illustration, never a string this code path prints. Seven.
* ``REMEDY-DEBT`` - an actual printed or returned runnable command, still
  hardcoded. Real debt, not fixed here; `test_no_new_remedy_debt_over_the_baseline`
  below is the only thing standing between this count and silent growth. 26.

Keyed on `path::enclosing function`, the same construction as
`tests/test_preset_git_splitlines_register_1130.py`, for the reason its own
docstring gives: a register keyed on line numbers goes stale on every
unrelated edit and teaches people to regenerate it without reading it.

`ast.Constant` rather than a `Call` visitor (contrast the splitlines
registers): the literal shows up as plain strings and as the literal segments
of f-strings alike, and `ast.Constant` sees both without needing to special-case
`JoinedStr`. It cannot see inside a `#` comment -- Python's `ast` module does
not parse comments at all -- so two occurrences are invisible to it on
purpose: `presets/git/conflicts.py`'s comment at its `st_hint`-routed print
site, and one comment in `presets/git/push.py::_watch_argv`. Both are
accounted for in `test_the_full_tree_census_has_not_moved` below, which reads
raw file text rather than parsing it, specifically so a comment or a non-`.py`
file (`presets/git.json`, `presets/watch/README.md`,
`presets/watch/watch-mine.sh`) cannot drift without failing something.
`watch-mine.sh` and `README.md` are out of `_st_hint`'s reach by construction
(one is shell, the other is prose); `README.md` is also held by `fix/1798`
per this issue's own brief and is reported rather than edited.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parent.parent
TREE = "presets"

GROUNDS = ("DEFINITION - ", "PROSE - ", "REMEDY-DEBT - ")

GROUND_TALLY = {"DEFINITION - ": 2, "PROSE - ": 9, "REMEDY-DEBT - ": 26}

#: The ratchet ceiling for `test_no_new_remedy_debt_over_the_baseline`,
#: deliberately a SEPARATE literal from `GROUND_TALLY["REMEDY-DEBT - "]`
#: rather than read from it. Comparing the tally against itself is
#: tautological -- it would pass no matter how large REMEDY-DEBT grew, as
#: long as whoever added the new site also bumped GROUND_TALLY, which
#: `test_every_hardcoded_invocation_in_presets_is_registered` already forces
#: them to do. This ceiling is the one number in this file a new REMEDY-DEBT
#: entry does NOT get to move for free.
_REMEDY_DEBT_CEILING = 26

REGISTER: dict[str, str] = {
    # -- the implementation itself -------------------------------------------
    "presets/_st_hint.py::st_hint":
        "DEFINITION - the shared helper #905 adds. `./supertool ` is the "
        "quoted-wrapper branch's own return value, reached only when the "
        "wrapper exists and is executable.",
    "presets/git/_git_common.py::st_hint":
        "DEFINITION - the original, git-only helper. Left duplicated rather "
        "than turned into a re-export of `_st_hint.st_hint`: "
        "`test_printed_invocation_worktree_1012.py` asserts "
        "`conflicts.st_hint.__globals__ is vars(git_common)`, and several "
        "tests in that file and in `test_st_hint_interpreter_1017.py` "
        "monkeypatch `git_common.install_dir` and read the effect through "
        "`git_common.st_hint` -- both rely on the function's globals being "
        "`_git_common`'s own module dict, which a re-export from a different "
        "module would break.",

    # -- prose: history and illustration, never printed ----------------------
    "presets/_st_hint.py::_wrapper_is_runnable":
        "PROSE - the Windows-probe helper's own docstring names "
        "`./supertool` twice to explain what it decides is runnable and "
        "what the probe cannot establish (#1919); the function returns a "
        "bool, it prints nothing.",
    "presets/git/_git_common.py::_wrapper_is_runnable":
        "PROSE - the duplicate of the helper above, same reason: its "
        "docstring names `./supertool` to describe the probe, and returns a "
        "bool rather than printing anything (#1919).",
    "presets/_branch_locale.py::<module>":
        "PROSE - the module docstring's own example of the line five ops "
        "used to build by hand, kept as the before-picture #850 fixed.",
    "presets/_branch_locale.py::describe":
        "PROSE - `describe()`'s docstring names `./supertool` only to explain "
        "why THIS function renders no imperative at all (#905 is cited by "
        "number in the docstring itself); the function it describes prints "
        "no command, hostile or otherwise.",
    "presets/_refname.py::<module>":
        "PROSE - the module docstring recounts the #924 injection this "
        "module's own charset check now refuses, quoting the exact string "
        "that closed a shell command's quote early.",
    "presets/_st_hint.py::<module>":
        "PROSE - this module's own docstring, explaining what it replaces "
        "and why (twice: the general #1012 problem statement, and the #905 "
        "note about which callers used to hand-build the literal).",
    "presets/github/find_starable.py::<module>":
        "PROSE - a usage tip in the module docstring showing three chained "
        "ops. Illustrative only; nothing in the module renders this text.",
    "presets/git/push.py::_st_hint":
        "PROSE - `_st_hint`'s own docstring, which explains it now delegates "
        "to `_git_common.st_hint` (#1012) and mentions the wrapper only to "
        "say why the old inline version was wrong.",
    "presets/git/push.py::_watch_argv":
        "PROSE - the docstring explains the #642 history; the one runtime "
        "string (`no runnable supertool at {root} (neither ./supertool nor "
        "supertool.py)`) NAMES both files it checked and found absent -- a "
        "description of what was tried, not an imperative to run either.",

    # -- remedy debt: an actual printed command, still hardcoded (#905) ------
    "presets/git/commit.py::_all_ambiguous_refusal":
        "REMEDY-DEBT - the `--all` opt-in remedy line.",
    "presets/git/commit.py::_all_with_paths_refusal":
        "REMEDY-DEBT - two remedy lines (everything dirty / only what you "
        "name), both hand-built.",
    "presets/git/commit.py::_amend_refusal":
        "REMEDY-DEBT - the literal-subject override remedy.",
    "presets/git/commit.py::_colon_remedy":
        "REMEDY-DEBT - the colon-form commit remedy builder.",
    "presets/git/commit.py::_colon_split_refusal":
        "REMEDY-DEBT - the `git-commit:@-` heredoc remedy.",
    "presets/git/commit.py::_colon_split_refusal._colon_form":
        "REMEDY-DEBT - the nested colon-form helper's own remedy line.",
    "presets/git/commit.py::_nothing_staged_lines":
        "REMEDY-DEBT - two remedy lines for the nothing-staged refusal.",
    "presets/git/commit.py::_payload_fields_refusal":
        "REMEDY-DEBT - the payload-fields heredoc remedy.",
    "presets/git/commit.py::_payload_remedy":
        "REMEDY-DEBT - the generic payload remedy builder.",
    "presets/git/commit.py::main":
        "REMEDY-DEBT - the post-commit `Next:` line, two invocations on one "
        "line (git-push and mr).",
    "presets/git/diverge.py::main":
        "REMEDY-DEBT - the `Next:` merge suggestion.",
    "presets/git/merge.py::main":
        "REMEDY-DEBT - three post-merge `Next:` lines (conflicts, commit, "
        "resolve).",
    "presets/git/resolve.py::_print_refusal_help":
        "REMEDY-DEBT - the refusal-help two-line remedy.",
    "presets/git/resolve.py::_resolve_partial":
        "REMEDY-DEBT - two `Next:`/`Inspect:` lines after a partial resolve.",
    "presets/git/resolve.py::main":
        "REMEDY-DEBT - three remedy lines across the resolve outcomes "
        "(inspect, merge-continue, plain commit).",
    "presets/github/find_followable.py::main":
        "REMEDY-DEBT - the batch-follow next-step comment line.",
    "presets/github/find_starable.py::main":
        "REMEDY-DEBT - the batch-star next-step comment line.",
    "presets/github/job.py::_absent_job_message":
        "REMEDY-DEBT - three cross-namespace remedy fragments (gh-check by "
        "id, by PR, and the retry-other-namespace line). The GitHub twin of "
        "`presets/github/check.py`'s equivalent function, which #905 DID "
        "route through `_st_hint` -- this one did not, because the issue "
        "named `check.py`, not `job.py`.",
    "presets/github/job.py::_missing_log_message":
        "REMEDY-DEBT - the retry-once-it-finishes remedy.",
    "presets/github/job.py::_print_unmatched_failure":
        "REMEDY-DEBT - the `Next:` raw/grep pair after an unmatched failure.",
    "presets/github/job.py::_selection_mismatch":
        "REMEDY-DEBT - the raw-tail / grep pair on a status mismatch.",
    "presets/github/job.py::main":
        "REMEDY-DEBT - the per-pattern `Resolve:` line -- the GitHub twin of "
        "`presets/gitlab/job.py`'s equivalent, which #905 DID fix.",
    "presets/github/pr.py::_leg_unit_line":
        "REMEDY-DEBT - the leg-vs-test-count explainer's `gh-job:ID` pointer.",
    "presets/gitlab/api.py::path_refusal":
        "REMEDY-DEBT - the empty-path refusal's `help:gl-api` pointer.",
    "presets/watch/dispatcher.py::cmd_watch":
        "REMEDY-DEBT - the already-watching refusal's `unwatch:` pointer.",
    "presets/watch/tiers/gl_mrs.py::loss_warnings":
        "REMEDY-DEBT - the lost-poller radar warning's re-arm pointer.",
}


class _Visitor(ast.NodeVisitor):
    """Collect string constants naming `./supertool`, keyed by enclosing def."""

    def __init__(self, rel: str, found: dict[str, list[int]]) -> None:
        self._rel = rel
        self._found = found
        self._stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "./supertool" in node.value:
            key = f"{self._rel}::{'.'.join(self._stack) or '<module>'}"
            self._found.setdefault(key, []).append(node.lineno)
        self.generic_visit(node)


def _call_sites() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted((REPO / TREE).rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"))
        _Visitor(rel, found).visit(module)
    return found


def test_every_hardcoded_invocation_in_presets_is_registered() -> None:
    sites = _call_sites()
    unregistered = {k: v for k, v in sites.items() if k not in REGISTER}
    assert not unregistered, (
        "New hardcoded './supertool' in presets/. Route it through "
        "_st_hint.st_hint() (preferred) or add it to REGISTER as PROSE / "
        "REMEDY-DEBT / DEFINITION with a reason: "
        f"{unregistered}"
    )


def test_the_register_names_no_site_that_is_gone() -> None:
    sites = _call_sites()
    stale = sorted(k for k in REGISTER if k not in sites)
    assert not stale, (
        "REGISTER entries with no call site left -- either the fix landed "
        f"(good, delete the entry and lower GROUND_TALLY) or the register is "
        f"stale: {stale}"
    )


def test_every_register_entry_names_the_ground_it_rests_on() -> None:
    unpartitioned = sorted(k for k, r in REGISTER.items()
                            if not r.startswith(GROUNDS))
    assert not unpartitioned, (
        f"REGISTER entries that name no ground. Open the reason with one of "
        f"{GROUNDS}: {unpartitioned}"
    )


def test_every_register_entry_states_a_reason() -> None:
    for key, reason in REGISTER.items():
        assert len(reason) > 40, (key, reason)


def test_the_published_partition_is_the_one_in_the_register() -> None:
    got = {g: sum(1 for r in REGISTER.values() if r.startswith(g))
           for g in GROUNDS}
    assert got == GROUND_TALLY, (
        "the partition moved. If a REMEDY-DEBT site was just routed through "
        "_st_hint.st_hint(), delete its entry (it no longer matches this "
        "register at all) rather than reclassifying it, and update "
        f"GROUND_TALLY: {GROUND_TALLY} -> {got}"
    )
    assert sum(got.values()) == len(REGISTER), "an entry counted twice"


def test_no_new_remedy_debt_over_the_baseline() -> None:
    """The one-way ratchet: this count may only go down.

    `test_every_hardcoded_invocation_in_presets_is_registered` catches an
    UNREGISTERED new site. It would NOT catch a new site added already
    wearing a `REMEDY-DEBT` label -- that passes every check above and still
    grows the exact debt #905 exists to stop growing. This is the test that
    would fail on that pull request.
    """
    debt = sum(1 for r in REGISTER.values() if r.startswith("REMEDY-DEBT - "))
    assert debt <= _REMEDY_DEBT_CEILING, (
        f"REMEDY-DEBT grew from {_REMEDY_DEBT_CEILING} to {debt}. A new "
        "printed remedy was added hardcoding './supertool' instead of "
        "calling _st_hint.st_hint() -- route it through the helper. If this "
        "is a legitimate reduction of debt elsewhere that still nets out "
        "above the ceiling, lower _REMEDY_DEBT_CEILING deliberately, in its "
        "own line of the diff."
    )


# ---------------------------------------------------------------------------
# The whole-tree census: catches what AST cannot see (comments, non-.py)
# ---------------------------------------------------------------------------

#: Every file under `presets/` naming `./supertool`, and how many times —
#: including comments and non-Python files, which the register above cannot
#: see at all. Measured with plain substring counting, the same measure a
#: reader doing `grep -c` would get, so this cannot be fooled by a literal
#: moved from a string into a comment or vice versa.
FILE_CENSUS: dict[str, int] = {
    "presets/_branch_locale.py": 2,
    "presets/_refname.py": 1,
    "presets/_st_hint.py": 5,
    "presets/git.json": 1,
    "presets/git/_git_common.py": 4,
    "presets/git/commit.py": 13,
    "presets/git/conflicts.py": 1,  # a comment — invisible to the AST register
    "presets/git/diverge.py": 1,
    "presets/git/merge.py": 3,
    "presets/git/push.py": 4,  # 3 registered constants + 1 comment
    "presets/git/resolve.py": 7,
    "presets/github/find_followable.py": 1,
    "presets/github/find_starable.py": 2,
    "presets/github/job.py": 8,
    "presets/github/pr.py": 1,
    "presets/gitlab/api.py": 1,
    "presets/watch/README.md": 10,  # prose; held by fix/1798, report don't edit
    "presets/watch/dispatcher.py": 1,
    "presets/watch/tiers/gl_mrs.py": 1,
    "presets/watch/watch-mine.sh": 1,  # shell: `${SUPERTOOL:-./supertool}`
}


def _whole_tree_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(REPO.joinpath(TREE).rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = text.count("./supertool")
        if n:
            counts[path.relative_to(REPO).as_posix()] = n
    return counts


def test_the_full_tree_census_has_not_moved() -> None:
    """Every occurrence anywhere under `presets/`, not only what AST can see.

    A comment, a JSON description field, a README code block, a shell
    default — none of these are `ast.Constant` nodes, so none of them can
    ever be caught by the register above. This is the net under that net:
    exact per file, in both directions, so ANY drift — a new site in a
    comment, a count that quietly grew inside an existing file, one that
    disappeared because it was fixed — is a diff to this dict, not a
    silent pass.
    """
    got = _whole_tree_counts()
    assert got == FILE_CENSUS, (
        "the whole-tree './supertool' census moved. If something was fixed, "
        "lower its count (or remove the entry) here. If something new "
        "appeared, decide whether it needs _st_hint.st_hint() before raising "
        f"the number: {FILE_CENSUS} -> {got}"
    )
