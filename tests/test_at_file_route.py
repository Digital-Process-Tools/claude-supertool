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
        ("edit:::OLD:::NEW:::PATH",           [("old", False, False), ("new", False, False), ("path", False, False)]),
        ("replace:::OLD:::NEW:::PATH",        [("old", False, False), ("new", False, False), ("path", False, False)]),
        ("replace_lines:::PATH:::START:::END:::CONTENT", [("path", False, False), ("start", False, False), ("end", False, False), ("content", False, False)]),
        ("paste:::PATH:::CONTENT",            [("path", False, False), ("content", False, False)]),
        ("vim:::PATH:::SCRIPT",               [("path", False, False), ("script", False, False)]),
        # Trailing optional group → optional; '...' → variadic. Brackets/ellipsis stripped from names.
        ("git-commit:::MESSAGE[:::PATHS...]", [("message", False, False), ("paths", True, True)]),
        ("op:::A[:::B]",                      [("a", False, False), ("b", True, False)]),
        # Bracket depth, not "seen any '['": a required field AFTER a closed
        # optional group stays required.
        ("op:::A[:::B]:::C",                  [("a", False, False), ("b", True, False), ("c", False, False)]),
        # First alternative is used when ' | ' separates alternatives
        ("op:::A:::B | op:::X:::Y",           [("a", False, False), ("b", False, False)]),
        # Syntax carrying prose/punctuation a payload key can't match → no @file
        # route at all (git-resolve's real syntax: comma list + inline prose).
        ("git-resolve:::SIDE:::PATH[,PATH...][:::BLOCKS]  (SIDE: ours|theirs|both)", []),
        ("op:::A:::B WITH WORDS",             []),
        # Read-only op (no :::) → empty list
        ("read:PATH",                         []),
        ("grep:PATTERN:PATH",                 []),
    ])
    def test_fields_from_syntax(self, syntax: str, expected: list) -> None:
        assert supertool._fields_from_syntax(syntax) == expected

    def test_builtin_defaults_cover_all_write_ops(self) -> None:
        """_AT_FILE_BUILTIN_DEFAULTS must derive the same field specs as the hardcoded table."""
        expected = {
            "edit":          [("old", False, False), ("new", False, False), ("path", False, False)],
            "replace":       [("old", False, False), ("new", False, False), ("path", False, False)],
            "replace_dry":   [("old", False, False), ("new", False, False), ("path", False, False)],
            "replace_lines": [("path", False, False), ("start", False, False), ("end", False, False), ("content", False, False)],
            "paste":         [("path", False, False), ("content", False, False)],
            "append":        [("path", False, False), ("content", False, False)],
            "vim":           [("path", False, False), ("script", False, False)],
        }
        assert supertool._AT_FILE_BUILTIN_DEFAULTS == expected

    def test_at_file_fields_returns_names_only(self) -> None:
        """_at_file_fields stays name-only for the truthiness/sub-op callers."""
        assert supertool._at_file_fields("edit") == ["old", "new", "path"]

    def test_registry_populated_after_first_dispatch(self, tmp_path: Path) -> None:
        """After any dispatch call the registry must contain the builtin write ops."""
        # Force a dispatch so the registry is built
        supertool.dispatch(f"read:{tmp_path}/nope")
        for op in ("edit", "replace", "replace_dry", "replace_lines", "paste", "append", "vim"):
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
        # as_posix avoids Windows backslashes being interpreted as TOML escapes.
        spec.write_text(
            f'path = "{target.as_posix()}"\n'
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
            f'path = "{target.as_posix()}"\n'
        )
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" not in out
        assert target.read_text() == "DEBUG = True\n"

    def test_replace_lines_with_integers_and_triple_literal(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a\nb\nc\nd\n")
        spec = tmp_path / "rl.toml"
        spec.write_text(
            f'path = "{target.as_posix()}"\n'
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
            f'path = "{target.as_posix()}"\n'
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

# ---------------------------------------------------------------------------
# Batch snapshot mode — replace_lines line numbers refer to original file
# ---------------------------------------------------------------------------

class TestBatchSnapshot:
    def test_out_of_order_replace_lines_all_land(self, tmp_path: Path) -> None:
        """Three replace_lines on one file in out-of-order line numbers should all
        land correctly — line numbers refer to original file, not mutated state."""
        target = tmp_path / "x.txt"
        target.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\nL9\nL10\n")
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "ops": [
                {"op": "replace_lines", "path": str(target), "start": 8, "end": 8, "content": "EIGHT"},
                {"op": "replace_lines", "path": str(target), "start": 2, "end": 2, "content": "TWO"},
                {"op": "replace_lines", "path": str(target), "start": 5, "end": 5, "content": "FIVE"},
            ]
        }))
        out = supertool.dispatch(f"batch:@{ops_file}")
        assert "ERROR" not in out
        assert target.read_text() == "L1\nTWO\nL3\nL4\nFIVE\nL6\nL7\nEIGHT\nL9\nL10\n"

    def test_overlapping_ranges_error_no_writes(self, tmp_path: Path) -> None:
        """Two replace_lines with overlapping ranges should error before any write."""
        target = tmp_path / "x.txt"
        original = "L1\nL2\nL3\nL4\nL5\n"
        target.write_text(original)
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "ops": [
                {"op": "replace_lines", "path": str(target), "start": 2, "end": 3, "content": "A"},
                {"op": "replace_lines", "path": str(target), "start": 3, "end": 4, "content": "B"},
            ]
        }))
        out = supertool.dispatch(f"batch:@{ops_file}")
        assert "ERROR" in out
        assert "overlapping" in out
        assert target.read_text() == original  # no write happened

    def test_single_replace_lines_no_reorder_needed(self, tmp_path: Path) -> None:
        """A single replace_lines op should pass through unchanged."""
        target = tmp_path / "x.txt"
        target.write_text("a\nb\nc\n")
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps([
            {"op": "replace_lines", "path": str(target), "start": 2, "end": 2, "content": "B"}
        ]))
        out = supertool.dispatch(f"batch:@{ops_file}")
        assert "ERROR" not in out
        assert target.read_text() == "a\nB\nc\n"

    def test_replace_lines_on_different_files_independent(self, tmp_path: Path) -> None:
        """replace_lines on different files should each work without reorder."""
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("x\ny\nz\n")
        b.write_text("p\nq\nr\n")
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps([
            {"op": "replace_lines", "path": str(a), "start": 2, "end": 2, "content": "Y"},
            {"op": "replace_lines", "path": str(b), "start": 2, "end": 2, "content": "Q"},
        ]))
        out = supertool.dispatch(f"batch:@{ops_file}")
        assert "ERROR" not in out
        assert a.read_text() == "x\nY\nz\n"
        assert b.read_text() == "p\nQ\nr\n"


