r"""markdownlint and phpmd carry the same unanchored line parser #1934 fixed
in five sibling adapters (#1940): `^(?:.*?):(\d+)...` binds to the EARLIEST
`:digit` run anywhere in the line, including one supplied by a filename
crafted to contain its own `N:M: ` sequence -- discarding the real
diagnostic location and reporting the filename author's chosen line/col
instead.

Control pair per adapter, mirroring #1934/#1937: a file whose *name* contains
a `:1:1: ` sequence must report the real diagnostic line and column, and an
ordinary filename must keep reporting exactly what it reports today.

Both tools are stubbed at `subprocess.run`, following
tests/test_validators_scope_is_not_a_verdict_1601.py's `_adapter`/`_drive`
pattern, rather than gated on the tool being installed -- neither ships on
the CI image, and a gated test asserts nothing anywhere it matters.

**Observed, not reasoned, for both adapters** (#1940 asks explicitly not to
assume this): markdownlint-cli 0.49.1 (via npx, node v22.22.1) and phpmd
2.15.0 (phar, PHP 8.2) were both run directly against a crafted
`x:1:1: fake.{md,php}` filename on this machine and both echo the exact
argv path back, embedded fragment and all -- see the module docstrings in
validators/markdownlint/markdownlint.py and validators/phpmd/phpmd.py for
the full transcripts.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

VALIDATORS = Path(__file__).resolve().parent.parent / "validators"


def _adapter(directory: str, filename: str):
    path = VALIDATORS / directory / filename
    spec = importlib.util.spec_from_file_location(
        "adapter_1940_" + directory.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(mod, monkeypatch, capsys, target, runner) -> dict:
    monkeypatch.setattr(mod.subprocess, "run", runner)
    if hasattr(mod, "shutil"):
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/stub")
    monkeypatch.setattr(mod.sys, "argv", ["adapter.py", str(target)])
    mod.main()
    return json.loads(capsys.readouterr().out.strip())


# ---------------------------------------------------------------------------
# markdownlint
# ---------------------------------------------------------------------------

def _md_runner(stdout: str, rc: int = 1):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc, stdout, "")
    return runner


def test_markdownlint_crafted_filename_reports_the_real_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    r"""A filename crafted to embed the WHOLE tail shape the old regex
    expected -- not merely a bare `1:1` -- must not hijack the location.

    A bare `:1:1: ` embed is not sufficient to fool the old parser: its tail
    pattern requires `\s+(?:error|warning\s+)?MD\d+[^\s]*\s+`, so unless the
    embedded fragment ALSO satisfies that shape, the non-greedy `.*?` simply
    fails at the embedded position and keeps scanning until it reaches the
    tool's own real diagnostic further down the line -- which would make
    this test pass against the OLD code too, for the wrong reason (verified
    while writing it: a bare `x:1:1: fake.md` embed does not reproduce the
    misattribution here, because "fake.md" does not match `MD\d+`).
    """
    mod = _adapter("markdownlint", "markdownlint.py")
    f = tmp_path / "x:1:1 MD000 evilrule evil message.md"
    f.write_text("#bad heading\n\ntext\n", encoding="utf-8")
    # The real diagnostic is at line 9, col 3 -- nowhere near the filename's
    # own embedded "1:1 MD000". markdownlint echoes the exact argv path
    # (observed, see the module docstring above).
    stdout = f"{f}:9:3 MD018/no-missing-space-atx No space after hash\n"
    out = _drive(mod, monkeypatch, capsys, f, _md_runner(stdout))
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 9, err
    assert err["col"] == 3, err
    assert err["code"] == "MD018/no-missing-space-atx", err


def test_markdownlint_ordinary_filename_still_reports_what_it_reports_today(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """The control half: an ordinary filename is unaffected by the anchor."""
    mod = _adapter("markdownlint", "markdownlint.py")
    f = tmp_path / "ordinary.md"
    f.write_text("#bad heading\n\ntext\n", encoding="utf-8")
    stdout = f"{f}:1:1 MD018/no-missing-space-atx No space after hash\n"
    out = _drive(mod, monkeypatch, capsys, f, _md_runner(stdout))
    assert out["ok"] is False, out
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 1, err
    assert err["col"] == 1, err


# ---------------------------------------------------------------------------
# phpmd
# ---------------------------------------------------------------------------

def _phpmd_run(stdout: str, rc: int = 2):
    return subprocess.CompletedProcess(["phpmd"], rc, stdout, "")


def test_phpmd_crafted_filename_reports_the_real_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Mirrors the markdownlint case above: the crafted filename must embed
    the WHOLE tab-separated tail shape the old regex expected -- a bare
    `:1:1: ` is not enough, since the old pattern requires a literal TAB
    right after the digits, which an ordinary `1:1:` embed does not supply.
    A tab is an ordinary, legal byte in a POSIX filename.
    """
    mod = _adapter("phpmd", "phpmd.py")
    f = tmp_path / "x:1\tEvilRule\tfake finding.php"
    f.write_text("<?php\n$x = 1;\n", encoding="utf-8")
    # Real diagnostic at line 9 -- nowhere near the filename's own "x:1".
    stdout = f"{f}:9\tUnusedLocalVariable\tAvoid unused local variables such as '$x'.\n"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _phpmd_run(stdout))
    monkeypatch.setattr(mod.sys, "argv", ["phpmd.py", str(f)])
    mod.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 9, err
    assert err["code"] == "UnusedLocalVariable", err


def test_phpmd_ordinary_filename_still_reports_what_it_reports_today(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    mod = _adapter("phpmd", "phpmd.py")
    f = tmp_path / "ordinary.php"
    f.write_text("<?php\n$x = 1;\n", encoding="utf-8")
    stdout = f"{f}:3\tUnusedLocalVariable\tAvoid unused local variables such as '$x'.\n"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _phpmd_run(stdout))
    monkeypatch.setattr(mod.sys, "argv", ["phpmd.py", str(f)])
    mod.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 3, err
    assert err["code"] == "UnusedLocalVariable", err


def test_phpmd_observed_real_output_shape_space_separated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Real phpmd 2.15.0 uses repeated SPACES, not tabs -- observed directly
    against the phar on this machine, transcript in phpmd.py's own comment."""
    mod = _adapter("phpmd", "phpmd.py")
    f = tmp_path / "dirty.php"
    f.write_text("<?php\nfunction f() {\n    $x = 1;\n}\n", encoding="utf-8")
    stdout = (f"{f}:2  ShortMethodName      Avoid using short method names "
              "like ::f(). The configured minimum method name length is 3.\n")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _phpmd_run(stdout))
    monkeypatch.setattr(mod.sys, "argv", ["phpmd.py", str(f)])
    mod.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["count"] == 1, out
    err = out["errors"][0]
    assert err["line"] == 2, err
    assert err["code"] == "ShortMethodName", err
