"""#1807 - the marker tuples are narrower than the call sites, on purpose.

`_repo_target._ABSENT_CWD` / `_ABSENT_TARGET` were documented as holding *"the
same phrases the twelve classifying call sites already match on"*. They are not
the same phrases, and the sentence is the defect: the tuples are strictly
narrower, and that narrowness is load-bearing rather than an oversight.

Derived, not retyped. The call sites are enumerated by
`test_repo_target_no_repo_error_1789.py`'s AST register; the *markers* those
sites match on are derived below from the same tree, because a hand-copied list
of phrases is the thing that went stale the first time.

**Why not simply widen the tuples to match.** The call sites can afford broad
markers and `classify_detail` cannot, because they are not the same kind of
classifier:

* A call site is an **ordered if-chain** over one resource lookup. Its `repo`
  arm is tried before `notfound`, `auth` and `ratelimit`, and it runs only
  after the caller has already turned a missing binary and a timeout into their
  own messages (`run.py`'s `except FileNotFoundError`). The broad phrase is
  fenced by everything tried before it.
* `classify_detail` is a **standalone two-way** classifier with no ordering and
  no pre-filtering. The same words carry a different risk there.

Concretely, borrowing the call sites' target vocabulary would classify
`"gh not found - install from https://cli.github.com"` - the string
`test_repo_target_no_repo_error_1789.py` names `GH_MISSING` and asserts must be
`UNKNOWN` - as `ABSENT`, because that vocabulary contains a bare `"not found"`.
That is #1789's exact defect re-imported into the module written to fix it, so
`test_widening_to_the_call_site_vocabulary_would_reintroduce_1789` runs the
cost rather than leaving it as a claim in prose.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load("presets/_repo_target.py", "_repo_target")
status_probe = _load("presets/_status_probe.py", "_status_probe_1807")

#: `_in_strings` below resolves a call to either of these (#1864) into the
#: marker tuple it tests, the same way it resolves a literal `"x" in s`. The
#: seven target call sites under `presets/github/` and the four under
#: `presets/gitlab/` route their not-found reading through
#: `_status_probe.says_not_found` rather than a bare `"404" in s` now, and a
#: derivation that only understood the literal shape would see nothing at
#: those sites -- the same "silently saw less than it claimed" failure this
#: module's own docstring names about the `notfound`-bucket-only keying.
_STATUS_PROBE_MARKERS = {
    "says_not_found": tuple(status_probe.NOT_FOUND_MARKERS),
    "says_forbidden": tuple(status_probe.FORBIDDEN_MARKERS),
}

#: A string that satisfies the call sites' broad `git remotes` and none of the
#: tuple's narrow spellings. #1807's own example.
BROAD_ONLY_CWD = "...configured git remotes were found, none usable"
#: The same shape on the target side: `could not resolve`, but not
#: `could not resolve to a repository`. gh really does say this for a bad
#: number on a repository that exists.
BROAD_ONLY_TARGET = ("GraphQL: Could not resolve to a PullRequest with the "
                     "number 99999. (pullRequest)")
#: What `pr_merge.py`'s `_gh_json` produces when gh is not installed. Asserted
#: `UNKNOWN` by #1789 and re-used here as the price of widening.
GH_MISSING = "gh not found - install from https://cli.github.com"


# ===========================================================================
# deriving the call sites' own vocabulary from the tree
# ===========================================================================

def _in_strings(test: ast.AST) -> set:
    """Every `"literal" in <name>` string in one `if` test.

    Also resolves a call to `_status_probe.says_not_found` / `says_forbidden`
    (#1864) into the marker tuple it tests, keyed on the attribute/function
    name alone -- the call sites all spell the module `_status_probe`, but
    matching on the name rather than the qualified path is the same choice
    `_body_calls` below already makes for `no_repo_error`.
    """
    out = set()
    for node in ast.walk(test):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, ast.In) for op in node.ops):
            if isinstance(node.left, ast.Constant) and isinstance(
                    node.left.value, str):
                out.add(node.left.value)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = ((isinstance(fn, ast.Attribute) and fn.attr)
                     or (isinstance(fn, ast.Name) and fn.id))
            if name in _STATUS_PROBE_MARKERS:
                out |= set(_STATUS_PROBE_MARKERS[name])
    return out


def _body_returns(body: list, literal: str) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if (isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == literal):
                return True
    return False


def _body_calls(body: list, name: str) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                fn = node.func
                named = ((isinstance(fn, ast.Attribute) and fn.attr)
                         or (isinstance(fn, ast.Name) and fn.id))
                if named == name:
                    return True
    return False


def call_site_markers(root: Path) -> tuple:
    """`(cwd_sites, target_sites)` of `(relpath, lineno, sorted_markers)`.

    Both halves are keyed on **what the arm produces**, not on how it is
    spelled, because the same verdict is written two ways in this tree:

    * a *cwd* guard calls `no_repo_error` directly, or returns the `"repo"`
      bucket that `check.py` and `job.py` route through;
    * a *target* guard returns the `"notfound"` bucket, or builds the
      not-found sentence inline by calling `_repo_target.not_found_scope` /
      `not_found_hint`.

    That second arm of each pair is the whole reason this is not keyed on the
    bucket alone. Keyed on `return "notfound"` only, this found **2** target
    guards where the tree has **7**: `pr.py`, `issue.py`, `branch.py`, `run.py`
    and `labels.py` inline the message instead of bucketing it, and their
    markers went underived. The union came out right anyway, because those
    sites happen to use the same phrases as the two that were seen - which is
    precisely the shape this repository keeps filing: a derivation that
    silently saw less than it claimed, reported as a clean answer.

    Keying on the *next arm in the if-chain* was tried and rejected: it is
    right for six sites and picks up `labels.py`'s `auth` arm, because
    `labels.py` has no target arm at all.

    `_repo_target.py` itself is skipped: the definition is not a call site.

    **Known blind spot**, harmless today and written down rather than left to
    be rediscovered: `_in_strings` reads only the left operand, so a guard
    inverted to `s in "literal"` would go unseen. No guard in `presets/` is
    written that way, and the inverted form does not mean the same thing.
    """
    cwd, target = [], []
    for path in sorted(Path(root).rglob("*.py")):
        try:
            rel = path.relative_to(_ROOT).as_posix()
        except ValueError:
            rel = path.name
        if rel == "presets/_repo_target.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            markers = _in_strings(node.test)
            if not markers:
                continue
            if (_body_calls(node.body, "no_repo_error")
                    or _body_returns(node.body, "repo")):
                cwd.append((rel, node.lineno, sorted(markers)))
            elif (_body_returns(node.body, "notfound")
                    or _body_calls(node.body, "not_found_scope")
                    or _body_calls(node.body, "not_found_hint")):
                target.append((rel, node.lineno, sorted(markers)))
    return cwd, target


def _union(sites: list) -> set:
    out = set()
    for _, _, markers in sites:
        out |= set(markers)
    return out


def _under(sites: list, prefix: str) -> list:
    """The sites under one forge's directory.

    `classify_detail` reads **gh's** stderr, so `_ABSENT_TARGET` is a claim
    about `presets/github/`. `presets/gitlab/` builds the same not-found
    sentence from the same helper and so lands in the same derivation - and it
    matches on the same phrases today, which is a fact worth asserting on its
    own rather than one worth folding into the GitHub union. Kept apart so
    that a GitLab rewording fails a GitLab-named assertion instead of looking
    like a change in what gh says.
    """
    return [s for s in sites if s[0].startswith(prefix)]


def test_the_derivation_sees_a_guard_written_either_way(tmp_path) -> None:
    """The positive control for every assertion below.

    A derivation that found nothing would make `refines a call site marker`
    vacuously true for the tuples and pass forever. Both guard shapes are
    asserted against a synthetic module, because a direct `no_repo_error`
    guard and a `"repo"`-bucket guard are two different AST shapes and
    `presets/` would keep passing a matcher that understood only one.
    """
    (tmp_path / "fake.py").write_text(
        "def direct(s):\n"
        "    if 'alpha marker' in s:\n"
        "        return no_repo_error('gh-x:1')\n"
        "def bucket(s):\n"
        "    if 'beta marker' in s:\n"
        "        return 'repo'\n"
        "    if 'gamma marker' in s:\n"
        "        return 'notfound'\n"
        "def inline(s):\n"
        "    if 'delta marker' in s:\n"
        "        return f'ERROR: x {_repo_target.not_found_scope()}.'\n",
        encoding="utf-8")
    cwd, target = call_site_markers(tmp_path)
    assert _union(cwd) == {"alpha marker", "beta marker"}, repr(cwd)
    assert _union(target) == {"gamma marker", "delta marker"}, repr(target)


def test_the_call_site_vocabulary_is_what_1807_reported() -> None:
    """The other half of the control: the real tree, not a fixture.

    Written down so a guard that stops matching is a failure here rather than
    a silently smaller union feeding every assertion below.
    """
    cwd, target = call_site_markers(_ROOT / "presets")

    assert _union(cwd) == {
        "github host", "not a git repository", "git remotes",
        "could not determine",
    }, repr(cwd)
    # `422` and `no commit found` are `branch.py`'s own resource-specific
    # markers, sitting in the same arm. They are part of what that call site
    # matches on, so they belong in the derived union; nothing below requires
    # a tuple counterpart for them.
    github_target = _under(target, "presets/github/")
    assert _union(github_target) == {
        "could not resolve", "http 404", "not found", "422", "no commit found",
    }, repr(github_target)
    assert len(cwd) == 10, (
        "the number of cwd-classifying guards changed (was 10 at #1807, "
        "covering the 12 classifying calls the #1789 register counts): "
        + repr(cwd))
    assert len(github_target) == 7, (
        "the number of target-classifying guards changed (was 7 at #1807). "
        "This count is the half that was wrong first: keyed on the "
        "`notfound` bucket alone it read 2, and the union was right anyway - "
        "so a count that is merely plausible is not evidence here: "
        + repr(github_target))
    assert all(rel.startswith("presets/github/") for rel, _, _ in cwd), (
        "a cwd guard turned up outside presets/github/. `no_repo_error` and "
        "the `repo` bucket are gh's vocabulary: " + repr(cwd))


def test_gitlab_matches_on_the_same_target_phrases_for_now() -> None:
    """Asserted apart from the GitHub union, on purpose.

    `presets/gitlab/` reaches the same not-found sentence through the same
    helper, so it lands in the same derivation. It is not what
    `_ABSENT_TARGET` is a claim about, and folding it in would mean a GitLab
    rewording failed an assertion named for gh. Pinned here instead, so the
    overlap is a stated fact rather than an accident nobody is watching.
    """
    _, target = call_site_markers(_ROOT / "presets")
    gitlab = _under(target, "presets/gitlab/")

    assert len(gitlab) == 4, repr(gitlab)
    assert _union(gitlab) == {"could not resolve", "http 404", "not found"}, (
        "GitLab's target vocabulary drifted from GitHub's. That is allowed - "
        "say so here. It does not by itself say anything about "
        "_ABSENT_TARGET, which reads gh's stderr: " + repr(gitlab))


# ===========================================================================
# the relationship the docstring promises, pinned mechanically
# ===========================================================================

def test_every_tuple_marker_refines_a_call_site_marker() -> None:
    """No thirteenth spelling.

    This is the half the docstring gets right and the reason the tuples exist:
    a marker here that no call site matches would be a new vocabulary, and the
    one unclassified caller would reach a verdict the other twelve cannot.
    """
    cwd_sites, target_sites = call_site_markers(_ROOT / "presets")
    target_sites = _under(target_sites, "presets/github/")
    for tuple_markers, site_markers, label in (
            (rt._ABSENT_CWD, _union(cwd_sites), "_ABSENT_CWD"),
            (rt._ABSENT_TARGET, _union(target_sites), "_ABSENT_TARGET"),
    ):
        assert tuple_markers, label + " is empty"
        assert site_markers, label + " has no call-site markers to refine"
        for marker in tuple_markers:
            assert any(site in marker for site in site_markers), (
                label + " has a spelling no call site matches: " + repr(marker)
                + " against " + repr(sorted(site_markers)))


def test_the_tuples_are_strictly_narrower_not_the_same_phrases() -> None:
    """The half the docstring got wrong, written as an assertion.

    Named pairs rather than a count, so that a maintainer reading a failure
    here is told which phrase moved.
    """
    cwd_sites, target_sites = call_site_markers(_ROOT / "presets")
    cwd_site_markers = _union(cwd_sites)
    target_site_markers = _union(_under(target_sites, "presets/github/"))

    for site_marker, tuple_marker in (
            ("git remotes", "no git remotes"),
            ("git remotes", "git remotes found"),
            ("could not determine", "could not determine base repository"),
    ):
        assert site_marker in cwd_site_markers, site_marker
        assert tuple_marker in rt._ABSENT_CWD, tuple_marker
        assert site_marker != tuple_marker and site_marker in tuple_marker

    for site_marker, tuple_marker in (
            ("could not resolve", "could not resolve to a repository"),
    ):
        assert site_marker in target_site_markers, site_marker
        assert tuple_marker in rt._ABSENT_TARGET, tuple_marker
        assert site_marker != tuple_marker and site_marker in tuple_marker

    # ... and the broadest call-site marker of all has no tuple counterpart.
    assert "not found" in target_site_markers
    assert not any("not found" == m for m in rt._ABSENT_TARGET)

    # The markers that really are identical, so `strictly narrower` is not
    # read as `every phrase differs`. `http 404` joined this list at #1864:
    # the call sites used to match the bare digit `404` and the tuple already
    # matched the narrow `http 404` -- the fix that removed the bare digit
    # made the call site marker literally the tuple's own spelling, where it
    # used to be a superstring of it.
    for shared in ("github host", "not a git repository"):
        assert shared in cwd_site_markers and shared in rt._ABSENT_CWD
    assert "http 404" in target_site_markers and "http 404" in rt._ABSENT_TARGET


def test_the_comment_says_narrower_and_no_longer_says_the_same_phrases(
) -> None:
    """The sentence itself, pinned.

    #1807 is a documentation defect: the code was right and the comment above
    it promised something else. A test on the tuples alone would have stayed
    green through the whole of it.
    """
    source = (_ROOT / "presets/_repo_target.py").read_text(encoding="utf-8")
    head, _, _ = source.partition("_ABSENT_TARGET")
    _, _, block = head.rpartition("UNKNOWN = ")

    assert "same phrases" not in block, (
        "the comment above _ABSENT_CWD still claims the tuples hold the same "
        "phrases as the call sites. They are strictly narrower - #1807.")
    assert "narrower" in block, (
        "the comment above _ABSENT_CWD no longer states that the tuples are "
        "deliberately narrower than the call sites - #1807.")


# ===========================================================================
# what the narrowness buys, and what widening would have cost
# ===========================================================================

def test_a_broad_only_string_is_unknown_and_its_narrow_twin_is_absent(
) -> None:
    """Both directions in one fixture.

    `everything is UNKNOWN` passes on a classifier that always says UNKNOWN,
    so each broad-only string is paired with a spelling that must still come
    out `ABSENT`.
    """
    assert rt.classify_detail(BROAD_ONLY_CWD) == rt.UNKNOWN
    assert rt.classify_detail("no git remotes found") == rt.ABSENT

    slug = "jbkkz/requivo"
    assert rt.classify_detail(BROAD_ONLY_TARGET, slug) == rt.UNKNOWN
    assert rt.classify_detail(
        "GraphQL: Could not resolve to a Repository with the name 'x/y'.",
        slug) == rt.ABSENT


def test_widening_to_the_call_site_vocabulary_would_reintroduce_1789(
) -> None:
    """The cost of the fix #1807 offered and this change declined.

    Run rather than asserted in prose, so a maintainer who wants to widen the
    tuples sees the price instead of reading about it. `GH_MISSING` is not a
    hypothetical string: `pr_merge.py` produces exactly it, and #1789 exists
    because a failure to reach gh was rendered as a claim about the machine.
    """
    cwd_sites, target_sites = call_site_markers(_ROOT / "presets")
    target_sites = _under(target_sites, "presets/github/")

    def widened(detail: str, slug: str | None) -> str:
        low = detail.lower()
        markers = _union(target_sites) if slug else _union(cwd_sites)
        return rt.ABSENT if any(m in low for m in markers) else rt.UNKNOWN

    slug = "jbkkz/requivo"
    assert widened(GH_MISSING, slug) == rt.ABSENT, (
        "the call-site target vocabulary no longer classifies a missing gh "
        "as absent - #1807's argument for staying narrow may have expired")
    assert rt.classify_detail(GH_MISSING, slug) == rt.UNKNOWN

    # The cwd half of the same trade.
    for transient in ("failed to list git remotes: connection reset",
                      "could not determine the default branch"):
        assert widened(transient, None) == rt.ABSENT, transient
        assert rt.classify_detail(transient) == rt.UNKNOWN, transient
