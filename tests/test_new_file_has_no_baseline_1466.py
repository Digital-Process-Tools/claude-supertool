"""A file that did not exist has no baseline to measure (#1466).

`paste` on a new file ran the full validator fan-out *before* the write, against
a path that was not there. Most adapters decline that with `code: "adapter"`,
which `_validator_not_checked` already routes to the `?` column -- which is why
`py-syntax` printed `? -> 0 (baseline not measured)` and was right.

Three of them answer instead, because "this file does not exist" is a finding
in their own vocabulary rather than an adapter fault: ruff calls it `E902`,
`tsc-check` calls it `TS6053`, `prettier-check` calls it `formatting`. None of
those is `code: "adapter"`, so the core accepted the fabricated `count: 1` as a
measured baseline and rendered `ruff : 1 -> 0 (-1) OK` -- the shape of a
successful result, about a file that did not exist.

The same `1` is arithmetic, not decoration. `_validator_regressed` subtracts it,
so a real finding the create introduced cancels against the invented baseline
and reports `(pre-existing -- not from this edit)` about a file whose entire
content this op wrote.

The fix is above the adapter layer, not in ruff. `_pre_existed` is already
sampled before the op for the rollback arms; when it is False there is no
pre-op state for ANY of the 36 adapters to measure, so the baseline pass is not
run at all and every row takes the `?` road that #832 built.
"""
import shutil
from pathlib import Path

import pytest

import supertool

NL = chr(10)
Q3 = chr(39) * 3

FAKE = "fake-lint"


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return supertool.dispatch("paste:@" + str(p))


def _install_fake(monkeypatch, after_count: int) -> list:
    """A validator that reports a not-found finding in its OWN vocabulary.

    `code` is deliberately not `"adapter"`: that channel is the one already
    handled, and using it here would make the test pass against the unfixed
    core. `E902` is the literal code ruff emits for a missing path.
    """
    seen = []

    def run_one(name, spec, path, doc_maybe_stale=False):
        exists = Path(path).exists()
        seen.append(exists)
        if not exists:
            return {"tool": FAKE, "file": path, "ok": False, "count": 1,
                    "errors": [{"line": 1, "col": 1, "severity": "error",
                                "code": "E902",
                                "msg": "No such file or directory (os error 2)"}],
                    "duration_ms": 1}
        errors = [{"line": 1, "col": 1, "severity": "warning", "code": "F401",
                   "msg": "unused import"} for _ in range(after_count)]
        return {"tool": FAKE, "file": path, "ok": not errors,
                "count": after_count, "errors": errors, "duration_ms": 1}

    monkeypatch.setattr(
        supertool, "_applicable_validators",
        lambda op, path: {FAKE: {"rollback_on_fail": False}})
    monkeypatch.setattr(supertool, "_applicable_formatters", lambda op, path: {})
    monkeypatch.setattr(supertool, "_validator_run_one", run_one)
    return seen


