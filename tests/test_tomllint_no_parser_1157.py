"""tomllint with no TOML parser reachable must report the third state (#1157).

`ok: true, count: 0` on a machine where neither `tomllib` nor `tomli` can be
imported is a file that was never opened, published as a pass. SCHEMA.md,
"Skipped: the third state".

The absence is produced with a shim on `PYTHONPATH` rather than by hunting for a
3.10 interpreter: `PYTHONPATH` precedes the stdlib in `sys.path`, so a
`tomllib.py` that raises `ImportError` reproduces exactly the failed import a
3.10-without-tomli machine hits, on any interpreter and on any platform.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).parent.parent / "validators" / "tomllint" / "tomllint.py"


def _shim(tmp_path: Path) -> str:
    d = tmp_path / "noparser"
    d.mkdir(exist_ok=True)
    for name in ("tomllib", "tomli"):
        (d / f"{name}.py").write_text(
            f'raise ImportError("{name} is not available here")\n', encoding="utf-8"
        )
    return str(d)


def _run(tmp_path: Path, target: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _shim(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("SUPERTOOL_REQUIRE_VALIDATORS", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def _toml(tmp_path: Path) -> Path:
    f = tmp_path / "sample.toml"
    f.write_text('[package]\nname = "x"\n', encoding="utf-8")
    return f


def test_no_parser_emits_json_at_all(tmp_path: Path) -> None:
    """The adapter answers on stdout. The 3.11+ stdlib import was unguarded, so
    a `tomllib` that fails to import crashed it before any `emit` -- no JSON,
    exit 1, and nothing for the core to read but its own synthesised verdict."""
    r = _run(tmp_path, _toml(tmp_path))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)


def test_no_parser_is_skipped_not_ok(tmp_path: Path) -> None:
    out = json.loads(_run(tmp_path, _toml(tmp_path)).stdout)
    assert "skipped" in out
    assert "ok" not in out
    assert "count" not in out
    assert "errors" not in out
    assert out["tool"] == "tomllint"
    assert out["duration_ms"] >= 0


def test_no_parser_reason_names_the_missing_parser(tmp_path: Path) -> None:
    reason = json.loads(_run(tmp_path, _toml(tmp_path)).stdout)["skipped"]
    assert "tomli" in reason
    assert "pip install tomli" in reason
    assert "NOT checked" in reason


def test_no_parser_is_loud_when_required(tmp_path: Path) -> None:
    """$SUPERTOOL_REQUIRE_VALIDATORS turns the quiet local skip into a red."""
    out = json.loads(_run(tmp_path, _toml(tmp_path),
                          SUPERTOOL_REQUIRE_VALIDATORS="tomllint").stdout)
    assert "skipped" not in out
    assert out["ok"] is False
    assert out["count"] == 1
    assert out["errors"][0]["code"] == "adapter"
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]
    assert "NOT checked" in out["errors"][0]["msg"]


def test_parser_skip_precedes_the_missing_file_check(tmp_path: Path) -> None:
    """No parser is the reason the caller can act on, and it is the true one:
    without a parser the adapter would not have read the file either way."""
    out = json.loads(_run(tmp_path, tmp_path / "gone.toml").stdout)
    assert "skipped" in out


def test_parser_present_still_reports_a_verdict(tmp_path: Path) -> None:
    """The skip arm must not swallow the normal path. Same file, no shim."""
    if sys.version_info < (3, 11):
        import importlib.util
        if importlib.util.find_spec("tomli") is None:
            pytest.skip("no TOML parser on this interpreter")
    f = _toml(tmp_path)
    r = subprocess.run([sys.executable, str(ADAPTER), str(f)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = json.loads(r.stdout)
    assert out["ok"] is True
    assert out["count"] == 0
