"""Tests for the @file input route on mutating ops (edit, replace, replace_lines, paste, vim)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, name: str, payload: dict) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


# ---------------------------------------------------------------------------
# edit:@file
# ---------------------------------------------------------------------------

class TestAtFileEdit:
    def test_basic_edit(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\nb = 2\n")
        spec = _write_json(tmp_path, "e.json", {"old": "a = 1", "new": "a = 99", "path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert "edited" in out
        assert "a = 99" in target.read_text()

    def test_missing_field_returns_error(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\n")
        spec = _write_json(tmp_path, "e.json", {"old": "a = 1", "path": str(target)})  # missing 'new'
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "new" in out

    def test_nonexistent_at_file_returns_error(self, tmp_path: Path) -> None:
        out = supertool.dispatch(f"edit:@{tmp_path}/does_not_exist.json")
        assert "ERROR" in out
        assert "not found" in out

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "bad.json"
        # Payload format auto-detected: starts with `{` → JSON route.
        spec.write_text("{not valid json {{{")
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "JSON parse error" in out

    def test_payload_not_object_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "arr.json"
        spec.write_text('["a", "b"]')
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out

    def test_validators_still_run(self, tmp_path: Path, monkeypatch) -> None:
        """_run_with_validators must be called even via @file route."""
        called = []

        original = supertool._run_with_validators

        def spy(op, parts, do_op):
            called.append(op)
            return original(op, parts, do_op)

        monkeypatch.setattr(supertool, "_run_with_validators", spy)
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        spec = _write_json(tmp_path, "e.json", {"old": "foo", "new": "bar", "path": str(target)})
        supertool.dispatch(f"edit:@{spec}")
        assert "edit" in called

    def test_header_contains_at_ref(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        spec = _write_json(tmp_path, "e.json", {"old": "foo", "new": "bar", "path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert f"edit:@{spec}" in out


# ---------------------------------------------------------------------------
# replace:@file
# ---------------------------------------------------------------------------

class TestAtFileReplace:
    def test_basic_replace(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo bar foo\n")
        spec = _write_json(tmp_path, "r.json", {"old": "foo", "new": "baz", "path": str(target)})
        out = supertool.dispatch(f"replace:@{spec}")
        assert "ERROR" not in out
        assert "foo" not in target.read_text()
        assert "baz" in target.read_text()


# ---------------------------------------------------------------------------
# replace_lines:@file
# ---------------------------------------------------------------------------

class TestAtFileReplaceLines:
    def test_basic_replace_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("line1\nline2\nline3\n")
        spec = _write_json(tmp_path, "rl.json", {
            "path": str(target), "start": 2, "end": 2, "content": "REPLACED"
        })
        out = supertool.dispatch(f"replace_lines:@{spec}")
        assert "replaced" in out
        assert target.read_text() == "line1\nREPLACED\nline3\n"

    def test_missing_start_field_returns_error(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("line1\n")
        spec = _write_json(tmp_path, "rl.json", {"path": str(target), "end": 1, "content": "X"})
        out = supertool.dispatch(f"replace_lines:@{spec}")
        assert "ERROR" in out
        assert "start" in out


# ---------------------------------------------------------------------------
# paste:@file
# ---------------------------------------------------------------------------

class TestAtFilePaste:
    def test_basic_paste(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("old content\n")
        spec = _write_json(tmp_path, "p.json", {"path": str(target), "content": "new content"})
        out = supertool.dispatch(f"paste:@{spec}")
        assert "rewrote" in out
        assert "new content" in target.read_text()

    def test_paste_creates_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new_file.py"
        spec = _write_json(tmp_path, "p.json", {"path": str(target), "content": "hello"})
        out = supertool.dispatch(f"paste:@{spec}")
        assert "created" in out
        assert target.exists()


# ---------------------------------------------------------------------------
# @- stdin route
# ---------------------------------------------------------------------------

class TestAtFileStdin:
    def test_edit_from_stdin(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "x.py"
        target.write_text("alpha\n")
        payload = json.dumps({"old": "alpha", "new": "beta", "path": str(target)})
        import io
        monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(payload))
        out = supertool.dispatch("edit:@-")
        assert "edited" in out
        assert "beta" in target.read_text()


# ---------------------------------------------------------------------------
# Non-mutating op — @file route NOT applied (plain parse fallback)
# ---------------------------------------------------------------------------

class TestAtFileNotAppliedToReadOps:
    def test_read_at_file_not_intercepted(self, tmp_path: Path) -> None:
        """read:@something should NOT go through the @file route — it's not a mutating op."""
        # Create a file literally named @something (edge case — just verify no crash)
        # More practically: read:@path is just a weird path, handled by op_read as "file not found"
        out = supertool.dispatch(f"read:@{tmp_path}/nonexistent.json")
        # Should be a normal "file not found" error from op_read, not @file machinery
        assert "ERROR" in out or "not found" in out.lower()


