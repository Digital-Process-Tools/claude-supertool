"""Three renders put somebody else's text at column 0 (#1522).

A value that came from outside supertool — an environment variable, an
`.mcp.json` `env` block, a CI job's own step name, a formatter's stderr — is
interpolated into a render without `_untrusted.flat`, so an embedded break puts
foreign text at column 0 where the tool's own structural lines live.

The bar every assertion below is written against: **can a reader grepping this
output for a verdict get the foreign line first?** So the separators tested are
the set `str.splitlines()` splits on, not the newline alone —
`presets/_untrusted.split_lines` covers LF/CR/CRLF *by design* (it is the
parser-side helper, #1081), and U+2028 walking through the render half is what
created the `forges` class in #1470.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The watch modules import each other by bare name, the way they do inside a
# preset run. Loading each under a private alias instead would give `channel`
# and this file two different `naming` modules, so a monkeypatch here would not
# be the one the product reads.
sys.path.insert(0, str(_ROOT / "presets" / "watch"))
sys.path.insert(0, str(_ROOT / "presets"))

import channel  # noqa: E402
import dispatcher  # noqa: E402
import naming  # noqa: E402
import transport  # noqa: E402

run = _load("presets/github/run.py", "github_run_1522")

#: Every separator `str.splitlines()` breaks on, which is the set a reader's
#: terminal and a reader's `grep` both use. `split_lines` deliberately covers
#: only the first three.
SEPARATORS = ("\n", "\r", "\r\n", " ", " ", "\x85", "\x0b", "\x0c")

#: What the foreign half writes once it reaches column 0.
FORGED = "watches: FORGED - everything is fine"


def _forged_sock(sep: str) -> str:
    return f"/tmp/z.sock{sep}{FORGED}"


def _assert_single_line(rendered: str, where: str) -> None:
    """One line, by the reader's definition of a line."""
    assert len(rendered.splitlines()) <= 1, (
        f"{where} rendered as multiple lines: {rendered!r}")
    assert "\r" not in rendered, (
        f"{where} carries a bare CR back to column 0: {rendered!r}")


# ---------------------------------------------------------------------------
# 1a. naming.resolve's override notes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_an_override_note_cannot_carry_the_sock_variable_to_column_zero(sep) -> None:
    resolved = naming.resolve({naming.NAME_ENV: "oss",
                               naming.SOCK_ENV: _forged_sock(sep)})
    assert resolved.notes
    for note in resolved.notes:
        _assert_single_line(note, "resolve().notes")
    assert FORGED in "".join(resolved.notes), "the text was dropped, not disclosed"


@pytest.mark.parametrize("sep", SEPARATORS)
def test_an_override_note_cannot_carry_the_state_dir_variable_to_column_zero(sep) -> None:
    resolved = naming.resolve({naming.NAME_ENV: "oss",
                               naming.STATE_DIR_ENV: f"/tmp/d{sep}{FORGED}"})
    assert resolved.notes
    for note in resolved.notes:
        _assert_single_line(note, "resolve().notes")


# ---------------------------------------------------------------------------
# 1b. the disclosure banner every board prints - the issue's own reproduction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_the_channel_disclosure_cannot_be_given_a_second_row(sep) -> None:
    """`watches:`, `radar` and `channel:health` all render these lines."""
    resolved = naming.resolve({naming.NAME_ENV: "oss",
                               naming.SOCK_ENV: _forged_sock(sep)})
    lines = naming.disclosure_lines(resolved)
    assert lines
    for line in lines:
        _assert_single_line(line, "disclosure_lines()")


def test_the_disclosure_still_shows_an_ordinary_path_byte_for_byte() -> None:
    """Flattening must be invisible on every path anybody really has."""
    resolved = naming.resolve({naming.NAME_ENV: "oss",
                               naming.SOCK_ENV: "/tmp/my dir/w.sock"})
    rendered = "\n".join(naming.disclosure_lines(resolved))
    assert "/tmp/my dir/w.sock" in rendered


