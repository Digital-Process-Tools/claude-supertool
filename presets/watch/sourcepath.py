#!/usr/bin/env python3
"""Where a watch source may live, resolved once for all five watch ops (#2135).

`dispatcher.SOURCES_DIR` used to be `Path(__file__).parent / "sources"` and
nothing overrode it, so a source plugin could only exist inside the installed
plugin directory -- which every plugin update overwrites, and which a symlink
does not survive. That is not a missing convenience: `watch:my-source:scope`
answers `unknown source`, and with it go the pid slots, `unwatch`, the `watches`
board and radar's healing, about three thousand lines of poller runtime that
exist, work, and cannot be reached by anything a user watches privately.

`SUPERTOOL_WATCH_SOURCES_PATH` is a search path of extra directories, separated
by `os.pathsep`. It is an ordinary environment variable, so a non-reserved key
in an op's `.supertool.json` block reaches it with no plumbing at all
(`docs/contributing.md`, "Extra config keys as environment variables"):

    {"ops": {"watch": {"watch_sources_path": "/opt/private/watch-sources"}}}

arrives here as `SUPERTOOL_WATCH_SOURCES_PATH`. `naming.py` is the precedent for
the shape -- one knob, resolved once, reported on every surface, with a
precedence rule that is a decision rather than an accident.

**This imports and executes Python from a caller-supplied path.** A directory on
this path is loaded with `importlib` and its `poller.py` runs in the poller
process with the caller's own privileges, on a value that arrived from an
environment variable or from a JSON file in a repository. Anyone who can write
to a directory on the path, or to the `.supertool.json` that names it, can run
arbitrary code as you. Nothing here is a sandbox and nothing here pretends to
be one; the docs say so in the same words (`docs/presets/watch.md`).

**Shipped sources win, and a shadow is named rather than skipped.** An external
directory declaring `gitlab-mr` would otherwise replace a shipped poller with
arbitrary Python on a path that came from a config file. Refusing it silently is
the same defect one layer down -- the operator believes their source is loaded
and it is not -- so `shadowed()` reports every collision and every rendering
surface prints it.

**Absolute paths only.** A relative entry is refused and named. A poller is
detached, re-execs itself (`dispatcher._exec_labelled`) and then runs for days;
`radar` re-derives the same path from a possibly different directory. A relative
entry resolved against whatever the working directory happened to be is a source
that loads once and cannot be found again, which is exactly the failure the
refusal makes visible.

**Nothing here is derived, so nothing here needs pinning across the exec.**
`transport.poller_env` pins the state directory and the socket because those are
*derived* from `SUPERTOOL_WATCH_NAME` and re-deriving them in the exec'd image
was not equivalent (#1477/#1534). This value is used exactly as the environment
carries it, and `poller_env` is `dict(os.environ)` plus those two pins, so the
search path survives an exec by construction. `tests/test_watch_sources_path_2135.py`
pins that property rather than assuming it: an env built from an allowlist would
drop it, and a re-exec'd poller would answer `unknown source` about the source it
was already polling.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))        # for naming
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _untrusted

import _untrusted  # noqa: E402  (a path and a directory entry are somebody else's text, #1522)
import naming  # noqa: E402  (flat_path, the config walk, and the five op names)
import transport  # noqa: E402  (the reserved probe source, #1593)

#: Source names no external directory may answer to, whatever it holds.
#:
#: `transport.PROBE_SOURCE` was reserved by #1593 so a synthetic probe event can
#: never be confused with a real watcher's, and the reservation was a *checked*
#: property rather than a promise: `tests/test_watch_channel_probe_1593.py`
#: enumerates `presets/watch/sources/`. A search path widens the space that test
#: enumerates, so the check has to move here or the guarantee quietly narrows to
#: the directories that happen to ship -- and an external `channel-probe` would
#: emit events a session cannot tell from the probe's own.
#:
#: Imported rather than restated: a second copy of the name is a second thing to
#: keep in step, and the whole defect being prevented is two spellings of one
#: identity drifting apart.
RESERVED_NAMES = (transport.PROBE_SOURCE,)

PATH_ENV = "SUPERTOOL_WATCH_SOURCES_PATH"

#: The `.supertool.json` key that reaches `PATH_ENV`. Declaring it with
#: *different* values on two op blocks is refused by the core before this module
#: is reached (`_supertool.py::_op_config_collision_refusal`, #1009); declaring
#: the same value on several is the intended shape and is what `watch_name`
#: already does across this repository's own five blocks.
CONFIG_KEY = "watch_sources_path"

#: The five ops that must resolve the same path. Borrowed rather than restated:
#: a second list is a second thing to keep in step, and `naming` already carries
#: the one this preset ships (`channel`, `radar`, `unwatch`, `watch`, `watches`).
WATCH_OPS = naming.WATCH_OPS

SHIPPED_DIR = Path(__file__).parent / "sources"

#: What `find` returns as the origin of a shipped source. A sentinel rather than
#: the path, because callers branch on "did this come from the plugin" and a
#: string comparison against a path is how that question gets answered wrongly.
SHIPPED = "shipped"


class Refused(NamedTuple):
    """A declared entry that was not searched, and why not."""

    entry: str
    why: str


class Resolved(NamedTuple):
    """Every directory this process will look in, and what it declined.

    `refused` is not decoration and is the reason this is not a list of paths:
    an entry that was declared and not searched is the difference between "your
    source is not there" and "I never looked", and a resolution that drops it
    reports the second as the first.
    """

    shipped: Path
    external: tuple[Path, ...]
    refused: tuple[Refused, ...]
    #: Exactly what `PATH_ENV` held, so a surface can say the knob is unset
    #: rather than saying nothing.
    raw: str
    #: Entries that named a directory already on the list -- in practice the
    #: shipped one. Not `refused`: they *are* searched, first, and reporting
    #: them as unusable would be false about a knob the operator set correctly
    #: if redundantly.
    redundant: tuple[str, ...] = ()


def resolve(env: dict[str, str] | None = None) -> Resolved:
    """The directories to search, in precedence order. Shipped is always first.

    Reads a mapping and stats each entry; creates nothing. Called per lookup
    rather than cached in a module constant, because a poller reached through a
    fork and an exec has no import-time moment that both processes share -- the
    same reason `transport.channel_key` reads `STATE_DIR` late.
    """
    src = os.environ if env is None else env
    raw = src.get(PATH_ENV) or ""

    external: list[Path] = []
    refused: list[Refused] = []
    redundant: list[str] = []
    # The shipped directory is searched first, always, so naming it again on the
    # path is a duplicate rather than an addition -- and searching it twice would
    # report every shipped source as shadowing itself.
    seen = {os.path.normcase(os.path.normpath(str(SHIPPED_DIR)))}

    for raw_entry in raw.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            # An operator who exports an empty string, or a trailing separator.
            # Neither is a declaration, and refusing it would be a complaint
            # about a path nobody set.
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
            # `os.path.isdir` is this `stat` inside a bare `except OSError:
            # return False`, so a directory this uid cannot traverse answers in
            # exactly the words a missing one uses. They are different facts and
            # the operator needs the second one (#1732 raised the same shape
            # against `naming.find_config`).
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
    """True when `name` is a single path component and cannot leave a directory.

    `dispatcher._parse_args` already refuses `/` and `__` in a source name, and
    neither covers `..` or a backslash -- which is a separator on Windows and an
    ordinary character on POSIX, so the check has to be the platform's own.
    """
    if name in ("", ".", ".."):
        return False
    return os.path.basename(name) == name and "/" not in name


def find(name: str, resolved: Resolved | None = None) -> tuple[Path | None, str]:
    """(`poller.py` for `name`, where it came from), or (None, "").

    The single door. `dispatcher._load_source` is the only caller, and `radar`
    and `tiers/gl_mrs` reach it through that -- so all five ops resolve one path
    through one function, which is what stops a source being startable by
    `watch` and unhealable by `radar`.

    Shipped first, and that is the precedence decision: an external directory
    may add sources, never replace one. See `shadowed()` for what is said about
    a collision -- it is said, not swallowed.
    """
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


#: What an external directory must hold to be a radar *tier* for the name it
#: wears, beside the `poller.py` that makes the same directory a watch source
#: (#2165). One directory per population, both halves in it: a tier and a source
#: for one population are one concern, and a second search path for the second
#: half is a second thing to keep in step -- which is how the two ends up
#: describing a fleet nobody polls.
#:
#: Radar's own tiers do not live here and are not named by this file: a shipped
#: tier is `presets/watch/tiers/<name>.py`, resolved by `radar._tier_module`
#: before this search path is consulted at all. Shipped wins, as it does for a
#: source, for the same reason and with the same disclosure.
TIER_FILE = "tier.py"


def find_tier(name: str, resolved: Resolved | None = None) -> tuple[Path | None, str]:
    """(`tier.py` for `name`, the directory it came from), or (None, "").

    External directories only. A shipped tier is radar's own file and radar
    resolves it first; this answers the question radar asks *after* that, so a
    hit here can never displace a shipped board -- and `radar.tier_shadow_lines`
    names the external file that was passed over rather than swallowing it.

    Reads the same `Resolved` a source does, so a relative entry refused for a
    poller is refused for a tier without the rule being written twice.
    """
    resolved = resolve() if resolved is None else resolved
    if not _is_one_component(name):
        return None, ""
    for directory in resolved.external:
        candidate = Path(directory) / name / TIER_FILE
        if candidate.is_file():
            return candidate, str(directory)
    return None, ""


def tier_names_in(directory: Path | str) -> tuple[tuple[str, ...], str]:
    """(the tier names in `directory`, why the listing failed) -- `names_in` for tiers.

    Three states again, and the middle one is the reason this exists: a
    directory that could not be listed must not render as a directory holding
    no tiers, or radar's "I could not resolve your tier" names a search that
    never happened as one that came back empty.
    """
    entries, state, why = naming.state_dir_listing(str(directory))
    if state == naming.STATE_DIR_UNREADABLE:
        return (), why
    if state == naming.STATE_DIR_ABSENT:
        return (), (f"{naming.flat_path(str(directory))} is gone -- it was a "
                    f"directory when the search path was resolved")
    return tuple(n for n in entries
                 if (Path(directory) / n / TIER_FILE).is_file()), ""


def tier_search_report(resolved: Resolved) -> list[str]:
    """Every directory a tier lookup consulted and what it held.

    The counterpart of `search_report`, and it exists for the same measured
    reason: an absence that does not name where it looked is this tracker's
    most-filed defect class, and radar's version of it lands in the one message
    an operator meets while wiring their first tier.
    """
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
    """(the source names in `directory`, why the listing failed).

    Three states, not two: `()` with an empty `why` means the directory holds no
    sources, `()` with a `why` means nobody looked. A reader that collapses the
    second into the first prints a claim about the world on the strength of a
    listing that never happened.

    Borrowed from `naming.state_dir_listing` rather than hand-rolled, and its
    name is the only thing about it that is state-directory-specific: it takes
    the directory as an argument precisely so it is not, and
    `tests/test_watch_state_dir_absent_1502.py` holds the whole preset to it as
    the one place `os.listdir` may name a directory. Two copies of a three-state
    classifier is two places to get it wrong.
    """
    entries, state, why = naming.state_dir_listing(str(directory))
    if state == naming.STATE_DIR_UNREADABLE:
        return (), why
    if state == naming.STATE_DIR_ABSENT:
        # `resolve` refuses an entry that is not a directory, so reaching this
        # means it went away afterwards. Said rather than rendered as empty.
        return (), (f"{naming.flat_path(str(directory))} is gone -- it was a "
                    f"directory when the search path was resolved")
    return tuple(n for n in entries
                 if (Path(directory) / n / "poller.py").is_file()), ""


def shadowed(resolved: Resolved) -> tuple[tuple[str, Path], ...]:
    """Every external source a shipped one of the same name hides.

    Named, never silently skipped: an operator who put a `gitlab-mr` on their
    own path believes it is the one running, and a load that quietly answers
    with the shipped module is the absence-read-as-presence defect wearing a
    plugin loader.
    """
    shipped_names, _why = names_in(resolved.shipped)
    out: list[tuple[str, Path]] = []
    for directory in resolved.external:
        names, _ = names_in(directory)
        for name in names:
            if name in shipped_names:
                out.append((name, Path(directory)))
    return tuple(out)


def reserved_hits(resolved: Resolved) -> tuple[tuple[str, Path], ...]:
    """External sources wearing a reserved name -- never loaded, always named.

    Shipped-name shadowing and this are the same refusal with different
    reasoning, and they are reported separately because the reasons differ: a
    shipped source exists and wins, while nothing at all answers to
    `RESERVED_NAMES` and the name is refused so that a synthetic probe event
    stays distinguishable from a watcher's (#1593).
    """
    out: list[tuple[str, Path]] = []
    for directory in resolved.external:
        names, _ = names_in(directory)
        for name in names:
            if name in RESERVED_NAMES:
                out.append((name, Path(directory)))
    return tuple(out)


def _flat_names(names: tuple[str, ...]) -> str:
    """Directory entries from somebody else's filesystem, on our own board."""
    return ", ".join(_untrusted.flat(n, disclose_newline=True) for n in names)


