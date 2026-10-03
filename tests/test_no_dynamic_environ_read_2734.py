"""_supertool.py never reads os.environ with a non-literal key (#2734).

The Anthropic directory's MCP_FORWARDS_CREDENTIAL_ENV aggregate hold cited
`_supertool.py` for "an environment variable named at run time" -- an
`os.environ[x]` / `os.environ.get(x)` / `os.getenv(x)` where `x` is a
variable rather than a string literal, even when that variable is itself a
fixed module-level constant never influenced by attacker input. Two
rounds of hand sweeps each missed an instance a real portal scan then
cited: round one found `main()`'s SUPERTOOL_REPO restore loop; round two
(`c607ed1`) found `_deterministic_time()`'s `DETERMINISTIC_TIME_ENV`
lookup; round three (`03c7f00`) found a THIRD shape entirely -- a local
alias (`environ = os.environ if env is None else env`) read with a
non-literal key, in `_syntax_floor_interpreter`.

So this guard is now an AST walk with alias tracking, not a flat pattern
match over `os.environ` text, covering everything the maintainer's three
rounds of portal citations have named between them:

1. `os.environ[x]` / `os.environ.get(x)` / `os.getenv(x)` with a
   non-literal key -- the original shape.
2. The same three, but on a NAME bound (directly, or via one arm of a
   conditional expression) to `os.environ` rather than on the attribute
   access itself -- `environ = os.environ if cond else other` is the
   confirmed real instance; `environ = os.environ` alone is the simpler
   case the same tracking catches for free.
3. Any call that passes `os.environ` itself, or a tracked alias of it,
   as a plain argument -- "receives the environment wholesale" rather
   than naming one variable, which a callee the scanner cannot see into
   (a sibling #2706 part file, in this plugin's case) makes opaque.
4. Iterating `os.environ` (or a tracked alias) directly, or via
   `.keys()`/`.values()`/`.items()`, and the whole-mapping-copy shapes
   `os.environ.copy()`, `dict(os.environ)`, `{**os.environ}` -- also
   "wholesale", a different shape from #3's function-call form.

`_supertool_mcp.py` and the other #2706 part files are deliberately NOT
covered here -- they are exec()'d into `_supertool.py`'s own globals()
rather than being this file's own source text, and every portal citation
to date has named `_supertool.py` specifically.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "_supertool.py"


def _is_os_environ(node: ast.AST) -> bool:
    """True for the literal expression `os.environ`."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _is_literal_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _environ_aliases(tree: ast.AST) -> set[str]:
    """Names assigned `os.environ`, directly or via one arm of an `if`/`else`
    conditional expression -- `environ = os.environ if env is None else env`
    is the confirmed real shape (#2734, round three); `environ = os.environ`
    alone is the simpler case this also covers.

    Deliberately NOT a full dataflow analysis: a name reassigned later to
    something unrelated still counts as an alias everywhere in the file,
    which only ever widens what gets flagged -- the safe direction for a
    guard whose false positive costs a read, and whose false negative
    costs a hold.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if _is_os_environ(value):
            aliases.add(target.id)
        elif isinstance(value, ast.IfExp) and (
                _is_os_environ(value.body) or _is_os_environ(value.orelse)):
            aliases.add(target.id)
    return aliases


def _is_environ_like(node: ast.AST, aliases: set[str]) -> bool:
    return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in aliases)


def _non_literal_environ_reads(source: str) -> list[tuple[int, str]]:
    """(lineno, description) for every environ-reading shape in SOURCE that
    is not clearly literal-keyed, tracking simple aliases of `os.environ`."""
    tree = ast.parse(source)
    aliases = _environ_aliases(tree)
    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ_like(node.value, aliases):
            key = node.slice
            if not _is_literal_str(key):
                hits.append((node.lineno, f"environ[{ast.dump(key)}]"))

        elif isinstance(node, ast.Call):
            func = node.func
            # .get(x) / .pop(x) / .keys() / .values() / .items() on an
            # environ-like receiver.
            if isinstance(func, ast.Attribute) and _is_environ_like(func.value, aliases):
                if func.attr in ("get", "pop") and node.args:
                    arg = node.args[0]
                    if not _is_literal_str(arg):
                        hits.append((node.lineno, f"environ.{func.attr}({ast.dump(arg)})"))
                elif func.attr in ("keys", "values", "items", "copy"):
                    hits.append((node.lineno, f"environ.{func.attr}() -- whole mapping"))
            # os.getenv(x) specifically (getenv has no alias form worth
            # tracking -- it is always called on the `os` module itself).
            elif (isinstance(func, ast.Attribute) and func.attr == "getenv"
                    and isinstance(func.value, ast.Name) and func.value.id == "os"
                    and node.args):
                arg = node.args[0]
                if not _is_literal_str(arg):
                    hits.append((node.lineno, f"os.getenv({ast.dump(arg)})"))
            # dict(os.environ) / dict(alias) -- whole-mapping copy via the
            # dict() constructor.
            elif (isinstance(func, ast.Name) and func.id == "dict" and node.args
                    and _is_environ_like(node.args[0], aliases)):
                hits.append((node.lineno, "dict(environ) -- whole mapping"))
            # Any OTHER call passing os.environ/an alias as a plain
            # argument: "receives the environment wholesale" to a callee
            # this scan cannot see into.
            else:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if _is_environ_like(arg, aliases):
                        callee = ast.dump(func)[:60]
                        hits.append((node.lineno,
                                      f"call passes environ wholesale to {callee}"))

        elif isinstance(node, ast.For) and _is_environ_like(node.iter, aliases):
            hits.append((node.lineno, "for ... in environ -- whole mapping"))

        elif isinstance(node, ast.DictComp) and node.generators:
            for gen in node.generators:
                if _is_environ_like(gen.iter, aliases):
                    hits.append((node.lineno, "dict comprehension over environ"))

        elif isinstance(node, ast.Dict):
            # {**os.environ, ...} -- a `**unpack` entry has key=None.
            for i, key in enumerate(node.keys):
                if key is None and _is_environ_like(node.values[i], aliases):
                    hits.append((node.lineno, "{**environ, ...} -- whole mapping"))

    return hits


def test_no_dynamic_environ_read_in_core() -> None:
    hits = _non_literal_environ_reads(CORE.read_text(encoding="utf-8"))
    assert not hits, (
        f"_supertool.py reads/receives os.environ in a shape the directory's "
        f'MCP_FORWARDS_CREDENTIAL_ENV hold cites at: {hits} -- use a literal '
        "string key, or avoid passing/iterating the whole mapping.")


def test_the_scan_itself_catches_a_planted_instance() -> None:
    """Positive control, one per shape this scan claims to catch -- a
    negative assertion that only ever sees nothing passing proves nothing
    about whether the scan can see a real hit. Confirmed by #2734's own
    history: three real portal citations, three shapes a narrower version
    of this scan missed in turn."""
    cases_with_hits = {
        "direct non-literal get": "import os\nNAME='X'\nos.environ.get(NAME)\n",
        "direct non-literal subscript": "import os\nNAME='X'\nv=os.environ[NAME]\n",
        "os.getenv non-literal": "import os\nNAME='X'\nos.getenv(NAME)\n",
        "simple alias, non-literal get": (
            "import os\nNAME='X'\nenviron=os.environ\nenviron.get(NAME)\n"),
        "conditional alias, non-literal get": (
            "import os\nNAME='X'\n"
            "def f(env=None):\n"
            "    environ = os.environ if env is None else env\n"
            "    return environ.get(NAME)\n"),
        "whole mapping passed to a call": (
            "import os\ndef scrub(e): pass\nscrub(os.environ)\n"),
        "whole mapping passed via alias": (
            "import os\nenviron=os.environ\ndef scrub(e): pass\nscrub(environ)\n"),
        "iterate environ directly": "import os\nfor k in os.environ:\n    pass\n",
        "environ.copy()": "import os\nos.environ.copy()\n",
        "dict(os.environ)": "import os\ndict(os.environ)\n",
        "dict-unpack os.environ": "import os\nd = {**os.environ}\n",
    }
    for label, planted in cases_with_hits.items():
        hits = _non_literal_environ_reads(planted)
        assert hits, f"the scan did not see: {label}"

    clean_cases = {
        "literal get": "import os\nos.environ.get('X')\n",
        "literal getenv": "import os\nos.getenv('X')\n",
        "literal pop": "import os\nos.environ.pop('X', None)\n",
        "alias with literal get": (
            "import os\nenviron=os.environ\nenviron.get('X')\n"),
        "call passing an unrelated dict": (
            "import os\ndef f(e): pass\nf({'a': 1})\n"),
    }
    for label, clean in clean_cases.items():
        hits = _non_literal_environ_reads(clean)
        assert not hits, f"a clean case was flagged: {label} -- {hits}"
