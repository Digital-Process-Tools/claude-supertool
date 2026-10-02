"""_supertool_presets -- custom-op / preset resolution, split out of
_supertool.py (#2706).

Loaded by `_load_part("_supertool_presets")` from inside `_supertool.py`, at
the exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "_safe_path", ...)` (and the
other names this part defines) keeps reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_presets` or `python3 _supertool_presets.py` gets this
freshly loaded module its own globals(), which has no such name, and refuses
with a clear ImportError rather than failing later with a NameError on the
first name this file assumes `_supertool.py` already defined (Dict, Any, os,
re, shlex, ...).
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_presets.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

# ---------------------------------------------------------------------------
# Custom ops and aliases — config-driven dispatch extensions
# ---------------------------------------------------------------------------

class SecurityError(Exception):
    """Raised when a path arg violates the cwd containment policy."""
    pass


# Hard cap on path length passed to _safe_path. Sized well above MAX_PATH (260)
# and the extended-length namespace (32767) is too permissive for an op arg —
# 4096 catches obvious abuse (1MB args from fuzz tests) while leaving every
# legitimate path well under the limit.
_MAX_SAFE_PATH_LEN = 4096

#: The opt-out sentence every containment refusal ends with. One constant
#: rather than one copy per refusal site: `glob` refuses without naming the
#: file it matched (#1366), so it cannot reuse `_safe_path`'s message wholesale
#: and would otherwise have retyped this — and a retyped remedy drifts out of
#: step with the knobs it names.
_ALLOW_OUTSIDE_HINT = (
    "For a one-off call, no config edit and no residue: prefix the call "
    "with `cwd:PATH` to move the boundary there for this call only "
    "(#1784). To allow it for every future call: set "
    "SUPERTOOL_ALLOW_OUTSIDE_CWD=1 (env), or add "
    '`"allow_outside_cwd": true` to .supertool.json.'
)


def _safe_path(p: str, *, allow_outside_cwd: Optional[bool] = None,
               root: Optional[str] = None, boundary: str = "cwd") -> str:
    """Resolve `p` and enforce repo-root containment (closes #146).

    Strict mode (default): the realpath of `p` must equal cwd or live under
    cwd. Symlinks crossing the boundary are rejected. `..` traversal that
    escapes cwd is rejected. Returns the resolved absolute path.

    `root` moves the boundary without moving the rule (#1287). The core's own
    boundary is the cwd and stays the default; a preset op that resolves its
    argument against something else — `claims` resolves a relative path against
    the repository root, so a cwd boundary would refuse `claims:docs/x.md` run
    from `docs/` — declares that root and gets the *same* check under it.
    `boundary` is only the word the refusal uses, so the message names the line
    the caller actually crossed. Parameterised rather than reimplemented: the
    one thing this repo has learned twice about containment is that a second
    copy of the rule drifts (#882, #889).

    Opt-out (any one is enough):
      1. `allow_outside_cwd=True` per-call kwarg
      2. `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` env var (CI / one-off)
      3. `"allow_outside_cwd": true` in `.supertool.json` (project-pinned)

    Test suites set the env var via conftest.py so tmp_path-based fixtures
    keep working; production deployments leave it unset.

    Trust model note: lookup (3) reads from the project's `.supertool.json`,
    same trust level as the other knobs there (validators, custom ops,
    presets). A user cloning a hostile repo is already running its code via
    supertool validators / ops; in-config opt-out adds no new attack
    surface. The env var takes precedence for one-off overrides.

    `~` expansion happens via os.path.expanduser — a user-supplied
    `~/.ssh/id_rsa` is resolved to the real path BEFORE the cwd check, which
    is what catches the threat. `$VAR` expansion does NOT happen here (#1370):
    no op gated by THIS check ever expands `$` when it opens the file — every
    such op opens the literal, unexpanded path — so a check that expanded
    `$HOME` while the op opened the literal string `$HOME` refused a
    directory that never escaped cwd at all, only its expanded interpretation
    would have (had it existed). This is the same check/use mismatch #1300
    fixed for `~`, applied the other way: there, the fix was to make the
    *use* match the *check* (both expanded); here, `$` is a legal filename
    character that no op ever expands on open, so the coherent fix is to make
    the *check* match the *use* (neither expanded). One rule, applied
    uniformly, rather than a per-op special case that would drift
    `_containment_error` the way #1366's review specifically rejected for
    `glob`.

    One caller elsewhere in this file DOES still expand `$VAR`: the `cwd:PATH`
    CLI pre-pass (`os.path.expanduser(os.path.expandvars(...))` ahead of
    `os.chdir`). That is not a counter-example — `cwd:` never calls this
    function at all. It establishes a new boundary rather than checking
    against the existing one, so there is no check/use pair here to disagree
    with itself; the claim above is scoped to callers of `_safe_path`, not to
    every path-shaped argument this file ever reads.
    """
    if allow_outside_cwd is None:
        if os.environ.get("SUPERTOOL_ALLOW_OUTSIDE_CWD") == "1":
            allow_outside_cwd = True
        else:
            # Project config opt-in. Wrapped in try/except so a broken /
            # missing config never raises out of a path check.
            try:
                allow_outside_cwd = bool(_load_config().get("allow_outside_cwd"))
            except Exception:
                allow_outside_cwd = False
    # NUL byte rejection — os.path.* raises ValueError on embedded NULs which
    # would leak as an uncaught traceback. Reject early with a clean message.
    if "\x00" in p:
        raise SecurityError(f"path contains NUL byte: {p!r}")
    # Windows: paths longer than MAX_PATH (260) make _getfinalpathname raise
    # ValueError("path too long for Windows") from inside os.path.realpath.
    # Reject oversized paths up front with a clean SecurityError so dispatch
    # returns a clean "ERROR: ..." instead of an uncaught traceback. 4096 is
    # well above MAX_PATH (260) and extended-length (32767) workable values
    # — any real op path stays well under it.
    if len(p) > _MAX_SAFE_PATH_LEN:
        raise SecurityError(
            f"path too long ({len(p)} chars, max {_MAX_SAFE_PATH_LEN})"
        )
    expanded = os.path.expanduser(p)  # not expandvars — #1370
    try:
        abs_p = os.path.realpath(expanded)
    except (ValueError, OSError) as e:
        # Truncate path in the error message — matches existing SecurityError
        # style of not echoing arbitrarily-large user input back verbatim.
        shown = p if len(p) <= 120 else p[:120] + "…"
        raise SecurityError(f"path cannot be resolved: {shown!r} ({e})") from e
    if allow_outside_cwd:
        return abs_p
    # Windows: NTFS is case-insensitive (`C:\Users` == `c:\users`) and uses
    # backslash separators. `os.path.normcase` lowercases + normalises
    # separators on Windows; on POSIX it's a no-op so the check stays exact.
    # This also handles drive-letter case (`c:\` vs `C:\`) and forward-slash
    # variants (`C:/Users` vs `C:\Users`).
    abs_p_cmp = os.path.normcase(abs_p)
    base = os.path.realpath(root) if root else os.path.realpath(os.getcwd())
    root_cmp = os.path.normcase(base)
    if abs_p_cmp == root_cmp:
        return abs_p
    if not abs_p_cmp.startswith(root_cmp + os.sep):
        # %s for the root, %r for the caller's own string: on Windows a %r
        # doubles every separator, so a caller comparing the printed root
        # against a real one would never match (#1283). Named only when it is
        # NOT the cwd — the default message is load-bearing in a dozen tests
        # and gains nothing from echoing the directory the caller stands in.
        where = "" if root is None else f", root {base}"
        raise SecurityError(
            f"path escapes {boundary}: {p!r} (resolved to {abs_p!r}{where}). "
            + _ALLOW_OUTSIDE_HINT
        )
    return abs_p


def _containment_error(candidates: Iterable[str], *,
                       root: Optional[str] = None,
                       boundary: str = "cwd") -> Optional[str]:
    """The one containment gate every path-bearing route passes through.

    Returns the ``ERROR: …`` line to print, or ``None`` when every candidate is
    contained. Dispatch applies it positionally from ``_PATH_ARG_POSITIONS``;
    the ``@payload`` route applies it to the fields a payload names. **One rule,
    one implementation, several callers** — #882 was a second copy of this rule
    written beside the real one, and it covered the list form while missing the
    single-path one, so ``{"path":"/etc/hosts"}`` validated a file
    ``validate:/etc/hosts`` refuses. A third copy would drift the same way.

    ``""`` and ``"."`` are skipped because neither is a filename: one means no
    path was given, the other *is* cwd, and ``_safe_path`` allows both anyway
    — the skip saves a `realpath` and grants nothing.

    ``"full"`` and ``"raw"`` used to sit beside them and are a different thing
    entirely: they are names a repo can hold. They were read's mode tokens,
    which live at ``parts[2]``/``parts[3]`` — no ``_PATH_ARG_POSITIONS`` slot
    is ever one, so the skip never had a case to serve even on dispatch. What
    it did have was an effect: #884 moved it from the dispatch loop into this
    helper, and the ``@payload`` route, whose ``paths`` entries are always
    filenames, inherited it — a symlink named ``raw`` pointing outside the root
    then validated where an ordinary name was refused (#889). Parity held and
    was not the property wanted; the routes agreed on the wrong rule. Deleted
    rather than moved back or made a parameter: a skip that guards nothing and
    opens something is only the hole.

    Accepted risk, documented rather than closed (#896 F3): this realpaths
    each candidate ONCE, here, and the op that runs afterwards re-resolves
    the same string independently -- `open()`, `os.path.realpath()` inside
    the op body, whatever that op already does. A symlink swapped between
    this check's resolution and the op's own is the classic TOCTOU window:
    the containment gate validates one target, the op reads another. This is
    not a defect #892 (or any single PR) introduced -- it is the shape of
    "check, then act" wherever the check and the act are two separate
    syscalls, which is every route through this gate. Closing it for real
    means opening the file through an `O_NOFOLLOW`-style handle at check
    time and using THAT handle for the op, rather than a path string handed
    off to be resolved a second time -- a change to every op's own open
    path, not to this function, and out of proportion to a low-severity,
    narrow-window race that needs a second local account (or process)
    racing the exact candidate path between two syscalls. Named here so the
    gap is on the record rather than pretended closed.
    """
    for candidate in candidates:
        if not candidate or candidate == ".":
            continue
        try:
            _safe_path(candidate, root=root, boundary=boundary)
        except SecurityError as exc:
            return f"ERROR: {exc}\n"
    return None


def _expand_home(p: str) -> str:
    """`~` / `~user` expansion, for a value the gate is about to clear.

    Not a new policy. `_safe_path` has expanded `~` since #146 — on the
    string it *checks*. What it never did was hand the expansion back, so
    every op re-used the caller's literal `~/x`, which `os.path.abspath`
    turns into `<cwd>/~/x` and which never exists (#1300). The gate approved
    one path and the op opened another.

    Deliberately narrower than `_safe_path`'s own normalisation:

    * **no `realpath`** — that resolves symlinks, and handing an op the
      symlink target would change what a write lands on and what every
      receipt prints, which is not what was filed;
    * **no `expandvars`** — a `$` is a legal character in a filename, so
      expanding one would break a file that reads fine today. `_safe_path`
      no longer expands `$VAR` either (#1370), so there is nothing here to
      hand back for it — the gate now checks the same literal string every
      op opens; `~` is the only shape that cannot name a real relative path
      and so is the only one this function still has to translate.

    Whatever the platform's `expanduser` makes of `~user` is what the op
    gets, and the two cannot disagree — POSIX leaves an unknown user exactly
    as typed, while `ntpath.expanduser` invents a home for any name by
    joining it onto the parent of `USERPROFILE` without checking it exists.
    Neither is normalised away here: the point of this function is that the
    string returned is the string opened, not that the two platforms answer
    the same.
    """
    if not isinstance(p, str) or not p.startswith("~"):
        return p
    return os.path.expanduser(p)


def _gate_paths(candidates: Iterable[str], *,
                root: Optional[str] = None,
                boundary: str = "cwd") -> Tuple[Optional[str], List[str]]:
    """`_containment_error`, plus the strings the caller must actually use.

    One return value, so a caller cannot check one string and open another —
    which is the whole of #1300. The expansion is applied *after* the check
    and is the same one the check itself performs, so this widens nothing:
    a `~` resolving outside the boundary is refused before the expanded
    value is ever produced for use. `test_a_tilde_path_outside_the_boundary_is_still_refused`
    pins that ordering.

    On refusal the caller gets the error and must not use the list; it is
    returned unconditionally only so the signature has one shape.
    """
    originals = list(candidates)
    err = _containment_error(originals, root=root, boundary=boundary)
    return err, [_expand_home(c) for c in originals]


#: Stands in for a whole magic path component while a glob pattern is gated.
#: An ordinary name with no separator and no metacharacter in it, so it moves
#: the cursor exactly one level down and nothing else.
_GLOB_MAGIC_STANDIN = "__supertool_glob_magic__"


def _glob_split(pattern: str) -> List[str]:
    """Path components, on either separator."""
    seps = "/" + (os.sep if os.sep != "/" else "")
    return re.split("[" + re.escape(seps) + "]", pattern)


def _glob_reach(pattern: str) -> str:
    """The deepest path a glob pattern can address, as an ordinary path.

    **No glob metacharacter can invent a separator.** `*`, `?` and `[...]`
    match within one component; `**` spans components but only ever downward.
    So a pattern's traversal is fully determined by the `/` separators and the
    `..` components already written in it, and replacing every magic component
    with a plain name loses nothing the boundary cares about.

    That is what makes gating the *pattern* sound where the two obvious
    answers are not (#1366). The literal prefix before the first magic
    character is empty in `*/../../etc/*`, so a prefix gate clears it. And
    filtering the expanded results turns a refusal into a shorter list, which
    is this repo's house defect: a population narrowed without saying so.

    **This is one of two reaches, not the reach** (#1392). Every magic
    component becomes exactly one level down, which is what `*` and `?` do and
    is the *shallowest* thing `**` does — see `_glob_reach_min` for the other
    end, which is the one an escape uses.
    """
    return "/".join(
        _GLOB_MAGIC_STANDIN if WILDCARD_CHARS.search(comp) else comp
        for comp in _glob_split(pattern)
    )


def _glob_reach_min(pattern: str) -> str:
    """The same pattern with every `**` expanded to **zero** components (#1392).

    `glob` expands `**` to zero-or-more levels; `_glob_reach` modelled it as
    exactly one. A `..` written after a `**` therefore climbed one level
    further than the gate believed, and a pattern carrying one `**` per `..`
    cleared the gate and then walked out — `**/../secrets/*` reads as
    `<one level down>/../secrets` (contained) and expands to `../secrets`.

    Zero is the extreme worth checking and the only one that needs checking:
    dropping a downward component can only move the cursor up, never down, so
    the all-zero expansion climbs at least as far as any other. Checking it
    alongside `_glob_reach` covers both ends of the range, and every expansion
    in between lands within them.
    """
    kept = [_GLOB_MAGIC_STANDIN if WILDCARD_CHARS.search(comp) else comp
            for comp in _glob_split(pattern) if comp != "**"]
    return "/".join(kept) or "."


def _glob_pattern_containment_error(pattern: str) -> Optional[str]:
    """`_containment_error` for a glob pattern, per brace branch.

    Braces are expanded here because `_expand_braces` runs *inside*
    `_glob_files`, i.e. after any gate on the pattern string: `{.,/etc}/host*`
    reads as one contained component and fans out into two patterns, only one
    of which the gate ever saw (#1366).

    The refusal is `_containment_error`'s own — same wording, same opt-out
    sentence, one implementation (#882, #889) — with the two echoed strings
    rewritten to the caller's spelling, so the message names a pattern rather
    than the stand-in this function substituted. Two things it does not
    reproduce exactly, both in the trailing `(resolved to …)` hint only — the
    pattern itself is echoed verbatim ahead of it: with braces the message
    names the offending *branch* rather than the whole typed string (the more
    useful of the two, since it says which alternative crossed the line), and
    every magic component renders as `*` whatever it was written as, because
    the stand-in has already replaced `?`, `[abc]` and `**` alike.
    """
    for sub in _expand_braces(pattern):
        for reach in (_glob_reach(sub), _glob_reach_min(sub)):
            err = _containment_error([reach])
            if err:
                return (err.replace(repr(reach), repr(sub))
                           .replace(_GLOB_MAGIC_STANDIN, "*"))
    return None


def _glob_results_escape(files: Iterable[str]) -> bool:
    """Did any matched file resolve outside the boundary?

    The one escape `_glob_reach` cannot predict: a wildcard component landing
    on a symlink that points out of the tree. The pattern is honest and the
    result is not, and only `realpath` on the match can tell.

    Returns a bare bool on purpose. The caller must refuse the **whole call**
    and must not name the offending file: dropping it would be the narrowed
    list this gate exists to avoid, and printing it would disclose the very
    path outside the boundary that the refusal is about.
    """
    for f in files:
        try:
            _safe_path(f)
        except SecurityError:
            return True
    return False


#: Registry keys whose value is a boundary this core knows how to enforce.
#: `cwd` is the core's own and the default everywhere else; `repo` is the
#: repository root, which is what `claims` resolves relative arguments against.
_PATH_BOUNDARIES = ("cwd", "repo")

#: Syntax-string components that mean "this argument is a filesystem path".
#: Matched per `_`-separated component so `MD_FILE`, `TEXT_OR_FILE_OR_file`
#: and `@FILE` all count, while `NUMBER_OR_BRANCH` and `PATTERN` do not.
_PATH_SYNTAX_COMPONENTS = frozenset(("PATH", "PATHS", "FILE", "FILES"))

_SYNTAX_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")

#: The core's own path placeholders in a `cmd` template. `{file}` is
#: `parts[1]` — the FIRST token only, and since #873 a caller token neither it
#: nor `{arg}`/`{dir}` can reach is refused rather than dropped.
#: `{dir}` is its `os.path.dirname`. Both are substituted by
#: `_resolve_custom_op` below, so an op writing either has already told the
#: core which argument it means a filesystem path to be — a stronger signal
#: than a prose `syntax` string, and the one #1350 was filed about.
#:
#: `{arg}` is deliberately absent even though it substitutes the very same
#: `parts[1]`. Twenty shipped ops carry `{arg}` (24 until #873 moved three
#: multi-token ops to `{args}`, then `gh-run` for #1715); 8 of them name a path
#: in `syntax` and are already held by `_syntax_names_a_path`, leaving 12 that
#: pass a handle, a ref, a tag, an ID or a repo slug and take no path at all.
#: Promoting `{arg}` would refuse those 12 and gate nothing. `{file}` and
#: `{dir}` are the placeholders whose NAME is the claim; `{arg}` is the one
#: that declines to make it. An op that means a path and writes `{arg}` is
#: still ungated, and that is a naming problem in the op rather than a hole
#: the core can close without over-refusing.
#:
#: #1357 proposed a *lint* instead — flag `{arg}` beside a PATH-shaped
#: `syntax` — and it is not built, because the measurement says it reaches
#: nothing: all 8 such ops are already refused by the detector unless they
#: declare, or named in `_UNDECLARED_PATH_OPS`. It is also aimed at the wrong
#: half. The residue is an op that MEANS a path behind a `syntax` that does
#: not say so, and such an op is invisible to a lint keyed on the `syntax`
#: saying so. Pinned in `tests/test_arg_placeholder_and_paths_env_1357.py`.
_PATH_CMD_PLACEHOLDERS = ("{file}", "{dir}")

#: Placeholders that consume EVERY caller token — `parts[1:]`, all of it.
_ALL_ARGS_PLACEHOLDERS = ("{args}", "{argjoin}")

#: Placeholders that consume exactly `parts[1]` and nothing after it. `{dir}`
#: is its `os.path.dirname`, which is still that one token.
_ONE_ARG_PLACEHOLDERS = ("{file}", "{dir}", "{arg}")


def _unconsumed_arg_tokens(cmd_template: str, parts: List[str]) -> List[str]:
    """Caller tokens the `cmd` template has no placeholder to carry (#873).

    The op string is split on every ':' before it gets here, so `parts` holds
    everything the caller typed. What reaches the subprocess is whatever the
    template asks for, and a template writing only `{file}`/`{dir}`/`{arg}`
    asks for `parts[1]` — `op:all:dry` used to run as `argv == ["all"]` with
    no warning, no error and no mention in the receipt. `:dry` was a safety
    flag in the filed case: the op pushed for real while its caller read the
    receipt as a dry run.

    Returns [] when nothing is lost, which includes the case where every
    unconsumed token is empty — `op:all:` carries no text to lose, and
    refusing a bare trailing separator would be noise rather than disclosure.

    **A template with NO argument placeholder at all** (#1532, split out of
    #873 deliberately, priced and declined until now) is held to the same
    rule: nothing in `parts[1:]` can ever reach the subprocess, so every one
    of those tokens is unconsumed, not just `parts[2:]`. `op:x` against
    `"cmd": "make lint"` used to drop `x` exactly as silently as the
    single-placeholder case #873 already refuses. Measured population is 4
    shipped ops (`git-conflicts`, `mcp_status`, `mcp_stop_all`, `watches`,
    pinned in `tests/test_custom_op_dropped_tokens_873.py`) plus whatever a
    project's own `.supertool.json` defines — a much smaller net than #873
    feared, because the pinned population is exactly 4, not an unbounded
    fraction of the registry.
    """
    if any(p in cmd_template for p in _ALL_ARGS_PLACEHOLDERS):
        return []
    if any(p in cmd_template for p in _ONE_ARG_PLACEHOLDERS):
        extra = list(parts[2:])
        return extra if any(extra) else []
    extra = list(parts[1:])
    return extra if any(extra) else []


def _declared_path_slots(entry: Any) -> List[int]:
    """The argument positions this registry entry declares as paths (#1560).

    Read-only, and deliberately tolerant: a malformed `"paths"` yields `[]`
    here rather than a second copy of the refusal, because
    `_preset_path_containment` runs first at dispatch and an op whose
    declaration does not parse never reaches a message that quotes it. What
    this answers is the one question a refusal needs — "is there a containment
    claim, and over which positions" — so the message cannot describe a
    boundary the gate does not enforce.
    """
    if not isinstance(entry, dict):
        return []
    decl = entry.get("paths")
    if not isinstance(decl, dict) or not isinstance(decl.get("args"), list):
        return []
    return [i for i in decl["args"]
            if isinstance(i, int) and not isinstance(i, bool) and i >= 0]


def _dropped_tokens_refusal(
        op: str, entry: Any, cmd_template: str, dropped: List[str]) -> str:
    """Name the text that will not be passed, and what would pass it.

    Refused rather than passed through: widening a single-token placeholder to
    the whole remainder would interpolate `parts[2:]` into `{file}`, a slot the
    `"paths"` declaration gates at index 1 only — a second path arriving
    downstream of the containment check, which is #1135's shape. It would also
    change what every existing `{arg}` op receives. `{args}` is already the
    pass-through, so the fix is to say which one the template asked for.

    **And for an op that declares `"paths"`, that pass-through is not offered**
    (#1560). The paragraph above is the reason this function will not widen the
    placeholder internally, and the message used to hand the operator the same
    manoeuvre in one line, with no word about extending the declaration
    alongside it. Measured on a fixture op declaring
    `{"args": [1], "root": "cwd"}`: `showit:../outside.txt` is refused at
    position 1, and after obeying the advice `showit:a.txt:../outside.txt`
    printed the file. So the remedy performed exactly what the refusal declined
    to — the `misdirects` shape, and the reader most likely to obey a refusal
    verbatim is an agent, immediately.

    What replaces it is the third state rather than a reworded one-liner: the
    declaration would have to name every position the widened template can
    receive, and `args` is a fixed index list while `{args}` takes an unbounded
    tail, so it contains that tail only up to the highest index it names. There
    is no one-line `cmd` change that keeps such an op contained, and saying so
    is more use than a remedy that reads as freshly checked. Pinned in
    `tests/test_payload_key_and_misdirect_refusals_1551_1554_1560.py`.
    """
    sep = _ARG_SEP[0] or ":"
    used = [p for p in _ONE_ARG_PLACEHOLDERS if p in cmd_template]
    reach = (f"substitutes {', '.join(used)}, which is the FIRST argument token "
             f"and nothing after it"
             if used else
             "substitutes no argument placeholder, so no argument token reaches it")
    syntax = entry.get("syntax") if isinstance(entry, dict) else None
    lines = [
        f"ERROR: op {op!r} was given {len(dropped)} argument token(s) its cmd "
        f"cannot reach: {sep.join(dropped)!r}.",
        f"       The template {reach}.",
        "       Refused rather than dropped (#873): a discarded ':dry' once ran "
        "an op in live mode",
        "       while its receipt read as a dry run.",
    ]
    gated = _declared_path_slots(entry)
    if gated:
        positions = ", ".join(str(i) for i in gated)
        lines += [
            "       Widening the cmd to {args} or {argjoin} is NOT the remedy "
            "here: this op declares",
            f'       "paths": {{"args": [{positions}]}}, so only argument '
            f"position(s) {positions} are containment-checked,",
            "       and a template that consumes the whole tail hands every "
            "later position to the child",
            "       unchecked (#1135, #1560). Extending that `args` list "
            "alongside it is necessary and not",
            "       sufficient — it is a fixed index list, so it holds an "
            "unbounded {args} tail only up to",
            "       the highest index it names. There is no one-line cmd "
            "change that keeps this op contained.",
            "       What IS checked is the first token: carry several fields "
            "inside it, separated by ',' or '|'.",
        ]
    else:
        lines += [
            "       To take every token, write {args} (one shell-quoted argv "
            "word per token) or",
            "       {argjoin} (every token rejoined with ':::' as a single "
            "argument) in the cmd.",
            "       To carry several fields inside the first token, separate "
            "them with ',' or '|'.",
        ]
    if isinstance(syntax, str) and syntax:
        lines.append(f"       This op documents: {syntax}")
    return chr(10).join(lines) + chr(10)

#: Preset ops that name a path and predate the declaration (#1287). **This set
#: only ever shrinks.** It is not a policy — it is a debt register: 33 shipped
#: PRESET ops name a path, 14 declare a boundary (#227 added `youtube_comment`,
#: the same `{"args": []}` shape as the ops below it -- its `file://PATH` sits
#: inside a pipe-separated field, not in an argument slot, so the containment
#: that matters is `safe_resolve_body_path`. `bluesky_publish` is the identical
#: shape sitting in the register, which is the difference a grandfather clause
#: makes and not a difference in the ops; #1796 added `gh-job` and
#: `gl-job` to the declared side, both `"paths": {"args": []}` — the `gl-api`
#: precedent, since PATH there names an artifact's own path or a GitLab API
#: route rather than anything on this filesystem; #532 added `worktree`, the
#: same `"paths": {"args": []}` shape again, since PATH there deliberately
#: points outside cwd; #2593 added `youtube_reply`, the identical
#: `{"args": []}` shape as `youtube_comment` right above it -- its own
#: `file://PATH` sits inside the same pipe-separated field, so named and
#: declared each moved by one again), these 19 do not. It opened
#: at
#: 20 — see the #1351 note below for the one it has lost. Counting this repo's
#: own `.supertool.json` as well used to add one to each of those first two
#: numbers, the extra being `oss_train`, deleted in #1472; the two scopes now
#: agree at 31 and 12. All
#: four numbers are pinned in
#: `tests/test_cmd_placeholder_path_detector_1350.py` so this comment cannot
#: drift again.
#: Refusing them at the time would have broken every one of them in the same
#: release that introduced the rule, so they are grandfathered *by name*,
#: which means a
#: newly written op cannot inherit the old default by being written after it —
#: it is refused at dispatch and red in
#: `tests/test_preset_path_chokepoint_1287.py` until it declares.
#:
#: It has shrunk once, to 19: `gl-api` now declares `{"args": []}` for real
#: (#1351). It was the op `_syntax_names_a_path`'s docstring held up as the
#: worked example of the declared pattern while sitting in this register — the
#: guard citing a member of the set it exists to empty.
#:
#: Several of these cannot be expressed as a `parts` index at all: the publish
#: ops carry their file inside a `|`-separated blob, `git-commit` takes a
#: `:::`-separated tail, `git-resolve` a comma list. Draining the register is
#: therefore per-op work, not a sweep, and the ops living in files with open
#: PRs (`presets/git*`, `presets/github*`, `presets/gitlab*`) were left alone
#: here on purpose rather than overlooked.
_UNDECLARED_PATH_OPS = frozenset((
    "bluesky_publish",
    "devto_comment",
    "devto_publish",
    "gh-batch-follow",
    "gh-batch-star",
    "gh-issue-create",
    "gh-pr",
    "gh-pr-create",
    "git-blame",
    "git-commit",
    "git-diff",
    "git-investigate",
    "git-resolve",
    "git-trail",
    "git-worktrees",
    "gl-issue-create",
    "hashnode_comment",
    "hashnode_publish",
    "hashnode_reply",
))


def _syntax_names_a_path(syntax: str) -> bool:
    """Does this op's registry syntax string name a filesystem path argument?

    The detector, not the declaration. The registry's `syntax` is already
    parsed rather than merely displayed, and it is the one field every op
    fills in, so it is what tells the core that an op *should* have declared a
    boundary. It cannot say *where* the path is — `TITLE|MD_FILE|CANONICAL`
    hides one inside a pipe-separated blob — which is exactly why the position
    is declared explicitly in `paths` and this function only ever produces the
    question, never the answer.

    Over-detection is the safe direction and it happens: `gl-api:PATH` is an
    API route, not a file. That op answers with `"paths": {"args": []}` — a
    declaration that no argument here is a filesystem path, which is a claim
    someone made rather than a default nobody noticed. It only became true in
    #1351: for one release `gl-api` was cited here as the declared example
    while sitting in `_UNDECLARED_PATH_OPS`, so the sentence teaching the next
    author what "declared" looks like pointed at an ungated op.

    It is also not the only detector. `_cmd_names_a_path` reads the `cmd`
    template for the core's `{file}` / `{dir}` placeholders, and
    `_entry_names_a_path` is the OR of the two — see #1350. The two are not
    ranked: of the 24 shipped ops this function detects, zero carry either
    placeholder, so a `cmd`-supersedes-`syntax` detector would have disarmed
    the gate for every one of them.
    """
    if not isinstance(syntax, str):
        return False
    for token in _SYNTAX_TOKEN_RE.split(syntax):
        for component in token.split("_"):
            if component in _PATH_SYNTAX_COMPONENTS:
                return True
    return False


def _cmd_names_a_path(cmd: Any) -> Optional[str]:
    """Which core path placeholder this `cmd` template substitutes, if any.

    Returns the placeholder rather than a bool so the refusal can name the
    signal that actually fired. An op with no `syntax` key was told it "names
    a path in its syntax ()", which reads as a bug in the guard rather than a
    demand on the op.
    """
    if not isinstance(cmd, str):
        return None
    for placeholder in _PATH_CMD_PLACEHOLDERS:
        if placeholder in cmd:
            return placeholder
    return None


def _entry_names_a_path(entry: Any) -> Optional[str]:
    """Why this op is being asked to declare a boundary, or `None` (#1350).

    The whole detector, in one place, returning the phrase the refusal prints.
    Two signals, OR'd, neither superseding the other:

    * the `syntax` string names a `PATH`/`FILE` component — 27 shipped ops;
    * the `cmd` template substitutes `{file}` or `{dir}` — **no shipped op
      today.** `oss_train` was the one and #1472 deleted it, so this arm's
      live instance is a fixture: a real `.supertool.json` driven through
      `dispatch()` in `tests/test_cmd_placeholder_path_detector_1350.py`.
      Zero shipped instances is not zero coverage, and it must not be read as
      permission to drop the arm — the arm is what asks an op with no
      `syntax` at all to declare, which is where the hole was.

    Before this, only the first was read, so an op with no `syntax` key at all
    took the `return None` arm: no declaration demanded, no check run, and a
    verdict indistinguishable from declared-clean. That is #1287's own rule
    answering "no path here" where it meant "I could not tell" — the two-state
    shape the rule exists to refuse, inside the rule.

    A bare-string op entry (`{"ops": {"lint": "php -l {file}"}}`) never reaches
    here: `_preset_path_containment` returns early on a non-dict. A string has
    no `paths` key and so no way to answer the demand, and the answer is to
    write the op as an object — `docs/contributing.md` has the reasoning.
    """
    if not isinstance(entry, dict):
        return None
    syntax = entry.get("syntax", "")
    if _syntax_names_a_path(syntax):
        return f"names a path in its syntax ({syntax})"
    placeholder = _cmd_names_a_path(entry.get("cmd", ""))
    if placeholder is not None:
        return (f"substitutes the core's {placeholder} path placeholder in "
                f"its cmd")
    return None


def _path_boundary_label(boundary: str) -> str:
    """How a boundary is named in a refusal.

    A function rather than a module-level dict: #397's register in
    `tests/test_state_reset_and_lint_timeout.py` accounts for every mutable
    global in this file, and a constant lookup table is still a mutable global.
    Two strings rather than one word because "path escapes cwd" is already in a
    dozen tests and dozens of transcripts, and the repo one has to read as a
    sentence.
    """
    return "the repository root" if boundary == "repo" else "cwd"


def _repo_root_for_containment() -> str:
    """Nearest ancestor of cwd holding a `.git`, else cwd.

    `.git` is tested with `exists`, not `isdir`: in a linked worktree it is a
    *file* pointing at the common dir, and this repo's own agents work almost
    exclusively in worktrees.

    No subprocess. `git rev-parse --show-toplevel` is what `presets/claims`
    uses and the two agree everywhere it matters; spawning git on every gated
    call to learn something a directory walk already knows would put a process
    launch in front of every preset op that takes a path. Where they could
    disagree — `GIT_WORK_TREE` pointing elsewhere — the core has already
    scrubbed those variables out of the environment (#692, #714).

    Falling back to cwd rather than to `/` is deliberate: a narrower boundary
    refuses more, and the failure mode of guessing wide is the bug this whole
    chokepoint exists to close.
    """
    d = os.path.realpath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.realpath(os.getcwd())
        d = parent


def _undeclared_path_refusal(op: str, signal: str) -> str:
    """What an op that takes a path and declares no boundary gets (#1287).

    A refusal, not a `skipped`. The three-state rule this repo applies
    everywhere else — `ok`, a finding, `skipped` — has no third state here,
    because a path argument that reaches no check is not a check that could
    not run. It is an unchecked read, and it renders identically to a checked
    one.
    """
    return (
        f"ERROR: op {op!r} {signal} and declares "
        f"no containment boundary.\n"
        f'       Add "paths": {{"args": [1], "root": "cwd"}} to its registry '
        f"entry — \"args\" lists the\n"
        f"       argument positions that are filesystem paths, \"root\" is "
        f'"cwd" (the core\'s boundary)\n'
        f'       or "repo" (the repository root, for an op that resolves '
        f'relative paths against it).\n'
        f'       "args": [] declares that no argument here is a filesystem '
        f"path.\n"
    )


def _preset_path_containment(
        op: str, entry: Any, parts: List[str]) -> Optional[str]:
    """The universal half of #1287: one chokepoint, a declared boundary.

    Every preset and custom op reaches `_resolve_custom_op`, so that is where
    a path argument can be gated once instead of per-op. What the core cannot
    supply is the *boundary*: `_PATH_ARG_POSITIONS` imposes the cwd on every
    builtin, and imposing it on presets too would refuse `claims:docs/x.md`
    run from `docs/` — a call that works today and should, because `claims`
    resolves relative arguments against the repository root.

    So the op declares both, and the core enforces both:

        "paths": {"args": [1], "root": "repo"}

    Returns the `ERROR: …` line to print, or `None` to let the op run.

    **Mutates `parts` in place** on the declared path, writing back the gate's
    own `~` expansion into each declared slot (#1300). In place because the
    caller substitutes `{file}` / `{args}` from that same list immediately
    after, and a preset op handed the literal `~/x` passes it to a child
    process with no shell to expand it — `cat: ~/t.txt: No such file or
    directory`, measured. Only the declared branch does this: an op in
    `_UNDECLARED_PATH_OPS` reaches no check, so there is nothing for an
    expansion to sit behind and it keeps the literal, exactly as `glob` does.
    """
    if not isinstance(entry, dict):
        return None
    decl = entry.get("paths")
    if decl is None:
        if op in _UNDECLARED_PATH_OPS:
            return None
        signal = _entry_names_a_path(entry)
        if signal is None:
            return None
        return _undeclared_path_refusal(op, signal)
    if (not isinstance(decl, dict) or not isinstance(decl.get("args"), list)
            or not all(isinstance(i, int) and not isinstance(i, bool) and i >= 0
                       for i in decl["args"])):
        # Negative indices are refused rather than resolved Python-style. They
        # would read as "the last argument", which is a real shape — `between`
        # symbol mode takes `parts[-1]` — but the core cannot honour it here:
        # `parts[-1]` on a bare `op` call is the op NAME, so the declaration
        # would gate a token that is never a path and skip the one that is.
        # Refused loudly because the alternative found in review was worse:
        # an out-of-range index was silently filtered out of the generator
        # below, so a typo'd declaration ran the op completely unchecked while
        # looking exactly like a declared one.
        return (
            f'ERROR: op {op!r} has a malformed "paths" declaration — expected '
            f'{{"args": [<non-negative int>, ...], "root": "cwd"|"repo"}}.\n'
        )
    boundary = decl.get("root", "cwd")
    if boundary not in _PATH_BOUNDARIES:
        return (
            f'ERROR: op {op!r} declares an unknown path root {boundary!r} — '
            f'expected one of: {", ".join(_PATH_BOUNDARIES)}.\n'
        )
    slots = [i for i in decl["args"] if 0 <= i < len(parts)]
    err, gated = _gate_paths(
        (parts[i] for i in slots),
        root=_repo_root_for_containment() if boundary == "repo" else None,
        boundary=_path_boundary_label(boundary),
    )
    if err:
        return err
    for _i, _slot in enumerate(slots):
        parts[_slot] = gated[_i]
    return None


def _path_not_found(path: str, *, label: str = "path",
                     suggest: Optional[str] = None,
                     op: Optional[str] = None,
                     call_prefix: Optional[str] = None,
                     creates: bool = False) -> str:
    """The "not found" error, naming the path it actually tried (#624).

    `ERROR: file not found: src/foo.py` is true and useless: it cannot tell a
    typo from a cwd that drifted — a `cd` in an earlier shell call, which is
    the shape #624 was filed about. The absolute path tried separates the two
    at a glance, with no second `pwd` round-trip.

    When cwd sits under a project root that DOES hold the path, the root and
    the exact `cwd:` prefix that would reach it are named as well. Named, not
    used: auto-recovery (#363) already re-roots the unambiguous call, so every
    call that reaches here is one it declined — ambiguous by construction.
    Resolving it here would trade a loud wrong path for a quiet wrong root,
    which is the defect this tracker is about, not the fix.

    `suggest`, when given, replaces the generic `wrong CWD?` hint (#734).
    The cwd-drift explanation is a real default, but a caller that already
    knows a *specific*, more plausible mistake — e.g. `around` recognising
    its PATH argument as an all-digit token that looks like the LINE its
    sibling `around_line` wants — should say that instead of pointing at the
    one thing that provably did not cause this failure.

    `creates`, when set by a *write* op, appends the clause naming the ops that
    would have made the file (#1334, #1372). It renders on one arm only: the
    generic `wrong CWD?` fallback, where nothing more specific was identified.
    Where the project-root branch or a `suggest` fired, the cause is already
    known and is not "the file was never there" — offering `paste` beside a
    positively diagnosed cwd drift would send the caller to write a second copy
    under the wrong directory. Read ops never pass it: nothing was to be
    written, so there is no next step to name.

    `op` asks this helper to derive that suggestion itself, from the two
    shapes known to produce a joined-up filename: a comma list (#921) and a
    whitespace-separated path list (#1261). Derived here rather than at each
    call site because six ops reach this function and only `grep` had ever
    been wired to the first of the two — `read`, `around`, `around_line`,
    `map` and `replace`, the other five, answered both with `wrong CWD?`,
    the one cause that provably did not apply. `call_prefix` is the op call
    up to but excluding the path, used to print the batched repair; omit it
    where the path is not the op's last argument, since the printed call
    would then be one nobody can run.
    """
    if not path:
        return f"ERROR: {label} not found: {path}\n"
    if not suggest and op:
        suggest = (_comma_path_list_suggest(op, path)
                   or _multi_path_suggest(op, path, call_prefix)
                   or None)
    # `os.path.abspath` and nothing else. This line used to run its own
    # `expanduser`, so a `~/x` that the op had stat-ed literally was reported
    # as the expanded path — one that existed, and that the reader had just
    # listed. The receipt named a path the tool never touched (#1300). Ops
    # now receive the expansion from the containment gate, so the two agree;
    # where they cannot (an unknown `~user`), the literal is what is shown
    # because the literal is what was opened.
    tried = os.path.abspath(path)
    # Every path this helper *renders* is flattened; every path it *uses* is
    # not (#1019). The four lines below are the tool's own, at column 0, and a
    # filename is whatever the filesystem accepted — `str.splitlines()` breaks
    # on ten separators (#886), so `a<U+2028>[result] …` wrote a forged marker
    # line into the middle of a refusal. The stat, the abspath and the join
    # above still run on the real name, so the receipt keeps naming the path
    # that was actually opened (#1300).
    shown, shown_tried = (
        _flat_field(path, disclose_newline=True),
        _flat_field(tried, disclose_newline=True),
    )
    shown_cwd = _flat_field(os.getcwd(), disclose_newline=True)
    if not suggest and path.startswith("~") and os.path.expanduser(path) == path:
        # `cwd:` provably cannot fix this one: no working directory makes a
        # `~user` resolve. Same reasoning as #734 and #921 — a hint pointing
        # at the one cause that did not apply costs a round-trip and a wrong
        # conclusion.
        suggest = ("`~` was not expanded: no such user. Pass an absolute "
                   "path, or `~/` for your own home.")
    # Exists, wrong kind. Three states, not two: `edit:::a:::b:::docs` used to
    # answer `file not found: docs` about a directory sitting right there — an
    # absence this helper produced, read as an absence in the world — and every
    # line after it (cwd drift, and since #1334 the create clause) was advice
    # for a path that is not the problem. Found writing #1334's create clause,
    # which would otherwise have offered to `paste` a whole file over a
    # directory.
    if os.path.isdir(path):
        return (
            # `{path} is a directory` rather than #630's `{field} is a
            # directory, not a file: {path}`. That shape reads well there
            # because the label is a payload FIELD name (`body_file`); here it
            # is the generic noun "file", and "file is a directory, not a file"
            # is not a sentence.
            f"ERROR: {shown} is a directory, not a file\n"
            f"  tried: {shown_tried} (cwd: {shown_cwd})\n"
            f"  '{op or label}' takes a single file. `ls:{shown}` or "
            f"`tree:{shown}` lists what is in it.\n"
        )
    lines = [
        f"ERROR: {label} not found: {shown}",
        f"  tried: {shown_tried} (cwd: {shown_cwd})",
    ]
    root = None
    if not os.path.isabs(path):
        try:
            root = _project_root_above_cwd()
        except OSError:
            root = None
    if root and os.path.exists(os.path.join(root, path)):
        lines.append(
            f"  exists at "
            f"{_flat_field(os.path.join(root, path), disclose_newline=True)}"
            f" — prefix the call with "
            f"'cwd:{_flat_field(root, disclose_newline=True)}' to run it "
            f"from the project root"
        )
    elif suggest:
        lines.append(f"  {suggest}")
    else:
        lines.append(
            "  wrong CWD? Prefix the call with cwd:PATH to run it from "
            "elsewhere."
        )
        if creates:
            lines.append(f"  {_create_instead_hint()}")
    return "\n".join(lines) + "\n"


def _extract_env_prefix(cmd: str) -> Tuple[Dict[str, str], str]:
    """Split a leading `KEY=VAL KEY2=VAL2 ...` shell env-prefix off `cmd`.

    Returns ({KEY:VAL,...}, remaining_cmd_without_prefix). Mirrors POSIX shell
    semantics: a `KEY=VAL` token before the command sets KEY in the child's
    env. Stops at the first non-assignment token. Tokens are parsed via
    shlex.split, so quoted values (`KEY='one two'`) work.

    Needed because the argv-form fix (#145) broke shipped cmd templates that
    set env this way — `subprocess.run(shlex.split(cmd), shell=False)` treats
    the assignment as a literal argv[0], yielding ENOENT.
    """
    env: Dict[str, str] = {}
    tokens = shlex.split(cmd, posix=True)
    idx = 0
    # `\Z` with DOTALL, not `$`. Two effects, and the second is the wider one.
    # `$` matches before a final newline, so `FOO='bar<LF>' cmd` set FOO to
    # "bar" and dropped the newline the caller quoted on purpose — the
    # assignment still matched, so nothing said so. And without DOTALL a value
    # with a newline *inside* it matched nothing at all, so the whole token
    # fell through as argv[0] and the env was never set; POSIX sets it. Both
    # are the same character deciding where a value ends (#1188).
    # anchored-ok: DOTALL is the point. `(.*)` is meant to reach the true end
    # and keep a newline the caller quoted, so the `\Z` is not the #1241 no-op
    # it reads as -- it states the intent the flag already carries.
    _kv = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
    while idx < len(tokens):
        m = _kv.match(tokens[idx])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        idx += 1
    if not env:
        return {}, cmd
    # Rebuild remaining cmd as shell-safe string so callers can keep using
    # shlex.split on it (the placeholder-substituted file path already
    # passed through shlex.quote upstream, so it survives a second pass).
    remaining = " ".join(shlex.quote(t) for t in tokens[idx:])
    return env, remaining




def _in_template_single_quotes(s: str, pos: int) -> bool:
    r"""Whether `pos` in shell-syntax string `s` sits inside a single-quoted
    span the string ITSELF opens -- the state `_expand_env` needs at each
    `$VAR` match to know whether splicing in `shlex.quote()`'d value would
    nest a second quote pair inside the template author's own (#2291).

    Deliberately narrower than a full POSIX tokenizer, in the same spirit
    as the docstring above it names as the larger alternative: it tracks
    single-quote and double-quote spans and backslash escapes OUTSIDE
    quotes accurately, but inside a double-quoted span it treats every
    backslash as consuming the next character, where POSIX only grants
    that to `\\`, `\"`, `` \` `` and `\$` -- a backslash immediately
    followed by an unescaped `"` in some OTHER escape context could
    therefore misjudge the state. No template shipped in this tree, or
    named in any issue, has that shape; a project-authored one that does
    is the same "differently-shaped value" caveat #2291's own docstring
    already carries for the untracked case, one level further in rather
    than a new gap this introduces.
    """
    in_single = False
    in_double = False
    i = 0
    while i < pos:
        c = s[i]
        if in_single:
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_double = False
        else:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
        i += 1
    return in_single


def _expand_env(s: str, env: Dict[str, str]) -> str:
    r"""Safe $VAR / ${VAR} expansion from env (no shell).

    Replaces $NAME and ${NAME} with values from env. Unknown vars are left
    literal (vs shell which silently empties them). Used at all argv-form
    dispatch sites (custom ops, validators, formatters, resolve) so users
    can keep cmd templates that rely on env-var expansion without invoking
    a shell.

    **Only ever hand this a SHIELDED string** — the output of
    `_shield_substitute`, never `_substitute_placeholders`. Its subject is the
    template a project wrote, not the argument a caller passed, and it has no
    way to tell them apart once they are one string. Passing it an assembled
    command is #1734: `git-commit:::chore: about $HOME:::f.txt` committed a
    real home directory, and a `$FAKE_TOKEN` in a subject put the token in the
    commit object. A sixth call site that skips the shield reintroduces that
    silently, because the substitution is invisible unless the variable
    happens to be defined on the machine that runs it.

    **Every substituted value is `shlex.quote`d (#2164 CI, four Windows-only
    failures on `tests/test_watch_sources_path_relative_2164.py`).** Every
    call site below builds its command by concatenating this function's
    output onto a template string, then feeds the WHOLE result to
    `shlex.split()` before spawning the child argv-form. A value inserted
    here unquoted is not "no shell" the way the docstring above promised —
    it is untrusted text handed to `shlex.split`'s own POSIX-mode escape
    grammar, where a bare backslash escapes (and drops) the next character.
    An anchored Windows path such as
    ``C:\\\\Users\\\\runneradmin\\\\...\\\\watch-sources``, exported as
    `SUPERTOOL_WATCH_SOURCES_PATH` and referenced as `$SUPERTOOL_WATCH_SOURCES_PATH`
    in a `cmd` template, came back with every single backslash removed —
    `C:UsersrunneradminAppDataLocalTemp...` — not doubled, not converted to
    `/`, consumed outright, because none of the characters that followed each
    backslash formed a real escape pair. `shlex.quote` wraps the value in
    single quotes -- an embedded single quote is closed out and re-opened
    (`'` becomes the four bytes `'"'"'`), never backslash-escaped, but that
    detail does not matter here: what matters is that POSIX `shlex.split`
    treats a backslash inside a single-quoted segment as a literal
    character, not an escape introducer -- matching the treatment
    `{file}`/`{dir}`/`{arg}` already get via `shlex.quote` at every one of
    this function's call sites, and the same reasoning #1734 applied to
    those placeholders one layer up. A defined variable whose value has no
    shell-special characters (the common case — plain words, existing
    shipped `cmd` templates) round-trips through `shlex.quote` unchanged, so
    this narrows nothing that worked before; a caller relying on a `$VAR`
    value being split into SEVERAL argv tokens by `shlex.split` gets it as
    one token instead, which is the same one-token guarantee `{file}` etc.
    already make and is what makes this expansion "no shell" in fact rather
    than only in the docstring.

    **Formerly a known gap, closed by #2291 for the single-quote case:**
    this quoting used to be blind to the template's OWN quoting. A template
    that already wraps a `$VAR` reference in its own single quotes -- the
    shipped example at `docs/notifiers.md`'s `bash -c '...$SLACK_WEBHOOK'`
    pattern -- got a SECOND, nested pair of quotes spliced in when that
    variable's value contained a space or other shell-special character,
    and `shlex.split` has no notion of nested quoting (POSIX shell does not
    either), so the value split back apart at the space, or raised
    `ValueError: No closing quotation` outright for an odd count of special
    characters. `_in_template_single_quotes` below now tracks that state and
    splices the value directly when it is already inside the template's own
    single-quote span, so this case no longer regresses.

    **Still open:** the identical shape inside the template's OWN DOUBLE
    quotes (`bash -c "echo $VAR"`) is untouched -- `_in_template_single_quotes`
    only tracks double-quote spans well enough to know it is not INSIDE a
    single-quote span there, not well enough to splice correctly into one.
    No template shipped in this tree, or named in any issue, uses that
    pattern; a project-authored one that does keeps today's (already
    imperfect, pre-#2291) behaviour. Fixing this needs the substitution to
    track double-quote-specific escaping rules too (POSIX grants a
    backslash real escape power there only before `\`, `"`, `` ` `` and
    `$`), which is a different, larger change than the one this docstring
    documents.
    """
    def _replace(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        if name not in env:
            return m.group(0)
        value = env[name]
        if _in_template_single_quotes(s, m.start()):
            # Already inside a single-quote span the TEMPLATE itself opened
            # (docs/notifiers.md's `bash -c '...$SLACK_WEBHOOK'` pattern) --
            # nothing but a literal `'` is special there, so wrapping the
            # value in ANOTHER quote pair (shlex.quote's job below) would
            # nest quotes `shlex.split` has no notion of (#2291). Splice the
            # value directly; a `'` inside it is broken out with the same
            # close-quote/literal-quote/reopen-quote idiom shlex.quote uses
            # internally, just without the wrapping pair this context
            # already provides.
            return value.replace("'", "'\"'\"'")
        return shlex.quote(value)

    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
        _replace,
        s,
    )


def _shield_substitute(
    template: str, values: Dict[str, str],
) -> Tuple[str, Dict[str, str]]:
    """Substitute placeholders with inert tokens, so `_expand_env` cannot see
    the values (#1734).

    `_expand_env`'s subject is the **cmd template** — a project writing
    `$MY_TOOL_HOME` in its own `cmd` and having it resolved without a shell.
    It used to run over the *assembled* command instead, after `{args}` /
    `{arg}` / `{argjoin}` / `{file}` had been interpolated, so it could not
    tell the template's own text from the caller's data and rewrote both:

        git-commit:::chore: about $HOME:::f.txt
        → committed subject "chore: about /Users/<name>"

    `discloses`, and it survived because it is invisible unless the variable
    happens to be defined — an undefined one round-trips byte-exact, so the
    same message behaves differently on another machine. The commit that hit
    it was about to be pushed.

    Substituting a random per-call token first and restoring it after
    expansion, rather than expanding the template before substitution, is what
    keeps the *rest* of the pipeline byte-identical: `_extract_env_prefix`
    still sees a fully substituted string, so a `KEY={dir} cmd` prefix
    resolves as it always did, and an env value that happens to read `{args}`
    is still never re-substituted. Reordering the two would have changed both.

    Tokens are alphanumerics **bracketed by a dot**, which buys four properties
    the restore depends on. `shlex.quote` leaves `.` and `[A-Za-z0-9]` bare, so
    the requote inside `_extract_env_prefix` round-trips; `_expand_env` cannot
    match a token, having no `$`; `_extract_env_prefix`'s `KEY=VAL` test cannot
    match one, having no `=`; and the leading dot TERMINATES a variable name,
    which the bare-alphanumeric first version did not. `[A-Za-z0-9]` is a legal
    continuation of `[A-Za-z_][A-Za-z0-9_]*`, so `cmd $PREFIX{file}` expanded a
    name that ran into the token, found it undefined, and left `$PREFIX`
    literal — a silent regression, because leaving an unknown literal is the
    documented behaviour and nothing distinguishes the two cases. The nonce is
    per call, so no caller value can collide.
    """
    nonce = "STPH" + os.urandom(8).hex()
    tokens: Dict[str, str] = {}
    shield: Dict[str, str] = {}
    for i, name in enumerate(sorted(values)):
        token = f".{nonce}x{i}x."
        tokens[name] = token
        shield[token] = values[name]
    return _substitute_placeholders(template, tokens), shield


def _unshield(s: str, shield: Dict[str, str]) -> str:
    """Put the real placeholder values back into a COMMAND STRING.

    Called after `_expand_env` at every site. The values went in
    `shlex.quote`d and come back out that way, which is what the command
    string wants — `_unshield_env_value` is the variant for the other
    destination.
    """
    for token, value in shield.items():
        s = s.replace(token, value)
    return s


def _unshield_env_value(s: str, shield: Dict[str, str]) -> str:
    """Put the real values back into an ENV VALUE lifted by `_extract_env_prefix`.

    A command string keeps the `shlex.quote`ing; an environment value must not.
    `_extract_env_prefix` tokenises through `shlex.split`, so before shielding
    it stripped the quotes as it lifted `KEY={dir}` — the child got
    ``MYDIR=my dir``. Restoring the quoted form into the value handed it
    ``MYDIR='my dir'``, apostrophes included, for any value `shlex.quote`
    actually quotes. That is invisible for a value needing no quoting, which
    is why the first `{dir}` test round-tripped by accident (#1734 review).

    Re-tokenising the restored value reproduces the pre-shield bytes exactly,
    since the whole assignment was one shlex token by construction. A value
    that will not tokenise is returned as-is rather than dropped: a mangled
    env var is recoverable, a missing one is a silent behaviour change.
    """
    restored = _unshield(s, shield)
    if restored == s:
        return restored  # held no token; never went through shlex.quote
    try:
        parts = shlex.split(restored)
    except ValueError:
        return restored
    return parts[0] if len(parts) == 1 else restored


# Git's repo pointers. Any of these, set anywhere in the parent environment,
# overrides discovery-from-cwd and points a git command at the repository they
# name instead — so an op run from repoB acts on repoA (#692). Proven: a
# `git-commit` with cwd=repoB and GIT_DIR=repoA/.git wrote the commit into
# repoA, and the receipt named no repository at all.
#
# Not hypothetical, and not something the caller has to do to themselves: git
# exports GIT_DIR to every hook it runs, and `.githooks/pre-commit` invokes
# `./supertool 'git-diff:staged'`. This repo hands supertool a leaked git
# environment as a matter of routine.
#
# #416 learned this once and fixed it for the TEST RUNNER (`.githooks/pre-push`
# unsets them before pytest; `tests/conftest.py` again before every test). The
# ops never got the same treatment. This is the same list, kept in one place —
# conftest imports it from here, and a test pins the hook's `unset` line to it,
# because three copies of one lesson is how the ops came to be missed.
#
# Membership rule: does the variable change WHICH repository, index, or refs a
# git command reads or writes? GIT_COMMON_DIR and GIT_NAMESPACE do and were not
# in #416's five — the first redirects config and refs for a worktree, the
# second redirects every ref a push writes. GIT_CEILING_DIRECTORIES and
# GIT_DISCOVERY_ACROSS_FILESYSTEM do not: they only restrict discovery, so the
# worst they cause is finding no repo rather than the wrong one, and people set
# them deliberately on slow network mounts. Scrubbing those would make
# supertool disagree with the user's own shell about where they are.
GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)

# What `_main` removed from `os.environ` on the way in, so the notice can name
# it after the cwd has settled — the scrub happens before argv is parsed, the
# cwd it acted under is only known after `cwd:`/auto-root.
#
# Per-run scratch, filled in place rather than rebound: a daemon reuses the
# process, and a leak list left over from an earlier call would print a notice
# on a later clean one — #680's lesson about `_SKIP_COUNT`, same shape (#714).
_LEAKED_GIT_ENV: List[str] = []


def scrub_git_env(env: MutableMapping[str, str]) -> List[str]:
    """Delete git's repo pointers from `env`; return the names removed.

    `env` is `os.environ` itself at the one call site (#714), not a copy: a
    `del` there unsets the variable for this process AND for every child it
    spawns, which is what makes the guard total. Typed as a MutableMapping
    rather than a Dict because `os._Environ` is not a dict.
    """
    removed = [name for name in GIT_ENV_VARS if name in env]
    for name in removed:
        del env[name]
    return removed


def _git_env_notice(removed: List[str]) -> str:
    """One line naming what was scrubbed, or "" when nothing was.

    Scrubbed rather than refused: refusing would break the caller this repo
    creates itself — `.githooks/pre-commit` runs `git-diff:staged` under git's
    exported GIT_DIR, and a commit hook that aborts because supertool declined
    is a worse outcome than one that reads the right repo. But scrubbed
    LOUDLY. A silent scrub makes the tool ignore something the caller may have
    set on purpose and say nothing about it — the same "quiet where a loud
    answer belonged" that made the original bug invisible, just pointing the
    other way. The line costs one row of output and is the only thing that
    tells a caller their environment is leaking.

    Said once per call rather than once per op (#714). The scrub is now a
    property of the process, so repeating the line under each of six reads
    would describe one event six times.
    """
    if not removed:
        return ""
    return (
        f"scrubbed inherited git env: {', '.join(removed)} — this call acted "
        f"on the repo at {os.getcwd()}, not the one those variables named "
        f"(#692, #714)\n"
    )


def _declared_value_exits(entry: object) -> FrozenSet[int]:
    """Exit codes this op declares are ANSWERS, not verdicts about the op (#1672).

    `git-worktrees` compresses its occupancy verdict into the process exit
    status — `0 = idle, 1 = occupied, 2 = cannot tell` — and says so in the line
    immediately above the batch footer. The dispatcher read every non-zero child
    exit as a refusal, so three correct answers each rendered `FAIL` and the
    footer said `all 3 refused`, one line under the op's own `the op itself did
    not fail`.

    Not #1291, and not fixable where #1297 fixed that one: dispatch is right that
    the exit was non-zero, and cannot know that for this op non-zero is not
    failure. That is a property of the op, so it is declared where the op's other
    properties are — beside `paths` and `safety` in the registry.

    Tolerant like `_preset_path_decl`: a malformed declaration yields no codes,
    which is the behaviour that predates this and counts every non-zero as a
    refusal. Wrong in the loud direction rather than the quiet one.
    """
    if not isinstance(entry, dict):
        return frozenset()
    decl = entry.get("exitStatus")
    if not isinstance(decl, dict):
        return frozenset()
    values = decl.get("values")
    if not isinstance(values, list):
        return frozenset()
    # `bool` is an `int` in Python and `true` is legal JSON in that slot.
    return frozenset(v for v in values
                     if isinstance(v, int) and not isinstance(v, bool))


def _declared_clean_exits(entry: object) -> frozenset:
    """Which of an op's declared value exits mean "nothing to worry about" (#1705).

    A value-exit scheme spends the exit integer on an answer, and #1672 spent
    it without saying what `0` still had to mean. It kept meaning "the op did
    not fail" and stopped meaning "clear to proceed": `git-worktrees` answered
    `occupied` and supertool exited 0, so `supertool 'git-worktrees:P' && rm -rf
    P` — the consumer shape #1282 records — passed the guard on the one answer
    that exists to stop it.

    So the meaning of each value is the op's to state. `"clean": [0]` beside
    `"values"` names the ones a caller may proceed on; every other declared
    value is an answer supertool will not certify, and it reaches the process
    exit through `_UNCLEAN_VALUE_EXITS` without becoming a refusal.

    `0` is always clean and cannot be declared away here: exit 0 is never read
    as a value exit in the first place (`_resolve_custom_op` gates on non-zero),
    so listing it is documentation and omitting it changes nothing. A malformed
    or missing declaration therefore collapses to `{0}` — tolerant like
    `_declared_value_exits` above, and wrong in the loud direction, since the
    failure mode of guessing here is a caller proceeding on `occupied`.
    """
    if not isinstance(entry, dict):
        return frozenset({0})
    decl = entry.get("exitStatus")
    if not isinstance(decl, dict):
        return frozenset({0})
    clean = decl.get("clean")
    if not isinstance(clean, list):
        return frozenset({0})
    return frozenset({0}) | frozenset(
        v for v in clean if isinstance(v, int) and not isinstance(v, bool))


def _preset_declares_error(body: str) -> bool:
    """Did an op that gave up its exit integer state a failure in its body?

    The other half of the `exitStatus` bargain (#1672). Declaring the exit code
    a value spends the channel a refusal was travelling on, so the failure has to
    arrive somewhere else — and both replacements are conventions this repo
    already runs on: a line at column 0 beginning `ERROR: ` (the refusal form
    `_op_body_failed` reads at the head of every builtin's return), and anything
    on stderr, which is where a traceback lands when a crash exits with a code
    that happens to be declared.

    Any line, not just the first: a preset prints its board before it can know
    that a probe inside it went silent, and `git-worktrees` puts a foreign-copy
    note above its own `ERROR:` arm. Reading the op's own lines rather than the
    rendered receipt is what keeps this out of #1291's territory — and the ops
    that can declare `exitStatus` are ours, so "nothing a stranger named reaches
    column 0" is a guarantee the op makes (`git-worktrees` flattens every
    untrusted field for exactly this reason) rather than one assumed of it.
    """
    return any(line.startswith("ERROR: ") for line in body.splitlines())


def _op_config_collision_refusal(op: str, entry: Any,
                                  config: Dict[str, Any]) -> str | None:
    """ERROR text when `op`'s own config would export a colliding env var (#1009).

    Only ever fires for a key `op` ITSELF declared in the project's "ops"
    section: `config["_op_config_collisions"]` was built from `project_ops`
    alone (`_op_config_key_collisions`), so an op that merely inherits a
    preset's shared vocabulary key (`REPO_TARGET`, `DEFAULT_LIMIT`, ...) is
    never named here — only two ops that picked the same name independently,
    in the same project's own config, for genuinely differing values. A
    value deliberately repeated across a family of ops (this repo's own
    `watch_name: "oss-supertool"` on `channel`/`radar`/`unwatch`/`watch`/
    `watches`) is never a collision at all, so it never reaches this
    function's `collisions` lookup in the first place.
    """
    if not isinstance(entry, dict):
        return None
    collisions = config.get("_op_config_collisions")
    if not isinstance(collisions, dict) or not collisions:
        return None
    for key in entry:
        if not isinstance(key, str) or key in _OP_CONFIG_RESERVED_KEYS:
            continue
        env_key = f"SUPERTOOL_{key.upper()}"
        others = collisions.get(env_key)
        if others and op in others:
            rest = ", ".join(o for o in others if o != op)
            return (
                f"ERROR: `ops.{op}.{key}` in .supertool.json reaches this op "
                f"as `{env_key}`, and `ops.{rest}` declares the same key "
                f"name in the same file. Both would read one shared "
                f"variable with no way for either op to tell whose value it "
                f"got (#1009). Rename one — e.g. "
                f"`{op.replace('-', '_')}_{key}` — or drop the duplicate.\n"
            )
    return None


def _resolve_custom_op(op: str, parts: List[str]) -> str | None:
    """Try to run op as a custom command from config["ops"].

    Runs argv-form (shell=False) — shell metachars in the cmd template are
    literal tokens, not shell operators. `$VAR` / `${VAR}` expansion from
    the op's env is performed by supertool (see _expand_env below), no shell,
    and it reaches THE TEMPLATE ONLY: an argument interpolated through
    `{file}`/`{dir}`/`{arg}`/`{args}`/`{argjoin}` is caller data and is
    shielded from it (#1734).

    Returns formatted output string on match, None if op is not a custom op.
    """
    config = _load_config()
    ops = config.get("ops")
    if not ops or op not in ops:
        return None
    # Cleared before anything can return, so a frame whose op declined without
    # running never inherits the previous frame's verdict. `None` is the third
    # state and it is load-bearing: "did not run" must not read as "failed",
    # and it must certainly not read as "succeeded".
    _CUSTOM_OP_OK[0] = None

    # #678 — this op is defined by whatever tree the cwd resolved to, which is
    # not necessarily the tree this core came from. Decide before running it:
    # once the subprocess has answered, its output is indistinguishable from
    # the right one.
    _mixed = _mixed_tree_pair()
    if _mixed is not None and not _mixed_tree_allowed():
        _bump_counter(_SKIP_COUNT, "cnt_skip")
        return _mixed_tree_decline(op, _mixed)

    entry = ops[op]
    if isinstance(entry, str):
        cmd_template = entry
        timeout = config.get("timeout", 60)
    elif isinstance(entry, dict):
        cmd_template = entry.get("cmd", "")
        timeout = entry.get("timeout", config.get("timeout", 60))
    else:
        return f"ERROR: invalid config for custom op {op!r}\n"

    if not cmd_template:
        return f"ERROR: empty command for custom op {op!r}\n"

    # #1009 — before anything else runs: two ops that each declared their own
    # `ops.<op>.<key>` and landed on the same `SUPERTOOL_<KEY>` got no warning
    # before this, only whichever value its own entry happened to carry. Above
    # the subprocess for the same reason the containment gate below is —
    # once a colliding value has been read, the run it produced is
    # indistinguishable from a correctly configured one.
    _collision = _op_config_collision_refusal(op, entry, config)
    if _collision:
        return _collision

    # #1287 — the universal path chokepoint for preset and custom ops. Builtins
    # have had `_PATH_ARG_POSITIONS` in `_dispatch_impl` since #146; no preset
    # op was ever in it, so a preset op with a path argument enforced
    # containment itself or not at all, and "not at all" was the default for a
    # newly written one. #1283 was one instance of that, not the class.
    #
    # Above the subprocess, not inside it: an out-of-boundary read has already
    # happened by the time any of the op's output could be filtered.
    # Not counted as a skip. `_mixed_tree_decline` above is a skip because it
    # is one — the op could not be run safely and nobody was refused anything.
    # This is the builtin containment gate's twin, which returns its ERROR and
    # touches no counter, and the whole argument of #1287 is that an unchecked
    # path is a refusal rather than a check that could not run.
    _paths = _preset_path_containment(op, entry, parts)
    if _paths:
        return _paths

    # #873 — a token the template cannot carry is refused, not discarded. Kept
    # DOWNSTREAM of the containment gate above on purpose: this adds no path
    # slot and moves none, and a call that both escapes the boundary and drops
    # a token must still report the escape, which is the more severe of the
    # two. Above the subprocess for the same reason as the gate — once the op
    # has run under an argument list nobody asked for, the damage is done and
    # its output is indistinguishable from the one the caller meant.
    _dropped = _unconsumed_arg_tokens(cmd_template, parts)
    if _dropped:
        return _dropped_tokens_refusal(op, entry, cmd_template, _dropped)

    # Build the command — substitute {file}, {dir}, {arg}, {args}, {argjoin},
    # {python} in ONE pass. Chained str.replace calls would rescan the text a
    # previous pass just inserted, so an ARGUMENT VALUE containing a later
    # placeholder token (a commit message mentioning {argjoin}) got expanded
    # inside its own shlex.quote'd value — shattering the quoting and leaking
    # the value's words into argv. One pass never looks at inserted text.
    file_arg = parts[1] if len(parts) > 1 else ""
    dir_arg = os.path.dirname(file_arg) if file_arg else "."
    # {argjoin}: parts[1:] rejoined with ':::' as a single shell-quoted arg.
    # Lets the receiving script split fields itself when they contain colons
    # (e.g. XPath like .//ns:tag or [position()=1]).
    arg_join = ":::".join(parts[1:]) if len(parts) > 1 else ""
    # Shielded (#1734): every value below is CALLER DATA, and `_expand_env`
    # further down used to rewrite a `$NAME` inside one — so a commit message
    # naming `$HOME` was committed with the home directory in it. The values
    # are restored after the expansion, so the template keeps its own `$VAR`
    # feature and the caller's bytes reach the op untouched.
    cmd, _shield = _shield_substitute(cmd_template, {
        "python": _python_token(),
        "file": shlex.quote(file_arg),
        "dir": shlex.quote(dir_arg),
        "arg": shlex.quote(file_arg),
        "args": " ".join(shlex.quote(p) for p in parts[1:]) if len(parts) > 1 else "",
        "argjoin": shlex.quote(arg_join),
    })

    # Pass extra config keys as SUPERTOOL_ env vars
    # `replaces` is reserved because it is metadata for the raw-command guard
    # (#1347), read by the hook and never by the op's own subprocess. Without
    # it every gh-* call would carry a JSON-encoded copy of its own mapping
    # table in SUPERTOOL_REPLACES, which nothing reads.
    # `paths` for the same reason and one more (#1357): it is the containment
    # declaration `_preset_path_containment` has already enforced by the time
    # this line runs, so the subprocess it would reach is the very process the
    # declaration constrains. That is the wrong direction for the information
    # to travel, and nothing reads SUPERTOOL_PATHS — a tree-wide grep returns
    # zero. Reserved rather than left exported with a note, because "our own
    # config, so harmless" is a judgement that has to be re-made by every
    # future reader instead of once, here.
    # `exitStatus` joins them for the same reason as `paths` (#1672): it is a
    # declaration the dispatcher acts on around the subprocess, so exporting it
    # into that subprocess is the wrong direction for the information to travel.
    # `safety` and `repo_target` join for #1757 — both are core-only metadata
    # (safety classification, whether a leading `repo:OWNER/NAME` token is
    # honoured) with no reader in any op's own subprocess.
    # Shared with `_op_config_key_collisions` — see that constant's docstring
    # for why the two must read the same set (#1009).
    _RESERVED_KEYS = _OP_CONFIG_RESERVED_KEYS
    # No scrub here any more (#714). #692 put one on this line because every
    # PRESET op is launched from it — true, and the reasoning holds, but this
    # is the launcher for half the op table. Built-ins never reach it, and core
    # spawns git in six of its own functions. `os.environ` is scrubbed once in
    # `_main` instead, so this copy is already clean and a preset stays covered
    # by being launched rather than by opting in.
    env = dict(os.environ)
    # Which separator produced the argv this op is about to receive (#946).
    # A preset that reconstructs the caller's input — git-commit's spilled
    # message refusal is the one that does — cannot otherwise tell ':::' from
    # ':' from a payload whose fields were never split, and rejoining on the
    # wrong one hands back a suggestion that silently rewrites the message.
    env["SUPERTOOL_ARG_SEP"] = _ARG_SEP[0]
    if isinstance(entry, dict):
        for k, v in entry.items():
            if k not in _RESERVED_KEYS:
                # A relative entry in a search-path key (`watch_sources_path`)
                # is anchored to the declaring config file's directory before
                # export, so what the subprocess inherits — and what a poller
                # re-derives after any number of re-execs — is already an
                # absolute path (#2164). Only fires with a config file to
                # anchor against; a value with none (arrived through the raw
                # environment with no `.supertool.json` at all — `_CONFIG_PATH`
                # is unset) is untouched and keeps the absolute-only rule
                # `sourcepath.resolve()` itself still enforces.
                if (k in _RELATIVE_SEARCH_PATH_KEYS and isinstance(v, str)
                        and _CONFIG_PATH):
                    v = _anchor_relative_search_path(v, _CONFIG_PATH)
                # Strings pass through verbatim (e.g. CSV "error_patterns").
                # Non-scalars (lists/dicts, e.g. "job_patterns") are JSON-encoded
                # so the receiving preset can json.loads them back — str() would
                # emit a Python repr that json.loads can't parse.
                env[f"SUPERTOOL_{k.upper()}"] = v if isinstance(v, str) else json.dumps(v)

    _prefix_env, cmd = _extract_env_prefix(cmd)
    # Unshield the prefix VALUES too: `KEY={dir} cmd` puts caller data in one,
    # and it has to reach the child's environment as the real path (#1734).
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    env.update(_prefix_env)
    cmd = _unshield(_expand_env(cmd, env), _shield)

    t0 = time.monotonic()
    try:
        # argv-form (shell=False) — shell metachars in the template become
        # literal tokens, not shell operators. Placeholder values are still
        # shlex.quote'd above so values containing spaces survive shlex.split.
        # encoding is pinned rather than left to the locale: presets print
        # ✓/✗/⚠ as UTF-8, and a cp1252 default decodes those three bytes into
        # three wrong characters, so the receipt renders mojibake for an op
        # that worked. errors="replace" keeps a preset emitting genuinely
        # undecodable bytes from taking the whole run down with it.
        result = subprocess.run(
            shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=env,
        )
        elapsed = _elapsed_since(t0)
        output = result.stdout
        # A declared value exit is an answer, so the verdict comes from the two
        # channels the declaration leaves the op (#1672). Only for a NON-ZERO
        # code: exit 0 has always been a pass here whatever it printed, and
        # widening that is a different change with a different blast radius.
        _value_exit = (result.returncode != 0
                       and result.returncode in _declared_value_exits(entry))
        _failed = result.returncode != 0
        if _value_exit:
            _failed = bool(result.stderr.strip()) or _preset_declares_error(output)
        _CUSTOM_OP_OK[0] = not _failed
        # #1705 — the answer is not a refusal (that is #1672, and it stands),
        # but an answer the op does not declare clear-to-proceed must not leave
        # the process exiting 0. It travels the same road as a skipped write and
        # a rolled-back edit: a per-call list `_main` reads as a delta, never
        # `_mark_op_failure`, so the receipt keeps saying PASS and the batch
        # footer keeps not saying `refused`.
        _unclean_value = (
            _value_exit and not _failed
            and result.returncode not in _declared_clean_exits(entry))
        if _unclean_value:
            _UNCLEAN_VALUE_EXITS.append(f"{op} exited {result.returncode}")
        if _failed:
            if result.stderr:
                output += result.stderr
            return f"FAIL ({elapsed:.2f}s)\n{output}"
        # A deliberate mix still never prints a bare PASS — the verdict line
        # carries which two trees produced it (#678).
        _stamp = f" [{_mixed_tree_note(_mixed)}]" if _mixed is not None else ""
        # PASS beside a non-zero child exit has to say why it is one, on the
        # verdict line, or the reader is left to reconcile it against the op's
        # own exit note further down (#1496's rule, #1672's case).
        _value_note = (f" [exit {result.returncode} is this op's answer, not a "
                       f"verdict — its registry entry declares the exit code a "
                       f"value]") if _value_exit else ""
        # And when that value is not one the op declares clear to proceed, the
        # same line has to carry why supertool itself exits non-zero — a single
        # op prints no batch tally, so this is the only place a reader gets it.
        if _unclean_value:
            _value_note = (f" [exit {result.returncode} is this op's answer, not "
                           f"a verdict — its registry entry declares the exit "
                           f"code a value, and not one it declares clear to "
                           f"proceed. The op did not fail; supertool exits "
                           f"non-zero so a `&&` guard does not read this answer "
                           f"as permission]")
        return (f"PASS ({elapsed:.2f}s){_stamp}{_value_note}\n"
                f"{output}{_maybe_restart_mcp(entry)}")
    except subprocess.TimeoutExpired as e:
        # Not `_elapsed_since`: the freeze it applies zeroes the one number
        # this line exists to report (#727).
        return (f"{_timeout_verdict_line(t0, timeout)}\n"
                f"{_timeout_partial_output(e)}")
    except OSError as e:
        return f"FAIL: {e}\n"


def _timeout_partial_output(exc: subprocess.TimeoutExpired) -> str:
    """Whatever the killed command printed before the clock ran out.

    Dropping it costs the caller the only evidence of how far the op got — for
    an op that mutates remote state (a push), the difference between "retry" and
    "already landed" was sitting in that discarded buffer (#399).
    """
    chunks = []
    for stream in (exc.stdout, exc.stderr):
        if not stream:
            continue
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream
        if text.strip():
            chunks.append(text if text.endswith("\n") else text + "\n")
    if not chunks:
        return ""
    return "--- partial output before timeout ---\n" + "".join(chunks)


def _maybe_restart_mcp(entry: object) -> str:
    """SIGTERM the warm MCP daemons a custom op asks to restart via `restartMcp`.

    A custom op that invalidates state the warm LSP daemons cache (autoload map,
    PHPStan result cache, etc.) can declare `restartMcp` so the daemons are
    stopped after the cmd succeeds; the next op that touches each server
    cold-starts a fresh daemon that re-reads the cleared state. Accepts:
      true       -> every server in the config "mcp" block
      ["a","b"]  -> only those servers
      "name"     -> a single server
    Names not present in the config "mcp" block are reported separately instead
    of being counted as restarted, so the status line never claims to have
    stopped a daemon that was never configured. Returns a one-line status suffix
    (empty when nothing to restart). Best-effort like the new-file path — a stop
    failure never fails the op.

    A stop that failed is reported as failed rather than counted as a restart
    (#547). This is the one caller that already asserts an outcome out loud, so
    the honest version costs no new noise — only the false claim goes away. A
    daemon that would not die keeps answering from the index it captured before
    the op cleared the state, which is exactly what `restartMcp` exists to
    prevent, so the reader needs to know it did not happen.
    """
    if not isinstance(entry, dict):
        return ""
    spec = entry.get("restartMcp")
    if not spec:
        return ""
    if spec is True:
        names = list(_mcp_specs.keys())
    elif isinstance(spec, list):
        names = [str(n) for n in spec]
    else:
        names = [str(spec)]
    known = [n for n in names if n in _mcp_specs]
    unknown = [n for n in names if n not in _mcp_specs]
    restarted, failed = [], []
    for name in known:
        outcome = _mcp_stop_server(name)
        (restarted if outcome.ok else failed).append(name)
    # Through `_flat_keys`, like the payload-key refusals of #1583, and for the
    # same mechanism: a server name is an arbitrary JSON string, `restartMcp`
    # accepts it verbatim, and `', '.join` would put a line of that string's
    # choosing at column 0 inside a line supertool signs — a second
    # `mcp: restarted 9 daemon(s)` the tool never said. Lower severity than
    # #1583's, and named rather than assumed: these come from
    # `.supertool.json`, which #146's trust note places at the same level as
    # the validators and custom ops it declares alongside them. Done anyway
    # because the alternative is one call cheaper and the reasoning has to be
    # re-derived by every reader who checks (#1489 release audit).
    note = ""
    if restarted:
        note += (f"mcp: restarted {len(restarted)} daemon(s) "
                 f"({_flat_keys(restarted)})\n")
    if failed:
        note += (f"mcp: FAILED to stop {len(failed)} daemon(s) ({_flat_keys(failed)})"
                 f" — they may still answer from a stale index"
                 f" (SUPERTOOL_DEBUG=1 for the reason)\n")
    if unknown:
        note += f"mcp: unknown server(s) ignored ({_flat_keys(unknown)})\n"
    return note


_IN_ALIAS = False  # recursion guard — prevents alias-from-alias expansion


def _resolve_alias(op: str, parts: List[str]) -> str | None:
    """Try to expand op as an alias from config["aliases"].

    Returns concatenated output of all expanded ops, None if not an alias.
    Aliases expand to ops (built-in or custom) but NOT to other aliases.
    """
    global _IN_ALIAS
    if _IN_ALIAS:
        return None  # block recursive alias expansion

    config = _load_config()
    aliases = config.get("aliases")
    if not aliases or op not in aliases:
        return None

    alias_def = aliases[op]
    if not isinstance(alias_def, dict):
        return f"ERROR: alias {op!r} must be an object with 'ops' key\n"

    op_list = alias_def.get("ops", [])
    if not isinstance(op_list, list):
        return f"ERROR: alias {op!r} 'ops' must be a list\n"

    if not op_list:
        return ""

    # Replace {file}, {dir}, {arg}, and {args} placeholders in each expanded op
    file_arg = parts[1] if len(parts) > 1 else ""
    dir_arg = os.path.dirname(file_arg) if file_arg else "."
    all_args = " ".join(parts[1:]) if len(parts) > 1 else ""

    _IN_ALIAS = True
    try:
        output_parts: List[str] = []
        alias_values = {
            "file": file_arg, "dir": dir_arg,
            "arg": file_arg, "args": all_args,
        }
        for expanded_op in op_list:
            # Single pass — same reason as _resolve_custom_op: a path or
            # argument containing '{args}' must not expand itself.
            resolved = _substitute_placeholders(expanded_op, alias_values)
            output_parts.append(dispatch(resolved))
        return "".join(output_parts)
    finally:
        _IN_ALIAS = False


