"""The new read op over the write-through mirror (#1955): `presets/github/mirror.py`.

Loaded and driven the same way `tests/test_gh_issue_classify_2049.py` drives
`issue.py` -- by path, calling `main()` with a monkeypatched `sys.argv` and a
`tmp_path` cwd, never by shelling out. TDD: run RED against a repo with no
`presets/github/mirror.py` yet, then GREEN.

Four renders, and the fourth must never look like any of the first three:
`cached (age)`, `not-cached`, `mirror-unreadable`, and "not configured at
all" -- which the issue's own three states do not name separately, but which
this op must not fold into `mirror-unreadable` (a mirror nobody turned on is
not a finding about a broken mirror).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "presets"))
import _mirror  # noqa: E402

PRESET_PATH = REPO / "presets" / "github" / "mirror.py"
_spec = importlib.util.spec_from_file_location("github_mirror_op_1955", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mirror_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror_op)


def _configure(tmp_path) -> Path:
    mirror_dir = tmp_path / ".max" / "gh-mirror"
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"gh_mirror_dir": str(mirror_dir.relative_to(tmp_path))}),
        encoding="utf-8",
    )
    return mirror_dir


def test_a_cached_issue_reports_its_age(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure(tmp_path)
    _mirror.write_issue(str(mirror_dir), "1955", {"number": 1955, "body": "x"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "1955"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "cached" in out
    assert "not-cached" not in out
    assert "mirror-unreadable" not in out


def test_an_issue_never_fetched_is_not_cached(monkeypatch, capsys, tmp_path) -> None:
    """Positive control for the case below: with a real (empty) mirror
    present, an unfetched number reads not-cached, not unreadable."""
    _configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "999"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not-cached" in out
    assert "cached (" not in out
    assert "mirror-unreadable" not in out


def test_a_corrupted_manifest_reports_mirror_unreadable_not_not_cached(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure(tmp_path)
    (mirror_dir / "issues").mkdir(parents=True)
    (mirror_dir / "issues" / "manifest.json").write_text("{broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "1955"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "mirror-unreadable" in out
    assert "not-cached" not in out
    assert "cached (" not in out


def test_with_no_config_at_all_it_says_not_configured_not_unreadable(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "1955"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not configured" in out.lower()
    assert "mirror-unreadable" not in out
    assert "cached" not in out.lower() or "not configured" in out.lower()


def test_a_non_numeric_number_is_refused(monkeypatch, capsys, tmp_path) -> None:
    _configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "not-a-number"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc != 0
    assert "ERROR" in out
