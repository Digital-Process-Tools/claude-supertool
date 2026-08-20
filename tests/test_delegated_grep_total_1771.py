"""A truncated delegated grep must say how many matches there are (#1771).

`grep` is not optional: a rule in `claude-jit-context` refuses the harness
`Grep` tool and names this op as the replacement, so every completeness sweep an
agent runs goes through it. On the rtk-delegated path a truncated result said

    (10 results in 1 files, scanned ? files - delegated to rtk, limit 10
     - TRUNCATED, more matches exist (total not counted))

so the caller was told the answer was partial and not how partial, over a
denominator that was unknown too. That is this repo's named class - a bounded
presence read as a complete one - with the numerator and the denominator both
missing.

The fix is a second delegated pass, `grep -rc`, run only when the first pass
came back truncated. It yields the exact total and a real scanned-file
denominator, and when it cannot run the report says the total is unknown rather
than filing it as an aside.

**What these tests pin, and why each one is here.**

* The total is the *real* total. Asserting "the receipt names a number" passes
  on a receipt that always names one, so every corpus here has an independently
  computed count and two different corpora assert two different numbers.
* A complete result acquires none of the truncation vocabulary - no
  `TRUNCATED`, no `matches total`. A fix that decorates every receipt is not a
  fix.
* The census declines rather than guesses. When the count pass cannot run, the
  report must carry no number at all and must say the total is unknown.
* Excluded files are not counted. The census argv cannot express the negations
  in the default exclude list, so a `*.pem` really is scanned by the system
  grep - and must be dropped from both the total and the denominator, or the
  delegated report starts disagreeing with the native one about what exists.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Corpora: the count is computed from the corpus, never typed into the assert
# ---------------------------------------------------------------------------

NEEDLE = "ZQNEEDLEZQ"

# name -> how many lines in that file contain NEEDLE. A file with zero matches
# is deliberate: it makes the scanned denominator differ from the file count,
# so a fix that reports one as the other cannot pass.
CORPUS_A = {"a0.txt": 3, "a1.txt": 8, "a2.txt": 1, "a3.txt": 0, "a4.txt": 25}
CORPUS_B = {"b0.txt": 11, "b1.txt": 4, "b2.txt": 0}


def _write_corpus(root: Path, spec: dict[str, int]) -> None:
    for name, hits in spec.items():
        lines = [f"{NEEDLE} line {i}" for i in range(hits)]
        lines.append("filler line with no needle")
        (root / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _expected(spec: dict[str, int]) -> tuple[int, int]:
    """(total matching lines, files scanned) - the independent computation."""
    return sum(spec.values()), len(spec)


def test_the_corpora_are_what_the_asserts_below_assume() -> None:
    """Guard on every test here. If someone edits a corpus, the numbers the
    receipts are checked against move with it - but the two corpora must stay
    *different*, or "the total is real" degenerates into "a total is printed"."""
    assert _expected(CORPUS_A) == (37, 5)
    assert _expected(CORPUS_B) == (15, 3)
    assert _expected(CORPUS_A)[0] != _expected(CORPUS_B)[0]


def _reported_total(out: str) -> int | None:
    m = re.search(r"TRUNCATED, (\d+) matches total", out)
    return int(m.group(1)) if m else None


def _reported_scanned(out: str) -> int | None:
    m = re.search(r"scanned (\d+) files", out)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fixture: the system grep behind a fake rtk, so the argv decides the answer
# ---------------------------------------------------------------------------


class _RealGrepCalls:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def census_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "-rc" in c]


@pytest.fixture
def rtk_real_grep(monkeypatch: pytest.MonkeyPatch) -> _RealGrepCalls:
    """Delegate to the real system grep, exactly as rtk does.

    Canned census output would let a broken census argv pass unnoticed - the
    whole question here is whether a second pass can be built that counts the
    same things the first one matched.
    """
    if not shutil.which("grep"):
        pytest.skip("system grep unavailable")
    seen = _RealGrepCalls()

    def _fake_rtk_run(args, timeout: int = 30) -> str | None:
        seen.calls.append(list(args))
        assert args[0] == "grep"
        proc = subprocess.run(
            ["grep"] + list(args[1:]),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.stdout if proc.returncode == 0 else None

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake_rtk_run)
    return seen


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_name", ["A", "B"])
def test_a_truncated_delegated_grep_reports_the_real_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls, spec_name: str,
) -> None:
    """Two corpora, two different totals. A receipt that always prints a number
    passes one of these and fails the other."""
    spec = {"A": CORPUS_A, "B": CORPUS_B}[spec_name]
    total, scanned = _expected(spec)
    _write_corpus(tmp_path, spec)
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep(NEEDLE, ".", limit=10, no_auto_read=True)

    assert rtk_real_grep.calls, "delegated branch not taken - this pins nothing"
    assert "delegated to rtk" in out, "fell back to the native walker"
    assert "TRUNCATED" in out, f"corpus of {total} was not truncated at 10"
    assert _reported_total(out) == total, (
        f"receipt does not state the real total {total}: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )
    assert "total not counted" not in out
    assert "total unknown" not in out


def test_a_truncated_delegated_grep_reports_a_real_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls,
) -> None:
    """`scanned ? files` cannot support a conclusion about breadth. The census
    pass knows the denominator, so the truncated receipt stops printing `?`.

    CORPUS_A has a file with no matches in it, so the denominator (5) and the
    matched-file count (4) are different numbers - reporting one as the other
    fails here."""
    total, scanned = _expected(CORPUS_A)
    _write_corpus(tmp_path, CORPUS_A)
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep(NEEDLE, ".", limit=10, no_auto_read=True)

    assert rtk_real_grep.calls
    assert _reported_scanned(out) == scanned, (
        f"receipt does not state the real denominator {scanned}: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )
    assert "scanned ? files" not in out


def test_the_census_pass_runs_only_when_the_result_was_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls,
) -> None:
    """The cost bound. A complete delegated grep keeps the single pass it had
    before; the second pass is bought only where it buys something."""
    _write_corpus(tmp_path, CORPUS_A)
    monkeypatch.chdir(tmp_path)

    supertool.op_grep(NEEDLE, ".", limit=500, no_auto_read=True)
    assert rtk_real_grep.census_calls == [], "census ran on a complete result"

    supertool.op_grep(NEEDLE, ".", limit=10, no_auto_read=True)
    assert len(rtk_real_grep.census_calls) == 1, "census did not run on truncation"


# ---------------------------------------------------------------------------
# The control: a complete result must not acquire the truncation vocabulary
# ---------------------------------------------------------------------------


def test_a_complete_delegated_result_gains_no_truncation_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls,
) -> None:
    """The marker's absence is a positive statement: this count is exact. A fix
    that decorates every receipt with a total destroys that."""
    total, _ = _expected(CORPUS_A)
    _write_corpus(tmp_path, CORPUS_A)
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep(NEEDLE, ".", limit=500, no_auto_read=True)

    assert rtk_real_grep.calls
    assert "delegated to rtk" in out, "fell back to the native walker"
    assert "TRUNCATED" not in out
    assert "matches total" not in out
    assert "total unknown" not in out
    assert f"({total} results in" in out, (
        "the complete result does not show every match: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )


# ---------------------------------------------------------------------------
# The third state: the census declines rather than guessing
# ---------------------------------------------------------------------------


class _HeadOnlyRtk:
    """rtk answers the match pass and fails the census pass.

    Stubbed rather than real-grepped because "the second call failed" is not a
    state a working grep can be asked to produce, and the honest-unknown branch
    is the one a reader most needs to be able to trust.
    """

    def __init__(self, head_out: str) -> None:
        self.head_out = head_out
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout: int = 30) -> str | None:
        self.calls.append(list(args))
        if "-rc" in args:
            return None
        return self.head_out


@pytest.fixture
def rtk_no_census(monkeypatch: pytest.MonkeyPatch):
    def _install(head_out: str) -> _HeadOnlyRtk:
        stub = _HeadOnlyRtk(head_out)
        monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
        monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
        monkeypatch.setattr(supertool, "_rtk_run", stub)
        return stub
    return _install


def test_when_the_census_cannot_run_the_total_is_named_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_no_census,
) -> None:
    """`(total not counted)` reads as an aside. The caller has to be able to
    tell "more than 10, and I do not know how many" from a counted total, and
    from a complete result - three states, none collapsing into another."""
    a = tmp_path / "a.txt"
    a.write_text("alpha\n", encoding="utf-8")
    head = "".join(f"{a}:{i}:alpha\n" for i in range(1, 6))
    stub = rtk_no_census(head)
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert stub.calls, "delegated branch not taken - this pins nothing"
    assert any("-rc" in c for c in stub.calls), "census was never attempted"
    assert "TRUNCATED" in out
    assert "total unknown" in out, (
        f"the unknown total is not named: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )
    assert _reported_total(out) is None, (
        "a number was printed for a total that was never counted"
    )
    assert "scanned ? files" in out, (
        "the census failed, so the denominator is not known either"
    )


def test_the_census_declines_output_it_cannot_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An rtk release that answers `-rc` with something other than `path:N`
    must produce the unknown state, never a number derived from a guess."""
    calls: list[list[str]] = []

    def _fake(args, timeout: int = 30) -> str | None:
        calls.append(list(args))
        if "-rc" in args:
            return "a.txt:12:alpha\nnot a census line at all\n"
        return "".join(f"a.txt:{i}:alpha\n" for i in range(1, 6))

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake)
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert any("-rc" in c for c in calls)
    assert "total unknown" in out
    assert _reported_total(out) is None


