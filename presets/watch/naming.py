#!/usr/bin/env python3













































from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))  

import _image_root  
import _untrusted  

NAME_ENV = "SUPERTOOL_WATCH_NAME"
SOCK_ENV = "SUPERTOOL_WATCH_SOCK"
STATE_DIR_ENV = "SUPERTOOL_WATCH_STATE_DIR"







ROOT_ENV = "SUPERTOOL_WATCH_NAME_ROOT"





BASE_DIR = "/tmp"
DEFAULT_SOCK = f"{BASE_DIR}/supertool-watch.sock"
DEFAULT_STATE_DIR = BASE_DIR












NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")


class Resolved(NamedTuple):








    name: str
    sock: str
    state_dir: str
    notes: list[str]
    refusal: str















    state_dir_is_derived: bool = False




    state_dir_env_set: bool = False


def sock_for(name: str) -> str:
    return f"{BASE_DIR}/supertool-watch-{name}.sock"


def state_dir_for(name: str) -> str:
    return f"{BASE_DIR}/supertool-watch-{name}"


def flat_path(path: str) -> str:


















    return _untrusted.flat(path, disclose_newline=True)


def resolve(overrides: dict[str, str] | None = None) -> Resolved:















    if overrides is not None:
        raw = (overrides.get("SUPERTOOL_WATCH_NAME") or "").strip()
        explicit_sock = overrides.get("SUPERTOOL_WATCH_SOCK") or ""
        explicit_state = overrides.get("SUPERTOOL_WATCH_STATE_DIR") or ""
    else:
        raw = (os.environ.get("SUPERTOOL_WATCH_NAME") or "").strip()
        explicit_sock = os.environ.get("SUPERTOOL_WATCH_SOCK") or ""
        explicit_state = os.environ.get("SUPERTOOL_WATCH_STATE_DIR") or ""

    notes: list[str] = []
    refusal = ""
    name = ""
    if raw:
        if NAME_RE.match(raw):
            name = raw
        else:
            refusal = (
                f"{NAME_ENV}={_untrusted.flat(raw)!r} is not usable as a path "
                f"component and was ignored — it must match {NAME_RE.pattern}. "
                f"This channel is on the default paths, not a private one."
            )

    sock = sock_for(name) if name else DEFAULT_SOCK
    state_dir = state_dir_for(name) if name else DEFAULT_STATE_DIR







    if explicit_sock:
        if name and explicit_sock != sock:
            notes.append(
                f"{SOCK_ENV} is set and overrides the name: the socket is "
                f"{flat_path(explicit_sock)}, not {sock}")
        sock = explicit_sock
    if explicit_state:
        if name and explicit_state != state_dir:
            notes.append(
                f"{STATE_DIR_ENV} is set and overrides the name: poller slots are "
                f"in {flat_path(explicit_state)}, not {state_dir}")
        state_dir = explicit_state

    if not name and bool(explicit_sock) != bool(explicit_state):



        missing = STATE_DIR_ENV if explicit_sock else SOCK_ENV
        notes.append(
            f"{missing} is NOT set while its partner is — the half-configured "
            f"state that is worse than configuring neither (#1309). "
            f"{NAME_ENV} sets both from one word.")

    return Resolved(name=name, sock=sock, state_dir=state_dir,
                    notes=notes, refusal=refusal,
                    state_dir_is_derived=bool(name) and state_dir == state_dir_for(name),
                    state_dir_env_set=bool(explicit_state))


def state_dir_provenance(resolved: Resolved) -> str:








    if resolved.state_dir_is_derived:
        if resolved.state_dir_env_set:



            return (f"{STATE_DIR_ENV} is set to the path {NAME_ENV}="
                    f"{resolved.name} derives")
        return (f"{STATE_DIR_ENV} is not set — this directory was derived from "
                f"{NAME_ENV}={resolved.name}")
    if resolved.state_dir != DEFAULT_STATE_DIR:
        return f"set by {STATE_DIR_ENV}"
    return (f"the default; {NAME_ENV} or {STATE_DIR_ENV} would move it")













STATE_DIR_OK = "ok"
STATE_DIR_ABSENT = "absent"
STATE_DIR_UNREADABLE = "unreadable"


def state_dir_listing(state_dir: str) -> tuple[list[str], str, str]:

















    try:
        return sorted(os.listdir(state_dir)), STATE_DIR_OK, ""
    except FileNotFoundError:
        return [], STATE_DIR_ABSENT, ""
    except OSError as err:





        return [], STATE_DIR_UNREADABLE, (
            f"{flat_path(state_dir)} could not be listed ({type(err).__name__})")


def state_dir_absence_note(state_dir: str, state: str, why: str) -> str:





    if state == STATE_DIR_ABSENT:
        return (f"the state directory {flat_path(state_dir)} does not exist yet, "
                f"so nothing has ever spawned on this channel")
    if state == STATE_DIR_UNREADABLE:
        return f"{why}, so nothing here is evidence of absence"
    return ""










WATCH_OPS = ("channel", "radar", "unwatch", "watch", "watches")

CONFIG_NAME = ".supertool.json"







DECLARED_FOUND = "found"
DECLARED_SILENT = "silent"
DECLARED_NO_CONFIG = "no-config"
DECLARED_UNREADABLE = "unreadable"


class Declared(NamedTuple):










    state: str

    path: str


    names: tuple[str, ...]

    declaring_ops: tuple[str, ...]


    silent_ops: tuple[str, ...]

    why: str = ""


