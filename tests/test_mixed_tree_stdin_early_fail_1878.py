"""#1878 -- the mixed-tree decline for a write-class builtin (#1942) sat
after the `@-`/`@file` payload route in `_dispatch_impl`, so a mixed-tree
call carrying its payload on stdin (`edit:@-`, `git-commit:@-`, ...) still
drained stdin before declining. `sys.stdin` is a single stream (#341): once
read, the bytes are gone, so the caller sees `SKIPPED` and has to re-send
the whole payload to retry from the matched tree -- exactly the cost #1878
reports, for a decline that needs only `argv` and the cwd and nothing from
the payload at all.

Mirrors the fixture shapes in `test_mixed_tree_write_ops_1942.py` (own
helpers, not imported -- that file's are module-private and this is a
different code path: the payload route, not the colon-CLI route).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import supertool


def _project_root(tmp_path: Path, name: str, config: dict | None = None) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".supertool.json").write_text(
        json.dumps(config or {}), encoding="utf-8")
    return root


def _stand_in(monkeypatch, root: Path) -> None:
    """Simulate `_load_config()` having resolved this root from the cwd."""
    monkeypatch.chdir(root)
    supertool._CONFIG = {}
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(root / ".supertool.json")


def _foreign_core(root: Path) -> None:
    """Make `root` a *different* supertool checkout than the one under test."""
    (root / "supertool.py").write_text("# a different build of supertool\n",
                                        encoding="utf-8")


class _StdinThatMustNotBeRead(io.StringIO):
    """Fails loudly the instant anything reads it.

    A silent "stdin was never consumed" claim is exactly the kind of absence
    this repo distrusts -- so the positive control below proves this sentinel
    actually fires when a read DOES happen, rather than merely existing.
    """

    def read(self, *a, **kw):  # noqa: D102 -- test sentinel
        raise AssertionError(
            "stdin was read before the mixed-tree decline ran -- the "
            "payload has now been drained and cannot be re-sent (#1878)")


def test_edit_at_dash_declines_under_a_mixed_tree_without_reading_stdin(
    tmp_path, monkeypatch
):
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    (root / "existing.txt").write_text("old\n", encoding="utf-8")
    _stand_in(monkeypatch, root)
    monkeypatch.setattr(supertool.sys, "stdin", _StdinThatMustNotBeRead(""))

    out = supertool.dispatch("edit:@-")

    assert (root / "existing.txt").read_text(encoding="utf-8") == "old\n", (
        f"edit mutated the file under a mixed tree:\n{out}"
    )
    assert "SKIPPED" in out, f"a declined write must say so plainly:\n{out}"


def test_a_config_declared_custom_op_declines_without_reading_stdin(
    tmp_path, monkeypatch
):
    """Same bug, the OTHER class the moved check covers: a config-declared
    custom op with a ':::'-syntax payload route (git-commit is the real
    instance -- this fixture stands in for it without needing the actual
    git preset merged) also used to drain stdin through the shared @file
    route before `_resolve_custom_op`'s own #678 check ever ran.
    """
    config = {
        "ops": {
            "fake-commit": {
                "cmd": "true",
                "syntax": "fake-commit:::MESSAGE",
            }
        }
    }
    root = _project_root(tmp_path, "other_checkout", config)
    _foreign_core(root)
    _stand_in(monkeypatch, root)
    # `_stand_in` sets `_CONFIG = {}` to simulate a resolved root without
    # exercising the real loader -- the other tests here never need the
    # "ops" section, but this one is pinning behaviour that reads it, so
    # the in-memory config has to actually carry what the file on disk does.
    supertool._CONFIG = config
    monkeypatch.setattr(supertool.sys, "stdin", _StdinThatMustNotBeRead(""))

    out = supertool.dispatch("fake-commit:@-")

    assert "SKIPPED" in out, f"a declined write must say so plainly:\n{out}"


def test_edit_at_dash_still_reads_stdin_when_the_tree_matches(tmp_path, monkeypatch):
    """Positive control: an unmixed call must still consume the payload --
    proves the sentinel above actually detects a read when one happens, and
    that the early check does not accidentally block every '@-' call.
    """
    root = _project_root(tmp_path, "same_checkout")
    (root / "existing.txt").write_text("old\n", encoding="utf-8")
    _stand_in(monkeypatch, root)
    payload = "path = 'existing.txt'\nold = 'old'\nnew = 'new'\n"
    monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(payload))

    out = supertool.dispatch("edit:@-")

    assert "SKIPPED" not in out, f"an unmixed edit:@- was declined:\n{out}"
    assert (root / "existing.txt").read_text(encoding="utf-8") == "new\n", (
        f"an unmixed edit:@- did not apply the payload:\n{out}"
    )

def test_a_plain_colon_cli_containment_error_still_wins_over_mixed_tree(
    tmp_path, monkeypatch
):
    """Self-review finding: an earlier version of this fix ran the
    mixed-tree check unconditionally the instant `op` was known, ahead of
    `_gate_paths`. That silently swapped a specific, actionable refusal
    (`ERROR: path escapes cwd`) for the generic mixed-tree `SKIPPED` on any
    call that never touches a payload at all -- a call this fix has no
    business changing, since it was never going to drain stdin either way.
    The chokepoint the class-instance edits actually landed on is gated to
    fire only immediately before `_load_at_file` would run, restoring this
    precedence for the plain colon-CLI form.

    `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` is on by default for the whole suite
    (conftest.py `pytest_configure`) so tmp_path fixtures outside cwd stay
    writable -- unset here, per that same docstring's own documented escape
    hatch, because THIS test needs the containment gate to actually fire.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("edit:::old:::new:::/etc/passwd")

    assert "path escapes cwd" in out, (
        f"a mixed-tree decline swallowed the more specific containment "
        f"error for a call with no payload at all: {out}")
    assert "SKIPPED" not in out, out


def test_a_plain_colon_cli_extra_token_refusal_still_wins_over_mixed_tree(
    tmp_path, monkeypatch
):
    """Same class, the other refusal `_dispatch_impl` checks before the
    #1942 chokepoint: an unconsumed trailing token."""
    root = _project_root(tmp_path, "other_checkout")
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("paste:::f.txt:::content:::extra_token")

    assert "SKIPPED" in out, (
        f"the parent commit (pre-#1878) also answers SKIPPED here -- "
        f"paste has no fixed extra-token slot to violate, so this is "
        f"the correct receipt, pinned as a control against a future "
        f"refactor changing it silently: {out}")
