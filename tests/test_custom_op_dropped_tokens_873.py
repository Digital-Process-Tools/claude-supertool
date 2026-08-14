"""#873 — a token the `cmd` template cannot reach is refused, not dropped.

`_resolve_custom_op` splits the op string on every `:` and hands the template
whichever tokens it asks for. A template writing `{file}`, `{dir}` or `{arg}`
asks for `parts[1]` and nothing else, so `op:all:dry` reached the subprocess as
`argv == ["all"]` — no warning, no error, no mention in the receipt. The filer's
`:dry` was a safety flag: the op ran live and force-pushed two branches while
its caller read the receipt as a dry run.

**Measured on 2026-08-12** in a scratch project root, since #1472 deleted the
`oss_train` op every example in the issue body uses::

    probe_arg  = "python3 probe.py {arg}"
    probe_args = "python3 probe.py {args}"
    probe_none = "python3 probe.py"

    supertool 'probe_arg:all:dry'   ->  PASS  argv= ['all']          # 'dry' gone
    supertool 'probe_args:all:dry'  ->  PASS  argv= ['all', 'dry']
    supertool 'probe_none:all:dry'  ->  PASS  argv= []               # both gone

The third line is out of scope and stays as it is — a placeholder-free `cmd`
never claimed to take an argument, so its receipt cannot read as a flag that
was honoured, and refusing there reaches 15 tests across 6 files plus every
`check:PRESET:PATH` whose entry takes no path. Filed separately.

**Preset ops take the same route** — `_resolve_custom_op`, one substitution
pass. Zero shipped presets write `{file}` (grep over 160 preset files); before
this change 58 wrote `{args}` or `{argjoin}` and got every token, 24 wrote
`{arg}` and got one, 4 wrote no argument placeholder at all. The three ops
repaired below move the split to 61/21/4, which is what the population test at
the bottom of this file pins. So the difference is not the kind of op,
it is which placeholder its author picked, and that is the trap: `{args}` is the
pass-through, `{file}`/`{arg}`/`{dir}` are single-token by definition.

**Refused rather than passed through.** Widening `{file}` to the whole remainder
would interpolate `parts[2:]` into a declared path slot — `"paths": {"args":
[1]}` gates index 1 only, so the new text would land downstream of the
containment check #1287 built to hold it, which is #1135's shape exactly. It
would also change what all 24 shipped `{arg}` ops receive, several of which
index their `|`-separated fields positionally. So the core keeps one token per
single-token placeholder and refuses the rest above the subprocess, naming the
text it will not pass — the shape the `@file` route already uses for
`op:@payload:extra`.

The refusal sits **downstream of `_preset_path_containment`**: it adds no path
slot and moves none, and a call that both escapes the boundary and drops a token
still reports the containment violation, the more severe of the two.

The issue proposes exit 2 for this, citing #647. There is no exit-2 route in
this core — a refusal is an `ERROR:` line and the run exits 1 — so the refusal
follows the house shape instead.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, List

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
RAN = "__probe_ran__"


def _echo(tmp_path: Path) -> str:
    """A script that prints its argv, so a dropped token is visible."""
    p = tmp_path / "echo.py"
    p.write_text(
        "import sys" + chr(10)
        + "print('" + RAN + " ' + repr(sys.argv[1:]))" + chr(10),
        encoding="utf-8")
    # posix separators and quoted: the cmd template goes through
    # shlex.split(posix=True), which eats a Windows backslash as an escape and
    # splits an unquoted path containing a space.
    return shlex.quote(p.as_posix())


def _run(entry: Any, args: List[str], op: str = "probe") -> str:
    supertool._CONFIG = {"ops": {op: entry}}
    out = supertool._resolve_custom_op(op, [op] + args)
    assert out is not None
    return out


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # conftest sets the containment opt-out globally; the ordering test below
    # asserts the boundary refusal answers first, so it must not be opted out.
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    supertool._CONFIG = None


class TestTheDroppedTokenIsRefused:
    def test_a_single_token_template_refuses_the_token_it_cannot_reach(
            self, tmp_path: Path) -> None:
        """The filer's case: `:dry` evaporated and the op ran live."""
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        out = _run(entry, ["all", "dry"])
        assert out.startswith("ERROR:"), out
        assert RAN not in out, "the op ran with the flag silently dropped"
        assert "dry" in out, out
        assert "{arg}" in out, out

    def test_the_refusal_points_at_the_placeholder_that_would_carry_it(
            self, tmp_path: Path) -> None:
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        out = _run(entry, ["all", "dry"])
        assert "{args}" in out, out

    def test_a_file_template_refuses_rather_than_promoting_a_second_token(
            self, tmp_path: Path) -> None:
        """`{file}` is a declared path slot; token 2 must not become one."""
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {file}",
                 "paths": {"args": [1], "root": "cwd"}}
        out = _run(entry, ["f.txt", "dry"])
        assert out.startswith("ERROR:"), out
        assert RAN not in out, out
        assert "dry" in out, out

    def test_containment_still_answers_first_when_both_are_wrong(
            self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """A boundary escape with a trailing token reports the escape."""
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        outside = str(tmp_path / "outside.txt")
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {file}",
                 "paths": {"args": [1], "root": "cwd"}}
        out = _run(entry, [outside, "dry"])
        assert out.startswith("ERROR:"), out
        assert RAN not in out, out
        assert "#873" not in out, out
        assert "path escapes cwd" in out, out


    def test_a_bare_string_entry_is_gated_too(self, tmp_path: Path) -> None:
        """A `.supertool.json` op written as a plain command string."""
        out = _run("{python} " + _echo(tmp_path) + " {arg}", ["all", "dry"])
        assert out.startswith("ERROR:"), out
        assert RAN not in out, out


