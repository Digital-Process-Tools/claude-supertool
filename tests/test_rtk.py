from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

import supertool


def _rtk_available() -> bool:
    return which("rtk") is not None


@pytest.mark.skipif(not _rtk_available(), reason="rtk not installed")
def test_rtk_read_delegation(tmp_path: Path, enable_rtk) -> None:
    f = tmp_path / "sample.py"
    f.write_text("line1\nline2\nline3\n")
    out = supertool.op_read(str(f))
    assert "line1" in out
    assert "line2" in out


@pytest.mark.skipif(not _rtk_available(), reason="rtk not installed")
def test_rtk_grep_delegation(tmp_path: Path, enable_rtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\nimport sys\ndef main():\n    pass\n")
    out = supertool.op_grep("import", str(f))
    assert "import" in out


@pytest.mark.skipif(not _rtk_available(), reason="rtk not installed")
def test_rtk_wc_delegation(tmp_path: Path, enable_rtk) -> None:
    f = tmp_path / "sample.txt"
    f.write_text("hello world\nfoo bar baz\n")
    out = supertool.op_wc(str(f))
    assert "2" in out


@pytest.mark.skipif(not _rtk_available(), reason="rtk not installed")
def test_rtk_fallback_on_offset(tmp_path: Path, enable_rtk) -> None:
    """RTK not used when offset/filter specified — falls back to native."""
    f = tmp_path / "test.py"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.op_read(str(f), offset=2)
    assert "3→c" in out
