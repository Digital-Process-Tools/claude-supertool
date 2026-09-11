from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import supertool


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# op_output_format — output format examples from .supertool.json, an env
# override (SUPERTOOL_OUTPUT_FORMAT), or a shipped default (#2342)
# ---------------------------------------------------------------------------

def test_output_format_from_config(tmp_path: Path, monkeypatch) -> None:
    """output-format key in config is output verbatim."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- read:foo ---\n     1→hello"
    }), encoding="utf-8")
    monkeypatch.delenv("SUPERTOOL_OUTPUT_FORMAT", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "--- read:foo ---" in out
    assert "1→hello" in out


def test_output_format_no_config_falls_back_to_shipped_default(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json at all -> the shipped default, not a permanent
    "No ... configured" non-answer (#2342)."""
    monkeypatch.delenv("SUPERTOOL_OUTPUT_FORMAT", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" not in out
    assert "--- op:args ---" in out
    assert "[batch]" in out


def test_output_format_missing_key_falls_back_to_shipped_default(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"compact": True}))
    monkeypatch.delenv("SUPERTOOL_OUTPUT_FORMAT", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" not in out
    assert "--- op:args ---" in out


def test_output_format_disabled_in_config_prints_not_configured(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"output-format": ""}))
    monkeypatch.delenv("SUPERTOOL_OUTPUT_FORMAT", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" in out


def test_output_format_env_override_wins_over_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({"output-format": "from config"}))
    monkeypatch.setenv("SUPERTOOL_OUTPUT_FORMAT", "from env")
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "from env" in out
    assert "from config" not in out


def test_output_format_env_can_disable_the_shipped_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_OUTPUT_FORMAT", "0")
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_output_format()
    assert "No output-format configured" in out


def test_output_format_dispatch(tmp_path: Path, monkeypatch) -> None:
    """dispatch('output-format') routes to op_output_format."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "output-format": "--- example ---\nsome output"
    }))
    monkeypatch.delenv("SUPERTOOL_OUTPUT_FORMAT", raising=False)
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("output-format")
    assert "--- output-format ---" not in out
    assert "--- example ---" in out


# ---------------------------------------------------------------------------
# The default's transcript must be measured against real output, not
# written from memory (#2342 acceptance criteria) — assert every shape it
# describes against a live batched call.
# ---------------------------------------------------------------------------

def test_output_format_default_shapes_match_a_live_batched_call() -> None:
    """The default is measured against real output rather than written from
    memory (#2342's acceptance criteria) -- assert every shape it names
    against a real, live batched call: a multi-line read, a miss, and
    `version` (a meta-op that prints no '---' header of its own, so the
    batch as a whole carries two headers rather than three)."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "supertool.py"),
         "read:README.md:1:3", "read:no-such-file-xyz-test.md", "version"],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
    )
    live = proc.stdout

    default = supertool._DEFAULT_OUTPUT_FORMAT

    # 1. per-op header, one per op, in call order — the only segment
    #    boundary. `version` is a meta-op and prints no header of its own,
    #    so only the two `read` ops contribute one each.
    headers = re.findall(r"^--- .+ ---$", live, re.MULTILINE)
    assert len(headers) == 2
    assert "--- op:args ---" in default

    # 2. a meta line sits directly under a read's header, carrying the verdict
    assert re.search(r"^\(\d+ lines, \d+ bytes", live, re.MULTILINE)
    assert re.search(r"\(\d+ lines, \d+ bytes\)", default)

    # 3. a failing op reports under its own header (error segment)
    assert "ERROR: file not found" in live
    assert "ERROR: file not found" in default

    # 4. the batch footer names ok/refused counts
    assert re.search(r"\[batch\] \d+ ops ran", live)
    assert "[batch]" in default

    # 5. the batch's exit code is distinct from any one op's outcome — the
    #    process exits non-zero (one op refused) while the other two
    #    complete answers are still printed
    assert proc.returncode != 0
    assert "read:README.md" in live
    assert "supertool " in live  # the version op's own answer still printed
    assert re.search(r"does\s+not mean no answers came back", default, re.IGNORECASE)

    # 6. the '↳ to modify:' affordance line, only on ops that carry one
    assert "↳ to modify:" in live
    assert "↳ to modify:" in default
