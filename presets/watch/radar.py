#!/usr/bin/env python3
"""radar — reconcile registered tiers against live truth, then report.

Radar's core is one sentence, and merge requests are not in it:

    Reconcile registered tiers against live truth, heal their watchers, report,
    stay idempotent, and never render an unknown as green.

GitLab MRs were simply the first tier anyone wrote (#528). The board lives in
`tiers/gl_mrs.py` and joins on exactly the same footing as `gl-runners`,
`gh-prs`, or anything else with a population and a standing question.

    {"ops": {"radar": {"radar_tiers": {"gl-mrs": {}, "gl-runners": {}}}}}

With no tiers configured radar refuses and says how to fix it. It does not
quietly do nothing, and it does not assume `gl-mrs`:

  * Silence is the failure this whole preset is built against. An unconfigured
    radar that prints nothing is byte-identical to a healthy one.
  * A `gl-mrs` default is an opinion imposed on strangers — it points GitLab
    API calls at people who may be on GitHub, and hides from them that radar
    is configurable at all.
  * The refusal is also the documentation: it teaches the config at the moment
    someone needs it.

The tier contract
-----------------

A tier is a Python module reachable by name (see `_tier_module`) exposing:

    RADAR_OPTIONS       set[str] — config keys this tier understands. Anything
                        else in its config block is named on the board, never
                        silently ignored.
    RADAR_QUIET_DEFAULT bool, optional (default True) — is a healthy tier
                        silent? True for a side-concern like the runner fleet,
                        where a green line per run is the noise that trains a
                        reader to skim past the red one. False for a tier whose
                        report *is* the board: an MR reconcile that prints
                        nothing on a quiet day is indistinguishable from one
                        that failed to run.
    radar_report(options) -> (lines, healthy)
                        `healthy` means "this tier could tell you the truth",
                        not "the world is fine". A board full of red MRs is
                        healthy; a board that could not be built is not.

Radar injects two reserved keys into `options` before the call. Config cannot
set them — `read_tiers` refuses any key starting with `_` — so a tier can trust
them:

    _arg    str, the raw invocation argument (`radar:author=@me` -> "author=@me")
    _watch  callable(source, scope, only=None) -> "alive"|"spawned"|"failed"|
            "capped". Radar's bounded spawner. Every slot a tier asks for is
            recorded, and radar itself emits the cap warning when one is
            refused.

`_watch` is a callable rather than the declarative `radar_watchers(options) ->
[(source, scope, only)]` #528 proposed, and the difference is load-bearing. A
declared slot must be spawned before the report, and the MR tier must *not*
spawn its discovery feed when live GitLab was unreachable — the current
behaviour, pinned by a test. Only the tier knows whether spawning is safe, and
it needs the spawn result inside its own report (the feed's status is a token
in the board footer). Two mechanisms for one job is the drift this codebase
keeps filing bugs about, so there is one: radar owns the bound, the tier owns
the timing.

Never green when it cannot tell
-------------------------------

Three states, not two: ok, a finding, and *cannot tell*. A tier that fails to
resolve or raises is not a quiet degradation — its message goes to stderr and
radar exits non-zero, because a refactor that traded a loud failure for a quiet
one would be the house defect (#486, #533) delivered by the fix for it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

_HERE = Path(__file__).parent

sys.path.insert(0, str(_HERE))
import dispatcher  # noqa: E402
import transport  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ops.radar.radar_tiers, JSON-encoded into the env by the op runner — the same
# route ops.gl-job.job_patterns takes.
TIERS_ENV = "SUPERTOOL_RADAR_TIERS"

# Keys radar fills in on a tier's options dict. Refused in config so a tier can
# tell radar's context apart from the operator's configuration.
RESERVED_PREFIX = "_"

NO_TIERS = ('radar: no tiers configured. Add ops.radar.radar_tiers to .supertool.json —\n'
            '       e.g. {"gl-mrs": {}} for the GitLab MR board.')


def ensure_watcher(source: str, scope: str, only: list[str] | None = None) -> str:
    """One live poller for a slot.

    "alive"|"spawned"|"failed"|"capped"|"unclaimable" — the last is #693's third
    state, passed straight through from `start_poller`: the slot could not be
    claimed, so nothing was spawned and nothing was established about it.

    Idempotent, because radar runs on a loop and n pollers over one slot means
    n copies of every event. `start_poller` claims the slot before the fork:
    reading a pidfile and then spawning is not that check, since the PID is
    published after a fork and a detach, so two runs landing in that window
    both see an empty slot and both spawn (#476).

    The death cap from #513 is applied here rather than in any one caller. That
    bound lived on the per-MR path alone, so every other spawner would respawn
    a poller that dies on every tick, forever, silently. Reproducing a fixed
    defect in a new tier is how it comes back, so the cap belongs at the one
    place that spawns.
    """
    if dispatcher._load_source(source) is None:
        return "failed"
    if len(transport.deaths(source, scope)) >= transport.DEATH_RESPAWN_LIMIT:
        return "capped"
    return dispatcher.start_poller(source, scope, only or [])[0]


def watcher_cap_warnings(statuses: dict[str, str]) -> list[str]:
    """Name every slot radar has stopped respawning. Never silent about it.

    A capped slot is not being watched, and the whole lesson of #513 is that a
    monitoring surface going quiet reads exactly like nothing being wrong.
    """
    return [
        f"radar: WARNING — stopped respawning {name}: it has died "
        f"{transport.DEATH_RESPAWN_LIMIT}+ times and nothing is polling it. "
        f"Fix it, then re-arm with `watch:{name}`."
        for name, status in sorted(statuses.items()) if status == "capped"
    ]


def read_tiers(raw: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """({op_name: options}, complaints) from ops.radar.radar_tiers.

    Empty by default, and that default is why `main` refuses rather than
    printing an empty board. Radar has no opinion about what anyone watches;
    a tier that reaches for GitLab, or runners, or any other resource, must be
    asked for by name.

    Nothing here raises. A config this cannot parse yields no tiers plus a
    complaint, which reaches the reader as the refusal plus the reason.
    """
    raw = os.environ.get(TIERS_ENV, "") if raw is None else raw
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
        # Underscored keys are radar's own context channel (_arg, _watch). A
        # config that could forge them would be a config that could lie to a
        # tier about what radar asked for.
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
    """Resolve a registered tier name to the module implementing it, or None.

    Two places, in order:

      1. `tiers/<name>.py` beside this file, with dashes as underscores. A tier
         that needs radar's own internals — the transport, the dispatcher, the
         shared watch defaults — belongs on this side of the preset boundary.
         The MR board is that case: `presets/gitlab/mrs.py` is a GitLab preset
         and the watch preset already depends on it, so putting reconcile
         machinery there would make the dependency mutual.
      2. the script the preset's op declares, read out of the preset itself
         rather than from a second hardcoded table: the op's `cmd` already
         names its script, and a mapping kept alongside it is one that drifts
         the first time a file moves. `gl-runners` joins this way — its report
         needs nothing but its own API helpers.

    A name resolves to a directory entry or to an op, never to a table, so
    neither route can fall out of step with the files on disk.
    """
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
    return None


def _spawner() -> tuple[Callable[..., str], dict[str, str]]:
    """A `_watch` callable plus the ledger of every slot it was asked for.

    The tier chooses when to spawn; radar keeps the bound and the record. A
    slot radar refused is a slot nobody is polling, and the ledger is what lets
    it say so on the same run.
    """
    seen: dict[str, str] = {}

    def watch(source: str, scope: str, only: list[str] | None = None) -> str:
        status = ensure_watcher(source, scope, only)
        seen[f"{source}:{scope}"] = status
        return status

    return watch, seen


def tier_reports(arg: str = "") -> tuple[list[str], bool, list[str]]:
    """(lines, all_healthy, failures) from every registered tier, in order.

    `lines` is the board — stdout. `failures` is the *cannot tell* channel: a
    tier that could not be resolved, or that raised. It is returned apart from
    `lines` rather than folded into them because the two have different
    destinations and different exit codes. A broken tier must not be able to
    cost a working one its board, and it must not be able to leave radar
    exiting 0 either.

    A healthy tier is silent when its `RADAR_QUIET_DEFAULT` says so — true for
    a side concern, false for a tier whose report is the board itself.

    Order is registration order, so an operator who wants the fleet verdict
    above the MR board writes `gl-runners` first. Nothing here sorts: a tier
    that says "the board below may be this, not your code" is making a claim
    about position, and only the person who wrote the config can settle it.
    """
    tiers, lines = read_tiers()
    watch, spawned = _spawner()
    all_ok = True
    failures: list[str] = []

    for name, opts in tiers.items():
        module = _tier_module(name)
        report = getattr(module, "radar_report", None) if module else None
        if report is None:
            failures.append(f"radar: WARNING — tier '{name}' is registered but exposes no "
                            f"radar_report(); it contributes nothing. Check the name.")
            all_ok = False
            continue

        unknown = set(opts) - set(getattr(module, "RADAR_OPTIONS", set()))
        if unknown:
            lines.append(f"radar: WARNING — tier '{name}' has unknown option(s) "
                         f"{sorted(unknown)}; ignored. Check for a typo.")

        try:
            tier_lines, ok = report({**opts, "_arg": arg, "_watch": watch})
        except Exception as exc:  # noqa: BLE001 — a broken tier must not take radar down
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

    return watcher_cap_warnings(spawned) + lines, all_ok, failures


def main(argv: list[str] | None = None) -> int:
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
    if lines:
        print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
