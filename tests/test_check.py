from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_check
# ---------------------------------------------------------------------------

def test_check_no_ops_defined() -> None:
    supertool._CONFIG = {}
    out = supertool.op_check("phpstan", "some/file.php")
    assert "ERROR" in out
    assert "no ops defined" in out


def test_check_unknown_preset() -> None:
    supertool._CONFIG = {
        "ops": {"phpstan": {"cmd": "php -l {file}"}, "phpmd": {"cmd": "echo {file}"}}
    }
    out = supertool.op_check("unknown", "file.php")
    assert "ERROR" in out
    assert "unknown" in out
    assert "phpstan" in out


def test_check_pass(tmp_path: Path) -> None:
    f = tmp_path / "good.txt"
    f.write_text("hello")
    supertool._CONFIG = {"ops": {"lint": {"cmd": "cat {file}"}}}
    out = supertool.op_check("lint", str(f))
    assert "PASS" in out


def test_check_fail() -> None:
    supertool._CONFIG = {"ops": {"fail": {"cmd": "exit 1"}}}
    out = supertool.op_check("fail", "dummy.php")
    assert "FAIL" in out


def test_check_timeout() -> None:
    supertool._CONFIG = {"ops": {"slow": {"cmd": "sleep 10", "timeout": 1}}}
    out = supertool.op_check("slow", "dummy.php")
    assert "FAIL" in out
    assert "timeout" in out


def test_check_dict_config(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("ok")
    supertool._CONFIG = {"ops": {"mycheck": {"cmd": "cat {file}", "timeout": 5}}}
    out = supertool.op_check("mycheck", str(f))
    assert "PASS" in out


def test_check_empty_preset() -> None:
    supertool._CONFIG = {"ops": {"lint": {"cmd": "echo ok"}}}
    out = supertool.op_check("", "file.php")
    assert "ERROR" in out
