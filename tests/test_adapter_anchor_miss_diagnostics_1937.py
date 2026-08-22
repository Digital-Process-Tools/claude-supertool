"""When the anchor matches nothing, the failure has to say what it saw
(#1937, third CI round).

Three rounds of CI reds on the same two tests (test_ruby_check.py's real-
ruby tests) produced two different, both-partly-right diagnoses -- Windows
spelling, then symlink canonicalisation -- and neither, on its own, closed
the case. What was missing throughout: when `anchor()` matches nothing but
the tool DID emit a diagnostic, nothing in the adapter's own output named
the two strings that differ. A maintainer reading CI logs had to guess the
transform from an assertion failure and a raw exit code.

`unanchored_path_hint` extracts a best-effort path-looking prefix from the
tool's raw output -- the SAME non-greedy shape #1934 removed from the actual
location-extraction path, but used here for NOTHING but a diagnostic
string. It never assigns a line, a column or a `code` that reads as a
verdict; it only feeds `anchor_miss_message`, which -- when that hint looks
like a genuinely different path than the one the adapter invoked -- prefixes
the existing fallback message with both strings side by side, so the next
red CI run prints the answer instead of another hypothesis to test.

This is NOT a location-finding mechanism and must never become one: unlike
`path_anchor.anchor()`, `unanchored_path_hint`'s result never reaches
`errors[]`'s `line`/`col`/`code` fields, only free text in `msg` on the
`code: "adapter"`/`code: "lint"` non-verdict rows that already exist for an
anchor miss. A forged filename can still make `unanchored_path_hint` return
attacker-chosen text, and that is fine: it can only ever become part of a
message a human reads, never a location a caller trusts.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

VALIDATORS = Path(__file__).parent.parent / "validators"


def _load_common(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_anchor_miss_1937", VALIDATORS / "common" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_adapter(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace(chr(45), chr(95))}_anchor_miss_1937",
        VALIDATORS / name / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


path_anchor = _load_common("path_anchor")
xmllint = _load_adapter("xmllint")
ruby_check = _load_adapter("ruby-check")
gofmt_check = _load_adapter("gofmt-check")
hadolint = _load_adapter("hadolint")
actionlint = _load_adapter("actionlint")


class _FakeProc:
    """Just enough of `CompletedProcess` for an adapter's parse path --
    mirrors `tests/test_validators_splitlines_1486.py`'s own fixture."""

    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _drive_main(mod, monkeypatch, capsys, argv, proc):
    monkeypatch.setattr(mod.sys, "argv", argv)
    if hasattr(mod, "shutil"):
        monkeypatch.setattr(mod.shutil, "which", lambda *a, **k: "/fake/bin/tool")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: proc)
    capsys.readouterr()
    mod.main()
    out = capsys.readouterr().out.strip()
    return json.loads(out.split("\n")[-1])


# ---------------------------------------------------------------------------
# End to end: the adapter's own emitted `msg` names both strings, not just
# `unanchored_path_hint` in isolation.
# ---------------------------------------------------------------------------

def test_xmllint_anchor_miss_names_both_paths(monkeypatch, capsys):
    out = _drive_main(
        xmllint, monkeypatch, capsys, ["xmllint.py", "/tmp/link/a.xml"],
        _FakeProc(1, "", "/private/tmp/real/a.xml:2: parser error : mismatch"))
    assert out["ok"] is False, out
    msg = out["errors"][0]["msg"]
    assert "/tmp/link/a.xml" in msg, msg
    assert "/private/tmp/real/a.xml" in msg, msg


def test_ruby_check_anchor_miss_names_both_paths(monkeypatch, capsys):
    out = _drive_main(
        ruby_check, monkeypatch, capsys, ["ruby-check.py", "/tmp/link/bad.rb"],
        _FakeProc(1, "", "/private/tmp/real/bad.rb:3: syntax error, unexpected end-of-input"))
    assert out["ok"] is False, out
    msg = out["errors"][0]["msg"]
    assert "/tmp/link/bad.rb" in msg, msg
    assert "/private/tmp/real/bad.rb" in msg, msg


def test_gofmt_check_anchor_miss_names_both_paths(monkeypatch, capsys):
    out = _drive_main(
        gofmt_check, monkeypatch, capsys, ["gofmt-check.py", "/tmp/link/bad.go"],
        _FakeProc(2, "", "/private/tmp/real/bad.go:3:12: expected close paren, found brace"))
    assert out["ok"] is False, out
    msg = out["errors"][0]["msg"]
    assert "/tmp/link/bad.go" in msg, msg
    assert "/private/tmp/real/bad.go" in msg, msg


