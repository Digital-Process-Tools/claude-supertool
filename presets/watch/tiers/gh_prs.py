#!/usr/bin/env python3
"""gh-prs — the GitHub pull-request board, as a radar tier (#859).

`presets/watch/tiers/` held exactly one tier and it spoke GitLab, so the board
this repository is actually merged from was the one population radar could not
watch. Registered by name like any other tier:

    {"ops": {"radar": {"radar_tiers": {"gh-prs": {}}}}}

Why this is a parallel module and not `gl_mrs` generalised
----------------------------------------------------------

Three of the four things `gl_mrs` does turn out not to transfer, and forcing
one interface over them would have bent GitLab's semantics to fit GitHub's or
the reverse:

  * **discovery used to have no analogue and now does (#1780).** This section
    used to say flatly that there was no `github-pr-feed`, so a PR opened
    after a radar run was invisible until the next tick. `github-pr-feed`
    closes that: this tier spawns and keeps alive one feed poller over its own
    filter, exactly as `gl_mrs` does with `gitlab-mr-feed` — see "3b. feed"
    below. The footer states the feed's own state on every board
    (`discovery: feed ok` / `feed DOWN` / ...) rather than a fixed sentence,
    because "radar ticks only" was true when there was nothing to lose and is
    a false claim of degraded coverage now that there is a feed to be down.
  * **drift has no analogue.** GitLab's drift is `last_event.pipeline_id` vs
    `source_state.pipeline_id`. A GitHub PR has no pipeline id; its identity
    under a re-push is the head SHA, which is a *snapshot* concern here rather
    than an event-vs-state one.
  * **watch state is repo-blind.** `/tmp/supertool-watch-github-pr__{number}.pid`
    carries no repo (#673), which gives this tier a failure mode `gl_mrs` does
    not have — see "The one-filter invariant" below.

What *does* transfer is the snapshot: keeping a previous board keyed by the
population it describes, so a delta cannot lie. That reasoning is not GitLab's,
so it moved to `tiers/_snapshot.py` and both tiers read it. One copy, because a
second copy is how a fixed defect comes back.

The one-filter invariant
------------------------

`gl_mrs` states it as *board, watcher fleet and feed are three views of one
resolved filter*. Here it is two views, not three — and it acquires a clause
GitLab does not need:

    The board and the watcher fleet come from one resolved filter, **and that
    filter must describe one repository**, because watch state is keyed by PR
    number alone.

Under a repo target (`gh-prs` against another repo) a live poller for `#12`
cannot be told apart from `#12` of the repo the watcher was started in. So
coverage is **UNKNOWN**, not zero, and nothing is healed: spawning
`watch:github-pr:N` there would start polling *this* clone's `#N` while the
board it came from is about another repo. Rendering that as `0 watched` would
be a number a reader acts on, and healing on it would be an action taken on a
misidentification.

Never green when it cannot tell
-------------------------------

Every route by which this board could narrow itself says so:

  filter        a token `gh-prs` cannot honour is **refused**, before any call.
                `gh pr list` silently ignores an unrecognised key, so
                `radar:milestone=v19` would otherwise return the whole
                unfiltered board and read as "everything matched" — the #486
                shape, and the reason `gh-prs` itself is named in this file's
                changelog fragment as still carrying it.
  auth / rate   a non-zero `gh pr list` raises `RadarError`. Radar prints it on
                stderr and exits non-zero; nothing is healed and **nothing is
                snapshotted**, because acting on a population we could not read
                is how a cache gets overwritten with a guess. The subset of
                those failures that mean the request never landed — no
                credentials, throttled, or the socket — raise the
                `RadarUnreachable` subclass, so a caller can tell "GitHub did
                not answer" from "the board says X" without matching on the
                message (#1568). Radar treats both identically.

                Within that, the *message* has three states and not two
                (#1823). Only a probe that got an answer saying the credential
                is unusable names the credential and prints `gh auth login`;
                a failure that established no cause quotes the exit status and
                the stderr of the call that did not answer, and prints no
                remedy at all. The remedy is a claim about a cause, and the
                caller acts on it — a loop told the credential is gone stops,
                where a loop told the tier could not answer retries.
  empty match   a filter that selected nothing is reported *with its scope*.
                "No open PRs" and "this filter matched nothing" are different
                facts and only one of them is about the world.
  no checks     a PR whose rollup is empty is `unchecked`, never green — the
                run may not exist yet, and "not yet" has rendered as "fine" on
                this board's GitLab twin before (#659).
  short tally   a PR the board calls **green** is the only claim that can be
                wrong in the expensive direction, so green rows — and only
                green rows, capped — are reconciled against the legs their runs
                declare, through `gh-pr`'s own `_reconcile_checks` (#724/#804/
                #837). A shortfall makes the row `[legs UNVERIFIED]` and counts
                as unchecked. The tier consumes that arithmetic; it never
                re-implements it, and it never prints a leg count of its own —
                a tally that looks reconciled and is not is the defect three
                PRs just closed.

The default branch is a member
------------------------------

The case that cost the most was not a PR: `master` sat red after a squash
landed, because a green PR is a statement about its merge base and nothing
watches the default branch afterwards. So it is a row on this board, answered
by composing `gh-branch`'s own four states — `GREEN` / `NOT GREEN` / `NO RUN` /
`UNKNOWN` — rather than a second, weaker verdict written here. `default_branch`
names it; `""` switches it off; absent resolves it from the repo.

Heal versus report
------------------

This tier heals, through radar's `_watch` rather than by calling
`dispatcher.start_poller` itself — so radar owns the death cap and the ledger,
and every slot refused is named by radar on the same run. And because heal
spawns processes, everything read-only is reachable without it: `radar_state()`
answers what this tier knows — scope, repo, snapshot, live pollers — and starts
nothing. `watches` is read-only about the fleet; this is the same guarantee
about the tier.
"""
from __future__ import annotations

import concurrent.futures
import glob
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import dispatcher  # noqa: E402,F401  (radar_state reads its source registry)
import naming  # noqa: E402  (`flat_path` for the state directory it renders)
import transport  # noqa: E402

sys.path.insert(0, str(_WATCH.parent))
import _checks  # noqa: E402
import _filter_tokens  # noqa: E402  (the one tokenizer the boards share)
import _repo_target  # noqa: E402
import _st_hint  # noqa: E402  (a runnable invocation, not a hardcoded one -- #905)
import _untrusted  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("radar_github_prs", _WATCH.parent / "github" / "prs.py")
pr = _load("radar_github_pr", _WATCH.parent / "github" / "pr.py")
branch = _load("radar_github_branch", _WATCH.parent / "github" / "branch.py")
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")
# The one predicate both tiers share: "did the probe establish that there is no
# usable credential?" (#1823). One copy, for `_snapshot.py`'s reason -- and it
# now sits in `presets/`, not here, because #1846 found the same bare-`401`
# collapse in 23 `presets/github/` and `presets/gitlab/` sites that cannot
# reach a module under `watch/tiers/`.
_auth_probe = _load("radar_auth_probe", _WATCH.parent / "_auth_probe.py")

# The failure *kinds*, shared with the GitLab tier for the same reason the
# predicate above is shared — and reached by `import`, not `_load`, because
# `_load` would give each tier its own `RadarError` class object and an
# `except` written against one would never fire on the other (#1847).
sys.path.insert(0, str(_HERE))
import _radar_errors  # noqa: E402

#: This tier's failure vocabulary, from the one copy. Re-exported under the
#: names callers already use, so `except gh_prs.RadarError` keeps working and
#: is now the *same* class as `except gl_mrs.RadarError`.
RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable
RadarUnconfigured = _radar_errors.RadarUnconfigured

#: Re-exported so this tier's own vocabulary is readable from one place, and so
#: the structural test that no marker is a bare status number has a name to
#: reach for that does not depend on how the module is loaded.
NOT_AUTHENTICATED_MARKERS = _auth_probe.NOT_AUTHENTICATED_MARKERS

