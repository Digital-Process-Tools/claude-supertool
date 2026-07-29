"""#482 — `lsp-diag` must not return a confident clean verdict it cannot substantiate.

Two separate ways the adapter renders an `ok` that carries no information:

1. **Stale document.** cclsp's `getDiagnostics` returns `serverState.diagnostics`
   — a per-URI cache filled by `publishDiagnostics` — and short-circuits on a
   cache hit before it ever re-reads the file. `ensureFileOpen` returns early
   when the file is already open, so the daemon keeps answering about the bytes
   it saw at open time. Nothing invalidates that entry for the daemon's life:
   measured on this repo, a `.py` file opened clean and then broken on disk
   still reported "No diagnostics found" after a 20s wait and an mtime bump,
   while a cold daemon reported the unterminated literal instantly. The
   framework's own pre-edit baseline pass is what opens the document, so the
   after-check is stale by construction on exactly the file just edited.

2. **Infra conditions read as a clean bill.** `_mcp_call_or_message` prefixes
   infra output with the op name (`diag: MCP error: ...`) so adapters can drop
   it instead of counting it as a finding. `parse_cclsp_diagnostics` duly drops
   it — and the adapter then emitted `{"ok": true, "count": 0}`, i.e. the
   dropped infra condition became a pass. #488 makes "no daemon" the common
   case, so this path stops being an edge case.

Both must render as an absence of information (`skipped`), never as a verdict.
Pinned in both directions: a genuinely clean file with a fresh view must still
report ok, and py-syntax (#479) must keep its row and its rollback.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import supertool


_VPATH = Path(__file__).parent.parent / "validators" / "lsp-diag" / "lsp-diag.py"
_spec = importlib.util.spec_from_file_location("lsp_diag_482", _VPATH)
assert _spec and _spec.loader
lsp_diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lsp_diag)


CLEAN_TEXT = (
    "--- diag:/tmp/target.py ---\n"
    "No diagnostics found for /tmp/target.py. "
    "The file has no errors, warnings, or hints.\n"
)
REAL_DIAGNOSTIC_TEXT = (
    "--- diag:/tmp/target.py ---\n"
    "Found 1 diagnostic in /tmp/target.py:\n"
    "\n"
    "• [error] String literal is unterminated at line 2, col 5\n"
)

VALID_PY = "x = 1\ny = 2\n"
BROKEN_PY = 'x = 1\ny = "unterminated\n'

STALE_ENV = "SUPERTOOL_LSP_DOC_MAYBE_STALE"
RESYNC_ENV = "SUPERTOOL_LSP_RESYNC_ON_QUERY"


def _run_adapter(tmp_path: Path, diag_text: str, *, env_extra: dict | None = None,
                 content: str = VALID_PY) -> dict:
    """Run the real adapter against a stub `supertool` that prints `diag_text`.

    Exercises the shipped `main()`, not a helper written for the test — the
    receipt asserted on is the one the framework would consume.
    """
    target = tmp_path / "target.py"
    target.write_text(content, encoding="utf-8")
    out_file = tmp_path / "diag_out.txt"
    out_file.write_text(diag_text, encoding="utf-8")
    stub = tmp_path / "fake_supertool.py"
    stub.write_text(
        "import sys\n"
        f"sys.stdout.write(open({str(out_file)!r}, encoding='utf-8').read())\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items()
           if k not in (STALE_ENV, RESYNC_ENV)}
    env["SUPERTOOL_BIN"] = str(stub)
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(_VPATH), str(target)],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.stdout.strip(), f"adapter produced no output; stderr:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _assert_not_a_verdict(receipt: dict, *, why: str) -> None:
    assert "skipped" in receipt, f"{why}\nreceipt: {receipt}"
    assert receipt.get("ok") is not True, (
        f"{why} — receipt carries ok=True alongside the skip, so a reader (or a "
        f"consumer keying off `ok`) still sees a pass.\nreceipt: {receipt}"
    )


class TestStaleDocumentIsNotAVerdict:
    """Direction 1: an unsubstantiated view must never render as ok."""

    def test_stale_document_is_skipped_not_ok(self, tmp_path: Path) -> None:
        """The reported case: file broken on disk, daemon answers from its cache."""
        receipt = _run_adapter(tmp_path, CLEAN_TEXT,
                               env_extra={STALE_ENV: "1"}, content=BROKEN_PY)
        with pytest.raises(SyntaxError):
            ast.parse(BROKEN_PY)  # ground truth: the bytes on disk do not parse
        _assert_not_a_verdict(
            receipt,
            why="#482: the daemon answered about the pre-edit document and the "
                "adapter passed that off as a clean verdict",
        )

    def test_stale_skip_names_the_reason(self, tmp_path: Path) -> None:
        """`skipped` alone sends the reader to the config; name the mechanism."""
        receipt = _run_adapter(tmp_path, CLEAN_TEXT, env_extra={STALE_ENV: "1"})
        reason = str(receipt.get("skipped", "")).lower()
        assert "stale" in reason, f"skip reason does not name staleness: {receipt}"

    def test_stale_flag_also_suppresses_a_findings_verdict(self, tmp_path: Path) -> None:
        """Errors from a stale view are the *previous* version's, at its columns.

        Reporting them is as wrong as reporting a clean bill — they point at
        lines the caller no longer has.
        """
        receipt = _run_adapter(tmp_path, REAL_DIAGNOSTIC_TEXT,
                               env_extra={STALE_ENV: "1"})
        assert "skipped" in receipt, (
            "stale findings were reported as current findings; their line/col "
            f"describe the pre-edit file.\nreceipt: {receipt}"
        )


class TestInfraConditionIsNotAVerdict:
    """Direction 1, one layer down (#488 makes these common)."""

    def test_mcp_error_is_skipped_not_silently_ok(self, tmp_path: Path) -> None:
        receipt = _run_adapter(
            tmp_path, "--- diag:/tmp/target.py ---\ndiag: MCP error: broken pipe\n")
        _assert_not_a_verdict(
            receipt,
            why="#482: an MCP transport error was dropped by the op_name guard "
                "and the empty result rendered as a pass",
        )

    def test_unavailable_daemon_is_a_pure_skip(self, tmp_path: Path) -> None:
        """#488 stops validators spawning daemons, so this is the normal miss."""
        receipt = _run_adapter(
            tmp_path,
            "--- diag:/tmp/target.py ---\ndiag: MCP server 'py-lsp' unavailable\n")
        _assert_not_a_verdict(
            receipt,
            why="#482/#488: 'no daemon available' must render as clearly "
                "not-a-verdict",
        )

    def test_no_result_is_skipped(self, tmp_path: Path) -> None:
        receipt = _run_adapter(
            tmp_path,
            "--- diag:/tmp/target.py ---\ndiag: no result from get_diagnostics\n")
        _assert_not_a_verdict(receipt, why="#482: an empty MCP result is not a pass")

    def test_no_lsp_configured_is_skipped(self, tmp_path: Path) -> None:
        receipt = _run_adapter(
            tmp_path,
            "--- diag:/tmp/target.py ---\n"
            "diag: no LSP configured for /tmp/target.py (add mcp.diag mapping)\n")
        _assert_not_a_verdict(receipt, why="#482: no route configured is not a pass")


class TestFreshViewStillAnswers:
    """Direction 2 — these must pass before AND after the fix.

    Without them, "make lsp-diag always skip" would be a green implementation.
    """

    def test_clean_file_with_fresh_view_still_reports_ok(self, tmp_path: Path) -> None:
        receipt = _run_adapter(tmp_path, CLEAN_TEXT)
        assert "skipped" not in receipt, (
            f"a fresh, genuinely clean view must still render clean: {receipt}")
        assert receipt["ok"] is True
        assert receipt["count"] == 0

    def test_real_diagnostics_with_fresh_view_still_reported(self, tmp_path: Path) -> None:
        receipt = _run_adapter(tmp_path, REAL_DIAGNOSTIC_TEXT)
        assert "skipped" not in receipt, f"findings were swallowed: {receipt}"
        assert receipt["ok"] is False
        assert receipt["count"] == 1
        assert receipt["errors"][0]["line"] == 2

    def test_resync_opt_in_answers_despite_stale_flag(self, tmp_path: Path) -> None:
        """A server that re-reads the file on every query is not stale.

        The escape hatch for any MCP LSP whose diagnostics are not a
        never-invalidated cache — cclsp's are, but that is cclsp's choice.
        """
        receipt = _run_adapter(tmp_path, REAL_DIAGNOSTIC_TEXT,
                               env_extra={STALE_ENV: "1", RESYNC_ENV: "1"})
        assert "skipped" not in receipt, (
            f"declared-resyncing server was skipped anyway: {receipt}")
        assert receipt["count"] == 1

    def test_parser_untouched(self) -> None:
        """The text parser is not the bug and must not move."""
        assert lsp_diag.parse_cclsp_diagnostics(
            "No diagnostics found for /x. The file has no errors, warnings, "
            "or hints.", "/x") == []
        errs = lsp_diag.parse_cclsp_diagnostics(
            "• [error] undefined variable $foo at line 42, col 13", "/x")
        assert errs[0]["line"] == 42 and errs[0]["severity"] == "error"


class TestFrameworkStampsStaleness:
    """The framework owns the fact — only it knows a baseline pass just ran."""

    @staticmethod
    def _capture(monkeypatch) -> list:
        seen: list = []

        def fake_run_one(name, spec, file, *args, **kwargs):
            seen.append({"name": name, "file": file,
                         "args": args, "kwargs": kwargs})
            return {"tool": name, "file": file, "ok": True, "count": 0,
                    "errors": [], "duration_ms": 1}

        monkeypatch.setattr(supertool, "_validator_run_one", fake_run_one)
        return seen

    @staticmethod
    def _stale_flags(seen: list, name: str = "lsp-diag") -> list:
        """The staleness signal each `name` _validator_run_one call carried.

        Filtered by name: py-syntax (#479) is a builtin backstop that runs on
        the same .py path, so every pass produces two calls, not one.
        """
        out = []
        for call in seen:
            if call["name"] != name:
                continue
            if "doc_maybe_stale" in call["kwargs"]:
                out.append(bool(call["kwargs"]["doc_maybe_stale"]))
            elif call["args"]:
                out.append(bool(call["args"][0]))
            else:
                out.append(False)
        return out

    def _configure(self) -> None:
        supertool._CONFIG = {
            "validators": {
                "lsp-diag": {"cmd": "{python} -c pass", "hooks_into": ["edit"],
                             "match": "*.py"},
            }
        }
        supertool._CONFIG_CHECKED = True

    def test_after_pass_on_existing_file_is_flagged_stale(
            self, tmp_path: Path, monkeypatch) -> None:
        seen = self._capture(monkeypatch)
        self._configure()
        f = tmp_path / "t.py"
        f.write_text(VALID_PY, encoding="utf-8")

        supertool._run_with_validators(
            "edit", ["edit", "", "", str(f)],
            lambda: (f.write_text("x = 3\n", encoding="utf-8"), "done\n")[1])

        flags = self._stale_flags(seen)
        assert len(flags) == 2, f"expected a before and an after pass, got {seen}"
        assert flags == [False, True], (
            "#482: the framework must tell the after-pass that its own baseline "
            f"opened the document in the warm daemon; got {flags}")

    def test_new_file_after_pass_is_not_flagged_stale(
            self, tmp_path: Path, monkeypatch) -> None:
        """Nothing was open pre-op — the after-pass is the first look."""
        seen = self._capture(monkeypatch)
        self._configure()
        f = tmp_path / "brand_new.py"  # does not exist pre-op

        supertool._run_with_validators(
            "edit", ["edit", "", "", str(f)],
            lambda: (f.write_text(VALID_PY, encoding="utf-8"), "created\n")[1])

        assert self._stale_flags(seen) == [False, False], (
            "a file the daemon never had open is not stale; flagging it would "
            "turn every new-file check into a skip")

    def test_stale_flag_reaches_the_adapter_env(self, tmp_path: Path,
                                                monkeypatch) -> None:
        """The signal is only worth anything if the child process sees it."""
        captured: dict = {}

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return real_run([sys.executable, "-c",
                             "print('{\"tool\":\"lsp-diag\",\"skipped\":\"x\"}')"],
                            **{k: v for k, v in kwargs.items() if k != "env"})

        monkeypatch.setattr(supertool.subprocess, "run", fake_run)
        f = tmp_path / "t.py"
        f.write_text(VALID_PY, encoding="utf-8")

        supertool._validator_run_one(
            "lsp-diag", {"cmd": "{python} -c pass", "match": "*.py", "cache": False},
            str(f), doc_maybe_stale=True)

        env = captured.get("env")
        assert env is not None, "no env passed to the adapter — the flag cannot arrive"
        assert env.get(STALE_ENV) == "1", (
            f"{STALE_ENV} missing from the adapter's environment: "
            f"{ {k: v for k, v in (env or {}).items() if k.startswith('SUPERTOOL')} }")
        assert "PATH" in env, "ambient environment must still be inherited"

    def test_no_stale_flag_means_no_stale_env(self, tmp_path: Path,
                                              monkeypatch) -> None:
        """Opposite direction: the flag is not stamped unconditionally."""
        captured: dict = {}
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return real_run([sys.executable, "-c",
                             "print('{\"tool\":\"lsp-diag\",\"ok\":true,\"count\":0}')"],
                            **{k: v for k, v in kwargs.items() if k != "env"})

        monkeypatch.setattr(supertool.subprocess, "run", fake_run)
        f = tmp_path / "t.py"
        f.write_text(VALID_PY, encoding="utf-8")

        supertool._validator_run_one(
            "lsp-diag", {"cmd": "{python} -c pass", "match": "*.py", "cache": False},
            str(f))

        env = captured.get("env") or {}
        assert env.get(STALE_ENV) is None, (
            f"baseline pass must not be told it is stale: {env.get(STALE_ENV)}")


class TestRenderingAndPySyntaxUnharmed:
    """#479 must keep its row and its rollback, and a skip must read as a skip."""

    def test_skipped_row_renders_the_reason_not_ok(self) -> None:
        rows = supertool._validator_render_diff(
            {"tool": "lsp-diag", "ok": True, "count": 0},
            {"tool": "lsp-diag", "skipped": "stale document (warm daemon)"})
        assert len(rows) == 1
        assert "skipped" in rows[0]
        assert "stale document" in rows[0]
        assert "no new errors" not in rows[0], (
            "a skip must not borrow the clean-verdict wording — that is the "
            "self-contradiction #482 is about")

    def test_skipped_never_rolls_back(self) -> None:
        assert supertool._validator_regressed(
            {"tool": "lsp-diag", "ok": True, "count": 0},
            {"tool": "lsp-diag", "skipped": "stale document"}) is False

    def test_py_syntax_still_rolls_back_with_lsp_diag_present(
            self, tmp_path: Path, monkeypatch) -> None:
        """The self-contradiction case, end to end: both validators, one file."""
        monkeypatch.setattr(
            supertool, "_validator_run_one",
            lambda name, spec, file, *a, **k: (
                supertool._builtin_syntax_run(name, str(spec["builtin"]), file)
                if spec.get("builtin")
                else {"tool": name, "file": file, "skipped": "stale document"}))
        supertool._CONFIG = {
            "validators": {
                "lsp-diag": {"cmd": "{python} -c pass", "hooks_into": ["edit"],
                             "match": "*.py", "rollback_on_fail": False},
            }
        }
        supertool._CONFIG_CHECKED = True
        f = tmp_path / "t.py"
        f.write_text(VALID_PY, encoding="utf-8")

        out = supertool._run_with_validators(
            "edit", ["edit", "", "", str(f)],
            lambda: (f.write_text(BROKEN_PY, encoding="utf-8"), "edited\n")[1])

        assert f.read_text(encoding="utf-8") == VALID_PY, (
            f"py-syntax (#479) no longer rolls back:\n{out}")
        assert "rolled back" in out.lower()
        rows = [ln for ln in out.splitlines() if ln.startswith("py-syntax")]
        assert len(rows) == 1, f"py-syntax row duplicated:\n{out}"
        assert "lsp-diag" in out and "skipped" in out
