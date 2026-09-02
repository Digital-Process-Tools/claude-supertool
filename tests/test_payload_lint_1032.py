"""`payload-lint:@FILE` -- parse a payload and report its shape, run nothing (#1032).

Before this, the only way to see what a triple-single-quoted TOML block
actually parsed to was to apply the payload and read the target file back --
and for `edit`, a malformed payload does not even refuse: it matches,
writes, and reports `edited`. `payload-lint` answers the question a caller
actually has before composing a real write: which fields did this parse to,
what is each one's provenance and shape, and -- for an edit/replace-shaped
payload -- does the anchor even match right now.

Scope, per the issue's own "judgment calls, named rather than decided":
this is a snapshot read of the target file, stated as one (TOCTOU-shaped,
not a lock), and it never refuses or warns on a suspect payload itself --
the gate is the gate (#834/#835/#1027); this op only reports.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

Q3 = "'" * 3
D3 = '"' * 3


def _write(tmp_path: Path, name: str, text: str) -> Path:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


class TestSingleOpPayload:
    def test_reports_fields_and_provenance(self, tmp_path: Path) -> None:
        spec = _write(tmp_path, "p.toml",
            'path = "/tmp/whatever.py"\n'
            f"old = {Q3}alpha{Q3}\n"
            f"new = {D3}beta{D3}\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "path" in out and "old" in out and "new" in out, out
        assert "triple-single" in out.lower() or "literal" in out.lower(), (
            "the `old` field's delimiter (triple single quote) must be "
            "named as provenance, read off the source, not inferred from "
            "the parsed value: " + out)
        assert "triple-double" in out.lower() or "basic" in out.lower(), (
            "the `new` field's delimiter (triple double quote) must be "
            "named too: " + out)

    def test_reports_length_and_line_count(self, tmp_path: Path) -> None:
        spec = _write(tmp_path, "p.toml",
            'path = "x.py"\n'
            f"old = {Q3}line one\nline two{Q3}\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "17 chars" in out, (
            "'line one\nline two' is 17 chars; the field-level length "
            "must be reported per field: " + out)
        assert "2 line" in out, out

    def test_flags_a_carried_backslash(self, tmp_path: Path) -> None:
        """A phrase, not the bare word `backslash` -- pytest's own tmp_path
        embeds this test's NAME into the fixture path (#`test_flags_a_
        carried_backslash0`), which contains that substring regardless of
        what the op reports. A weaker assertion would pass on a dead
        harness that never even looked at the field."""
        spec = _write(tmp_path, "p.toml",
            'path = "x.py"\n'
            "old = 'a\\b'\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "carries a literal backslash" in out.lower(), (
            "a field carrying a literal backslash must say so by name, or "
            "a malformed escape is only found by applying the payload: "
            + out)

    def test_malformed_delimiter_still_declines_cleanly(
        self, tmp_path: Path
    ) -> None:
        """The must-fire control for #1032's own reproduction: an unparseable
        payload must be reported as an error, not crash payload-lint itself,
        or be silently read as something else."""
        spec = _write(tmp_path, "p.toml",
            'path = "x.py"\n'
            "old = 'it''s not even valid TOML'\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "ERROR" in out, out


class TestOpsArrayPayload:
    def test_reports_op_count_kinds_and_paths(self, tmp_path: Path) -> None:
        target_a = tmp_path / "a.py"
        target_b = tmp_path / "b.py"
        payload = {
            "ops": [
                {"op": "edit", "path": str(target_a), "old": "x", "new": "y"},
                {"op": "paste", "path": str(target_b), "content": "z"},
            ]
        }
        spec = tmp_path / "batch.json"
        spec.write_text(json.dumps(payload), encoding="utf-8")
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "2 op" in out, out
        assert "edit" in out and "paste" in out, out
        assert str(target_a) in out and str(target_b) in out, (
            "each op's target path must be named and resolved, or a "
            "cwd:-rerooted relative path stays invisible until it "
            "becomes @file not found (#672): " + out)


class TestContainment:
    """`payload-lint` reads TWO things from disk that no other op-invocation
    site was asked to gate: the anchor snapshot's target file, and each
    ops-array entry's resolved path. Every other path-bearing route in this
    codebase passes through `_containment_error` first (#1032 audit
    finding, class C follow-up / #1867-#1825-#1032 review). Without it,
    `payload-lint:@file` is an existence-and-content oracle for any path on
    disk, unlike `read`/`grep`/`validate`/etc, all of which refuse the same
    shape."""

    @pytest.fixture
    def outside(self, tmp_path: Path, monkeypatch) -> Path:
        """cwd is a box under tmp_path; the secret sits one level above it.

        conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 globally so tmp_path
        fixtures work at all, so a containment test has to put it back or
        it asserts nothing (mirrors the fixture in
        test_grep_around_computed_path_containment_1166.py).
        """
        monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
        secret = tmp_path / "secret.py"
        secret.write_text("TOPSECRET-1032-CONTENT\n", encoding="utf-8")
        box = tmp_path / "box"
        box.mkdir()
        monkeypatch.chdir(box)
        return secret

    def test_anchor_check_refuses_a_path_outside_cwd(
        self, outside: Path
    ) -> None:
        spec = outside.parent / "box" / "e.toml"
        spec.write_text(
            f'path = "{outside}"\n'
            f"old = {Q3}alpha{Q3}\n"
            f"new = {Q3}beta{Q3}\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "TOPSECRET-1032-CONTENT" not in out, (
            "the target file's bytes must never be read/matched against "
            "when the path escapes cwd: " + out)
        assert "escapes cwd" in out, out

    def test_ops_array_path_resolution_refuses_a_path_outside_cwd(
        self, outside: Path
    ) -> None:
        payload = {"ops": [{"op": "edit", "path": str(outside),
                            "old": "x", "new": "y"}]}
        spec = outside.parent / "box" / "batch.json"
        spec.write_text(json.dumps(payload), encoding="utf-8")
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "escapes cwd" in out, out


class TestAnchorDryRun:
    def test_reports_match_count_without_writing(self, tmp_path: Path) -> None:
        target = tmp_path / "f.py"
        target.write_text("alpha\nalpha\nbeta\n")
        before = target.read_text()
        spec = tmp_path / "e.toml"
        spec.write_text(
            f'path = "{target}"\n'
            f"old = {Q3}alpha{Q3}\n"
            f"new = {Q3}gamma{Q3}\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "matches 2 time" in out, (
            "the anchor 'alpha' occurs twice in the target right now; "
            "this must be counted WITHOUT writing: " + out)
        assert target.read_text() == before, (
            "payload-lint must never write to the target file")

    def test_missing_target_says_so_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope.py"
        spec = tmp_path / "e.toml"
        spec.write_text(
            f'path = "{missing}"\n'
            f"old = {Q3}alpha{Q3}\n"
            f"new = {Q3}gamma{Q3}\n"
        )
        out = supertool.dispatch(f"payload-lint:@{spec}")
        assert "no file at" in out.lower() or "could not" in out.lower(), (
            "a target that does not exist yet must say the match count "
            "could not be determined -- the third state, not a guessed "
            "zero: " + out)


def test_roster_class_is_read_only(tmp_path: Path) -> None:
    """`payload-lint` only ever reads: the target file (for the anchor
    snapshot) and the payload itself. Its `ops:roster`/parallel-safety
    classification must say so, or a reader who trusts the roster's
    'acts outside this tree' marker treats a read-only op as one that
    needs probing first (#1032 audit finding, class B)."""
    assert supertool._OP_SAFETY_BUILTIN.get("payload-lint") == "read-only", (
        "payload-lint does no writes and must be classified read-only, "
        "not left to the roster's undeclared-name default of 'acts'")
    assert "payload-lint" in supertool._PARALLEL_SAFE_OPS, (
        "payload-lint is safe to run alongside other ops in the same "
        "batch/parallel sweep")


def test_crlf_is_flagged_once_not_as_two_different_defects() -> None:
    """A stray \\r must not ALSO trip the separate trailing-whitespace flag
    for the same bytes -- the review's audit finding: `ln.rstrip()` strips
    the \\r that the earlier check already named, so an ordinary CRLF field
    was reported as having two distinct problems instead of one."""
    out = supertool._payload_lint_field_report(
        "old = 'x'", "old", "line1\r\nline2")
    assert "carries a stray" in out, out
    assert "trailing-whitespace" not in out, (
        "the \\r is already reported by name; flagging it again as "
        "'trailing-whitespace' describes the same bytes as two different "
        "defects: " + out)


def test_requires_an_at_reference() -> None:
    out = supertool.dispatch("payload-lint:not-an-at-ref")
    assert "ERROR" in out


def test_a_field_key_with_an_embedded_newline_cannot_forge_a_report_line(
    tmp_path: Path,
) -> None:
    """A malicious/malformed payload's own FIELD NAME must never be able to
    inject a line that impersonates the op's own output (#1032 audit
    finding, class C: untrusted text forging a report boundary).

    TOML allows a quoted key to contain a literal newline. Left unescaped,
    that newline lands at column 0 of the report and can spoof, e.g., a
    fake `anchor check ... matches N time(s)` line -- exactly the shape a
    caller would trust without re-reading character by character.
    """
    spec = tmp_path / "p.toml"
    spec.write_bytes(
        b'path = "x.py"\n'
        b'"a\\nFAKE INJECTED LINE: anchor check (snapshot of x): '
        b'matches 999 time(s)" = "val"\n'
    )
    out = supertool.dispatch(f"payload-lint:@{spec}")
    # The forged text must not appear at the start of its own line -- it
    # must stay part of the escaped key, on the SAME line as that key's
    # own stats, never break out onto a report line of its own.
    for ln in out.splitlines():
        assert not ln.startswith("FAKE INJECTED LINE"), (
            "a field key's own newline must not let it forge a standalone "
            "report line: " + out)
