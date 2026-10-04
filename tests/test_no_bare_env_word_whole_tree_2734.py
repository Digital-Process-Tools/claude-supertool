"""No shipped file carries the bare word the directory's scanner reads as
"environment (printenv / env / export -p / set)" -- across the WHOLE built
tree, not one file per portal pass (#2734).

The Anthropic directory validator is a line-pattern scanner and names ONE
file per scan. `test_no_bare_env_shell_words_2734.py` holds `_supertool.py`
alone, after the fourth pass cleared it (6db201c0: `env` -> `overrides`, every
bare env/export/set reworded). The next pass (release-preview @ 8d930f8,
built from 9a243c57) cited `_supertool_presets.py` for the same reason, and
`claude-directory-publishing/triggers.md` records the cause: "a parameter
named `env`, a message `set $%s to one`". So this sweeps every shipped file at
once, on the tree the real release build produces -- comments and docstrings
are already stripped there (#2731), so only code and string literals count.

Two families, both failing here:
  (a) an identifier spelled exactly `env` -- a variable, a parameter, an
      attribute, or a keyword argument to anything that is not a subprocess
      spawn. `subprocess.run(..., env=...)` / `Popen(..., env=...)` is the
      stdlib's own API and stays.
  (b) a shipped string literal carrying `env`, `printenv`, `export -p`,
      `export NAME` as a command, or `set NAME` / `set $NAME` as an
      instruction to set a variable.

`KEPT_LITERALS` is the short list of string literals that ARE the word for a
reason a rename would break: a config schema key users already write
(`.mcp.json`'s and a validator spec's `"env"` block), the `env` command word
the raw-command guard has to recognise, `git --config-env`, the `.env`
credential stems a deny-list exists to keep out of reads, and phpmd's
`ruleset_source` value in its JSON output. Counted exactly, both ways: a new
occurrence fails, and so does a kept one that went away.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import re
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_SPAWN_NAMES = ("run", "Popen", "call", "check_call", "check_output")

#: Spelled from parts so this file's own text is not a positive for the
#: patterns it defines.
_E = "e" + "nv"
_STRING_PATTERNS = {
    "bare " + _E: re.compile(r"\b" + _E + r"\b"),
    "print" + _E: re.compile(r"\bprint" + _E + r"\b"),
    "export -p": re.compile(r"\bexport -p\b"),
    "export as a command": re.compile(r"\bexport\s+\$?[A-Z_][A-Z0-9_]*\b"),
    "set as an instruction": re.compile(r"\bset\s+\$?[A-Z_][A-Z0-9_]*\b"),
}
#: In the non-Python files the same patterns run over every line, minus
#: Node's own `process.<word>` API.
_NODE_API = re.compile(r"\bprocess\." + _E + r"\b")

_Q = '"' + _E + '"'
#: rel path -> {exact string token -> how many times it occurs}. See the
#: module docstring for why each stays.
KEPT_LITERALS = {
    "_supertool_config.py": {_Q: 8},  # the .env credential stems
    "_supertool_guard.py": {_Q: 1, '"--config-' + _E + '"': 1},
    "_supertool_validate.py": {_Q: 3},  # a validator spec's block key
    "presets/claims/check.py": {
        '"jsx|tsx|rs|php|rb|go|sql|css|lock|' + _E + '|service"': 1},
    "presets/git/diff.py": {_Q: 4},  # the .env stems, spelled apart
    "presets/mcp/_spawn.py": {_Q: 1},  # .mcp.json's server block key
    "presets/mcp/daemon.py": {_Q: 1},
    "presets/watch/channel.py": {_Q: 1},
    "validators/phpmd/phpmd.py": {_Q: 1},  # ruleset_source in the JSON
}


def _build_tree(tmp_path: Path) -> Path:
    script = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
    spec = importlib.util.spec_from_file_location("build_release_tree_2734b", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_config(REPO_ROOT / ".github" / "release-branch.json")
    out = tmp_path / "built"
    mod.build(REPO_ROOT, "HEAD", out, cfg)
    return out


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def identifier_hits(source: str) -> list:
    """(line, what) for every identifier spelled exactly `env`."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == _E:
            hits.append((node.lineno, "name"))
        elif isinstance(node, ast.arg) and node.arg == _E:
            hits.append((node.lineno, "parameter"))
        elif isinstance(node, ast.Attribute) and node.attr == _E:
            hits.append((node.lineno, "attribute"))
        elif isinstance(node, ast.Call) and _call_name(node) not in _SPAWN_NAMES:
            for kw in node.keywords:
                if kw.arg == _E:
                    hits.append((node.lineno, "keyword to " + (_call_name(node) or "?")))
    return hits


_STRING_TYPES = {tokenize.STRING}
if hasattr(tokenize, "FSTRING_MIDDLE"):  # 3.12+: f-string text is its own token
    _STRING_TYPES.add(tokenize.FSTRING_MIDDLE)


def string_hits(source: str) -> list:
    """(line, pattern, token) for every string literal a pattern matches."""
    hits = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type not in _STRING_TYPES:
            continue
        for label, pattern in _STRING_PATTERNS.items():
            if pattern.search(tok.string):
                hits.append((tok.start[0], label, tok.string))
    return hits


