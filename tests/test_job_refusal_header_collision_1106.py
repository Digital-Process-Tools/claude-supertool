"""#1106 — one phrase, two renders, opposite meanings.

`no error pattern matched` ended up in both halves of the job ops:

  * inside a *successful* classification, as `gap_marker`'s aside about the
    lines between two anchors — `... (105 lines elided by this op — no error
    pattern matched them; the log itself is intact)`;
  * and as the header of a *refusal* to classify at all —
    `## FAILED — no error pattern matched`.

It has already cost something. Two of #1099's tests asserted
`"no error pattern matched" not in out` as shorthand for "the refusal header
is absent", went red after rebasing onto #1091, and were red against entirely
correct code — the phrase had simply started appearing in healthy output.

The next readers are worse placed than that agent was: a grep over saved
output, a watch rule, an agent scanning a render to decide whether a red leg
was classified. All of them see a string, not a section, and the elision
notice can appear many times in one render while the refusal header appears
once at the top. Concluding "supertool could not classify this job" from a
hit on the phrase is exactly wrong in the common case.

#1091's wording wins, deliberately: `the log itself is intact` is the only
clause separating *this op cut lines* from *the log was truncated*, and #1014
was filed on precisely that misread. So the refusal side moves.

Pinned here as a property rather than as two literals — the guard has to
survive the next rewording of either string, which is the whole failure being
fixed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh = _load("presets/github/job.py", "github_job_1106")
gl = _load("presets/gitlab/job.py", "gitlab_job_1106")
TWINS = (("gh-job", gh), ("gl-job", gl))


def _refusal_header(mod: Any, capsys: Any) -> str:
    """The header line an unclassifiable failed job actually renders."""
    mod._print_unmatched_failure("123", "failed", ["ZZZ"], ["a", "b"], 2)
    out = capsys.readouterr().out
    heads = [ln for ln in out.splitlines() if ln.startswith("## FAILED")]
    assert heads, f"no refusal header in the render:{chr(10)}{out}"
    return heads[0]


def _words(text: str) -> list[str]:
    return [w for w in text.replace("—", " ").replace("(", " ")
            .replace(")", " ").replace(";", " ").split() if w]


def _longest_shared_run(a: str, b: str) -> list[str]:
    """The longest run of consecutive words the two strings have in common."""
    wa, wb = _words(a), _words(b)
    best: list[str] = []
    for i in range(len(wa)):
        for j in range(len(wb)):
            k = 0
            while (i + k < len(wa) and j + k < len(wb)
                   and wa[i + k].lower() == wb[j + k].lower()):
                k += 1
            if k > len(best):
                best = wa[i:i + k]
    return best


# The bar. Two or three words in common is ordinary English ("no error", "the
# log"); four consecutive words is a phrase, and a phrase is what a grep, a
# watch rule or a test assertion keys on.
MAX_SHARED_WORDS = 3


def test_a_refusal_header_and_an_elision_notice_share_no_phrase(capsys) -> None:
    for name, mod in TWINS:
        header = _refusal_header(mod, capsys)
        for n in (1, 5, 105):
            marker = mod.gap_marker(n)
            shared = _longest_shared_run(header, marker)
            assert len(shared) <= MAX_SHARED_WORDS, (
                f"{name}: a refusal and a healthy elision share the phrase "
                f"{' '.join(shared)!r}. A reader who greps it cannot tell "
                f"'supertool could not classify this job' from 'these lines "
                f"were between two anchors'.{chr(10)}"
                f"  header: {header!r}{chr(10)}  marker: {marker!r}")


def test_the_elision_notice_keeps_the_clause_1014_needs(capsys) -> None:
    """The fix must land on the refusal side, not by shortening the marker.

    `the log itself is intact` is the only thing separating *this op elided
    lines* from *the log was truncated*, and #1014 was filed on the second
    reading of the first fact.
    """
    for name, mod in TWINS:
        marker = mod.gap_marker(105)
        assert "the log itself is intact" in marker, f"{name}: {marker!r}"
        assert "105 lines elided by this op" in marker, f"{name}: {marker!r}"


def test_the_refusal_header_still_refuses(capsys) -> None:
    """Renaming it must not quieten it — it is a three-state disclosure."""
    for name, mod in TWINS:
        mod._print_unmatched_failure("123", "failed", ["ZZZ"], ["a", "b"], 2)
        out = capsys.readouterr().out
        assert "## FAILED" in out, f"{name}: {out}"
        assert "not that the log is clean" in out, f"{name}: {out}"
        assert "could not classify" in out, f"{name}: {out}"


def test_the_two_twins_refuse_in_the_same_words(capsys) -> None:
    """Same reason as `gap_marker` (#1066): one idea, one wording, two forges."""
    headers = [_refusal_header(mod, capsys) for _, mod in TWINS]
    assert headers[0] == headers[1], headers
