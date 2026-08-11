"""#1287 — preset ops sat outside the core's path chokepoint entirely.

`_PATH_ARG_POSITIONS` is the core's per-op table of which argument is a path,
and **no preset op was in it**. So a preset op with a path argument enforced
containment itself or not at all, and "not at all" was the default for anything
newly written. #1283 (`claims` reading `/etc/hosts`) was one instance; nothing
about fixing it made the next preset op safe.

The split this file pins:

* **The chokepoint is universal.** Every preset/custom op passes through
  `_resolve_custom_op`, and the gate lives there.
* **The boundary is not.** `claims` resolves a relative argument against the
  repository root, so a cwd boundary would refuse `claims:docs/x.md` run from
  `docs/` — a call that works today and must keep working. The op declares its
  root; the core enforces it.
* **An op that declares nothing and names a path in its syntax is refused.**
  `skipped` is not available: a path argument reaching no check is not a check
  that could not run, it is an unchecked read. The 20 shipped ops that predate
  the declaration are grandfathered by name in `_UNDECLARED_PATH_OPS`, which
  only shrinks — a NEW op cannot join it without a deliberate edit that this
  file's audit test puts in the diff.
* **The core's opt-outs stay honoured**, at every boundary, because the gate is
  `_safe_path` with a different root rather than a second copy of the rule.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent

RAN = "PRESET-OP-RAN-1287"


@pytest.fixture(autouse=True)
def _no_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest sets the env opt-out globally; containment tests must not."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)


def _echo(tmp_path: Path) -> str:
    script = tmp_path / "ran.py"
    script.write_text("print('" + RAN + "')" + chr(10), encoding="utf-8")
    return shlex.quote(script.as_posix())


def _entry(tmp_path: Path, **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "cmd": "{python} " + _echo(tmp_path) + " {args}",
        "syntax": "probe:PATH",
    }
    entry.update(extra)
    return entry


def _run(entry: Dict[str, Any], args: List[str]) -> str:
    supertool._CONFIG = {"ops": {"probe": entry}}
    out = supertool._resolve_custom_op("probe", ["probe"] + args)
    assert out is not None
    return out


class TestDeclaredBoundaryIsEnforced:

    def test_cwd_boundary_refuses_and_the_op_never_runs(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        monkeypatch.chdir(work)
        out = _run(_entry(tmp_path, paths={"args": [1], "root": "cwd"}),
                   [str(outside)])
        assert out.startswith("ERROR:"), out
        assert "escapes" in out, out
        assert RAN not in out, out

    def test_repo_boundary_accepts_what_the_cwd_boundary_would_refuse(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The #1283 call that must keep working: `claims:...` from a subdir."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        doc = repo / "README.md"
        doc.write_text("x", encoding="utf-8")
        sub = repo / "docs"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert _run(_entry(tmp_path, paths={"args": [1], "root": "cwd"}),
                    [str(doc)]).startswith("ERROR:")
        out = _run(_entry(tmp_path, paths={"args": [1], "root": "repo"}),
                   [str(doc)])
        assert RAN in out, out

    def test_repo_boundary_still_refuses_outside_the_repo(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        monkeypatch.chdir(repo)
        out = _run(_entry(tmp_path, paths={"args": [1], "root": "repo"}),
                   [str(outside)])
        assert out.startswith("ERROR:"), out
        assert "repository root" in out, out
        assert RAN not in out, out

    def test_the_named_slot_is_the_one_gated(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path in an UNDECLARED slot is not refused — the declaration is the
        claim, and over-gating is the #1164 defect from the other side."""
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        entry = _entry(tmp_path, syntax="probe:PATTERN:PATH",
                       paths={"args": [2], "root": "cwd"})
        assert RAN in _run(entry, [str(tmp_path / "outside.txt"), "."])
        assert _run(entry, [".", str(tmp_path / "outside.txt")]).startswith(
            "ERROR:")


