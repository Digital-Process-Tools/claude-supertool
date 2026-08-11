"""A refused create that nothing rolled back is disclosed as still on disk (#1320).

The issue's stated mechanism is wrong and re-deriving it is half the work: it
says a create "has no baseline, so the comparison cannot fire". #1088 already
built the create arm — `_rollback_action(False, None) == "unlink"` — and a
broken new `.json` really is removed. The only discriminator is per-validator
`rollback_on_fail`, which `changelog-fragment` sets to `false`, deliberately,
along with `ruff`, `lsp-diag` and `git-status`.

So the fix is not "implement create rollback" and it is not flipping those
flags — reverting an author's edit over a lint nit is the loud-bug-for-quiet-bug
trade this repo rules out. It is disclosure, and it is specific to a create: on
an edit the leftover bytes are in a file the caller already owned and the
`edited` line above is true, whereas a create's leftover is a path that did not
exist, that the receipt announced with `created`, that a validator then refused,
and that now reddens `git status`, test collection and the next read.

Would these pass if the code did nothing? No. Each asserts a marker and a
`[result]` clause that do not exist, or the absence of them on the four
boundaries that would make the disclosure worse than the bug (a clean create, a
skip, an overwrite, and a create a rollback already removed).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import supertool

NL = chr(10)
Q3 = chr(39) * 3
REPO = Path(__file__).resolve().parents[1]
SUPERTOOL = REPO / "supertool.py"

BAD = "BAD" + NL
GOOD = "fine" + NL

FINDING = {"tool": "advisory", "ok": False, "count": 1,
           "errors": [{"line": 1, "code": "shape", "msg": "refused by advisory"}]}
CLEAN = {"tool": "advisory", "ok": True, "count": 0, "errors": []}
SKIPPED = {"tool": "advisory", "skipped": "no parser installed"}


def _install(monkeypatch, *, rollback: bool = False, verdict: str = "content") -> None:
    """One validator whose verdict is a function of the bytes on disk.

    Derived from the file rather than from a call counter so the baseline pass
    (pre-op, over a path that may not exist) and the after pass are answered by
    the same rule — a fake that returned a finding every time would make
    `_validator_regressed` false on an overwrite for a reason unrelated to what
    is under test.
    """
    monkeypatch.setattr(
        supertool, "_applicable_validators",
        lambda op, path: {"advisory": {"rollback_on_fail": rollback}})

    def _batch(applicable, path, doc_maybe_stale=False):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return {}
        if verdict == "skipped":
            return {"advisory": dict(SKIPPED)}
        return {"advisory": dict(FINDING if "BAD" in text else CLEAN)}

    monkeypatch.setattr(supertool, "_validators_run_batch", _batch)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = ("path = " + json.dumps(str(target)) + NL
            + "content = " + Q3 + content + Q3 + NL)
    payload = tmp_path / "p.toml"
    payload.write_text(body, encoding="utf-8")
    return supertool.dispatch("paste:@" + str(payload))


def _result_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("[result]")]
    assert lines, "no [result] line:" + NL + out
    return lines[-1]


def test_a_refused_create_no_validator_rolled_back_is_disclosed(tmp_path, monkeypatch):
    """The issue, asserted against the receipt rather than against the flag."""
    _install(monkeypatch)
    target = tmp_path / "sub" / "new.txt"
    out = _paste(tmp_path, target, BAD)
    assert target.exists(), "premise gone: an advisory validator must not unlink"
    assert "[left on disk]" in out, out
    assert str(target) in out, out


def test_the_result_line_carries_it(tmp_path, monkeypatch):
    """The part that survives a pipe. `1 op run, 1 write` is what a clean create
    prints too, so the footer could not tell them apart at all."""
    _install(monkeypatch)
    target = tmp_path / "new.txt"
    out = _paste(tmp_path, target, BAD)
    line = _result_line(out)
    assert "left on disk" in line, line
    assert "1 write" in line, line


def test_the_disclosure_quotes_the_created_line_back(tmp_path, monkeypatch):
    """Same rule `_retraction_line` follows (#952): a filter that caught the
    claim has to catch the correction, or the two never meet in one read."""
    _install(monkeypatch)
    target = tmp_path / "new.txt"
    out = _paste(tmp_path, target, BAD)
    created = [ln for ln in out.splitlines() if ln.startswith("created ")]
    assert created, out
    assert created[0] in out.split("[left on disk]")[1], out


def test_a_clean_create_says_nothing(tmp_path, monkeypatch):
    """The boundary that would make this noise instead of a signal."""
    _install(monkeypatch)
    target = tmp_path / "new.txt"
    out = _paste(tmp_path, target, GOOD)
    assert target.exists(), out
    assert "left on disk" not in out, out


def test_a_skipped_validator_is_not_a_finding(tmp_path, monkeypatch):
    """A checker that declined refused nothing. Announcing a refused artefact
    over a skip is the absence-read-as-presence defect pointed the other way."""
    _install(monkeypatch, verdict="skipped")
    target = tmp_path / "new.txt"
    out = _paste(tmp_path, target, BAD)
    assert target.exists(), out
    assert "left on disk" not in out, out


def test_an_overwrite_is_not_reported_as_left_on_disk(tmp_path, monkeypatch):
    """Scoped to a create on purpose. An overwrite left the caller's own file
    modified, which the `edited` line above already states truthfully; a path
    that did not exist until this op is the one nothing else announces."""
    _install(monkeypatch)
    target = tmp_path / "existing.txt"
    target.write_text(GOOD, encoding="utf-8")
    out = _paste(tmp_path, target, BAD)
    assert target.read_text(encoding="utf-8") == BAD
    assert "left on disk" not in out, out


def test_a_rolled_back_create_does_not_also_say_left_on_disk(tmp_path, monkeypatch):
    """The two messages are mutually exclusive and would contradict each other.
    Exactly one undo-or-disclosure per op, whichever was reached (#1088)."""
    _install(monkeypatch, rollback=True)
    target = tmp_path / "new.txt"
    out = _paste(tmp_path, target, BAD)
    assert not target.exists(), out
    assert "[rolled back]" in out, out
    assert "left on disk" not in out, out


def test_result_line_renders_the_clause_from_its_own_counter():
    """Read at the footer, where every other state of this receipt is decided
    from a counter rather than from the prose above it."""
    line = supertool._result_line(1, 1, left_on_disk=1)
    assert "left on disk" in line, line
    assert "left on disk" not in supertool._result_line(1, 1)


def test_a_paste_of_a_bad_fragment_says_the_file_is_still_there(tmp_path):
    """End to end, through the CLI, over the real `changelog-fragment` — the
    validator the issue was reported against, with its real
    `rollback_on_fail: false`."""
    (tmp_path / "changelog.d").mkdir(parents=True, exist_ok=True)
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / ".github" / "scripts" / "assemble_changelog.py",
                 scripts / "assemble_changelog.py")
    target = tmp_path / "changelog.d" / "9999.fixed.md"
    payload = ("path = " + json.dumps(str(target)) + NL
               + "content = " + Q3 + "## heading not a bullet" + NL + Q3 + NL)
    proc = subprocess.run([sys.executable, str(SUPERTOOL), "paste:@-"],
                          input=payload, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(REPO))
    receipt = proc.stdout + proc.stderr
    assert "changelog-fragment" in receipt, receipt
    assert target.exists(), receipt
    assert "[left on disk]" in receipt, receipt
    assert "left on disk" in _result_line(receipt), receipt