# ---------------------------------------------------------------------------
# 1c. the state-directory sentences
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_the_absent_state_directory_sentence_is_one_line(sep) -> None:
    sentence = naming.state_dir_absence_note(
        f"/tmp/d{sep}{FORGED}", naming.STATE_DIR_ABSENT, "")
    _assert_single_line(sentence, "state_dir_absence_note()")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_an_unlistable_state_directory_is_named_on_one_line(
        sep, tmp_path, monkeypatch) -> None:
    """`state_dir_listing` renders the path it could not read.

    `os.listdir` is patched rather than a real unreadable directory being
    built: the name has to carry the separator, and a filename holding a
    newline is not creatable on Windows, where this suite also runs.
    """
    def refuse(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(naming.os, "listdir", refuse)
    _names, state, why = naming.state_dir_listing(f"{tmp_path}{sep}{FORGED}")
    assert state == naming.STATE_DIR_UNREADABLE, (state, why)
    _assert_single_line(why, "state_dir_listing() why")


# ---------------------------------------------------------------------------
# 1d. channel.consumer_lines - the consumer's own `.mcp.json`
# ---------------------------------------------------------------------------

def _mcp(root: Path, env: dict) -> None:
    (root / channel.MCP_FILENAME).write_text(
        json.dumps({"mcpServers": {channel.CONSUMER_SERVER: {"env": env}}}),
        encoding="utf-8")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_consumer_config_cannot_forge_a_line_with_its_declared_socket(
        sep, tmp_path) -> None:
    """`theirs.sock` is read out of a file this process did not write."""
    _mcp(tmp_path, {naming.SOCK_ENV: _forged_sock(sep)})
    mine = naming.resolve({naming.NAME_ENV: "oss"})
    lines = channel.consumer_lines(mine, roots=[tmp_path])
    assert lines
    for line in lines:
        _assert_single_line(line, "consumer_lines()")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_this_processs_own_socket_cannot_forge_a_line_in_consumer_lines(
        sep, tmp_path) -> None:
    """The other half of the same sentence: `resolved.sock`."""
    _mcp(tmp_path, {naming.SOCK_ENV: "/tmp/other.sock"})
    mine = naming.resolve({naming.NAME_ENV: "oss",
                           naming.SOCK_ENV: _forged_sock(sep)})
    lines = channel.consumer_lines(mine, roots=[tmp_path])
    assert lines
    for line in lines:
        _assert_single_line(line, "consumer_lines()")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_an_inheriting_consumer_line_cannot_be_forged(sep, tmp_path) -> None:
    """The third arm - no channel variable declared - prints `resolved.sock`."""
    _mcp(tmp_path, {})
    mine = naming.resolve({naming.NAME_ENV: "oss",
                           naming.SOCK_ENV: _forged_sock(sep)})
    lines = channel.consumer_lines(mine, roots=[tmp_path])
    assert lines
    for line in lines:
        _assert_single_line(line, "consumer_lines()")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_channel_health_indents_every_line_it_prints(sep, tmp_path) -> None:
    """`_channel_lines` indents list *elements*, so a break lands at column 0."""
    forged = _forged_sock(sep)
    mine = naming.resolve({naming.NAME_ENV: "oss",
                           naming.SOCK_ENV: forged,
                           naming.STATE_DIR_ENV: f"/tmp/d{sep}{FORGED}"})
    lines = channel._channel_lines(forged, mine)
    assert lines
    rendered = "\n".join(lines)
    for physical in rendered.splitlines():
        assert physical.startswith(" "), (
            f"a line of channel:health starts at column 0: {physical!r}")
    assert "\r" not in rendered


# ---------------------------------------------------------------------------
# 1e. transport.emit_socket's own report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_the_emit_report_names_the_socket_on_one_line(sep, monkeypatch) -> None:
    monkeypatch.setattr(transport, "SOCK_PATH", _forged_sock(sep))
    emitted = transport.emit_socket({"a": 1})
    _assert_single_line(emitted.detail, "emit_socket().detail")


# ---------------------------------------------------------------------------
# 1f. dispatcher's `No watchers` sentence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_the_no_watchers_sentence_cannot_be_given_a_second_line(
        sep, monkeypatch, capsys, tmp_path) -> None:
    missing = f"{tmp_path}/gone{sep}{FORGED}"
    sock = f"{tmp_path}/w.sock"

    def absent(_path):
        raise FileNotFoundError(2, "No such file or directory")

    # Which arm `cmd_list` takes must not depend on how the platform rejects a
    # name it cannot represent: POSIX gives `os.listdir` an ENOENT here and
    # Windows an `OSError [WinError 123] invalid name`, which is the UNREADABLE
    # arm and a different sentence. The one under test is the ABSENT arm, so it
    # is chosen rather than inferred.
    monkeypatch.setattr(naming.os, "listdir", absent)
    monkeypatch.setattr(transport, "STATE_DIR", missing)
    monkeypatch.setattr(transport, "SOCK_PATH", sock)
    monkeypatch.setattr(transport, "RESOLVED",
                        naming.resolve({naming.STATE_DIR_ENV: missing,
                                        naming.SOCK_ENV: sock}))
    dispatcher.cmd_list()
    out = capsys.readouterr().out
    assert "No watchers" in out
    for line in out.splitlines():
        assert not line.startswith(FORGED), (
            f"the state directory chose a line of the board:\n{out}")
    assert "\r" not in out