class TestUndeclaredIsRefusedNotSkipped:

    def test_path_shaped_syntax_without_a_declaration_is_refused(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        out = _run(_entry(tmp_path), ["anything.txt"])
        assert out.startswith("ERROR:"), out
        assert "paths" in out, out
        assert RAN not in out, out

    def test_the_refusal_names_the_field_and_both_roots(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        out = _run(_entry(tmp_path), ["anything.txt"])
        for token in ('"paths"', '"args"', '"root"', "cwd", "repo"):
            assert token in out, (token, out)

    def test_an_op_with_no_path_in_its_syntax_is_untouched(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        entry = _entry(tmp_path, syntax="probe:NUMBER[:full]")
        assert RAN in _run(entry, ["7"])

    def test_a_plain_string_custom_op_is_untouched(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`.supertool.json` ops written as a bare command string carry no
        syntax, so there is nothing to detect and nothing to refuse."""
        monkeypatch.chdir(tmp_path)
        supertool._CONFIG = {"ops": {
            "probe": "{python} " + _echo(tmp_path) + " {args}"}}
        out = supertool._resolve_custom_op("probe", ["probe", "/etc/hosts"])
        assert out is not None and RAN in out

    def test_empty_args_declares_that_no_argument_is_a_filesystem_path(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`gl-api:PATH` is an API route, not a file. Declared, not defaulted."""
        monkeypatch.chdir(tmp_path)
        entry = _entry(tmp_path, paths={"args": []})
        assert RAN in _run(entry, ["/projects/1/issues"])


class TestAMalformedDeclarationIsRefusedRatherThanIgnored:
    """A declaration the core cannot honour must not read as a declaration.

    Found in review, and it was the live hole in the first cut of this change:
    `{"args": [-1]}` passed the shape check and was then silently dropped by
    the `0 <= i < len(parts)` filter feeding the containment generator, so the
    op ran **completely unchecked** while looking exactly like a gated one.
    That is this repo's standing defect — an absence produced by the tool, read
    as an absence in the world — reintroduced inside the fix for it.
    """

    @pytest.mark.parametrize("decl", [
        {"args": [-1], "root": "cwd"},
        {"args": "1", "root": "cwd"},
        {"args": [True], "root": "cwd"},
        {"args": ["1"], "root": "cwd"},
        {"root": "cwd"},
        "args=1",
    ])
    def test_a_declaration_the_core_cannot_honour_refuses_the_call(
            self, decl: Any, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        out = _run(_entry(tmp_path, paths=decl),
                   [str(tmp_path / "outside.txt")])
        assert out.startswith("ERROR:"), out
        assert "malformed" in out, out
        assert RAN not in out, out

    def test_an_unknown_root_refuses_rather_than_falling_back_to_cwd(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        out = _run(_entry(tmp_path, paths={"args": [1], "root": "home"}),
                   ["local.txt"])
        assert out.startswith("ERROR:"), out
        assert "'home'" in out, out
        assert RAN not in out, out

    def test_a_position_past_the_end_of_the_call_is_not_an_error(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`git-diff[:PATH]` shapes: the slot is legitimately absent."""
        monkeypatch.chdir(tmp_path)
        entry = _entry(tmp_path, syntax="probe:PATTERN:PATH",
                       paths={"args": [2], "root": "cwd"})
        assert RAN in _run(entry, ["only-one-arg"])


class TestOptOutsStayHonoured:

    def test_env_opt_out_reaches_the_universal_gate(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        entry = _entry(tmp_path, paths={"args": [1], "root": "cwd"})
        escaping = str(tmp_path / "outside.txt")
        # Both halves in one test on purpose: an opt-out assertion that only
        # checks the op ran passes just as well against a gate that was never
        # wired up at all.
        assert _run(entry, [escaping]).startswith("ERROR:")
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
        assert RAN in _run(entry, [escaping])

    def test_env_opt_out_reaches_the_repo_boundary_too(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.chdir(repo)
        entry = _entry(tmp_path, paths={"args": [1], "root": "repo"})
        escaping = str(tmp_path / "outside.txt")
        assert _run(entry, [escaping]).startswith("ERROR:")
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
        assert RAN in _run(entry, [escaping])

    def test_the_refusal_names_the_opt_outs_it_honours(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.chdir(repo)
        out = _run(_entry(tmp_path, paths={"args": [1], "root": "repo"}),
                   [str(tmp_path / "outside.txt")])
        assert "SUPERTOOL_ALLOW_OUTSIDE_CWD" in out, out
        assert "allow_outside_cwd" in out, out

    def test_config_opt_out_reaches_the_gate(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        entry = _entry(tmp_path, paths={"args": [1], "root": "cwd"})
        escaping = str(tmp_path / "outside.txt")
        assert _run(entry, [escaping]).startswith("ERROR:")
        supertool._CONFIG = {"allow_outside_cwd": True, "ops": {"probe": entry}}
        out = supertool._resolve_custom_op("probe", ["probe", escaping])
        assert out is not None and RAN in out


def _shipped_ops() -> Dict[str, Dict[str, Any]]:
    ops: Dict[str, Dict[str, Any]] = {}
    for manifest in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, entry in data.get("ops", {}).items():
            if isinstance(entry, dict):
                ops[name] = entry
    return ops


class TestTheGrandfatherSetOnlyShrinks:
    """The mechanism that makes the NEXT preset author safe by default.

    24 shipped ops name a path in their syntax and 4 are declared, so 20 are
    grandfathered. A new op either declares `paths` or fails here — it cannot
    inherit the old default by being written after it.
    """

    def test_every_path_shaped_op_is_declared_or_grandfathered(self) -> None:
        missing = sorted(
            name for name, entry in _shipped_ops().items()
            if supertool._syntax_names_a_path(entry.get("syntax", ""))
            and "paths" not in entry
            and name not in supertool._UNDECLARED_PATH_OPS
        )
        assert missing == [], (
            "these preset ops name a path in their syntax and declare no "
            "containment boundary: " + ", ".join(missing)
        )

    def test_the_grandfather_set_holds_no_name_that_stopped_existing(
            self) -> None:
        shipped = _shipped_ops()
        stale = sorted(n for n in supertool._UNDECLARED_PATH_OPS
                       if n not in shipped)
        assert stale == [], stale

    def test_the_grandfather_set_holds_no_op_that_has_since_declared(
            self) -> None:
        shipped = _shipped_ops()
        redundant = sorted(n for n in supertool._UNDECLARED_PATH_OPS
                           if "paths" in shipped.get(n, {}))
        assert redundant == [], redundant

    def test_the_count_is_recorded_so_a_silent_addition_is_visible(
            self) -> None:
        assert len(supertool._UNDECLARED_PATH_OPS) == 20


class TestTheShippedDeclarations:

    @pytest.mark.parametrize("name,root,args", [
        ("claims", "repo", [1]),
        ("xml", "cwd", [1]),
        ("xml_attr", "cwd", [1]),
        ("xml_count", "cwd", [1]),
    ])
    def test_declaration_is_present_and_says_what_it_should(
            self, name: str, root: str, args: List[int]) -> None:
        entry = _shipped_ops()[name]
        assert entry["paths"] == {"args": args, "root": root}

    def test_claims_declares_the_repository_root_not_the_cwd(self) -> None:
        """#1283's whole argument: a cwd boundary would refuse
        `claims:docs/x.md` run from `docs/`."""
        assert _shipped_ops()["claims"]["paths"]["root"] == "repo"
