"""#1766 — two of #1708's three flattening sites had no positive control.

#1708 moved `_list_conflicts` to `-z`, which makes a conflicted path arrive as
its real bytes rather than git's `core.quotePath` spelling of it — and a real
path can carry LF, CR or U+2028 into a heading this tool owns at column 0. All
three renders that print such a path flatten it with
`_untrusted.flat(path, disclose_newline=True)`:

* `resolve.py::_shown`, exercised (and mutation-tested) by
  `tests/test_list_conflicts_quotepath_1708.py::test_a_conflicted_filename_cannot_forge_a_receipt_row`.
* `conflicts.py::main`, at the `## {...}` heading — untested until this file.
* `merge.py::main`, at the same heading — untested until this file.

Measured (#1766): restoring the pre-#1708 raw interpolation at both of the
untested sites and running the full suite twice, mutated and restored,
produced the identical `21 failed, 13934 passed, 56 skipped` — no test
anywhere noticed. These two tests are that missing positive control, built the
same way as `resolve.py`'s: mock `_list_conflicts` to hand back a path holding
a live line separator and a live `/etc/passwd` payload, drive the op's own
`main()`, and assert the separator cannot reach column 0 while the payload is
still shown (disclosed, not censored — #1652's loss half).

**Per-site, not a register.** #1766 asks which shape survives a fourth site:
a register keyed on call site (`resolve.py`'s own `_shown` docstring already
names the convention: "every rendered path goes through here"), or one test
per site, the cheap precedent `resolve.py` already has. The candidate register
in this tree for "every X survives a fourth site" is
`tests/test_preset_git_splitlines_register_1130.py`, and it answers a
different question — whether a `str.splitlines()` call may safely stay
unnarrowed — with a published deciding rule #1654 measured as correct for
only 3 of its 27 entries at the time, because a static AST check cannot tell
whether a downstream render actually needs the raw separator disclosed. The
question here is behavioural in the same way: "does this call site's *render*
put a forged separator at column 0", which only running it answers. The
established convention for that behavioural question in this tree is already
per-site and functional — `tests/test_foreign_worktree_forged_line_1557.py`,
`tests/test_column_zero_renders_1522.py`,
`tests/test_gh_branch_flattens_every_remote_field_r1.py`,
`tests/test_list_conflicts_quotepath_1708.py` itself — none of them a static
register, all of them driving the render with a forged payload. `_list_conflicts`
has exactly three callers in `presets/git/` (grep, #1766): `resolve.py`,
`conflicts.py`, `merge.py`; a register with three live entries buys nothing a
fourth per-site test would not, and the one precedent for "AST register that
outlives a fourth site" in this directory is the ground #1654 measured as
mostly wrong. So: per-site, following the shape already used for this exact
class.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).parent.parent
LF = chr(10)
SEP = chr(0x2028)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conflicts = _load("presets/git/conflicts.py", "git_conflicts_1766")
merge = _load("presets/git/merge.py", "git_merge_1766")


def _ok(stdout: str = "") -> mock.Mock:
    return mock.Mock(stdout=stdout, returncode=0, stderr="")


def _dead(rc: int = 1, stderr: str = "") -> mock.Mock:
    return mock.Mock(stdout="", returncode=rc, stderr=stderr)


# ---------------------------------------------------------------------------
# conflicts.py::main — the `## {...}` heading
# ---------------------------------------------------------------------------

def test_git_conflicts_heading_cannot_be_forged_by_a_conflicted_filename(
        monkeypatch, capsys) -> None:
    hostile = "a" + SEP + "  " + "PWNED /etc/passwd"

    monkeypatch.setattr(conflicts, "probe_repo", lambda git_fn: (True, ""))
    monkeypatch.setattr(conflicts, "_detect_state", lambda: "")
    monkeypatch.setattr(
        conflicts, "_list_conflicts",
        lambda: ([hostile, "plain.txt"], ""))
    monkeypatch.setattr(conflicts, "_incoming_info", lambda path, state: [])
    monkeypatch.setattr(
        conflicts, "_all_conflict_blocks",
        lambda path, preview: "  --- block 1 ---")
    monkeypatch.setattr(sys, "argv", ["git-conflicts"])

    rc = conflicts.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "plain.txt" in out, (
        "the fixture produced no rendered conflicts, so nothing was "
        "measured: " + repr(out))
    for line in out.split(LF):
        assert SEP not in line, (
            "a conflicted filename put a live line separator into the "
            "`## ...` heading `git-conflicts` owns at column 0:" + LF
            + repr(out))
    # Not a censor - the crafted text is still shown, disclosed rather than
    # silently dropped (#1652's loss half).
    assert "/etc/passwd" in out, out


# ---------------------------------------------------------------------------
# merge.py::main — the same heading, the `## ... (N block(s))` render
# ---------------------------------------------------------------------------

def _fake_git_for_merge(ref: str):
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "rev-parse":
            if "--verify" in args and args[-1].startswith("refs/heads/"):
                return _dead(1)
            if "--verify" in args:
                return _ok("")
            if "--short" in args:
                return _ok("abc1234")
            if "--abbrev-ref" in args:
                return _ok("mybranch")
            return _dead(1)
        if head == "merge-base":
            return _ok("deadbeef123")
        if head == "merge":
            return _dead(1, stderr="CONFLICT (content): Merge conflict")
        return _dead(1)
    return fake


def test_git_merge_heading_cannot_be_forged_by_a_conflicted_filename(
        monkeypatch, capsys) -> None:
    ref = "deadbeefcafe"
    hostile = "a" + SEP + "  " + "PWNED /etc/passwd"

    monkeypatch.setattr(merge, "_git", _fake_git_for_merge(ref))
    monkeypatch.setattr(
        merge, "_list_conflicts", lambda: ([hostile, "plain.txt"], ""))
    monkeypatch.setattr(merge, "_count_blocks", lambda path: 1)
    monkeypatch.setattr(
        merge, "_first_conflict_block",
        lambda path, preview: "  --- block 1 ---")
    monkeypatch.setattr(sys, "argv", ["git-merge", ref])

    rc = merge.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "plain.txt" in out, (
        "the fixture produced no rendered conflicts, so nothing was "
        "measured: " + repr(out))
    for line in out.split(LF):
        assert SEP not in line, (
            "a conflicted filename put a live line separator into the "
            "`## ...` heading `git-merge` owns at column 0:" + LF
            + repr(out))
    assert "/etc/passwd" in out, out



