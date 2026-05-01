from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool
from conftest import _has_any_tree_sitter


# ---------------------------------------------------------------------------
# op_map — regex fallback (default in tests, TS and ctags disabled)
# ---------------------------------------------------------------------------

def test_map_empty_path() -> None:
    out = supertool.op_map("")
    assert "ERROR" in out


def test_map_nonexistent_path() -> None:
    out = supertool.op_map("/nonexistent/path")
    assert "ERROR" in out
    assert "not found" in out


def test_map_php_file(tmp_path: Path) -> None:
    f = tmp_path / "Module.php"
    f.write_text("""<?php
final class MyModule extends SiModule
{
    const VERSION = '1.0';
    const NAME = 'test';

    public function init(): void {}
    protected function helper(string $x): bool {}
}

interface IMyEntity
{
    public function getId(): int;
}

trait MyTrait
{
    public function traitMethod(): void {}
}
""")
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "class MyModule" in out
    assert "const VERSION" in out
    assert "const NAME" in out
    assert "function init" in out
    assert "function helper" in out
    assert "interface IMyEntity" in out
    assert "trait MyTrait" in out


def test_map_python_file(tmp_path: Path) -> None:
    f = tmp_path / "service.py"
    f.write_text("""class Service:
    def run(self, ctx):
        pass

    def stop(self):
        pass

def standalone():
    pass
""")
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "class Service" in out
    assert "def run" in out
    assert "def stop" in out
    assert "def standalone" in out


def test_map_python_indented_def_gets_depth(tmp_path: Path) -> None:
    f = tmp_path / "cls.py"
    f.write_text("""class Foo:
    def bar(self):
        pass
""")
    out = supertool.op_map(str(f))
    # class at depth 0 → 2 spaces indent, method at depth 1 → 4 spaces indent
    lines = out.splitlines()
    class_line = [l for l in lines if "class Foo" in l]
    def_line = [l for l in lines if "def bar" in l]
    assert class_line
    assert def_line
    # method should have more indentation than class
    assert len(def_line[0]) - len(def_line[0].lstrip()) > \
           len(class_line[0]) - len(class_line[0].lstrip())


def test_map_typescript_file(tmp_path: Path) -> None:
    f = tmp_path / "types.ts"
    f.write_text("""export interface Config {
    name: string;
}

export type UserId = string;

export enum Status {
    Active,
    Inactive,
}

export class Handler {
    handle(): void {}
}

export async function process(): Promise<void> {}
""")
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "interface Config" in out
    assert "type UserId" in out
    assert "enum Status" in out
    assert "class Handler" in out
    assert "function process" in out


def test_map_go_file(tmp_path: Path) -> None:
    f = tmp_path / "main.go"
    f.write_text("""package main

type Server struct {
    port int
}

func (s *Server) Start() error {
    return nil
}

func main() {
}
""")
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "type Server" in out
    assert "func Start" in out or "func main" in out


def test_map_rust_file(tmp_path: Path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text("""pub struct Config {
    name: String,
}

pub enum State {
    Active,
    Done,
}

pub trait Handler {
    fn handle(&self);
}

impl Config {
    pub fn new() -> Self { Config { name: String::new() } }
}

pub async fn run() {}
""")
    out = supertool.op_map(str(f))
    assert "tier: regex" in out
    assert "struct Config" in out
    assert "enum State" in out
    assert "trait Handler" in out
    assert "impl Config" in out
    assert "fn run" in out


def test_map_java_file(tmp_path: Path) -> None:
    f = tmp_path / "App.java"
    f.write_text("""public class Application {
    public void start() {}
}

public interface Service {
    void run();
}

public enum Color {
    RED, GREEN, BLUE
}
""")
    out = supertool.op_map(str(f))
    assert "class Application" in out
    assert "interface Service" in out
    assert "enum Color" in out


def test_map_ruby_file(tmp_path: Path) -> None:
    f = tmp_path / "app.rb"
    f.write_text("""module MyApp
  class Server
    def start
    end
  end
end
""")
    out = supertool.op_map(str(f))
    assert "module MyApp" in out
    assert "class Server" in out
    assert "def start" in out


def test_map_unsupported_extension(tmp_path: Path) -> None:
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n1,2,3\n")
    out = supertool.op_map(str(f))
    assert "no supported files" in out or "no symbols" in out


def test_map_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n")
    (tmp_path / "b.py").write_text("def beta():\n    pass\n")
    out = supertool.op_map(str(tmp_path))
    assert "2 files" in out
    assert "class Alpha" in out
    assert "def beta" in out


def test_map_skips_hidden_and_vendor_dirs(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "lib.py").write_text("class Vendor:\n    pass\n")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "hook.py").write_text("class Hook:\n    pass\n")
    (tmp_path / "main.py").write_text("class Main:\n    pass\n")
    out = supertool.op_map(str(tmp_path))
    assert "class Main" in out
    assert "class Vendor" not in out
    assert "class Hook" not in out


