"""#1954 -- `map` used to drop a class's parent and interfaces, so a
subclass with two methods of its own and a parent supplying the rest read
as a small standalone class.

Covers both tiers that carry the gap: the regex tier (the default in this
suite -- tree-sitter is not force-enabled here) and the tree-sitter tier
directly, since `tree_sitter_language_pack` happens to be installed on this
machine and the fix lives in both places (`_header_hierarchy` for regex,
`_ts_class_hierarchy` for tree-sitter).

Every "must show a parent" case is paired with a "must NOT show one" case
in the same tier, so the assertion cannot pass by `map` growing a `<` on
every class regardless of content.
"""
from pathlib import Path

import pytest

import supertool
from conftest import _has_any_tree_sitter


# ---------------------------------------------------------------------------
# regex tier -- the default in this suite
# ---------------------------------------------------------------------------

def test_php_class_with_parent_and_interfaces_shows_hierarchy(tmp_path: Path) -> None:
    """The exact shape the issue was filed from: parent + interfaces wrapped
    across several lines."""
    f = tmp_path / "BriefHasUser.class.php"
    f.write_text(
        "<?php\n"
        "class BriefHasUser extends SiBriefHasUser implements\n"
        "    IACLOwner,\n"
        "    IRatingRatedEntity,\n"
        "    IRatingRelatedEntity,\n"
        "    IEntityMetadata\n"
        "{\n"
        "    public function mock() {}\n"
        "}\n"
    )
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "class BriefHasUser < SiBriefHasUser, IACLOwner, IRatingRatedEntity, IRatingRelatedEntity, +1 more" in out


def test_php_class_with_no_parent_shows_no_hierarchy(tmp_path: Path) -> None:
    """Must-not-fire pair: a genuinely standalone class must render exactly
    as it did before -- no `<` invented out of nothing."""
    f = tmp_path / "Standalone.php"
    f.write_text("<?php\nclass Standalone\n{\n    public function run() {}\n}\n")
    out = supertool.op_map(str(f))
    assert "class Standalone " in out or "class Standalone[" in out or "class Standalone  [" in out
    assert "<" not in out.split("class Standalone", 1)[1].split("\n", 1)[0]


def test_python_class_with_bases_shows_hierarchy_and_skips_metaclass(tmp_path: Path) -> None:
    f = tmp_path / "svc.py"
    f.write_text("class Service(Base, Mixin, metaclass=Meta):\n    def run(self):\n        pass\n")
    out = supertool.op_map(str(f))
    assert "class Service < Base, Mixin" in out
    assert "Meta" not in out


def test_typescript_class_extends_and_implements(tmp_path: Path) -> None:
    f = tmp_path / "handler.ts"
    f.write_text("export class Handler extends Base implements IA, IB {\n  run(): void {}\n}\n")
    out = supertool.op_map(str(f))
    assert "class Handler < Base, IA, IB" in out


def test_ruby_class_with_superclass(tmp_path: Path) -> None:
    f = tmp_path / "widget.rb"
    f.write_text("class Widget < Base\n  def run\n  end\nend\n")
    out = supertool.op_map(str(f))
    assert "class Widget < Base" in out


def test_java_class_extends_and_implements(tmp_path: Path) -> None:
    f = tmp_path / "Handler.java"
    f.write_text("public class Handler extends Base implements IA, IB {\n}\n")
    out = supertool.op_map(str(f))
    assert "class Handler < Base, IA, IB" in out


def test_wide_interface_list_is_capped_and_says_what_was_cut(tmp_path: Path) -> None:
    """The issue's own stated budget risk: cap the rendered names, say what
    was cut, never claim a shorter hierarchy than the file has."""
    ifaces = ", ".join(f"I{i}" for i in range(8))
    f = tmp_path / "Wide.java"
    f.write_text(f"public class Wide implements {ifaces} {{\n}}\n")
    out = supertool.op_map(str(f))
    assert "class Wide < I0, I1, I2, I3, +4 more" in out


def test_python_nested_paren_default_does_not_truncate_the_base_list(tmp_path: Path) -> None:
    """Regression: a `[^)]*`-style capture stops at the FIRST `)`, which for
    `class Foo(Base, x=(1, 2), Mixin):` is the one closing the nested tuple
    literal -- `Mixin` (a real base class) used to be silently dropped."""
    f = tmp_path / "svc.py"
    f.write_text("class Service(Base, x=(1, 2), Mixin):\n    pass\n")
    out = supertool.op_map(str(f))
    assert "class Service < Base, Mixin" in out