def test_a_created_file_reports_no_baseline_rather_than_an_invented_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The reported line. `1 -> 0 (-1)` is an improvement claimed against a
    file that was never there."""
    _install_fake(monkeypatch, after_count=0)
    target = tmp_path / "brand_new.py"
    out = _paste(tmp_path, target, "x = 1" + NL)
    assert "? " + chr(8594) + " 0" in out, out
    assert "baseline not measured" in out, out
    assert "(-1)" not in out, "an improvement measured against nothing:" + NL + out


def test_the_invented_baseline_is_not_rendered_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    """Narrower than the row text: the number 1 must not reach the arrow."""
    _install_fake(monkeypatch, after_count=0)
    target = tmp_path / "brand_new2.py"
    out = _paste(tmp_path, target, "x = 1" + NL)
    assert "1 " + chr(8594) + " 0" not in out, out


def test_a_finding_the_create_introduced_is_not_excused_as_pre_existing(
    tmp_path: Path, monkeypatch
) -> None:
    """The half that is arithmetic rather than prose.

    Baseline 1 (invented) against a real post-write finding of 1 gives equal
    counts and equal `ok`, so `_validator_regressed` returns False and the row
    reads `(pre-existing -- not from this edit)` about a file this op created
    whole. Nothing about that file pre-existed anything.
    """
    _install_fake(monkeypatch, after_count=1)
    target = tmp_path / "brand_new3.py"
    out = _paste(tmp_path, target, "import os" + NL)
    assert "pre-existing" not in out, out
    assert "? " + chr(8594) + " 1" in out, out


def test_the_baseline_pass_does_not_run_on_a_path_that_is_not_there(
    tmp_path: Path, monkeypatch
) -> None:
    """Asking 36 adapters to measure a nonexistent file is 36 spawns whose only
    honest answer is `?`. The adapter is called once, after the write."""
    seen = _install_fake(monkeypatch, after_count=0)
    _paste(tmp_path, tmp_path / "brand_new4.py", "x = 1" + NL)
    assert seen == [True], "the baseline pass ran against a missing file: " + repr(seen)


def test_an_overwrite_still_measures_its_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    """The regression this fix could most easily cause. A file that existed has
    a real pre-op state, and dropping it would turn every edit into `?`."""
    target = tmp_path / "already.py"
    target.write_text("import os" + NL, encoding="utf-8")
    seen = _install_fake(monkeypatch, after_count=0)
    out = _paste(tmp_path, target, "x = 1" + NL)
    assert seen == [True, True], repr(seen)
    assert "baseline not measured" not in out, out


def test_a_rollback_validator_now_reaches_a_created_file_it_flagged(
    tmp_path: Path, monkeypatch
) -> None:
    """The behaviour change this fix carries, said out loud.

    The invented baseline did not only misprint a row -- for the three adapters
    that produced one it also *suppressed* the rollback, because `1` against a
    real post-write `1` is equal counts and equal `ok`. Removing it restores
    the arm, so a `rollback_on_fail` validator that flags a file this op
    created now unlinks it, exactly as the 33 adapters that already declined
    the baseline always did.

    This is reachable through no shipped registration -- see the audit below --
    but it is the arm the change moves, so it is pinned rather than argued.
    """
    _install_fake(monkeypatch, after_count=1)
    monkeypatch.setattr(
        supertool, "_applicable_validators",
        lambda op, path: {FAKE: {"rollback_on_fail": True}})
    target = tmp_path / "flagged_create.py"
    out = _paste(tmp_path, target, "import os" + NL)
    assert not target.exists(), "the flagged create was left behind:" + NL + out
    assert "rolled back" in out, out


def test_the_disclosure_1320_built_for_ruff_was_suppressed_by_the_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    """`[left on disk]` names ruff by name in its own docstring as the case it
    exists for -- advisory, never rolls back, so the file stands and the caller
    has to be told. The invented baseline cancelled ruff's finding against
    itself, so for ruff the disclosure could not fire. It fires now."""
    _install_fake(monkeypatch, after_count=1)
    target = tmp_path / "advisory_create.py"
    out = _paste(tmp_path, target, "import os" + NL)
    assert target.exists(), out
    assert "[left on disk]" in out, out


def test_no_shipped_rollback_validator_ever_had_a_measured_baseline() -> None:
    """Why the arm above moves nothing anyone ships, checked rather than said.

    A registration only changes behaviour here if it is BOTH `rollback_on_fail`
    AND answers a missing file in its own vocabulary rather than with
    `code: "adapter"`. Every `rollback_on_fail` validator in both config files
    is in the second group -- they decline or skip -- so their baseline was
    already `None` before this change and the rollback gate did not move for
    any of them. The three that answered (ruff, tsc-check, prettier-check) set
    `rollback_on_fail` nowhere, by the deliberate choice `ruff.py`'s module
    docstring argues at length.

    A class guard, not a pin of the fix: it passes on both sides of the change.
    It fails the day someone turns rollback on for one of the three, which is
    the moment the paragraph above stops being theoretical.
    """
    import json
    import re

    root = Path(supertool.__file__).resolve().parent
    comment = re.compile("^" + chr(92) + "s*//.*$", re.M)
    answers_in_its_own_vocabulary = {"ruff", "tsc-check", "tsc-check-tsx",
                                     "prettier-check"}
    for name in (".supertool.json", ".supertool.example.json"):
        raw = comment.sub("", (root / name).read_text(encoding="utf-8"))
        validators = json.loads(raw).get("validators") or {}
        rolls_back = {k for k, v in validators.items() if v.get("rollback_on_fail")}
        overlap = rolls_back & answers_in_its_own_vocabulary
        assert not overlap, (
            name + " gives rollback_on_fail to a validator that reports a "
            "missing file as a finding rather than as an adapter fault: "
            + repr(sorted(overlap)))


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not on PATH")
def test_ruff_really_does_report_a_missing_file_as_a_measured_finding(
    tmp_path: Path,
) -> None:
    """Why the decline had to go in the core rather than in the adapter.

    The real ruff adapter, handed a path that is not there, returns
    `ok: false, count: 1, code: "E902"`. `E902` is ruff's IO error and not the
    `adapter` code SCHEMA.md reserves for "I could not answer", so every
    existing guard reads this as a measurement of the file: the row rendered
    `1 -> 0 (-1)` and `_validator_baseline` handed the `1` to the arithmetic.

    Pinned rather than fixed in ruff: the reply is not wrong on its own terms
    -- ruff was asked about a path and said the path is missing -- and the same
    sentence is true of `tsc-check` (TS6053) and `prettier-check`. Whoever
    changes this adapter should know the core no longer asks it this question.
    """
    import json
    import subprocess
    import sys

    adapter = Path(supertool.__file__).resolve().parent / "validators" / "ruff" / "ruff.py"
    missing = tmp_path / "not_here_1466.py"
    r = subprocess.run([sys.executable, str(adapter), str(missing)],
                       capture_output=True, text=True, timeout=120)
    reply = json.loads((r.stdout or "").strip().splitlines()[-1])
    assert reply.get("count") == 1, reply
    assert reply["errors"][0].get("code") == "E902", reply
    assert supertool._validator_no_verdict(reply) is None, (
        "if this ever becomes a non-verdict the core gate is belt-and-braces, "
        "not the only thing standing between E902 and the arithmetic")
    assert supertool._validator_baseline(reply) is not None, reply