def test_map_line_numbers_in_output(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("# comment\n\nclass Foo:\n    pass\n")
    out = supertool.op_map(str(f))
    assert "[3]" in out  # class Foo is on line 3


def test_map_dispatch_integration(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("class Mod:\n    pass\n")
    out = supertool.dispatch(f"map:{f}")
    assert f"--- map:{f} ---" in out
    assert "class Mod" in out


def test_map_dispatch_default_cwd() -> None:
    out = supertool.dispatch("map:.")
    assert "tier:" in out


def test_map_max_files_truncation(tmp_path: Path) -> None:
    # Create more files than MAX_MAP_FILES
    old_max = supertool.MAX_MAP_FILES
    supertool.MAX_MAP_FILES = 3
    try:
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text(f"class C{i}:\n    pass\n")
        out = supertool.op_map(str(tmp_path))
        assert "3 files" in out
        assert "truncated" in out
    finally:
        supertool.MAX_MAP_FILES = old_max


def test_map_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("")
    out = supertool.op_map(str(f))
    assert "no symbols" in out


def test_map_file_with_no_symbols(tmp_path: Path) -> None:
    f = tmp_path / "script.py"
    f.write_text("x = 1\ny = 2\nprint(x + y)\n")
    out = supertool.op_map(str(f))
    assert "no symbols" in out


# ---------------------------------------------------------------------------
# map helpers — _collect_files, _count_lines, _regex_extract
# ---------------------------------------------------------------------------

def test_collect_files_single_file(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    files = supertool._collect_files(str(f), ())
    assert files == [str(f)]


def test_collect_files_nonexistent() -> None:
    files = supertool._collect_files("/does/not/exist", ())
    assert files == []


def test_count_lines(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("a\nb\nc\n")
    assert supertool._count_lines(str(f)) == 3


def test_count_lines_nonexistent() -> None:
    assert supertool._count_lines("/nope") == 0


def test_regex_extract_unsupported() -> None:
    assert supertool._regex_extract("file.unknown") == []


def test_format_map_symbols() -> None:
    symbols = [
        ("class", "Foo", 1, 0),
        ("method", "bar", 5, 1),
    ]
    out = supertool._format_map_symbols(symbols, "test.py", 10)
    assert "test.py (10 lines)" in out
    assert "class Foo  [1]" in out
    assert "method bar  [5]" in out


def test_format_ctags_symbols() -> None:
    symbols = [
        ("class", "Foo", 1, ""),
        ("method", "bar", 5, "Foo"),
    ]
    out = supertool._format_ctags_symbols(symbols, "test.py", 10)
    assert "test.py (10 lines)" in out
    assert "  class Foo  [1]" in out
    assert "    method bar  [5]" in out  # indented because scope is set


# ---------------------------------------------------------------------------
# map — ctags tier (integration, only if ctags is installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("shutil").which("ctags"),
    reason="ctags not installed"
)
def test_map_ctags_tier(tmp_path: Path, enable_ctags) -> None:
    f = tmp_path / "mod.py"
    f.write_text("class MyClass:\n    def my_method(self):\n        pass\n")
    out = supertool.op_map(str(f))
    assert "tier: ctags" in out
    assert "MyClass" in out


# ---------------------------------------------------------------------------
# map — tree-sitter tier (integration, only if tree-sitter-languages installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_any_tree_sitter(),
    reason="no tree-sitter package installed"
)
def test_map_tree_sitter_tier(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "mod.py"
    f.write_text("class MyClass:\n    def my_method(self):\n        pass\n")
    out = supertool.op_map(str(f))
    assert "tier: tree-sitter" in out
    assert "MyClass" in out
    assert "my_method" in out


@pytest.mark.skipif(
    not _has_any_tree_sitter(),
    reason="no tree-sitter package installed"
)
def test_map_php_trait_usage(tmp_path: Path, enable_tree_sitter) -> None:
    """PHP use TraitName; inside a class should appear in map output."""
    f = tmp_path / "MyClass.php"
    f.write_text("""<?php
class MyClass {
    use FirstTrait;
    use SecondTrait;

    public function doSomething(): void {}
}
""")
    out = supertool.op_map(str(f))
    assert "tier: tree-sitter" in out
    assert "class MyClass" in out
    assert "use FirstTrait" in out
    assert "use SecondTrait" in out
    assert "method doSomething" in out


# ---------------------------------------------------------------------------
# map — additional coverage for branches and error paths
# ---------------------------------------------------------------------------

def test_map_directory_no_supported_files(tmp_path: Path) -> None:
    """Directory with only unsupported file types."""
    (tmp_path / "data.csv").write_text("a,b\n")
    (tmp_path / "readme.txt").write_text("hello\n")
    out = supertool.op_map(str(tmp_path))
    assert "no supported files" in out


def test_regex_extract_oserror(tmp_path: Path) -> None:
    """_regex_extract returns [] when file can't be read."""
    f = tmp_path / "broken.py"
    f.write_text("class X:\n    pass\n")
    # Make unreadable
    f.chmod(0o000)
    try:
        result = supertool._regex_extract(str(f))
        assert result == []
    finally:
        f.chmod(0o644)


def test_ctags_extract_returns_empty_when_disabled() -> None:
    """_ctags_extract returns [] when ctags is not available (default in tests)."""
    result = supertool._ctags_extract("some_file.py")
    assert result == []


def test_ctags_extract_parses_json(tmp_path: Path, monkeypatch) -> None:
    """_ctags_extract parses ctags JSON output correctly."""
    import subprocess as sp

    ctags_output = (
        '{"_type": "tag", "name": "MyClass", "kind": "class", "line": 3, "scope": ""}\n'
        '{"_type": "tag", "name": "my_method", "kind": "member", "line": 5, "scope": "MyClass"}\n'
        '{"_type": "ptag", "name": "!_TAG_FILE_FORMAT"}\n'  # non-tag line
        '\n'  # blank line
        'not json\n'  # malformed line
    )

    class FakeResult:
        stdout = ctags_output
        returncode = 0

    # Enable ctags with a fake path
    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = "/usr/local/bin/ctags"

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeResult())

    f = tmp_path / "mod.py"
    f.write_text("class MyClass:\n    def my_method(self):\n        pass\n")

    result = supertool._ctags_extract(str(f))
    assert len(result) == 2
    assert result[0] == ("class", "MyClass", 3, "")
    assert result[1] == ("member", "my_method", 5, "MyClass")


def test_ctags_extract_handles_timeout(tmp_path: Path, monkeypatch) -> None:
    """_ctags_extract returns [] on subprocess timeout."""
    import subprocess as sp

    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = "/usr/local/bin/ctags"

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired(cmd="ctags", timeout=15)

    monkeypatch.setattr(sp, "run", fake_run)

    f = tmp_path / "mod.py"
    f.write_text("class X:\n    pass\n")
    result = supertool._ctags_extract(str(f))
    assert result == []


def test_ctags_extract_handles_oserror(tmp_path: Path, monkeypatch) -> None:
    """_ctags_extract returns [] on OSError."""
    import subprocess as sp

    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = "/usr/local/bin/ctags"

    def fake_run(*a, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(sp, "run", fake_run)

    f = tmp_path / "mod.py"
    f.write_text("class X:\n    pass\n")
    result = supertool._ctags_extract(str(f))
    assert result == []


def test_map_with_ctags_tier(tmp_path: Path, monkeypatch) -> None:
    """op_map uses ctags tier when ctags is available."""
    import subprocess as sp

    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = "/usr/local/bin/ctags"

    ctags_output = '{"_type": "tag", "name": "Greeter", "kind": "class", "line": 1, "scope": ""}\n'

    class FakeResult:
        stdout = ctags_output
        returncode = 0

    monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeResult())

    f = tmp_path / "mod.py"
    f.write_text("class Greeter:\n    pass\n")
    out = supertool.op_map(str(f))
    assert "tier: ctags" in out
    assert "Greeter" in out


def test_has_tree_sitter_caching() -> None:
    """_has_tree_sitter caches its result."""
    # Reset cache
    supertool._TS_CHECKED = False
    supertool._TS_AVAILABLE = False
    # First call detects (will be False since tree-sitter-languages likely not installed)
    result = supertool._has_tree_sitter()
    assert supertool._TS_CHECKED is True
    # Second call uses cache
    result2 = supertool._has_tree_sitter()
    assert result == result2


def test_has_ctags_caching() -> None:
    """_has_ctags caches its result."""
    supertool._CTAGS_CHECKED = False
    supertool._CTAGS_PATH = None
    result = supertool._has_ctags()
    assert supertool._CTAGS_CHECKED is True
    result2 = supertool._has_ctags()
    assert result == result2


def test_map_js_file(tmp_path: Path) -> None:
    """JavaScript regex extraction."""
    f = tmp_path / "app.js"
    f.write_text("""class App {
    constructor() {}
}

export async function fetchData() {}

function helper() {}
""")
    out = supertool.op_map(str(f))
    assert "class App" in out
    assert "function fetchData" in out
    assert "function helper" in out


def test_map_jsx_file(tmp_path: Path) -> None:
    """JSX shares JS patterns."""
    f = tmp_path / "component.jsx"
    f.write_text("export class Component {}\nfunction render() {}\n")
    out = supertool.op_map(str(f))
    assert "class Component" in out
    assert "function render" in out


def test_map_tsx_file(tmp_path: Path) -> None:
    """TSX shares TS patterns."""
    f = tmp_path / "app.tsx"
    f.write_text("export interface Props {}\nexport class App {}\n")
    out = supertool.op_map(str(f))
    assert "interface Props" in out
    assert "class App" in out


def test_map_php_enum(tmp_path: Path) -> None:
    """PHP enum extraction."""
    f = tmp_path / "status.php"
    f.write_text("<?php\nenum Status {\n    case Active;\n    case Closed;\n}\n")
    out = supertool.op_map(str(f))
    assert "enum Status" in out


def test_map_php_abstract_class(tmp_path: Path) -> None:
    """PHP abstract class extraction."""
    f = tmp_path / "base.php"
    f.write_text("<?php\nabstract class Base {\n    abstract public function run(): void;\n}\n")
    out = supertool.op_map(str(f))
    assert "class Base" in out
    assert "function run" in out


def test_collect_files_skips_generated(tmp_path: Path) -> None:
    """Generated/ directories are skipped."""
    gen = tmp_path / "Generated"
    gen.mkdir()
    (gen / "auto.php").write_text("<?php\nclass Auto {}\n")
    (tmp_path / "real.php").write_text("<?php\nclass Real {}\n")
    files = supertool._collect_files(str(tmp_path), ())
    assert any("real.php" in f for f in files)
    assert not any("auto.php" in f for f in files)


def test_collect_files_sorted(tmp_path: Path) -> None:
    """Files within a directory are sorted alphabetically."""
    (tmp_path / "z.py").write_text("x = 1\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "m.py").write_text("x = 1\n")
    files = supertool._collect_files(str(tmp_path), ())
    basenames = [os.path.basename(f) for f in files]
    assert basenames == sorted(basenames)


# ---------------------------------------------------------------------------
# tree-sitter tier — direct unit tests (only when TS available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_extract_python(tmp_path: Path) -> None:
    """_ts_extract returns correct symbols for Python."""
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    # Detect which package is actually installed
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    f = tmp_path / "mod.py"
    f.write_text("class Foo:\n    def bar(self):\n        pass\n\ndef top():\n    pass\n")
    symbols = supertool._ts_extract(str(f), "python")
    kinds = [s[0] for s in symbols]
    names = [s[1] for s in symbols]
    assert "class" in kinds
    assert "Foo" in names
    assert "bar" in names
    assert "top" in names
    # bar should have depth > Foo
    foo_sym = [s for s in symbols if s[1] == "Foo"][0]
    bar_sym = [s for s in symbols if s[1] == "bar"][0]
    assert bar_sym[3] > foo_sym[3]


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_extract_php_with_consts(tmp_path: Path) -> None:
    """_ts_extract handles PHP classes with consts and methods."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    f = tmp_path / "module.php"
    f.write_text(
        "<?php\nclass MyMod {\n    const VER = '1';\n"
        "    public function init(): void {}\n}\n"
    )
    symbols = supertool._ts_extract(str(f), "php")
    names = [s[1] for s in symbols]
    assert "MyMod" in names
    assert "init" in names


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_extract_invalid_language() -> None:
    """_ts_extract returns [] for an unsupported language."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    result = supertool._ts_extract("/dev/null", "nonexistent_language_xyz")
    assert result == []


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_extract_nonexistent_file() -> None:
    """_ts_extract returns [] when file doesn't exist."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    result = supertool._ts_extract("/does/not/exist.py", "python")
    assert result == []


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_extract_typescript(tmp_path: Path) -> None:
    """_ts_extract handles TypeScript interfaces and enums."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    f = tmp_path / "types.ts"
    f.write_text(
        "interface Config { name: string; }\n"
        "enum Status { Active, Done }\n"
        "class Handler { handle(): void {} }\n"
    )
    symbols = supertool._ts_extract(str(f), "typescript")
    names = [s[1] for s in symbols]
    assert "Config" in names
    assert "Status" in names
    assert "Handler" in names


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_ts_node_name_fallback() -> None:
    """_ts_node_name returns <anonymous> for nodes without identifiers."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()

    # Create a minimal mock node with no name field and no identifier children
    class MockNode:
        type = "unknown_node"
        children = []
        def child_by_field_name(self, name):
            return None

    result = supertool._ts_node_name(MockNode(), "python")
    assert result == "<anonymous>"


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="no tree-sitter")
def test_map_tree_sitter_php_full(tmp_path: Path) -> None:
    """Full map with tree-sitter on a PHP file with class, method, interface."""
    supertool._TS_CHECKED = False
    supertool._has_tree_sitter()
    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = None
    f = tmp_path / "full.php"
    f.write_text(
        "<?php\ninterface IEntity {\n    public function getId(): int;\n}\n"
        "trait MyTrait {\n    public function traitMethod(): void {}\n}\n"
        "final class Module {\n    const NAME = 'x';\n"
        "    public function run(): void {}\n}\n"
    )
    out = supertool.op_map(str(f))
    assert "tier: tree-sitter" in out
    assert "Module" in out
    assert "run" in out


# ---------------------------------------------------------------------------
# _has_tree_sitter detection branches
# ---------------------------------------------------------------------------

def test_has_tree_sitter_detection_uncached() -> None:
    """_has_tree_sitter detects installed package when cache is cleared."""
    supertool._TS_CHECKED = False
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""
    result = supertool._has_tree_sitter()
    assert supertool._TS_CHECKED is True
    # On this machine tree-sitter-language-pack is installed
    if result:
        assert supertool._TS_PACKAGE in ("pack", "languages")


def test_has_tree_sitter_fallback_branch(monkeypatch) -> None:
    """When tree_sitter_language_pack import fails, falls back to tree_sitter_languages."""
    import builtins
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "tree_sitter_language_pack":
            raise ImportError("mocked")
        return original_import(name, *args, **kwargs)

    supertool._TS_CHECKED = False
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = supertool._has_tree_sitter()
    # Result depends on whether tree_sitter_languages is installed
    assert supertool._TS_CHECKED is True
    if result:
        assert supertool._TS_PACKAGE == "languages"


def test_has_tree_sitter_both_fail(monkeypatch) -> None:
    """When both packages fail to import, _TS_AVAILABLE is False."""
    import builtins
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name in ("tree_sitter_language_pack", "tree_sitter_languages"):
            raise ImportError("mocked")
        return original_import(name, *args, **kwargs)

    supertool._TS_CHECKED = False
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = supertool._has_tree_sitter()
    assert result is False
    assert supertool._TS_AVAILABLE is False
    assert supertool._TS_PACKAGE == ""
