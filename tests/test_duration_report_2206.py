"""A hot test found by a person reading a CI log tail is this repo's own defect

shape, one level up: `--durations=25` has printed since #891, and nothing
reads it (#2206). A test can grow to a fifth of a whole suite -- measured on
`claude-oss`'s own `test_shell_probe.py`, 43.92s against ~6.5s for the next
slowest -- and the only detector was luck.

## Why junit.xml, not `--durations` stdout text

The issue's own acceptance criteria talk about "the durations output", and a
first draft of this module parsed pytest's `--durations=N` text block
directly. That format is not machine-shaped: pytest reorders and wraps it
under `-q`, its wording has changed across major versions, and this repo's
own `tests.yml` already runs the suite under `-n auto` (`pyproject.toml`
`addopts`), where a naive per-worker parse would double-count or miss
entries depending on how xdist interleaves worker output.

`--junit-xml=junit.xml` is already a flag on the same pytest invocation
(`.github/workflows/tests.yml`, the "Run tests" step), already the input to
`junit_summary.py` next to it, and already immune to both problems: pytest's
junitxml plugin aggregates every worker into one `<testsuite>` regardless of
`-n auto`, one `<testcase time="...">` per test is unambiguous, and the
`<testsuite time="...">` attribute is the run's real wall-clock (recorded at
`pytest_sessionstart`/`pytest_sessionfinish`, not summed from testcases) --
which is exactly "total suite time" under parallel execution: if one test
pins a worker for 44s while eleven others finish in 2s, wall clock is ~44s
and that test's share approaches 100%, which is the shape the issue exists to
surface. A percentage against that denominator, not a raw second count,
because `docs/contributing.md`'s own worked case for this "travels between
machines" argument.

## The three states

`parse_junit` raises `DurationsUnavailable` for every way the file can fail
to answer -- absent, unparseable XML, no `<testsuite>`, no numeric
`time=`, or a `<testsuite>` with no testcase carrying a parseable time --
and `render()` turns that into `could-not-measure` rather than printing an
empty top-N list that would read exactly like "no hot test" (the
`docs/validators.md` "declining instead of guessing" shape, and the
scenario the issue names by name). `no-baseline` and `measured` are the two
states once parsing succeeds, split on whether
`.github/duration-baseline.json` exists and parses -- and a baseline that
exists but is unreadable (bad JSON, or missing the one key read) renders as
`no-baseline` too, with a note that it exists and could not be read, on the
same "never silently render as no change" rule.
"""
from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO / ".github" / "scripts" / "duration_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("duration_report_2206", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dr = _load()


