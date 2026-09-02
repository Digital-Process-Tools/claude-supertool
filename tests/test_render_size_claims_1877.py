"""#1877 -- five files state a render's size as a present-tense fact, unpinned.

Handed back by the #1783 items 4-6 lane (PR #1876), which fixed the same class
inside `docs/operations/meta.md` and pinned three figures there. The sibling
sites were outside its file set, so all of them were stale: `ops:full` stated at
74,838 when it renders 72,7xx; `ops:roster` at ~1.7KB and ~1.4KB when it renders
~2.0KB; and two figures (`ops` 47,254, `ops-compact` 9,067) still describing the
tree as it was before #1774 moved the descriptions out of `ops` into `ops:full`.

**Why this is a registry and not a walker.** The issue asks for "a single test
that walks every file stating a ~N.NKB or bare byte count for a named render".
Measured before writing this, with a survey probe rather than the pattern below
-- a render name in backticks, then a size within 80 characters of it on the
same sentence. It finds 40 hits in 11 files across this tree, and they are at
least five kinds that no regex separates:

    a live claim          README.md  "`ops:full` is 74,838 bytes here"
    a historical record   CHANGELOG.md, and the changelog.d/ fragments
    a past-tense sentence meta.md    "`ops` was before #1774, ~73KB"
    a cap, not a render   contributing.md "the ~7KB SessionStart cap"
    pinned somewhere else meta.md, by test_meta_doc_figures_1783.py

That second row names the *directory* rather than the fragment file this survey
actually read, and the reason is this file's own subject matter arriving one
level up. The draft named #1783's own pending fragment by filename, which was
true when it was written and stops being true on a scheduled future event: the
tag that ships #1783 folds that fragment into `CHANGELOG.md` and deletes it, so
the citation is green in this pull request and red on the release and every
release after. Naming it in this paragraph would have had the same fault, so
this paragraph does not either: `tests/test_changelog_findable_1293.py` refuses
the filename even in prose explaining the filename, and it is right to.
It caught this one, and
its refusal records that the same mistake cost five CI legs on v0.26.0 and
thirteen of twenty on v0.27.0, none of them visible from inside the pull request
that wrote them. A citation whose target is scheduled for deletion is exactly the
"correct today, wrong later, and nothing re-reads it" shape every figure in
`SITES` is about; it just rots on a release rather than on an edit.

No count of those rows appears in this docstring, deliberately. The first draft
of the paragraph above said "the fourteen figures below" while `SITES` held
fifteen -- a receipt about the change stating a number the change contradicts,
written into the file whose whole subject is that failure. `SITES` is in this
file and `len(SITES)` cannot disagree with it, so the prose points at the list
instead of counting it.

A dated measurement is a record and stays; an undated present-tense one is a
claim and gets graded. Nothing in the text marks which is which, so a walker
would need an exemption list about as long as the site list, and would redden
on docs edits that have nothing to do with any render. A pin that goes red on
an unrelated docs edit is worse than the stale number it replaces.

So membership is explicit. `SITES` below is the list of graded claims; a record
is simply absent from it. `test_no_ungraded_figure_in_a_registered_file` then
closes the half a walker was wanted for -- a *new* figure written into a file
already in the registry -- without ever opening a file that is not.

**The checkout path is normalised away before grading, not tolerated.** Every one of
these renders carries `_preset_disclosure()`, which names the *absolute path*
of the config it read, so each render's raw byte count is partly a function of
where the checkout sits on disk. `tests/test_ops_roster_1231.py` states as
policy that the roster is "deliberately *not* pinned to a literal in prose" for
that reason.

This file's first version absorbed that into the tolerance, on a sample of one:
PR #1876 had measured the same tree at two paths six characters apart and got a
six-byte difference, and 100 bytes looked like ample headroom over six. The
sample was six; the range is not bounded by anything. Measured 2026-08-21 at
c6dd83ae, one tree at two paths:

              43 chars    130 chars
    ops          3,724       3,811
    ops:full    72,712      72,799

87 characters of path, 87 bytes of render, 87% of the tolerance -- and the
failure it produced was not read as a path at all. A clone taken under a
128-character scratchpad path failed four rows and was briefed as a composition
defect between this pin and PR #1890; #1890 in fact leaves all four renders
byte-identical, and CI never saw any of it, because a runner checks out at
about 50 characters. A guard whose red says "this checkout renders 3,810 bytes"
while meaning "your directory is deep" costs more than the staleness it was
built to catch.

So `graded_bytes` substitutes one fixed path for whatever this checkout is,
and `TOLERANCE_KB = 0.1` goes back to meaning only what #1884 argued for: room
for prose to round. Tolerating the path instead would have been worse than it
looks in both directions -- a deep checkout false-fires, and a shallow one
*hides* real drift byte for byte, since a figure measured 90 characters deeper
than the reader's checkout eats 90 bytes of the same budget. 100 bytes is still
two orders below the 2,129-byte drift that produced this issue, which is the
class the registry exists for.

Substituted rather than deleted, and that is not cosmetic: stripping the path
grades a render nobody receives. `ops:roster` is 1.907KB stripped, which rounds
to ~1.9 -- so the honest consequence of deleting would have been to correct
three files away from ~2.0KB, the figure a reader at any real checkout actually
sees. The fix would have made the prose less true in order to make the grader
more convenient. `CANONICAL_CONFIG_PATH` keeps the graded number inside the
sentence being graded.

An exact byte count cannot be pinned even so -- prose rounds -- which is why
the sentences this test grades were reworded into the KB form these files
already used elsewhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest

import supertool

REPO_ROOT = Path(__file__).parent.parent

#: 100 bytes of room for the prose to round. The checkout path is not in here:
#: `graded_bytes` normalises it away first. See the module docstring for why
#: absorbing it into this number cost a false red and a wrong diagnosis.
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


#: The config path every render is graded as if it had been produced under.
#:
#: A *stand-in*, not a deletion, and the difference decides four of the five
#: figures in `SITES`. Deleting the path grades a render nobody ever receives:
#: `ops:roster` comes to 1.907KB stripped, which rounds to ~1.9 and would have
#: had three files corrected away from the ~2.0KB that is what a reader at any
#: real checkout actually gets. Substituting a fixed path of realistic length
#: keeps the graded number inside the sentence a reader is reading.
#:
#: This literal is CI's own checkout path, so the graded figure is the one the
#: Linux leg renders. Any fixed string of similar length would serve -- it
#: shifts all five figures together and none of them relative to each other --
#: and it is pinned here rather than derived so that no environment can move
#: it. Measured 2026-08-21 with this value: the tightest figure in `SITES`
#: sits 68 bytes inside `TOLERANCE_KB` and the loosest 89.
CANONICAL_CONFIG_PATH = "/home/runner/work/claude-supertool/claude-supertool/.supertool.json"


def graded_bytes(text: str) -> int:
    """*text*'s size, as if this checkout sat at `CANONICAL_CONFIG_PATH`.

    Every render here carries `_preset_disclosure()`, which names the absolute
    path of the config it read, so a raw byte count is partly a measurement of
    the developer's directory layout. Substituting one fixed path for whatever
    this checkout happens to be leaves a number that is the same in every
    checkout and on every platform, which is the only kind of number a
    documented figure can be graded against.

    Normalised rather than tolerated. A tolerance wide enough to absorb an
    arbitrary path is also wide enough to hide that many bytes of the drift the
    registry exists to catch, and the two errors cancel: a checkout 90
    characters deeper than the one a figure was measured at masks 90 bytes of
    genuine staleness. Taking the variable out instead leaves `TOLERANCE_KB`
    free to mean only what #1884 argued it meant -- room for prose to round.

    Two ways this can fail to normalise, and neither may return a number.

    The first is *no config path at all*. `_preset_disclosure` falls back to
    naming `os.getcwd()` when `_CONFIG_PATH` is falsy, so the render still
    carries a checkout-dependent absolute path -- and the one string that would
    have located it is gone. Returning `len(text)` there is the raw count
    wearing the shape of a normalised one, which is this repository's own
    defect class sitting inside the guard against it, and the failure it
    produces is the same sentence ("this checkout renders N bytes") that sent a
    maintainer after the wrong pull request in the first place. So it refuses.
    Every row reaches this through the `shipped_config` fixture, which always
    sets the path, so the refusal costs nothing until a caller arrives without
    one -- which is exactly when it is worth having.

    The second is *the path is set but never appears in the render*. That one
    is legitimate here -- a render carrying no disclosure has nothing to
    normalise -- so it passes through, and
    `test_the_graded_size_does_not_move_with_the_checkout_path` is what turns it
    red for the renders that are supposed to carry one: it requires the raw
    sizes at two config paths to differ by exactly the path difference before
    it grades anything.
    """
    path = supertool._CONFIG_PATH or ""
    assert path, (
        "graded_bytes was asked to normalise a render with no `_CONFIG_PATH` "
        "set. The disclosure names os.getcwd() in that state, so the render is "
        "still checkout-dependent and the substitution below cannot find it -- "
        "returning a byte count here would be the un-normalised number in the "
        "shape of a normalised one. Install a config path (the "
        "`shipped_config` fixture does) rather than grading blind.")
    return len(text.replace(path, CANONICAL_CONFIG_PATH).encode("utf-8"))


def _hook_payload() -> str:
    """What `hooks/session-start.sh` prints, by the ops it prints it with."""
    return "".join(supertool.dispatch(op) for op in
                   ("introduction", "output-format", "ops:session"))


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
        return graded_bytes(self.render()) / 1000


def _r(name: str) -> Callable[[], str]:
    return RENDERS[name]


SITES = (
    # README.md carried these four until #2142 moved the mechanism-heavy
    # "How to use" section (hook output cap, byte figures) out to docs/ --
    # the same figures are still graded below via docs/operations/index.md,
    # docs/contributing.md and hooks/session-start.sh, so nothing here goes
    # unpinned.
    Claim("docs/operations/index.md", "ops", _r("ops")),
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
#:
#: Empty since #2028. Its one entry existed because `docs/contributing.md` said
#: "the ~7KB SessionStart cap" beside `ops:roster` — a cap wearing the shape of
#: a render size. #2029 replaced every `~7KB` cap mention with the harness's
#: actual `10,000-byte`, which the `~([\d.]+)KB` pattern does not match, so the
#: false positive is gone rather than suppressed. Kept as an empty tuple rather
#: than deleted: the mechanism is the interesting part, and the next cap phrased
#: in KB beside a render name will need it again.
NOT_A_RENDER_SIZE: tuple = ()


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


#: A short and a long stand-in for the config path the disclosure names. The
#: long one is deliberately far past any plausible checkout, so that a grader
#: absorbing the difference into `TOLERANCE_KB` cannot pass by luck.
_SHORT_CONFIG_PATH = "/c.json"
_LONG_CONFIG_PATH = "/" + "d" * 200 + "/c.json"


@pytest.mark.parametrize(
    "claim", SITES, ids=["%s:%s" % (c.path, c.subject) for c in SITES])
def test_the_graded_size_does_not_move_with_the_checkout_path(
        claim, shipped_config, monkeypatch) -> None:
    """Every render names the absolute config path, so its raw byte count is a
    fact about where the checkout sits. Grading that raw count makes each row
    above a claim about the developer's directory layout, wearing the sentence
    "this checkout renders N bytes".

    Measured 2026-08-21 at c6dd83ae: the same tree renders `ops` at 3,724 bytes
    from a 43-character worktree path and 3,811 from a 130-character one -- 87
    bytes apart, for a path 87 characters longer. That is 87% of the whole
    tolerance, spent on a variable none of these figures is about, and it had
    already produced a false red plus a wrong diagnosis. A clone taken under a
    128-character scratchpad path failed four of the rows above and was
    reported as a composition defect against PR #1890 -- which in fact leaves
    every one of these renders byte-identical (3,724 / 72,712 / 14,705 / 1,966
    on both sides of it, re-measured at one path).

    The pair of assertions is the point, and neither survives alone:

    * The **positive control** first. If the disclosure ever stops naming the
      path, both renders come back the same length and the equality below
      passes while checking nothing -- a guard reduced to the absence it exists
      to detect. Requiring the raw sizes to differ by exactly the path
      difference makes "the path stopped reaching the render" a red line naming
      this test, rather than a green tick.
    * Then the property: with the path normalised away, the graded size is the
      same number in every checkout and on every platform. A Windows leg checking
      out at `D:\\a\\claude-supertool\\claude-supertool` and a Linux leg at
      `/home/runner/work/claude-supertool/claude-supertool` differ by 12
      characters today, and by whatever the runner images decide tomorrow.
    """
    monkeypatch.setattr(supertool, "_CONFIG_PATH", _SHORT_CONFIG_PATH)
    raw_short = len(claim.render().encode("utf-8"))
    graded_short = claim.actual_kb()

    monkeypatch.setattr(supertool, "_CONFIG_PATH", _LONG_CONFIG_PATH)
    raw_long = len(claim.render().encode("utf-8"))
    graded_long = claim.actual_kb()

    expected = len(_LONG_CONFIG_PATH) - len(_SHORT_CONFIG_PATH)
    assert raw_long - raw_short == expected, (
        "%s's raw size moved by %s bytes for a %s-character change of config "
        "path. This test can say nothing about path-independence unless the "
        "path demonstrably reaches the render, and it no longer does so the "
        "way it did -- which is a failure here, not a pass."
        % (claim.subject, raw_long - raw_short, expected))

    assert graded_short == graded_long, (
        "%s grades as %.3fKB from a %s-character config path and %.3fKB from "
        "a %s-character one. The graded size must be a fact about the render, "
        "not about where this checkout sits on disk."
        % (claim.subject, graded_short, len(_SHORT_CONFIG_PATH), graded_long,
           len(_LONG_CONFIG_PATH)))


def test_grading_without_a_config_path_refuses_rather_than_answering(
        shipped_config, monkeypatch) -> None:
    """The third state, asserted rather than assumed.

    `_preset_disclosure` names `os.getcwd()` when `_CONFIG_PATH` is falsy, so
    the render is still checkout-dependent while the string that locates it is
    gone. A `graded_bytes` that returned a length there would hand every row
    the raw count in the shape of a normalised one -- and the assertion it
    fails is worded "this checkout renders N bytes", the exact sentence that
    sent a maintainer after the wrong pull request.

    Every row reaches `graded_bytes` through `shipped_config`, which always
    sets a path, so this branch is unreachable from the suite as it stands.
    That is precisely why it is pinned here: a refusal nobody can produce is
    indistinguishable from one that was never written.
    """
    monkeypatch.setattr(supertool, "_CONFIG_PATH", "")
    with pytest.raises(AssertionError, match="rather than grading blind"):
        graded_bytes("`ops` renders something at /some/checkout/path.")

    # ...and the same call answers normally once a path is installed, so the
    # refusal above is about the missing path rather than the helper refusing
    # everything it is handed.
    monkeypatch.setattr(supertool, "_CONFIG_PATH", "/x/.supertool.json")
    assert graded_bytes("no path in here") == len("no path in here")


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
