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
    total, cases, dropped = dr.parse_junit(path)
    assert total == 100.0
    assert dropped == []
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


# --- review findings (#2206 self-review): each is a regression test for a
# mechanism a reviewer or the auditor reproduced against an earlier version
# of this module, not a hypothetical. --------------------------------------

def test_parse_junit_reads_every_testsuite_not_just_the_first(tmp_path):
    """A `<testsuites>` root can carry more than one `<testsuite>` sibling.

    `Element.find()` returns only the first match -- reading just it would
    silently drop a whole suite's total and testcases rather than raising
    `could-not-measure`, which is a confidently wrong report, not an absent
    one (the reviewer's finding #1)."""
    root = ET.Element("testsuites")
    for name, total, cases in (
        ("gw0", 5.0, [("t.a", 4.9)]),
        ("gw1", 50.0, [("t.b", 49.9)]),
    ):
        suite = ET.SubElement(root, "testsuite", {"name": name, "time": str(total)})
        for ident, t in cases:
            classname, _, cname = ident.rpartition(".")
            ET.SubElement(suite, "testcase", {
                "classname": classname, "name": cname or ident, "time": str(t)})
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(str(path), encoding="unicode")

    total, cases, dropped = dr.parse_junit(path)
    assert total == 55.0
    assert dropped == []
    assert ("t.b", 49.9) in cases
    assert ("t.a", 4.9) in cases