class TestReorderHelper:
    """Direct tests of the _reorder_batch_for_snapshot helper."""

    def test_empty_batch(self) -> None:
        new, err = supertool._reorder_batch_for_snapshot([])
        assert err == ""
        assert new == []

    def test_no_replace_lines(self) -> None:
        ops = [{"op": "edit", "old": "a", "new": "b", "path": "x"}]
        new, err = supertool._reorder_batch_for_snapshot(ops)
        assert err == ""
        assert new == ops

    def test_single_replace_lines_unchanged(self) -> None:
        ops = [{"op": "replace_lines", "path": "x", "start": 10, "end": 10, "content": "y"}]
        new, err = supertool._reorder_batch_for_snapshot(ops)
        assert err == ""
        assert new == ops

    def test_two_replace_lines_sorted_descending(self) -> None:
        ops = [
            {"op": "replace_lines", "path": "x", "start": 5, "end": 5, "content": "A"},
            {"op": "replace_lines", "path": "x", "start": 20, "end": 20, "content": "B"},
        ]
        new, err = supertool._reorder_batch_for_snapshot(ops)
        assert err == ""
        assert new[0]["start"] == 20
        assert new[1]["start"] == 5

    def test_overlap_detection(self) -> None:
        ops = [
            {"op": "replace_lines", "path": "x", "start": 5, "end": 10, "content": "A"},
            {"op": "replace_lines", "path": "x", "start": 8, "end": 12, "content": "B"},
        ]
        new, err = supertool._reorder_batch_for_snapshot(ops)
        assert "overlapping" in err
        assert new == ops  # unchanged on error

    def test_different_files_no_cross_overlap_check(self) -> None:
        ops = [
            {"op": "replace_lines", "path": "a", "start": 5, "end": 10, "content": "X"},
            {"op": "replace_lines", "path": "b", "start": 5, "end": 10, "content": "Y"},
        ]
        new, err = supertool._reorder_batch_for_snapshot(ops)
        assert err == ""


# ---------------------------------------------------------------------------
# Optional + variadic fields (#340 — git-commit:@- multi-line message route)
# ---------------------------------------------------------------------------

class TestAtFileOptionalVariadic:
    """_at_file_to_parts honours optional fields and expands variadic lists.

    Uses the same (name, optional, variadic) spec shape the registry builds
    for `git-commit:::MESSAGE[:::PATHS...]` — message required, paths optional
    and list-valued — without depending on a preset config being loaded.
    """

    _SPECS = [("message", False, False), ("paths", True, True)]

    def _patch(self, monkeypatch) -> None:
        monkeypatch.setattr(
            supertool, "_at_file_specs",
            lambda op: self._SPECS if op == "git-commit" else [],
        )

    def test_message_only_omits_optional_paths(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        parts, replace_all = supertool._at_file_to_parts(
            "git-commit", {"message": "subject\n\nbody"}
        )
        assert parts == ["git-commit", "subject\n\nbody"]
        assert replace_all is False

    def test_paths_list_expands_to_multiple_parts(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        parts, _ = supertool._at_file_to_parts(
            "git-commit", {"message": "m", "paths": ["a.txt", "b.txt"]}
        )
        assert parts == ["git-commit", "m", "a.txt", "b.txt"]

    def test_paths_scalar_becomes_single_part(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        parts, _ = supertool._at_file_to_parts(
            "git-commit", {"message": "m", "paths": "only.txt"}
        )
        assert parts == ["git-commit", "m", "only.txt"]

    def test_paths_empty_list_omits(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        parts, _ = supertool._at_file_to_parts(
            "git-commit", {"message": "m", "paths": []}
        )
        assert parts == ["git-commit", "m"]

    def test_paths_null_omits_no_literal_none(self, monkeypatch) -> None:
        """paths:null (JSON) must not emit a literal 'None' positional arg."""
        self._patch(monkeypatch)
        parts, _ = supertool._at_file_to_parts(
            "git-commit", {"message": "m", "paths": None}
        )
        assert parts == ["git-commit", "m"]

    def test_paths_list_drops_null_elements(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        parts, _ = supertool._at_file_to_parts(
            "git-commit", {"message": "m", "paths": ["a.txt", None, "b.txt"]}
        )
        assert parts == ["git-commit", "m", "a.txt", "b.txt"]

    def test_missing_required_message_raises(self, monkeypatch) -> None:
        self._patch(monkeypatch)
        with pytest.raises(ValueError, match="missing required field 'message'"):
            supertool._at_file_to_parts("git-commit", {"paths": ["a.txt"]})
