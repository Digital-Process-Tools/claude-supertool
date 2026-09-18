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
    path = Path(os.path.expanduser(record or INSTALL_RECORD))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _active_version(name, record=None):
    """The version actually enabled for `name`, or `None`.

    One entry per scope is possible; the highest wins, matching
    `claude-oss`'s own `active_versions` (the scope that wins at load).
    """
    doc = _load_record(record)
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, dict):
        return None
    versions = []
    for key, entries in plugins.items():
        if key.split("@", 1)[0] != name or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("version"):
                versions.append(str(entry["version"]))
    if not versions:
        return None
    return sorted(versions)[-1]


def _install_roots(name, version, record=None, cache_root=None):
    doc = _load_record(record)
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
        return list(dict.fromkeys(roots))
    cache = Path(os.path.expanduser(cache_root or PLUGIN_CACHE_ROOT))
    try:
        return sorted(
            p for p in cache.glob("*/{}/{}".format(name, version)) if p.is_dir()
        )
    except OSError:
        return []


def resolve(name=PLUGIN_NAME, record=None, cache_root=None):
    """`(state, detail)`.

    `detail` is `(version, scripts_dir)` on `"resolved"`; a one-line reason
    string on `"resolved-but-different"` and `"could-not-resolve"`.
    """
    version = _active_version(name, record)
    if not version:
        return "could-not-resolve", "{} is not in the install record".format(name)

    roots = _install_roots(name, version, record=record, cache_root=cache_root)
    if not roots:
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
