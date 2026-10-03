#!/usr/bin/env python3
















































































from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

_HERE = Path(__file__).parent

sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))  
import _untrusted  
import channel  
import dispatcher  
import sourcepath  
import transport  


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module




TIERS_ENV = "SUPERTOOL_RADAR_TIERS"



RESERVED_PREFIX = "_"

NO_TIERS = ('radar: no tiers configured. Add ops.radar.radar_tiers to .supertool.json —\n'
            '       e.g. {"gl-mrs": {}} for the GitLab MR board.')


def ensure_watcher(source: str, scope: str, only: list[str] | None = None) -> str:


















    if dispatcher._load_source(source) is None:
        return "failed"
    if len(transport.deaths(source, scope)) >= transport.DEATH_RESPAWN_LIMIT:
        return "capped"
    return dispatcher.start_poller(source, scope, only or [])[0]


def watcher_cap_warnings(statuses: dict[str, str]) -> list[str]:





    return [
        f"radar: WARNING — stopped respawning {name}: it has died "
        f"{transport.DEATH_RESPAWN_LIMIT}+ times and nothing is polling it. "
        f"Fix it, then re-arm with `watch:{name}`."
        for name, status in sorted(statuses.items()) if status == "capped"
    ]







TIERS_ARG_PREFIX = "tiers="


def split_tiers_arg(arg: str) -> tuple[list[str] | None, str]:


















    arg = arg.strip()
    if not arg.startswith(TIERS_ARG_PREFIX):
        return None, arg
    names = [n.strip() for n in arg[len(TIERS_ARG_PREFIX):].split(",") if n.strip()]
    return names, ""


def select_tiers(tiers: dict[str, dict],
                  names: list[str] | None) -> tuple[dict[str, dict], list[str]]:





















    if names is None:
        return tiers, []
    if not names:
        return {}, [f"radar: WARNING — tiers= named no tiers at all (nothing after "
                    f"the '='). Registered: {sorted(tiers)}. Nothing rendered."]
    unknown = sorted(n for n in names if n not in tiers)
    selected = {name: opts for name, opts in tiers.items() if name in names}
    lines = [f"radar: tiers= selected {sorted(selected)} of {sorted(tiers)} registered."]
    if unknown:
        lines.append(f"radar: WARNING — tiers= named {unknown}, not registered in "
                     f"ops.radar.radar_tiers ({sorted(tiers)}). Not rendered — "
                     f"check for a typo.")
    return selected, lines


def read_tiers(raw: str | None = None) -> tuple[dict[str, dict], list[str]]:












    raw = os.environ.get("SUPERTOOL_RADAR_TIERS", "") if raw is None else raw
    raw = raw.strip()
    if not raw:
        return {}, []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, [f"radar: WARNING — radar_tiers is not valid JSON ({exc.msg}). "
                    f"No tiers loaded."]
    if not isinstance(loaded, dict):
        return {}, ["radar: WARNING — radar_tiers must be an object keyed by op name, "
                    "e.g. {'gl-mrs': {}}. No tiers loaded."]

    out: dict[str, dict] = {}
    problems: list[str] = []
    for name, opts in loaded.items():
        if opts is None:
            opts = {}
        if not isinstance(opts, dict):
            problems.append(f"radar: WARNING — tier '{name}' options must be an object; "
                            f"got {type(opts).__name__}. Tier skipped.")
            continue



        reserved = sorted(k for k in opts if str(k).startswith(RESERVED_PREFIX))
        if reserved:
            problems.append(f"radar: WARNING — tier '{name}' option(s) {reserved} start "
                            f"with '{RESERVED_PREFIX}', which radar reserves for the "
                            f"context it supplies. Dropped.")
            opts = {k: v for k, v in opts.items()
                    if not str(k).startswith(RESERVED_PREFIX)}
        out[str(name)] = opts
    return out, problems


