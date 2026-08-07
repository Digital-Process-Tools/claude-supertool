"""`_mcp_specs` is per-run scratch, not a permanent table (#1030).

`_load_config()` parses the optional `mcp` block into the module-level
`_mcp_specs` dict *in place*. Any test that forces a real config load —
`tests/test_at_file_route.py::TestPayloadRoutePin` chdirs to the repo root,
clears `_CONFIG_CHECKED` and rebuilds the registry, which is a legitimate
thing to do — therefore leaves this repo's own `py-lsp` spec (`match: "*.py"`,
tools `refs`/`diag`) in the table for the rest of that xdist worker's life.

Every later `op_workspace` / `resolve` / `refs` call on a `.py` file in that
worker then routes to an LSP that no test asked for. That is the whole of
#1030: ~10 of the 16 tests in `tests/test_op_workspace.py` drive a `.py`
file, and whether they see it depends on how `--dist load` happened to split
the run — green on one run, red on the next, green in isolation either way.

The conftest fixture already saves and restores `_CONFIG`, `_CONFIG_CHECKED`
and `_CONFIG_PATH`. `_mcp_specs` is derived from `_CONFIG` and was missed,
and the guard that exists to catch exactly this omission could not see it —
see `test_state_reset_and_lint_timeout.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import conftest
import supertool


_SPEC = {
    "cmd": "cclsp",
    "match": "*.py",
    "tools": {"refs": "find_references", "diag": "get_diagnostics"},
}


def test_reset_module_state_clears_mcp_specs() -> None:
    """The per-test reset must return `_mcp_specs` to empty.

    Driven directly rather than through test ordering: under `-n auto` xdist
    splits individual tests, so an a/b pair can land on two workers and the
    second half then passes without the first having polluted anything.
    """
    supertool._mcp_specs["py-lsp"] = dict(_SPEC)
    assert supertool._mcp_route("x.py", "refs") == ("py-lsp", "find_references")

    conftest._reset_module_state()

    assert supertool._mcp_specs == {}
    assert supertool._mcp_route("x.py", "refs") is None


def test_a_real_config_load_populates_mcp_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pollution is the setup — and it comes from production code.

    Nothing here reaches into a private table by hand: `_load_config()` is the
    only writer, and this is the shape of config that makes it write.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"mcp": {"py-lsp": _SPEC}}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)

    supertool._load_config()

    assert "py-lsp" in supertool._mcp_specs


def test_b_the_next_test_sees_no_leftover_mcp_route() -> None:
    """Best-effort pair for the real path; may co-schedule elsewhere.

    Kept because it is the only assertion that exercises the *fixture*, not
    `_reset_module_state` directly. When xdist puts the two halves on
    different workers this passes without having been tested — the
    deterministic claim is the test above, not this one.
    """
    assert supertool._mcp_specs == {}
    assert supertool._mcp_route("x.py", "refs") is None
