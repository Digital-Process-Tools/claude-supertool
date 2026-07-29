"""#477 — a mutating op that makes a .py file unparseable must roll back.

The `@file` mutating routes advertise "validators run post-edit and roll back on
a syntax failure". That guarantee was returning `ok (no new errors)` on files
that did not parse: the only Python validator wired into the chain was
`lsp-diag`, a *semantic* diagnostics pass served from a warm daemon cache, with
`rollback_on_fail: false`. Nothing in the chain ever asked "does this parse?".

These tests run with an empty config (see conftest's autouse fixture), so they
pin the guarantee the tool owns by itself — not one a repo happens to configure.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import supertool


VALID = 'def foo():\n    return 1\n\n\ndef bar():\n    return 2\n'

# The shape from the issue: a string literal opened and never closed.
BROKEN_LINE = '    x = "unterminated\n'


def _parses(p: Path) -> bool:
    try:
        ast.parse(p.read_text(encoding="utf-8"))
        return True
    except SyntaxError:
        return False


def _target(tmp_path: Path, name: str = "t.py") -> Path:
    f = tmp_path / name
    f.write_text(VALID, encoding="utf-8")
    assert _parses(f)
    return f


def _assert_rolled_back(out: str, target: Path) -> None:
    assert _parses(target), (
        "file left unparseable on disk — the edit was not rolled back:\n"
        + target.read_text(encoding="utf-8")
    )
    assert target.read_text(encoding="utf-8") == VALID, "content not restored"
    assert "rolled back" in out.lower(), f"no rollback reported in receipt:\n{out}"


class TestSyntaxRollbackPerRoute:
    def test_replace_lines_at_file_rolls_back(self, tmp_path: Path) -> None:
        """The route from the report: replace_lines:@payload.toml."""
        target = _target(tmp_path)
        payload = tmp_path / "p.toml"
        payload.write_text(
            f'path = "{target.as_posix()}"\n'
            "start = 2\nend = 2\n"
            f'content = """{BROKEN_LINE}"""\n',
            encoding="utf-8",
        )
        out = supertool.dispatch(f"replace_lines:@{payload}")
        _assert_rolled_back(out, target)

    def test_edit_at_file_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        payload = tmp_path / "e.json"
        payload.write_text(
            json.dumps({"path": str(target), "old": "    return 1",
                        "new": BROKEN_LINE.rstrip("\n")}),
            encoding="utf-8",
        )
        out = supertool.dispatch(f"edit:@{payload}")
        _assert_rolled_back(out, target)

    def test_edit_inline_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(
            f"edit:::    return 1:::{BROKEN_LINE.rstrip(chr(10))}:::{target}"
        )
        _assert_rolled_back(out, target)

    def test_replace_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(
            f"replace:::    return 1:::{BROKEN_LINE.rstrip(chr(10))}:::{target}"
        )
        _assert_rolled_back(out, target)

    def test_paste_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(f"paste:::{target}:::def foo():\n{BROKEN_LINE}")
        _assert_rolled_back(out, target)

    def test_vim_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(f'vim:::{target}::::%s/return 1/x = "oops/')
        _assert_rolled_back(out, target)

    def test_append_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(f"append:::{target}:::{BROKEN_LINE}")
        _assert_rolled_back(out, target)

    def test_batch_at_file_rolls_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        payload = tmp_path / "ops.json"
        payload.write_text(
            json.dumps([{"op": "edit", "path": str(target),
                         "old": "    return 1",
                         "new": BROKEN_LINE.rstrip("\n")}]),
            encoding="utf-8",
        )
        out = supertool.dispatch(f"batch:@{payload}")
        _assert_rolled_back(out, target)


class TestSyntaxBackstopDoesNotOverreach:
    """It must revert regressions, not police files that were already broken."""

    def test_valid_edit_is_not_rolled_back(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(f"edit:::    return 1:::    return 42:::{target}")
        assert "return 42" in target.read_text(encoding="utf-8")
        assert "rolled back" not in out.lower()

    def test_already_unparseable_file_can_still_be_edited(self, tmp_path: Path) -> None:
        """A file that did not parse before the edit has not regressed."""
        target = tmp_path / "broken.py"
        target.write_text('x = "already broken\ny = 1\n', encoding="utf-8")
        out = supertool.dispatch(f"edit:::y = 1:::y = 2:::{target}")
        assert "y = 2" in target.read_text(encoding="utf-8")
        assert "rolled back" not in out.lower()

    def test_repairing_a_broken_file_is_not_rolled_back(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.py"
        target.write_text('x = "already broken\n', encoding="utf-8")
        out = supertool.dispatch(f'paste:::{target}:::x = "fixed"\n')
        assert _parses(target)
        assert "rolled back" not in out.lower()

    def test_non_python_file_is_untouched(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.md"
        target.write_text("hello\n", encoding="utf-8")
        out = supertool.dispatch(f'paste:::{target}:::x = "unterminated\n')
        assert "unterminated" in target.read_text(encoding="utf-8")
        assert "rolled back" not in out.lower()


class TestSyntaxBackstopReceipt:
    def test_receipt_names_the_syntax_error(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        out = supertool.dispatch(
            f"edit:::    return 1:::{BROKEN_LINE.rstrip(chr(10))}:::{target}"
        )
        assert "[validators]" in out
        assert "unterminated" in out.lower()
        assert "L2" in out, f"error line not reported:\n{out}"

    def test_configured_syntax_validator_wins(self, tmp_path: Path, monkeypatch) -> None:
        """A repo that configures its own parse check is not double-checked."""
        target = _target(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "validators": {
                "mine": {
                    "cmd": "true",
                    "match": "*.py",
                    "syntax": True,
                    "hooks_into": ["edit"],
                },
            },
        })
        applicable = supertool._applicable_validators("edit", str(target))
        assert "mine" in applicable
        assert not any(
            spec.get("builtin") for spec in applicable.values()
        ), "builtin backstop should defer to a configured syntax validator"


class TestBuiltinSyntaxCheckUnit:
    def test_reports_ok_on_valid_source(self, tmp_path: Path) -> None:
        f = tmp_path / "ok.py"
        f.write_text(VALID, encoding="utf-8")
        r = supertool._validator_run_one(
            "py-syntax", {"builtin": "python"}, str(f))
        assert r is not None and r["ok"] is True and r["count"] == 0

    def test_reports_error_on_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text('x = "unterminated\n', encoding="utf-8")
        r = supertool._validator_run_one(
            "py-syntax", {"builtin": "python"}, str(f))
        assert r is not None and r["ok"] is False and r["count"] == 1
        assert r["errors"][0]["line"] == 1
        assert r["errors"][0]["code"] == "syntax"

    def test_missing_file_is_skipped_not_clean(self, tmp_path: Path) -> None:
        """`could not check` must never render as `no new errors` (#454/#469)."""
        r = supertool._validator_run_one(
            "py-syntax", {"builtin": "python"}, str(tmp_path / "gone.py"))
        assert r is not None
        assert "skipped" in r, f"absent file reported as a verdict: {r}"

    def test_unknown_builtin_is_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "ok.py"
        f.write_text(VALID, encoding="utf-8")
        r = supertool._validator_run_one("weird", {"builtin": "klingon"}, str(f))
        assert r is not None and "skipped" in r