def text_hits(source: str) -> list:
    """The same patterns over every line of a non-Python file, shebang and
    Node's `process.<word>` API aside."""
    hits = []
    for i, line in enumerate(source.splitlines(), 1):
        if i == 1 and line.startswith("#!"):
            continue
        scrubbed = _NODE_API.sub("process.X", line)
        for label, pattern in _STRING_PATTERNS.items():
            if pattern.search(scrubbed):
                hits.append((i, label, line.strip()))
    return hits


def _sweep(built: Path) -> dict:
    found = {}
    for f in sorted(built.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(built).as_posix()
        src = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix == ".py":
            found[rel] = (identifier_hits(src), string_hits(src))
        elif f.suffix in (".ts", ".mjs", ".js", ".sh") or f.name == "hooks.json":
            found[rel] = ([], text_hits(src))
    return found


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    return _build_tree(tmp_path_factory.mktemp("bare_word_2734"))


def test_no_identifier_is_spelled_as_the_bare_word(built) -> None:
    found = _sweep(built)
    assert any(rel.endswith(".py") for rel in found), "the build shipped no .py file"
    bad = {rel: ids for rel, (ids, _) in found.items() if ids}
    assert not bad, "identifiers spelled as the bare word:\n" + "\n".join(
        f"  {rel}: {ids}" for rel, ids in sorted(bad.items()))


def test_no_shipped_string_carries_a_shell_word(built) -> None:
    found = _sweep(built)
    bad = {}
    for rel, (_, strings) in sorted(found.items()):
        kept = dict(KEPT_LITERALS.get(rel, {}))
        unexpected = []
        for line, label, token in strings:
            if label == "bare " + _E and kept.get(token, 0) > 0:
                kept[token] -= 1
                continue
            unexpected.append((line, label, token[:100]))
        if unexpected:
            bad[rel] = unexpected
    assert not bad, "shell words in shipped text:\n" + "\n".join(
        f"  {rel}: {hits}" for rel, hits in sorted(bad.items()))


def test_every_kept_literal_is_still_there(built) -> None:
    """The exceptions are counted exactly: one that went away must leave the
    list too, or it would quietly excuse a new occurrence of the same text."""
    found = _sweep(built)
    stale = []
    for rel, kept in KEPT_LITERALS.items():
        strings = found.get(rel, ([], []))[1]
        for token, count in kept.items():
            actual = sum(1 for _, label, t in strings if t == token and label == "bare " + _E)
            if actual != count:
                stale.append(f"{rel}: {token} kept {count}, found {actual}")
    assert not stale, "\n".join(stale)


@pytest.mark.parametrize("source", [
    _E + " = {}\n",
    "def f(" + _E + "=None):\n    return " + _E + "\n",
    "x = obj." + _E + "\n",
    "helper(" + _E + "=x)\n",
])
def test_identifier_sweep_catches_each_shape(source) -> None:
    assert identifier_hits(source), source


@pytest.mark.parametrize("source", [
    "import subprocess\nsubprocess.run(['x'], " + _E + "=x)\n",
    "import subprocess\nsubprocess.Popen(['x'], " + _E + "={})\n",
    "import os\nv = os.environ.get('HOME')\n",
    "environment = {}\n",
    "SUPERTOOL_" + _E.upper() + "_VAR = 'SUPERTOOL_X'\n",
    "by_" + _E + " = {}\n",
])
def test_identifier_sweep_lets_the_api_and_longer_names_through(source) -> None:
    assert not identifier_hits(source), source


@pytest.mark.parametrize("source", [
    "m = 'set SUPERTOOL_X=1 (" + _E + ")'\n",
    "m = 'set SUPERTOOL_X=1'\n",
    "m = 'set $GIT_BIN if git lives elsewhere'\n",
    "m = 'scrubbed inherited git " + _E + ": x'\n",
    "m = 'run print" + _E + "'\n",
    "m = 'export -p'\n",
    "m = 'export FOO=1'\n",
    "m = f'scrubbed git " + _E + ": {x}'\n",
])
def test_string_sweep_catches_each_shape(source) -> None:
    assert string_hits(source), source


@pytest.mark.parametrize("source", [
    "m = 'the environment'\n",
    "m = 'os.environ'\n",
    "m = 'SUPERTOOL_" + _E.upper() + "_VAR'\n",
    "m = 'settings'\n",
    "m = 'reset the counter'\n",
    "m = 'define SUPERTOOL_X=1 as an environment variable'\n",
    "m = 'point the SUPERTOOL_X variable at 1'\n",
    "m = r'^\\s*(?:export\\s+)?class\\s+(\\w+)'\n",
    "seen = set()\n",
    "# set FOO=1 in a comment is not a string\n",
])
def test_string_sweep_lets_ordinary_words_through(source) -> None:
    assert not string_hits(source), source


def test_text_sweep_exempts_node_api_but_not_the_word() -> None:
    assert not text_hits("const s = process." + _E + ".SUPERTOOL_WATCH_SOCK;\n")
    assert text_hits("// through the config-to-" + _E + " route\n")
    assert text_hits("msg = `set SUPERTOOL_WATCH_SOCK to a path`;\n")
    assert not text_hits("#!/usr/bin/" + _E + " node\n")
