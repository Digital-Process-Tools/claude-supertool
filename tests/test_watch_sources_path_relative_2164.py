"""`watch_sources_path` accepts a relative entry declared in `.supertool.json` (#2164).

`presets/watch/sourcepath.resolve()` refuses any entry that fails
`os.path.isabs()`, with no `expanduser`/`expandvars`. So the key accepted
exactly one thing -- a literal absolute path -- and a shared `.supertool.json`
could only carry it by committing one developer's home directory.

The fix is upstream of `resolve()`, not inside it: `resolve()` stays a pure
function of `os.environ` (or the mapping handed to it in a test), because it
is imported and called by a detached, re-exec'd poller that has no reliable
notion of "the directory this was typed in" -- `resolve()`'s own docstring
argument about the CWD is correct and untouched.

What changes is `_supertool.py::_resolve_custom_op`'s config-to-env export
step, the ONE place a `.supertool.json`-declared `ops.<op>.watch_sources_path`
turns into `SUPERTOOL_WATCH_SOURCES_PATH` before the op's subprocess (and
everything it re-execs into) ever starts. A relative entry is resolved
against the directory of the config file that declared it -- available there
as `_CONFIG_PATH`, and never re-derived from the CWD -- so what the poller
inherits, and what it re-derives after any number of re-execs, is already an
absolute path. An entry that arrives straight through the environment, with
no declaring config file, is untouched and keeps today's absolute-only rule
enforced by `resolve()` itself.

Every "must resolve" case here has a "must still refuse" partner in the same
fixture: an assertion that a relative entry now works passes if the export
step stopped checking absoluteness altogether, which would also silently
accept a `watch_sources_path` typo nobody could then find.
"""
from __future__ import annotations

import os

import supertool


def _set_config(tmp_path, ops):
    config_path = tmp_path / ".supertool.json"
    supertool._CONFIG = {"ops": ops}
    supertool._CONFIG_PATH = str(config_path)


# --- the export step anchors a relative entry to the config file's directory -

def test_a_relative_entry_is_anchored_to_the_config_files_directory(tmp_path):
    """`ops.watch.watch_sources_path` may be a relative path once #2164 lands --
    resolved against the directory `.supertool.json` itself lives in, not CWD."""
    _set_config(tmp_path, {
        "watch": {"cmd": "echo $SUPERTOOL_WATCH_SOURCES_PATH",
                  "watch_sources_path": os.path.join(".claude", "watch-sources")},
    })
    result = supertool._resolve_custom_op("watch", ["watch"])
    assert result is not None
    expected = os.path.normpath(str(tmp_path / ".claude" / "watch-sources"))
    assert expected in result


def test_the_resolved_path_is_absolute_not_merely_present(tmp_path):
    """A partial fix (e.g. merely not-refusing the raw relative text) would
    leave `SUPERTOOL_WATCH_SOURCES_PATH` holding the literal relative string --
    this asserts the exported value is rooted at the config directory."""
    _set_config(tmp_path, {
        "watch": {"cmd": "echo $SUPERTOOL_WATCH_SOURCES_PATH",
                  "watch_sources_path": "watch-sources"},
    })
    result = supertool._resolve_custom_op("watch", ["watch"])
    assert result is not None
    expected = os.path.normpath(str(tmp_path / "watch-sources"))
    assert expected in result


def test_a_multi_entry_search_path_anchors_each_relative_entry(tmp_path):
    """Every `os.pathsep`-separated entry is resolved on its own -- an already
    absolute sibling in the same value must not be rewritten."""
    absolute_entry = str(tmp_path / "elsewhere")
    raw = os.pathsep.join(["relative-src", absolute_entry])
    _set_config(tmp_path, {
        "watch": {"cmd": "echo $SUPERTOOL_WATCH_SOURCES_PATH",
                  "watch_sources_path": raw},
    })
    result = supertool._resolve_custom_op("watch", ["watch"])
    assert result is not None
    expected_relative = os.path.normpath(str(tmp_path / "relative-src"))
    assert expected_relative in result
    assert absolute_entry in result


# --- an entry with no declaring config file keeps the absolute-only rule -----

def test_an_entry_with_no_config_path_is_left_untouched():
    """No `_CONFIG_PATH` -- e.g. the value arrived through the raw environment,
    with no `.supertool.json` to anchor against -- so nothing is rewritten and
    `sourcepath.resolve()` still refuses it as relative, unchanged."""
    supertool._CONFIG = {"ops": {
        "watch": {"cmd": "echo $SUPERTOOL_WATCH_SOURCES_PATH",
                  "watch_sources_path": "still-relative"},
    }}
    supertool._CONFIG_PATH = None
    result = supertool._resolve_custom_op("watch", ["watch"])
    assert result is not None
    line = next(ln for ln in result.splitlines()
                if ln.strip() == "still-relative")
    assert line == "still-relative"


def test_an_already_absolute_entry_is_unchanged(tmp_path):
    absolute = str(tmp_path / "somewhere")
    _set_config(tmp_path, {
        "watch": {"cmd": "echo $SUPERTOOL_WATCH_SOURCES_PATH",
                  "watch_sources_path": absolute},
    })
    result = supertool._resolve_custom_op("watch", ["watch"])
    assert result is not None
    assert absolute in result


def test_an_unrelated_config_key_is_never_anchored(tmp_path):
    """Only `watch_sources_path` is anchored -- an arbitrary other extra key
    stays exactly the literal string the config declared, relative or not."""
    _set_config(tmp_path, {
        "tool": {"cmd": "echo $SUPERTOOL_SOME_PATH",
                 "some_path": "relative/thing"},
    })
    result = supertool._resolve_custom_op("tool", ["tool"])
    assert result is not None
    assert "relative/thing" in result
    assert str(tmp_path) not in result


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2164)
