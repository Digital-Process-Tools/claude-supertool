"""`classify` end-to-end (#2046): the op's own orchestration -- which stage
ran, the short-circuit direction, and `file://`-only path resolution.

`model.classify` is monkeypatched module-wide in every test that reaches
stage 2, so nothing here spawns a real `claude -p` process either. What is
under test is `check.run()`'s wiring: does a scanner hit actually skip the
spawn, does a clean scan actually reach it, and does `could-not-classify`
survive the trip to the printed report without being read as `safe`.
"""
from __future__ import annotations

import pytest

from _preset_loader import load_preset_module

check = load_preset_module("classify", "check", prefix="cls_")


def _stub_model(monkeypatch, state, axes=None, reason=""):
    """Replace the module `check` already imported as `model` -- patching
    the name check.model.classify (not the classify module fresh-imported
    elsewhere) is what actually intercepts the call `check.run` makes."""
    calls = []

    def _fake(text, **kwargs):
        calls.append(text)
        return check.model.Verdict(state, axes or [], reason)
    monkeypatch.setattr(check.model, "classify", _fake)
    return calls


# --- the short-circuit direction: scanner hit skips the spawn ------------

def test_a_scanner_hit_produces_suspect_and_never_calls_the_model(monkeypatch) -> None:
    calls = _stub_model(monkeypatch, "safe")  # would answer safe if reached
    report = check.run("token: ghp_abcdefghijklmnopqrstuvwxyz012345")
    assert "verdict: suspect" in report
    assert "model: not-run" in report
    assert calls == [], "the model must not run once the scanner has matched"


def test_clean_text_reaches_the_model_stage(monkeypatch) -> None:
    calls = _stub_model(monkeypatch, "safe")
    report = check.run("Deploy finished, all green.")
    assert "verdict: safe" in report
    assert calls == ["Deploy finished, all green."]


# --- could-not-classify never renders as safe, end to end -----------------

def test_a_could_not_classify_model_result_is_not_reported_as_safe(monkeypatch) -> None:
    _stub_model(monkeypatch, "could-not-classify", reason="spawn timed out after 45s")
    report = check.run("some ordinary text with nothing scanner-detectable")
    assert "verdict: could-not-classify" in report
    assert "verdict: safe" not in report
    assert "spawn timed out after 45s" in report


def test_a_suspect_model_result_names_its_axes(monkeypatch) -> None:
    _stub_model(monkeypatch, "suspect", axes=["role-persona"])
    report = check.run("ordinary-looking text")
    assert "verdict: suspect" in report
    assert "role-persona" in report


# --- file:// only, no bare-path arm (#2039 is the precedent) --------------

def test_file_scheme_reads_the_file(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "msg.txt").write_text("hello from disk", encoding="utf-8")
    assert check.resolve_text("file://msg.txt") == "hello from disk"


def test_a_bare_path_that_happens_to_exist_is_treated_as_literal_text(
        monkeypatch, tmp_path) -> None:
    """The `#2039` precedent this op is built against: no bare-path arm.
    A string that looks like a path but carries no `file://` prefix is
    classified AS TEXT, never read off disk."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "msg.txt").write_text("file contents, not the literal string",
                                       encoding="utf-8")
    assert check.resolve_text("msg.txt") == "msg.txt"


def test_a_file_scheme_path_escaping_cwd_is_refused(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        check.resolve_text("file://../../etc/passwd")
    assert exc.value.code == 2
    assert "escapes the working directory" in capsys.readouterr().err


def test_a_file_scheme_path_that_does_not_exist_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        check.resolve_text("file://does-not-exist.txt")
    assert exc.value.code == 2


def test_an_absolute_file_scheme_path_outside_cwd_is_refused(monkeypatch, tmp_path) -> None:
    """The credential-exfil shape #2039 named directly: an absolute path
    pointed straight at a file outside the tree."""
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / "some_other_dir_credential"
    outside.write_text("secret", encoding="utf-8")
    try:
        with pytest.raises(SystemExit) as exc:
            check.resolve_text(f"file://{outside}")
        assert exc.value.code == 2
    finally:
        outside.unlink(missing_ok=True)
