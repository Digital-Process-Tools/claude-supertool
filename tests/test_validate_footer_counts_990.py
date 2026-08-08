"""`validate:` ends on a whole-run verdict, not on whichever file sorted last (#990).

#979 gave `validate:` a `[result]` footer **only** when a checker declined. On
the clean path the last line of the run was the last file's own row — so the
final line of a multi-file run was a statement about one file presented where a
statement about the run belongs. That is the bug class this release has been
fixing (#970's forged row, #877's merged coverage data, #984/#1018's
`git-status : ok` reading as an edit's success), arriving through the one op
whose whole output is verdicts.

**This is not a workaround for `| tail -1`.** The repo's own rule is to never
pipe an op through `tail`, because `tail` selects against the answer. The defect
here survives that rule: whatever a reader pipes, a run that *ends* on a per-file
row has no line that describes the run, and there is nowhere to look for one.
The footer is owed on its own merits; `tail -1` becoming safe is a consequence.

The shape is #990's own proposal — a count, with the not-checked slice always
shown, because it degrades correctly: a run that could not check something says
so whether or not anything failed.

  [result] 3 files, 1 with findings, 0 not checked

`N files` / `M with findings` / `K not checked` are file counts and they do not
partition — a file can hold both a finding and a checker that declined. `K`
counts a file where at least one validator returned no verdict, `skipped`
included: #665 refused to *escalate* an optional tool nobody installed, and
disclosing it on a count line is not escalating it. The exit code is untouched.

Two things this file pins that a narrower change would break:

* `0 ops run, 0 writes` must stay off this line (#621) — a read-only op did not
  run any ops, and a zero about a number that means nothing is what got #621
  filed.
* `NOT RUN` must not appear on a clean run. It is the token consumers grep for,
  and printing `0 validators NOT RUN` would put it in the output of every green
  validate — the zero-nobody-reads failure with a live tripwire attached.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
})
ADAPTER_ERROR = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": None, "col": None, "severity": "error",
                "code": "adapter",
                "msg": "fake exited 2 and said nothing about the file"}],
    "duration_ms": 1,
})
SKIPPED = json.dumps({"tool": "fake", "skipped": "fake not installed",
                      "duration_ms": 1})


def _adapter(tmp_path: Path, reply_by_name: dict) -> str:
    """An adapter that answers according to the basename it is handed."""
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import os, sys" + chr(10)
        + f"replies = {reply_by_name!r}" + chr(10)
        + "name = os.path.basename(sys.argv[-1])" + chr(10)
        + f"sys.stdout.write(replies.get(name, {CLEAN!r}))" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()} {{file}}"


def _configure(cmd: str) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False, "timeout": 10},
    }}
    supertool._CONFIG_CHECKED = True


def _result_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result]"):
            return line
    return ""


def _files(tmp_path: Path, *names: str) -> list:
    """Create the files and return their BARE names, cwd being `tmp_path`.

    Relative names on purpose. `validate:f1,f2` joins on `:` and `,`, and an
    absolute Windows path carries a drive-letter colon into that join — the op
    reassembles drive letters, but there is no reason for these tests to be the
    thing that proves it. `monkeypatch.chdir` in `_run` makes the names resolve.
    """
    made = []
    for n in names:
        (tmp_path / n).write_text('{"a": 1}' + chr(10), encoding="utf-8")
        made.append(n)
    return made


def _run(paths: list, capsys, monkeypatch, tmp_path: Path) -> "tuple[int, str]":
    monkeypatch.chdir(tmp_path)
    rc = supertool.main(["validate:" + ",".join(paths)])
    return rc, capsys.readouterr().out


def test_a_clean_run_ends_on_a_verdict_about_the_run(tmp_path, capsys, monkeypatch) -> None:
    """The whole point: the run ends describing the run, not the last file.

    Asserted against the tail of the output rather than against `tail -1`, and
    that is not a detail. #990's premise is that a footer makes `| tail -1` a
    verdict; it does not, because #381 requires `[branch: ...]` to be the last
    line whenever a footer prints, and the #969 decline path already ends there
    today. The footer is owed anyway — a multi-file run that ENDS on one file's
    row has no line describing the run at all, wherever a reader looks.
    """
    _configure(_adapter(tmp_path, {}))
    rc, out = _run(_files(tmp_path, "a.json", "b.json", "c.json"), capsys, monkeypatch, tmp_path)
    assert rc == 0, out
    tail = [ln for ln in out.rstrip(chr(10)).splitlines()
            if not ln.startswith("[branch")]
    assert tail[-1] == "[result] 3 files, 0 with findings, 0 not checked", out


def test_the_footer_counts_files_with_findings(tmp_path, capsys, monkeypatch) -> None:
    _configure(_adapter(tmp_path, {"b.json": REAL_FINDING}))
    _rc, out = _run(_files(tmp_path, "a.json", "b.json", "c.json"), capsys, monkeypatch, tmp_path)
    assert _result_line(out) == "[result] 3 files, 1 with findings, 0 not checked", out


def test_a_skip_is_disclosed_on_the_count_line(tmp_path, capsys, monkeypatch) -> None:
    """`skipped` is an absence, and disclosing it is not escalating it (#665)."""
    _configure(_adapter(tmp_path, {"b.json": SKIPPED}))
    rc, out = _run(_files(tmp_path, "a.json", "b.json"), capsys, monkeypatch, tmp_path)
    assert _result_line(out) == "[result] 2 files, 0 with findings, 1 not checked", out
    assert rc == 0, "a skip still exits 0 — this line discloses, it does not gate"


def test_a_single_file_run_reads_singular(tmp_path, capsys, monkeypatch) -> None:
    _configure(_adapter(tmp_path, {}))
    _rc, out = _run(_files(tmp_path, "a.json"), capsys, monkeypatch, tmp_path)
    assert _result_line(out) == "[result] 1 file, 0 with findings, 0 not checked", out


def test_a_clean_run_never_prints_the_token_consumers_grep_for(
        tmp_path, capsys, monkeypatch) -> None:
    """`0 validators NOT RUN` would put NOT RUN in every green run's output."""
    _configure(_adapter(tmp_path, {}))
    _rc, out = _run(_files(tmp_path, "a.json"), capsys, monkeypatch, tmp_path)
    assert "NOT RUN" not in out, out


def test_the_footer_still_names_a_validator_that_did_not_run(
        tmp_path, capsys, monkeypatch) -> None:
    """#979's clause survives, appended to the counts rather than replacing them."""
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    _configure(_adapter(tmp_path, {"b.json": ADAPTER_ERROR}))
    rc, out = _run(_files(tmp_path, "a.json", "b.json"), capsys, monkeypatch, tmp_path)
    line = _result_line(out)
    assert line.startswith("[result] 2 files, 0 with findings, 1 not checked"), line
    assert "1 validator NOT RUN (fake)" in line, line
    assert "NOT checked" in line, line
    assert rc == 1, out


def test_the_footer_does_not_count_ops_it_never_ran(tmp_path, capsys, monkeypatch) -> None:
    """`0 ops run, 0 writes` about a read-only op is #621's own defect (#969)."""
    _configure(_adapter(tmp_path, {}))
    _rc, out = _run(_files(tmp_path, "a.json"), capsys, monkeypatch, tmp_path)
    line = _result_line(out)
    assert "ops run" not in line, line
    assert "writes" not in line, line


def test_the_footer_terminates(tmp_path, capsys, monkeypatch) -> None:
    """A footer that does not end in a real newline joins whatever prints next."""
    _configure(_adapter(tmp_path, {}))
    _rc, out = _run(_files(tmp_path, "a.json"), capsys, monkeypatch, tmp_path)
    assert chr(92) + "n" not in out, out
    assert out.endswith(chr(10)), repr(out[-40:])

# ---------------------------------------------------------------------------
# Raised by the independent review of the first commit
# ---------------------------------------------------------------------------

def test_a_file_no_validator_matched_is_not_counted_as_clean(
        tmp_path, capsys, monkeypatch) -> None:
    """`0 not checked` must not cover "we own no checker for this type".

    The `fake` validator matches `*.json` only. A `.md` in the same run gets an
    empty block — no rows at all — and folding that into the clean count makes
    "every checker passed" and "nothing looked" the same number, on the line
    whose entire job is telling those apart.
    """
    _configure(_adapter(tmp_path, {}))
    (tmp_path / "notes.md").write_text("# x" + chr(10), encoding="utf-8")
    _rc, out = _run(_files(tmp_path, "a.json") + ["notes.md"], capsys,
                    monkeypatch, tmp_path)
    assert _result_line(out) == "[result] 2 files, 0 with findings, 1 not checked", out


def test_a_validate_inside_a_mutating_batch_still_reports_its_counts(
        tmp_path, capsys, monkeypatch) -> None:
    """An inner op is at dispatch depth > 1 and renders no footer of its own.

    So a `validate:` bundled with an edit had its counts computed and dropped —
    #990's guarantee with a hole in exactly the call shape where a reader is
    least likely to go looking for it.
    """
    _configure(_adapter(tmp_path, {"b.json": REAL_FINDING}))
    _files(tmp_path, "a.json", "b.json")
    ops = tmp_path / "ops.toml"
    ops.write_text(
        "[[ops]]" + chr(10)
        + 'op = "append"' + chr(10)
        + 'path = "a.json"' + chr(10)
        + 'content = ""' + chr(10)
        + chr(10)
        + "[[ops]]" + chr(10)
        + 'op = "validate"' + chr(10)
        + 'paths = ["a.json", "b.json"]' + chr(10),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    supertool.main(["batch:@ops.toml"])
    line = _result_line(capsys.readouterr().out)
    assert "validated 2 files (1 with findings, 0 not checked)" in line, line
    assert "1 op run" in line, "the mutating counts must survive beside them"


def test_the_call_footer_is_not_folded_into_the_last_files_block() -> None:
    """`presets/git/resolve.py` splits validate output into per-file blocks.

    It splits on `validate: PATH` headers, so a footer printed after the last
    block with no header of its own lands inside that file's block. It was inert
    only because the row regexes anchor on a word character and these open with
    `[` — an accident, and #990 turned it from a decline-only case into every
    run. The fold drops them explicitly now.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_resolve_under_test", "presets/git/resolve.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for footer in ("[result] 3 files, 0 with findings, 0 not checked",
                   "[result] 1 validator NOT RUN (fake) — those validators "
                   "returned no verdict, so the file was NOT checked",
                   "[branch: fix/1048]"):
        assert mod._CALL_FOOTER.match(footer), footer
        assert not mod._RESULT_ROW.match(footer), footer
        assert not mod._SKIPPED_ROW.match(footer), footer