def search_report(resolved: Resolved) -> list[str]:
    """Every directory consulted and what it held -- for the unknown-source error.

    An absence that does not say where it looked is this tracker's most-filed
    defect class, and it used to land in the one message a user hits while
    setting the feature up: `Available: <shipped>` named neither the directory
    those came from nor any directory the operator had configured.
    """
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
    """What this process resolved, for any surface that renders a board.

    `[]` when the knob is untouched -- a banner on every board is one nobody
    reads (#1495), and with nothing configured there is no half-set state to
    disclose.
    """
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
        # Non-empty, every entry blank. Saying nothing here would render a knob
        # that is set and doing nothing as a knob nobody set -- and the two
        # conditions above it matter, because an entry that was refused or that
        # named the shipped directory is not "no usable directory".
        out.append(f"{PATH_ENV} is set and names no usable directory, so only "
                   f"the shipped sources are available")
    return out


def config_notes(op: str, start_dir: str | None = None) -> list[str]:
    """Whether this project declared the path for every watch op, or only some.

    A key in an op's `.supertool.json` block reaches only *that* op's subprocess,
    so a `watch_sources_path` on `watch` alone gives a fleet `watch` can start
    and `radar` can neither see nor re-spawn -- the half-configured shape #1309
    and #1732 are both about, arriving through the config door a third time.

    Reads a file, so it is only ever called from a render path where there is a
    reader -- the same rule `naming.declared_names` follows.

    Silent when nothing declares the key: the default is every op searching the
    shipped directory, and there is no half-set state in that.
    """
    state, path, declaring, why = naming.declared_op_values(CONFIG_KEY, start_dir)
    if state == naming.DECLARED_UNREADABLE:
        # Not folded in with "declares nothing", which is what it was and which
        # is this repo's own defect class: a look that failed rendering as a
        # fact about the world. `naming.project_notes` says something adjacent
        # on three of the five surfaces -- but only under a named channel, and
        # `watch` and `unwatch` never call it at all, so on the two ops run most
        # often an unreadable config produced no line at all. The claim here is
        # its own: whether the key is declared for every watch op is unknown.
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
    """Everything a watch op should say about where its sources come from.

    One accessor over both halves so the five surfaces cannot disagree, exactly
    as `transport.channel_disclosure` sits over `naming.disclosure_lines`.
    """
    resolved = resolve() if resolved is None else resolved
    return disclosure_lines(resolved) + config_notes(op)
