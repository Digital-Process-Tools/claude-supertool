"""Placeholder substitution in notifiers, validators and advice is order-free (#379).

Follow-up to #377/#378, which fixed the chained-`str.replace` shape in
`_resolve_custom_op` and `_resolve_alias`. The remaining call sites were safe
only because each happened to substitute its caller-controlled value last —
a property nothing enforced, so a future reorder would silently reintroduce
argv injection. They all go through `_substitute_placeholders` now.

The notifier subprocess discards stdout and stderr, so a corrupted invocation
there fails silently and would never be reported by anything but a test.
"""

from __future__ import annotations

import time
from pathlib import Path

import supertool


def _set_config(d: dict) -> None:
    supertool._CONFIG = d
    supertool._CONFIG_CHECKED = True


def _argv_dump_cmd(marker: Path) -> str:
    """A notifier cmd that records argc and every argv entry it received."""
    return (
        f"{{python}} -c \"import sys, pathlib; "
        f"pathlib.Path(r'{marker.as_posix()}').write_text("
        f"repr(len(sys.argv) - 1) + chr(10) + chr(10).join(sys.argv[1:]))\" "
        f"{{file}} {{line}}"
    )


def _wait_for(marker: Path, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "notifier never fired"
    # The writer may be mid-write when the file first appears.
    deadline = time.time() + timeout
    while time.time() < deadline and not marker.read_text().strip():
        time.sleep(0.05)
    return marker.read_text()


# ---------------------------------------------------------------------------
# notifiers
# ---------------------------------------------------------------------------

def test_path_containing_a_later_token_stays_one_argv_entry(tmp_path: Path) -> None:
    """The repro: `{file}` is substituted before `{line}`, so under the old
    chain a path holding the literal text `{line}` had that token expanded
    inside its own shlex.quote'd value, shattering it across argv."""
    marker = tmp_path / "argv.txt"
    target = tmp_path / "{line}.txt"
    target.write_text("x\n")
    _set_config({
        "notifiers": {
            "argv": {
                "cmd": _argv_dump_cmd(marker),
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    supertool._run_notifiers("edit", str(target), line=42)
    argc, *argv = _wait_for(marker).split("\n")
    assert argc == "2", f"path was split across argv: {argv!r}"
    assert argv[0] == str(target)
    assert argv[1] == "42"


def test_path_containing_before_file_token_is_literal(tmp_path: Path) -> None:
    """`{before_file}` is substituted after `{file}` too — same exposure."""
    marker = tmp_path / "argv.txt"
    target = tmp_path / "{before_file}.txt"
    target.write_text("x\n")
    _set_config({
        "notifiers": {
            "argv": {
                "cmd": _argv_dump_cmd(marker),
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    supertool._run_notifiers("edit", str(target), line=7)
    argc, *argv = _wait_for(marker).split("\n")
    assert argc == "2"
    assert argv[0] == str(target)


def test_empty_placeholder_still_occupies_an_argv_slot(tmp_path: Path) -> None:
    """Regression guard on the behaviour documented at the `_sub` helper:
    shlex.quote("") → '' keeps the positional slot rather than collapsing it."""
    marker = tmp_path / "argv.txt"
    target = tmp_path / "plain.txt"
    target.write_text("x\n")
    _set_config({
        "notifiers": {
            "argv": {
                "cmd": _argv_dump_cmd(marker),
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    supertool._run_notifiers("edit", str(target))  # no line
    argc, *argv = _wait_for(marker).split("\n")
    assert argc == "2"
    assert argv[0] == str(target)
    assert argv[1] == ""


# ---------------------------------------------------------------------------
# advice messages
# ---------------------------------------------------------------------------

def _advice(monkeypatch, message: str, path: str, target: str) -> str:
    monkeypatch.setattr(supertool, "_advice_resolve", lambda cmd, p: target)
    spec = {"message": message, "resolve": "true"}
    return supertool._eval_advice_rule(spec, "edit", path, True, None, {})


def test_advice_path_containing_target_token_uses_append_branch(monkeypatch) -> None:
    """The `{target}` test must read the template, not the substituted text.

    Under the chained form, `{path}` was interpolated first — so a path holding
    the literal string `{target}` made `"{target}" in message` true, and the
    resolver's target was spliced into the middle of the path instead of being
    appended as the advice's suggestion.
    """
    _set_config({})
    out = _advice(monkeypatch, "touched {path}", "src/{target}.py", "src/other.py")
    assert "touched src/{target}.py" in out
    assert "consider src/other.py" in out


def test_advice_target_token_interpolates_when_written_in_the_template(monkeypatch) -> None:
    _set_config({})
    out = _advice(monkeypatch, "pair {path} with {target}", "src/a.py", "tests/a_test.py")
    assert "pair src/a.py with tests/a_test.py" in out
    assert "consider" not in out
