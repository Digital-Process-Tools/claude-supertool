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


def _op_modules() -> list:
    """`(op_name, manifest, syntax, module)` for every op whose script declares
    a filter vocabulary. Imported, not AST-read: `_FILTER_KEYS` is built from
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
            source = path.read_text(encoding="utf-8", errors="replace")
            if "_FILTER_KEYS" not in source and "_FLAGS = " not in source:
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
