"""`glob` still answered questions about directories outside the boundary (#1392).

Two mechanisms, one class — containment (read).

* **`_glob_reach` modelled `**` as exactly one component.** The expander treats
  it as zero-or-more, so a pattern carrying one `**` per `..` clears the
  pattern gate and then climbs. `_glob_results_escape` catches the survivors
  only when something is *there* to survive, which turns the pair
  "the directory exists" / "it does not" into two different renders.
* **The exclude-paths filter ran before the containment check**, so an outside
  directory whose contents are excluded produced `(0 files, 6 files hidden by
  exclude-paths)` — a refusal converted into a count, and this repo's own
  defect class with the sign flipped: an absence the tool produced, reading as
  an absence in the world, except here the absence is *informative*.

No filenames and no bytes ever crossed. Existence and exact cardinality did,
which is the same existence oracle #1135 and #1142 are about.

The countervailing fact, so nobody reads this as a regression: before
`e2d5087`, inside this same release delta, `glob` had no gate at all and would
have printed the names. This is a narrowing that left a residue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool

NL = chr(10)
MARK = "GLOB-1392"


@pytest.fixture
def boxed(tmp_path: Path, monkeypatch) -> Path:
    """cwd is `box/`; two sibling directories sit outside it.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 for the whole suite so
    tmp_path fixtures work at all — a containment test that does not put it
    back asserts nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    # Six files, and every one of them is hidden by the default exclude-paths
    # (`.ssh/`), which is what turned a refusal into a count.
    secret = tmp_path / ".ssh"
    secret.mkdir()
    for i in range(6):
        (secret / (MARK + "-" + str(i))).write_text("x" + NL, encoding="utf-8")
    plain = tmp_path / "aaaa"
    plain.mkdir()
    (plain / (MARK + "-p")).write_text("x" + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)
    return box


def _err(out: str) -> bool:
    return out.startswith("ERROR:")


def test_a_pattern_that_can_climb_out_is_refused_whether_or_not_it_lands(
        boxed):
    """`**` is zero-or-more, so a `..` after it moves the cursor.

    The two rows are the oracle: one names a directory that exists outside the
    boundary, the other names one that does not. They must be answered the
    same way, and the only honest same answer is a refusal.
    """
    here = supertool.op_glob("**/../aaaa/*")
    gone = supertool.op_glob("**/../bbbb/*")
    assert _err(here), here
    assert _err(gone), gone


def test_a_refusal_is_not_a_count(boxed):
    """`(0 files, 6 files hidden by exclude-paths)` is exact cardinality."""
    out = supertool.op_glob("**/../.ssh/*")
    assert _err(out), out
    assert "hidden by exclude-paths" not in out, out


def test_the_refusal_does_not_report_how_many_matched(boxed):
    """Two outside directories of different sizes must refuse identically.

    #1366's own reasoning: dropping entries hands back a shorter list under an
    honest header, and printing them discloses the paths. The count is the
    third form of the same disclosure, and it is the one that shipped.
    """
    six = supertool.op_glob("**/../.ssh/*").replace(".ssh", "DIR")
    one = supertool.op_glob("**/../aaaa/*").replace("aaaa", "DIR")
    assert _err(six) and _err(one), (six, one)
    assert six == one, (six, one)


def test_the_multi_star_shape_from_the_audit(boxed):
    """The literal patterns the round-2 audit ran, from a nested cwd."""
    for pattern in ("**/**/../../aaaa/*", "./**/**/../../aaaa/*"):
        out = supertool.op_glob(pattern)
        assert _err(out), (pattern, out)


def test_an_ordinary_recursive_pattern_still_works(boxed):
    """The gate must not refuse everything: that is a fix nobody can use."""
    (boxed / "inside").mkdir()
    (boxed / "inside" / (MARK + "-in.txt")).write_text("x" + NL,
                                                       encoding="utf-8")
    out = supertool.op_glob("**/*.txt")
    assert not _err(out), out
    assert MARK + "-in.txt" in out, out