def find_config(start_dir: str | None = None) -> tuple[str, str]:
















    here = os.path.abspath(start_dir if start_dir is not None else os.getcwd())
    while True:
        candidate = os.path.join(here, CONFIG_NAME)
        try:
            if stat.S_ISREG(os.stat(candidate).st_mode):
                return candidate, ""
        except (FileNotFoundError, NotADirectoryError):


            pass
        except OSError as err:
            return "", (f"{_untrusted.flat(candidate, disclose_newline=True)} "
                        f"could not be reached ({type(err).__name__})")
        parent = os.path.dirname(here)
        if parent == here:
            return "", ""
        here = parent


def declared_op_values(key: str, start_dir: str | None = None
                       ) -> tuple[str, str, dict[str, str], str]:












    path, unreachable = find_config(start_dir)
    if unreachable:
        return DECLARED_UNREADABLE, "", {}, unreachable
    if not path:
        return DECLARED_NO_CONFIG, "", {}, ""
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as err:
        return DECLARED_UNREADABLE, path, {}, type(err).__name__
    if not isinstance(doc, dict):
        return (DECLARED_UNREADABLE, path, {},
                f"top level is {type(doc).__name__}, not an object")
    ops = doc.get("ops")
    blocks = ops if isinstance(ops, dict) else {}
    declaring = {
        op: block[key]
        for op, block in blocks.items()
        if isinstance(block, dict) and isinstance(block.get(key), str)
        and block[key]
    }
    return (DECLARED_FOUND if declaring else DECLARED_SILENT), path, declaring, ""


def declared_names(start_dir: str | None = None) -> Declared:






    state, path, declaring, why = declared_op_values("watch_name", start_dir)
    if state == DECLARED_UNREADABLE:
        return Declared(state=state, path=path, names=(),
                        declaring_ops=(), silent_ops=(), why=why)
    if state == DECLARED_NO_CONFIG:
        return Declared(state=state, path="", names=(),
                        declaring_ops=(), silent_ops=tuple(WATCH_OPS))
    silent = tuple(sorted(set(WATCH_OPS) - set(declaring)))
    if not declaring:
        return Declared(state=DECLARED_SILENT, path=path, names=(),
                        declaring_ops=(), silent_ops=silent)
    return Declared(state=DECLARED_FOUND, path=path,
                    names=tuple(sorted(set(declaring.values()))),
                    declaring_ops=tuple(sorted(declaring)),
                    silent_ops=silent)


def _flat_list(values: tuple[str, ...]) -> str:
    return ", ".join(_untrusted.flat(v, disclose_newline=True) for v in values)


def project_notes(resolved: Resolved, declared: Declared | None,
                  overrides: dict[str, str] | None = None) -> list[str]:




















    if declared is None or not resolved.name:












        return []
    name = _untrusted.flat(resolved.name)

    out: list[str] = []
    where = _untrusted.flat(declared.path, disclose_newline=True)
    if declared.state == DECLARED_UNREADABLE:



        what = (f"{where} could not be read ({declared.why})" if where
                else declared.why)
        return [f"{what}, so which project claims {name} is unknown — "
                f"not unclaimed"]
    if declared.state == DECLARED_NO_CONFIG:
        return [f"no {CONFIG_NAME} at or above this directory, so nothing here "
                f"claims the name {name} — this socket and these poller slots "
                f"may be another project's fleet"]
    if declared.state == DECLARED_SILENT:


        if overrides is not None:
            root = (overrides.get("SUPERTOOL_WATCH_NAME_ROOT") or "").strip()
        else:
            root = (os.environ.get("SUPERTOOL_WATCH_NAME_ROOT") or "").strip()
        if root and declared.path and os.path.abspath(root.rstrip(os.sep)) == \
                os.path.dirname(os.path.abspath(declared.path)):






            return [f"{name} came from the environment, but {ROOT_ENV} "
                    f"says it was derived for this directory — not another "
                    f"project's fleet"]
        return [f"{where} declares no watch_name in any op block, so {name} came "
                f"from the environment — this socket and these poller slots may "
                f"be another project's fleet"]

    theirs = _flat_list(declared.names)
    if resolved.name not in declared.names:
        out.append(f"{name} is in force but {where} declares {theirs} — this is "
                   f"not this project's channel, and its socket and poller slots "
                   f"are shared with whatever exported {NAME_ENV}")
    elif len(declared.names) > 1:
        out.append(f"op blocks in {where} disagree about watch_name ({theirs}) — "
                   f"{name} is in force, so the ops declaring the others are on "
                   f"a channel this board is not about")
    else:
        out.append(f"name {name} is declared by {where} "
                   f"(ops: {_flat_list(declared.declaring_ops)}) — this "
                   f"project's own channel")

    if declared.silent_ops:
        out.append(f"watch ops declaring no watch_name: "
                   f"{_flat_list(declared.silent_ops)} — those resolve from the "
                   f"environment alone, so this project's board and its pollers "
                   f"can end up on different channels")
    return out


def disclosure_lines(resolved: Resolved,
                     declared: Declared | None = None) -> list[str]:
























    out: list[str] = []
    if resolved.name:
        out.append(
            f"name {_untrusted.flat(resolved.name)} (from {NAME_ENV}) — socket "
            f"{flat_path(resolved.sock)}, poller slots "
            f"{flat_path(resolved.state_dir)}")
    if resolved.refusal:
        out.append(resolved.refusal)
    out.extend(resolved.notes)
    out.extend(project_notes(resolved, declared))
    return out


def ensure_state_dir(resolved: Resolved, state_dir: str) -> str:























































































    if not resolved.state_dir_is_derived:
        return ""
    _root, why = _image_root.ensure(state_dir)
    if why:
        return f"{why} — no poller slot can be claimed there"
    return ""