def test_a_unicode_decimal_in_the_census_is_not_read_as_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class #1727 and #1748 exist for, aimed at this parser. Every line
    here parses under `str.isdigit()` - which would sum to 11, clear the
    "greater than the rows shown" bar, and print a total nobody counted. `int()`
    converts U+0662, so the wrongness is silent and no crash test can see it.

    The rest of the census is deliberately ordinary: strip the Arabic-Indic
    digit and this fixture produces a *counted* receipt, so the test fails for
    the one reason it is about."""
    def _fake(args, timeout: int = 30) -> str | None:
        if "-rc" in args:
            return "a.txt:9\nb.txt:٢\n"
        return "".join(f"a.txt:{i}:alpha\n" for i in range(1, 6))

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake)
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert "total unknown" in out, (
        "U+0662 was read as 2: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )
    assert _reported_total(out) is None


def test_the_unicode_fixture_would_otherwise_produce_a_counted_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the test above. With both counts in ASCII the same
    fixture yields `TRUNCATED, 11 matches total` - so the assertion up there is
    about the digit and not about some unrelated reason the census declined."""
    def _fake(args, timeout: int = 30) -> str | None:
        if "-rc" in args:
            return "a.txt:9\nb.txt:2\n"
        return "".join(f"a.txt:{i}:alpha\n" for i in range(1, 6))

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake)
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert _reported_total(out) == 11, out.splitlines()[0] if out else "<empty>"
    assert "scanned 2 files" in out


