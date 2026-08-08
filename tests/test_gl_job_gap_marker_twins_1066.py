"""#1066 — the two job ops must say the same thing about an elision.

`gh-job` and `gl-job` hold private, near-identical copies of the same two
renderers. `e5714e2` gave the GitLab side a counted gap marker in #409; the
GitHub side rediscovered the same defect 640 issues later as #1050 and fixed it
with a *different string*, and `presets/gitlab/job.py:_emit_grep_hits` never got
either fix — it still opened a grep render with a bare `...` covering zero lines
whenever the first hit sat at index 0.

Three properties are pinned here, and the third is the one that stops this
recurring:

  1. no marker is emitted for a gap of zero lines, on either renderer;
  2. every withheld line is covered by exactly one marker, `gl-job` included,
     which needs the trailing marker the GitHub twin already has;
  3. the two ops render the marker with the **same words**, checked by
     comparing the twins directly. A reader who learns one wording and meets
     the other has to work out whether the difference means something.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl = _load("presets/gitlab/job.py", "gitlab_job_1066")
gh = _load("presets/github/job.py", "github_job_1066")

#: Matches the counted form of the marker, whatever prose follows the count.
_COUNT = re.compile(r"\.\.\. \((\d+) lines? elided")


@pytest.fixture(autouse=True)
def _no_cause_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gl-job` widens around built-in cause markers as well as the patterns.

    The fixtures below are about gap arithmetic, so that extra selection would
    make the counts depend on whether a filler line happened to look like a
    stack frame. Turned off through the knob the op itself publishes.
    """
    monkeypatch.setenv("GL_JOB_CAUSE_MARKERS", "0")


def _gap_lines(sections: list[tuple[int, str]]) -> list[str]:
    return [text for num, text in sections if num == -1]


def _elided_counts(text: str) -> list[int]:
    return [int(n) for n in _COUNT.findall(text)]


# ---------------------------------------------------------------------------
# 1. the phantom marker — a gap of zero lines
# ---------------------------------------------------------------------------

def _grep_render(mod: Any, lines: list[str], hits: list[int], capsys: Any) -> str:
    rx = re.compile("HIT")
    mod._emit_grep_hits(lines, hits, rx, len(hits), 65536, "KNOB", "HIT", 0)
    return capsys.readouterr().out


def test_a_grep_render_whose_first_hit_is_line_one_opens_with_no_marker(
    capsys: Any,
) -> None:
    """`prev = -2` made index 0 satisfy `idx > prev + 1` — a marker over nothing."""
    lines = ["HIT one", "filler", "HIT two"]
    out = _grep_render(gl, lines, [0, 2], capsys)
    body = [ln for ln in out.splitlines() if ln.strip()]
    assert not body[1].lstrip().startswith("..."), (
        "the render opens with an elision marker above the log's own first "
        "line, covering zero lines:\n" + out)


def test_no_grep_marker_ever_claims_zero_lines(capsys: Any) -> None:
    lines = ["HIT one", "filler", "HIT two"]
    out = _grep_render(gl, lines, [0, 2], capsys)
    assert 0 not in _elided_counts(out), (
        "a marker accounts for zero withheld lines:\n" + out)


def test_a_real_grep_gap_is_still_counted(capsys: Any) -> None:
    """The fix must delete the phantom marker, not the marker."""
    lines = ["HIT one"] + [f"filler {i}" for i in range(5)] + ["HIT two"]
    out = _grep_render(gl, lines, [0, 6], capsys)
    assert _elided_counts(out) == [5], (
        "the five withheld lines are not accounted for:\n" + out)


@pytest.mark.parametrize("first_hit", [0, 1, 2])
def test_the_grep_marker_count_matches_the_lines_actually_withheld(
    first_hit: int, capsys: Any
) -> None:
    """Index 0, index 1 and index 2 — the boundary the `-2`/`-1` slip lives on."""
    lines = [f"line {i}" for i in range(10)]
    lines[first_hit] = "HIT head"
    lines[9] = "HIT tail"
    out = _grep_render(gl, lines, [first_hit, 9], capsys)
    expected = [n for n in (first_hit, 9 - first_hit - 1) if n > 0]
    assert _elided_counts(out) == expected, (
        f"first hit at index {first_hit}: markers {_elided_counts(out)!r} do "
        f"not match the withheld runs {expected!r}:\n" + out)


