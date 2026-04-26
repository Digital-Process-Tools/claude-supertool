from __future__ import annotations

import supertool


# ---------------------------------------------------------------------------
# op_version
# ---------------------------------------------------------------------------

def test_version_returns_version_string() -> None:
    out = supertool.op_version()
    assert out == f"supertool {supertool.VERSION}\n"


def test_version_dispatch() -> None:
    out = supertool.dispatch("version")
    assert supertool.VERSION in out
    assert "---" not in out  # meta-op, no header
