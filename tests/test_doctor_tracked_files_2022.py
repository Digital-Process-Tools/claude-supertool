"""#2022 -- `_doctor_tracked_files` must not fold `git ls-files` output on
`str.splitlines()`'s ten separators, and must not leave `git`'s C-quoting on
a tracked filename it hands to `_match_glob`.

Two findings, one root, one fix (`git ls-files -z`, NUL-delimited):

1. `containment (read)`, blocking. `splitlines()` folds on U+2028/U+2029 (and
   eight ASCII separators besides LF) that `git ls-files`' own delimiter never
   uses. A tracked path containing U+2028 became two list entries, and the
   fragment after the separator reached `_validator_run_one`'s `target`
   argument -- a path handed straight to a subprocess. Bounded by
   `core.quotePath=false`, an ordinary config choice, not an exotic one.
2. `misreports`, non-blocking, fires under **stock** git config. A tracked
   filename holding a byte git treats as "unusual" comes back C-quoted
   (`"ff\\ff.py"` for a backslash, `"caf\\303\\251.py"` for a non-ASCII
   `e`-acute) and `_match_glob` cannot match a glob like `*.py` against a
   quoted string -- an in-scope validator renders as "not applicable".
   Exercised here with a non-ASCII filename rather than the issue's own
   backslash one: a backslash is git's own path separator on Windows and
   `git update-index --cacheinfo` refuses it there even off the real
   filesystem (`fatal: git update-index: --cacheinfo cannot add ff\\ff.py`,
   observed on CI, windows-latest), so that exact fixture cannot exist on
   every platform this suite runs on. A non-ASCII byte is C-quoted by the
   identical mechanism (`core.quotePath`'s "unusual character" rule) and is
   an ordinary, legal filename character on every platform in the CI
   matrix, so the behaviour under test -- the quoting is left on and
   `_match_glob` cannot see through it -- is exercised everywhere rather
   than skipped anywhere.

Every "must not fire" case here is paired with a "must fire" case in the same
fixture, per this repo's own rule: a negative assertion that passes on a
broken harness is worse than no assertion.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager

import supertool

_HERMETIC = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_HERMETIC}
    return subprocess.run(["git"] + args, cwd=cwd, env=env,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")


@contextmanager
def _tracking_one_file(name: str, *, quote_path: bool = True):
    """A fresh repo tracking exactly one file named `name`, chdir'd into.

    `quote_path` mirrors git's own default (`core.quotePath=true`); the
    U+2028 finding is reproduced under the *stock* value to match the
    issue's own reproduction, the backslash finding is reproduced under
    the stock value too, since #2022 says it fires regardless.
    """
    with tempfile.TemporaryDirectory() as d:
        _run(["init", "-q"], d)
        if not quote_path:
            _run(["config", "core.quotePath", "false"], d)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        _run(["add", "."], d)
        r = _run(["commit", "-q", "-m", "x"], d)
        assert r.returncode == 0, r.stderr
        cwd = os.getcwd()
        os.chdir(d)
        try:
            yield d
        finally:
            os.chdir(cwd)


def test_tracked_files_must_fire_a_plain_ascii_name() -> None:
    """Positive control: an ordinary tracked file is reported, unmangled."""
    with _tracking_one_file("plain.py"):
        files = supertool._doctor_tracked_files()
    assert files == ["plain.py"]


def test_tracked_files_must_not_split_a_u2028_filename_in_two() -> None:
    """The reproduced finding 1 route: a U+2028 in the filename must survive
    as one entry, and must never surface the text after the separator as a
    path outside the repository.
    """
    name = "a weird.py"
    with _tracking_one_file(name, quote_path=False):
        files = supertool._doctor_tracked_files()
    assert files == [name]
    # The old splitlines() route folded this into two entries; assert the
    # failure mode by name so a regression is legible, not just red.
    assert not any(f == "weird.py" for f in (files or []))


def test_tracked_files_must_not_leave_c_quoting_on_a_non_ascii_name() -> None:
    """The reproduced finding 2 route: a non-ASCII byte in the filename is
    C-quoted by git under stock config. The list must carry the real
    filename, not the quoted transcript, so `_match_glob` can still match it.

    A non-ASCII name (`caf\\u00e9.py`) rather than the issue's own
    backslash one (`ff\\ff.py`): both trigger the identical C-quoting
    mechanism, but a backslash is git's own Windows path separator, so
    `git update-index --cacheinfo` refuses it there even via plumbing --
    observed on CI, windows-latest: `fatal: git update-index: --cacheinfo
    cannot add ff\\ff.py`. A non-ASCII byte is an ordinary, legal filename
    character on every platform this suite runs on, via plain `open()`, so
    the behaviour under test is exercised everywhere rather than skipped
    anywhere.
    """
    name = "café.py"
    with _tracking_one_file(name, quote_path=True):
        files = supertool._doctor_tracked_files()
    assert files == [name]
    assert supertool._match_glob(files[0], "*.py")


def test_validators_section_target_never_leaves_the_repo_for_a_u2028_name(
        monkeypatch) -> None:
    """End-to-end: `doctor:probe` must never hand `_validator_run_one` a
    target built from the tail of a split multi-separator filename. Pins the
    consumer (`_doctor_validators_section`) rather than only the producer,
    since the fix could otherwise land in a helper this call site does not
    reach.

    `monkeypatch.setattr(supertool, "_validator_run_one", ...)` rather than a
    manual save/restore: the latter is an `ast.Attribute` reference the
    #1501 sweep (`tests/test_core_timeout_is_not_a_verdict_1501.py`) treats
    as a possible alias to an unguarded spawning call, by design (a
    `run = supertool._validator_run_one` followed by `run(...)` would
    otherwise be invisible to that guard) -- `monkeypatch.setattr` passes
    the name as a *string*, which is not an `ast` reference at all.
    """
    name = "a outside.py"
    config = {"validators": {"fake": {"match": "*.py",
                                       "cmd": "true {file}"}}}
    seen_targets = []

    def _fake_run_one(name_, spec, target, doc_maybe_stale=False):
        seen_targets.append(target)
        return {"tool": name_, "file": target, "ok": True, "count": 0,
                "errors": [], "duration_ms": 1}

    monkeypatch.setattr(supertool, "_validator_run_one", _fake_run_one)
    with _tracking_one_file(name, quote_path=False):
        supertool._doctor_validators_section(config, probe=True)

    for t in seen_targets:
        assert " " not in t or t == name
        assert not os.path.isabs(t)


def test_classify_probe_render_does_not_forge_a_row_from_an_embedded_newline(
        monkeypatch) -> None:
    """#2022 finding 3: `_doctor_classify_probe`'s `detail` (and, at the call
    site, `target`) reach the rendered `- name (target): state -- detail`
    row with no `_flat_field`, unlike every other system-authored render in
    this tree. A `skipped` reason carrying a newline must not be able to
    forge a second row under the real tally.
    """
    forged_reason = "not applicable\n- forged: resolves -- everything is fine"
    data = {"tool": "fake", "file": "x.py", "skipped": forged_reason}
    _state, detail = supertool._doctor_classify_probe(data)

    config = {"validators": {"fake": {"match": "*.py", "cmd": "true {file}"}}}

    def _fake_run_one(name_, spec, target, doc_maybe_stale=False):
        return data

    monkeypatch.setattr(supertool, "_validator_run_one", _fake_run_one)
    with _tracking_one_file("real.py"):
        out = supertool._doctor_validators_section(config, probe=True)

    assert detail == forged_reason  # the classifier itself is not the bug
    rendered_lines = out.splitlines()
    forged_rows = [ln for ln in rendered_lines if "forged: resolves" in ln]
    # A faithfully rendered row keeps the embedded newline on ONE list entry
    # (flattened), so the forged text never becomes a row of its own under
    # the real tally line.
    assert not any(ln.strip().startswith("- forged:") for ln in rendered_lines), (
        f"embedded newline in an adapter reason forged a row: {forged_rows!r}")


def test_a_crashing_probe_does_not_forge_a_row_from_the_exception_text(
        monkeypatch) -> None:
    """Caught in review: the `except Exception` branch right next to the two
    rows this fix just flattened does not flatten `str(exc)` either, so an
    exception message carrying a newline can still forge a row the same way
    finding 3 of #2022 describes.

    Second round on CI (#2036, windows-latest 3.11, job #98795655692): a
    newline-carrying FILENAME was the original route to that exception
    text, via `_tracking_one_file_via_index` -- but git's own path
    validation refuses a newline in an index path on Windows too, before
    the working tree is even consulted (`fatal: git update-index:
    --cacheinfo cannot add real\n- forged: ...`), the same wall the
    backslash fixture hit one commit earlier, one layer down.

    The newline filename was never the property under test, though -- it
    was one route to a multi-line exception message, and the assertion
    only cares that the RENDER flattens whatever `str(exc)` is. So the
    exception is raised directly with a multi-line message now, and the
    fixture is an ordinary ASCII filename with no platform opinion at all.
    Exercised identically on every platform in the CI matrix, no plumbing
    and no skip needed.
    """
    forged_message = "probe crashed\n- forged: resolves -- everything is fine"
    config = {"validators": {"fake": {"match": "*.py", "cmd": "true {file}"}}}

    def _fake_run_one(name_, spec, target, doc_maybe_stale=False):
        raise ValueError(forged_message)

    monkeypatch.setattr(supertool, "_validator_run_one", _fake_run_one)
    with _tracking_one_file("real.py"):
        out = supertool._doctor_validators_section(config, probe=True)

    rendered_lines = out.splitlines()
    assert not any(ln.strip().startswith("- forged:") for ln in rendered_lines), (
        f"exception text forged a row: {out!r}")
