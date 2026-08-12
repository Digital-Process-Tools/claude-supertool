"""An unreadable file must not render as a file with no context to show (#1446).

`validators/common/source_context.py` swallowed `OSError` and returned `[]` —
the same value it returns for a located finding whose window falls outside the
file. Every receipt rendered both identically, which is this repo's house defect:
an absence produced by the tool, read as an absence in the world.

Reachability is not theoretical here, and not Windows-only the way #1443's
`pkg_paths` instance was. Measured on macOS against `Path.read_text`:

    directory        IsADirectoryError  errno 21
    chmod 000        PermissionError    errno 13
    deleted target   FileNotFoundError  errno 2
    symlink loop     OSError            errno 62

All four are ordinary POSIX outcomes. Two adapters make them plainly reachable
without a race: `phpunit-mcp` renders context from a path taken out of the
*tool's own output* (`entry["file"]`), and a `resolve` spec can hand any adapter
a target that no longer exists by the time a `slow`-tier validator runs at the
end of the call.

The contract: the finding survives — the tool said something is wrong at that
line and that claim does not depend on our ability to reprint the line — and the
receipt carries a separate `context_unavailable` reason beside the empty list.
Three states, not two: lines, no lines, and could-not-look.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import supertool

ROOT = Path(__file__).parent.parent
VALIDATORS = ROOT / "validators"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ctx():
    return _load("source_context_1446", VALIDATORS / "common" / "source_context.py")


# ---------------------------------------------------------------------------
# the helper itself
# ---------------------------------------------------------------------------

def test_readable_file_yields_lines_and_no_reason(ctx, tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    fields = ctx.context_fields(str(target), 2)
    assert fields["source_context"], fields
    assert any("2→" in line for line in fields["source_context"]), fields
    assert "context_unavailable" not in fields, fields


def test_no_line_claims_nothing_at_all(ctx, tmp_path: Path) -> None:
    """`line is None` is not a failure to read: no location was ever claimed."""
    assert ctx.context_fields(str(tmp_path / "missing.txt"), None) == {}


def test_window_outside_the_file_is_an_earned_empty(ctx, tmp_path: Path) -> None:
    """The file opened and has no line 900. That empty list is a real answer."""
    target = tmp_path / "short.txt"
    target.write_text("only\n", encoding="utf-8")
    fields = ctx.context_fields(str(target), 900)
    assert fields["source_context"] == [], fields
    assert "context_unavailable" not in fields, fields


def test_missing_file_reports_the_reason(ctx, tmp_path: Path) -> None:
    fields = ctx.context_fields(str(tmp_path / "gone.txt"), 3)
    assert fields["source_context"] == [], fields
    assert "FileNotFoundError" in fields["context_unavailable"], fields
    assert "gone.txt" in fields["context_unavailable"], fields


def test_directory_target_reports_the_reason(ctx, tmp_path: Path) -> None:
    """A `resolve` spec that hands an adapter a directory (#1446)."""
    fields = ctx.context_fields(str(tmp_path), 1)
    assert fields["source_context"] == [], fields
    assert fields.get("context_unavailable"), fields


@pytest.mark.skipif(os.name == "nt", reason="chmod 000 is not a POSIX-mode read denial on Windows")
def test_unreadable_file_reports_the_reason(ctx, tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    target.chmod(0o000)
    try:
        if os.access(str(target), os.R_OK):  # root, or an ACL-y filesystem
            pytest.skip("this process can read a 000 file")
        fields = ctx.context_fields(str(target), 2)
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert fields["source_context"] == [], fields
    assert "PermissionError" in fields["context_unavailable"], fields


def test_reason_is_one_line(ctx, tmp_path: Path) -> None:
    """The renderer puts it on a line of its own; a newline would forge a row."""
    weird = tmp_path / "no\nsuch"
    fields = ctx.context_fields(str(weird), 1)
    assert "\n" not in fields.get("context_unavailable", ""), fields


# ---------------------------------------------------------------------------
# the class, not the instance: no adapter keeps its own swallowing copy
# ---------------------------------------------------------------------------

def test_no_adapter_defines_its_own_source_context() -> None:
    """`phpstan-mcp` and `phpunit-mcp` carried private copies of the same bug.

    A private copy is how the fix rots: it is invisible to a grep for the shared
    helper, and it swallows the same `OSError` one directory over.
    """
    offenders = [
        str(p.relative_to(ROOT))
        for p in VALIDATORS.glob("*/*.py")
        if p.name != "source_context.py"
        and "def source_context(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


def test_no_adapter_still_calls_the_swallowing_helper() -> None:
    """`source_context()` returned a list with nowhere to put the reason.

    It is gone rather than deprecated: left in place, the next adapter author
    copies the call that is already in twenty-eight files and reintroduces #1446
    in a file nobody is looking at.
    """
    callers = [
        str(p.relative_to(ROOT))
        for p in VALIDATORS.glob("*/*.py")
        if "import source_context" in p.read_text(encoding="utf-8")
    ]
    assert callers == [], callers


# ---------------------------------------------------------------------------
# an adapter payload, end to end
# ---------------------------------------------------------------------------

def test_phpunit_mcp_finding_survives_an_unreadable_context_target(tmp_path: Path) -> None:
    """phpunit renders context from `entry["file"]` — a path out of tool output.

    The failure is real and located; only its illustration is missing. So the
    error stays, and the receipt says why there are no lines under it.
    """
    mod = _load("phpunit_mcp_1446", VALIDATORS / "phpunit-mcp" / "phpunit-mcp.py")
    output = json.dumps({
        "tests": 1, "assertions": 1, "skipped": [], "errors": [],
        "failures": [{"file": str(tmp_path / "Deleted.php"), "line": 12,
                      "method": "testThing", "message": "failed asserting"}],
    })
    payload = mod.parse_json_output(str(tmp_path / "Deleted.php"), output, 5)
    assert payload["ok"] is False, payload
    assert payload["count"] == 1, payload
    err = payload["errors"][0]
    assert err["line"] == 12, err
    assert err["source_context"] == [], err
    assert "FileNotFoundError" in err["context_unavailable"], err


def test_changelog_fragment_finding_survives_an_unreadable_context_target(tmp_path: Path) -> None:
    """The `if context:` call sites dropped the key entirely, which SCHEMA.md
    reserves for "this diagnostic is about another file" (#754)."""
    mod = _load("changelog_fragment_1446", VALIDATORS / "changelog-fragment" / "changelog-fragment.py")
    err = mod._error(str(tmp_path / "nope.md"), "nope.md", "nope.md:3: bad", "x")
    assert err["line"] == 3, err
    assert err.get("context_unavailable"), err


# ---------------------------------------------------------------------------
# the receipt a human reads
# ---------------------------------------------------------------------------

def test_verbose_row_states_that_the_context_could_not_be_read() -> None:
    data = {"tool": "t", "ok": False, "count": 1, "duration_ms": 1,
            "errors": [{"line": 12, "code": "E1", "msg": "bad call",
                        "source_context": [],
                        "context_unavailable": "FileNotFoundError reading /x/y.php: No such file or directory"}]}
    lines = supertool._validator_render_row(data, verbose=True)
    body = "\n".join(lines)
    assert "no source context" in body, body
    assert "FileNotFoundError" in body, body


def test_verbose_row_flattens_the_reason() -> None:
    """It is adapter-supplied text on a line of its own — `docs/validators.md`
    ("Every adapter-supplied string these renderers put on a line of their own
    now goes through `_flat_cell`")."""
    data = {"tool": "t", "ok": False, "count": 1, "duration_ms": 1,
            "errors": [{"line": 1, "code": "E1", "msg": "m", "source_context": [],
                        "context_unavailable": "boom\nphplint    : ok        (1ms)"}]}
    lines = supertool._validator_render_row(data, verbose=True)
    assert not any(l.startswith("phplint") for l in lines), lines


def test_non_verbose_row_ignores_the_reason() -> None:
    """Default mode ignores `source_context`; its reason is not louder than it."""
    data = {"tool": "t", "ok": False, "count": 1, "duration_ms": 1,
            "errors": [{"line": 1, "code": "E1", "msg": "m", "source_context": [],
                        "context_unavailable": "FileNotFoundError reading /x"}]}
    lines = supertool._validator_render_row(data)
    assert not any("no source context" in l for l in lines), lines
