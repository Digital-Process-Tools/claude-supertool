"""Tests for parallel execution mode (SUPERTOOL_PARALLEL=1)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import supertool


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Subprocess env that works on POSIX + Windows.

    Inherit the parent env (Windows Python needs SYSTEMROOT, APPDATA, etc.
    to start at all; pinning a minimal POSIX-only env breaks the runner).
    Force PYTHONIOENCODING=utf-8 so supertool's `→` arrow doesn't crash
    the default cp1252 codec on Windows. Strip SUPERTOOL_PARALLEL so
    callers control it explicitly via `extra`.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("SUPERTOOL_PARALLEL", None)
    # SUPERTOOL_NO_RTK=1 is set in conftest.pytest_configure (covers all
    # subprocess-spawning tests) — env.copy() picks it up here. Without it,
    # supertool delegates `read` to rtk and rtk's output format (`1 │ hi`)
    # breaks the byte-identical assertions below.
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def test_parallel_safe_includes_read_only_ops() -> None:
    # `blame` sat in this list until #1285 — it left the dispatcher in b4099a5
    # (moved to the git preset as `git-blame`) and the assertion went on
    # holding, because `_is_parallel_safe` answers about the set, not about
    # whether anything dispatches the name.
    for op in ("read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
               "map", "tree", "around", "around_line", "between", "diff",
               "version"):
        assert supertool._is_parallel_safe(f"{op}:foo")


def test_parallel_safe_excludes_mutating_ops() -> None:
    for op in ("edit", "replace", "replace_dry", "replace_lines", "paste", "append", "vim"):
        assert not supertool._is_parallel_safe(f"{op}:a:b:c")
        assert not supertool._is_parallel_safe(f"{op}:::a:::b:::c")


def test_parallel_safe_excludes_unknown_ops() -> None:
    """Custom ops (mysql_write, mr, phpstan, etc.) — unknown to safety set."""
    assert not supertool._is_parallel_safe("mysql_write:UPDATE x SET y=1")
    assert not supertool._is_parallel_safe("mr:.max/mr.md|1h|labels")
    assert not supertool._is_parallel_safe("phpstan:src/")


def test_parallel_safe_handles_triple_colon() -> None:
    assert supertool._is_parallel_safe("read:::path")
    assert not supertool._is_parallel_safe("edit:::a:::b:::c")


def test_parallel_safe_handles_malformed() -> None:
    assert not supertool._is_parallel_safe("")
    assert not supertool._is_parallel_safe("::just-colons")


# ---------------------------------------------------------------------------
# End-to-end via subprocess — verify ordering and correctness
# ---------------------------------------------------------------------------

def _supertool_path() -> Path:
    return Path(__file__).parent.parent / "supertool.py"


# `read`'s meta line ends in a compact suffix, and one of its tokens is
# `git?` — supertool's disclosure that the working-tree lookup DECLINED, i.e.
# state unknown rather than clean (#705). In a `tmp_path` outside any repo the
# steady-state answer is no token at all: git exits non-zero with "not a git
# repository", which `_path_meta_suffix` recognises and stays silent about. The
# token appears only when that git call fails some OTHER way — its 2s timeout,
# or a non-zero exit whose stderr says something else (a dubious-ownership
# refusal, a held index lock). Both were reproduced against the product on
# macOS by putting a shim `git` on PATH: `sleep 4` and `exit 128` each turn
# `(1 lines, 10 bytes) crlf` into `(1 lines, 10 bytes) crlf git?`.
#
# `seq` and `par` are two independent subprocesses, so either can trip that and
# the other not — and the parallel one spawns four `git status` at once, which
# is the side more likely to hit the timeout. That is what reddened only
# `pytest (windows-latest, 3.10)` on the docs-only #1362: expected
# `crlf git?`, got `crlf` (#1364). Comparing the two receipts byte-for-byte
# asserts an environment condition as if it were a product verdict — this
# repo's own defect class, relocated into the harness (#1205, #1218, #1360).
#
# So the token is normalised out of BOTH sides. Everything else in the receipt,
# including the order these tests exist to check, is still compared byte-exact.
_META_LINE = re.compile(
    r"^\(\d+ lines, \d+ bytes(?:, modified \d+[smhd] ago)?\)"
)

# The freshness note itself (#1379) -- ", modified Xs/Xm/Xh/Xd ago" -- computed
# from wall-clock elapsed time between the read and the moment `now` is taken.
# Two subprocess runs of the identical read a couple of seconds apart --
# exactly what the sequential-then-parallel comparison below does -- can
# legitimately disagree on this note alone, worse on a loaded CI runner. #2347
# reddened `pytest (windows-latest, 3.10)` on master over exactly this: same
# file, same content, same order, different age. Mirrors `_short_age` in
# `_supertool.py` (`{int}s`/`{int}m`/`{int}h`/`{int}d`) precisely enough to
# match it and nothing else. Scoped to the meta-line prefix, same as
# `_META_LINE` above, so a body line that happens to spell the same words
# out is left alone -- the same anti-vacuity concern `_without_decline_token`
# is built to satisfy.
_META_LINE_PREFIX = re.compile(r"^\(\d+ lines, \d+ bytes")
_FRESHNESS_NOTE = re.compile(r", modified \d+[smhd] ago")


def _without_decline_token(receipt: str) -> str:
    """Erase the `git?` decline token from every meta line. Nothing else."""
    token = " " + supertool.PATH_META_UNKNOWN
    out = []
    for line in receipt.split("\n"):
        if _META_LINE.match(line):
            line = line.replace(token, "")
        out.append(line)
    return "\n".join(out)


def _without_freshness_note(receipt: str) -> str:
    """Erase the `, modified Xs/Xm/Xh/Xd ago` suffix from every meta line.

    Nothing else -- same scoping discipline as `_without_decline_token`: only
    a line that opens with the `(N lines, N bytes...)` parenthetical is a
    candidate, so a body line that happens to spell out the same words (an
    agent quoting this very feature back, say) is content, not a disclosure,
    and is left untouched.
    """
    out = []
    for line in receipt.split("\n"):
        if _META_LINE_PREFIX.match(line):
            line = _FRESHNESS_NOTE.sub("", line, count=1)
        out.append(line)
    return "\n".join(out)


def _normalized_receipt(receipt: str) -> str:
    """Strip every source of receipt-to-receipt timing noise a seq-vs-par
    comparison is not supposed to be sensitive to -- the git-status decline
    token (#1364) and the read freshness note (#2347) -- leaving order,
    content and formatting exactly as load-bearing as before.
    """
    return _without_decline_token(_without_freshness_note(receipt))


def _run(argv: list[str], parallel: bool, tmp_path: Path) -> str:
    extra = {"SUPERTOOL_PARALLEL": "4"} if parallel else None
    result = subprocess.run(
        [sys.executable, str(_supertool_path()), *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=_subprocess_env(extra), cwd=str(tmp_path), errors="replace",
    )
    return result.stdout


def test_decline_token_is_normalised_out_but_order_is_not() -> None:
    """`_without_decline_token` erases the decline token and nothing else.

    Pins the normaliser used by the seq/par comparisons below. The last two
    assertions are the anti-vacuity clauses: a normaliser that returned its
    input would fail the first, and one that flattened the whole meta line
    would pass the first and fail the third.
    """
    token = supertool.PATH_META_UNKNOWN
    declined = f"--- read:f0.txt ---\n(1 lines, 10 bytes) crlf {token}\n1→content0\n"
    certain = "--- read:f0.txt ---\n(1 lines, 10 bytes) crlf\n1→content0\n"
    assert _without_decline_token(declined) == _without_decline_token(certain)
    assert _without_decline_token(certain) == certain
    # Order is still load-bearing after normalisation.
    reordered = "--- read:f1.txt ---\n(1 lines, 10 bytes) crlf\n1→content0\n"
    assert _without_decline_token(declined) != _without_decline_token(reordered)
    # The token is only ever a meta-line suffix; a body line that happens to
    # contain it is content, not a disclosure.
    body = f"--- read:f0.txt ---\n(1 lines, 10 bytes) crlf\n1→ask {token} yes\n"
    assert _without_decline_token(body) == body


def test_freshness_note_is_normalised_out_but_order_is_not() -> None:
    """`_without_freshness_note` erases the freshness suffix and nothing else.

    Same shape as `test_decline_token_is_normalised_out_but_order_is_not`,
    for the other source of receipt-to-receipt timing noise (#2347). The
    anti-vacuity clauses are the equality against `fresh == bare`, which a
    normaliser that returned its input unchanged would fail, and the
    inequality against `reordered`/`changed`, which one that flattened the
    whole meta line -- or the whole receipt -- would pass by accident. The
    final `body` check is the scoping guard: the phrase spelled out in
    content, not in a meta line, must survive untouched.
    """
    fresh = "--- read:f0.txt ---\n(1 lines, 10 bytes, modified 0s ago) crlf\n1\u2192content0\n"
    stale = "--- read:f0.txt ---\n(1 lines, 10 bytes, modified 47s ago) crlf\n1\u2192content0\n"
    hours = "--- read:f0.txt ---\n(1 lines, 10 bytes, modified 3h ago) crlf\n1\u2192content0\n"
    bare = "--- read:f0.txt ---\n(1 lines, 10 bytes) crlf\n1\u2192content0\n"
    assert _without_freshness_note(fresh) == _without_freshness_note(stale)
    assert _without_freshness_note(fresh) == _without_freshness_note(hours)
    assert _without_freshness_note(fresh) == bare
    assert _without_freshness_note(bare) == bare
    # Order is still load-bearing after normalisation.
    reordered = "--- read:f1.txt ---\n(1 lines, 10 bytes, modified 0s ago) crlf\n1\u2192content0\n"
    assert _without_freshness_note(fresh) != _without_freshness_note(reordered)
    # Content is still load-bearing after normalisation.
    changed = "--- read:f0.txt ---\n(1 lines, 10 bytes, modified 0s ago) crlf\n1\u2192content9\n"
    assert _without_freshness_note(fresh) != _without_freshness_note(changed)
    # The phrase is only ever a meta-line suffix; a body line that happens to
    # spell it out is content, not a disclosure.
    body = "--- read:f0.txt ---\n(1 lines, 10 bytes) crlf\n1\u2192wait, modified 5s ago now\n"
    assert _without_freshness_note(body) == body


def test_parallel_preserves_input_order_despite_freshness_skew(
    tmp_path: Path,
) -> None:
    """Deterministic reproduction of #2347.

    `test_parallel_preserves_input_order` below relies on real elapsed
    wall-clock time to trip the freshness-note race, which is exactly why it
    took a loaded Windows CI runner to show it and stayed invisible
    everywhere else. Here the skew is manufactured with `os.utime` -- the
    files are backdated between the two runs -- so the two freshness notes
    are guaranteed to differ regardless of how fast this machine actually is.
    """
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"content{i}\n")
    argv = [f"read:f{i}.txt" for i in range(5)]
    seq = _run(argv, parallel=False, tmp_path=tmp_path)
    # 45s, not a token 2-5s margin: `_short_age`'s seconds-branch spans
    # [0, 90) and reports the integer second exactly, so a margin close to
    # the gap #2347 actually observed (2s on a loaded windows-latest runner)
    # leaves a real chance the sequential subprocess itself stalls long
    # enough that its own freshness note lands on the same second this
    # backdates the parallel run to -- which would make the comparison below
    # pass whether or not the normalizer does anything, on the exact kind of
    # loaded runner this test exists to cover. 45s keeps well clear of both
    # that stall range and the 90s boundary where the unit itself changes.
    past = time.time() - 45
    for i in range(5):
        os.utime(tmp_path / f"f{i}.txt", (past, past))
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    assert _normalized_receipt(seq) == _normalized_receipt(par)


def test_parallel_preserves_input_order(tmp_path: Path) -> None:
    """Output must match input order, not completion order."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"content{i}\n")
    argv = [f"read:f{i}.txt" for i in range(5)]
    seq = _run(argv, parallel=False, tmp_path=tmp_path)
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    assert _normalized_receipt(seq) == _normalized_receipt(par)


def test_parallel_falls_back_to_sequential_for_mixed_batch(
    tmp_path: Path,
) -> None:
    """Any non-safe op present → whole batch runs sequentially.

    We can't observe sequential vs parallel directly, but the output should
    still be byte-identical between modes when correct.
    """
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    # `replace_dry` is not in the safe set
    argv = ["read:x.txt", "replace_dry:::foo:::FOO:::."]
    seq = _run(argv, parallel=False, tmp_path=tmp_path)
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    assert _normalized_receipt(seq) == _normalized_receipt(par)


def test_parallel_single_op_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi\n")
    seq = _run(["read:x.txt"], parallel=False, tmp_path=tmp_path)
    par = _run(["read:x.txt"], parallel=True, tmp_path=tmp_path)
    assert _normalized_receipt(seq) == _normalized_receipt(par)


def test_parallel_disabled_by_default(tmp_path: Path) -> None:
    """Without env var = sequential (no SUPERTOOL_PARALLEL set)."""
    f = tmp_path / "x.txt"
    f.write_text("hi\n")
    result = subprocess.run(
        [sys.executable, str(_supertool_path()), "read:x.txt"],
        capture_output=True, text=True, encoding="utf-8",
        env=_subprocess_env(),  # no SUPERTOOL_PARALLEL
        cwd=str(tmp_path), errors="replace",
    )
    assert "1→hi" in result.stdout


def test_parallel_workers_int_from_json(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 4})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 4


def test_parallel_workers_zero_disables(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 0})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 0


def test_parallel_workers_bool_compat(monkeypatch) -> None:
    """Back-compat: `true` → 4, `false` → 0."""
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": True})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 4
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": False})
    assert supertool._parallel_workers() == 0


def test_parallel_workers_env_overrides_json(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 8})
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "0")
    assert supertool._parallel_workers() == 0
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "3")
    assert supertool._parallel_workers() == 3


def test_parallel_workers_default_zero(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 0


def test_parallel_workers_invalid_str_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "garbage")
    assert supertool._parallel_workers() == 0


def test_parallel_error_isolation(tmp_path: Path) -> None:
    """One failing op shouldn't corrupt other ops' output."""
    (tmp_path / "good.txt").write_text("ok\n")
    argv = ["read:good.txt", "read:nope.txt", "read:good.txt"]
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    # All three headers present, in order
    headers = [line for line in par.splitlines() if line.startswith("--- ")]
    assert headers == [
        "--- read:good.txt ---",
        "--- read:nope.txt ---",
        "--- read:good.txt ---",
    ]
    # Middle one is the error
    assert "ERROR: file not found" in par
