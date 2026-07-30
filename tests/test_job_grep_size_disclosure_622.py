"""gl-job / gh-job `:grep:` must bound its own output and say when it did (#622).

The incident: `gl-job:6990710:grep:Owner component` was read as "13 components"
and the blast radius judged small. It was not. The op had emitted every match —
and the *consumer* silently cut the tail, because each matched line was a whole
assertion failure with rendered HTML and the dump ran to hundreds of KB.

So this is not the usual "the limit cut it and did not say so". It is worse:
nothing in the chain bounded the output, and nothing in the chain disclosed the
cut, so a partial read was indistinguishable from a complete one.

Three states, not two (docs/validators.md, "Declining instead of guessing"):

  - a result that fits says nothing extra — its silence is a positive claim
    that the list is whole;
  - a result cut by the size budget names the shortfall in real numbers, which
    ARE knowable here because the match count is computed before printing;
  - and a result cut by *size* must never dress itself up as a count limit.
    `raise :LIMIT, current 20` on an op that has no :LIMIT is a confidently
    wrong disclosure, which is worse than silence.

The API boundary stubbed here is `subprocess.run` — the glab call — not the
internal emitter, so the tests exercise the real main() end to end.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_job = _load("gitlab/job.py", "gitlab_job_622")
gh_job = _load("github/job.py", "github_job_622")

# One knob per preset, so a caller who hits the bound can lift it.
BUDGET_ENV = {"gl": "GL_JOB_GREP_MAX_BYTES", "gh": "GH_JOB_GREP_MAX_BYTES"}

PATTERN = "Owner component"
# ~4KB per matched line — a rendered-HTML assertion blob, as in the incident.
FAT_MATCH = f"Failed asserting {PATTERN} " + "<div class='x'>y</div>" * 180


def _trace(n_lines: int, every: int) -> tuple[list[str], int]:
    """Trace where every `every`-th line is a fat match. Returns (lines, matches)."""
    lines = [
        FAT_MATCH if i % every == 0 else f"ordinary log line {i}"
        for i in range(n_lines)
    ]
    return lines, sum(1 for ln in lines if PATTERN in ln)


def _fake_run(trace_lines: list[str]):
    trace = "\n".join(trace_lines) + "\n"
    meta = json.dumps({
        "name": "phpunit", "status": "failed", "stage": "test", "duration": 12.0,
        "web_url": "https://gitlab.example/job/1", "ref": "feature/x",
        "pipeline": {"id": 999},
        # github/job.py reads its own shape; supplying both keeps one stub.
        "conclusion": "failure", "workflowName": "ci", "displayTitle": "ci",
        "headBranch": "feature/x", "url": "https://github.example/job/1",
    })

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        joined = " ".join(str(a) for a in args)
        is_trace = "/trace" in joined or "log" in joined
        return subprocess.CompletedProcess(args, 0, trace if is_trace else meta, "")

    return run


def _run(mod, monkeypatch, capsys, trace_lines: list[str], budget: int | None):
    monkeypatch.setattr(sys, "argv", ["job.py", "123", "grep", PATTERN])
    monkeypatch.setattr(mod.subprocess, "run", _fake_run(trace_lines))
    key = BUDGET_ENV["gl" if mod is gl_job else "gh"]
    if budget is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, str(budget))
    rc = mod.main()
    return rc, capsys.readouterr().out


def _shown(out: str) -> int:
    """Matched lines actually printed (header line excluded)."""
    return sum(
        1 for ln in out.splitlines()
        if PATTERN in ln and not ln.lstrip().startswith("##")
    )


ALL_MODS = pytest.mark.parametrize("mod", [gl_job, gh_job], ids=["gl-job", "gh-job"])


# ---------------------------------------------------------------------------
# State 1 — it all fit. Silence is the positive claim.
# ---------------------------------------------------------------------------

@ALL_MODS
def test_untruncated_grep_says_nothing_extra(mod, monkeypatch, capsys) -> None:
    lines, matches = _trace(120, every=40)
    rc, out = _run(mod, monkeypatch, capsys, lines, budget=5_000_000)

    assert rc == 0
    assert _shown(out) == matches, "every match must be printed when nothing is cut"
    lowered = out.lower()
    assert "capped" not in lowered
    assert "not shown" not in lowered
    assert "shown —" not in out


# ---------------------------------------------------------------------------
# State 2 — the size budget bit. Name the shortfall, in real numbers.
# ---------------------------------------------------------------------------

@ALL_MODS
def test_size_capped_grep_names_the_shortfall(mod, monkeypatch, capsys) -> None:
    lines, matches = _trace(22_333, every=370)
    budget = 40_000
    rc, out = _run(mod, monkeypatch, capsys, lines, budget=budget)

    assert rc == 0
    shown = _shown(out)
    assert 0 < shown < matches, "the fixture must actually trigger the bound"

    # The shortfall is stated, and the two numbers are the true ones.
    assert f"{shown} of {matches} matching lines shown" in out, (
        f"expected an exact shortfall for {shown}/{matches}; got:\n"
        + "\n".join(ln for ln in out.splitlines() if "matching lines" in ln)
    )


@ALL_MODS
def test_size_capped_grep_actually_bounds_the_output(mod, monkeypatch, capsys) -> None:
    """The loud bug must not be traded for the quiet one: bound, then disclose.

    Emitting 277KB into a consumer that cuts at ~30KB is what produced the
    incident. A disclosure appended to an unbounded dump is cut along with it.
    """
    lines, _ = _trace(22_333, every=370)
    budget = 40_000
    _, out = _run(mod, monkeypatch, capsys, lines, budget=budget)

    # Header, footer and the final straddling line are allowed past the budget;
    # a whole extra fat line is not.
    assert len(out.encode()) < budget + 3 * len(FAT_MATCH)


@ALL_MODS
def test_disclosure_is_one_bounded_line(mod, monkeypatch, capsys) -> None:
    """#605: `+N more` is bounded vocabulary — never one line per dropped match."""
    lines, matches = _trace(22_333, every=370)
    _, out = _run(mod, monkeypatch, capsys, lines, budget=40_000)

    notes = [ln for ln in out.splitlines() if "matching lines shown" in ln]
    assert len(notes) == 1, f"disclosure must be a single line, got {len(notes)}"
    assert len(notes[0]) < 300


