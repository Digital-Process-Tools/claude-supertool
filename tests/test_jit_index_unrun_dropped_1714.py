r"""A jit-index run that stalled partway must not read as a complete one (#1714).

`_compile_findings` hands every pattern to one awk process, and only on a
non-zero exit does it buy a second pass, one process per pattern. Either half
can stop dead — a timeout, or an awk that cannot be spawned at all — and when it
does, the patterns after that point were never compiled.

With no findings in hand the adapter already says so: it emits `absent()`, which
is `skipped` locally and escalates under `$SUPERTOOL_REQUIRE_VALIDATORS`. With a
finding in hand it did not. `if errors:` published the findings and dropped
`unrun` on the floor, so a run that compiled 2 of 20 patterns and stalled on the
third rendered as `2 err` — byte-identical to a complete run that found two
things. That is this repo's house defect: an absence produced by the tool, read
as an absence in the world.

The fix is an extra error with `code: "adapter"` naming what was not reached.
`_supertool.py:_validator_not_checked` requires **every** error to be `adapter`
before it declares a file unmeasured, so an adapter row sitting beside real
findings is a shape the core already handles — the file was measured, and one
part of the measurement is missing, and both statements now reach the reader.

Would these pass if the code did nothing? No. Each asserts a message that does
not exist on master, and the last asserts the *absence* of verdict keys, which a
fabricated `ok` cannot satisfy.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _preset_loader import load_validator_module

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "jit-index" / "jit-index.py"

TAB = "\t"

@pytest.fixture
def adapter():
    """A fresh import of the adapter per test, and `sys.path` put back after.

    Loaded under a unique module name and never cached, so a test that
    replaces `_awk_run` cannot leak that replacement into the next one.

    **`sys.path` is restored, because importing an adapter mutates it.** Every
    validator reaches its shared helpers with
    `sys.path.insert(0, .../validators/common)` at module scope
    (`jit-index.py:71`) — invisible in a subprocess, which is how adapters have
    always been exercised, and permanent for the rest of the worker once one is
    imported in-process. `load_validator_module` owns that restore, for the
    reason `tests/_preset_loader.py` gives: the six hand-rolled versions of it
    were #552/#555, and a seventh written here would be the seventh copy that
    test refuses.
    """
    yield load_validator_module("jit-index")


posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="needs an executable shim on PATH to make awk stall on demand")


def _run(target, env_path):
    env = dict(os.environ)
    env["PATH"] = str(env_path)
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env)
    assert proc.stdout.strip(), "adapter emitted nothing (stderr: {0})".format(proc.stderr)
    return json.loads(proc.stdout)


def _paths_index(tmp_path, *patterns):
    d = tmp_path / "jit-context" / "paths" / "00-manual"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "00-index.tsv"
    f.write_text("".join(p + TAB + "r.md\n" for p in patterns), encoding="utf-8")
    return f


def _bin(tmp_path, body):
    binder = tmp_path / "bin"
    binder.mkdir(exist_ok=True)
    shim = binder / "awk"
    shim.write_text(body, encoding="utf-8")
    shim.chmod(0o755)
    return binder


#: awk that cannot be spawned at all: `which` finds it, exec fails. The batch
#: run is the first thing to touch it, so nothing is ever compiled.
DEAD_AWK = "#!/nonexistent/interpreter\n"

#: awk that answers for a while and then vanishes mid-second-pass. `--version`
#: is served without touching stdin because `_awk_version` passes no input; the
#: per-pattern call carrying `stophere/` removes the shim, so the *next*
#: pattern's spawn raises. The whole-index batch carries all three patterns at
#: once and so fails the exact compare, which is what keeps the stall partial.
#: `cat` and `rm` are spelled absolutely because $PATH is replaced by the shim
#: directory for the duration of the run — a bare `cat` is not found, `seen` is
#: empty, and the shim then never removes itself, which silently turns this
#: fixture into a complete run.
DYING_AWK = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "fake awk 1.0"; exit 0; fi
seen=$(/bin/cat)
if [ "$seen" = "stophere/" ]; then /bin/rm -f "$0"; fi
echo "awk: syntax error" >&2
exit 2
"""


def _errors(result):
    return result.get("errors", [])


def _adapter_rows(result):
    return [e for e in _errors(result) if e.get("code") == "adapter"]


def _msgs(result):
    return " | ".join(e["msg"] for e in _errors(result))