def test_java_generic_interface_comma_is_not_split_as_two_names(tmp_path: Path) -> None:
    """Regression: `implements Comparable<Foo, Bar>` has a comma INSIDE the
    generic argument list -- a naive `.split(",")` used to garble it into
    two half-names (`Comparable<Foo`, `Bar>`) instead of the one real one."""
    f = tmp_path / "Handler.java"
    f.write_text("public class Handler implements Comparable<Foo, Bar>, Serializable {\n}\n")
    out = supertool.op_map(str(f))
    assert "class Handler < Comparable<Foo, Bar>, Serializable" in out


# ---------------------------------------------------------------------------
# tree-sitter tier
# ---------------------------------------------------------------------------

def _ts_or_skip(monkeypatch):
    """Undo the suite-wide autouse fixture that forces every test onto the
    regex tier (`tests/conftest.py`, around #1801) -- this module wants the
    real tree-sitter tier, which is the tier the issue names."""
    if not _has_any_tree_sitter():
        pytest.skip("no tree-sitter package installed -- this tier did not run")
    monkeypatch.setattr(supertool, "_TS_CHECKED", True)
    monkeypatch.setattr(supertool, "_TS_AVAILABLE", True)
    monkeypatch.setattr(supertool, "_TS_PACKAGE", "pack")


def test_ts_tier_python_class_hierarchy(tmp_path: Path, monkeypatch) -> None:
    _ts_or_skip(monkeypatch)
    f = tmp_path / "svc.py"
    f.write_text("class Service(Base, metaclass=Meta):\n    def run(self):\n        pass\n")
    symbols = supertool._ts_extract(str(f), "python")
    names = [s[1] for s in symbols if s[0] == "class"]
    assert names == ["Service < Base"]


def test_ts_tier_php_class_hierarchy(tmp_path: Path, monkeypatch) -> None:
    _ts_or_skip(monkeypatch)
    f = tmp_path / "Foo.php"
    f.write_text("<?php\nclass Foo extends Base implements A, B {\n}\n")
    symbols = supertool._ts_extract(str(f), "php")
    names = [s[1] for s in symbols if s[0] == "class"]
    assert names == ["Foo < Base, A, B"]


def test_ts_tier_class_without_parent_shows_no_hierarchy(tmp_path: Path, monkeypatch) -> None:
    """Must-not-fire pair for the tree-sitter tier."""
    _ts_or_skip(monkeypatch)
    f = tmp_path / "Plain.php"
    f.write_text("<?php\nclass Plain {\n}\n")
    symbols = supertool._ts_extract(str(f), "php")
    names = [s[1] for s in symbols if s[0] == "class"]
    assert names == ["Plain"]


def test_ts_tier_java_interface_extending_interfaces(tmp_path: Path, monkeypatch) -> None:
    """Regression: an interface's OWN `extends A, B` sits under a different
    node type (`extends_interfaces`) than a class's (`superclass`/
    `super_interfaces`) -- omitting it left interface-to-interface
    inheritance silently unrendered, the exact symptom #1954 was filed
    against, just one level up."""
    _ts_or_skip(monkeypatch)
    f = tmp_path / "Sub.java"
    f.write_text("public interface Sub extends Base, Other {\n}\n")
    symbols = supertool._ts_extract(str(f), "java")
    names = [s[1] for s in symbols if s[0] == "interface"]
    assert names == ["Sub < Base, Other"]


def test_ts_tier_typescript_interface_extending_interfaces(tmp_path: Path, monkeypatch) -> None:
    """Same regression as the Java case above, TS/JS's own node type
    (`extends_type_clause`)."""
    _ts_or_skip(monkeypatch)
    f = tmp_path / "Foo.ts"
    f.write_text("interface Foo extends Bar, Baz {\n}\n")
    symbols = supertool._ts_extract(str(f), "typescript")
    names = [s[1] for s in symbols if s[0] == "interface"]
    assert names == ["Foo < Bar, Baz"]


# ---------------------------------------------------------------------------
# regex tier -- window truncation must decline rather than misreport
# ---------------------------------------------------------------------------

def test_regex_tier_declines_the_suffix_when_the_header_window_is_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    """A header longer than the scan window must not report a name count
    computed from a truncated read -- that is the same "absence read as
    a wrong answer" defect CLAUDE.md names, one level up from simply
    dropping the hierarchy. Shrinking the window in the test (rather than
    writing a multi-kilobyte fixture file) exercises the real decline
    path with the real production constant's own logic."""
    monkeypatch.setattr(supertool, "_CLASS_HEADER_WINDOW", 20)
    f = tmp_path / "Handler.java"
    f.write_text("public class Handler extends Base implements IA, IB {\n}\n")
    out = supertool.op_map(str(f))
    assert "class Handler " in out or "class Handler[" in out or "class Handler  [" in out
    assert "<" not in out.split("class Handler", 1)[1].split("\n", 1)[0]