# ---------------------------------------------------------------------------
# State 3 — cut by size must not claim it was cut by a count.
# ---------------------------------------------------------------------------

@ALL_MODS
def test_size_cap_does_not_claim_a_count_limit(mod, monkeypatch, capsys) -> None:
    """`raise :LIMIT, current 20` on an op with no :LIMIT is worse than silence.

    Florian's surprise in #622 was that the cut fired far earlier than a count
    limit would suggest — because the lines were enormous. The disclosure must
    say size, and must point at the knob that actually governs it.
    """
    lines, _ = _trace(22_333, every=370)
    budget = 40_000
    _, out = _run(mod, monkeypatch, capsys, lines, budget=budget)

    note = next(ln for ln in out.splitlines() if "matching lines shown" in ln)
    assert "bytes" in note, "the cutter is a byte budget — say so"
    assert str(budget) in note, "name the budget that actually bit"
    assert BUDGET_ENV["gl" if mod is gl_job else "gh"] in note, "name the knob"
    assert ":LIMIT" not in note
    assert "limit 20" not in note


@ALL_MODS
def test_shortfall_count_is_exact_not_an_estimate(mod, monkeypatch, capsys) -> None:
    """Two budgets, two different true shortfalls — a hardcoded string can't pass.

    This is the "would it still pass if the code did nothing" guard: the numbers
    have to track the fixture, so neither a constant footer nor no footer works.
    """
    lines, matches = _trace(22_333, every=370)

    _, tight = _run(mod, monkeypatch, capsys, lines, budget=20_000)
    shown_tight = _shown(tight)
    _, loose = _run(mod, monkeypatch, capsys, lines, budget=120_000)
    shown_loose = _shown(loose)

    assert shown_tight < shown_loose < matches
    assert f"{shown_tight} of {matches} matching lines shown" in tight
    assert f"{shown_loose} of {matches} matching lines shown" in loose


@ALL_MODS
def test_header_match_count_stays_the_true_total(mod, monkeypatch, capsys) -> None:
    """The header counts the trace, not the dump — bounding must not corrupt it."""
    lines, matches = _trace(22_333, every=370)
    _, out = _run(mod, monkeypatch, capsys, lines, budget=40_000)

    header = next(ln for ln in out.splitlines() if ln.startswith("## grep "))
    assert f"{matches} matching lines" in header


# ---------------------------------------------------------------------------
# The disclosure must survive the cut it is describing.
# ---------------------------------------------------------------------------

@ALL_MODS
def test_warning_is_in_the_header_not_only_the_footer(mod, monkeypatch, capsys) -> None:
    """A trailing note is lost to the very truncation it exists to report.

    The budget here (64KB by default) is deliberately generous — wider than a
    consumer that cuts at a few tens of KB. So a footer-only disclosure is read
    by nobody in exactly the case that produced #622: the reader sees the head,
    the note was at the end, and the silence is back one layer further out.
    The head must carry the warning.
    """
    lines, matches = _trace(22_333, every=370)
    _, out = _run(mod, monkeypatch, capsys, lines, budget=40_000)

    head = out.encode()[:2048].decode("utf-8", "replace")
    assert "CAPPED" in head, f"head of output carries no warning:\n{head[:600]}"
    assert str(matches) in head, "the true total belongs in the head"


@ALL_MODS
def test_no_header_warning_when_nothing_was_cut(mod, monkeypatch, capsys) -> None:
    lines, _ = _trace(120, every=40)
    _, out = _run(mod, monkeypatch, capsys, lines, budget=5_000_000)
    assert "CAPPED" not in out