def test_a_census_that_contradicts_the_shown_rows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tree can change between the two passes. A total at or below the
    number of rows printed under it is not a total - it is a smaller, more
    confident wrong answer, and the honest unknown is the right output."""
    def _fake(args, timeout: int = 30) -> str | None:
        if "-rc" in args:
            return "a.txt:2\n"          # 2 total, under the 3 rows shown
        return "".join(f"a.txt:{i}:alpha\n" for i in range(1, 6))

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake)
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert "total unknown" in out
    assert _reported_total(out) is None
    # The census *ran* here. Reporting that it "returned no total" would be a
    # receipt misreporting its own mechanism, in the change that exists to stop
    # receipts doing that. Raised by the audit of this commit's first version.
    assert "refused as incoherent" in out, (
        f"the refusal is reported as a pass that never ran: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )
    assert "returned no total" not in out


def test_the_two_declines_do_not_share_one_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_no_census,
) -> None:
    """The control for the assertion above: a census that never answered gets
    the other sentence, so the two states cannot collapse into each other."""
    a = tmp_path / "a.txt"
    a.write_text("alpha\n", encoding="utf-8")
    rtk_no_census("".join(f"{a}:{i}:alpha\n" for i in range(1, 6)))
    monkeypatch.chdir(tmp_path)

    out = supertool.op_grep("alpha", ".", limit=3, no_auto_read=True)

    assert "returned no total" in out, (
        f"{out.splitlines()[0] if out else '<empty>'!r}")
    assert "refused as incoherent" not in out


def test_the_second_pass_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls,
) -> None:
    """The census is a second full scan of the tree. On a corpus where that is
    the wrong trade it has to be refusable - and refusing it must produce the
    honest unknown, not a silent return to the old aside."""
    _write_corpus(tmp_path, CORPUS_A)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        supertool, "_CONFIG",
        {"rtk": True, "builtin-ops": {"grep": {"count_truncated": 0}}})

    out = supertool.op_grep(NEEDLE, ".", limit=10, no_auto_read=True)

    assert rtk_real_grep.calls
    assert rtk_real_grep.census_calls == [], "census ran despite being switched off"
    assert "TRUNCATED" in out
    assert "total unknown" in out
    assert _reported_total(out) is None


# ---------------------------------------------------------------------------
# The census counts what the report shows, and nothing else
# ---------------------------------------------------------------------------


def test_the_census_does_not_count_excluded_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rtk_real_grep: _RealGrepCalls,
) -> None:
    """The default exclude list carries negations, so wildcard entries such as
    `*.pem` are withheld from the delegated argv entirely and the system grep
    really does read the file. The match pass drops it in `_rtk_drop_excluded`;
    the census has to drop it too, or the total counts matches the caller was
    told were not there and the denominator counts a file nobody searched.
    """
    _write_corpus(tmp_path, CORPUS_A)
    total, scanned = _expected(CORPUS_A)
    secret = tmp_path / "server.pem"
    secret.write_text("\n".join(f"{NEEDLE} secret {i}" for i in range(9)) + "\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    excl = supertool._get_exclude_paths("grep")
    assert supertool._is_excluded("server.pem", excl), "fixture premise broken"

    census = supertool._rtk_grep_census(NEEDLE, ".", excl)

    assert census is not None, "census declined on a tree it should have counted"
    assert census == (total, scanned), (
        f"census counted the excluded file: {census} != {(total, scanned)}"
    )