# ---------------------------------------------------------------------------
# Refactor 1 — _fields_from_syntax: derive field list from syntax string
# ---------------------------------------------------------------------------

class TestFieldsFromSyntax:
    @pytest.mark.parametrize("syntax,expected", [
        ("edit:::OLD:::NEW:::PATH",           ["old", "new", "path"]),
        ("replace:::OLD:::NEW:::PATH",        ["old", "new", "path"]),
        ("replace_lines:::PATH:::START:::END:::CONTENT", ["path", "start", "end", "content"]),
        ("paste:::PATH:::CONTENT",            ["path", "content"]),
        ("vim:::PATH:::SCRIPT",               ["path", "script"]),
        # First alternative is used when ' | ' separates alternatives
        ("op:::A:::B | op:::X:::Y",           ["a", "b"]),
        # Read-only op (no :::) → empty list
        ("read:PATH",                         []),
        ("grep:PATTERN:PATH",                 []),
    ])
    def test_fields_from_syntax(self, syntax: str, expected: list) -> None:
        assert supertool._fields_from_syntax(syntax) == expected

    def test_builtin_defaults_cover_all_write_ops(self) -> None:
        """_AT_FILE_BUILTIN_DEFAULTS must derive the same field lists as the hardcoded table."""
        expected = {
            "edit":          ["old", "new", "path"],
            "replace":       ["old", "new", "path"],
            "replace_dry":   ["old", "new", "path"],
            "replace_lines": ["path", "start", "end", "content"],
            "paste":         ["path", "content"],
            "vim":           ["path", "script"],
        }
        assert supertool._AT_FILE_BUILTIN_DEFAULTS == expected

    def test_registry_populated_after_first_dispatch(self, tmp_path: Path) -> None:
        """After any dispatch call the registry must contain the builtin write ops."""
        # Force a dispatch so the registry is built
        supertool.dispatch(f"read:{tmp_path}/nope")
        for op in ("edit", "replace", "replace_dry", "replace_lines", "paste", "vim"):
            assert supertool._at_file_fields(op), f"missing @file fields for op '{op}'"


# ---------------------------------------------------------------------------
# Refactor 1 — case-insensitive payload key matching
# ---------------------------------------------------------------------------

