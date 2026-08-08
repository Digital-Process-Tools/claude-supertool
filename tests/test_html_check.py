"""Tests for the html-check validator adapter (#833)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _adapter_verdict import assert_ok, assert_declined

ADAPTER = Path(__file__).parent.parent / "validators" / "html-check" / "html-check.py"


def _run(file_path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert result.stdout.strip(), f"no stdout; stderr={result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The gap #833 exists to close: broken inline JS in <script> must be caught
# ---------------------------------------------------------------------------

def test_broken_inline_script_is_a_finding(tmp_path: Path) -> None:
    f = tmp_path / "dashboard.html"
    f.write_text(
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><title>x</title></head>\n"
        "<body>\n"
        "<script>\n"
        "const b = (;\n"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] >= 1
    err = out["errors"][0]
    assert err["line"] == 6, f"expected the error pinned to source line 6, got {err}"


def test_valid_inline_script_is_ok(tmp_path: Path) -> None:
    f = tmp_path / "dashboard.html"
    f.write_text(
        "<!DOCTYPE html>\n"
        "<html><body>\n"
        "<script>\n"
        "const a = 1;\n"
        "console.log(a);\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_html_with_no_script_is_ok(tmp_path: Path) -> None:
    f = tmp_path / "plain.html"
    f.write_text("<html><body><p>hello</p></body></html>\n")
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# Scope: external and non-JS <script> blocks must be left alone
# ---------------------------------------------------------------------------

def test_external_script_src_is_not_checked(tmp_path: Path) -> None:
    """A src= script has no inline body — broken syntax elsewhere must not leak in."""
    f = tmp_path / "external.html"
    f.write_text(
        "<html><body>\n"
        '<script src="/app.js">this is not js and must be ignored anyway(;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_json_type_script_is_not_checked(tmp_path: Path) -> None:
    """type=application/json payloads are data, not JS — must not be run through node."""
    f = tmp_path / "data.html"
    f.write_text(
        "<html><body>\n"
        '<script type="application/json">{not: valid, json,,,}</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)
    assert out["count"] == 0


def test_data_src_attribute_is_not_mistaken_for_src(tmp_path: Path) -> None:
    """data-src (consent-management scripts like OneTrust) is not `src` —

    a `\b`-based attribute match fires on the hyphen and would misread a
    still-inline, still-real script as external and skip it.
    """
    f = tmp_path / "consent.html"
    f.write_text(
        "<html><body>\n"
        '<script data-src="/deferred.js">const b = (;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_data_type_attribute_is_not_mistaken_for_type(tmp_path: Path) -> None:
    f = tmp_path / "consent.html"
    f.write_text(
        "<html><body>\n"
        '<script data-type="application/json">const b = (;</script>\n'
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1


def test_two_script_blocks_are_paired_and_checked_independently(tmp_path: Path) -> None:
    """Non-greedy `.*?` scanning must not let the first tag's search swallow
    the second, real inline block as part of the first one's body."""
    f = tmp_path / "two.html"
    f.write_text(
        "<html><body>\n"
        "<script>const a = 1;</script>\n"
        "<script>\n"
        "const b = (;\n"
        "</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_declined(out)
    assert out["count"] == 1
    assert out["errors"][0]["line"] == 4


# ---------------------------------------------------------------------------
# A strict HTML parser would reject a page a browser renders fine — this
# adapter must not (judgment call: script-extraction, not well-formedness).
# ---------------------------------------------------------------------------

def test_unclosed_void_tags_do_not_fail_the_check(tmp_path: Path) -> None:
    f = tmp_path / "loose.html"
    f.write_text(
        "<html><body>\n"
        '<img src="x.png">\n'
        "<br>\n"
        "<script>const a = 1;</script>\n"
        "</body></html>\n"
    )
    out = _run(str(f))
    assert_ok(out)


# ---------------------------------------------------------------------------
# Tool absent — third state, never a silent ok (per SCHEMA.md)
# ---------------------------------------------------------------------------

def test_missing_node_is_skipped_not_ok(tmp_path: Path) -> None:
    f = tmp_path / "dashboard.html"
    f.write_text("<html><body><script>const a = 1;</script></body></html>\n")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": str(empty_bin)}
    result = subprocess.run(
        [sys.executable, str(ADAPTER), str(f)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, f"expected the third state, got {out}"
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    assert "node" in out["skipped"].lower()


# ---------------------------------------------------------------------------
# No argument / schema shape
# ---------------------------------------------------------------------------

def test_no_arg_returns_error() -> None:
    result = subprocess.run(
        [sys.executable, str(ADAPTER)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert_declined(out)
    assert out["errors"][0]["code"] == "adapter"


def test_output_contains_required_fields(tmp_path: Path) -> None:
    f = tmp_path / "plain.html"
    f.write_text("<html><body><p>hi</p></body></html>\n")
    out = _run(str(f))
    for key in ("tool", "file", "ok", "count", "errors", "duration_ms"):
        assert key in out
