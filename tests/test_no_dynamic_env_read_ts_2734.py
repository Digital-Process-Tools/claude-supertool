"""#2734 -- no shipped TypeScript/JavaScript reads an environment variable by a
name held in a variable.

The Python sweeps (tests/test_no_dynamic_environ_whole_tree_2734.py) never
looked at `.ts`/`.js`. `notifiers/claude-channel/channel.ts` had the exact
shape the directory validator cites as "an environment variable named at run
time": `capFromEnv(name, ...)` reading `process.env[name]`. The file set is
whatever the real release build ships, not a hand-picked list.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
SUFFIXES = (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs")

# `process.env[anything]`, and `process.env` handed over whole by bracket
# access through an alias, are the run-time-named shapes. A literal member
# access (`process.env.SUPERTOOL_X`) is the shape this file wants.
_BY_NAME = re.compile(r"process\.env\s*\[")


def _build(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("build_release_tree_ts_2734", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "built"
    mod.build(REPO_ROOT, "HEAD", out, mod.load_config(REPO_ROOT / ".github" / "release-branch.json"))
    return out


def _hits(text: str) -> list:
    return [n for n, line in enumerate(text.splitlines(), 1) if _BY_NAME.search(line)]


def test_the_pattern_catches_the_shape_it_is_for() -> None:
    assert _hits("const raw = process.env[name];") == [1]
    assert _hits("const raw = process.env.SUPERTOOL_X;") == []


def test_no_shipped_script_reads_the_environment_by_a_variable_name(tmp_path) -> None:
    built = _build(tmp_path)
    scripts = [p for p in built.rglob("*") if p.suffix in SUFFIXES and p.is_file()]
    assert any(p.name == "channel.ts" for p in scripts), "channel.ts no longer ships -- re-derive this test"
    found = {}
    for p in scripts:
        hits = _hits(p.read_text(encoding="utf-8", errors="replace"))
        if hits:
            found[p.relative_to(built).as_posix()] = hits
    assert not found, f"environment read by a variable name: {found}"