def _tier_module(name: str):


































    local = _HERE / "tiers" / f"{name.replace('-', '_')}.py"
    if local.is_file():
        return _load(f"radar_tier_{name.replace('-', '_')}", local)

    presets_dir = _HERE.parent
    for preset in sorted(presets_dir.glob("*.json")):
        try:
            with open(preset, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entry = (data.get("ops") or {}).get(name)
        if not isinstance(entry, dict):
            continue
        for token in str(entry.get("cmd") or "").split():
            if token.endswith(".py"):
                script = presets_dir / token.replace("{path}", "")
                if script.is_file():
                    return _load(f"radar_tier_{name.replace('-', '_')}", script)

    external, _origin = sourcepath.find_tier(name)
    if external is not None:
        return _load(f"radar_tier_{name.replace('-', '_')}", external)
    return None


def _ships_tier(name: str) -> bool:

    return (_HERE / "tiers" / f"{name.replace('-', '_')}.py").is_file()


def tier_shadow_lines(names: list[str] | tuple[str, ...]) -> list[str]:













    resolved = sourcepath.resolve()
    out: list[str] = []
    for name in names:
        if not _ships_tier(name):
            continue
        external, origin = sourcepath.find_tier(name, resolved)
        if external is None:
            continue
        out.append(f"radar: tier '{_untrusted.flat(name, disclose_newline=True)}' in "
                   f"{origin} was NOT loaded -- radar ships a tier of that name "
                   f"and shipped tiers always win.")
    return out


def _tier_search_lines(name: str) -> list[str]:







    resolved = sourcepath.resolve()
    lines = ["       looked in:",
             f"  {_HERE / 'tiers'}/{name.replace('-', '_')}.py (shipped)",
             f"  the script named by an op '{name}' in {_HERE.parent}/*.json"]
    lines.extend(sourcepath.tier_search_report(resolved))
    return [lines[0]] + ["       " + line for line in lines[1:]]


def _spawner() -> tuple[Callable[..., str], dict[str, str], list[str]]:




















    seen: dict[str, str] = {}
    reaped: list[str] = []
    done = False

    def watch(source: str, scope: str, only: list[str] | None = None) -> str:
        nonlocal done
        if not done:
            done = True
            reaped.extend(dispatcher.reap_duplicate_pollers())
        status = ensure_watcher(source, scope, only)
        seen[f"{source}:{scope}"] = status
        return status

    return watch, seen, reaped


def route_tier_arg(arg: str, tier_names: list[str]) -> tuple[dict[str, str], list[str]]:











































    if not arg or len(tier_names) <= 1:
        return {name: arg for name in tier_names}, []
    matched = [name for name in tier_names
               if arg == name or arg.startswith(name + ":")]
    if not matched:
        return {name: arg for name in tier_names}, []







    longest = max(len(name) for name in matched)
    matched = [name for name in matched if len(name) == longest]
    return {name: (arg if name in matched else "") for name in tier_names}, []


def tier_reports(arg: str = "") -> tuple[list[str], bool, list[str]]:

















    tier_names, arg = split_tiers_arg(arg)
    tiers, lines = read_tiers()
    tiers, sel_lines = select_tiers(tiers, tier_names)
    lines = lines + sel_lines
    watch, spawned, reaped = _spawner()
    all_ok = True
    failures: list[str] = []

    lines.extend(tier_shadow_lines(list(tiers)))
    arg_by_tier, route_lines = route_tier_arg(arg, list(tiers))
    lines.extend(route_lines)

    for name, opts in tiers.items():
        try:
            module = _tier_module(name)
        except Exception as exc:  




            failures.append(f"radar: WARNING — tier '{name}' could not be loaded: "
                            f"{exc.__class__.__name__}: {exc}")
            all_ok = False
            continue
        report = getattr(module, "radar_report", None) if module else None
        if report is None:




            why = ("could not be resolved" if module is None
                   else "exposes no radar_report()")
            failures.append("\n".join(
                [f"radar: WARNING — tier '{name}' is registered but {why}; it "
                 f"contributes nothing. Check the name."] + _tier_search_lines(name)))
            all_ok = False
            continue

        unknown = set(opts) - set(getattr(module, "RADAR_OPTIONS", set()))
        if unknown:
            lines.append(f"radar: WARNING — tier '{name}' has unknown option(s) "
                         f"{sorted(unknown)}; ignored. Check for a typo.")

        try:
            tier_lines, ok = report({**opts, "_arg": arg_by_tier[name], "_watch": watch})
        except Exception as exc:  
            failures.append(f"radar: WARNING — tier '{name}' failed: "
                            f"{exc.__class__.__name__}: {exc}")
            all_ok = False
            continue

        all_ok = all_ok and ok
        quiet = opts.get("quiet_when_healthy",
                         getattr(module, "RADAR_QUIET_DEFAULT", True))
        if ok and quiet:
            continue
        lines.extend(tier_lines)

    return reaped + watcher_cap_warnings(spawned) + lines, all_ok, failures


def tier_states(arg: str = "") -> tuple[list[str], list[str]]:












    tier_names, arg = split_tiers_arg(arg)
    tiers, lines = read_tiers()
    tiers, sel_lines = select_tiers(tiers, tier_names)
    lines = lines + sel_lines
    failures: list[str] = []
    lines.extend(tier_shadow_lines(list(tiers)))
    arg_by_tier, route_lines = route_tier_arg(arg, list(tiers))
    lines.extend(route_lines)
    for name, opts in tiers.items():
        try:
            module = _tier_module(name)
        except Exception as exc:  
            failures.append(f"radar: WARNING — tier '{name}' could not be loaded: "
                            f"{exc.__class__.__name__}: {exc}")
            lines.append(f"{name}: UNLOADABLE — {exc.__class__.__name__}: {exc}")
            continue
        if module is None:
            failures.append("\n".join(
                [f"radar: WARNING — tier '{name}' is registered but could not be "
                 f"resolved; it contributes nothing. Check the name."]
                + _tier_search_lines(name)))



            lines.append(f"{name}: UNRESOLVED — no tier module of that name")
            lines.extend(_tier_search_lines(name))
            continue
        lines.append(f"{name}:")
        lines.append(f"  module    : {getattr(module, '__file__', '?')}")
        unknown = sorted(set(opts) - set(getattr(module, "RADAR_OPTIONS", set())))
        if unknown:
            lines.append(f"  UNKNOWN opt: {unknown} — ignored; check for a typo")
        quiet = opts.get("quiet_when_healthy",
                         getattr(module, "RADAR_QUIET_DEFAULT", True))
        lines.append(f"  quiet ok  : {bool(quiet)}")
        state = getattr(module, "radar_state", None)
        if state is None:
            lines.append("  state     : this tier exposes no radar_state(); its "
                         "state can only be seen by running radar, which spawns")
            continue
        try:
            lines.extend(state({**opts, "_arg": arg_by_tier[name]}))
        except Exception as exc:  
            failures.append(f"radar: WARNING — tier '{name}' radar_state failed: "
                            f"{exc.__class__.__name__}: {exc}")
    return lines, failures





_DELIVERY_FOOTNOTE = (
    "counted over the watcher state files, which includes slots whose poller "
    "has since gone; `watches` renders it per watcher and `channel:health` is "
    "the judgement about the socket. Radar neither stops, re-arms nor reaps "
    "anything on the strength of this line."
)


def channel_banner() -> list[str]:














    return ["radar: " + line
            for line in transport.channel_disclosure() + sourcepath.op_lines("radar")]


def delivery_banner() -> list[str]:



















    rows = transport.delivery_survey()
    if not rows:
        return []
    total = len(rows)
    lost = [row for row in rows if row[2] == transport.EMIT_NO_LISTENER]
    unsure = [row for row in rows if row[2] == transport.EMIT_UNKNOWN]
    took = [row for row in rows if row[2] == transport.EMIT_ACCEPTED]
    if lost:
        head = (f"radar: DELIVERY — {len(lost)} of {total} watcher state file(s) "
                f"record a last emit that found nobody listening on the socket.")
    elif unsure:
        head = (f"radar: DELIVERY — {len(unsure)} of {total} watcher state file(s) "
                f"cannot say whether their last emit reached anyone.")
    elif len(took) == total:
        head = (f"radar: delivery — all {total} watcher state file(s) had their "
                f"last emit accepted by a listener.")
    elif took:




        head = (f"radar: delivery — {len(took)} of {total} watcher state file(s) "
                f"had their last emit accepted by a listener; the other "
                f"{total - len(took)} have not emitted yet.")
    else:
        head = (f"radar: delivery — no watcher has recorded an emit yet across "
                f"{total} state file(s), so nothing here says whether the socket "
                f"delivers.")
    lines = [head, "       " + _DELIVERY_FOOTNOTE, *_destination_lines(rows)]
    if took:





        lines += _subscription_lines()
    return lines


def _subscription_lines() -> list[str]:








    sub = channel.subscription_for_socket(transport.SOCK_PATH)
    if sub.state == channel.SUB_SUBSCRIBED:
        return []
    if sub.state == channel.SUB_NOT_SUBSCRIBED:
        head = ("radar: DELIVERY — the consumer on this socket is BOUND, NOT "
                "SUBSCRIBED: the events counted above were read and then "
                "discarded, because no session is subscribed to this channel.")
    else:
        head = ("radar: DELIVERY — whether any session is SUBSCRIBED to this "
                "channel was not established, so nothing above says the events "
                "counted reached one.")
    return [head, *["       " + line.strip() for line in sub.lines]]


def _destination_lines(rows: list[tuple[str, str, str]]) -> list[str]:













































    emitted = [(source, wid) for source, wid, state in rows
               if state != transport.DELIVERY_NO_EMIT]
    if not emitted:
        return []
    recorded = {(source, wid): path
                for source, wid, path in transport.emit_destinations()}

    here = _untrusted.flat(transport.SOCK_PATH)
    elsewhere = [slot for slot in emitted
                 if recorded.get(slot, "") not in ("", transport.SOCK_PATH)]
    unrecorded = [slot for slot in emitted if not recorded.get(slot, "")]
    out: list[str] = []
    if elsewhere:
        others = sorted({_untrusted.flat(recorded[slot]) for slot in elsewhere})




        out.append("       " + _untrusted.flat_note(
            "the socket path(s)", "the watchers' own state files"))
        out.append(
            f"radar: DELIVERY — {len(elsewhere)} of {len(emitted)} watcher "
            f"state file(s) that emitted last wrote to a socket this session "
            f"does not read: {', '.join(others)}. This session reads {here}, "
            f"so those events reached a consumer that is not this one.")
    if unrecorded:



        out.append(
            f"radar: DELIVERY — {len(unrecorded)} of {len(emitted)} watcher "
            f"state file(s) that emitted do not record which socket they "
            f"wrote to, so nothing here says whether they reach {here}.")
    return out


def state_main(arg: str = "") -> int:
    tiers, complaints = read_tiers()
    if not tiers:
        for line in complaints:
            print(line, file=sys.stderr)
        print(NO_TIERS, file=sys.stderr)
        return 1
    lines, failures = tier_states(arg)



    banner = channel_banner() + delivery_banner()
    if banner:
        print("\n".join(banner))
    if lines:
        print("\n".join(lines))
    for line in failures:
        print(line, file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (argv or [])[1:] if a]
    if args and args[0] == "--state":
        return state_main(args[1].strip() if len(args) > 1 else "")

    arg = argv[1].strip() if argv and len(argv) > 1 and argv[1] else ""

    tiers, complaints = read_tiers()
    if not tiers:
        for line in complaints:
            print(line, file=sys.stderr)
        print(NO_TIERS, file=sys.stderr)
        return 1





    lines, _all_ok, failures = tier_reports(arg)
    for line in failures:
        print(line, file=sys.stderr)
    banner = channel_banner() + delivery_banner()
    if banner:
        print("\n".join(banner))
    if lines:
        print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
