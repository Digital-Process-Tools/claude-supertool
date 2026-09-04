#!/usr/bin/env python3
"""Report the suite's own time shape (#2206) -- never gate on it.

`--durations=25` has printed on every pytest leg since #891, and until this
script nothing read it. A test can quietly grow to a large share of a whole
suite and the only way anyone found out was a maintainer reading a CI log
tail by chance (`claude-oss`'s own `test_shell_probe.py`: 43.92s against
~6.5s for the next slowest, roughly a fifth of that run). This is the
detector that reads the log instead of hoping somebody does.

**This is a report, never a gate.** A shared CI runner's load is not in
anybody's diff, and a wall-clock threshold that reddens a pull request for a
neighbour's noisy build teaches people to re-run until green, which costs
more than the slow test did (the issue's own argument, and the same trade
`docs/contributing.md` already makes for `ruff` in this same workflow file:
"a gate reds someone else's unrelated PR ... that cost is paid by whoever
happens to push next rather than by whoever caused it"). Nothing in this
module can fail the build: `main()` always returns 0, whatever it read.

## Why junit.xml, not `--durations` stdout text

pytest's `--durations=N` text block is not machine-shaped: its wording has
moved across major versions, it wraps and reorders under `-q`, and this
repo's own `tests.yml` runs the suite under `-n auto`
(`pyproject.toml` `addopts`), where a plain-text parse would have to guess
how xdist interleaved worker output. `--junit-xml=junit.xml` is already a
flag on the same pytest invocation and already the input `junit_summary.py`
sits next to -- pytest's junitxml plugin aggregates every xdist worker into
one `<testsuite>` regardless of `-n auto`, each `<testcase time="...">` is
unambiguous, and `<testsuite time="...">` is the run's real wall-clock
(recorded at session start/finish, not summed from testcases). That last
part is exactly the denominator "share of total suite time" needs under
parallel execution: if one test pins a worker for 44s while eleven others
finish in 2s, wall clock is ~44s and that test's share approaches 100% --
which is precisely the shape this exists to surface, not an artifact of
summing serial times that were never serial.

A percentage over a raw second count for the same reason the issue gives:
an absolute duration says more about the runner than about the test, and a
percentage travels between machines.

## The three states, and a fourth disclosure

- **could-not-measure** -- `junit.xml` is absent, not parseable XML, has no
  `<testsuite>`, none of its `<testsuite>` elements has a numeric `time=`, or
  none of its testcases carry a parseable time. Never rendered as an empty
  top-N list, which would read exactly like "no hot test" -- the defect class
  the issue is itself named after, reappearing inside its own detector.
- **no-baseline** -- durations were measured, but
  `.github/duration-baseline.json` is absent, or present and unreadable
  (malformed JSON, or missing the one key this reads). The number is
  printed either way, explicitly compared to nothing, never rendered as "no
  change".
- **measured** -- durations were measured and a valid baseline was found, so
  the slowest share is printed next to what the baseline says it used to be.

A `<testsuites>` root can hold more than one `<testsuite>`, and a single
suite's own `time=` can be missing or unparseable while its siblings are
fine. When that happens (#2274), that one suite -- and every testcase inside
it, since a share computed against a total that dropped one suite's seconds
but kept the same suite's testcases would render each of those testcases'
percentages against the wrong denominator -- is dropped from the parse
rather than sinking the whole report to `could-not-measure`. That is a
**fourth, disclosed state**, orthogonal to the three above: `partial-parse`
prints its own line naming which suite(s) were dropped, because the
`(NN.NN% of total)` line below it is still printed against a *partial*
total, and printing that percentage with nothing saying the denominator is
incomplete is exactly the "empty top-N list" defect one level up -- a
partial answer rendered as a complete one.

## Where the baseline lives

The design: a committed file, `.github/duration-baseline.json`, holding one
number, `slowest_share_pct` -- the plain percentage this script itself would
have printed on a run somebody decided was normal. **No such file is
committed today** (`git ls-files .github/` does not list it -- #2277), so
every run of this report is currently in its `no-baseline` state, comparing
to nothing rather than to a real number; that is disclosed loudly in the
output (see "The three states" above) rather than silently, but it means
the comparison half of this feature has not shipped yet, only the
absence-handling for it. Whether a real baseline lands in a follow-up or
this repo simply carries `no-baseline` indefinitely is an open call, not
one this module makes. Simplest option and the one
that is auditable in a diff -- a change to "what's normal" is a visible pull
request, same as any other repo constant, rather than a workflow artifact
compared run-to-run, which would need a second retention/rotation policy
and would not show a repo constant changing in a `git log`. The cost is
symmetric with every other hand-maintained number in this repo (the CI
timeout comments a few hundred lines up in `tests.yml` say as much about
themselves): it goes stale unless someone updates it, and that is exactly
why it is a report and never a gate -- staleness in a gate is a false red
for whoever pushes next; staleness in a report is a number someone reads
skeptically, which is the correct failure mode for a fact that ages.

Nothing here writes the baseline. Updating it is a judgement call about
whether a shift is real, which belongs to a person reading this script's own
output, not to the script that produced it.

## What counts as "interesting"

`HOT_MARKER` is printed next to the slowest test's line when its share
crosses `DEFAULT_THRESHOLD_PCT` (10.0). Chosen, not measured: an even split
among the top 25 durations `--durations=25` already prints would be 4% each,
so a single test at 10% is worth as much as the next ~2.5 slowest combined --
visibly disproportionate without being triggered by ordinary variance
between a suite's naturally slowest handful. It marks a line in the normal
output; it does not gate, and it does not require an extra flag to see (the
issue's own acceptance criterion).
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

#: Printed next to a duration whose share of the total crosses the threshold.
#: Plain ASCII on purpose -- this runs on windows-latest too, and stdout
#: there is a console codepage that does not carry every glyph (see the
#: `-X utf8` comment on the "Run tests" step in `.github/workflows/tests.yml`
#: for the failure shape when a non-ASCII marker meets that codec).
HOT_MARKER = "[HOT]"

DEFAULT_TOP = 10
DEFAULT_THRESHOLD_PCT = 10.0
DEFAULT_JUNIT = Path("junit.xml")
DEFAULT_BASELINE = Path(".github/duration-baseline.json")


class DurationsUnavailable(Exception):
    """`junit.xml` cannot answer -- absent, unparseable, or carries no timing.

    Every path that raises this must say, in the message, which of those it
    was: the whole point of a third state is that "I could not look" and "I
    looked and found nothing" print differently.
    """


def parse_junit(
    path: Path,
) -> tuple[float, list[tuple[str, float]], list[str]]:
    """`(total suite seconds, [(test id, seconds), ...], [dropped suite name, ...])`, or raise.

    Handles every sibling `<testsuite>` under a `<testsuites>` root, not just
    the first -- `Element.find()` returns one match, and a report that reads
    only the first suite while a second sits unread is not `could-not-measure`,
    it is confidently wrong, which is worse (a review finding on this module,
    #2206). Reads all of them and sums their `time=` totals, and collects
    testcases from all of them too. A `<testsuite time="0.0">` or a negative
    one is also refused rather than accepted as a valid zero: `share()` would
    otherwise render "0.00% of total" for an invalid denominator exactly as it
    would for a genuinely negligible one, which is this module's own defect
    class reappearing inside itself (a second review finding).

    A single `<testsuite>` whose own `time=` is missing or unparseable does
    not sink the whole parse -- it is dropped, by name, into the third list
    returned, and the caller discloses that rather than staying silent about
    a total that is now short one suite's seconds (#2274).
    """
    if not path.exists():
        raise DurationsUnavailable(
            f"no {path} was written -- pytest did not reach the end of its "
            f"run, or ran without --junit-xml; its own console output is the "
            f"only record")
    try:
        root = ET.parse(str(path)).getroot()
    except ET.ParseError as exc:
        raise DurationsUnavailable(
            f"{path} exists but is not parseable XML: {exc}") from exc
    except OSError as exc:
        # e.g. permission denied, or the file vanished between the exists()
        # check above and this read -- a third review finding: the narrower
        # `except ET.ParseError` alone let this propagate out of main() and
        # crash the CI step the module docstring promises can never fail.
        raise DurationsUnavailable(
            f"{path} exists but could not be read: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    if not suites:
        raise DurationsUnavailable(
            f"{path} has no <testsuite> element -- not a pytest junit report")
    total = 0.0
    any_total_parsed = False
    cases: list[tuple[str, float]] = []
    dropped: list[str] = []
    for suite in suites:
        try:
            total += float(suite.attrib.get("time"))
            any_total_parsed = True
        except (TypeError, ValueError):
            # This `continue` advances the *suite* loop, not the testcase
            # one below -- it skips this suite's testcases entirely rather
            # than folding them into a total that never counted this
            # suite's own seconds. Recorded by name so the caller can
            # disclose which suite(s) were dropped instead of silently
            # reporting every remaining percentage against a partial
            # denominator (#2274; an earlier version of this comment
            # claimed the opposite of what this loop does).
            dropped.append(suite.attrib.get("name") or "<unnamed testsuite>")
            continue
        for case in suite.iter("testcase"):
            raw_time = case.attrib.get("time")
            try:
                t = float(raw_time)
            except (TypeError, ValueError):
                continue  # one testcase missing a time does not sink the report
            classname = case.attrib.get("classname", "")
            name = case.attrib.get("name", "")
            ident = f"{classname}.{name}" if classname else name
            cases.append((ident, t))
    if not any_total_parsed:
        raise DurationsUnavailable(
            f"{path}'s <testsuite time=...> is missing or not a number on "
            f"every <testsuite> found")
    if total <= 0:
        raise DurationsUnavailable(
            f"{path}'s <testsuite time=...> totals {total!r} -- a share "
            f"cannot be computed against a zero or negative denominator")
    if not cases:
        raise DurationsUnavailable(
            f"{path} has a <testsuite> but no <testcase> carries a "
            f"parseable time -- nothing to report")
    return total, cases, dropped


def top_n(cases: list[tuple[str, float]], n: int) -> list[tuple[str, float]]:
    return sorted(cases, key=lambda c: c[1], reverse=True)[:n]


def share(part: float, total: float) -> float:
    """`part` as a percentage of `total`, or 0.0 if `total` cannot divide."""
    if total <= 0:
        return 0.0
    return part / total * 100.0


def load_baseline(path: Path) -> tuple[str, Optional[float]]:
    """`("present", pct)`, `("absent", None)`, or `("unreadable", None)`."""
    if not path.exists():
        return "absent", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pct = float(data["slowest_share_pct"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unreadable", None
    return "present", pct


def render(
    junit_path: Path,
    baseline_path: Path,
    top: int = DEFAULT_TOP,
    threshold: float = DEFAULT_THRESHOLD_PCT,
) -> list[str]:
    """The lines to print for one run. Never raises -- this is a report."""
    try:
        total, cases, dropped = parse_junit(junit_path)
    except DurationsUnavailable as exc:
        return [
            "duration-report state: could-not-measure",
            f"  ({exc})",
        ]

    slowest_ident, slowest_time = max(cases, key=lambda c: c[1])
    slowest_pct = share(slowest_time, total)
    hottest = top_n(cases, top)

    lines = [f"suite duration report -- total {total:.2f}s across {len(cases)} tests"]
    if dropped:
        # A fourth, disclosed state alongside could-not-measure/no-baseline/
        # measured below: some suites parsed and some did not, so every
        # percentage from here on is computed against a total that is
        # missing the dropped suite(s)' own seconds. Never silently folded
        # into "measured" -- that would be this module's own "empty top-N
        # list read as no hot test" defect one level up (#2274).
        lines.append(
            f"duration-report state: partial-parse -- {len(dropped)} "
            f"testsuite(s) dropped from this total ({', '.join(dropped)}) "
            f"-- percentages below are of an incomplete total, not the "
            f"whole run")
    lines.append(f"top {min(top, len(hottest))} durations:")
    for ident, t in hottest:
        marker = f"  {HOT_MARKER}" if share(t, total) >= threshold else ""
        lines.append(f"  {t:8.2f}s  {ident}{marker}")
    lines.append(
        f"slowest test: {slowest_ident} = {slowest_time:.2f}s "
        f"({slowest_pct:.2f}% of total){' ' + HOT_MARKER if slowest_pct >= threshold else ''}")

    baseline_state, baseline_pct = load_baseline(baseline_path)
    if baseline_state == "absent":
        lines.append(
            f"baseline: none at {baseline_path} -- this is compared to "
            f"nothing; do not read the absence of a delta below as unchanged")
        lines.append("duration-report state: no-baseline")
    elif baseline_state == "unreadable":
        lines.append(
            f"baseline: {baseline_path} exists but could not be read as "
            f"{{\"slowest_share_pct\": <number>}} -- compared to nothing; "
            f"do not read the absence of a delta below as unchanged")
        lines.append("duration-report state: no-baseline")
    else:
        delta = slowest_pct - baseline_pct
        lines.append(
            f"baseline: {baseline_pct:.2f}% (from {baseline_path}), "
            f"delta {delta:+.2f}pp")
        lines.append("duration-report state: measured")
    return lines


def main(argv: list[str]) -> int:
    """Parse argv and print the report. Always returns 0 -- see the module

    docstring: this never gates. That includes a malformed CLI argument --
    a review finding on an earlier version of this function was that a bare
    `int(args[i + 1])` / `float(args[i + 1])` propagated a `ValueError`
    straight out of `main()`, so `--top abc` crashed the CI step the module
    promises can never fail. A flag with no following value used to fall
    through silently and be treated as the junit-xml path instead -- also
    fixed here, by reporting it rather than misparsing it.
    """
    junit_path = DEFAULT_JUNIT
    baseline_path = DEFAULT_BASELINE
    top = DEFAULT_TOP
    threshold = DEFAULT_THRESHOLD_PCT
    value_flags = {"--baseline", "--top", "--threshold"}

    positional: list[str] = []
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_flags:
            if i + 1 >= len(args):
                print("duration-report state: could-not-measure")
                print(f"  ({arg} was given with no value)")
                return 0
            value = args[i + 1]
            if arg == "--baseline":
                baseline_path = Path(value)
            elif arg == "--top":
                try:
                    top = int(value)
                except ValueError:
                    print("duration-report state: could-not-measure")
                    print(f"  (--top expects an integer, got {value!r})")
                    return 0
            else:  # --threshold
                try:
                    threshold = float(value)
                except ValueError:
                    print("duration-report state: could-not-measure")
                    print(f"  (--threshold expects a number, got {value!r})")
                    return 0
            i += 2
        else:
            positional.append(arg)
            i += 1
    if positional:
        junit_path = Path(positional[0])

    for line in render(junit_path, baseline_path, top=top, threshold=threshold):
        print(line)
    return 0  # always -- see the module docstring: this never gates


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
