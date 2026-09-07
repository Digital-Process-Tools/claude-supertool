"""`-n auto` decides worker count on every leg and nothing said it (#2345).

`pyproject.toml`'s `addopts` carries `-n auto`, and `tests.yml`'s "Run tests"
step overrode the marker expression and `--cov` but not `-n`, so all twelve
CI legs resolved a worker count through xdist that nobody read. `-q` on that
same step additionally suppressed xdist's own "N workers [M items]" banner --
confirmed locally, same invocation as the workflow's "Run tests" step (marker
expr, `--tb=no`, `--no-cov`, `--durations=25`, `--junit-xml`): the banner
prints without `-q` and disappears the instant `-q` is added.

## The two-part fix, and why it is two parts

A coordinator message mid-implementation raised a config-shaped alternative
the issue body does not consider: since this repo already runs `-n auto` on
twelve legs, it can *observe* the resolved count directly (xdist's own "N
workers [M items]" banner) rather than predict one with a transcription of
xdist's resolution hook. That was reproduced here on this repo's real
`pytest`/`pytest-xdist` versions (9.1.1 / 3.8.0) and the real "Run tests"
flags, not a throwaway suite:

    -n auto (no -q)     -> "created: 3/3 workers" then "3 workers [54 items]"
    -n auto -q          -> "bringing up nodes..." and no count anywhere

Grepping `.github/` and `tests/` confirms nothing downstream parses that
step's raw stdout -- `junit_summary.py` and `duration_report.py` both read
`junit.xml`, never this stream -- so dropping `-q` is a config-only change
with no blast radius. `tests.yml`'s "Run tests" step now omits `-q`; this is
therefore the PRIMARY, measured answer to the issue and is pinned below by
asserting `-q` is absent from that step's `run:` line.

What the banner cannot say is WHICH source resolved the number -- and that
is the one thing the issue's own worry (a transitive `psutil` dependency
silently switching `-n auto` from counting logical cores to counting
*physical* ones, halving parallelism on an SMT runner with no other visible
cause) needs named rather than merely observed as a smaller N. That is what
`.github/scripts/print_worker_sizing.py` is for, kept as a small,
report-only complement to the banner rather than a bigger replacement of it.

A second coordinator message pointed at `Digital-Process-Tools/claude-oss`'s
`scripts/doctor.py` (`xdist_auto_workers`) as prior art with a stronger
transcription and three fixes worth taking regardless of which repo's shape
is used: (1) an unparseable `PYTEST_XDIST_AUTO_NUM_WORKERS` is xdist's own
warn-and-ignore case, not a value to silently fall through past without
saying so; (2) whether `xdist` is importable at all should be reported,
because a worker count is a number about nothing on an interpreter that
cannot run `-n auto`; (3) the None-not-0/1 rule for "nothing answered" was
already right in the salvaged script and stays right. All three are
implemented in `print_worker_sizing.py` and pinned below.

## Disclosure

The `resolve()`/`is_xdist_installed()`/`render()` unit tests below were NOT
seen red first in the "watch a broken script fail" sense -- the script's
core shape was salvaged already-written from a prior session's worktree.
They *were* driven properly for the two coordinator-requested fixes (the
unparseable-env-var note, the xdist-installed check): each assertion below
that exercises those two paths was written and run against the
pre-coordinator-message version of the script first, where it failed
(`ValueError` silently swallowed with no note; `render()` took one argument
and had no not-installed branch), and passes only against the rewritten
version. The workflow-wiring tests are genuine TDD in the same sense:
RED against the tree this PR started from (no step called the script, and
`-q` was still on the "Run tests" line), GREEN only once both workflow edits
landed.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO / ".github" / "scripts" / "print_worker_sizing.py"
WORKFLOW_PATH = REPO / ".github" / "workflows" / "tests.yml"


def _load():
    spec = importlib.util.spec_from_file_location("print_worker_sizing_2345", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


# ---------------------------------------------------------------------------
# resolve(): the four-source walk and the "unknown" third state
# ---------------------------------------------------------------------------


def test_env_var_wins_when_set_and_parseable():
    resolution = mod.resolve(env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "7"})
    assert resolution == ("PYTEST_XDIST_AUTO_NUM_WORKERS env var", 7, None)


def test_env_var_unparseable_falls_through_but_says_the_cap_is_not_in_effect(monkeypatch):
    """The coordinator-requested fix: xdist warns-and-ignores an unparseable

    value rather than treating it as fatal, and a reader must be told the
    cap did NOT apply rather than seeing a number with no explanation of
    where it came from.
    """
    monkeypatch.setattr(mod, "_psutil_physical_count", lambda: None)
    monkeypatch.setattr(mod, "_affinity_count", lambda: None)
    monkeypatch.setattr(mod, "_os_cpu_count", lambda: 5)
    source, count, note = mod.resolve(env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "not-a-number"})
    assert (source, count) == ("os.cpu_count()", 5)
    assert note is not None
    assert "not-a-number" in note
    assert "NOT in effect" in note


def test_psutil_source_used_when_env_var_absent(monkeypatch):
    monkeypatch.setattr(mod, "_psutil_physical_count", lambda: 4)
    monkeypatch.setattr(mod, "_affinity_count", lambda: 99)
    monkeypatch.setattr(mod, "_os_cpu_count", lambda: 99)
    resolution = mod.resolve(env={})
    assert resolution == ("psutil.cpu_count(logical=False)", 4, None)


def test_affinity_used_when_psutil_absent(monkeypatch):
    monkeypatch.setattr(mod, "_psutil_physical_count", lambda: None)
    monkeypatch.setattr(mod, "_affinity_count", lambda: 6)
    monkeypatch.setattr(mod, "_os_cpu_count", lambda: 99)
    resolution = mod.resolve(env={})
    assert resolution == ("os.sched_getaffinity(0)", 6, None)


def test_os_cpu_count_is_the_last_resort(monkeypatch):
    monkeypatch.setattr(mod, "_psutil_physical_count", lambda: None)
    monkeypatch.setattr(mod, "_affinity_count", lambda: None)
    monkeypatch.setattr(mod, "_os_cpu_count", lambda: 2)
    resolution = mod.resolve(env={})
    assert resolution == ("os.cpu_count()", 2, None)


def test_none_of_the_sources_answer_reports_unknown_not_a_number(monkeypatch):
    """The load-bearing case: nothing answered must render distinguishably

    from any real worker count -- not 0, not None silently, but a named
    third state ('none', None, <explanatory note>).
    """
    monkeypatch.setattr(mod, "_psutil_physical_count", lambda: None)
    monkeypatch.setattr(mod, "_affinity_count", lambda: None)
    monkeypatch.setattr(mod, "_os_cpu_count", lambda: None)
    source, count, note = mod.resolve(env={})
    assert source == "none"
    assert count is None
    assert note is not None and "unknown" in note


# ---------------------------------------------------------------------------
# is_xdist_installed(): a count is meaningless on an interpreter without xdist
# ---------------------------------------------------------------------------


def test_xdist_installed_reflects_a_real_import(monkeypatch):
    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: object())
    assert mod.is_xdist_installed() is True
    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: None)
    assert mod.is_xdist_installed() is False


def test_xdist_installed_never_raises_on_a_hostile_import_system(monkeypatch):
    def _boom(name):
        raise RuntimeError("broken meta path finder")

    monkeypatch.setattr(mod.importlib.util, "find_spec", _boom)
    assert mod.is_xdist_installed() is False


# ---------------------------------------------------------------------------
# render(): the three states must never look alike
# ---------------------------------------------------------------------------


def test_render_of_a_real_count_names_the_source():
    text = mod.render(("os.sched_getaffinity(0)", 6, None), xdist_installed=True)
    assert text == "xdist -n auto would resolve to 6 worker(s) (source: os.sched_getaffinity(0))"


def test_render_of_unknown_never_looks_like_a_count():
    """A reader must be able to tell 'nobody could tell' from a real number

    at a glance -- the whole point of the issue. `render()`'s unknown text
    must not contain a bare digit that could be misread as a worker count.
    """
    text = mod.render(("none", None, "worker sizing unknown -- nothing answered"), xdist_installed=True)
    assert "unknown" in text
    assert not re.search(r"resolve to \d+ worker", text)


def test_render_when_xdist_is_not_installed_reports_that_not_a_count(monkeypatch):
    """The second coordinator-requested fix: a worker count is a number

    about nothing on an interpreter that cannot import xdist at all -- this
    must render as its own state, not silently coincide with a real count
    of 1 or a made-up 0.
    """
    text = mod.render(("os.cpu_count()", 1, None), xdist_installed=False)
    assert "not importable" in text
    assert not re.search(r"resolve to \d+ worker", text)


def test_main_exits_zero_always(capsys):
    """The module docstring is explicit this never gates a build."""
    exit_code = mod.main()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() != ""


# ---------------------------------------------------------------------------
# Workflow wiring: the "Run tests" step must actually print a worker count
# ---------------------------------------------------------------------------


def test_tests_workflow_runs_the_worker_sizing_script_before_pytest():
    """RED on the tree this PR started from: `.github/scripts/print_worker_sizing.py`

    existed but nothing under `.github/workflows/` referenced it. GREEN once
    a step invoking the script is added ahead of "Run tests" on the `pytest`
    job.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "print_worker_sizing.py" in text, (
        "tests.yml has no step invoking .github/scripts/print_worker_sizing.py"
    )

    sizing_pos = text.find("print_worker_sizing.py")
    run_tests_pos = text.find('name: Run tests (excludes slow and benchmark')
    assert run_tests_pos != -1, "the 'Run tests' step this must precede was not found"
    assert sizing_pos < run_tests_pos, (
        "print_worker_sizing.py must run BEFORE the 'Run tests' step"
    )


def test_run_tests_step_no_longer_suppresses_the_xdist_banner_with_q():
    """RED on the tree this PR started from: the pytest invocation carried

    `-q`, which measurably (see module docstring) suppresses xdist's own "N
    workers [M items]" banner. GREEN once `-q` is removed from that line --
    the primary, measured fix for #2345, config-only and confirmed to have
    no downstream reader of this step's raw stdout.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"run: python -X utf8 -m pytest -m 'not slow and not benchmark'[^\n]*",
        text,
    )
    assert match is not None, "the 'Run tests' pytest invocation was not found"
    run_line = match.group(0)
    assert "--junit-xml=junit.xml" in run_line, "sanity: matched the wrong line"
    tokens = run_line.split()
    assert "-q" not in tokens, (
        f"the 'Run tests' step still passes -q, which suppresses xdist's own "
        f"worker-count banner: {run_line!r}"
    )
