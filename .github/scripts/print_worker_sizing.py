#!/usr/bin/env python3
"""Print what pytest-xdist "-n auto" resolves to on this runner (#2345).

pyproject.toml addopts carries "-n auto", and tests.yml's "Run tests" step
overrides the marker expression and --cov but not -n, so all twelve CI legs
run under xdist with a worker count nobody prints by name. `-q` on that same
step additionally suppressed xdist's own "N workers [M items]" banner
(confirmed locally: the banner prints under -n auto with no -q, and is gone
the moment -q is added, with or without --junit-xml) -- so tests.yml now
drops -q there instead, and the banner is the primary, measured answer.

This script is not a substitute for that banner and does not try to be: this
repository already runs -n auto on twelve legs, so it can observe the real
resolved count directly rather than predict one. What xdist's own banner
cannot say is WHICH of its four sources decided the number -- and that is
the half of the issue worth a script for, because the day something pulls
`psutil` into the CI environment transitively, `-n auto` silently switches
from counting logical cores (`os.sched_getaffinity`/`os.cpu_count`) to
counting *physical* ones, halving the worker count on any SMT runner with no
other visible cause. The banner shows the new, smaller N; only a source
reading like this one says why it changed.

Nothing here gates anything. This is a `git log` for a number, not a check on
it: it always exits 0, and it prints "worker sizing unknown" rather than a
number when none of xdist's own sources can answer, so a reader can tell "I
looked and nobody had an answer" from "N workers" -- the two must never look
the same (CLAUDE.md, "The defect this codebase keeps having").

The resolution order below is copied from xdist.plugin's
pytest_xdist_auto_num_workers hook, because that hook is what -n auto
actually calls -- there is no public API that returns the number without
either running the hook or duplicating its four branches:

1. PYTEST_XDIST_AUTO_NUM_WORKERS env var, if set and parses as an int. If it
   is set to something that does NOT parse, xdist warns and ignores it --
   the cap is NOT in effect, and resolution falls through to the sources
   below. This script reports that explicitly rather than silently falling
   through and reporting a number a reader could mistake for the env var's
   own value.
2. psutil.cpu_count(logical=False) -- psutil is not a declared dependency of
   this repo today (pyproject.toml's [project.optional-dependencies] has no
   psutil entry), so this branch answers only if something else pulled it in
   transitively. It uses logical=False here because this repo runs -n auto,
   not -n logical -- a future switch to -n logical would flip that branch,
   and this script would report which one answered either way.
3. os.sched_getaffinity(0) -- POSIX only; not present on macOS or Windows.
4. os.cpu_count() -- the fallback of last resort.

This is also a transcription of xdist's own logic, and a transcription is a
claim about a dependency that can go stale if xdist changes its hook --
`Digital-Process-Tools/claude-oss`'s scripts/doctor.py carries the same
transcription (xdist_auto_workers) and says so about itself. Accepted the
same way here: if it drifts, this prints a stale number rather than gating a
build on a wrong one, and the measured banner above is the fallback truth
either way.

Whether xdist is even importable in this interpreter is also reported
separately (`is_xdist_installed()`): a worker count is a number about
nothing on a machine that could not run -n auto at all, and the two must not
be conflated -- an environment where xdist failed to install looks, from
this script's other output alone, exactly like one where `os.cpu_count()`
happened to answer 1.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Mapping, Optional, Tuple

Resolution = Tuple[str, Optional[int], Optional[str]]


def is_xdist_installed() -> bool:
    """Is pytest-xdist importable in THIS interpreter? Never raises.

    A broken meta path finder or similarly hostile import machinery is
    reported as "not installed" rather than propagated -- this script's
    whole job is to report, never to crash a step that runs before the
    real test suite does.
    """
    try:
        return importlib.util.find_spec("xdist") is not None
    except Exception:
        return False


def _psutil_physical_count() -> Optional[int]:
    """xdist branch 2: psutil.cpu_count(logical=False), falling back to
    psutil.cpu_count() if the physical count comes back falsy -- the same
    fallback xdist itself performs, verbatim.
    """
    try:
        import psutil
    except ImportError:
        return None
    count = psutil.cpu_count(logical=False) or psutil.cpu_count()
    return count or None


def _affinity_count() -> Optional[int]:
    """xdist branch 3: len(os.sched_getaffinity(0)). Absent on non-POSIX
    platforms, and can raise on a platform that has the name but not a
    working implementation -- treated the same as "not available".
    """
    try:
        from os import sched_getaffinity
    except ImportError:
        return None
    try:
        return len(sched_getaffinity(0)) or None
    except OSError:
        return None


def _os_cpu_count() -> Optional[int]:
    """xdist branch 4: os.cpu_count()."""
    return os.cpu_count()


def resolve(env: Optional[Mapping[str, str]] = None) -> Resolution:
    """Walk xdist's own resolution order and report which source answered.

    Returns (source, count, note). count is None exactly when nothing
    answered -- the third state a reader must be able to tell apart from a
    real number, never collapsed into 0 or a made-up default.

    `note` carries a finding that survives even when a later source DID
    answer -- today, only the "env var set but unparseable" case, which
    xdist itself treats as a warning-and-ignore rather than a fatal error,
    so resolution correctly continues past it. Silently continuing without
    saying so would read as "the env var was not set" to anyone scanning
    the output, which is a different and wrong claim.
    """
    env = os.environ if env is None else env
    note: Optional[str] = None

    raw = env.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if raw:
        try:
            return "PYTEST_XDIST_AUTO_NUM_WORKERS env var", int(raw), None
        except ValueError:
            note = (
                f"PYTEST_XDIST_AUTO_NUM_WORKERS is set to {raw!r}, which is not "
                "a number: xdist warns and ignores it, so the cap is NOT in "
                "effect -- falling through to the next source"
            )

    count = _psutil_physical_count()
    if count:
        return "psutil.cpu_count(logical=False)", count, note

    count = _affinity_count()
    if count:
        return "os.sched_getaffinity(0)", count, note

    count = _os_cpu_count()
    if count:
        return "os.cpu_count()", count, note

    unknown_note = (
        "worker sizing unknown -- none of xdist resolution sources "
        "(env var, psutil, sched_getaffinity, os.cpu_count) answered"
    )
    if note:
        unknown_note = f"{note}; {unknown_note}"
    return "none", None, unknown_note


def render(resolution: Resolution, xdist_installed: bool) -> str:
    source, count, note = resolution
    lines = []
    if not xdist_installed:
        lines.append(
            "xdist -n auto: pytest-xdist is not importable in this "
            "interpreter -- a worker count would be a number about nothing, "
            "so none is reported"
        )
        if note:
            lines.append(f"  note: {note}")
        return "\n".join(lines)
    if count is None:
        lines.append(f"xdist -n auto: worker sizing unknown ({note})")
        return "\n".join(lines)
    lines.append(f"xdist -n auto would resolve to {count} worker(s) (source: {source})")
    if note:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main() -> int:
    print(render(resolve(), is_xdist_installed()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
