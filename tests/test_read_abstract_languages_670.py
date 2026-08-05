"""Abstract read is a tree-sitter capability, not a PHP capability (#670).

`read` on a file over the threshold returns a symbol map instead of raw source,
at roughly a twentieth of the bytes. That was gated on `path.endswith(".php")`
while the language tables next to it already covered eighteen extensions.

These tests pin three things:
  - the gate is language-table membership, not a `.php` suffix
  - the legacy `read.php_abstract` config key keeps enabling it
  - when the map cannot beat the raw source, the read says so rather than
    returning a map that is empty or larger (docs/validators.md
    "Declining instead of guessing" — an absence has to be stated)
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _enable(monkeypatch, key: str = "abstract", threshold: int = 400) -> None:
    monkeypatch.setattr(
        supertool, "_CONFIG",
        {"builtin-ops": {"read": {key: 1, "abstract_threshold_bytes": threshold}}},
    )


def _big_ts(path: Path) -> Path:
    body = ["export interface Shape { kind: string; }"]
    for i in range(60):
        body.append(f"export class Widget{i} {{")
        body.append(f"  render{i}(): string {{")
        body.append(f"    // {'pad ' * 12}")
        body.append(f"    return 'widget-{i}';")
        body.append("  }")
        body.append("}")
    path.write_text("\n".join(body) + "\n")
    return path


def _big_py(path: Path) -> Path:
    body = []
    for i in range(60):
        body.append(f"class Handler{i}:")
        body.append(f"    def handle{i}(self):")
        body.append(f"        # {'pad ' * 12}")
        body.append(f"        return {i}")
        body.append("")
    path.write_text("\n".join(body) + "\n")
    return path


# ---------------------------------------------------------------------------
# The gate: any language in the table, not just PHP
# ---------------------------------------------------------------------------

def test_abstract_read_applies_to_typescript(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    f = _big_ts(tmp_path / "widgets.ts")
    out = supertool.op_read(str(f))
    assert "[abstract read" in out
    assert "Widget7" in out
    assert "return 'widget-7'" not in out, "raw source leaked — this is not a map"


def test_abstract_read_applies_to_python(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    f = _big_py(tmp_path / "handlers.py")
    out = supertool.op_read(str(f))
    assert "[abstract read" in out
    assert "Handler7" in out
    assert "return 7" not in out


def test_abstract_banner_names_the_language(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    f = _big_py(tmp_path / "handlers.py")
    out = supertool.op_read(str(f))
    assert "python" in out.split("[abstract read", 1)[1].split("]", 1)[0]


def test_abstract_read_still_applies_to_php(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    f = tmp_path / "Foo.php"
    f.write_text("<?php\nclass Foo { public function bar() {} }\n" + "// pad\n" * 200)
    out = supertool.op_read(str(f))
    assert "[abstract read" in out
    assert "Foo" in out


def test_abstract_read_off_by_default(tmp_path: Path) -> None:
    f = _big_ts(tmp_path / "widgets.ts")
    out = supertool.op_read(str(f))
    assert "[abstract read" not in out
    assert "return 'widget-0'" in out


def test_abstract_read_full_bypasses_for_typescript(
    tmp_path: Path, monkeypatch
) -> None:
    _enable(monkeypatch)
    f = _big_ts(tmp_path / "widgets.ts")
    out = supertool.op_read(str(f), force_full=True)
    assert "[abstract read" not in out
    assert "return 'widget-0'" in out


# ---------------------------------------------------------------------------
# The legacy key stays public API
# ---------------------------------------------------------------------------

def test_legacy_php_abstract_key_enables_every_language(
    tmp_path: Path, monkeypatch
) -> None:
    """`read.php_abstract` is documented in .supertool.example.json and listed
    by `ops`. A project that set it keeps the feature — now for all languages,
    which is what the key meant to ask for."""
    _enable(monkeypatch, key="php_abstract")
    f = _big_ts(tmp_path / "widgets.ts")
    out = supertool.op_read(str(f))
    assert "[abstract read" in out


# ---------------------------------------------------------------------------
# Silence has to be stated (docs/validators.md — declining instead of guessing)
# ---------------------------------------------------------------------------

def test_no_symbols_falls_back_to_raw_and_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """A big data-only module parses fine and yields no definitions. Returning
    that empty map would read as 'this file has no code'."""
    _enable(monkeypatch)
    f = tmp_path / "data.ts"
    rows = ",\n".join(f'  {{ id: {i}, name: "row-{i}" }}' for i in range(120))
    f.write_text("export const ROWS = [\n" + rows + "\n];\n")
    out = supertool.op_read(str(f))
    assert "[abstract read skipped" in out
    assert "no symbols" in out
    assert "data.ts" in out
    assert '"row-3"' in out, "must fall back to the actual source"


def test_map_no_smaller_than_source_falls_back_and_reports_sizes(
    tmp_path: Path, monkeypatch
) -> None:
    """A symbol map that is not smaller than what a raw read would emit is a
    worse answer than the source. Long names, no bodies — the map restates the
    file."""
    _enable(monkeypatch, threshold=200)
    monkeypatch.setattr(
        supertool, "_CONFIG",
        {"builtin-ops": {"read": {"abstract": 1, "abstract_threshold_bytes": 200,
                                  "max_bytes": 400}}},
    )
    f = tmp_path / "many.py"
    f.write_text("\n".join(
        f"def function_with_a_very_long_and_descriptive_name_number_{i}(): pass"
        for i in range(40)) + "\n")
    out = supertool.op_read(str(f))
    assert "[abstract read skipped" in out
    assert "not smaller" in out
    assert "def function_with_a_very_long_and_descriptive_name_number_0" in out


def test_skip_note_names_tree_sitter_when_it_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """No tree-sitter and no regex tier for this extension means no map. The
    reader has to be able to tell that from 'this file has no symbols'."""
    _enable(monkeypatch)
    monkeypatch.setattr(supertool, "_TS_AVAILABLE", False)
    monkeypatch.setattr(supertool, "_TS_CHECKED", True)
    monkeypatch.setattr(supertool, "_CTAGS_PATH", None)
    monkeypatch.setattr(supertool, "_CTAGS_CHECKED", True)
    f = tmp_path / "app.swift"
    f.write_text("\n".join(f"class Screen{i} {{ func draw{i}() {{}} }}"
                           for i in range(60)) + "\n")
    out = supertool.op_read(str(f))
    assert "[abstract read skipped" in out
    assert "tree-sitter" in out
    assert "class Screen0" in out


def test_unknown_extension_is_untouched_and_unannounced(
    tmp_path: Path, monkeypatch
) -> None:
    """A .txt file was never a candidate. It gets raw source and no note —
    the skip note means 'this could have been a map and was not', so it must
    not fire where the feature never applied."""
    _enable(monkeypatch)
    f = tmp_path / "notes.txt"
    f.write_text("word " * 4000)
    out = supertool.op_read(str(f))
    assert "[abstract read" not in out
    assert "word word" in out