class TestWhatMustKeepRunning:
    def test_args_receives_every_token(self, tmp_path: Path) -> None:
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {args}"}
        out = _run(entry, ["all", "dry", "3"])
        assert RAN in out, out
        assert "['all', 'dry', '3']" in out, out

    def test_argjoin_receives_every_token(self, tmp_path: Path) -> None:
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {argjoin}"}
        out = _run(entry, ["all", "dry"])
        assert RAN in out, out
        assert "all:::dry" in out, out

    def test_one_token_to_a_single_token_template_still_runs(
            self, tmp_path: Path) -> None:
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        out = _run(entry, ["all"])
        assert RAN in out, out
        assert "['all']" in out, out

    def test_no_token_at_all_still_runs(self, tmp_path: Path) -> None:
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        out = _run(entry, [])
        assert RAN in out, out

    def test_a_template_with_no_argument_placeholder_is_left_alone(
            self, tmp_path: Path) -> None:
        """`probe_none:all` reaches the script as `argv == []`, and runs.

        Deliberate, and the boundary of this fix. Such an op never claimed to
        take an argument, so no part of its receipt can read as a flag that was
        honoured — the specific harm #873 is about. Refusing here would also be
        a far wider net: 15 tests across 6 files hand a throwaway token to a
        placeholder-free `cmd`, and `op_check` forwards its path to whatever
        entry was named. Worth doing, filed separately, not ridden in here.
        """
        entry = {"cmd": "{python} " + _echo(tmp_path)}
        out = _run(entry, ["all"])
        assert RAN in out, out

    def test_a_trailing_empty_token_is_not_dropped_text(
            self, tmp_path: Path) -> None:
        """`op:all:` carries no text to lose, so there is nothing to refuse."""
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        out = _run(entry, ["all", ""])
        assert RAN in out, out

    def test_dir_and_file_together_still_read_one_token(
            self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {file} {dir}",
                 "paths": {"args": [1], "root": "cwd"}}
        out = _run(entry, ["f.txt"])
        assert RAN in out, out

    def test_a_colon_inside_a_url_is_one_token_not_two(
            self, tmp_path: Path) -> None:
        """`_split_arg` rejoins URL schemes, so a publish op is not refused."""
        entry = {"cmd": "{python} " + _echo(tmp_path) + " {arg}"}
        parts = supertool._split_arg(
            "probe:T|/tmp/p.md|https://example.com/post|ai")
        assert len(parts) == 2, parts
        supertool._CONFIG = {"ops": {"probe": entry}}
        out = supertool._resolve_custom_op("probe", parts)
        assert out is not None and RAN in out, out


