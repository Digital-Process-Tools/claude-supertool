"""#1877 -- five files state a render's size as a present-tense fact, unpinned.

Handed back by the #1783 items 4-6 lane (PR #1876), which fixed the same class
inside `docs/operations/meta.md` and pinned three figures there. The sibling
sites were outside its file set, so all of them were stale: `ops:full` stated at
74,838 when it renders 72,7xx; `ops:roster` at ~1.7KB and ~1.4KB when it renders
~2.0KB; and two figures (`ops` 47,254, `ops-compact` 9,067) still describing the
tree as it was before #1774 moved the descriptions out of `ops` into `ops:full`.

**Why this is a registry and not a walker.** The issue asks for "a single test
that walks every file stating a ~N.NKB or bare byte count for a named render".
Measured before writing this: a deliberately narrow regex -- a render name in
backticks, then a size within 80 characters on the same sentence -- finds 40
hits in 11 files across this tree, and they are at least five kinds that no
regex separates:

    a live claim          README.md  "`ops:full` is 74,838 bytes here"
    a historical record   CHANGELOG.md, changelog.d/1783.fixed.md
    a past-tense sentence meta.md    "`ops` was before #1774, ~73KB"
    a cap, not a render   contributing.md "the ~7KB SessionStart cap"
    pinned somewhere else meta.md, by test_meta_doc_figures_1783.py

A dated measurement is a record and stays; an undated present-tense one is a
claim and gets graded. Nothing in the text marks which is which, so a walker
would need an exemption list about as long as the site list, and would redden
on docs edits that have nothing to do with any render. A pin that goes red on
an unrelated docs edit is worse than the stale number it replaces.

So membership is explicit. `SITES` below is the list of graded claims; a record
is simply absent from it. `test_no_ungraded_figure_in_a_registered_file` then
closes the half a walker was wanted for -- a *new* figure written into a file
already in the registry -- without ever opening a file that is not.

**Tolerance, and why the figures are KB.** Every one of these renders carries
`_preset_disclosure()`, which names the *absolute path* of the config it read,
so each render's byte count is a function of where the checkout sits on disk.
PR #1876 measured the same tree at two paths and got exactly the path-length
difference in every render (six bytes for a six-character difference), and
`tests/test_ops_roster_1231.py` states as policy that the roster is
"deliberately *not* pinned to a literal in prose" for that reason. Re-measured
here 2026-08-21 at the 40-character st-wt/1877 path, against the 46-character
clone path:

    ops         3,721 / 3,727      ops:full     72,709 / 72,715
    ops-compact 14,702 / 14,708    ops:roster    1,963 /  1,969

TOLERANCE_KB = 0.1 is 100 bytes, more than an order above that variance and two
orders below the 2,129-byte drift that produced this issue. An exact byte count
cannot be pinned at all, which is why the prose this test grades was reworded
into the KB form these files already used elsewhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest

import supertool

REPO_ROOT = Path(__file__).parent.parent

#: 100 bytes. See the module docstring: the measured path variance is 6 bytes.
TOLERANCE_KB = 0.1

#: Every render whose size these files talk about, and how to produce it.
RENDERS: dict = {
    "ops": lambda: supertool.op_ops(),
    "ops:full": lambda: supertool.op_ops(full=True),
    "ops-compact": lambda: supertool.op_ops(compact=True),
    "ops:roster": lambda: supertool.op_ops_roster(),
}


def render_pattern(name: str) -> str:
    """The size a sentence attributes to a backticked render name.

    The span between the name and the figure excludes dots and newlines, which
    keeps the match inside one sentence, and it is non-greedy, so the figure
    harvested is the nearest one after the name rather than any later number on
    the line. An optional quote inside the backticks is accepted because
    README.md writes some of these names that way.

    This is the idiom `tests/test_meta_doc_figures_1783.py` uses for the roster.
    It is parameterised here rather than copied.
    """
    return r"`'?" + re.escape(name) + r"'?`[^.\n]*?~([\d.]+)KB"


def found_values(text: str, name: str) -> list:
    """Every KB figure `text` attributes to render `name`, in order."""
    return re.findall(render_pattern(name), text)


def _hook_payload() -> str:
    """What `hooks/session-start.sh` prints, by the ops it prints it with."""
    return "".join(supertool.dispatch(op) for op in
                   ("introduction", "output-format", "ops:roster"))


@dataclass(frozen=True)
class Claim:
    """One documented size, and the render that settles it."""

    path: str
    #: What the claim is about. For a render this is its op name, which also
    #: builds the pattern. `pattern` overrides that, for a claim about
    #: something carrying no backticked name in the prose.
    subject: str
    render: Callable[[], str]
    pattern: Optional[str] = None

    @property
    def regex(self) -> str:
        if self.pattern is not None:
            return self.pattern
        return render_pattern(self.subject)

    def stated(self, text: str) -> list:
        return re.findall(self.regex, text)

    def actual_kb(self) -> float:
        return len(self.render().encode("utf-8")) / 1000


def _r(name: str) -> Callable[[], str]:
    return RENDERS[name]


SITES = (
    Claim("README.md", "ops:full", _r("ops:full")),
    Claim("README.md", "ops-compact", _r("ops-compact")),
    Claim("README.md", "ops:roster", _r("ops:roster")),
    Claim("README.md", "ops", _r("ops")),
    Claim("docs/operations/index.md", "ops:roster", _r("ops:roster")),
    Claim("docs/operations/index.md", "ops-compact", _r("ops-compact")),
    Claim("docs/operations/index.md", "ops:full", _r("ops:full")),
    Claim("docs/contributing.md", "ops:full", _r("ops:full")),
    Claim("docs/contributing.md", "ops", _r("ops")),
    Claim("hooks/session-start.sh", "ops:full", _r("ops:full")),
    Claim("hooks/session-start.sh", "ops:roster", _r("ops:roster")),
    Claim("hooks/session-start.sh", "ops-compact", _r("ops-compact")),
    Claim("hooks/session-start.sh", "ops", _r("ops")),
    Claim(
        "hooks/session-start.sh",
        "the whole hook payload",
        _hook_payload,
        pattern=r"Whole hook: ~([\d.]+)KB",
    ),
)

#: Files this registry grades. Anything absent is never opened, which is how a
#: record stays a record -- see the module docstring.
REGISTERED_FILES = tuple(dict.fromkeys(c.path for c in SITES))

#: Figures the harvester reaches that are not a render's size, named one by one
#: with the reason. `render_pattern` takes the nearest figure after a render
#: name in the same sentence, and a sentence can mention a render while stating
#: something else's size -- the SessionStart cap, in the only case in the tree.
#:
#: Explicit, not a heuristic. Every distance or wording rule tried here either
#: excluded a real claim or admitted this one: the closest genuine claim sits 58
#: characters from its render name and this false positive sits 68, so nothing
#: separates them by shape. An entry carries the exact value, so a reworded or
#: renumbered sentence stops matching and the guard fires again -- which is the
#: safe direction, and is why this is a list of three-tuples rather than a set
#: of files to skip.
NOT_A_RENDER_SIZE = (
    ("docs/contributing.md", "ops:roster", "7"),
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _grade(claim: Claim, text: str) -> None:
    """Three states, which is why this is not a boolean.

    A row whose pattern finds nothing is a **failure**, not a pass. That is the
    defect this repo keeps having -- a checker that cannot look returning the
    shape of a clean result. A reworded sentence the pattern no longer reaches
    has to arrive as "this test stopped being able to see the claim", because
    the alternative is a figure going stale again behind a green tick.
    """
    stated = claim.stated(text)
    assert stated, (
        "%s states no size for %s that this test can see (pattern: %r). "
        "Either the claim was deleted -- then delete this row -- or it was "
        "reworded and this row must follow it. Not looking is not the same as "
        "finding nothing." % (claim.path, claim.subject, claim.regex))
    values = sorted(set(stated))
    assert len(values) == 1, (
        "%s states %s for %s -- two figures for one render is #1783 item 5 "
        "again. Which of them this test would grade against is arbitrary, so "
        "it grades neither." % (claim.path, values, claim.subject))
    actual = claim.actual_kb()
    stated_kb = float(values[0])
    assert abs(stated_kb - actual) < TOLERANCE_KB, (
        "%s says %s is ~%sKB; this checkout renders %.2fKB (%s bytes)."
        % (claim.path, claim.subject, stated_kb, actual,
           format(round(actual * 1000), ",")))


@pytest.mark.parametrize(
    "claim", SITES, ids=["%s:%s" % (c.path, c.subject) for c in SITES])
def test_every_documented_render_size_matches_the_render(
        claim, shipped_config) -> None:
    _grade(claim, _read(claim.path))


def test_a_wrong_figure_is_caught(shipped_config) -> None:
    """Positive control. "No site is out of tolerance" also passes when the
    grader is broken, so feed it a figure that is wrong on purpose.

    Wrong by 1.0KB -- ten times the tolerance, so it cannot pass by rounding.
    """
    claim = Claim("unused", "ops:roster", _r("ops:roster"))
    truth = claim.actual_kb()
    with pytest.raises(AssertionError, match="renders"):
        _grade(claim, "`ops:roster` is ~%.1fKB here." % (truth + 1.0))
    # ...and the same grader accepts the honest sentence, so the raise above is
    # about the number rather than the harness refusing every input it is given.
    _grade(claim, "`ops:roster` is ~%.1fKB here." % truth)


def test_a_claim_the_pattern_cannot_find_fails_rather_than_passes(
        shipped_config) -> None:
    """The third state, asserted rather than assumed.

    Without this, the first assertion in `_grade` is the one line in the file
    that nothing exercises -- and it is the line deciding whether a reworded
    sentence gets reported or silently dropped.
    """
    claim = Claim("unused", "ops:roster", _r("ops:roster"))
    with pytest.raises(AssertionError, match="Not looking is not the same"):
        _grade(claim, "the roster is small enough to fit the cap.")


def test_two_disagreeing_figures_are_refused(shipped_config) -> None:
    """#1783 item 5 was a file stating two sizes for one render four lines
    apart. Grading one of them at random would pass or fail run to run."""
    claim = Claim("unused", "ops:roster", _r("ops:roster"))
    truth = claim.actual_kb()
    text = ("`ops:roster` is ~%.1fKB here.\n`ops:roster` is ~%.1fKB there."
            % (truth, truth + 1.0))
    with pytest.raises(AssertionError, match="two figures for one render"):
        _grade(claim, text)


#: Sites carrying this defect that this registry deliberately does not grade,
#: each with the reason. They are listed rather than omitted because a file
#: absent from `REGISTERED_FILES` is indistinguishable from a file with nothing
#: in it -- and these have something in them.
UNCOVERED = (
    ("_supertool.py:2575", "`ops` 47,254 and `ops-compact` 9,067, both pre-#1774",
     "held by another lane when #1877 was implemented; the file was out of bounds"),
    ("_supertool.py:17101", "`ops:full` 74,838",
     "held by another lane when #1877 was implemented; the file was out of bounds"),
    ("tests/test_ops_roster_1231.py:3,181,292",
     "`ops` 47,254, `ops-compact` 9,067, `ops` ~47KB -- an undated docstring",
     "tests/ was held by another lane when #1877 was implemented"),
)


@pytest.mark.parametrize("site, states, why", UNCOVERED,
                         ids=[u[0] for u in UNCOVERED])
def test_known_uncovered_site(site, states, why) -> None:
    """Not a pass and not a failure -- a skip that names what went ungraded.

    The registry grades what it opens, so a site in a file it never opens is
    invisible: `test_no_ungraded_figure_in_a_registered_file` is green about
    these three because it is not looking at them. That is the exact shape this
    repo keeps filing -- an absence the tool produced, read as an absence in the
    world -- so the gap is a line in the report rather than a tick.

    Adding the file to `SITES` when its lane lands is one row and deletes the
    matching entry here.
    """
    pytest.skip("%s states %s -- ungraded: %s" % (site, states, why))


def test_every_exemption_still_exempts_something(shipped_config) -> None:
    """An exemption nobody can reach is indistinguishable from one that works.

    `NOT_A_RENDER_SIZE` suppresses a finding, so a stale entry silently widens
    the guard's blind spot by exactly one figure. If the sentence it names was
    reworded or deleted, the entry is dead and must go -- and the only moment
    anyone would notice is this assertion.
    """
    dead = []
    for path, name, value in NOT_A_RENDER_SIZE:
        if value not in found_values(_read(path), name):
            dead.append("%s no longer states ~%sKB for `%s`" % (path, value, name))
    assert not dead, (
        "an exemption in NOT_A_RENDER_SIZE matches nothing:\n  "
        + "\n  ".join(dead)
        + "\nDelete the entry. Keeping it suppresses a figure nobody has read.")


def test_no_ungraded_figure_in_a_registered_file(shipped_config) -> None:
    """The achievable half of the walker the issue asked for.

    Tree-wide discovery is what the module docstring rejects. This is the other
    direction, and it cannot false-fire: for the files already in the registry,
    every render name carrying a KB figure must be graded by a row above. An
    eleventh instance written into README.md is a red test naming it; a figure
    written into CHANGELOG.md is invisible here, because CHANGELOG.md is never
    opened.
    """
    graded = set((c.path, c.subject) for c in SITES)
    ungraded = []
    for path in REGISTERED_FILES:
        text = _read(path)
        for name in RENDERS:
            if (path, name) in graded:
                continue
            for value in found_values(text, name):
                if (path, name, value) in NOT_A_RENDER_SIZE:
                    continue
                ungraded.append("%s states ~%sKB for `%s`" % (path, value, name))
    assert not ungraded, (
        "a size claim in a registered file that no row grades:\n  "
        + "\n  ".join(ungraded)
        + "\nAdd a Claim row for it, or move the sentence into a file this "
          "registry does not open, if it is a record rather than a claim.")
