"""#1519 — the adapter trusts a path in both directions.

Instance 1: the target is handed to `tsc` as argv with nothing separating it
from `tsc`'s own option grammar, and `tsc` reads a leading `@` as a **response
file** — its command line comes from that file instead. Measured with real tsc
6.0.3, `@r.ts` on disk beside an `r.ts` holding `--noEmit false --outDir out`:
the receipt read `{"ok": true, "count": 0}` for a file whose one line is a type
error, and `out/a.js` / `out/b.js` were written, against `docs/validators.md`'s
"`--noEmit` — no output files written".

Instance 2: `tsc --noEmit FILE` type-checks the whole import graph, so a
diagnostic about an imported file is the common case and not an edge one.
Measured on the same tsc: `tsc --noEmit --skipLibCheck --pretty false g/main.ts`
prints `g/dep.ts(1,14): error TS2322: ...`. The parse threw the path away and
published that line number as this file's, with `context_fields(file, ln)`
printing *this* file's source at *that* file's line as the evidence.
`validators/SCHEMA.md` §"A located diagnostic still has to be about *this* file
(#754)" mandates `line: null` / `code: "adapter"` / no `source_context`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ADAPTER = (Path(__file__).resolve().parent.parent
           / "validators" / "tsc-check" / "tsc-check.py")


def _mod():
    spec = importlib.util.spec_from_file_location("tsc_check_1519", ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Instance 1 — the target enters argv unconstrained
# ---------------------------------------------------------------------------

def _argv_for(monkeypatch, tmp_path: Path, name: str) -> list:
    """The argv the adapter would hand `tsc` for a target called `name`."""
    mod = _mod()
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/tsc")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.sys, "argv", ["tsc-check.py", name])
    monkeypatch.chdir(tmp_path)
    mod.main()
    assert seen, "the adapter never spawned tsc"
    return seen[0]


@pytest.mark.parametrize("name", ["@r.ts", "@@r.ts"])
def test_an_at_named_target_cannot_become_a_response_file(
        monkeypatch, tmp_path, capsys, name) -> None:
    """`tsc` reads `@NAME` as a response file, so `--noEmit` does not survive it.

    The assertion is on the argument, not on the receipt: whatever a response
    file happens to contain, the defect is that the target is spelled in a way
    `tsc` reads as an option rather than as a path.
    """
    argv = _argv_for(monkeypatch, tmp_path, name)
    capsys.readouterr()
    target = argv[-1]
    assert not target.startswith("@"), argv
    assert target.endswith(name), argv


@pytest.mark.parametrize("name", ["-p.ts", "--build.ts"])
def test_a_dash_named_target_cannot_become_an_option(
        monkeypatch, tmp_path, capsys, name) -> None:
    """The same class one character over: `tsc` reads a leading `-` as a flag.

    `tsc --noEmit --skipLibCheck --pretty false -p.ts` is `-p .ts`, a project
    path — the file is never checked and the exit code is about something else.
    """
    argv = _argv_for(monkeypatch, tmp_path, name)
    capsys.readouterr()
    target = argv[-1]
    assert not target.startswith("-"), argv
    assert target.endswith(name), argv


def test_an_ordinary_target_is_passed_through_unchanged(
        monkeypatch, tmp_path, capsys) -> None:
    """The regression guard. Would pass with the code doing nothing — that is
    its job, beside the four above that would not."""
    argv = _argv_for(monkeypatch, tmp_path, os.path.join("src", "app.ts"))
    capsys.readouterr()
    assert argv[-1] == os.path.join("src", "app.ts"), argv


def test_an_absolute_target_is_passed_through_unchanged(
        monkeypatch, tmp_path, capsys) -> None:
    """An absolute path can never be read as an option, and prefixing it would
    produce a path that names nothing."""
    abs_target = str(tmp_path / "app.ts")
    argv = _argv_for(monkeypatch, tmp_path, abs_target)
    capsys.readouterr()
    assert argv[-1] == abs_target, argv


# ---------------------------------------------------------------------------
# Instance 2 — the parsed path is discarded
# ---------------------------------------------------------------------------

DEP = "g/dep.ts(1,14): error TS2322: Type 'string' is not assignable to type 'number'."
OWN = "g/main.ts(2,14): error TS2322: Type 'number' is not assignable to type 'string'."


def _parse(output: str, target: str, base: str) -> list:
    return _mod().parse_diagnostics(output, target, base)


def test_a_diagnostic_about_another_file_is_not_located_in_this_one(
        tmp_path) -> None:
    (tmp_path / "g").mkdir()
    main = tmp_path / "g" / "main.ts"
    main.write_text("import {b} from './dep';\nexport const a = b;\n",
                    encoding="utf-8")
    errors = _parse(DEP, str(main), str(tmp_path))
    assert len(errors) == 1, errors
    e = errors[0]
    assert e["line"] is None and e["col"] is None, e
    assert e["code"] == "adapter", e
    assert "source_context" not in e, e
    assert "dep.ts" in e["msg"], e


def test_its_own_diagnostic_keeps_its_location_and_its_code(tmp_path) -> None:
    """The other half. A rule that demotes everything is not an attribution."""
    (tmp_path / "g").mkdir()
    main = tmp_path / "g" / "main.ts"
    main.write_text("import {b} from './dep';\nexport const a: string = b;\n",
                    encoding="utf-8")
    errors = _parse(OWN, str(main), str(tmp_path))
    assert len(errors) == 1, errors
    e = errors[0]
    assert (e["line"], e["col"]) == (2, 14), e
    assert e["code"] == "TS2322", e
    assert any(row.startswith("2→") for row in e["source_context"]), e


def test_a_mixed_dump_keeps_both_and_locates_only_one(tmp_path) -> None:
    """`count` is what `_validator_regressed` subtracts, so the foreign
    diagnostic is kept rather than filtered: the program genuinely does not
    type-check and a caller told nothing cannot act on it."""
    (tmp_path / "g").mkdir()
    main = tmp_path / "g" / "main.ts"
    main.write_text("import {b} from './dep';\nexport const a: string = b;\n",
                    encoding="utf-8")
    errors = _parse(DEP + "\n" + OWN, str(main), str(tmp_path))
    assert len(errors) == 2, errors
    located = [e for e in errors if e["line"] is not None]
    assert len(located) == 1, errors
    assert located[0]["line"] == 2, located


def test_a_foreign_only_dump_is_a_non_verdict_about_this_file(tmp_path) -> None:
    """Every error `adapter`-coded is what `_validator_not_checked` keys on —
    the third state, reached without the `skipped` that would drop `errors`.

    This is the case that matters most: `docs/validators.md` recommends
    `rollback_on_fail: true` for `.ts`, and a foreign diagnostic reaching the
    rollback arm reverts a correct edit over a defect in a file the edit never
    touched.
    """
    (tmp_path / "g").mkdir()
    main = tmp_path / "g" / "main.ts"
    main.write_text("import {b} from './dep';\n", encoding="utf-8")
    errors = _parse(DEP, str(main), str(tmp_path))
    assert errors, errors
    assert all((e.get("code") or "") == "adapter" for e in errors), errors


def test_a_path_that_cannot_be_placed_is_not_charged_to_this_file(
        tmp_path) -> None:
    """`pkg_paths.attribute`'s third answer. Only `other` is entitled to say
    another file is at fault, so `unknown` says neither."""
    main = tmp_path / "main.ts"
    main.write_text("export const a = 1;\n", encoding="utf-8")
    errors = _parse("rel.ts(3,4): error TS1005: ';' expected.", str(main), "")
    assert len(errors) == 1, errors
    assert errors[0]["line"] is None, errors[0]
    assert errors[0]["code"] == "adapter", errors[0]
    assert "could not tell" in errors[0]["msg"], errors[0]


def test_windows_spellings_are_folded_before_the_separator_is_normalised() -> None:
    """`os.path.normcase` rewrites `/` into a backslash on Windows, so a
    comparison that normalises separators first matches nothing there and every
    diagnostic — including the file's own — is demoted (#1005, four red legs).

    Asserted from any platform by injecting the fold, which is why
    `pkg_paths.canon` takes one. This adapter now routes through that module
    rather than comparing paths of its own.
    """
    spec = importlib.util.spec_from_file_location(
        "pkg_paths_1519", ADAPTER.parent.parent / "common" / "pkg_paths.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fold = lambda s: s.lower().replace("/", chr(92))  # noqa: E731
    assert mod.canon("SRC/App.ts", normcase=fold) == "src/app.ts"


def test_an_unlocatable_dump_line_still_produces_no_record(tmp_path) -> None:
    """Unchanged by this fix, restated so the attribution split cannot eat it:
    a line carrying no `(line,col)` is not a diagnostic and the caller's
    `not errors` arm is what turns the dump into a non-verdict."""
    main = tmp_path / "main.ts"
    main.write_text("export const a = 1;\n", encoding="utf-8")
    assert _parse("error TS18003: No inputs were found in config file.",
                  str(main), str(tmp_path)) == []


def test_the_base_is_the_directory_the_adapter_ran_tsc_in(
        monkeypatch, tmp_path, capsys) -> None:
    """`tsc` prints relative to its working directory and the adapter chose it,
    so the base is known rather than inferred — `validators/SCHEMA.md` §"the
    adapter chose the working directory". Anchoring to anything else charges a
    foreign file's line to this one."""
    (tmp_path / "g").mkdir()
    main = tmp_path / "g" / "main.ts"
    main.write_text("export const a = 1;\nexport const b: string = 1;\n",
                    encoding="utf-8")
    mod = _mod()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, OWN + "\n", "")

    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/tsc")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod.sys, "argv",
                        ["tsc-check.py", os.path.join("g", "main.ts")])
    mod.main()
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert receipt["count"] == 1, receipt
    assert receipt["errors"][0]["line"] == 2, receipt
