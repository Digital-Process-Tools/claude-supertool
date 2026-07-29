"""#569 (second half): an unreadable `.supertool.json` read as "no servers".

`find_supertool_json` returned `{}` from two places that mean opposite things:
the walk reached the filesystem root without finding a config, and a config was
found and could not be read — malformed JSON, permission denied, a truncated
write. `main()` then built an empty `hash_to_name`, so **every** row printed `?`
in the NAME column.

Nothing is *asserted* falsely: `?` is already the honest rendering for an
orphan daemon. The cost is in the next action it implies. "These daemons are
not in your config" sends the reader hunting a stray process; "your config
could not be parsed" sends them to their JSON. This is the surface a human is
looking at while already confused about what is running, and the two readings
point in opposite directions.

Same house defect as the pid guard in this issue — an absence produced by the
tool read as an absence in the world — and the same fix shape as #551's
`list_pidfiles`: return the reason alongside the value, and say it. `main()`
keeps exiting `0` in every case (#552): this is a note, not a verdict.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="status.py's runtime dir is ownership-checked; os.geteuid is required.",
)


@pytest.fixture
def status_mod(tmp_path, monkeypatch):
    """`status` with an empty runtime dir and a cwd that has no config above it."""
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    import status  # noqa: PLC0415

    return status


def _pidfile(status_mod, h: str, body: str = "4242\n") -> Path:
    from _paths import runtime_dir  # noqa: PLC0415

    p = Path(runtime_dir()) / f"supertool-mcp-{h}.pid"
    p.write_text(body, encoding="utf-8")
    return p


class TestAnUnreadableConfigSaysSo:
    """The absent case and the unreadable case must not share a rendering."""

    def test_malformed_json_is_named(self, status_mod, capsys) -> None:
        Path(".supertool.json").write_text('{"mcp": {"php-lsp"', encoding="utf-8")
        _pidfile(status_mod, "aaaaaaaaaaaa")

        rc = status_mod.main()
        out = capsys.readouterr().out

        assert ".supertool.json" in out, f"config not named: {out!r}"
        assert "could not" in out.lower() or "parse" in out.lower(), out
        assert rc == 0, "a note is not a verdict — status exits 0 (#552)"

    def test_the_parse_error_itself_is_shown(self, status_mod, capsys) -> None:
        """"Could not be read" without the reason costs the reader the fix."""
        Path(".supertool.json").write_text("{\n  not json\n}\n", encoding="utf-8")
        _pidfile(status_mod, "bbbbbbbbbbbb")

        status_mod.main()
        out = capsys.readouterr().out

        assert "line" in out.lower() or "expecting" in out.lower(), (
            f"no parse detail: {out!r}"
        )

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root can read a 0o000 file, so permission denied cannot be staged.",
    )
    def test_permission_denied_is_named_too(self, status_mod, capsys) -> None:
        """The OSError arm, not only the JSONDecodeError arm."""
        p = Path(".supertool.json")
        p.write_text('{"mcp": {}}', encoding="utf-8")
        os.chmod(p, 0o000)
        try:
            status_mod.main()
        finally:
            os.chmod(p, 0o600)
        out = capsys.readouterr().out

        assert ".supertool.json" in out, f"config not named: {out!r}"

    def test_valid_json_that_is_not_an_object_is_named(self, status_mod, capsys) -> None:
        """`[]` parses, then `cfg.get` raised AttributeError out of `mcp_status`."""
        Path(".supertool.json").write_text("[]", encoding="utf-8")
        _pidfile(status_mod, "999999999999")

        rc = status_mod.main()
        out = capsys.readouterr().out

        assert ".supertool.json" in out, f"config not named: {out!r}"
        assert "999999999999" in out, "the table was lost to a crash"
        assert rc == 0

    def test_the_note_does_not_replace_the_table(self, status_mod, capsys) -> None:
        """The daemons are still listed — the note explains the NAME column."""
        Path(".supertool.json").write_text("{oops", encoding="utf-8")
        _pidfile(status_mod, "cccccccccccc")

        status_mod.main()
        out = capsys.readouterr().out

        assert "cccccccccccc" in out, f"row lost to the note: {out!r}"
        assert "SOCKET" in out, "header lost to the note"


class TestTheHonestAbsencesSurvive:
    """Do not trade a misleading `?` for a warning on every ordinary run."""

    def test_no_config_at_all_says_nothing(self, status_mod, capsys) -> None:
        """A genuine walk to the root is not an error and must stay silent."""
        _pidfile(status_mod, "dddddddddddd")

        status_mod.main()
        out = capsys.readouterr().out

        assert ".supertool.json" not in out, f"cried wolf over no config: {out!r}"

    def test_a_readable_config_still_names_its_daemons(self, status_mod, capsys) -> None:
        Path(".supertool.json").write_text(
            json.dumps({"mcp": {"php-lsp": {}}}), encoding="utf-8"
        )
        _pidfile(status_mod, status_mod.hash_for("php-lsp"))

        status_mod.main()
        out = capsys.readouterr().out

        assert "php-lsp" in out
        assert "could not" not in out.lower(), f"note on a readable config: {out!r}"

    def test_an_orphan_still_reads_as_orphan(self, status_mod, capsys) -> None:
        """`?` is the right answer for a daemon a valid config does not declare."""
        Path(".supertool.json").write_text(
            json.dumps({"mcp": {"php-lsp": {}}}), encoding="utf-8"
        )
        _pidfile(status_mod, "eeeeeeeeeeee")

        status_mod.main()
        out = capsys.readouterr().out

        assert "?" in out
        assert ".supertool.json" not in out


class TestTheReasonTravelsWithTheValue:
    """`find_supertool_json` returns `(cfg, reason)`, mirroring #551 and #549."""

    def test_absent_config_returns_no_reason(self, status_mod) -> None:
        cfg, reason = status_mod.find_supertool_json()

        assert cfg == {}
        assert reason == ""

    def test_unreadable_config_returns_a_reason(self, status_mod) -> None:
        Path(".supertool.json").write_text("{oops", encoding="utf-8")

        cfg, reason = status_mod.find_supertool_json()

        assert cfg == {}
        assert reason, "an unreadable config returned the same tuple as an absent one"
        assert ".supertool.json" in reason
