"""A run that opened nothing is not a clean verdict (#1601).

Three adapters published `ok: true, count: 0` for a file their linter may never
have read: `prettier-check` (a `.prettierignore` match exits 0 and prints
nothing), `markdownlint` (an ignored or absent path resolves to no files, and
the CLI then prints its help banner on a zero exit), `stylelint` (empty stdout
was read as clean unconditionally — and stylelint has written its report to
**stderr** since v16, so on a current install that arm was every run).

Every case here is stubbed at `subprocess.run` rather than gated on the tool
being installed: none of the three is on the CI image, so a gated test asserts
nothing anywhere it matters. The stub payloads are transcripts — prettier
3.6.2, markdownlint-cli 0.49.1, stylelint 17.14.1, measured on 2026-08-13.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_verdict import assert_declined

VALIDATORS = Path(__file__).resolve().parent.parent / "validators"

#: What markdownlint-cli prints, on stdout, at exit 0, when the path it was
#: handed resolved to no files at all — measured, 1501 bytes of it.
MD_HELP = (
    "Usage: markdownlint [options] [files|directories|globs...]\n"
    "\n"
    "MarkdownLint Command Line Interface\n"
)

#: stylelint's json formatter, one clean file and one with a warning.
SL_CLEAN = json.dumps([{"source": "x.css", "deprecations": [],
                        "invalidOptionWarnings": [], "parseErrors": [],
                        "errored": False, "warnings": []}])
SL_WARNED = json.dumps([{"source": "x.css", "deprecations": [],
                         "invalidOptionWarnings": [], "parseErrors": [],
                         "errored": True,
                         "warnings": [{"line": 1, "column": 12,
                                       "rule": "color-no-invalid-hex",
                                       "severity": "error",
                                       "text": "Unexpected invalid hex color"}]}])
SL_IGNORED = ("AllFilesIgnoredError: All input files were ignored because of "
              "the ignore pattern. Either change your input, ignore pattern "
              "or use \"--allow-empty-input\" to allow no inputs\n")
SL_NO_CONFIG = "ConfigurationError: No configuration provided for x.css\n"


def _adapter(directory: str, filename: str):
    """The adapter as a module — the arms below need a tool that is not here."""
    path = VALIDATORS / directory / filename
    spec = importlib.util.spec_from_file_location(
        "adapter_1601_" + directory.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(mod, monkeypatch, capsys, target, runner) -> dict:
    monkeypatch.setattr(mod.subprocess, "run", runner)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/stub")
    monkeypatch.setattr(mod.sys, "argv", ["adapter.py", str(target)])
    mod.main()
    return json.loads(capsys.readouterr().out.strip())


def _assert_skip(out: dict, needle: str) -> None:
    assert "skipped" in out, out
    assert needle in out["skipped"].lower(), out
    for key in ("ok", "count", "errors"):
        assert key not in out, "a skip must not carry " + repr(key) + ": " + repr(out)


# ---------------------------------------------------------------------------
# prettier-check — `.prettierignore` exits 0 with nothing on either stream
# ---------------------------------------------------------------------------

class _Prettier:
    """`--check` answers clean; the `--file-info` probe answers as configured."""

    def __init__(self, info: object) -> None:
        self.info = info
        self.calls: list = []
        self.kwargs: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs.append(kwargs)
        if "--file-info" in cmd:
            if isinstance(self.info, BaseException):
                raise self.info
            if isinstance(self.info, int):
                return subprocess.CompletedProcess(cmd, self.info, "", "")
            return subprocess.CompletedProcess(cmd, 0, self.info, "")
        return subprocess.CompletedProcess(cmd, 0, "Checking formatting...\n", "")


@pytest.fixture()
def prettier():
    return _adapter("prettier-check", "prettier-check.py")


def test_prettier_an_ignored_file_is_the_third_state(
    prettier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """An ignored file exits 0 with no output — byte for byte a clean check."""
    f = tmp_path / "vendored.js"
    f.write_text("const a=1\n", encoding="utf-8")
    stub = _Prettier('{"ignored": true, "inferredParser": null}')
    out = _drive(prettier, monkeypatch, capsys, f, stub)
    assert len(stub.calls) == 2, "the probe never ran: " + repr(stub.calls)
    _assert_skip(out, "ignore")


@pytest.mark.parametrize(
    "probe",
    [2, OSError("gone"), subprocess.TimeoutExpired("prettier", 10),
     "not json at all", '{"inferredParser": "babel"}'],
    ids=["nonzero", "oserror", "timeout", "nonjson", "no-ignored-key"],
)
def test_prettier_a_probe_that_cannot_answer_is_not_a_clean_pass(
    prettier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path, probe: object,
) -> None:
    """A zero it could not attribute is an absence, not a verdict."""
    f = tmp_path / "x.js"
    f.write_text("const a = 1;\n", encoding="utf-8")
    out = _drive(prettier, monkeypatch, capsys, f, _Prettier(probe))
    _assert_skip(out, "could not say")


def test_prettier_a_file_it_did_check_still_gets_a_verdict(
    prettier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    f = tmp_path / "x.js"
    f.write_text("const a = 1;\n", encoding="utf-8")
    out = _drive(prettier, monkeypatch, capsys, f,
                 _Prettier('{"ignored": false, "inferredParser": "babel"}'))
    assert out["ok"] is True, out
    assert out["count"] == 0, out


def test_prettier_probe_resolves_the_same_ignore_file_as_the_check(
    prettier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """A probe reading a different ignore set answers about a different run."""
    monkeypatch.setenv("PRETTIER_IGNORE_PATH", str(tmp_path / "custom.ignore"))
    monkeypatch.setenv("PRETTIER_CONFIG", str(tmp_path / "custom.json"))
    f = tmp_path / "x.js"
    f.write_text("const a = 1;\n", encoding="utf-8")
    stub = _Prettier('{"ignored": false, "inferredParser": "babel"}')
    _drive(prettier, monkeypatch, capsys, f, stub)
    probe_cmd = [c for c in stub.calls if "--file-info" in c][0]
    assert "--ignore-path" in probe_cmd, probe_cmd
    assert "--config" in probe_cmd, probe_cmd


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not installed")
def test_prettier_end_to_end_against_a_real_prettierignore(tmp_path: Path) -> None:
    """The stubs above are transcripts; this is the tool itself."""
    (tmp_path / ".prettierignore").write_text("vendored.js\n", encoding="utf-8")
    (tmp_path / "vendored.js").write_text("const a=1\n", encoding="utf-8")
    adapter = VALIDATORS / "prettier-check" / "prettier-check.py"
    r = subprocess.run([sys.executable, str(adapter), "vendored.js"],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    out = json.loads(r.stdout)
    _assert_skip(out, "ignore")


# ---------------------------------------------------------------------------
# markdownlint — zero files resolved exits 0 and prints the help banner
# ---------------------------------------------------------------------------

def _md_run(stdout: str, stderr: str = "", rc: int = 0):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    return runner


def test_markdownlint_output_on_a_zero_exit_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """`.markdownlintignore` match, or a path that is not there: help + exit 0."""
    mod = _adapter("markdownlint", "markdownlint.py")
    f = tmp_path / "ignored.md"
    f.write_text("#bad\n", encoding="utf-8")
    out = _drive(mod, monkeypatch, capsys, f, _md_run(MD_HELP))
    _assert_skip(out, "no files")
    assert "Usage: markdownlint" in out["skipped"], out


def test_markdownlint_a_silent_zero_exit_is_a_clean_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A file it really did lint clean prints nothing on either stream."""
    mod = _adapter("markdownlint", "markdownlint.py")
    f = tmp_path / "clean.md"
    f.write_text("# Title\n\nText.\n", encoding="utf-8")
    out = _drive(mod, monkeypatch, capsys, f, _md_run(""))
    assert out["ok"] is True, out
    assert out["count"] == 0, out


