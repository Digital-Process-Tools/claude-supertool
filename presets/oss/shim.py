




















import json
import os
from pathlib import Path







INSTALL_RECORD = "~/.claude/plugins/installed_plugins.json"

PLUGIN_CACHE_ROOT = "~/.claude/plugins/cache"

PLUGIN_NAME = "oss"


def _load_record(record=None):






    path = Path(os.path.expanduser(record or INSTALL_RECORD))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, str(exc.strerror or exc.__class__.__name__)
    try:
        return json.loads(text), None
    except ValueError as exc:
        return {}, "not valid JSON ({})".format(exc)


def _version_key(version):











    parts = []
    for segment in str(version).split("."):
        try:
            parts.append((0, int(segment)))
        except ValueError:
            parts.append((1, segment))
    return tuple(parts)


def _active_version(name, record=None):







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
