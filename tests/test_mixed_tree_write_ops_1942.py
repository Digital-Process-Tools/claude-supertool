"""#1942 -- the mixed-tree guard (#678) declined preset/custom ops outright but
let a built-in WRITE op run to completion under the same mismatch, disclosing
only a stderr warning nothing downstream reads. `paste` and `edit` resolve
their target through `os.getcwd()` regardless of which core answered, so the
write always landed in the right file -- the risk #678 exists for is not
"wrong directory", it is "wrong code answered": a different build's
validators, formatters and hooks ran, and the receipt read exactly like a
correct one.

`_OP_SAFETY_BUILTIN[op] == "writes"` already names every mutating builtin in
one place (paste, edit, append, replace, replace_lines, vim, format,
format_staged, gc, batch, rename). This file pins that the same chokepoint
`_resolve_custom_op` already uses for #678 now gates that whole class too, so
a write-class builtin declines exactly like a preset op instead of quietly
answering PASS-shaped output for a build the tool cannot name.

Mirrors the helper shapes in `test_mixed_tree_guard_678.py` rather than
importing them -- those are module-private (leading underscore) and specific
to `.supertool.json`-defined custom ops (which spawn a subprocess); this file
is about the built-in dispatch chokepoint in `_dispatch_impl`, a different
code path with a different fixture shape (no `ops` entry, no probe script).
"""

from __future__ import annotations

import json
from pathlib import Path

import supertool


def _project_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".supertool.json").write_text(json.dumps({}), encoding="utf-8")
    return root


def _stand_in(monkeypatch, root: Path) -> None:
    """Simulate `_load_config()` having resolved this root from the cwd."""
    monkeypatch.chdir(root)
    supertool._CONFIG = {}
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(root / ".supertool.json")


def _foreign_core(root: Path) -> None:
    """Make `root` a *different* supertool checkout than the one under test."""
    (root / "supertool.py").write_text("# a different build of supertool\n",
                                        encoding="utf-8")


def test_paste_declines_under_a_mixed_tree(tmp_path, monkeypatch):
    """The control pair, half one: must not silently write under a mix."""
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    before = supertool._SKIP_COUNT[0]
    out = supertool.dispatch("paste:::new_file.txt:::hello world")

    assert not (root / "new_file.txt").exists(), (
        "the write ran under a mixed core/tree pair -- this is the exact "
        f"silent-wrong-tree shape #1942 reports:\n{out}"
    )
    assert "SKIPPED" in out, f"a declined write must say so plainly:\n{out}"
    assert str(root) in out and supertool._INSTALL_DIR in out, (
        f"the decline never names both trees:\n{out}"
    )
    assert supertool._SKIP_COUNT[0] == before + 1, (
        "a decline must register as a skip so the call exits non-zero (#680)"
    )


def test_edit_declines_under_a_mixed_tree(tmp_path, monkeypatch):
    """Same guard, a second write-class builtin -- the class, not one op."""
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    (root / "existing.txt").write_text("old\n", encoding="utf-8")
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("edit:::old:::new:::existing.txt")

    assert (root / "existing.txt").read_text(encoding="utf-8") == "old\n", (
        f"edit mutated the file under a mixed tree:\n{out}"
    )
    assert "SKIPPED" in out


def test_paste_runs_normally_without_a_mix(tmp_path, monkeypatch):
    """The control pair, half two: the correctly-invoked call is untouched."""
    root = _project_root(tmp_path, "same_checkout")
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("paste:::new_file.txt:::hello world")

    assert (root / "new_file.txt").exists(), (
        f"an ordinary, unmixed paste stopped writing:\n{out}"
    )
    assert "SKIPPED" not in out


def test_read_only_builtins_are_unaffected_by_the_write_gate(tmp_path, monkeypatch):
    """The class distinction is real: a read-only builtin still answers under
    a mix, exactly as `test_builtin_ops_still_answer_under_a_mix` (#678) pins.
    """
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    (root / "hello.txt").write_text("line one\n", encoding="utf-8")
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("read:hello.txt")

    assert "line one" in out, f"the write-class gate took a read down with it:\n{out}"
    assert "SKIPPED" not in out


def test_override_still_lets_a_write_run_under_a_declared_mix(tmp_path, monkeypatch):
    """`SUPERTOOL_ALLOW_MIXED_TREE=1` is the documented opt-in (#678) -- it
    must still work for the class this issue adds gating to.
    """
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    _stand_in(monkeypatch, root)
    monkeypatch.setenv("SUPERTOOL_ALLOW_MIXED_TREE", "1")

    out = supertool.dispatch("paste:::new_file.txt:::hello world")

    assert (root / "new_file.txt").exists(), (
        f"the declared-mix override stopped a write-class builtin from running:\n{out}"
    )
