#!/usr/bin/env python3


































































from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))        
sys.path.insert(0, str(Path(__file__).parent.parent))  

import _untrusted  
import naming  
import transport  














RESERVED_NAMES = (transport.PROBE_SOURCE,)

PATH_ENV = "SUPERTOOL_WATCH_SOURCES_PATH"






CONFIG_KEY = "watch_sources_path"




WATCH_OPS = naming.WATCH_OPS

SHIPPED_DIR = Path(__file__).parent / "sources"




SHIPPED = "shipped"


class Refused(NamedTuple):


    entry: str
    why: str


class Resolved(NamedTuple):








    shipped: Path
    external: tuple[Path, ...]
    refused: tuple[Refused, ...]


    raw: str




    redundant: tuple[str, ...] = ()


def resolve(env: dict[str, str] | None = None) -> Resolved:







    src = os.environ if env is None else env
    raw = src.get(PATH_ENV) or ""

    external: list[Path] = []
    refused: list[Refused] = []
    redundant: list[str] = []



    seen = {os.path.normcase(os.path.normpath(str(SHIPPED_DIR)))}

    for raw_entry in raw.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:



            continue
        if not os.path.isabs(entry):
            refused.append(Refused(entry, (
                "is a relative path -- a poller is detached, re-execs and then "
                "runs for days, and `radar` re-derives this path from wherever "
                "it is invoked, so the directory a relative entry resolves "
                "against is not the one you typed it in. Give an absolute path")))
            continue
        key = os.path.normcase(os.path.normpath(entry))
        if key in seen:
            redundant.append(entry)
            continue
        seen.add(key)
        try:
            is_dir = stat.S_ISDIR(os.stat(entry).st_mode)
        except OSError as err:





            refused.append(Refused(entry, f"could not be reached ({type(err).__name__})"))
            continue
        if not is_dir:
            refused.append(Refused(entry, "is not a directory"))
            continue
        external.append(Path(entry))

    return Resolved(shipped=SHIPPED_DIR, external=tuple(external),
                    refused=tuple(refused), raw=raw,
                    redundant=tuple(redundant))


def _is_one_component(name: str) -> bool:






    if name in ("", ".", ".."):
        return False
    return os.path.basename(name) == name and "/" not in name


def find(name: str, resolved: Resolved | None = None) -> tuple[Path | None, str]:











    resolved = resolve() if resolved is None else resolved
    if not _is_one_component(name) or name in RESERVED_NAMES:
        return None, ""
    candidate = Path(resolved.shipped) / name / "poller.py"
    if candidate.is_file():
        return candidate, SHIPPED
    for directory in resolved.external:
        candidate = Path(directory) / name / "poller.py"
        if candidate.is_file():
            return candidate, str(directory)
    return None, ""













TIER_FILE = "tier.py"


def find_tier(name: str, resolved: Resolved | None = None) -> tuple[Path | None, str]:










    resolved = resolve() if resolved is None else resolved
    if not _is_one_component(name):
        return None, ""
    for directory in resolved.external:
        candidate = Path(directory) / name / TIER_FILE
        if candidate.is_file():
            return candidate, str(directory)
    return None, ""


def tier_names_in(directory: Path | str) -> tuple[tuple[str, ...], str]:







    entries, state, why = naming.state_dir_listing(str(directory))
    if state == naming.STATE_DIR_UNREADABLE:
        return (), why
    if state == naming.STATE_DIR_ABSENT:
        return (), (f"{naming.flat_path(str(directory))} is gone -- it was a "
                    f"directory when the search path was resolved")
    return tuple(n for n in entries
                 if (Path(directory) / n / TIER_FILE).is_file()), ""


def tier_search_report(resolved: Resolved) -> list[str]:







    lines: list[str] = []
    for directory in resolved.external:
        names, why = tier_names_in(directory)
        lines.append(f"  {naming.flat_path(str(directory))} ({PATH_ENV}): "
                     + (_flat_names(names) or why or f"(no {TIER_FILE})"))
    for refusal in resolved.refused:
        lines.append(f"  {naming.flat_path(refusal.entry)} ({PATH_ENV}): "
                     f"NOT searched -- {refusal.why}")
    if not resolved.raw.strip():
        lines.append(f"  {PATH_ENV} is not set, so nothing outside the plugin "
                     f"was searched. Set it to a directory holding "
                     f"<name>/{TIER_FILE} to add your own tier.")
    return lines


def names_in(directory: Path | str) -> tuple[tuple[str, ...], str]:














    entries, state, why = naming.state_dir_listing(str(directory))
    if state == naming.STATE_DIR_UNREADABLE:
        return (), why
    if state == naming.STATE_DIR_ABSENT:


        return (), (f"{naming.flat_path(str(directory))} is gone -- it was a "
                    f"directory when the search path was resolved")
    return tuple(n for n in entries
                 if (Path(directory) / n / "poller.py").is_file()), ""