def test_hadolint_anchor_miss_names_both_paths(monkeypatch, capsys):
    out = _drive_main(
        hadolint, monkeypatch, capsys, ["hadolint.py", "/tmp/link/Dockerfile"],
        _FakeProc(1, "/private/tmp/real/Dockerfile:5 DL3007 warning: using latest", ""))
    assert out["ok"] is False, out
    msg = out["errors"][0]["msg"]
    assert "/tmp/link/Dockerfile" in msg, msg
    assert "/private/tmp/real/Dockerfile" in msg, msg


def test_actionlint_anchor_miss_names_both_paths(monkeypatch, capsys):
    out = _drive_main(
        actionlint, monkeypatch, capsys, ["actionlint.py", "/tmp/link/deploy.yml"],
        _FakeProc(1, "/private/tmp/real/deploy.yml:7:15: specifying action \"bogus\"", ""))
    assert out["ok"] is False, out
    msg = out["errors"][0]["msg"]
    assert "/tmp/link/deploy.yml" in msg, msg
    assert "/private/tmp/real/deploy.yml" in msg, msg


def test_xmllint_anchor_hit_does_not_add_a_miss_message(monkeypatch, capsys):
    """The control: when the anchor DOES match, none of this machinery
    fires and the message is the ordinary located finding, not a
    manufactured comparison."""
    out = _drive_main(
        xmllint, monkeypatch, capsys, ["xmllint.py", "/tmp/a.xml"],
        _FakeProc(1, "", "/tmp/a.xml:2: parser error : mismatch"))
    assert out["ok"] is False, out
    assert out["errors"][0]["line"] == 2, out
    assert "anchor matched no accepted spelling" not in out["errors"][0]["msg"], out


# ---------------------------------------------------------------------------
# unanchored_path_hint -- pure, diagnostic only, never a location.
# ---------------------------------------------------------------------------

def test_unanchored_path_hint_extracts_the_leading_path_looking_prefix():
    out = "/some/other/path.rb:7: syntax error, unexpected end-of-input"
    assert path_anchor.unanchored_path_hint(out) == "/some/other/path.rb"


def test_unanchored_path_hint_returns_none_when_nothing_looks_like_a_path():
    assert path_anchor.unanchored_path_hint("no colon-digit shape here at all") is None


def test_unanchored_path_hint_returns_none_on_empty_output():
    assert path_anchor.unanchored_path_hint("") is None


def test_unanchored_path_hint_reads_the_first_matching_line_not_the_last():
    out = "not a path line\n/real/hint.go:3:12: expected declaration\nmore text:9: noise"
    assert path_anchor.unanchored_path_hint(out) == "/real/hint.go"


# ---------------------------------------------------------------------------
# anchor_miss_message -- what actually reaches a CI log.
# ---------------------------------------------------------------------------

def test_anchor_miss_message_names_both_paths_when_they_differ():
    msg = path_anchor.anchor_miss_message(
        "/tmp/link/bad.rb",
        "/private/tmp/real/bad.rb:3: syntax error, unexpected end-of-input",
        "fallback text")
    assert "/tmp/link/bad.rb" in msg, msg
    assert "/private/tmp/real/bad.rb" in msg, msg
    assert "fallback text" in msg, msg


def test_anchor_miss_message_falls_back_unchanged_when_the_hint_matches_the_invoked_path():
    """No mismatch to report -- the anchor missed for some other reason
    (e.g. genuinely no located diagnostic in the output), so the message
    stays exactly the fallback rather than claiming a comparison that found
    nothing to compare."""
    msg = path_anchor.anchor_miss_message(
        "/tmp/a.rb", "/tmp/a.rb:3: syntax error", "fallback text")
    assert msg == "fallback text", msg


def test_anchor_miss_message_falls_back_unchanged_when_output_has_no_path_shape():
    msg = path_anchor.anchor_miss_message(
        "/tmp/a.rb", "some unrelated crash text with no path shape", "fallback text")
    assert msg == "fallback text", msg


def test_anchor_miss_message_falls_back_unchanged_on_empty_output():
    msg = path_anchor.anchor_miss_message("/tmp/a.rb", "", "fallback text")
    assert msg == "fallback text", msg