def test_a_grep_render_with_no_gaps_prints_no_marker(capsys: Any) -> None:
    lines = ["HIT one", "HIT two"]
    out = _grep_render(gl, lines, [0, 1], capsys)
    assert _elided_counts(out) == [], (
        "nothing was withheld, yet a marker was printed:\n" + out)


# ---------------------------------------------------------------------------
# 2. one vocabulary across the twins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 5, 34, 417, 8069])
def test_the_two_ops_word_the_marker_identically(n: int) -> None:
    """The pin #628 asks for, made concrete on the one string that drifted."""
    assert gl.gap_marker(n) == gh.gap_marker(n), (
        "gl-job and gh-job describe the same elision with different words; a "
        "reader who meets both has to work out whether the difference means "
        f"something:\n  gl: {gl.gap_marker(n)!r}\n  gh: {gh.gap_marker(n)!r}")


def test_the_marker_is_singular_for_one_line_on_both() -> None:
    for mod, name in ((gl, "gl-job"), (gh, "gh-job")):
        assert "1 line elided" in mod.gap_marker(1), f"{name}: {mod.gap_marker(1)!r}"


@pytest.mark.parametrize("mod,name", [(gl, "gl-job"), (gh, "gh-job")])
def test_the_marker_says_who_cut_and_how_much(mod: Any, name: str) -> None:
    """The log is intact; this selection of it is not. #1014 turned on that."""
    text = mod.gap_marker(34)
    assert "this op" in text, f"{name}: does not say who cut: {text!r}"
    assert "34" in text, f"{name}: does not say how much: {text!r}"


def test_every_gl_job_error_block_gap_uses_the_shared_marker() -> None:
    """`_find_error_sections` had its own inline wording. One string, one place."""
    lines = ["FAILED a"] + [f"filler {i}" for i in range(40)] + ["FAILED b"]
    gaps = _gap_lines(gl._find_error_sections(lines, ["FAILED"], 2))
    assert gaps, "fixture produced no gap"
    for gap in gaps:
        n = _elided_counts(gap)
        assert n, f"gap carries no count: {gap!r}"
        assert gap == gl.gap_marker(n[0]), (
            f"a gap is worded by hand instead of by gap_marker: {gap!r}")


# ---------------------------------------------------------------------------
# 3. every withheld line accounted for — the trailing gap on gl-job
# ---------------------------------------------------------------------------

def _fail_log() -> list[str]:
    return (["FAILED a"] + [f"filler {i}" for i in range(40)]
            + ["FAILED b"] + [f"trailing {i}" for i in range(30)])


@pytest.mark.parametrize("mod,name", [(gl, "gl-job"), (gh, "gh-job")])
def test_fail_mode_accounts_for_the_lines_after_the_last_block(
    mod: Any, name: str
) -> None:
    """`:fail` prints blocks and nothing else, so a trailing claim is truthful."""
    lines = _fail_log()
    sections = mod._find_error_sections(lines, ["FAILED"], 2, trailing_gap=True)
    shown = [num for num, _ in sections if num > 0]
    elided = sum(sum(_elided_counts(g)) for g in _gap_lines(sections))
    assert len(shown) + elided == len(lines), (
        f"{name}: {len(lines)} lines in, {len(shown)} shown and {elided} "
        f"declared elided — {len(lines) - len(shown) - elided} lines vanished "
        f"unmentioned")


@pytest.mark.parametrize("mod,name", [(gl, "gl-job"), (gh, "gh-job")])
def test_the_default_render_does_not_claim_a_trailing_elision(
    mod: Any, name: str
) -> None:
    """The default path prints `## Tail` right below, holding those very lines."""
    sections = mod._find_error_sections(_fail_log(), ["FAILED"], 2)
    assert [num for num, _ in sections if num > 0], f"{name}: matched nothing"
    assert sections[-1][0] > 0, (
        f"{name}: the render ends on a marker declaring lines elided that "
        f"`## Tail` prints three lines later: {sections[-1][1]!r}")
