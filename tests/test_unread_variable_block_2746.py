"""A validator, formatter or mcp spec whose variable block nothing reads is
said, not dropped (#2746).

#2734 renamed the spec key `env` to `variables` (the directory's scanner reads
the old string as a whole-environment read). A config still on the old key
lost its variables with no word: the audit ran a validator with
`"env": {"PROBE_X": "1"}` and it saw PROBE_X unset. The loader now names any
key holding NAME=value pairs that nothing reads, on a spec with no
`variables`, and points at `variables`. The key is printed from the user's
config, so the shipped code never spells the old one.
"""
import json
import subprocess
import sys
from pathlib import Path

import supertool

ROOT = Path(__file__).resolve().parent.parent


def test_the_old_key_is_named_with_its_replacement() -> None:
    found = supertool._unread_variable_blocks(
        {"validators": {"probe": {"cmd": "x", "env": {"PROBE_X": "1"}}}})
    assert len(found) == 1, found
    assert "validators.probe" in found[0] and "'env'" in found[0] and "variables" in found[0]


def test_formatters_and_mcp_are_read_too() -> None:
    found = supertool._unread_variable_blocks({
        "formatters": {"f": {"cmd": "x", "env": {"A": "1"}}},
        "mcp": {"m": {"cmd": "x", "env": {"B_2": "y"}}},
    })
    assert [w.split(":")[0] for w in found] == ["formatters.f", "mcp.m"], found


def test_a_spec_that_uses_variables_is_quiet() -> None:
    assert supertool._unread_variable_blocks(
        {"validators": {"ok": {"cmd": "x", "variables": {"PROBE_X": "1"}}}}) == []


def test_a_mapping_that_is_not_variable_shaped_is_quiet() -> None:
    assert supertool._unread_variable_blocks(
        {"validators": {"ok": {"cmd": "x", "resolve": {"php": "vendor/bin"}}}}) == []


def test_the_warning_reaches_the_caller(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".supertool.json").write_text(json.dumps(
        {"validators": {"probe": {"cmd": "true", "env": {"PROBE_X": "1"}}}}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "supertool.py"), "version"], cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    said = r.stdout + r.stderr
    assert "validators.probe" in said and "variables" in said, said
