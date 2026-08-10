"""#678 — a call whose core and whose presets come from two different supertool trees.

The reproduction, from the maintainer's own shell:

    $ cd ~/Documents/claude-supertool          # a master checkout
    $ python3 ~/Documents/st-wt/673/supertool.py 'repo:…/claude-remember' 'gh-pr:282:status'
    PASS  #282 | … url: …/claude-supertool/pull/282        # the WRONG repo

`_load_config()` walks up from **cwd**, so the `.supertool.json` and the
`{project_dir}/presets/*.json` that answered came from the master checkout,
while the core that parsed the ops came from the branch. Two versions of the
tool in one process, and the receipt said `PASS`.

The post-condition pinned here is not "a warning string appears" — it is that a
mixed invocation **cannot print a bare `PASS`**, and that the foreign tree's
command never runs (a sentinel file proves execution, which no message can
fake). `docs/validators.md` §"Declining instead of guessing": three states, and
this one is `skipped`.

The three negative cases matter as much as the positive one. The guard must not
fire for the documented install model — a clone symlinked onto `$PATH` and used
from arbitrary project roots — which is why the signal is "the resolved project
root is *itself a different supertool checkout*" and not the cheaper "the
invoked supertool.py is not under the project root". The latter is true of
almost every legitimate invocation there is.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from _symlink import require_symlink

import supertool


# A `PASS (0.01s)` header with nothing qualifying it on the line.
_BARE_PASS = re.compile(r"^PASS \(\d+\.\d+s\)\s*$", re.MULTILINE)

# Any PASS verdict at all, qualified or not. Matched at line start so the word
# appearing inside the decline's own prose ("rather than PASSing") is not read
# as a verdict — the first version of this file asserted `"PASS" not in out`
# and failed against its own explanatory sentence.
_ANY_PASS_LINE = re.compile(r"^PASS\b", re.MULTILINE)


def _probe_cmd(sentinel: str, token: str) -> str:
    """A custom-op cmd that proves it really executed (see #614's suite).

    Custom ops run argv-form through ``shlex.split(posix=True)``, which strips
    unescaped quotes — hence the outer double quotes. And a token appearing in
    the output is not proof of execution: a mis-quoted probe raises, and Python
    >= 3.13 echoes the offending ``-c`` source back inside the traceback, so the
    assertion matches its own input. The sentinel file is a filesystem side
    effect no error message can produce.
    """
    return (
        '{python} -c "'
        f"open('{sentinel}', 'w').close(); print('{token}')"
        '"'
    )


def _project_root(tmp_path: Path, name: str, cmd: str) -> Path:
    """A project root holding a .supertool.json that declares the `probe` op."""
    root = tmp_path / name
    root.mkdir()
    cfg = {"ops": {"probe": cmd}}
    (root / ".supertool.json").write_text(json.dumps(cfg), encoding="utf-8")
    return root


def _stand_in(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Simulate `_load_config()` having resolved this root from the cwd."""
    monkeypatch.chdir(root)
    cfg = json.loads((root / ".supertool.json").read_text(encoding="utf-8"))
    supertool._CONFIG = cfg
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(root / ".supertool.json")


def _foreign_core(root: Path) -> Path:
    """Make `root` a *different* supertool checkout than the one under test."""
    peer = root / "supertool.py"
    peer.write_text("# a different build of supertool\n", encoding="utf-8")
    return peer


# --------------------------------------------------------------------------
# the defect
# --------------------------------------------------------------------------

def test_mixed_trees_cannot_print_a_bare_pass(tmp_path, monkeypatch):
    """The whole contract: no `PASS` for an answer of unknown provenance."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    before = supertool._SKIP_COUNT[0]
    out = supertool.dispatch("probe")

    assert not _BARE_PASS.search(out), (
        "a mixed-tree invocation printed a bare PASS — the receipt claims a "
        f"verdict for a version the tool cannot name:\n{out}"
    )
    assert not _ANY_PASS_LINE.search(out), (
        f"a declined op must carry no PASS verdict at all:\n{out}")
    assert not (root / "ran.txt").exists(), (
        "the other tree's command ran — the guard disclosed but did not decline"
    )
    assert supertool._SKIP_COUNT[0] == before + 1, (
        "a decline must register as a skip so the call's exit code is non-zero "
        "(#680) — a silent decline is the same absence-read-as-success again"
    )


def test_decline_names_both_trees_and_the_way_out(tmp_path, monkeypatch):
    """A refusal a reader cannot act on is noise. Name both dirs and the fix."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("probe")

    assert str(root) in out, "the decline never names the tree the presets came from"
    assert supertool._INSTALL_DIR in out, "the decline never names the core that ran"
    assert "cwd:" in out, "the decline offers no remedy"


# --------------------------------------------------------------------------
# what the guard must NOT break
# --------------------------------------------------------------------------

def test_ordinary_project_root_is_untouched(tmp_path, monkeypatch):
    """The documented install: a clone on $PATH, run from any project root.

    That project root is not a supertool checkout, so nothing is mixed. This is
    the case the cheaper "binary is not under the project root" signal would
    have broken — it is true of essentially every real invocation.
    """
    root = _project_root(tmp_path, "some_project", _probe_cmd("ran.txt", "PROBE-OK"))
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("probe")

    assert _BARE_PASS.search(out), f"a normal custom op stopped passing:\n{out}"
    assert (root / "ran.txt").exists(), "a normal custom op stopped running"


def test_same_checkout_is_not_a_mix(tmp_path, monkeypatch):
    """Running from inside the checkout the binary belongs to — the correct call.

    The symlink is not scaffolding here: the guard's whole discriminator is
    `realpath` equality, so a copy of `supertool.py` would be a different tree
    and would test the opposite arm. There is no symlink-free restructuring.
    """
    require_symlink()
    root = _project_root(tmp_path, "same_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    os.symlink(os.path.realpath(supertool.__file__), root / "supertool.py")
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("probe")

    assert _BARE_PASS.search(out), (
        "the guard fired on a same-tree call — realpath equality is the whole "
        f"discriminator:\n{out}"
    )
    assert (root / "ran.txt").exists()


def test_builtin_ops_still_answer_under_a_mix(tmp_path, monkeypatch):
    """`read` comes from the core that was invoked. It is not of unknown origin."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    (root / "hello.txt").write_text("line one\n", encoding="utf-8")
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("read:hello.txt")

    assert "line one" in out, f"the guard took a built-in op down with it:\n{out}"


# --------------------------------------------------------------------------
# the deliberate override
# --------------------------------------------------------------------------

def test_override_runs_the_op_but_still_never_bare_passes(tmp_path, monkeypatch):
    """Opting in makes the mix intentional — it does not make the receipt silent."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    _stand_in(monkeypatch, root)
    monkeypatch.setenv("SUPERTOOL_ALLOW_MIXED_TREE", "1")

    out = supertool.dispatch("probe")

    assert (root / "ran.txt").exists(), "the override did not let the op run"
    assert "PROBE-OK" in out
    assert not _BARE_PASS.search(out), (
        "the override printed a bare PASS — the pairing must stay on the line "
        f"that carries the verdict:\n{out}"
    )
    assert str(root) in out and supertool._INSTALL_DIR in out, (
        "the stamped PASS does not say which two trees answered"
    )


def test_override_is_off_by_default(tmp_path, monkeypatch):
    """An unset / falsey knob must not be read as opt-in."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    _stand_in(monkeypatch, root)
    monkeypatch.setenv("SUPERTOOL_ALLOW_MIXED_TREE", "0")

    out = supertool.dispatch("probe")

    assert not (root / "ran.txt").exists()
    assert not _ANY_PASS_LINE.search(out)


# --------------------------------------------------------------------------
# disclosure for a call that declines nothing
# --------------------------------------------------------------------------

def test_main_discloses_the_mix_once_on_stderr(tmp_path, monkeypatch, capsys):
    """A built-in-only call runs fine, but the operator is still told what is loaded."""
    root = _project_root(tmp_path, "other_checkout", _probe_cmd("ran.txt", "PROBE-OK"))
    _foreign_core(root)
    (root / "hello.txt").write_text("line one\n", encoding="utf-8")
    monkeypatch.chdir(root)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    supertool._CONFIG_PATH = None

    rc = supertool.main(["read:hello.txt"])
    captured = capsys.readouterr()

    assert rc == 0, "disclosure must not fail a call that declined nothing"
    assert "line one" in captured.out
    assert "supertool.py" in captured.err and str(root) in captured.err, (
        f"nothing on stderr named the two trees:\n{captured.err}"
    )
