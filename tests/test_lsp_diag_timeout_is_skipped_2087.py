"""A `supertool diag:FILE` subprocess that never answers must be `skipped`, not a finding (#2087).

#2087 reports `get_diagnostics` returning nothing after 21s for one file while
a smaller file in the same tree answers in 48ms, and separately shows the
supertool-level MCP infra messages ("no result from get_diagnostics", "MCP
server unavailable", ...) already rendering as `skipped` correctly --
`tests/test_lsp_diag_staleness_482.py` pins that path.

What it does not cover is the OTHER way this adapter can fail to get an
answer: the whole `supertool diag:FILE` child process hanging past its own
`subprocess.run(timeout=...)` wall, which is the actual net under an infra
condition the MCP layer never got to report cleanly (a stuck daemon, a
socket that never closes). Before this fix that path emitted

    {"ok": false, "count": 1, "errors": [{"code": "adapter", "msg": "timeout"}]}

-- a definite failing finding about the file, from an adapter that never
looked at it. That is this repo's own defect class in the other direction:
an absence (no answer arrived) rendered as a presence (a finding), rather
than the absence it actually is. `rollback_on_fail` is false for this
validator so it does not revert an edit, but it still tells a reader "lsp-diag
found something wrong here", which is false.

`SUPERTOOL_LSP_DIAG_TIMEOUT` makes the wall configurable so this is testable
in well under a second rather than at the real 30s default.

Would this pass if the code did nothing? No -- the current handler's receipt
has `ok: False` and no `skipped` key, so `_assert_not_a_verdict` below fails
against it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "lsp-diag" / "lsp-diag.py"

_spec = importlib.util.spec_from_file_location("lsp_diag_2087", ADAPTER)
assert _spec and _spec.loader
lsp_diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lsp_diag)


def _assert_not_a_verdict(receipt: dict) -> None:
    assert "skipped" in receipt, f"a hung diag call must decline, not report a finding: {receipt}"
    assert receipt.get("ok") is not True, f"receipt carries ok=True alongside a hung call: {receipt}"
    assert "ok" not in receipt and "count" not in receipt and "errors" not in receipt, (
        f"a skip must not also carry verdict keys a consumer could key off: {receipt}"
    )


def test_a_hung_supertool_child_is_skipped_not_a_finding(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("x = 1\n", encoding="utf-8")

    # A stub `supertool` that never returns within the (shortened) wall.
    stub = tmp_path / "fake_supertool.py"
    stub.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    env = dict(os.environ)
    env["SUPERTOOL_BIN"] = str(stub)
    env["SUPERTOOL_LSP_DIAG_TIMEOUT"] = "0.2"
    r = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                       capture_output=True, text=True, env=env, timeout=15,
                       encoding="utf-8", errors="replace")
    assert r.stdout.strip(), f"adapter produced no output; stderr:\n{r.stderr}"
    receipt = json.loads(r.stdout.strip().splitlines()[-1])
    _assert_not_a_verdict(receipt)
    assert "timed out" in receipt["skipped"].lower() or "timeout" in receipt["skipped"].lower()