class TestAtFileCaseInsensitiveKeys:
    def test_uppercase_keys_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("hello world\n")
        spec = _write_json(tmp_path, "e.json", {"OLD": "hello", "NEW": "hi", "PATH": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert "edited" in out
        assert "hi world" in target.read_text()

    def test_mixed_case_keys_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        spec = _write_json(tmp_path, "e.json", {"Old": "foo", "New": "bar", "Path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert "edited" in out
        assert "bar" in target.read_text()


# ---------------------------------------------------------------------------
# Refactor 2 — replace_all via @file route
# ---------------------------------------------------------------------------

class TestAtFileReplaceAll:
    def test_replace_all_true_replaces_all_occurrences(self, tmp_path: Path) -> None:
        """edit:@file with replace_all:true should replace every occurrence."""
        target = tmp_path / "x.py"
        target.write_text("foo bar foo baz foo\n")
        spec = _write_json(tmp_path, "e.json", {
            "old": "foo", "new": "qux", "path": str(target), "replace_all": True
        })
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" not in out
        text = target.read_text()
        assert "foo" not in text
        assert text.count("qux") == 3

    def test_replace_all_false_errors_on_multiple_occurrences(self, tmp_path: Path) -> None:
        """edit:@file with replace_all:false should use edit semantics (error on >1 match)."""
        target = tmp_path / "x.py"
        target.write_text("foo bar foo\n")
        spec = _write_json(tmp_path, "e.json", {
            "old": "foo", "new": "qux", "path": str(target), "replace_all": False
        })
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "2" in out  # reports the count

    def test_replace_all_absent_uses_edit_semantics(self, tmp_path: Path) -> None:
        """edit:@file without replace_all key uses single-occurrence edit semantics."""
        target = tmp_path / "x.py"
        target.write_text("foo bar foo\n")
        spec = _write_json(tmp_path, "e.json", {"old": "foo", "new": "qux", "path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out  # two occurrences → error

    def test_replace_all_true_single_occurrence_succeeds(self, tmp_path: Path) -> None:
        """replace_all:true with a single match still succeeds."""
        target = tmp_path / "x.py"
        target.write_text("unique token here\n")
        spec = _write_json(tmp_path, "e.json", {
            "old": "unique token", "new": "replaced", "path": str(target), "replace_all": True
        })
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" not in out
        assert "replaced" in target.read_text()

    def test_replace_all_via_batch_op(self, tmp_path: Path) -> None:
        """replace_all:true inside a batch payload should also replace all occurrences."""
        target = tmp_path / "x.py"
        target.write_text("a a a\n")
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps([
            {"op": "edit", "old": "a", "new": "b", "path": str(target), "replace_all": True}
        ]))
        out = supertool.dispatch(f"batch:@{ops_file}")
        assert "ERROR" not in out
        assert target.read_text().strip() == "b b b"


# ---------------------------------------------------------------------------
# TOML payload route — auto-detected when first non-whitespace char is not { or [
# ---------------------------------------------------------------------------

class TestTomlPayload:
    def test_paste_with_triple_literal_preserves_backslash(self, tmp_path: Path) -> None:
        target = tmp_path / "out.sh"
        spec = tmp_path / "p.toml"
        spec.write_text(
            f'path = "{target}"\n'
            "content = '''\n"
            "claude -p \"...\" --permission-mode bypass \\\n"
            "  --disallowedTools \"Grep,Glob\"\n"
            "'''\n"
        )
        out = supertool.dispatch(f"paste:@{spec}")
        assert "ERROR" not in out
        # The trailing backslash + newline survives the round-trip — that's
        # the whole point of the TOML route over JSON.
        body = target.read_text()
        assert "bypass \\\n" in body
        assert "--disallowedTools" in body

    def test_edit_with_double_quoted_string(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("DEBUG = False\n")
        spec = tmp_path / "e.toml"
        spec.write_text(
            'old = "DEBUG = False"\n'
            'new = "DEBUG = True"\n'
            f'path = "{target}"\n'
        )
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" not in out
        assert target.read_text() == "DEBUG = True\n"

    def test_replace_lines_with_integers_and_triple_literal(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a\nb\nc\nd\n")
        spec = tmp_path / "rl.toml"
        spec.write_text(
            f'path = "{target}"\n'
            "start = 2\n"
            "end = 3\n"
            "content = '''X\nY'''\n"
        )
        out = supertool.dispatch(f"replace_lines:@{spec}")
        assert "ERROR" not in out
        assert target.read_text() == "a\nX\nY\nd\n"

    def test_stdin_toml(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "out.txt"
        import io
        payload = (
            f'path = "{target}"\n'
            "content = '''line1\nline2 with \\ backslash\nline3'''\n"
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        out = supertool.dispatch("paste:@-")
        assert "ERROR" not in out
        assert target.read_text() == "line1\nline2 with \\ backslash\nline3\n"

    def test_format_auto_detect_brace_routes_to_json(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\n")
        spec = tmp_path / "e.payload"
        # Starts with `{` → JSON route even though extension isn't .json.
        spec.write_text(json.dumps({"old": "a = 1", "new": "a = 2", "path": str(target)}))
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" not in out
        assert target.read_text() == "a = 2\n"

    def test_invalid_toml_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "bad.toml"
        spec.write_text("not = 'unclosed\n")
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "TOML parse error" in out


# ---------------------------------------------------------------------------
# Mini TOML parser fallback (used when stdlib tomllib unavailable, Python <3.11)
# ---------------------------------------------------------------------------

class TestMiniTomlLoads:
    def test_double_quoted_with_escapes(self) -> None:
        d = supertool._mini_toml_loads('a = "x\\ny"\n')
        assert d == {"a": "x\ny"}

    def test_single_quoted_literal(self) -> None:
        d = supertool._mini_toml_loads("a = 'x\\ny'\n")
        assert d == {"a": "x\\ny"}

    def test_triple_basic_with_leading_newline_stripped(self) -> None:
        d = supertool._mini_toml_loads('a = """\nhello"""\n')
        assert d == {"a": "hello"}

    def test_triple_literal_preserves_backslash(self) -> None:
        d = supertool._mini_toml_loads("a = '''\nlinex \\\nliney'''\n")
        assert d == {"a": "linex \\\nliney"}

    def test_int_and_bool(self) -> None:
        d = supertool._mini_toml_loads("n = 42\nm = -5\nt = true\nf = false\n")
        assert d == {"n": 42, "m": -5, "t": True, "f": False}

    def test_comments_skipped(self) -> None:
        d = supertool._mini_toml_loads("# header\na = 1  # trailing\nb = 2\n")
        assert d == {"a": 1, "b": 2}

    def test_bad_key_raises(self) -> None:
        with pytest.raises(ValueError):
            supertool._mini_toml_loads("= 1\n")

    def test_unterminated_string_raises(self) -> None:
        with pytest.raises(ValueError):
            supertool._mini_toml_loads('a = "unterminated\n')