class TestTheHelperItself:
    @pytest.mark.parametrize("cmd,args,expected", [
        ("x {args}", ["a", "b", "c"], []),
        ("x {argjoin}", ["a", "b"], []),
        ("x {file}", ["a"], []),
        ("x {file}", ["a", "b"], ["b"]),
        ("x {dir}", ["a", "b", "c"], ["b", "c"]),
        ("x {arg}", ["a", "b"], ["b"]),
        ("x", [], []),
        ("x", ["a"], []),
        ("x", ["a", "b"], []),
        ("x {arg}", ["a", ""], []),
        ("x {arg}", ["a", "", "b"], ["", "b"]),
    ])
    def test_unconsumed_tokens(
            self, cmd: str, args: List[str], expected: List[str]) -> None:
        assert supertool._unconsumed_arg_tokens(
            cmd, ["probe"] + args) == expected


class TestEveryShippedOpCanReachItsDocumentedTokens:
    """The class, not the instance.

    Three shipped ops documented a second colon token their `cmd` could not
    reach — `hashnode_list:USER[:N]`, `devto_list:USER[:N]` and
    `hashnode_search:QUERY[:N]`, all on `{arg}` — and each script split its one
    argument on `:` for a field that had already been discarded upstream. That
    is the filer's "I fixed the parse twice against argument shapes the tool
    never produces", shipped. This walks every manifest so the next one is red
    at write time rather than at use time.
    """

    @staticmethod
    def _entries() -> List[tuple]:
        out: List[tuple] = []
        files = sorted((_ROOT / "presets").glob("*.json")) + [
            _ROOT / ".supertool.json"]
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            for name, entry in (data.get("ops") or {}).items():
                cmd = entry.get("cmd") if isinstance(entry, dict) else entry
                if isinstance(cmd, str) and cmd:
                    out.append((name, f.name, entry, cmd))
        return out

    def test_the_population_is_what_it_was_measured_to_be(self) -> None:
        rows = self._entries()
        assert len(rows) == 86, len(rows)
        multi = [n for n, _f, _e, c in rows
                 if "{args}" in c or "{argjoin}" in c]
        one = [n for n, _f, _e, c in rows
               if n not in multi and re.search(r"\{(file|dir|arg)\}", c)]
        none = [n for n, _f, _e, c in rows
                if n not in multi and n not in one]
        # 61 → 62 / 21 → 20 in #1715: `gh-run` gained an `attempt=K` second
        # token, which a `{arg}` template cannot reach, so its cmd moved to
        # `{args}` — the same move this file's own class exists to require.
        assert (len(multi), len(one), len(none)) == (62, 20, 4), (
            len(multi), len(one), len(none))
        # The 4 placeholder-free ops are outside this gate on purpose — see
        # `_unconsumed_arg_tokens`. Named so the exclusion is a list, not a
        # blanket: a fifth one appearing is a decision somebody has to make.
        assert sorted(none) == [
            "git-conflicts", "mcp_status", "mcp_stop_all", "watches"], none
        assert [n for n, _f, _e, c in rows if "{file}" in c] == []

    def test_no_documented_syntax_names_a_token_the_cmd_cannot_reach(
            self) -> None:
        offenders: List[str] = []
        for name, fname, entry, cmd in self._entries():
            if not isinstance(entry, dict):
                continue
            for key in ("syntax", "example"):
                text = entry.get(key)
                if not isinstance(text, str):
                    continue
                for alt in re.split(r"\s\|\s", text):
                    alt = alt.strip()
                    if not alt.startswith(name):
                        continue
                    dropped = supertool._unconsumed_arg_tokens(
                        cmd, supertool._split_arg(alt))
                    if dropped:
                        offenders.append(f"{fname}:{name}:{key} -> {dropped}")
        assert offenders == [], offenders


def test_the_refusal_reaches_the_receipt_end_to_end(tmp_path: Path) -> None:
    """Through the real CLI, because a refusal nobody sees is the defect."""
    (tmp_path / ".supertool.json").write_text(json.dumps({
        "ops": {"probe": {"cmd": "{python} -c " + json.dumps("print(1)")
                                 + " {arg}"}}
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "probe:all:dry"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(tmp_path), timeout=120)
    assert "ERROR:" in proc.stdout, proc.stdout
    assert "dry" in proc.stdout, proc.stdout
    assert proc.returncode == 1, (proc.returncode, proc.stdout)
