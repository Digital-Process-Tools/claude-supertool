"""Locate the installed `oss` plugin's scripts directory (#1985).

`claude-oss` declares supertool as a dependency and this repository has no
symmetrical reach into it: `_find_preset_file` only ever looks in the
project's own `presets/`, `~/.config/supertool/presets/` or supertool's own
install directory -- none of them is `${CLAUDE_PLUGIN_ROOT}`. So a preset
that wants to run one of `claude-oss`'s own scripts has to find them the
same way `claude-oss`'s own `doctor.py:plugin_supertool_entries` /
`dependency_install_roots` resolve supertool's install: read the plugin
marketplace's install record for the *active* version, then the record's
own `installPath` for that version, falling back to a cache-directory glob
for an install record that predates the field.

Three states, never two -- a plugin that could not be resolved must not
render like one that resolved to nothing:

* ``resolved``              -- (version, scripts_dir); scripts_dir exists.
* ``resolved-but-different``-- an active version was found and a root
  resolved for it, but that root carries no `scripts/` directory.
* ``could-not-resolve``     -- no active version, or no root for it.
"""
import json
import os
from pathlib import Path

#: Where Claude Code records which version of each installed plugin is
#: active. NOT the cache directory listing -- old versions stay unpacked on
#: disk and more than one marketplace can carry the same plugin name, so a
#: glob across the cache answers "what was ever unpacked", never "what is
#: running" (the same distinction `claude-oss`'s own `active_versions`
#: docstring draws).
INSTALL_RECORD = "~/.claude/plugins/installed_plugins.json"

PLUGIN_CACHE_ROOT = "~/.claude/plugins/cache"

PLUGIN_NAME = "oss"


def _load_record(record=None):
    """`(doc, reason)`. `doc` is `{}` on failure; `reason` is `None` on
    success, else a string naming what actually went wrong reading or
    parsing the file -- an unreadable record must not render like one that
    is simply absent (#2638)."""
    path = Path(os.path.expanduser(record or INSTALL_RECORD))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, str(exc.strerror or exc.__class__.__name__)
    try:
        return json.loads(text), None
    except ValueError as exc:
        return {}, "not valid JSON ({})".format(exc)


def _version_key(version):
    """Numeric sort key for a dotted version string, e.g. `"0.9.0"`.

    A plain `sorted(strings)` is lexicographic: `"0.9.0" > "0.10.0"` as
    strings, so the highest-scope version selection below would silently
    pick the OLDER release the moment two versions straddle a
    single-digit/double-digit boundary in any segment -- already true today
    for any `0.40.x` line the moment two patch releases straddle `0.40.9`/
    `0.40.10` (#1985 self-review). Split on `.` and compare as ints; a
    non-numeric segment falls back to the raw string so a malformed version
    still sorts (last, deterministically) rather than raising.
    """
    parts = []
    for segment in str(version).split("."):
        try:
            parts.append((0, int(segment)))
        except ValueError:
            parts.append((1, segment))
    return tuple(parts)


def _active_version(name, record=None):
    """`(version, reason)`. `version` is `None` for both "not in the
    record" and "record could not be read" -- `reason` is what tells them
    apart: `None` for the former, the read/parse failure for the latter
    (#2638). One entry per scope is possible; the highest wins, matching
    `claude-oss`'s own `active_versions` (the scope that wins at load).
    Compared numerically via `_version_key`, never as plain strings.
    """
    doc, reason = _load_record(record)
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, dict):
        return None, reason
    versions = []
    for key, entries in plugins.items():
        if key.split("@", 1)[0] != name or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("version"):
                versions.append(str(entry["version"]))
    if not versions:
        return None, reason
    return sorted(versions, key=_version_key)[-1], None


def _install_roots(name, version, record=None, cache_root=None):
    """`(roots, reason)`. `reason` is `None` on success, or the reason no
    root could be resolved -- either the install record's own read/parse
    failure (propagated from `_load_record`) or an `OSError` scanning the
    cache-directory fallback (#2638)."""
    doc, reason = _load_record(record)
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    roots = []
    for key, entries in (plugins or {}).items() if isinstance(plugins, dict) else ():
        if key.split("@", 1)[0] != name or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("version") != version:
                continue
            if entry.get("installPath"):
                roots.append(Path(str(entry["installPath"])))
    if roots:
        return list(dict.fromkeys(roots)), None
    cache = Path(os.path.expanduser(cache_root or PLUGIN_CACHE_ROOT))
    try:
        found = sorted(
            p for p in cache.glob("*/{}/{}".format(name, version)) if p.is_dir()
        )
    except OSError as exc:
        return [], str(exc.strerror or exc.__class__.__name__)
    if found:
        return found, None
    return [], reason


def resolve(name=PLUGIN_NAME, record=None, cache_root=None):
    """`(state, detail)`.

    `detail` is `(version, scripts_dir)` on `"resolved"`; a one-line reason
    string on `"resolved-but-different"` and `"could-not-resolve"`.
    """
    version, reason = _active_version(name, record)
    if not version:
        if reason:
            return "could-not-resolve", (
                "{}'s install record could not be read -- {}".format(name, reason)
            )
        return "could-not-resolve", "{} is not in the install record".format(name)

    roots, reason = _install_roots(name, version, record=record, cache_root=cache_root)
    if not roots:
        if reason:
            return "could-not-resolve", (
                "{} {} is active, but its install path could not be resolved -- "
                "{}".format(name, version, reason)
            )
        return "could-not-resolve", (
            "{} {} is active, but its install path could not be resolved".format(
                name, version
            )
        )

    scripts_dir = roots[0] / "scripts"
    if not scripts_dir.is_dir():
        return "resolved-but-different", (
            "{} {} resolved to {}, but it carries no scripts/ directory".format(
                name, version, roots[0]
            )
        )

    return "resolved", (version, scripts_dir)