SOURCE = prs.WATCH_SOURCE
SNAPSHOT_PREFIX = "supertool-radar-gh-prs"

# The filter keys this tier takes. Anything else is refused rather than dropped
# — see the module docstring.
#
# This is a deliberate SUBSET of `gh-prs._FILTER_KEYS`, not a mirror of it, and
# the line above said "keys `_build_list_cmd` can actually put on the command
# line" until #1411 made that false: the builder also emits `merged-since`, a
# boundary the op refuses outside `state=merged`. A radar tier watches live PRs
# for state changes, and a board of already-merged ones has no state left to
# change, so the key is absent on purpose. `per` is absent for the same reason.
KNOWN_FILTERS = {"author", "assignee", "reviewer", "label", "state"}

# Tokens that are flags rather than key=value — and this tier takes none of
# them (#973). `iids` and `failed` are board *shapes* the op offers and this
# tier does not: a radar board that silently printed only the failing rows
# would be the narrowing this file is against.
#
# `nopipe` was accepted here and applied nowhere. On the op it gates
# `prs._enrich`, the review-thread pass — and `live_open_prs` does not run that
# pass at all, deliberately, so there was nothing for the flag to turn off and
# no way to tell a tier that honoured it from one that ignored it. An accepted
# token that cannot change the answer is the same lie as an ignored one.
#
# Refused on both tiers rather than one, because #939 closed an asymmetry here
# and #961 declined to re-open it; a tier that unilaterally kept the flag would
# be that asymmetry a third time.
KNOWN_FLAGS: set[str] = set()

# Keys whose value this tier maps rather than forwards (#973). `state=opne` is
# in KNOWN_FILTERS, so it survives the unknown-token check — and then
# `_build_list_cmd` tests `val in _STATES`, emits no `--state`, and gh answers
# with its default. The board renders as the default one and radar heals
# watchers onto it. The known-key/unmappable-value case is the quieter half of
# #939's defect: the token *is* recognised, so every vocabulary check passes,
# and only the argv builder knows the request was dropped.
#
# The op's own domain, never a second copy — two hand-written lists of GitHub's
# PR states is how they disagree. `per` is absent because this tier refuses
# that key outright, and a domain for a token that can never arrive is dead
# weight that reads as coverage.
VALUE_DOMAINS: dict[str, object] = {"state": prs._STATES}

# How many green PRs are reconciled against their runs' declared legs per tick.
# Each costs one call for the commit's runs plus one per run, and only a green
# needs proving, so the budget is small and its edge is disclosed.
RECONCILE_CAP = 6

# A `running` PR whose reported facts have not moved for this long comes back
# onto the delta board with the reason on the row (#1025). Four hours, because
# the false positive it must clear is a genuinely queued matrix: eight PRs sat
# at "18 passed, 2 pending" for the better part of an hour while the macOS
# runners were starved, and every one of them eventually landed. A threshold
# that names those trains its reader to skim, which costs more than the wedge
# it catches. Raise or lower it per board; 0 turns it off.
STALE_RUNNING_MINUTES = 240

RADAR_OPTIONS = {"quiet_when_healthy", "default_branch", "reconcile_cap",
                 "stale_running_minutes"}

# A healthy PR board still speaks: a board that prints nothing on a quiet day
# is byte-identical to a radar that failed to run.
RADAR_QUIET_DEFAULT = False


# The three failure kinds are `_radar_errors.py`, imported above and re-exported
# under these names. They moved out of this file in #1847 so that the GitLab
# tier could raise the same classes rather than a parallel set no `except` of
# this tier's would ever catch. Nothing about the taxonomy changed in the move;
# what did is that `gh_prs.RadarError is gl_mrs.RadarError`.
#
# The tier-local half stayed here, because it is the half that is about `gh`:
# the predicate for `RadarUnconfigured` is `gh`'s own exit code, not its prose.
# Measured on gh 2.50.0, exit 4 is its auth-configuration code and nothing else
# produces it — both spellings of "no credentials" (`gh auth login`
# interactively, `set the GH_TOKEN environment variable` under Actions) exit 4,
# while a *rejected* token exits 1 with `HTTP 401` and every product failure
# exits 1. That distinction is exact and matters: a rejected token means the
# request landed. PR #1586 went red on four legs with the Actions spelling,
# which carries no 401, no rate limit and no Go net error — nothing a prose
# predicate could ever have caught.


#: `gh`'s dedicated exit code for "I have no credentials to use". Not a
#: message, so it cannot be reworded out from under this.
GH_RC_NO_CREDENTIALS = 4

#: Substrings of `gh`'s own stderr that mean the request never landed. Read on
#: the failure path only, never on a green.
#:
#: A whitelist, and the direction matters: an UNRECOGNISED failure stays a
#: plain `RadarError` and stays red. The inverse — treat anything unmatched as
#: a transport problem — would swallow the malformed-argv class this tier can
#: genuinely produce (`_build_list_cmd` builds the filter flags), which is a
#: product bug wearing a flake's clothes. Adding a marker here is a decision
#: somebody makes with a log in front of them.
#:
#: `401`/`not logged in` and the rate-limit pair were already special-cased
#: below with their own messages; they move into this set unchanged, because
#: "no credentials" and "throttled" are both "GitHub did not answer this
#: question today" from every caller's point of view.
#:
#: "Unchanged" is why the bare `401` and `403` were here, and why they had to
#: be tightened with the arms they mirror (#1823): a status matched as a bare
#: three-character substring is also a request id, a user id and an epoch. This
#: set is the *fallback*, so a loose entry here is far less costly than one
#: above -- it produces "could not reach", which names no cause and prints no
#: remedy. It was still tightened, because two halves of one story that
#: disagree about what `401` means is how the next reader picks the wrong one.
#:
#: **The first four entries are shadowed at the only call site, and are kept
#: anyway.** `_unreachable()` runs after the not-authenticated arm and after
#: the rate-limit arm, so a stderr matching `not logged in`, `http 401`,
#: `rate limit` or `http 403` is consumed before it ever gets here -- that was
#: equally true before #1823 and is not a behaviour change. They stay because
#: this tuple is the *vocabulary* of "the request never landed", not a live
#: dispatch table, and a predicate named `_unreachable` that answered False for
#: a rate limit would be wrong for the next caller rather than merely unused.
#: Said out loud because a reader who assumes they dispatch will conclude the
#: arms above are dead instead, which is the same mistake pointing the other
#: way.
_UNREACHABLE_MARKERS = (
    "not logged in",
    "http 401",
    "rate limit",
    "http 403",
    # gh's transport layer surfaces Go's net errors verbatim.
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",
)


def _unreachable(err: str) -> bool:
    """Does this `gh` stderr describe a request that never landed?"""
    low = err.lower()
    return any(marker in low for marker in _UNREACHABLE_MARKERS)


# ---------------------------------------------------------------------------
# the one filter
# ---------------------------------------------------------------------------

