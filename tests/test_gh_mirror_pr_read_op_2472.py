"""`gh-mirror:pr:N` -- the read op extended to PRs (#2472).

Same four-render contract as `tests/test_gh_mirror_read_op_1955.py`'s
`issue` mode: `cached (age)`, `not-cached`, `mirror-unreadable`, and "not
configured at all". TDD: run RED against a `mirror.py` that only accepts
`issue` as its first argument, then GREEN once `pr` is a second valid kind.
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
_spec = importlib.util.spec_from_file_location("github_mirror_pr_op_2472", PRESET_PATH)
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


def test_a_cached_pr_reports_its_age(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure(tmp_path)
    _mirror.write_pr(str(mirror_dir), "2472", {"number": 2472, "body": "x"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "2472"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "cached" in out
    assert "not-cached" not in out
    assert "mirror-unreadable" not in out


def test_a_pr_never_fetched_is_not_cached(monkeypatch, capsys, tmp_path) -> None:
    """Positive control: with a real (empty) mirror present, an unfetched
    number reads not-cached, not unreadable."""
    _configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "999"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not-cached" in out
    assert "cached (" not in out
    assert "mirror-unreadable" not in out


def test_an_issue_and_a_pr_with_the_same_number_do_not_collide_in_the_read_op(monkeypatch, capsys, tmp_path) -> None:
    """The design constraint from `_mirror.py`'s docstring, exercised
    through the read op rather than the storage layer directly."""
    mirror_dir = _configure(tmp_path)
    _mirror.write_issue(str(mirror_dir), "2472", {"number": 2472, "body": "issue"})
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "2472"])
    rc = mirror_op.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "not-cached" in out, "the PR mirror must not see the issue's entry"

    monkeypatch.setattr(sys, "argv", ["mirror.py", "issue", "2472"])
    rc = mirror_op.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "cached" in out


def test_a_corrupted_pr_manifest_reports_mirror_unreadable_not_not_cached(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure(tmp_path)
    (mirror_dir / "prs").mkdir(parents=True)
    (mirror_dir / "prs" / "manifest.json").write_text("{broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "2472"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "mirror-unreadable" in out
    assert "not-cached" not in out
    assert "cached (" not in out


def test_with_no_config_at_all_pr_says_not_configured_not_unreadable(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "2472"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not configured" in out.lower()
    assert "mirror-unreadable" not in out
    assert "cached" not in out.lower() or "not configured" in out.lower()


def test_a_non_numeric_pr_number_is_refused(monkeypatch, capsys, tmp_path) -> None:
    _configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "pr", "not-a-number"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc != 0
    assert "ERROR" in out


def test_an_unknown_kind_is_refused_naming_both_valid_ones(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mirror.py", "milestone", "1"])

    rc = mirror_op.main()
    out = capsys.readouterr().out

    assert rc != 0
    assert "ERROR" in out
    assert "issue" in out and "pr" in out
