"""Tests for #2075: `presets/slack.json`'s `syntax` field still advertised
the bare-path arm `#2039` removed from `presets/slack/publish.py::_resolve_body`,
so `help:slack_publish` taught a call (`slack_publish:C0123|notes.md`) that
now silently posts the literal filename instead of the file's contents.

`syntax` is treated here as the op's contract, not decoration -- so this
pins the manifest string itself, and separately proves the behavior it
now describes is what the op actually does, rather than trusting the two
to stay in sync by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

from _preset_loader import load_preset_module

_MANIFEST = Path(__file__).parent.parent / "presets" / "slack.json"


def _syntax() -> str:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return manifest["ops"]["slack_publish"]["syntax"]


def test_manifest_no_longer_advertises_the_bare_path_arm_2039_removed() -> None:
    syntax = _syntax()
    assert "TEXT_OR_FILE_OR_file://PATH" not in syntax, (
        "the manifest still advertises the bare-path arm #2039 withdrew from "
        "publish.py -- see docs/presets/slack.md:42 for the migration note"
    )


def test_manifest_syntax_matches_docs_presets_slack_md() -> None:
    """Three of four surfaces already agreed (`docs/presets/slack.md:36`,
    `:42`, `changelog.d/2039.security.md`) -- the manifest was the outlier.
    Pin the manifest against the doc's own wording so a future edit to
    either one that drifts from the other is caught here rather than by a
    caller who trusted `help:slack_publish`."""
    syntax = _syntax()
    assert syntax == "slack_publish:CHANNEL_ID|TEXT_OR_file://PATH[|THREAD_TS[|force]]"


def test_manifest_syntax_matches_what_the_op_actually_accepts() -> None:
    """The behavioral half: a bare path that exists and sits inside the
    safety allowlist must NOT be read as a file -- only the explicit
    `file://` prefix may trigger a read. If this ever regresses, the
    manifest's own `syntax` string (asserted above) would be describing a
    contract the code does not honor, which is the exact defect #2075
    reports."""
    import tempfile

    publish = load_preset_module("slack", "publish", "sl2075_")
    with tempfile.TemporaryDirectory() as td:
        import os
        cwd = os.getcwd()
        os.chdir(td)
        try:
            os.mkdir(".max")
            with open(os.path.join(".max", "notes.md"), "w", encoding="utf-8") as f:
                f.write("private notes, not for Slack")
            _channel, text, _ts, _force = publish.parse_args("C0123456|.max/notes.md")
        finally:
            os.chdir(cwd)
    assert text == ".max/notes.md"
    assert "private notes" not in text
