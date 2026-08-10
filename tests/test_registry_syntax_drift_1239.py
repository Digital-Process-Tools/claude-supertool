"""Every filter key and flag an op accepts must appear in its declared syntax (#1239).

`presets/*.json`'s `syntax` string is the registry's statement about what an op
takes, and nothing compared it to the op's own `_FILTER_KEYS` / `_FLAGS`. So
`gh-issues` accepted `per=` and `assignee=`, printed `raise with per=N` in its
own output, and declared neither — which made the registry an oracle that
reports the *correct* documents as wrong (four of them, found by `claims`).

The string is parsed, not merely displayed: for a `:::`-bearing syntax it derives
the `@file` payload field names (`tests/test_at_file_route.py::TestPayloadRoutePin`,
#770). Drift there is a contract breaking, not a typo.

Discovery is by import rather than by a hand-written list, because a hand-written
list of ops-with-filters is the second copy of the same contract this test exists
to abolish.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESETS = ROOT / "presets"

#: `cmd` looks like `{python} {path}github/issues.py {args}`.
_SCRIPT = re.compile(r"\{path\}([A-Za-z0-9_./-]+\.py)")


#: The two module-level names an op uses to declare what it accepts.
_VOCABULARY_NAMES = ("_FILTER_KEYS", "_FLAGS")


def _declares_a_vocabulary(path: Path) -> bool:
    """Does this script bind `_FILTER_KEYS` or `_FLAGS` at module level?

    Parsed rather than grepped. A substring test for `"_FLAGS = "` misses
    `_FLAGS: set[str] = {...}` over one added annotation, and the op would then
    drop out of the sweep silently — a checker that stops looking, reported as a
    registry with no drift, which is the exact defect class #1239 is filed under.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover - the syntax-floor test owns this
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if any(n in _VOCABULARY_NAMES for n in names):
            return True
    return False


def _op_modules() -> list:
    """`(op_name, manifest, syntax, module)` for every op whose script declares
    a filter vocabulary. Discovery is a parse; the *values* are read by import
    rather than from the AST, because `_FILTER_KEYS` is built from
    another dict in `gitlab/mrs.py` (`set(_FILTER_FLAG) | {...}`), so a literal
    reader would silently see an incomplete set — the absence-read-as-absence
    shape this repo keeps filing.
    """
    found = []
    for manifest in sorted(PRESETS.glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, entry in sorted((data.get("ops") or {}).items()):
            if not isinstance(entry, dict):
                continue
            hit = _SCRIPT.search(str(entry.get("cmd") or ""))
            if not hit:
                continue
            path = PRESETS / hit.group(1)
            if not path.is_file():
                continue
            if not _declares_a_vocabulary(path):
                continue
            spec = importlib.util.spec_from_file_location(
                f"_syntax_drift_{name.replace('-', '_')}", path)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            found.append((name, manifest.name, str(entry.get("syntax") or ""),
                          mod))
    return found


MODULES = _op_modules()


def test_the_sweep_actually_found_ops() -> None:
    """A zero-length parametrisation passes silently and proves nothing —
    the discovery going quiet must not read as a registry with no drift."""
    names = [n for n, _m, _s, _mod in MODULES]
    assert len(names) >= 3, names
    for expected in ("gh-issues", "gh-prs", "gl-mrs"):
        assert expected in names, names


def test_an_annotated_declaration_is_still_discovered(tmp_path) -> None:
    """`_FLAGS: set[str] = {...}` must not fall out of the sweep.

    The discovery was a substring test until the review of this change; one
    added type annotation would have dropped an op from the sweep with no
    failure anywhere, and the count guard above cannot see it because the other
    ops keep the total up.
    """
    plain = tmp_path / "plain.py"
    plain.write_text("_FLAGS = {'a'}", encoding="utf-8")
    annotated = tmp_path / "annotated.py"
    annotated.write_text("_FLAGS: set = {'a'}", encoding="utf-8")
    nested = tmp_path / "nested.py"
    nested.write_text("def f():" + chr(10) + "    _FLAGS = {'a'}", encoding="utf-8")

    assert _declares_a_vocabulary(plain)
    assert _declares_a_vocabulary(annotated)
    assert not _declares_a_vocabulary(nested)


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1239)


def _tokens(mod) -> list:
    out = []
    for key in sorted(getattr(mod, "_FILTER_KEYS", set()) or set()):
        out.append((f"{key}=", "filter key"))
    for flag in sorted(getattr(mod, "_FLAGS", set()) or set()):
        out.append((flag, "flag"))
    return out


@pytest.mark.parametrize(
    "op,manifest,syntax,mod",
    MODULES,
    ids=[n for n, _m, _s, _mod in MODULES])
def test_every_accepted_token_is_declared_in_the_syntax(
        op: str, manifest: str, syntax: str, mod) -> None:
    missing = [f"{tok!r} ({kind})" for tok, kind in _tokens(mod)
               if tok not in syntax]
    assert not missing, (
        f"{op} (presets/{manifest}) accepts {', '.join(missing)} and its "
        f"declared syntax does not name it:\n    {syntax}\n"
        f"The registry is the oracle a reference checker, a completion and a "
        f"person reading `ops` all read — an omission there reports a correct "
        f"invocation as wrong (#1239)."
    )