def _junit(tmp_path: Path, suite_time: float, cases: list[tuple[str, float]]) -> Path:
    """Write a minimal but real pytest-shaped junit.xml, one <testsuite>."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", {
        "name": "pytest", "time": str(suite_time), "tests": str(len(cases)),
    })
    for ident, t in cases:
        classname, _, name = ident.rpartition(".")
        ET.SubElement(suite, "testcase", {
            "classname": classname, "name": name or ident, "time": str(t),
        })
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(str(path), encoding="unicode")
    return path


# --- parse_junit: the primitive every state is built on ---------------------

def test_parse_junit_reads_total_time_and_per_test_times(tmp_path):
    path = _junit(tmp_path, 100.0, [("tests.test_a.test_x", 44.0),
                                     ("tests.test_b.test_y", 6.5),
                                     ("tests.test_c.test_z", 1.0)])
    total, cases = dr.parse_junit(path)
    assert total == 100.0
    assert ("tests.test_a.test_x", 44.0) in cases
    assert len(cases) == 3


def test_parse_junit_raises_when_file_is_absent(tmp_path):
    missing = tmp_path / "no-such-junit.xml"
    try:
        dr.parse_junit(missing)
        raise AssertionError("expected DurationsUnavailable")
    except dr.DurationsUnavailable as exc:
        assert "no-such-junit.xml" in str(exc)


def test_parse_junit_raises_on_malformed_xml(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text("this is not xml <<<", encoding="utf-8")
    try:
        dr.parse_junit(path)
        raise AssertionError("expected DurationsUnavailable")
    except dr.DurationsUnavailable as exc:
        assert "not parseable" in str(exc) or "XML" in str(exc)


def test_parse_junit_raises_when_no_testcase_has_a_time(tmp_path):
    root = ET.Element("testsuites")
    ET.SubElement(root, "testsuite", {"name": "pytest", "time": "12.0"})
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(str(path), encoding="unicode")
    try:
        dr.parse_junit(path)
        raise AssertionError("expected DurationsUnavailable")
    except dr.DurationsUnavailable:
        pass


# --- share() -----------------------------------------------------------------

def test_share_is_a_percentage_of_total():
    assert dr.share(44.0, 100.0) == 44.0


def test_share_is_zero_when_total_is_not_positive():
    assert dr.share(5.0, 0.0) == 0.0


# --- render(): the three states, each distinguishable in plain output -------

def test_render_state_is_could_not_measure_when_junit_is_missing(tmp_path):
    lines = dr.render(tmp_path / "junit.xml", tmp_path / "baseline.json")
    text = "\n".join(lines)
    assert "state: could-not-measure" in text
    assert "junit.xml" in text


def test_render_state_is_could_not_measure_when_junit_is_malformed(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text("<<< not xml", encoding="utf-8")
    lines = dr.render(junit, tmp_path / "baseline.json")
    text = "\n".join(lines)
    assert "state: could-not-measure" in text


def test_render_state_is_no_baseline_when_baseline_file_is_absent(tmp_path):
    junit = _junit(tmp_path, 100.0, [("tests.test_a.test_x", 44.0),
                                      ("tests.test_b.test_y", 6.5)])
    lines = dr.render(junit, tmp_path / "no-such-baseline.json")
    text = "\n".join(lines)
    assert "state: no-baseline" in text
    assert "compared to nothing" in text
    assert "no change" not in text.lower()


def test_render_state_is_no_baseline_when_baseline_file_is_malformed(tmp_path):
    junit = _junit(tmp_path, 100.0, [("tests.test_a.test_x", 44.0)])
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{not json", encoding="utf-8")
    lines = dr.render(junit, baseline)
    text = "\n".join(lines)
    assert "state: no-baseline" in text
    assert "exists" in text and "could not" in text.lower()


def test_render_state_is_measured_when_baseline_is_present_and_valid(tmp_path):
    junit = _junit(tmp_path, 100.0, [("tests.test_a.test_x", 44.0),
                                      ("tests.test_b.test_y", 6.5)])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"slowest_share_pct": 40.0}), encoding="utf-8")
    lines = dr.render(junit, baseline)
    text = "\n".join(lines)
    assert "state: measured" in text
    assert "44.0" in text or "44.00" in text
    assert "baseline" in text.lower()


def test_render_lists_top_n_durations_slowest_first(tmp_path):
    junit = _junit(tmp_path, 60.0, [("t.a", 1.0), ("t.b", 30.0), ("t.c", 15.0)])
    lines = dr.render(junit, tmp_path / "no-baseline.json", top=2)
    text = "\n".join(lines)
    idx_b = text.index("t.b")
    idx_c = text.index("t.c")
    assert idx_b < idx_c
    assert "t.a" not in text  # top=2 excludes the third-slowest


def test_render_marks_a_test_that_crosses_the_interesting_threshold(tmp_path):
    hot_dir = tmp_path / "hot"
    hot_dir.mkdir()
    dominant = _junit(hot_dir, 50.0, [("t.hot", 44.0), ("t.cold", 1.0)])
    lines_hot = dr.render(dominant, hot_dir / "no-baseline.json", threshold=10.0)
    text_hot = "\n".join(lines_hot)
    assert dr.HOT_MARKER in text_hot

    calm_dir = tmp_path / "calm"
    calm_dir.mkdir()
    calm = _junit(calm_dir, 50.0, [("t.a", 3.0), ("t.b", 3.0), ("t.c", 3.0)])
    lines_calm = dr.render(calm, calm_dir / "no-baseline.json", threshold=10.0)
    text_calm = "\n".join(lines_calm)
    assert dr.HOT_MARKER not in text_calm


def test_render_never_fails_the_run_it_reports_on(tmp_path):
    """A reporter that can crash the build is not a report -- it is a gate

    wearing a report's name, and this issue exists specifically to forbid
    that (#2206: "Do not add to a wall-clock gate")."""
    # No junit.xml, no baseline: worst case, and render() must still return
    # plain text rather than raising.
    lines = dr.render(tmp_path / "missing.xml", tmp_path / "missing.json")
    assert isinstance(lines, list)
    assert all(isinstance(line, str) for line in lines)


def test_main_exits_zero_even_when_junit_is_absent(tmp_path, capsys):
    rc = dr.main(["duration_report.py", str(tmp_path / "no.xml"),
                  "--baseline", str(tmp_path / "no.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "could-not-measure" in out