def test_parse_junit_rejects_a_zero_total_rather_than_reporting_a_zero_share(tmp_path):
    """A degenerate `<testsuite time="0.0">` must not render identically to a

    genuinely negligible share -- `share()` returning 0.0 for both is the
    exact absence-as-clean-result shape this module exists to avoid (the
    auditor's Class A finding)."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", {"name": "pytest", "time": "0.0"})
    ET.SubElement(suite, "testcase", {"classname": "t", "name": "a", "time": "3.0"})
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(str(path), encoding="unicode")

    try:
        dr.parse_junit(path)
        raise AssertionError("expected DurationsUnavailable")
    except dr.DurationsUnavailable as exc:
        assert "zero" in str(exc).lower() or "negative" in str(exc).lower()


def test_parse_junit_converts_an_os_error_to_durations_unavailable(tmp_path, monkeypatch):
    """A `junit.xml` that exists but cannot be read (permission denied, or

    it vanished between the `exists()` check and the read) must render as
    `could-not-measure`, not crash `main()` -- the auditor's Class B
    finding, reproduced there with `chmod 000`. Reproduced here by
    monkeypatching `ET.parse` so the test does not depend on this platform's
    permission model (`chmod 000` is not enforced against root, and Windows
    has no equivalent bit)."""
    path = _junit(tmp_path, 10.0, [("t.a", 5.0)])

    def _raise_permission_denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(dr.ET, "parse", _raise_permission_denied)
    try:
        dr.parse_junit(path)
        raise AssertionError("expected DurationsUnavailable")
    except dr.DurationsUnavailable as exc:
        assert "could not be read" in str(exc)


def test_render_reports_could_not_measure_for_an_unreadable_junit_not_a_crash(tmp_path, monkeypatch):
    path = _junit(tmp_path, 10.0, [("t.a", 5.0)])
    monkeypatch.setattr(dr.ET, "parse", lambda *_a, **_k: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied", str(path))))
    lines = dr.render(path, tmp_path / "no-baseline.json")
    text = "\n".join(lines)
    assert "state: could-not-measure" in text


def test_main_reports_rather_than_crashes_on_a_malformed_top_value(tmp_path, capsys):
    """`--top abc` used to raise an uncaught `ValueError` out of `main()`,

    contradicting the module's own "always returns 0" guarantee (the
    reviewer's finding #2)."""
    junit = _junit(tmp_path, 10.0, [("t.a", 5.0)])
    rc = dr.main(["duration_report.py", str(junit), "--top", "abc"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "could-not-measure" in out
    assert "--top" in out


def test_main_reports_rather_than_misparses_a_dangling_flag(tmp_path, capsys):
    """`--top` as the last token with no value used to be silently treated

    as the junit-xml path instead of being reported as an error."""
    rc = dr.main(["duration_report.py", "--top"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "could-not-measure" in out
    assert "no value" in out


# --- #2274: partial parse disclosure -----------------------------------------
#
# `parse_junit`'s per-suite `except (TypeError, ValueError): continue` sits
# inside `for suite in suites:`, so it advances the *suite* loop -- the
# nested `for case in suite.iter("testcase")` never runs for that suite. An
# earlier comment on that line claimed the opposite ("its testcases below
# are not [unusable]"). These tests pin the actual behaviour: a suite with
# an unparseable `time=` is dropped, by name, and disclosed in the report
# rather than silently reflected in an unqualified `(NN.NN% of total)`.

def _junit_multi(tmp_path: Path, suites: list[tuple[str, object, list[tuple[str, float]]]]) -> Path:
    """Write a `<testsuites>` root with one `<testsuite>` per entry.

    `time` may be a string coercible to float, `None` (attribute omitted),
    or a non-numeric string -- whatever the caller wants to put on the
    element, to exercise `TypeError` vs `ValueError` in `parse_junit`.
    """
    root = ET.Element("testsuites")
    for name, time_value, cases in suites:
        attrib = {"name": name, "tests": str(len(cases))}
        if time_value is not None:
            attrib["time"] = str(time_value)
        suite = ET.SubElement(root, "testsuite", attrib)
        for ident, t in cases:
            classname, _, cname = ident.rpartition(".")
            ET.SubElement(suite, "testcase", {
                "classname": classname, "name": cname or ident, "time": str(t)})
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(str(path), encoding="unicode")
    return path


def test_parse_junit_drops_a_suite_with_unparseable_time_and_names_it(tmp_path):
    """The malformed suite's own testcases must not appear in `cases` --

    the loop that skips its total also skips its testcases, and this pins
    that as the return value rather than as report text."""
    junit = _junit_multi(tmp_path, [
        ("good", 50.0, [("t.a", 44.0)]),
        ("bad", "not-a-number", [("t.b", 999.0)]),
    ])
    total, cases, dropped = dr.parse_junit(junit)
    assert total == 50.0
    assert dropped == ["bad"]
    assert ("t.a", 44.0) in cases
    assert not any(ident == "t.b" for ident, _ in cases)


def test_parse_junit_drops_a_suite_with_missing_time_attribute(tmp_path):
    """`TypeError` path -- `time=` attribute entirely absent, not just malformed."""
    junit = _junit_multi(tmp_path, [
        ("good", 50.0, [("t.a", 44.0)]),
        ("no-time-attr", None, [("t.b", 1.0)]),
    ])
    total, cases, dropped = dr.parse_junit(junit)
    assert total == 50.0
    assert dropped == ["no-time-attr"]


def test_render_discloses_partial_parse_when_one_suite_of_several_is_dropped(tmp_path):
    junit = _junit_multi(tmp_path, [
        ("good", 50.0, [("t.a", 44.0), ("t.c", 6.0)]),
        ("bad", "garbage", [("t.b", 999.0)]),
    ])
    lines = dr.render(junit, tmp_path / "no-such-baseline.json")
    text = "\n".join(lines)
    assert "state: partial-parse" in text
    assert "bad" in text
    # the count itself, not a bare "1" -- tmp_path's own generated segment
    # names always carry digits (e.g. .../test_render_..._0/...), so a bare
    # "1" in text is satisfied by the fixture path regardless of whether the
    # drop count is right (self-review finding on #2274).
    assert "1 testsuite(s) dropped" in text
    # the other three states are still reachable independently of this one
    assert "state: no-baseline" in text


def test_render_does_not_disclose_partial_parse_when_every_suite_parses_cleanly(tmp_path):
    """Must-not-fire pair for the test above: an all-clean junit.xml with

    multiple suites produces no drop disclosure at all."""
    junit = _junit_multi(tmp_path, [
        ("good-1", 50.0, [("t.a", 44.0)]),
        ("good-2", 10.0, [("t.b", 9.0)]),
    ])
    lines = dr.render(junit, tmp_path / "no-such-baseline.json")
    text = "\n".join(lines)
    assert "partial-parse" not in text
    assert "dropped" not in text


# --- #2277: the source comments must not claim a committed file that does
# not exist -----------------------------------------------------------------

def test_no_committed_duration_baseline_file_exists_yet():
    """Pins the fact the two corrected comments (this module's docstring and

    `.github/workflows/tests.yml`'s "Suite duration report" step) now state
    accurately: `.github/duration-baseline.json` is not tracked. If this
    ever goes red, a baseline landed and the comments (and this test) need
    a matching update -- not a silent stale claim re-appearing (#2277)."""
    import subprocess
    result = subprocess.run(
        ["git", "ls-files", ".github/"],
        cwd=REPO, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    tracked = result.stdout.splitlines()
    assert "duration-baseline.json" not in {Path(f).name for f in tracked}


def test_load_baseline_absent_state_is_unchanged_by_the_2277_comment_fix(tmp_path):
    """`load_baseline` behaviour is untouched by #2277 -- that issue is a

    comment-accuracy fix, not a functional one. Pinned here so a future
    change to this function trips a test that names the issue it would be
    breaking a promise from."""
    state, pct = dr.load_baseline(tmp_path / "does-not-exist.json")
    assert state == "absent"
    assert pct is None