# ---------------------------------------------------------------------------
# stylelint — the report is on stderr, and empty was never a verdict
# ---------------------------------------------------------------------------

def _sl_run(stdout: str, stderr: str, rc: int):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    return runner


@pytest.fixture()
def stylelint():
    return _adapter("stylelint", "stylelint.py")


def test_stylelint_reads_the_report_stylelint_actually_writes(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """stylelint 16+ writes the report to stderr (`cli.mjs`: `stderr.write(report)`).

    Reading stdout alone made every CSS file clean — including this one, which
    carries a real error.
    """
    f = tmp_path / "x.css"
    f.write_text("a { color: #zzz; }\n", encoding="utf-8")
    out = _drive(stylelint, monkeypatch, capsys, f, _sl_run("", SL_WARNED, 2))
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    assert out["errors"][0]["code"] == "color-no-invalid-hex", out
    assert out["errors"][0]["line"] == 1, out


def test_stylelint_reads_a_report_on_stdout_too(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """Older stylelint wrote it to stdout; the stream is not the question."""
    f = tmp_path / "x.css"
    f.write_text("a { color: #zzz; }\n", encoding="utf-8")
    out = _drive(stylelint, monkeypatch, capsys, f, _sl_run(SL_WARNED, "", 2))
    assert out["ok"] is False, out
    assert out["count"] == 1, out


def test_stylelint_a_clean_report_is_a_clean_verdict(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    f = tmp_path / "x.css"
    f.write_text("a {\n  color: #fff;\n}\n", encoding="utf-8")
    out = _drive(stylelint, monkeypatch, capsys, f, _sl_run("", SL_CLEAN, 0))
    assert out["ok"] is True, out
    assert out["count"] == 0, out


def test_stylelint_an_ignored_file_is_the_third_state(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """`.stylelintignore` — stylelint names it, so no probe has to be invented."""
    f = tmp_path / "vendor.css"
    f.write_text("a { color: #zzz; }\n", encoding="utf-8")
    out = _drive(stylelint, monkeypatch, capsys, f, _sl_run("", SL_IGNORED, 1))
    _assert_skip(out, "ignore")


@pytest.mark.parametrize(
    "stdout,stderr,rc",
    [("", SL_NO_CONFIG, 78), ("", "", 0), ("not json", "", 0)],
    ids=["config-error", "silent-zero-exit", "unparseable"],
)
def test_stylelint_no_report_is_never_a_clean_pass(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path, stdout: str, stderr: str, rc: int,
) -> None:
    """No report means nothing was said about the file, whatever the exit code."""
    f = tmp_path / "x.css"
    f.write_text("a { color: #zzz; }\n", encoding="utf-8")
    out = _drive(stylelint, monkeypatch, capsys, f, _sl_run(stdout, stderr, rc))
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter", out


def test_stylelint_absent_is_the_third_state_not_a_failed_edit(
    stylelint, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """Uninstalled is `skipped`, and escalatable — not an `adapter` error (#1202).

    A loud non-verdict here failed every CSS edit on any machine that never
    installed stylelint, which is the case #665 decided the other way. It is
    the one absence arm in this file that was not routed through
    `refusal.absent()`.
    """
    f = tmp_path / "x.css"
    f.write_text("a { color: #fff; }\n", encoding="utf-8")
    monkeypatch.setattr(stylelint.shutil, "which", lambda _name: None)
    monkeypatch.setattr(stylelint.sys, "argv", ["stylelint.py", str(f)])
    stylelint.main()
    out = json.loads(capsys.readouterr().out.strip())
    _assert_skip(out, "stylelint not found")


def test_pyright_no_output_at_all_is_not_a_clean_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Empty stdout *and* empty stderr was `ok: true` — the same fabrication.

    Adjacent to the three the issue names, found sweeping the class.
    """
    mod = _adapter("pyright", "pyright.py")
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "")

    out = _drive(mod, monkeypatch, capsys, f, runner)
    _assert_skip(out, "no output")


@pytest.mark.parametrize(
    "line",
    ["b.md:1:1 error MD018/no-missing-space-atx No space after hash",
     "b.md:1:1 MD018/no-missing-space-atx No space after hash",
     "b.md:1 error MD041/first-line-heading/first-line-h1 First line"],
    ids=["with-severity", "without-severity", "no-column"],
)
def test_markdownlint_locates_a_finding_in_both_output_shapes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
    line: str,
) -> None:
    """markdownlint-cli 0.49 prints a severity word the row parser did not expect.

    Found measuring #1601 against the real CLI, not by the issue: every row
    missed the pattern and fell through to the whole-output catch-all, so four
    located findings arrived as one `lint` error with `line: null`, no source
    context and a count of 1.
    """
    mod = _adapter("markdownlint", "markdownlint.py")
    f = tmp_path / "b.md"
    f.write_text("#bad heading\n\ntext\n", encoding="utf-8")
    out = _drive(mod, monkeypatch, capsys, f, _md_run(line + "\n", rc=1))
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 1, err
    assert err["code"].startswith("MD"), err
    assert err["source_context"], err


class _Clock:
    """`time.time()`: the first reading, then a fixed later one."""

    def __init__(self, ticks) -> None:
        self.ticks = list(ticks)

    def __call__(self) -> float:
        return self.ticks.pop(0) if len(self.ticks) > 1 else self.ticks[0]


@pytest.mark.parametrize(
    "elapsed,expected", [(2.0, 13.0), (14.5, 1.0)],
    ids=["room-left", "budget-nearly-spent"],
)
def test_prettier_probe_is_bounded_by_what_is_left_of_the_budget(
    prettier, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    tmp_path: Path, elapsed: float, expected: float,
) -> None:
    """Two full walls would put the adapter at 2x the timeout it is registered with.

    The core kills an adapter at its own wall, and the caller then gets
    `NOT CHECKED (timed out)` naming no question. A probe out of clock declines
    and says which one went unanswered, so the budget is shared, not doubled.
    """
    f = tmp_path / "x.js"
    f.write_text("const a = 1;\n", encoding="utf-8")
    monkeypatch.setattr(prettier.time, "time", _Clock([0.0, elapsed]))
    stub = _Prettier('{"ignored": false, "inferredParser": "babel"}')
    _drive(prettier, monkeypatch, capsys, f, stub)
    probe = [k for c, k in zip(stub.calls, stub.kwargs) if "--file-info" in c][0]
    assert probe["timeout"] == expected, probe
    assert probe["timeout"] <= prettier.TIMEOUT_S, probe


def test_pyright_no_output_does_not_escalate_as_an_absent_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """`refusal.absent()` is for a tool that is not there, and says so in words.

    Routed through it, an operator who named pyright in the variable was told
    it "could not run" about a pyright that ran and simply printed nothing.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "pyright")
    mod = _adapter("pyright", "pyright.py")
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "")

    out = _drive(mod, monkeypatch, capsys, f, runner)
    _assert_skip(out, "no output")
