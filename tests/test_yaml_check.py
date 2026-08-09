"""Tests for the yaml-check validator adapter."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
ADAPTER = REPO / "validators" / "yaml-check" / "yaml-check.py"
PYPROJECT = REPO / "pyproject.toml"

# The interpreter running pytest, and no other. Until #1213 this hunted for a
# `python3.13` on PATH because that is where the author's PyYAML happened to
# live, so the happy-path tests below ran against an interpreter nobody
# configured. On CI no such interpreter exists, the fallback had no PyYAML, and
# they passed anyway on the `ok: true` this PR removed — four assertions about
# parsing YAML, on twelve legs, holding up a fabricated verdict.
_PYTHON_WITH_YAML = sys.executable
_HAS_PYYAML = subprocess.run(
    [_PYTHON_WITH_YAML, "-c", "import yaml"],
    capture_output=True,
).returncode == 0
_NEEDS_PYYAML = pytest.mark.skipif(
    not _HAS_PYYAML,
    reason="PyYAML not installed for this interpreter — `pip install -e .[dev]` provides it")


def _run(file_path: str, python: str = _PYTHON_WITH_YAML) -> tuple[dict, str]:
    result = subprocess.run(
        [python, str(ADAPTER), file_path],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout), result.stderr


# ---------------------------------------------------------------------------
# Valid YAML
# ---------------------------------------------------------------------------

@_NEEDS_PYYAML
def test_valid_yaml_simple(tmp_path: Path) -> None:
    f = tmp_path / "good.yml"
    f.write_text("key: value\nnum: 42\n")
    out, _ = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["errors"] == []
    assert out["tool"] == "yaml-check"


@_NEEDS_PYYAML
def test_valid_yaml_list(tmp_path: Path) -> None:
    f = tmp_path / "list.yml"
    f.write_text("- one\n- two\n- three\n")
    out, _ = _run(str(f))
    assert out["ok"] is True
    assert out["count"] == 0


@_NEEDS_PYYAML
def test_valid_yaml_nested(tmp_path: Path) -> None:
    f = tmp_path / "nested.yaml"
    f.write_text("stages:\n  - build\n  - test\njobs:\n  build:\n    script: make\n")
    out, _ = _run(str(f))
    assert out["ok"] is True


@_NEEDS_PYYAML
def test_valid_gitlab_ci_like(tmp_path: Path) -> None:
    f = tmp_path / ".gitlab-ci.yml"
    f.write_text(
        "image: php:8.3\nstages:\n  - test\nphpunit:\n  stage: test\n  script:\n    - phpunit\n"
    )
    out, _ = _run(str(f))
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Invalid YAML (requires PyYAML)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("key: [\nunclosed bracket\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    assert out["count"] == 1
    assert len(out["errors"]) == 1


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_has_line_info(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("good: ok\nbad: [\nstill bad\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert err["severity"] == "error"
    assert err["code"] == "syntax"


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_invalid_yaml_msg_populated(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("key: : invalid\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    assert out["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# Missing file (requires PyYAML — without it the validator exits 0)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_missing_file_returns_error(tmp_path: Path) -> None:
    out, _ = _run(str(tmp_path / "nonexistent.yml"))
    assert out["ok"] is False
    assert out["count"] == 1
    err = out["errors"][0]
    assert err["code"] == "adapter"
    assert "not found" in err["msg"]


# ---------------------------------------------------------------------------
# No argument
# ---------------------------------------------------------------------------

def test_no_arg_returns_error() -> None:
    result = subprocess.run(
        [sys.executable, str(ADAPTER)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# PyYAML missing — graceful degrade
# ---------------------------------------------------------------------------

def test_pyyaml_missing_is_the_third_state(tmp_path: Path) -> None:
    """Absent PyYAML is `skipped`, not `ok: true` (#1202).

    The reason moved from stderr into the payload, where a consumer can see it:
    a stderr warning next to `ok: true` is not something the delta, the
    validator row or CI reads.
    """
    f = tmp_path / "any.yml"
    f.write_text("key: value\n")
    # Patch builtins.__import__ to raise ImportError for 'yaml'
    shim = tmp_path / "yaml_missing_shim.py"
    shim.write_text(
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _mock(name, *a, **kw):\n"
        "    if name == 'yaml':\n"
        "        raise ImportError('mocked missing')\n"
        "    return _real(name, *a, **kw)\n"
        "builtins.__import__ = _mock\n"
        f"import runpy; runpy.run_path({str(ADAPTER)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, str(shim), str(f)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    out = json.loads(result.stdout)
    assert "skipped" in out, out
    assert "PyYAML" in out["skipped"], out
    # Not a pass, and not a failure either: `count`/`errors` are as absent as
    # `ok`, so no consumer reading any one of the three can turn an unparsed
    # file into a verdict about it.
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

def test_output_is_exactly_one_of_the_two_shapes(tmp_path: Path) -> None:
    """A verdict or a skip — never both, never neither (#1213).

    This asserted `ok` unconditionally, which is a claim about PyYAML being
    installed rather than about the adapter. Whether it is installed is the
    machine's business; what the adapter owes is that a reader can always tell
    which of the three states came back. A skip carries no verdict key, so it
    reads as neither a pass nor a failure, and both shapes describe the attempt.
    """
    f = tmp_path / "x.yml"
    f.write_text("a: 1\n")
    out, _ = _run(str(f))
    for key in ("tool", "file", "duration_ms"):
        assert key in out, out
    verdict = [k for k in ("ok", "count", "errors") if k in out]
    if "skipped" in out:
        assert out["skipped"], out
        assert verdict == [], f"a skip must carry no verdict key: {out}"
    else:
        assert verdict == ["ok", "count", "errors"], (
            f"a verdict must be complete, not partial: {out}")


def test_duration_ms_is_int(tmp_path: Path) -> None:
    f = tmp_path / "x.yml"
    f.write_text("a: 1\n")
    out, _ = _run(str(f))
    assert isinstance(out["duration_ms"], int)


def test_pyyaml_is_a_dev_dependency_so_ci_runs_the_parsing_half() -> None:
    """The gate above must not be how yaml-check gets tested on every leg (#1213).

    Every assertion in this file about parsing YAML is skipped when PyYAML is
    absent, which is honest and is also how a suite reports green while
    exercising nothing — this repo's most-filed defect, one level up. The dev
    extra is what makes the skip a local convenience rather than the CI outcome:
    both workflows install `.[dev]`, so removing this line would silently retire
    the parsing half instead of reddening anything. Same reasoning, and the same
    line, as `ruff`.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^dev = \[(.*?)^\]", text, re.S | re.M)
    assert block, "no `dev = [...]` extra in pyproject.toml"
    assert re.search(r'"pyyaml"', block.group(1), re.I), (
        "pyyaml dropped from the dev extra — tests/test_yaml_check.py's parsing "
        "half would skip on every CI leg and the board would stay green")


@pytest.mark.skipif(not _HAS_PYYAML, reason="PyYAML not available on this interpreter")
def test_source_context_present_on_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.yml"
    f.write_text("good: ok\nbad: [\nstill bad\n")
    out, _ = _run(str(f))
    assert out["ok"] is False
    err = out["errors"][0]
    assert err["line"] is not None
    assert "source_context" in err
    assert isinstance(err["source_context"], list)
    assert len(err["source_context"]) > 0
