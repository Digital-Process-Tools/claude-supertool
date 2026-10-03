"""_supertool.py never reads os.environ with a non-literal key (#2734).

The Anthropic directory's MCP_FORWARDS_CREDENTIAL_ENV aggregate hold cited
`_supertool.py` for "an environment variable named at run time" -- an
`os.environ[x]` / `os.environ.get(x)` / `os.getenv(x)` where `x` is a
variable rather than a string literal, even when that variable is itself a
fixed module-level constant never influenced by attacker input. The first
round fixed the one instance found by hand (`main()`'s SUPERTOOL_REPO /
SUPERTOOL_REPO_FROM_OP restore loop); a second portal validation
(`c607ed1`) showed the hold still citing `_supertool.py`, naming a second
instance (`DETERMINISTIC_TIME_ENV`) a hand sweep had missed.

This is the guard so a third one cannot come back silently: an AST walk
over `_supertool.py`'s own source, asserting every `os.environ` subscript
and every `os.environ.get(...)` / `os.getenv(...)` call's key argument is
an `ast.Constant` (a literal string), not a `Name`, `Attribute` or any
other expression. `_supertool_mcp.py` and the other #2706 part files are
deliberately NOT covered here -- they are exec()'d into `_supertool.py`'s
own globals() rather than being this file's own source text, and the
portal's hold named `_supertool.py` specifically.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "_supertool.py"


def _non_literal_environ_reads(source: str) -> list[tuple[int, str]]:
    """(lineno, description) for every os.environ[...]/os.environ.get(...)/
    os.getenv(...) in SOURCE whose key/name argument is not a literal
    string constant."""
    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "environ":
                key = node.slice
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    hits.append((node.lineno, f"os.environ[{ast.dump(key)}]"))
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ" and node.args):
                arg = node.args[0]
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    hits.append((node.lineno, f"os.environ.get({ast.dump(arg)})"))
            elif (isinstance(func, ast.Attribute) and func.attr == "getenv"
                    and isinstance(func.value, ast.Name) and func.value.id == "os"
                    and node.args):
                arg = node.args[0]
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    hits.append((node.lineno, f"os.getenv({ast.dump(arg)})"))
    return hits


def test_no_dynamic_environ_read_in_core() -> None:
    hits = _non_literal_environ_reads(CORE.read_text(encoding="utf-8"))
    assert not hits, (
        f"_supertool.py reads os.environ with a non-literal key at: {hits} -- "
        'this is exactly the shape the Anthropic directory\'s '
        'MCP_FORWARDS_CREDENTIAL_ENV hold cites as "an environment '
        'variable named at run time"; use a literal string, even for a '
        "module-level constant.")


def test_the_scan_itself_catches_a_planted_instance() -> None:
    """Positive control: a negative assertion that only ever sees nothing
    passing proves nothing about whether the scan can see a real hit.
    Confirmed by #2734's own history -- DETERMINISTIC_TIME_ENV sat in
    _supertool.py, cited by a real portal scan, for an entire round."""
    planted = "import os\nNAME = 'X'\nos.environ.get(NAME)\n"
    hits = _non_literal_environ_reads(planted)
    assert hits, "the scan did not see a deliberately non-literal os.environ.get(NAME)"
    planted_subscript = "import os\nNAME = 'X'\nv = os.environ[NAME]\n"
    hits2 = _non_literal_environ_reads(planted_subscript)
    assert hits2, "the scan did not see a deliberately non-literal os.environ[NAME]"
    clean = "import os\nos.environ.get('X')\n"
    assert not _non_literal_environ_reads(clean), "a literal-keyed read was flagged"