def shadowed(resolved: Resolved) -> tuple[tuple[str, Path], ...]:







    shipped_names, _why = names_in(resolved.shipped)
    out: list[tuple[str, Path]] = []
    for directory in resolved.external:
        names, _ = names_in(directory)
        for name in names:
            if name in shipped_names:
                out.append((name, Path(directory)))
    return tuple(out)


def reserved_hits(resolved: Resolved) -> tuple[tuple[str, Path], ...]:








    out: list[tuple[str, Path]] = []
    for directory in resolved.external:
        names, _ = names_in(directory)
        for name in names:
            if name in RESERVED_NAMES:
                out.append((name, Path(directory)))
    return tuple(out)


def _flat_names(names: tuple[str, ...]) -> str:

    return ", ".join(_untrusted.flat(n, disclose_newline=True) for n in names)


def search_report(resolved: Resolved) -> list[str]:







    lines: list[str] = []
    names, why = names_in(resolved.shipped)
    lines.append(f"  {naming.flat_path(str(resolved.shipped))} (shipped): "
                 + (_flat_names(names) or why or "(no sources)"))
    for directory in resolved.external:
        names, why = names_in(directory)
        lines.append(f"  {naming.flat_path(str(directory))} ({PATH_ENV}): "
                     + (_flat_names(names) or why or "(no sources)"))
    for refusal in resolved.refused:
        lines.append(f"  {naming.flat_path(refusal.entry)} ({PATH_ENV}): "
                     f"NOT searched -- {refusal.why}")
    if not resolved.raw.strip():
        lines.append(f"  {PATH_ENV} is not set, so nothing outside the plugin "
                     f"was searched. Set it to a directory holding "
                     f"<name>/poller.py to add your own.")
    return lines


def disclosure_lines(resolved: Resolved) -> list[str]:






    if not resolved.raw.strip() and not resolved.refused:
        return []
    out: list[str] = []
    if resolved.external:
        out.append("source search path (from " + PATH_ENV + "): "
                   + ", ".join(naming.flat_path(str(d)) for d in resolved.external))
    for refusal in resolved.refused:
        out.append(f"{PATH_ENV} entry {naming.flat_path(refusal.entry)} is NOT "
                   f"searched -- it {refusal.why}")
    for entry in resolved.redundant:
        out.append(f"{PATH_ENV} entry {naming.flat_path(entry)} names a "
                   f"directory already on the path (the shipped one is always "
                   f"searched first), so it adds nothing")
    for name, directory in shadowed(resolved):
        out.append(f"source {_untrusted.flat(name, disclose_newline=True)} in "
                   f"{naming.flat_path(str(directory))} is shadowed by the "
                   f"shipped source of the same name and was NOT loaded -- "
                   f"shipped sources always win")
    for name, directory in reserved_hits(resolved):
        out.append(f"source {_untrusted.flat(name, disclose_newline=True)} in "
                   f"{naming.flat_path(str(directory))} was NOT loaded -- that "
                   f"name is reserved (#1593) so that a probe event stays "
                   f"distinguishable from a watcher's")
    if (resolved.raw.strip() and not resolved.external
            and not resolved.refused and not resolved.redundant):




        out.append(f"{PATH_ENV} is set and names no usable directory, so only "
                   f"the shipped sources are available")
    return out


def config_notes(op: str, start_dir: str | None = None) -> list[str]:













    state, path, declaring, why = naming.declared_op_values(CONFIG_KEY, start_dir)
    if state == naming.DECLARED_UNREADABLE:







        where = _untrusted.flat(path, disclose_newline=True)
        return [f"{where or naming.CONFIG_NAME} could not be read ({why}), so "
                f"whether {CONFIG_KEY} is declared for every watch op is "
                f"unknown -- not no"]
    if state != naming.DECLARED_FOUND or not declaring:
        return []
    silent = tuple(sorted(set(WATCH_OPS) - set(declaring)))
    if not silent:
        return []
    where = _untrusted.flat(path, disclose_newline=True)
    return [f"{CONFIG_KEY} is declared in {where} for "
            f"{', '.join(sorted(declaring))} but not for "
            f"{', '.join(silent)} -- {PATH_ENV} reaches only the ops whose own "
            f"block declares it, so a source those ops can start is one the "
            f"others cannot find"
            + (f" (this op is {op})" if op in silent else "")]


def op_lines(op: str, resolved: Resolved | None = None) -> list[str]:





    resolved = resolve() if resolved is None else resolved
    return disclosure_lines(resolved) + config_notes(op)