def resolve_filter(arg: str = "") -> dict[str, str]:
    """The filter every other step reads, in `gh-prs` vocabulary, or `RadarError`.

    Refusing is the whole point. `gh-prs` used to drop a key it did not
    recognise and run the command without it, so `milestone=v19` returned every
    open PR and the reader believed they filtered. On a triage board that reads
    as "all of these matched", which is an absence produced by the tool rendered
    as a fact about the world.

    The op refuses that itself now (#939) — but this tier still parses against
    its *own* vocabulary, which is deliberately narrower: `iids`, `failed` and
    `nopipe` are board shapes and enrichment knobs `gh-prs` offers and a radar
    board must not silently take. So the check stays here; only the
    token-splitting is shared, because a second hand-rolled scan of the arg
    string is how the two answers drift.

    The *wording* is deliberately not shared, unlike `gl_mrs`'s. #939 pinned
    this message's shape after the old one fused two unapplied tokens into a
    `key=value` the caller never typed, and `_filter_tokens.unknown_error`
    words the same refusal differently. Routing through it would change this
    tier's error format for a reason unrelated to the token being refused.

    Two refusals now, and the second is the quieter one (#973). A token this
    tier cannot place at all is named; so is a *known* key carrying a value the
    argv builder has no mapping for. `state=opne` passes every vocabulary check
    and then reaches no command line, so the default board comes back and every
    layer above reads it as the filtered one. #939 added the key check here and
    left the value check to `gh-prs` itself; #961 built the value check for the
    GitLab tier; this is its twin.

    The return value is the filter alone. It used to be `(filters, flags)` and
    both callers bound the flag set and discarded it — an always-empty channel
    is one a future caller re-populates and re-drops, which is the defect this
    issue is about rather than a tidy-up.
    """
    arg = (arg or "").strip()
    filters, _flags, unknown = _filter_tokens.parse(
        arg, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        named = ", ".join(
            f"{t.partition('=')[0]}=" if "=" in t else t for t in unknown
        )
        # The flag clause is derived from KNOWN_FLAGS rather than written out,
        # even though that set is empty today. A sentence hardcoded to "no
        # flags" is a refusal that would misdescribe the vocabulary the moment
        # a flag is added back — which is the same defect as the token this
        # refusal exists for, wearing the error message instead of the board.
        flag_clause = (
            f"known flags: {', '.join(sorted(KNOWN_FLAGS))}"
            if KNOWN_FLAGS else "this tier accepts no flags at all"
        )
        raise RadarError(
            f"radar: gh-prs tier cannot honour {named!r}. Known filters: "
            f"{', '.join(sorted(KNOWN_FILTERS))}; {flag_clause}. Refusing "
            f"rather than running the query without it — an ignored filter "
            f"returns the whole board and reads as though everything matched."
        )
    bad = _filter_tokens.bad_values(filters, VALUE_DOMAINS)
    if bad:
        raise RadarError("radar: gh-prs tier " + _filter_tokens.value_error(bad))
    return filters


def filter_string(filters: dict[str, str]) -> str:
    """The filter back in `gh-prs` arg form, in one fixed spelling."""
    return ",".join(f"{k}={v}" for k, v in sorted(filters.items()))


def scope_label(filters: dict[str, str], repo: str) -> str:
    """Named on every board, the default included (#486).

    An unlabelled board spells both "this is the default population" and
    "nobody said which population this is", and the filter does not survive an
    invocation.

    The default population is the repo (#1230). It read `author=@me (default)`
    until then, and that was accurate about a board it should not have been
    building: `gh-prs` dropped the implicit author filter in #1207 and this
    tier kept it, so the op a maintainer tick opens with excluded every
    dependabot and outside-contributor PR — three green ones, unseen for
    between five hours and a day on 2026-08-09, found by a human opening
    GitHub in a browser (#1071). Disclosure was already the argument that lost
    there, and it loses harder here: this board is a *delta*, so a PR that was
    never in the population cannot appear as a change, and there is not even a
    row next to which to read the scope line.
    """
    spelled = filter_string(filters)
    if not spelled:
        return f"scope every author (default) on {repo}"
    return f"scope {spelled} on {repo}"


def repo_name() -> str:
    """`owner/name` this board is about — the target, or the cwd's clone."""
    return _repo_target.effective_slug(timeout=20) or "?"


# ---------------------------------------------------------------------------
# 1. live truth
# ---------------------------------------------------------------------------

def live_open_prs(filters: dict[str, str]) -> list[dict]:
    """Every open PR the filter describes, annotated. `RadarError`, never [].

    The filter is the caller's alone — `_build_list_cmd` adds no role of its
    own since #1230, so `radar` and `gh-prs` answer over one population. One
    live poller is spawned per row, so widening the board widens the fleet;
    that is bounded by the page (`per_page`, 50 by default) exactly as it was
    before, and `DEATH_RESPAWN_LIMIT` is a per-slot respawn cap rather than a
    fleet size, so nothing here caps a wide board that did not cap a narrow one.

    One `gh pr list` call: unlike GitLab, the rollup and the review decision
    ride on the list response, so the board costs one request. Review-thread
    enrichment is deliberately skipped — it is one GraphQL call per PR and the
    `[threads]` flag is not a completeness claim about anything.
    """
    cfg = prs._get_config()
    cmd = prs._build_list_cmd(filters, cfg["per_page"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        # The spawn itself never completed, so nothing was asked of GitHub:
        # `gh` absent, hung against the 30s budget, or killed. Unreachable by
        # construction (#1568).
        raise RadarUnreachable(f"gh pr list failed: {exc}") from exc
    if result.returncode != 0:
        # Flattened, because quoting the stderr is this issue's remedy and it
        # is also how the remote gets a say in a line the reader takes as
        # radar's (#1485). `gh` echoes GitHub's own error body, and radar
        # renders a tier failure at column 0 of its own stderr — so a newline
        # in here puts whatever follows it at column 0 too, in radar's voice.
        # The op-level twin already does exactly this to exactly this value
        # (`presets/github/prs.py`); the tier did not, and #1823 widened the
        # exposure by adding `{err}` to the one arm that used to be a fixed
        # string. One call at the point `err` is bound covers every arm below.
        err = _untrusted.flat((result.stderr or "").strip()) or "unknown error"
        if result.returncode < 0:
            # Not a finished answer. `subprocess` reports `-N` for a POSIX
            # signal, and a process that was killed did not finish deciding
            # anything — under a loaded `-n auto` run the OOM killer and a
            # runner's own reaper both land here, usually with empty stderr,
            # which the arm below would render as `gh pr list: unknown error`
            # and read as a verdict about the board. Not a prose match: the
            # sign of the return code is the whole predicate (#1568).
            #
            # The word "signal" is not asserted, deliberately (#1871). On
            # Windows the same field is the process exit status —
            # `_winapi.GetExitCodeProcess` is declared `unsigned long`, so a
            # negative value there is unexpected — but the negative spelling
            # of an NT status DWORD circulates widely enough (shells, Python
            # 2) that nobody here trusts the premise without a Windows runner
            # to check it on. Reasoned, not observed either way. The
            # classification below — the process did not finish, retry — is
            # right regardless; only the mechanism word would be wrong, so it
            # is hedged rather than stated. `gl_mrs` carries the same wording
            # for the same reason: one change to both tiers, not a divergence.
            raise RadarUnreachable(
                f"gh pr list did not finish before it answered (returncode "
                f"{result.returncode}, consistent with a killing signal on "
                f"POSIX — not established on Windows, see #1871): {err}")
        if result.returncode == GH_RC_NO_CREDENTIALS:
            # Checked before the message arms below, because `gh` spells this
            # differently depending on whether it thinks it is interactive, and
            # the CI spelling matches none of them.
            raise RadarUnconfigured(
                "gh has no credentials in this environment, so it refused "
                "before making a request: " + err)
        low = err.lower()
        if _auth_probe.says_not_authenticated(err):
            # State 2: the probe got an answer *saying* the credential is
            # unusable, so the remedy is a claim about a cause something
            # established, and it stays (#1823).
            raise RadarUnreachable(
                f"gh says this request was not authenticated (exit "
                f"{result.returncode}): {err}. Run: gh auth login")
        if "rate limit" in low or "http 403" in low:
            raise RadarUnreachable(
                f"gh refused the query (rate limit or permission, exit "
                f"{result.returncode}): {err}")
        if _unreachable(err):
            raise RadarUnreachable(
                f"gh could not reach the API (exit {result.returncode}): {err}")
        # State 3: `gh` failed and nothing here established why. The issue's
        # own fallback -- quote the exit status and the stderr of the call that
        # did not answer, rather than name a cause. Naming one costs more than
        # an inaccurate string: a loop told the credential is gone stops, where
        # a loop told the tier could not answer retries and continues.
        raise RadarError(
            f"gh pr list did not answer, and nothing in its output says why "
            f"(exit {result.returncode}): {err}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError("could not parse gh JSON output") from exc
    if not isinstance(data, list):
        raise RadarError("gh returned no PR list")
    prs._annotate(data)
    return data


# ---------------------------------------------------------------------------
# 2. the green claim is the one worth proving
# ---------------------------------------------------------------------------

def _reconcile_one(p: dict) -> tuple[str, list[str]]:
    """`gh-pr`'s reconciliation, not a second copy of it (#724/#804/#837)."""
    return pr._reconcile_checks(p)


def verify_green(open_prs: list[dict], cap: int = RECONCILE_CAP) -> list[str]:
    """Mark green PRs whose rollup cannot be squared with the declared legs.

    Only the greens, because red is already a finding and running is already
    unknown — a doubt attached to either changes no action. Capped, and the cap
    is disclosed when it cuts: a budget that silently stopped checking is the
    same silence one level down.
    """
    greens = [p for p in open_prs if p.get("_checks") == "success"]
    if not greens or cap <= 0:
        return ([f"radar: NOTE — green PRs are not being reconciled against "
                 f"their declared legs (reconcile_cap={cap}); a short rollup "
                 f"is indistinguishable from a complete one here."]
                if greens else [])
    lines: list[str] = []
    for p in greens[:cap]:
        marker, detail = _reconcile_one(p)
        if marker:
            p["_unverified"] = marker
            lines.append(
                f"radar: WARNING — #{p.get('number')} shows every check green, "
                f"but the tally could not be squared with what its runs declare "
                f"({marker}), so whether these are all the legs is UNKNOWN.")
            lines.extend(detail)
    if len(greens) > cap:
        lines.append(
            f"radar: WARNING — {len(greens) - cap} of {len(greens)} green PRs "
            f"were not reconciled against their declared legs (reconcile_cap is "
            f"{cap}): a short rollup among them is indistinguishable from a "
            f"complete one. Raise it in the tier's options.")
        for p in greens[cap:]:
            p["_unverified"] = "not reconciled"
    return lines


def unchecked(open_prs: list[dict]) -> list[str]:
    """PR numbers whose check state this board did not establish.

    Two ways in, one word: no rollup at all, and a green whose legs could not
    be reconciled. Both mean *unknown*, and unknown sorted among the green with
    nothing saying so is the defect (#659) on the other platform.
    """
    return [str(p.get("number")) for p in open_prs
            if not str(p.get("_checks") or "") or p.get("_unverified")]


# ---------------------------------------------------------------------------
# 3. coverage and heal
# ---------------------------------------------------------------------------

def watch_coverage() -> set[str] | None:
    """Numbers with a live `github-pr` poller, or `None` when unknowable.

    `None` under a repo target and it is not a shortcut: the pid filename is
    keyed by PR number with no repo (#673), so a live poller for `#12` of some
    other clone is indistinguishable from this board's `#12`. An empty set
    would assert nobody is watching; the set itself would mark the wrong rows.
    """
    return prs._watched_numbers(transport.STATE_DIR)


def heal(numbers: list[str], watched: set[str] | None,
         watch) -> tuple[list[str], list[str]]:
    """`(healed, uncovered)` — one live poller per open PR.

    Spawning goes through radar's `_watch`, never `dispatcher.start_poller`:
    radar owns the #476 slot claim and the #513 death cap, records every slot,
    and emits the cap warning itself. A tier that spawned directly would be a
    second bound to keep in step with the first.

    `watched is None` heals nothing at all — see `watch_coverage`. A slot
    already alive is neither healed nor uncovered: nothing was spawned here, so
    claiming the action would be false, but the PR *is* covered.
    """
    if watched is None:
        return [], []
    healed: list[str] = []
    uncovered: list[str] = []
    for number in [n for n in numbers if n not in watched]:
        status = watch(SOURCE, number, [])
        if status == "spawned":
            healed.append(number)
        elif status == "alive":
            continue
        else:
            uncovered.append(number)
    return healed, uncovered


# ---------------------------------------------------------------------------
# 3b. feed — the part that discovers PRs this tier has never seen (#1780)
# ---------------------------------------------------------------------------
#
# The GitLab tier has had this since #528: `gl_mrs.py` runs a
# `gitlab-mr-feed` poller as a first-class part of reconcile, and its own
# module docstring calls that "the discovery guarantee". This tier's own
# docstring stated the absence rather than closing it — "there is no
# discovery feed [...] a PR opened after this run is discovered on the next
# radar tick and not before" — and #1780 is that sentence being read as a
# defect rather than a disclosure. `github-pr-feed` closes it the same way
# `gitlab-mr-feed` did: one poller over the population, spawned and kept
# alive here exactly like a per-PR watcher is.

FEED_SOURCE = "github-pr-feed"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN",
              "capped": "feed DOWN (respawn capped)",
              "unknown": "feed coverage UNKNOWN (#673)"}

# Every event this source can emit (see `sources/github-pr-feed/events.json`).
# Unlike `gitlab-mr-feed`'s DEFAULT_FEED_ONLY, this is not a separate,
# hand-kept filter that can drift from the source's own vocabulary — the tier
# always wants every discovery event a feed it spawned can produce, so the
# filter is simply "all of them", read off the one list this tier already
# needs for the pid-name / scope reasoning above.
FEED_ONLY = ("pr_opened", "pr_merged", "pr_closed", "pr_left_feed", "prs_unreachable")


def feed_scope(filters: dict[str, str] | None = None) -> str:
    """The feed watcher id covering this population.

    `filter_string` is already the canonical spelling this tier uses
    everywhere else (the snapshot key, the board label), so reusing it here
    is what keeps `@me` and `author=@me` from becoming two feed pollers over
    one population the way `gl_mrs.feed_scope` guards against for its own
    dict-of-lists filter. The empty filter — the whole-repo default since
    #1207 — gets its own alias rather than an empty pid filename, matching
    `github-pr-feed.ALIASES["@open"]`.
    """
    filters = {} if filters is None else filters
    return filter_string(filters) or "@open"


def feed_pid(scope: str = "@open") -> int:
    """PID recorded for the feed poller, or 0 when there is no readable file."""
    return transport.read_pid(FEED_SOURCE, scope)


def other_feed_scopes(scope: str) -> list[str]:
    """Live feed pollers covering a population other than this board's.

    Changing the filter respawns the feed; the previous one is not retired,
    so a machine can carry a feed started with one scope while the current
    invocation resolved another. Read-only: pid files only, nothing is
    spawned and nothing is killed — see `gl_mrs.other_feed_scopes` for the
    same guarantee on the GitLab side.
    """
    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == FEED_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def feed_error(scope: str) -> str:
    """Last error the feed poller recorded, or "" when it is polling cleanly.

    A feed that is alive but erroring every tick discovers nothing while
    looking healthy in `watches` — the same silence as a dead one, so it gets
    the same report. The dispatcher clears this key on a successful poll, so a
    message here is current rather than a scar.
    """
    state = transport.read_state(FEED_SOURCE, scope)
    # Flat, because this is a render (#1197): a poller exception message can
    # carry newlines, and this board prints it into a multi-line report.
    return _untrusted.flat(str((state.get("last_error") or {}).get("message") or ""))


#: The feed poller's own `LOOKUP_UNAVAILABLE`, spelled again rather than
#: imported — importing it would load the source module on every board
#: render. `test_watch_feed_unreachable_1602`-style coverage keeps the two in
#: step so the copy cannot drift into a check that silently never matches.
FEED_LOOKUP_UNAVAILABLE = "unavailable"


def feed_blind(scope: str) -> str:
    """Why the feed could not establish its population, or "" when it could.

    A third fact, not a rewording of the two above: `feed_pid` answers "is the
    poller running", `feed_error` answers "did it crash" — a poll that reached
    GitHub and got a 401 raises nothing inside the poller. It returns
    cleanly, having seen nothing, and both of those two report a healthy
    feed. `gl_mrs.feed_blind` is the same read on the GitLab side (#1602).

    The empty string means two different things to this function's own
    caller — "not blind" and "blind, but the poller recorded no message" —
    and only one of them may return it, or a blind feed with a blank `error`
    would render exactly like a healthy one. Every path in
    `github-pr-feed/poller.py::fetch_population` that sets `lookup` to
    unavailable currently supplies a non-empty `error`, so this is a latent
    gap rather than a reproduced one — but nothing enforces that across the
    two files, so the fallback stays here rather than relying on it staying
    true.
    """
    state = transport.read_state(FEED_SOURCE, scope).get("source_state") or {}
    if not isinstance(state, dict) or state.get("lookup") != FEED_LOOKUP_UNAVAILABLE:
        return ""
    return (_untrusted.flat(str(state.get("error") or ""))
            or "(feed recorded no error message)")


# ---------------------------------------------------------------------------
# 4. the default branch — the member that is not a PR
# ---------------------------------------------------------------------------
#
# #2024: the row above answered "is master green right now", on demand, once
# per radar tick. Nothing pushed that answer between ticks, so the poller
# everyone actually wanted -- the one that survives a reboot or a `pkill` and
# tells a consumer the moment master goes red -- was the one nobody had
# running. `sources/gh-branch/poller.py` (#1953) is that poller; this section
# spawns and heals it exactly like `github-pr-feed` above, through the same
# `_watch` callable, so radar owns the death cap and the ledger.

BRANCH_SOURCE = "gh-branch"

# Every event `sources/gh-branch/events.json` can emit -- not just
# `went_green`. Subscribing to `went_green` alone would make a `gh` outage
# that could not even look arrive as silence, which is the same shape of bug
# this whole section exists to close: `branch_unreachable` is what a lookup
# failure emits, and it is edge-triggered (once per outage, not once per
# poll) so it must be on the filter or it is never seen at all.
BRANCH_ONLY = ("went_green", "went_not_green", "no_run", "unknown",
               "branch_unreachable")

#: The poller's own `LOOKUP_UNAVAILABLE`, spelled again rather than imported
#: — see `FEED_LOOKUP_UNAVAILABLE`'s own docstring for why.
BRANCH_LOOKUP_UNAVAILABLE = "unavailable"


def other_branch_scopes(scope: str) -> list[str]:
    """Live `gh-branch` pollers watching a ref other than this board's.

    Read-only, mirroring `other_feed_scopes` exactly: pid files only, nothing
    spawned, nothing killed.

    The hazard it names is sharper than the feed's own duplicate-discovery
    cost. Changing the feed's filter respawns the feed and does not retire
    the stray -- worst case, something is discovered twice. A branch poller
    has the same "the old one is not retired" shape and it bites harder: after
    a `master` -> `main` rename that keeps the old ref alive (the ordinary
    case), a stale poller left running on `master` keeps emitting
    `went_green` forever, and that green is indistinguishable from a real one
    -- a false verdict about the branch people actually merge into, not
    merely a duplicate. That is worse than the silence #2024 closes, which is
    why a stray here is read and named on the board rather than left as a
    disclosed, accepted cost the way the feed's is: it costs `healthy` in
    `radar_report` (never `quiet_when_healthy`-suppressible) and the warning
    names the exact scope an operator clears with one call --
    `unwatch:gh-branch:<stray ref>`. If the old ref were deleted instead of
    kept, the poller would say so itself (`branch_unreachable`) and the case
    would already be loud; this reader exists for the quiet, kept-old-ref
    shape, which is the only one that was ever silent.
    """
    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == BRANCH_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def branch_poller_error(scope: str) -> str:
    """Last error the branch poller recorded, or "" when it is polling cleanly.

    Same shape as `feed_error`: the dispatcher clears this key on a
    successful poll, so a message here is current rather than a scar.
    """
    state = transport.read_state(BRANCH_SOURCE, scope)
    return _untrusted.flat(str((state.get("last_error") or {}).get("message") or ""))


def branch_poller_blind(scope: str) -> str:
    """Why the branch poller could not establish a state, or "" when it could.

    A third fact, not a rewording of the two above -- see `feed_blind`'s own
    docstring for the argument: a poll that reached GitHub and got a 401
    raises nothing inside the poller, so it returns cleanly having seen
    nothing, and both `_watch`'s own status and `branch_poller_error` report
    a healthy poller over that silence unless this is read separately.
    """
    state = transport.read_state(BRANCH_SOURCE, scope).get("source_state") or {}
    if not isinstance(state, dict) or state.get("lookup") != BRANCH_LOOKUP_UNAVAILABLE:
        return ""
    return (_untrusted.flat(str(state.get("error") or ""))
            or "(branch poller recorded no error message)")


def _branch_poller_warnings(poller: str, err: str, others: list[str],
                            blind: str = "") -> list[str]:
    """`_feed_warnings`'s twin for the standing `gh-branch` poller (#2024).

    Silent when the poller is `alive`/`spawned` with no error, no blind spot
    and no stray scope — the same "speak only when there is something to
    say" calibration `default_branch_report` already applies to the board
    row itself (see its own docstring and #1077): a board that named a
    healthy poller on every tick would be exactly the habituation failure
    this tier is already built against, and it would also defeat the
    existing "blank on a fully-resolved green" contract these lines are
    prepended to. What must never be silent is degradation — see the return
    value's own reasoning below.
    """
    out = []
    if poller == "failed":
        out.append("radar: WARNING — default branch poller is down. A branch "
                   "that goes green or red after this point will not be "
                   "reported until the next radar tick.")
    elif poller == "capped":
        out.append("radar: WARNING — default branch poller has died too often "
                   "and is no longer being respawned. A branch that goes "
                   "green or red after this point will not be reported until "
                   "the next radar tick.")
    elif poller == "unknown":
        out.append("radar: WARNING — default branch poller coverage is "
                   "UNKNOWN for this board (#673, the same repo-blind pid "
                   "naming that leaves per-PR watch coverage unknown under a "
                   "repo target). Nothing was spawned; run radar from a clone "
                   "of that repo to get coverage back.")
    elif err:
        out.append(f"radar: WARNING — default branch poller is failing to "
                   f"poll: {err}")
    if blind:
        out.append(f"radar: WARNING — default branch poller is alive but "
                   f"could not establish the branch's state on its last "
                   f"poll: {blind}")
    for other in others:
        out.append(f"radar: WARNING — a default branch poller is also live "
                   f"for {other!r}, which this board is not reporting on. "
                   f"After a rename that kept the old ref, its green is "
                   f"indistinguishable from a real one for anyone still "
                   f"reading it — run "
                   f"{_st_hint.st_hint(f'unwatch:gh-branch:{other}')} to "
                   f"retire it.")
    return out


def default_branch_report(ref: str | None, repo: str,
                          watch=None) -> tuple[list[str], bool, bool]:
    """`(lines, could_tell, poller_ok)` for the branch nothing else watches.

    Composed from `gh-branch`'s own selection, verdict, reconciliation **and
    scope**, so the answer here and the answer from `gh-branch:master` are the
    same arithmetic rather than two renderings that agree today. `could_tell` is
    False for `UNKNOWN` and for `NO RUN` — neither establishes a green, and
    this tier's `healthy` is a claim about what it could see — and, since
    #1077, for a green whose scope is unresolved: a declared set that could not
    be read, or a push-triggered workflow that produced no run on the head
    commit. Those are greens over a universe of unknown size, which is the same
    claim-about-what-it-could-see failing.

    `ref is None` means "not configured" and resolves the repo's own default
    branch; `""` means the operator switched the member off. Two different
    intentions, and collapsing them would either cost a call nobody asked for
    or drop the row that this whole member exists for. `""` also means: do
    not spawn a poller either -- switching the row off is the operator's
    call being honoured, not overridden (#2024).

    `poller_ok` is a **third, independent** claim, deliberately not folded
    into `could_tell`: `could_tell` is about the query this call just made,
    `poller_ok` is about the standing watcher between now and the next tick.
    A red master reported by a perfectly healthy direct query is
    `could_tell=True` regardless of whether the poller is up -- conflating
    the two would have made `test_a_red_master_is_unaffected`'s own claim
    false. `watch` defaults to `_no_watch`, which always answers "failed":
    a caller with no spawner configured cannot claim the poller is up.
    """
    watch = watch or _no_watch
    if ref is None:
        ref = branch._repo_identity()[1]
    if not ref:
        return [], True, True

    poller = watch(BRANCH_SOURCE, ref, list(BRANCH_ONLY))
    poller_err = branch_poller_error(ref) if poller == "alive" else ""
    poller_blind = branch_poller_blind(ref) if poller == "alive" else ""
    poller_others = other_branch_scopes(ref)
    poller_lines = _branch_poller_warnings(poller, poller_err, poller_others,
                                           poller_blind)
    poller_ok = (poller not in ("failed", "capped", "unknown")
                and not poller_err and not poller_blind and not poller_others)

    sha, age, err = branch._head_commit(ref)
    if err:
        return (poller_lines +
                [f"radar: {ref} — {branch.UNKNOWN}: {err} The default branch's "
                 f"state is not established; a red master looks exactly like "
                 f"this line being absent."], False, poller_ok)

    runs, err = branch._run_list(ref)
    if err or runs is None:
        return (poller_lines +
                [f"radar: {ref} — {branch.UNKNOWN}: {err or 'run list unreadable'}"],
                False, poller_ok)

    selected = branch.runs_on_sha(runs, sha)
    _prev_sha, prev_names = branch.previous_head(runs, sha)
    # `missing_workflows`, not `prev_names - set(selected)`: the selection is
    # keyed per run since #1640, so a workflow with two runs on the head is in
    # neither key verbatim and the subtraction reported it as absent — a NOT
    # GREEN on every tick, invented out of a spelling.
    missing = branch.missing_workflows(prev_names, selected)

    fetched: dict = {}
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(branch.JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(selected, pool.map(
                lambda n: branch._jobs_for(branch._run_id(selected[n])), selected)))
    legs = {name: (None if jobs is None
                   else [_checks.github_state(j) for j in jobs])
            for name, jobs in fetched.items()}

    marker, shortfall = branch._reconcile(repo, selected, fetched)
    # #1077: `scope_for` says a caller that has to remember this will not, and
    # this was the caller that did not. Without it the tier published the same
    # unscoped green #846 exists to stop — on the one board that reports master
    # on every tick.
    scope, scope_lines, unresolved = branch.scope_for(repo, sha, selected)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)

    lines = [f"radar: {ref} @ {sha[:7]} — {sentence}"]
    lines.extend(f"  {line}" for line in shortfall)
    for name in missing:
        lines.append(f"  {_untrusted.flat(name)} ran on the previous head and "
                     f"has no run on {sha[:7]} — that is not 'ran and passed'.")
    lines.extend(f"  {line}" for line in scope_lines)
    # The blank-on-green is what made the disclosure unreachable, and removing
    # it outright would be the opposite mistake: on this repo `slow tests`
    # (schedule) and `changelog` (pull_request) produce no run on any master
    # push, forever, so an unconditional clause is two permanent lines on every
    # tick — the render nobody reads by the time it matters. `unresolved` is
    # the narrower question: a green this tier cannot account for, because a
    # push-triggered workflow produced no run or the declared set could not be
    # read at all. Those it says out loud.
    if state == branch.GREEN and not unresolved:
        lines = []
    # `could_tell`, not just the lines. Radar's `quiet_when_healthy` drops a
    # healthy tier's whole output, so lines emitted under a healthy verdict go
    # nowhere — un-blanking them without moving this would have looked fixed
    # and disclosed nothing to an operator running quiet.
    #
    # Only against the green, deliberately, and for the same reason `verdict()`
    # tests `unreconciled` last: every other state here is a *finding*, and a
    # finding is something this tier could tell. A green is a clearance, and a
    # clearance over a set of unknown size is not one.
    could_tell = (state == branch.NOT_GREEN
                  or (state == branch.GREEN and not unresolved))
    return poller_lines + lines, could_tell, poller_ok


# ---------------------------------------------------------------------------
# 5. snapshot and report
# ---------------------------------------------------------------------------

def snap_entry(p: dict) -> dict[str, Any]:
    """The facts this tier reports about one PR. Delta is computed over these.

    `head_sha` is the load-bearing field and the GitHub-shaped one. A push that
    lands a new head commit re-runs everything; the rollup word can be
    identical either side of it ("failed" before, "failed" after) while the
    board is describing a different commit. GitLab keys this on `pipeline_id`;
    number alone would suppress a rerun as "no change", which on a board a
    maintainer merges from is a missed event rather than a phantom one.
    """
    return {
        "checks": str(p.get("_checks") or ""),
        "head_sha": str(p.get("headRefOid") or ""),
        "draft": bool(p.get("isDraft")),
        "mergeable": str(p.get("mergeable") or ""),
        "review": str(p.get("reviewDecision") or ""),
        "unverified": str(p.get("_unverified") or ""),
    }


def snapshot_key(filters: dict[str, str], repo: str) -> str:
    """Filter *and* repo. The same filter over two repos is two populations,
    and sharing one file would report each one's PRs as the other's churn.

    Deliberately unchanged by #1230, which widened what an empty filter means.
    A snapshot taken under the narrow default is therefore reused once — and
    that is the wanted behaviour, not a hazard: widening only *adds* members,
    so the old snapshot cannot manufacture a departure, and the PRs it never
    held print as new rows on the first wide tick, which is the announcement
    this board owes. Adding a scope token to the key instead would cold-start
    every board, discarding the `_since` stamps every staleness verdict
    (#1025) is measured from, to buy a full board this path already prints.
    """
    return snapshot.key({"repo": repo, "filters": dict(sorted(filters.items()))})


def snapshot_path(filters: dict[str, str], repo: str) -> str:
    return snapshot.path(SNAPSHOT_PREFIX, snapshot_key(filters, repo))


def _marks(p: dict, healed: set[str], uncovered: set[str],
           coverage_known: bool, stale_minutes: float = 0.0) -> str:
    out = []
    if p.get("_unverified"):
        out.append(f"[legs UNVERIFIED: {p['_unverified']}]")
    if stale_minutes:
        out.append(f"[{snapshot.unchanged_label(stale_minutes, str(p.get('_checks') or ''))}]")
    number = str(p.get("number", "?"))
    if not coverage_known:
        out.append("[watch?]")
    elif number in healed:
        out.append("[healed]")
    elif number in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(p: dict) -> bool:
    """A current fact, so never delta-suppressed. An unfixed red is not history."""
    return (p.get("_checks") == "failed"
            or p.get("mergeable") == "CONFLICTING"
            or bool(p.get("_unverified"))
            or not str(p.get("_checks") or ""))


def _stale_running(p: dict, previous_entry: Any, threshold: float,
                   now: str | None = None) -> float:
    """Minutes a `running` PR has been unchanged, past `threshold`. Else 0.

    `running` is deliberately *not* a standing problem: a pipeline in progress
    is the ordinary state of a PR that was just pushed, and reprinting it every
    tick is exactly what the delta exists to prevent. It is also the only state
    that can persist indefinitely **while being wrong** — a wedged leg, a
    runner that never picks the job up, a workflow waiting on an approval
    nobody will give. None of those ever changes, so the snapshot never
    mismatches and the row is suppressed on every tick after the first (#1025).

    So the elision is kept and given an expiry, rather than removed. Under the
    threshold this returns 0 and the row stays off the board.

    `None` from `unchanged_minutes` is *unknown*, and unknown is not stale: a
    board that flagged every row it could not date would train its reader to
    skim, which is the failure this whole surface is built against. The unknown
    resolves itself on the next write — see `_snapshot.unchanged_minutes`.
    """
    if threshold <= 0 or str(p.get("_checks") or "") != "running":
        return 0.0
    mins = snapshot.unchanged_minutes(previous_entry, now)
    if mins is None or mins < threshold:
        return 0.0
    return mins


def _footer(open_prs: list[dict], covered: set[str] | None, healed: list[str],
            uncovered: list[str], gone: int, feed: str, label: str,
            unchecked_n: int, elided_n: int = 0,
            departed_capped: bool = False) -> str:
    """Tallies over the whole open population, plus what the delta held back.

    `elided_n` is the token that makes the footer checkable against the rows
    above it (#1022): every other count here describes all `len(open_prs)` PRs
    while the board prints only those that moved, so without it a reader has
    `6 open | 4 running` over three rows and no way to tell a suppressed row
    from a merged one.
    """
    counts: dict[str, int] = {}
    for p in open_prs:
        key = str(p.get("_checks") or "none")
        counts[key] = counts.get(key, 0) + 1
    parts = [label, f"{len(open_prs)} open"]
    if elided_n:
        parts.append(f"{elided_n} unchanged not shown")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failing")
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    green = counts.get("success", 0) - sum(
        1 for p in open_prs if p.get("_checks") == "success" and p.get("_unverified"))
    if green:
        parts.append(f"{green} green")
    if unchecked_n:
        parts.append(f"{unchecked_n} unchecked")
    if covered is None:
        parts.append("watch coverage UNKNOWN")
    else:
        parts.append(f"{len([p for p in open_prs if str(p.get('number')) in covered])} watched")
    if healed:
        parts.append(f"{len(healed)} healed")
    if uncovered:
        parts.append(f"{len(uncovered)} unwatched")
    if gone:
        # Not "no longer open" (#1024): `open_prs` is filter-scoped, so a PR
        # that was reassigned away is gone from here and still open there. And
        # on a full page not even "left" is established — see `departed_note`.
        parts.append(f"{gone} off this page" if departed_capped
                     else f"{gone} left this board")
    # Stated on every board (#1780): this used to be the fixed string
    # "discovery: radar ticks only" no matter what. Now it names the feed's
    # own state, because "ticks only" was true when there was no feed to lose
    # and is a false claim of degraded coverage once there is one — and a feed
    # that is alive but blind or capped is not the same fact as one that was
    # never spawned, so the token has to be able to say which.
    parts.append(f"discovery: {FEED_LABEL.get(feed, feed)}")
    return " | ".join(parts)


def _coverage_warning(covered: set[str] | None) -> list[str]:
    if covered is not None:
        return []
    return ["radar: WARNING — watch coverage is UNKNOWN for this board. Watch "
            "state is keyed by PR number with no repository (#673) and this "
            "board is about a repo target, so a live poller for #N cannot be "
            "told apart from #N of the clone it was started in. Nothing was "
            "healed; run radar from a clone of that repo to get coverage back."]


def _feed_warnings(feed: str, feed_err: str, others: list[str],
                   feed_blind: str = "") -> list[str]:
    """A blind board must say so. A dead, capped or erroring feed discovers
    nothing, and a healthy-looking footer over a dead feed is #1780's own
    defect one render up — see `gl_mrs._feed_warnings` for the GitLab twin
    this mirrors line for line.
    """
    out = []
    if feed == "failed":
        out.append("radar: WARNING — PR feed poller is down. New PRs will not be "
                   "discovered until the next radar tick.")
    elif feed == "capped":
        out.append("radar: WARNING — PR feed poller has died too often and is no "
                   "longer being respawned. New PRs will not be discovered until "
                   "the next radar tick.")
    elif feed == "unknown":
        out.append("radar: WARNING — PR feed coverage is UNKNOWN for this board "
                   "(#673, the same repo-blind pid naming that leaves per-PR "
                   "watch coverage unknown under a repo target). Nothing was "
                   "spawned; run radar from a clone of that repo to get "
                   "discovery back.")
    elif feed_err:
        out.append(f"radar: WARNING — PR feed poller is failing to poll: {feed_err}")
    if feed_blind:
        out.append(f"radar: WARNING — PR feed poller is alive but could not "
                   f"establish the population on its last poll: {feed_blind}")
    for other in others:
        out.append(f"radar: NOTE — a PR feed poller is also live on scope "
                   f"{other!r}, which this board did not resolve to. Its "
                   f"discoveries are not this board's.")
    return out


def _unchecked_warning(numbers: list[str], total: int) -> list[str]:
    """[] when the board saw everything — the absence *is* the positive claim."""
    if not numbers:
        return []
    shown = ", ".join(f"#{n}" for n in numbers[:8])
    if len(numbers) > 8:
        shown += f", +{len(numbers) - 8} more"
    return [f"radar: WARNING — {len(numbers)} of {total} PRs on this board have "
            f"no established check state ({shown}): unknown, not green, so a "
            f"failing one among them is indistinguishable from a passing one "
            f"here."]


def _departed(previous: dict | None, open_prs: list[dict]) -> list[str]:
    """Numbers in the previous snapshot and not in the live population.

    A function rather than two lines inside `render`, because `radar_report`
    needs the same answer for `healthy` and a second derivation is how the two
    come to disagree (#1024).
    """
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    live = {str(p.get("number")) for p in open_prs}
    return [n for n in prev_entries if n not in live]


def render(open_prs: list[dict], covered: set[str] | None, healed: list[str],
           uncovered: list[str], previous: dict | None, label: str,
           notes: list[str] | None = None,
           page_capped: bool = False,
           now: str | None = None,
           stale_running_minutes: float = STALE_RUNNING_MINUTES,
           feed: str = "alive", feed_err: str = "",
           other_feed_scopes: list[str] | None = None,
           feed_blind: str = "") -> list[str]:
    """Full board on cold start; changed + standing-problem rows afterwards.

    Every open PR lands in exactly one of `shown` and `elided`, and both are
    reported (#1022). The partition is the fix: the footer counts the whole
    population and the loop prints a subset, so the two were free to disagree,
    and a board that quietly prints three of six rows is byte-identical to a
    board with three PRs on it.

    The elision itself is kept. A running PR that has not moved since the last
    tick is genuinely no news, and re-printing it every tick is how a board
    trains its reader to skim. What was wrong was the silence, not the choice.
    """
    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)
    coverage_known = covered is not None
    unchecked_numbers = unchecked(open_prs)

    shown = []
    elided: list[str] = []
    for p in sorted(open_prs, key=prs._sort_key):
        number = str(p.get("number", "?"))
        prev_entry = prev_entries.get(number)
        # `facts`, not the raw entry: the entry also carries `_since`, which
        # changes shape between versions and must never read as a move.
        moved = snapshot.facts(prev_entry) != snap_entry(p)
        notable = number in healed_set or number in uncovered_set
        stale = _stale_running(p, prev_entry, stale_running_minutes, now)
        if cold or moved or notable or _is_standing_problem(p) or stale:
            shown.append(prs._row(p, covered,
                                  _marks(p, healed_set, uncovered_set,
                                         coverage_known, stale)))
        else:
            elided.append(number)

    departed = _departed(previous, open_prs)
    footer = _footer(open_prs, covered, healed, uncovered, len(departed), feed,
                     label, len(unchecked_numbers), len(elided), page_capped)

    # Named only when the board is *partial*. A board where everything was
    # elided already says so unambiguously on its `radar: no change` line, and
    # the footer token still carries the arithmetic; listing every number there
    # would print the whole population back on a board whose entire claim is
    # that there is nothing to look at.
    elision = (snapshot.elided_note(elided, len(open_prs), "PRs", "#", "gh-prs")
               if shown else [])

    lines = (_coverage_warning(covered)
             + _feed_warnings(feed, feed_err, other_feed_scopes or [], feed_blind)
             + _unchecked_warning(unchecked_numbers, len(open_prs))
             + elision
             + snapshot.departed_note(departed, "PR", "#", "gh-pr:<number>",
                                      page_capped)
             + list(notes or []))
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
        lines.append(_untrusted.flat_note("PR titles"))
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:
        # "No open PRs" would be a claim about the world. What is true is that
        # this filter selected nothing, and the two are only the same sentence
        # when the filter is the whole population.
        lines.append(f"No PRs matched — {label}.")
        lines.append("")
        lines.append(footer)
    elif departed:
        # A departure is a change, and the only one this board can never print
        # as a row — the entry is gone, so there is nothing left to render and
        # every surviving row legitimately elided. Taking the `no change` arm
        # here announces that nothing happened on the one tick where something
        # fell off the board, which is the token a reader skims by (#1024).
        lines.append(f"radar: no rows changed | {footer}")
    else:
        lines.append(f"radar: no change | {footer}")
    return lines


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:
    """Fallback `_watch`. "failed", never "alive" — a tier with no spawner
    cannot establish coverage, and saying it did would be the house defect."""
    return "failed"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:
    """`(lines, healthy)` — the PR board, as radar's tier contract wants it.

    `healthy` means "this tier could tell you the truth", not "no PR is red". A
    board of failing PRs is a healthy report of an unhealthy world; a board
    with unknown coverage, unchecked rows, or a default branch it could not
    read is not.

    Raises `RadarError` when the board could not be built at all. Radar prints
    it on stderr and exits non-zero, deliberately louder than `healthy=False`,
    and nothing is healed or snapshotted on that path.
    """
    options = options or {}
    watch = options.get("_watch") or _no_watch
    filters = resolve_filter(str(options.get("_arg") or ""))

    repo = repo_name()
    open_prs = live_open_prs(filters)

    cap = options.get("reconcile_cap", RECONCILE_CAP)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = RECONCILE_CAP
    notes = verify_green(open_prs, cap)

    numbers = [str(p.get("number")) for p in open_prs if p.get("number") is not None]
    watched = watch_coverage()
    healed, uncovered = heal(numbers, watched, watch)
    covered = None if watched is None else watched | set(healed)

    # The feed follows the same #673 rule as per-PR healing: under a repo
    # target the pid filename cannot tell this board's scope apart from the
    # same scope started against another clone, so nothing is spawned and the
    # gap is reported rather than guessed shut (`feed == "unknown"`).
    scope = feed_scope(filters)
    if watched is not None:
        feed = watch(FEED_SOURCE, scope, list(FEED_ONLY))
        feed_err = feed_error(scope) if feed == "alive" else ""
        blind = feed_blind(scope) if feed == "alive" else ""
        other_scopes = other_feed_scopes(scope)
    else:
        feed, feed_err, blind, other_scopes = "unknown", "", "", []

    raw_ref = options.get("default_branch")
    branch_lines, branch_ok, branch_poller_ok = default_branch_report(
        None if raw_ref is None else str(raw_ref), repo, watch)

    label = scope_label(filters, repo)
    key = snapshot_key(filters, repo)
    previous = snapshot.read(SNAPSHOT_PREFIX, key, "prs")
    # One page, no pagination loop. A full page means the population may be
    # truncated, and a truncated population cannot establish which of its
    # previous members left (#1024).
    per_page = int(prs._get_config().get("per_page") or 0)
    page_capped = bool(per_page) and len(open_prs) >= per_page
    departed = _departed(previous, open_prs)
    stale_after = options.get("stale_running_minutes", STALE_RUNNING_MINUTES)
    try:
        stale_after = float(stale_after)
    except (TypeError, ValueError):
        stale_after = STALE_RUNNING_MINUTES
    stamped_at = snapshot.now_iso()
    lines = branch_lines + render(open_prs, covered, healed, uncovered,
                                  previous, label, notes, page_capped,
                                  now=stamped_at,
                                  stale_running_minutes=stale_after,
                                  feed=feed, feed_err=feed_err,
                                  other_feed_scopes=other_scopes,
                                  feed_blind=blind)
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    snapshot.write(SNAPSHOT_PREFIX, key,
                   {str(p.get("number")): snapshot.stamp(
                       snap_entry(p), prev_entries.get(str(p.get("number"))),
                       stamped_at)
                    for p in open_prs},
                   "prs")

    # `departed` counts against health because `healthy` has one consumer —
    # `quiet_when_healthy`, which drops this tier's whole output. A
    # departure-only tick is every row elided plus one summary line, so a
    # healthy verdict there suppresses the only notice that something left.
    # `feed`/`blind`/`other_scopes` count for the same reason `gl_mrs` counts
    # its own feed against health (#1780): a feed that is down, capped,
    # unknown or blind is this tier failing at the one job #1779 was filed
    # about, and a quiet radar must not hide that.
    healthy = bool(branch_ok) and bool(branch_poller_ok) \
        and not uncovered and covered is not None \
        and not unchecked(open_prs) and not departed \
        and feed not in ("failed", "capped", "unknown") \
        and not feed_err and not blind and not other_scopes
    return lines, healthy


# ---------------------------------------------------------------------------
# read-only inspection — the `watches` guarantee, for a tier
# ---------------------------------------------------------------------------

def radar_state(options: dict | None = None) -> list[str]:
    """What this tier knows, without spawning or calling anything.

    Inspection and action were fused on this subsystem once and the cost was
    hours of not looking, because looking had side effects. Nothing here forks,
    nothing here reaches the network: it reads the resolved config, the
    snapshot file on disk and the pid files, and says what it cannot answer.
    """
    options = options or {}
    out: list[str] = []
    try:
        filters = resolve_filter(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  filter    : REFUSED — {exc}"]

    target = _repo_target.target()
    repo = str(target) if target else "(the cwd's clone — not resolved here, "\
                                     "that would be a call)"
    out.append(f"  filter    : "
               f"{filter_string(filters) or 'none — every author (default)'}")
    out.append(f"  repo      : {repo}")

    raw_ref = options.get("default_branch")
    out.append("  default br: " + ("(resolved at report time)" if raw_ref is None
                                   else (str(raw_ref) or "(off)")))

    path = snapshot_path(filters, str(target) if target else "?")
    if target:
        try:
            with open(path, encoding="utf-8") as f:
                rows = len((json.load(f).get("prs") or {}))
            out.append(f"  snapshot  : {path} — {rows} PR(s)")
        except (OSError, json.JSONDecodeError):
            out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:
        # Honest about the one thing this view cannot do without a call: the
        # key includes the repo, and resolving the cwd's repo costs a request.
        out.append(f"  snapshot  : under {naming.flat_path(transport.STATE_DIR)}/"
                   f"{SNAPSHOT_PREFIX}.*.snapshot.json — the exact key needs "
                   f"the repo name, which is a call, so it is not resolved here")

    prefix = f"supertool-watch-{SOURCE}__"
    pids = sorted(os.path.basename(p)[len(prefix):-len(".pid")]
                  for p in glob.glob(os.path.join(transport.STATE_DIR,
                                                  f"{prefix}*.pid")))
    if target:
        out.append(f"  pollers   : {len(pids)} pid file(s) — UNKNOWN whether they "
                   f"cover this repo (#673): {', '.join('#' + n for n in pids) or 'none'}")
    else:
        out.append(f"  pollers   : {', '.join('#' + n for n in pids) or 'none'}")

    # #1780: this view reads files and never asks whether a poller is alive,
    # so — like `gl_mrs.radar_state`'s own feed row — the pid row below says
    # whether anyone is still trying, and `feed_blind` says whether the last
    # poll that did run could see anything.
    scope = feed_scope(filters)
    if target:
        out.append(f"  feed      : scope {scope!r} — UNKNOWN whether a live pid "
                   f"here covers this repo (#673); nothing is spawned for it "
                   f"under a repo target")
    else:
        pid, pid_refusal = transport.read_pid_checked(FEED_SOURCE, scope)
        err = feed_error(scope)
        out.append(f"  feed      : scope {scope!r}, pid "
                   f"{pid or pid_refusal or 'none recorded'}"
                   f"{f' — last error: {err}' if err else ''}")
        blind = feed_blind(scope)
        if blind:
            out.append(f"  feed sight: last poll could NOT establish the "
                       f"population — {blind}")
        for other in other_feed_scopes(scope):
            out.append(f"  feed ALSO : scope {other!r} is live and is NOT on this board")
    return out