class TestUnrunSurvivesAFinding:

    @posix_only
    def test_a_total_stall_beside_a_structural_finding_is_stated(self, tmp_path):
        """The structural half found something; the compile half never ran."""
        idx = _paths_index(tmp_path, r"presets/\w+/", "omega/")
        result = _run(idx, _bin(tmp_path, DEAD_AWK))

        assert result["ok"] is False
        # The finding it did have is still published, unchanged.
        assert any(e["code"] == "escape" for e in _errors(result)), _msgs(result)

        rows = _adapter_rows(result)
        assert len(rows) == 1, _msgs(result)
        assert "never compiled" in rows[0]["msg"]
        assert "2 of 2" in rows[0]["msg"]
        assert rows[0]["line"] is None

    @posix_only
    def test_a_partial_stall_names_the_patterns_it_never_reached(self, tmp_path):
        """Two rows compiled and were refused; the third was never looked at."""
        idx = _paths_index(tmp_path, "alpha/", "stophere/", "omega/")
        result = _run(idx, _bin(tmp_path, DYING_AWK))

        assert result["ok"] is False
        compiled = [e for e in _errors(result) if e["code"] == "compile"]
        assert [e["line"] for e in compiled] == [1, 2], _msgs(result)

        rows = _adapter_rows(result)
        assert len(rows) == 1, _msgs(result)
        assert "1 of 3" in rows[0]["msg"]
        # Line 3 is the pattern nobody compiled, and naming it is the point.
        assert "line 3" in rows[0]["msg"]

    @posix_only
    def test_the_count_includes_the_unrun_row_so_nothing_is_hidden(self, tmp_path):
        idx = _paths_index(tmp_path, r"presets/\w+/")
        result = _run(idx, _bin(tmp_path, DEAD_AWK))
        assert result["count"] == len(_errors(result))
        assert result["count"] == 2


class TestOnAnyPlatform:
    """The same contract, reached without a shell shim.

    Every test above needs an executable on $PATH that misbehaves on cue, so
    all four skip on Windows — and a file that asserts nothing on a platform
    reports coverage it has not got. These drive the adapter in-process
    instead: `_awk_run` is the single seam every stall passes through, so
    stubbing it reproduces a timeout and a mid-loop exec failure exactly, on
    any platform, with no subprocess at all.
    """

    def test_a_timeout_before_anything_compiled_lists_every_pattern(
            self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapter, "_awk_run",
            lambda awk, patterns: (None, "awk did not answer within 10s"))
        rows = [(1, "alpha/", "paths"), (2, "beta/", "paths")]
        errors, unrun, unchecked = adapter._compile_findings("awk", rows)
        assert errors == []
        assert unrun == "awk did not answer within 10s"
        assert unchecked == [1, 2]

    def test_a_stall_inside_the_second_pass_lists_only_what_is_left(
            self, adapter, monkeypatch):
        calls = []

        def fake(awk, patterns):
            calls.append(list(patterns))
            if len(patterns) > 1:
                return 2, "awk: syntax error"      # the batch: something is wrong
            if patterns == ["beta/"]:
                return None, "awk could not be run: boom"
            return 2, "awk: syntax error"

        monkeypatch.setattr(adapter, "_awk_run", fake)
        monkeypatch.setattr(adapter, "_awk_version", lambda awk: "fake awk")
        rows = [(1, "alpha/", "paths"), (2, "beta/", "paths"), (3, "gamma/", "paths")]
        errors, unrun, unchecked = adapter._compile_findings("awk", rows)

        assert [e["line"] for e in errors] == [1]
        assert unrun == "awk could not be run: boom"
        assert unchecked == [2, 3], "the stalled pattern is unchecked too, not just the ones after it"

    def test_a_clean_compile_reports_nothing_unchecked(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_awk_run", lambda awk, patterns: (0, ""))
        errors, unrun, unchecked = adapter._compile_findings("awk", [(1, "alpha/", "paths")])
        assert (errors, unrun, unchecked) == ([], None, [])

    def test_the_published_payload_carries_the_stall_beside_the_finding(
            self, adapter, tmp_path, capsys, monkeypatch):
        idx = _paths_index(tmp_path, r"presets/\w+/", "beta/")
        # `adapter.shutil` **is** `sys.modules["shutil"]`, not a per-module copy,
        # so a plain assignment here replaces `shutil.which` for the whole
        # worker and never puts it back. It did: every later test in the process
        # got `which(<anything>) == "awk"`, `presets/git/_git_common.py:1139`
        # read that as "glab is installed", and ten tests in
        # tests/test_status_swallowed_705.py went red on 11 CI legs (#1718).
        # monkeypatch restores it; a bare `=` on a module attribute cannot.
        monkeypatch.setattr(adapter.shutil, "which", lambda name: "awk")
        monkeypatch.setattr(
            adapter, "_awk_run",
            lambda awk, patterns: (None, "awk did not answer within 10s"))
        monkeypatch.setattr(sys, "argv", ["jit-index.py", str(idx)])

        adapter.main()
        payload = json.loads(capsys.readouterr().out)

        assert payload["ok"] is False
        assert payload["count"] == 2
        codes = [e["code"] for e in payload["errors"]]
        assert codes == ["escape", "adapter"], "the stall sorts last, after located findings"
        assert "2 of 2" in payload["errors"][1]["msg"]
        assert "never compiled" in payload["errors"][1]["msg"]


class TestTheQuietArmIsUnchanged:
    """A stall with nothing else to say stays a skip — #1254's judgment, kept.

    This adapter carries `rollback_on_fail`. A stall with no findings must not
    revert a correct edit because a machine hiccuped, so that arm goes through
    `absent()`. The arm above can be loud precisely because `ok: false` and the
    rollback are already owed to the real findings, so the loudness costs
    nothing that was not already spent.
    """

    @posix_only
    def test_a_stall_with_no_findings_is_still_a_skip(self, tmp_path):
        idx = _paths_index(tmp_path, "alpha/", "omega/")
        result = _run(idx, _bin(tmp_path, DEAD_AWK))
        assert "skipped" in result
        assert "ok" not in result
        assert "count" not in result
        assert "errors" not in result
        assert "never compiled" in result["skipped"]
