"""#1757 -- the reserved-key denylist leaks two more core-only fields into a
preset op's own subprocess, and three shipped `.supertool.json` config
defaults (`default_status_posts`/`_comments`/`_limit`) are stamped under a
name (`SUPERTOOL_DEFAULT_STATUS_*`) nothing reads: `presets/devto/status_since.py`,
`presets/hashnode/status_since.py` and `presets/bluesky/status_since.py` all
read the un-prefixed `SUPERTOOL_STATUS_POSTS` / `_COMMENTS` / `_LIMIT`. A user
who sets a `default_status_*` key in their config gets the hardcoded fallback
and no sign their setting was ignored.

Two sub-shapes, two-plus tests each:

1. `safety` and `repo_target` are ops-entry metadata the core reads off the
   registry (`.get("safety")`, `.get("repo_target")`); nothing reads
   `SUPERTOOL_SAFETY` / `SUPERTOOL_REPO_TARGET` out of a subprocess's own
   environment -- a tree-wide grep for either string returns zero hits. They
   belong in `_OP_CONFIG_RESERVED_KEYS` alongside `replaces` (#1347), `paths`
   (#1357) and `exitStatus` (#1672).

2. The three `default_status_*` keys in `presets/devto.json`,
   `presets/hashnode.json` and `presets/bluesky.json` must be renamed so the
   stamped env var matches the name the corresponding `status_since.py`
   already reads -- fixed at the stamp (the config key), not at the three
   readers, since the un-prefixed name is what is already documented in each
   module's own docstring and shared by all three presets.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import supertool

_ROOT = Path(__file__).resolve().parent.parent


# --- sub-shape 1: core-only metadata leaked into the op's own subprocess ---

def test_safety_and_repo_target_are_reserved():
    """Both are read only by core off the registry entry
    (`.get("safety")` at the safety-classification helper, `.get("repo_target")`
    in `_repo_target_modes`) -- never by the op's own subprocess. Confirmed
    by a tree-wide grep for the env var name: zero hits for either
    `SUPERTOOL_SAFETY` or `SUPERTOOL_REPO_TARGET`.
    """
    assert "safety" in supertool._OP_CONFIG_RESERVED_KEYS
    assert "repo_target" in supertool._OP_CONFIG_RESERVED_KEYS


def test_docs_and_code_name_the_same_reserved_keys():
    """docs/contributing.md's reserved-key sentence must list exactly what
    `_OP_CONFIG_RESERVED_KEYS` reserves -- the same drift #1675 fixed once
    already, now for `safety` and `repo_target`."""
    text = (_ROOT / "docs/contributing.md").read_text(encoding="utf-8")
    m = re.search(r"Any key in an op config that isn.t a reserved key \(([^)]+)\)",
                  text)
    assert m, "reserved-key sentence not found in docs/contributing.md"
    documented = {tok.strip(" `") for tok in m.group(1).split(",")}
    coded = set(supertool._OP_CONFIG_RESERVED_KEYS)
    assert documented == coded, (
        f"docs/contributing.md and _OP_CONFIG_RESERVED_KEYS disagree -- "
        f"docs only: {documented - coded}, code only: {coded - documented}")


def test_safety_and_repo_target_not_handed_to_the_op_subprocess(tmp_path):
    """Same shape as #1357's `test_paths_is_not_handed_to_the_op_subprocess`:
    a negative assertion (the reserved keys are absent) paired with a
    positive control (an ordinary config key still reaches the subprocess),
    so a broken harness that saw nothing cannot pass this by accident."""
    probe = ("import os, json; print(json.dumps(sorted("
             "k for k in os.environ if k.startswith('SUPERTOOL_'))))")
    (tmp_path / ".supertool.json").write_text(json.dumps({
        "ops": {
            "probe": {
                "safety": "read-only",
                "repo_target": "op",
                "cmd": "{python} -c " + json.dumps(probe),
                "lines": 80,
                "syntax": "probe",
            }
        }
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "probe"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(tmp_path), timeout=120)
    assert "SUPERTOOL_LINES" in proc.stdout, proc.stdout  # positive control
    assert "SUPERTOOL_SAFETY" not in proc.stdout, proc.stdout
    assert "SUPERTOOL_REPO_TARGET" not in proc.stdout, proc.stdout


# --- sub-shape 2: default_status_* config that nothing reads ---

_STATUS_SINCE = {
    "devto": ("presets/devto.json", "devto_status_since",
              "presets/devto/status_since.py"),
    "hashnode": ("presets/hashnode.json", "hashnode_status_since",
                 "presets/hashnode/status_since.py"),
    "bluesky": ("presets/bluesky.json", "bluesky_status_since",
                "presets/bluesky/status_since.py"),
}


def _entry_env_names(manifest: str, op: str) -> set:
    data = json.loads((_ROOT / manifest).read_text(encoding="utf-8"))
    entry = data["ops"][op]
    return {f"SUPERTOOL_{k.upper()}" for k in entry
            if k not in supertool._OP_CONFIG_RESERVED_KEYS}


def _reader_env_names(module: str) -> set:
    text = (_ROOT / module).read_text(encoding="utf-8")
    return set(re.findall(r'env_int\("(SUPERTOOL_[A-Z_]+)"', text))


def test_default_status_config_reaches_the_env_var_its_own_preset_reads():
    """For every one of the three status-briefing presets, whatever the
    shipped config's `default_status_*` keys become as `SUPERTOOL_` env vars
    must intersect what that preset's own `status_since.py` actually reads
    with `env_int`. Before the fix the config is stamped under
    `SUPERTOOL_DEFAULT_STATUS_POSTS`/`_COMMENTS`/`_LIMIT` and every reader
    asks for the un-prefixed name -- disjoint sets, so this is red today."""
    for name, (manifest, op, module) in _STATUS_SINCE.items():
        stamped = _entry_env_names(manifest, op)
        read = _reader_env_names(module)
        assert stamped & read, (
            f"{name}: config on {manifest}:{op} stamps {sorted(stamped)}, "
            f"but {module} reads {sorted(read)} -- no overlap, so every "
            f"shipped default is silently ignored")


def test_no_default_prefixed_status_keys_remain_in_shipped_config():
    """The naming-mismatch fix moved the config keys themselves (fixed at
    the stamp, not the readers) -- `default_status_posts` and friends should
    no longer appear in any shipped preset manifest."""
    for manifest, _op, _module in _STATUS_SINCE.values():
        text = (_ROOT / manifest).read_text(encoding="utf-8")
        assert "default_status_" not in text, (
            f"{manifest} still has a default_status_* key")
