"""`pyproject.toml`'s version must match the code, like the manifest already does.

Two of the three places the version lives were pinned to each other:
`test_mcp_config_279.py::test_plugin_manifest_version_matches_code` ties
`.claude-plugin/plugin.json` to `supertool.VERSION`, and `test_version.py` ties
the `version` op's output to it.

`pyproject.toml` was pinned to nothing. It could drift silently, and the failure
would be the quiet kind this repository keeps filing: a wheel built from it
carries one version while every other surface agrees on another, and nothing
says so. Found while preparing 0.22.0 — the release is exactly when a bump gets
applied to two files out of three.
"""

from __future__ import annotations

import re
from pathlib import Path

import supertool

_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no top-level version = \"...\" line"
    return match.group(1)


def test_pyproject_version_matches_code() -> None:
    assert _pyproject_version() == supertool.VERSION, (
        f'pyproject.toml version {_pyproject_version()!r} != '
        f"supertool.VERSION {supertool.VERSION!r} — a release bump must move "
        "pyproject.toml, supertool.py and .claude-plugin/plugin.json together"
    )
