"""`channel:health`'s `consumer config` line says whose file it read (#2184).

`_mcp_roots()`'s first entry is `Path(__file__).resolve().parents[2]` --
whichever copy of supertool is executing, a development clone or the
marketplace cache directory for an installed plugin. Neither is a file the
harness loaded for the caller's own session, and neither is one the caller can
edit. Before this, `consumer_lines` rendered both roots through the identical
unlabelled `consumer config <path>` shape, so a reader could not tell "this is
your file" from "this is supertool's own copy" -- which is what made #2182's
own diagnosis take three release cycles: a maintainer read the line, edited
the wrong file, saw no change, learned nothing.

The fix is `_root_label`: every rendered `consumer config` line now carries
`(this plugin's own copy)` or `(the caller's project)`, decided by identity
against `_PLUGIN_ROOT`, never by list position -- `roots=` is `consumer_lines`'s
own test seam and a directory a test hands it stands in for "wherever this
looks", so it must render as the caller's-project label even at index 0.

Every "must label as the plugin's own" case is paired with a "must label as
the caller's" partner in the same fixture, because an assertion that checks
only one direction passes for a fix that always prints the same label.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402


def _mcp(dir_path: Path, env: dict[str, str] | None) -> Path:
    server: dict = {"command": "bun", "args": ["channel.ts"]}
    if env is not None:
        server["env"] = env
    (dir_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {channel.CONSUMER_SERVER: server}}),
        encoding="utf-8")
    return dir_path


# ---------------------------------------------------------------------------
# `_root_label` in isolation -- identity, not position
# ---------------------------------------------------------------------------

def test_the_real_plugin_root_is_labelled_as_the_plugins_own_copy() -> None:
    assert channel._root_label(channel._PLUGIN_ROOT) == channel._ROOT_LABEL_PLUGIN


def test_an_arbitrary_directory_is_labelled_as_the_callers_project(tmp_path) -> None:
    """The must-not-fire twin: anything that is not genuinely `_PLUGIN_ROOT`
    must never earn the plugin's-own label, even sitting at roots[0]."""
    assert channel._root_label(tmp_path) == channel._ROOT_LABEL_CALLER


def test_a_relative_alias_of_the_plugin_root_still_resolves_to_the_plugin_label() -> None:
    """Resolved-path identity, not string equality -- `.` inside the real
    plugin root must still match, the same way a symlinked install would."""
    aliased = channel._PLUGIN_ROOT / "presets" / ".."
    assert channel._root_label(aliased) == channel._ROOT_LABEL_PLUGIN


# ---------------------------------------------------------------------------
# `consumer_lines` -- the rendered line names which root it is about
# ---------------------------------------------------------------------------

def test_a_line_from_the_real_plugin_root_is_labelled_as_such(monkeypatch) -> None:
    """Wired through `consumer_lines` with `_mcp_roots()`'s own real first
    entry -- not a stand-in -- so this is the exact call #2182 filed against."""
    # The plugin root's shipped .mcp.json is read as-is; this only asserts on
    # whatever `consumer_lines` renders for it, never mutates it.
    resolved = naming.resolve({"SUPERTOOL_WATCH_NAME": "definitely-not-configured"})
    lines = channel.consumer_lines(resolved, roots=[channel._PLUGIN_ROOT])
    blob = "\n".join(lines)
    assert blob, "the shipped .mcp.json declares claude-channel with no env " \
                  "block, so this must render an `inherits` line"
    assert f"({channel._ROOT_LABEL_PLUGIN})" in blob, lines
    assert f"({channel._ROOT_LABEL_CALLER})" not in blob, lines


def test_a_line_from_a_project_directory_is_labelled_as_the_callers(tmp_path) -> None:
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_NAME": "other"})
    lines = channel.consumer_lines(
        naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}), roots=[root])
    blob = "\n".join(lines)
    assert f"({channel._ROOT_LABEL_CALLER})" in blob, lines
    assert f"({channel._ROOT_LABEL_PLUGIN})" not in blob, lines


def test_both_roots_together_are_labelled_distinctly(tmp_path) -> None:
    """Two directories, both differing from the resolved channel, rendered in
    one call: `consumer_lines` returns `differed` in full, so both labels
    must survive together -- the reader must be able to tell which line is
    about which file without reading either path by hand. Deliberately not
    mixing in the real plugin root here: `consumer_lines` drops `inherits`
    lines entirely once anything differs (existing behaviour, unrelated to
    #2184), so a mixed inherits+differs fixture would test that suppression
    rather than the label -- covered separately below and above."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = _mcp(tmp_path / "a", {"SUPERTOOL_WATCH_NAME": "alpha"})
    second = _mcp(tmp_path / "b", {"SUPERTOOL_WATCH_NAME": "beta"})
    lines = channel.consumer_lines(
        naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}), roots=[first, second])
    blob = "\n".join(lines)
    assert blob.count(f"({channel._ROOT_LABEL_CALLER})") == 2, lines
    # Neither root here IS the real plugin root, so neither may render as the
    # plugin's-own label -- the identity check from the other direction,
    # confirming position (index 0) alone never earns it.
    assert f"({channel._ROOT_LABEL_PLUGIN})" not in blob, lines


def test_agreement_is_labelled_too(tmp_path) -> None:
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_NAME": "oss"})
    lines = channel.consumer_lines(
        naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}), roots=[root])
    blob = "\n".join(lines)
    assert "agree" in blob, lines
    assert f"({channel._ROOT_LABEL_CALLER})" in blob, lines


def test_an_unread_config_is_labelled_when_it_is_reported(tmp_path) -> None:
    """`consumer config NOT checked` only renders when a name or non-default
    socket is in play -- reached here the same way the existing #1477 test
    for the unlabelled line does."""
    (tmp_path / ".mcp.json").write_text("{ not json", encoding="utf-8")
    lines = channel.consumer_lines(
        naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}), roots=[tmp_path])
    blob = "\n".join(lines)
    assert "NOT checked" in blob, lines
    assert f"({channel._ROOT_LABEL_CALLER})" in blob, lines


def test_a_line_never_carries_both_labels_at_once(tmp_path) -> None:
    """A must-not-fire sanity check on the label text itself -- the two
    labels share no words that could make one line satisfy both substring
    checks above by accident."""
    assert channel._ROOT_LABEL_PLUGIN != channel._ROOT_LABEL_CALLER
    assert channel._ROOT_LABEL_CALLER not in channel._ROOT_LABEL_PLUGIN
    assert channel._ROOT_LABEL_PLUGIN not in channel._ROOT_LABEL_CALLER


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2184)