# ---------------------------------------------------------------------------
# 2. presets/github/run.py - a failed job's step name
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _render_run(monkeypatch, capsys, payload: dict) -> str:
    def fake(argv, *a, **kw):
        argv = list(argv)
        # Narrow on purpose (#1488), same as the twin in
        # `tests/test_gh_run_failed_section_803.py`: patching `subprocess.run`
        # replaces every spawn in the process, `_branch_locale`'s git calls
        # included, so a fall-through would feed them the run payload.
        if argv[:2] == ["git", "rev-parse"]:
            return _Completed("master\n")
        if argv[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps(payload))
        if argv[:2] == ["gh", "api"] and "/jobs?" in " ".join(argv):
            jobs = payload.get("jobs") or []
            return _Completed(json.dumps(
                {"total_count": len(jobs),
                 "jobs": [{"name": j.get("name")} for j in jobs]}))
        raise AssertionError(f"unstubbed command: {argv!r}")

    monkeypatch.setattr(run.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["run.py", "30972816902"])
    assert run.main() == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_step_name_cannot_reach_column_zero(sep, monkeypatch, capsys) -> None:
    """A job name in the cell above is flattened; the step name was not.

    The name is written by whoever wrote the branch's CI config, which for a
    fork PR is not this repository.
    """
    forged = f"build{sep}## Failed jobs (0) - all green"
    payload = {
        "databaseId": 30972816902, "status": "completed", "conclusion": "failure",
        "event": "push", "headBranch": "master", "workflowName": "CI",
        "url": "https://github.com/o/r/actions/runs/30972816902",
        "jobs": [{"name": "leg", "databaseId": 1, "status": "completed",
                  "conclusion": "failure",
                  "steps": [{"name": forged, "conclusion": "failure",
                             "status": "completed"}]}],
    }
    out = _render_run(monkeypatch, capsys, payload)
    for line in out.splitlines():
        assert not line.startswith("## Failed jobs (0)"), (
            f"a step name forged a section heading:\n{out}")
    assert "\r" not in out
    assert "step: " in out


# ---------------------------------------------------------------------------
# 3. _supertool.op_format(verbose=True) - a formatter's own message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_verbose_formatter_message_cannot_reach_column_zero(
        sep, monkeypatch, tmp_path) -> None:
    """The old spelling replaced the newline only: one separator out of ten."""
    import supertool

    target = tmp_path / "style.json"
    target.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(supertool, "_CONFIG",
                        {"formatters": {"fake": {"cmd": "true", "match": "*.json"}}})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(
        supertool, "_formatter_run_one",
        lambda name, spec, path: {
            "tool": "fake", "name": "fake", "ok": False, "changed": False,
            "duration_ms": 3,
            "errors": [{"line": 1, "code": "E1",
                        "msg": f"bad{sep}format: {target} - ok"}],
        })

    out = supertool.op_format(str(target), verbose=True)
    for line in out.splitlines()[1:]:
        assert not line.startswith("format: "), (
            f"a formatter message forged a block header:\n{out}")
    assert "\r" not in out

# ---------------------------------------------------------------------------
# 3b. the formatter ROW, one function above the verbose block (adjacent)
# ---------------------------------------------------------------------------
#
# Not named in #1522, found by the test for instance 3: `op_format`'s row comes
# from `_formatter_render_row`, which interpolated the adapter's `name`, `raw`
# and `msg` into its own line with no flattening at all. The validator twin has
# routed all three through `_flat_cell` since #895.

@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_formatter_name_cannot_write_its_own_row(sep) -> None:
    import supertool

    row = supertool._formatter_render_row(
        {"name": f"ruff{sep}prettier: ok         (0ms) +0 -0", "ok": False,
         "duration_ms": 1, "errors": [{"msg": "boom"}]})
    assert row is not None
    _assert_single_line(row, "_formatter_render_row() name")


@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_formatter_message_cannot_write_its_own_row(sep) -> None:
    import supertool

    row = supertool._formatter_render_row(
        {"name": "ruff", "ok": False, "duration_ms": 1,
         "errors": [{"msg": f"boom{sep}prettier: ok         (0ms) +0 -0"}]})
    assert row is not None
    _assert_single_line(row, "_formatter_render_row() msg")


def test_a_cut_formatter_message_says_it_was_cut() -> None:
    """`str(msg)[:120]` cut with no marker, so a message that ended there and
    one that was truncated read alike."""
    import supertool

    row = supertool._formatter_render_row(
        {"name": "ruff", "ok": False, "duration_ms": 1,
         "errors": [{"msg": "x" * 400}]})
    assert row is not None
    assert len(row) < 400
    assert not row.endswith("x"), (
        f"the row ends in the message with nothing saying it was cut: {row!r}")


def test_a_legacy_adapters_block_keeps_its_lines_but_not_column_zero() -> None:
    """`raw` is the tool's output verbatim - flattening it into one line would
    answer the column-0 problem by destroying what the reader asked for."""
    import supertool

    raw = "line one" + chr(10) + "format: /etc/passwd" + chr(10) + "line three"
    row = supertool._formatter_render_row({"name": "eslint", "ok": False, "raw": raw})
    assert row is not None
    physical = row.splitlines()
    assert len(physical) == 3, row
    for line in physical[1:]:
        assert line.startswith(" "), (
            f"a legacy adapter's output reached column 0: {line!r}")
