"""_supertool_grep -- the grep-family ops, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_grep")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so any
`monkeypatch.setattr(supertool, "op_grep", ...)` (or any other name defined
here) keeps reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_grep` or `python3 _supertool_grep.py` gets this module's
own fresh globals(), which has no such name, and refuses with a clear
ImportError rather than failing later with a NameError on the first name this
file assumes `_supertool.py` already defined (Dict, Any, os, re, shlex, ...).

This part holds `op_grep`, `op_around`, `op_between_symbol`,
`op_between_pattern`, the pattern gate (`_pattern_gate` and every saturation/
literal/quote-pair helper feeding it) and the delegated-rtk and native-walker
grep internals (`_grep_recursive`, `_grep_candidates`, `_rtk_grep_census`,
...). `_pattern_gate` is called from `_supertool_read.py`'s `op_read` (the
`grep=` inline filter) and from elsewhere in `_supertool.py` itself -- that
cross-part call is exactly as safe as a same-file one, since every part
shares one globals() dict by the time any op runs; see `_supertool_read.py`'s
own docstring for the reverse direction.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_grep.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )




_REGEX_METACHARS = re.compile(r"[()\[\]{}|.*+?^$\\]")


def _is_regexy(pattern: str) -> bool:
    """True if pattern holds regex metacharacters that could make a literal
    code fragment match the wrong thing — or nothing. Gates the zero-hit
    literal fallback so plain-word searches never pay for a second pass."""
    return bool(_REGEX_METACHARS.search(pattern))


def _literal_note(pattern: str, count: int) -> str:
    """One-line banner shown when a pattern found 0 regex hits but matched
    literally once metacharacters were escaped — tells the caller the search
    auto-corrected so they learn the pattern was regex-ambiguous."""
    return (f"(no regex match; showing {count} literal "
            f"match(es) for {pattern!r})\n")


# #1435 — quoting a phrase is what a shell user does with a pattern containing
# a space, and every pattern-taking op here searched the quote characters as
# literal text. `grep:'beta':q.txt` returned `0 results ... scanned 1 files` —
# the disclosure that says the op *looked* — so the zero read as an absence in
# the file rather than in the pattern.
_PATTERN_QUOTE_CHARS = ("'", chr(34), "`")


def _unwrapped_pattern(pattern: str) -> str:
    """A pattern's body with a matched surrounding quote pair removed, or "".

    Only a *pair* counts. `'zzz'|'yyy'` begins and ends with `'` and is not
    one; stripping its ends yields `zzz'|'yyy`, a pattern nobody typed. So any
    further occurrence of the quote character disqualifies it, as does an empty
    body — `''` unwraps to nothing, which is not a search.
    """
    if len(pattern) < 3:
        return ""
    quote = pattern[0]
    if quote not in _PATTERN_QUOTE_CHARS or pattern[-1] != quote:
        return ""
    inner = pattern[1:-1]
    if quote in inner:
        return ""
    return inner


def _quoted_pattern_note(pattern: str, inner: str,
                         inner_matches: bool) -> str:
    """Explain a zero that a quote pair may have produced (#1435).

    Nothing is stripped and nothing is refused, which is the whole point of
    this shape: a caller hunting a genuinely quoted phrase — a real pattern in
    a codebase full of quoted JSON and quoted payload bodies — typed exactly
    what they meant and keeps their result untouched. Only the silence after a
    zero changes.

    The probe is what stops the note being noise. Naming the quotes alone would
    send every deliberate quoted search off to re-run a query that comes back
    just as empty, so the unwrapped spelling is tried and the answer reported:
    three states, not two — the quotes hid it, the quotes are innocent and the
    absence is real, or the pattern was never a pair and there is no note.
    """
    quote = pattern[0]
    lead = (f"(the pattern {pattern!r} begins and ends with `{quote}`, and "
            f"both were searched as literal text rather than read as "
            f"quoting. ")
    if inner_matches:
        return lead + (f"Without them, {inner!r} DOES match here — this zero "
                       f"is about the quotes, not about what was scanned.)"
                       + chr(10))
    return lead + (f"Without them, {inner!r} matches nothing here either — "
                   f"the quotes are not why this is zero.)" + chr(10))


def _compile_lenient(pattern: str) -> "re.Pattern[str]":
    """Compile, falling back to the escaped literal — the reading every search
    helper in this file already applies (`_grep_recursive`, `_grep_count`,
    `_op_around`). A #1435 probe that compiled strictly instead would print the
    note on `grep` and swallow it on `around`, so an unwrapped pattern that is
    not a regex would answer differently depending on which op asked."""
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


def _quote_pair_note(pattern: str, probe: Callable[[str], object]) -> str:
    """`_quoted_pattern_note` plus its probe, for a result that came back zero.

    Each caller supplies its own `probe` rather than sharing one search: the
    note has to be about the same population the zero was about, and `grep`'s
    candidate list, `around`'s walk and `read`'s line window are three
    different populations. A probe that raises answers nothing and prints
    nothing — the unwrapped pattern is not guaranteed to compile.
    """
    inner = _unwrapped_pattern(pattern)
    if not inner:
        return ""
    try:
        matches = bool(probe(inner))
    except (re.error, OSError, UnicodeError):
        # Named rather than bare (PR review, #1435): the unwrapped pattern is
        # not guaranteed to compile and the probe touches the filesystem, so
        # those two are expected and print nothing. A bare `except Exception`
        # here would turn a genuine bug on this rarely-walked path into a
        # missing note, which is the silence this whole change is against.
        return ""
    return _quoted_pattern_note(pattern, inner, matches)


def _case_insensitive_note(pattern: str, probe: Callable[[str], object]) -> str:
    """Explain a zero that case sensitivity alone may have produced (#1777).

    grep is case-sensitive throughout supertool, and the instruction callers
    are given says to confirm a write a second way. Grepping back a sentence
    that IS on disk with different capitalisation returns `0 results` -- a
    false negative on a successful write, produced by the exact step that
    exists to catch a failed one. `0 results` is the one output where a
    case-insensitivity hint is worth printing, because it is the only one
    where the caller cannot tell a real absence from a spelling mismatch.

    Probed rather than asserted, the same shape as `_quote_pair_note`: an
    inline `(?i)` re-run settles whether case is the reason, so a genuine
    absence gets no extra noise and there is no second search dialect to
    maintain -- Python's own `re` already understands the prefix.

    Gated on the pattern actually holding a case-sensitive character before
    the probe ever touches the filesystem (review finding, #1777): a pattern
    with no letters at all -- a bare number, punctuation -- cannot be
    case-ambiguous, and `_quote_pair_note`'s own zero-result probe is skipped
    for the equivalent reason (no quote pair, no probe). Without this, every
    genuinely-absent, letter-free pattern paid for a second full-corpus walk
    the disclosure could never use -- the exact cost the comment above this
    function's call sites already warns against paying twice (PR review,
    #1435).
    """
    if not any(c.isalpha() for c in pattern):
        return ""
    try:
        matches = bool(probe("(?i)" + pattern))
    except (re.error, OSError, UnicodeError):
        # Same reasoning as `_quote_pair_note`: the probe touches the
        # filesystem and the pattern is not guaranteed to compile with the
        # prefix prepended, so those two are expected and print nothing.
        return ""
    if not matches:
        return ""
    # Never suggest the quoted form: `_quote_pair_note`/`_unwrapped_pattern`
    # (right above this function) exist because supertool's grep does NOT
    # strip a quote pair -- a quoted pattern is searched literally, quotes
    # and all. Wrapping the rerun in quotes here would send the caller
    # straight into that other zero, undisclosed (review finding, #1777).
    return (f"(grep is case-sensitive; {pattern!r} DOES match here when "
            f"searched case-insensitively -- this zero may be about "
            f"capitalisation, not absence. Re-run as "
            f"grep:(?i){pattern}:PATH for a case-insensitive search -- no "
            f"quotes around the pattern, which grep searches literally.)"
            + chr(10))


# Patterns whose meaning differs between Python's `re` and POSIX ERE (#987).
# The delegated path hands the pattern to the system grep, so anything matching
# this never leaves the native walker:
#   \X  — every escape EXCEPT the punctuation both dialects agree on
#         (`\. \$ \* \+ \? \( \) \[ \] \{ \} \| \\ \/ \-`). That covers Python-only
#         classes (\d \w \s \b \A) and backreferences, and also GNU's word
#         boundaries \< \>, which ERE honours and Python reads as `<` and `>`
#         — a divergence a `\<alnum>` rule silently let through.
#   (?  — lookaround, non-capturing groups, inline flags: Python only
#   *? +? ??  — non-greedy; ERE reads a second, stray quantifier
#   [: [. [=  — POSIX bracket classes, which Python reads literally
_ERE_UNSAFE = re.compile(r"\\[^.^$*+?()\[\]{}|\\/-]|\\$|\(\?|[*+?}]\?|\[[:.=]")


# #1120 — supertool rewrites bash-grep BRE alternation so `a\|b` behaves the way
# a caller's fingers expect. The rewrite is unconditional, and it cannot tell that
# `\| \{` was an ESCAPED LITERAL pipe: it produces `| \{`, whose left alternation
# branch is empty. An empty branch matches the empty string, so the pattern matches
# every line of every file — and `1000+ matches` renders identically to a search
# that genuinely found a lot. The `:`-tokenizer, the filed suspect, is innocent.
_BRE_ALT = "\\|"


def _bre_alternation_rewrite(pattern: str) -> Tuple[str, bool]:
    """Apply the BRE-alternation rewrite, reporting whether it changed anything."""
    if _BRE_ALT not in pattern:
        return pattern, False
    return pattern.replace(_BRE_ALT, "|"), True


def _top_level_branches(pattern: str) -> List[str]:
    """Split on `|` at nesting depth 0, outside character classes, unescaped.

    Depth matters: `colo(u|)r` has an empty branch and matches exactly two words,
    while a bare `colou|r` with an empty branch would match everything. A `|`
    inside `[...]` is an ordinary character and starts no alternation at all.
    """
    branches: List[str] = []
    depth = 0
    in_class = False
    start = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "|" and depth == 0:
            branches.append(pattern[start:i])
            start = i + 1
        i += 1
    branches.append(pattern[start:])
    return branches


# Probe strings for "does this branch match every line?" (#1314). A branch that
# matches the EMPTY string matches at position 0 of every line, so the whole
# alternation does — that is the property, and an empty branch is only its
# simplest spelling. `^`, `$`, `.*` and `z*` all have it and all sailed past the
# #1120 predicate, which tested for `b == ""`: the filed call
# `grep:^\|def op_:_supertool.py:5` reported 5 of "1000+ matches" for a pattern
# that matched every line of the file.
#
# The empty probe alone is not enough, and this is where the line is drawn.
# `^$` matches the empty string and NOT `x`, so `^$|alpha` is a real search
# ("blank lines or alpha") and refusing it would remove the op for a legitimate
# caller. A branch has to match every probe — including non-empty ones — before
# it is called saturating. Same reason `.` and `\b` stay out: neither matches a
# blank line, so neither makes the pattern match every line.
_SATURATION_PROBES = ("", "x", "supertool 42", "  ")


def _branch_matches_everything(branch: str) -> bool:
    """True when this one top-level branch matches every line there is."""
    if branch == "":
        return True
    try:
        regex = re.compile(branch)
    except re.error:
        # An uncompilable branch is not a saturation claim to make here; the
        # pattern's own compile (and its literal fallback) decides what happens.
        return False
    return all(regex.search(probe) is not None for probe in _SATURATION_PROBES)


def _saturating_branch(pattern: str) -> Optional[str]:
    """The first TOP-LEVEL alternation branch that matches every line, or None.

    Returns the branch rather than a bool because the refusal has to name it:
    `^|def op_` and `|def op_` are one keystroke apart and want different fixes,
    and a diagnosis that does not say which half saturated is not actionable.
    """
    if "|" not in pattern:
        return None
    branches = _top_level_branches(pattern)
    if len(branches) < 2:
        return None
    for branch in branches:
        if _branch_matches_everything(branch):
            return branch
    return None


def _saturates(pattern: str) -> bool:
    """True when a TOP-LEVEL alternation branch matches every line, i.e. the
    pattern matches every line. Not a search — a saturation."""
    return _saturating_branch(pattern) is not None


def _saturating_pattern_refusal(written: str, effective: str,
                                rewritten: bool) -> str:
    """Refuse a pattern that matches every line, naming the spelling that works.

    The three-state contract: `ok`, a finding, and — here — declining, because a
    saturated match is not an answer to the question that was asked and its
    report cannot be told apart from a real one. Nothing downstream can recover
    the distinction, so it has to be refused at the call.
    """
    branch = _saturating_branch(effective)
    if branch is None:
        return ""
    if branch == "":
        why = ("has an empty alternation branch, so it matches every line of "
               "every file scanned")
    else:
        why = (f"has an alternation branch `{branch}` that matches the empty "
               f"string, so the whole pattern matches every line of every "
               f"file scanned")
    # Backticks, not !r: repr() DOUBLES every backslash, and a backslash is the
    # one character this message exists to show. `'\\| \\{'` for a pattern the
    # caller typed as `\| \{` is the tool mangling its own diagnosis.
    lines = [
        f"ERROR: pattern `{effective}` {why}. That is a saturated pattern, not "
        f"a search, and its result count is indistinguishable from a search "
        f"that genuinely found a lot.",
    ]
    if rewritten:
        became = ("a bare `|` with nothing to its left" if branch == ""
                  else "a top-level `|` that split the pattern into branches")
        lines.append(
            f"  written as `{written}` — supertool rewrites bash-grep BRE "
            f"alternation, so `{_BRE_ALT}` became {became}.")
        lines.append(
            f"  for a literal `|`, use a character class: "
            f"`{written.replace(_BRE_ALT, '[|]')}`")
    else:
        lines.append(
            "  for a literal `|`, use a character class: `[|]`")
    return chr(10).join(lines) + chr(10)


def _bre_rewrite_note(written: str, effective: str, rewritten: bool) -> str:
    """Disclose the rewrite whenever it fired (#1120).

    An escape being eaten is invisible in a result set that looks plausible, and
    the same move — say which pattern actually ran — is what #1065 added for the
    ':' rejoin and what `scanned N files` added for the zero case.
    """
    if not rewritten:
        return ""
    return (f"(pattern rewritten to `{effective}` — `{_BRE_ALT}` is bash-grep "
            f"BRE alternation and became a plain `|`. For a literal `|`, use "
            f"`[|]`.)" + chr(10))


def _pattern_gate(
    pattern: str, check_saturation: bool = True
) -> Tuple[str, str, str]:
    """The one place a caller-supplied search pattern is normalised and judged.

    Returns `(effective, refusal, note)`. A non-empty `refusal` is the whole
    answer and the op must return it unchanged; otherwise `effective` is the
    pattern to run and `note` is the disclosure to print above the result.

    Single-sourced because it was not, and the cost was #1344: the rewrite and
    the refusal were wired route by route, so `read:PATH:::grep=` — added later
    and reached through a different parser branch — arrived with neither, and
    the same pattern meant three different things across four routes.

    Whether a filter on a bounded window should be refused at all is the
    decision #1344 asked for, and it is answered here rather than at each call:
    yes. `read`'s `grep=` returns whole-file output when it saturates, which
    looks exactly like `read:PATH` and therefore hides nothing loud — but what
    the caller takes away is that every one of those lines matched the pattern
    they typed, which is the same false belief #1314 was filed about, arrived
    at more quietly. Disclosing instead would leave the caller holding a file
    they already had a spelling for (`read:PATH`), paid for in context, under a
    note correcting the request they just made. Refusing costs nothing a
    caller meant: the predicate fires only on a top-level alternation with a
    branch that matches every probe, so `^$|alpha` and `colo(u|)r` still run.

    The #150 ReDoS backtracking guard (`_has_outer_wrapped_unbounded_group`,
    hardened by #1311/#2535) lived only inside `_op_grep` until #2547: every
    other route through this chokepoint -- `around`'s public wrapper and
    `read`'s `grep=` -- reached `re.compile`/match against real file content
    with the same adversarial shape (`(x(a+)+y)`) completely unrefused. Single-
    sourcing it here, ahead of the rewrite, is the fix `_pattern_gate`'s own
    reason for existing already argues for: a guard wired route by route is a
    guard that drifts, the same way the rewrite and the saturation refusal
    once did (#1344).

    `check_saturation=False` (#2573) exists for callers whose semantics are a
    single-match position search rather than a filtered count -- `op_vim`'s
    `/`, `?`, `n`/`N`, the inline `o`/`O` search-then-open reflex, the `:d
    /PAT/` address resolver and the operator motion forms. There, "the whole
    pattern matches every line" is not a meaningless result the way it is
    for `grep`/`around`: a bare `|` is a completely ordinary, previously-
    working vim search for a literal pipe character (`_saturating_branch`
    sees the empty alternation branch and refuses it), and vim has no result
    count for the refusal's own rationale to apply to. `op_vim`'s `:s` and
    `:g`/`:v` do NOT pass `check_saturation=False`, on the same reasoning in
    reverse: those two act on every match (substitute every occurrence,
    delete every matching line), so a saturating pattern is exactly as
    destructive there as it is for `grep`/`around` -- a bare `|` as `:s`'s
    PAT would silently interleave every character of the buffer with the
    replacement, and as `:g`'s PAT would silently delete the whole file. The
    length cap and the ReDoS backtracking guard still apply unconditionally
    everywhere -- this parameter only turns off the one check whose
    rationale does not transfer, and only where it does not transfer.
    `trap.d/2571.between-saturating-refusal.md` flagged the identical
    mismatch for `op_between_pattern`, left unresolved there; `op_vim` is the
    first call site to actually need the opt-out rather than merely note the
    mismatch.
    """
    if len(pattern) > 1000:
        return pattern, (
            f"ERROR: pattern too long ({len(pattern)} > 1000 chars)\n"
        ), ""
    if _has_outer_wrapped_unbounded_group(pattern):
        return pattern, (
            "ERROR: pattern contains nested unbounded quantifiers "
            f"({pattern!r}) — would risk catastrophic backtracking. "
            "Rewrite without `(...+)+`-style nesting.\n"
        ), ""
    effective, rewritten = _bre_alternation_rewrite(pattern)
    if check_saturation:
        refusal = _saturating_pattern_refusal(pattern, effective, rewritten)
        if refusal:
            return effective, refusal, ""
    return effective, "", _bre_rewrite_note(pattern, effective, rewritten)


def _pattern_read_as_note(pattern: str, path: str, op: str = "grep") -> str:
    """Name the pattern `op` actually ran, when a ':' made that a choice (#1065).

    `grep:re:Checks|failed:PATH` tokenizes to the pattern `re:Checks|failed`,
    and `|` binds looser than concatenation, so the first alternation branch is
    the literal `re:Checks` — which matches nothing. The op then reports three
    confident results for a question nobody asked, and `scanned 1 files` is
    both true and useless: it did look, at someone else's pattern.

    Nothing is refused here, because the tokenization is not ambiguous — it is
    documented, deterministic, and the payload route already covers what the
    colon CLI cannot express. What was missing is the disclosure. A ':' in the
    pattern is exactly the condition under which the rejoin happened, so that
    is when the effective pattern is echoed back.

    The path is named too (#1166). The split has two outputs and the note
    disclosed one: a receipt that is precise about the pattern and silent about
    which of the remaining tokens became the FILE reads as complete, which is
    exactly what stops a reader checking the half that moved.

    Parameterised on the op since #1821, which was filed against grep and is
    true of `around`. Both rejoin with `_split_arg`, both share
    `_colon_split_hint` for the PATH slot that does not exist — and where the
    slot DOES exist, only grep said which pattern it had settled on. `around`
    printed a plausible answer and nothing else, which is the same defect one
    step further along: not a caveat in the wrong place, no caveat. The op name
    is threaded through rather than hardcoded because the escape hatch and the
    `re:` sentence are both op-specific, and a note that tells an `around`
    caller about `grep:@-` sends them to a route they did not call.
    """
    if ":" not in pattern:
        return ""
    note = (f"(pattern read as {pattern!r}, path as {path!r} — the ':' is part "
            f"of the regex, not a separator. Use {op}:@- with a `pattern` key "
            "if the split was meant to fall elsewhere.)" + chr(10))
    if pattern.startswith("re:"):
        note += (f"({op} has no `re:` prefix — every {op} pattern is already a "
                 "regex, so `re:` is literal text and forms part of the first "
                 "alternation branch. `between:re:START:END:PATH` is the op "
                 "that has one.)" + chr(10))
    return note


def _count_lines(path: str, on_error: int = 0) -> int:
    """Count lines in a file cheaply, streaming in binary (#362).

    `on_error` is what an unreadable file counts as, because the callers want
    opposite things and one hardcoded answer is wrong for the other (#388). The
    glob:/grep: auto-read line cap passes `MAX_AUTOREAD_LINES + 1` so a file it
    cannot measure is treated as over-cap and left unread — failing closed.
    `map` and the workspace summary keep the default 0, since a sentinel would
    render as a wildly wrong line count in a listing.
    """
    try:
        count = 0
        last = b""
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                count += chunk.count(b"\n")
                last = chunk
        if last == b"":
            return 0  # empty file
        # trailing line without a final newline
        if not last.endswith(b"\n"):
            count += 1
        return count
    except OSError:
        return on_error


# #1328 — `all` names the LIMIT slot and only that slot. Both neighbours are
# refusals rather than readings: `all` in the CONTEXT slot read as a limit runs
# a call nobody typed, and a third trailing token dropped on the floor (the
# parser peels every trailing digit and reads two) runs the default under a
# token the caller believes removed the cap.
_GREP_ALL_OUTSIDE_LIMIT_SLOT = (
    "ERROR: grep read `all` outside the LIMIT slot (grep:PATTERN:PATH:LIMIT:"
    "CONTEXT). `all` is a LIMIT — it removes the result cap so a sweep is "
    "complete; CONTEXT is a number of lines around each match and has no "
    "`all`, and nothing follows CONTEXT. Did you mean "
    "grep:PATTERN:PATH:all:CONTEXT?" + chr(10)
)

# `grep_around` is PATTERN:PATH:N:LIMIT — context FIRST, the opposite order to
# grep's LIMIT:CONTEXT. Copying `grep:PATTERN:PATH:all` across lands `all` in
# the N slot, where int() used to raise and the caller got the exception text.
_GREP_AROUND_ALL_IN_N_SLOT = (
    "ERROR: grep_around takes PATTERN:PATH:N:LIMIT — context first, the "
    "opposite order to grep's LIMIT:CONTEXT — so `all` landed in the N slot. "
    "Did you mean grep_around:PATTERN:PATH:N:all (e.g. "
    "grep_around:PATTERN:PATH:3:all), or grep:PATTERN:PATH:all:N?" + chr(10)
)


# #945 — `0` is the near-universal spelling of "no limit", and grep accepted it,
# ignored it, and quietly applied the default instead. Neither meaning is
# guessed: honouring it as unlimited would hand a caller who typed one character
# an unbounded dump into a shared output budget, and substituting the default
# means the number that ran was never the number that was typed.
# A function, not the constant it was until #1334. The refusal names two
# readings and used to give a spelling for neither, so a caller who meant either
# one paid a round-trip to find it — while the op has accepted BOTH since #1328.
# The default is resolved here rather than frozen at import, because
# `grep.max_results` is a per-project setting: naming MAX_GREP_RESULTS while a
# project's own cap was the one about to run is this repo's own defect, a number
# the tool produced being read as a number about the world.
def _grep_zero_limit() -> str:
    default = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    return (
        'ERROR: grep LIMIT 0 is not "unlimited" here, and supertool will not '
        "guess which of the two it meant. Uncapped output would land in the "
        "caller's context before it could be declined, and silently "
        "substituting the default would mean the LIMIT that ran was never the "
        "LIMIT that was typed. Both readings have a spelling: "
        f"grep:PATTERN:PATH:{_GREP_ALL_TOKEN} for every match, or omit LIMIT "
        f"for the default of {default} (grep:PATTERN:PATH:200 for any other "
        "cap)." + chr(10)
    )


def op_grep(pattern: str, path: str = ".", limit: int = 0,
            context: int = 0, count_only: bool = False,
            no_exclude: bool = False, no_auto_read: bool = False,
            via_payload: bool = False, full: bool = False) -> str:
    """grep, prefixed by #1065's disclosure of the pattern that actually ran.

    `via_payload` is set by the @file/@- route (#1825): there, `pattern` is
    taken verbatim from a `pattern` key, so nothing was split on ':' and the
    colon-rejoin disclosure would send the caller back to the route they are
    already on. The positional-CLI route leaves it False and keeps the note.

    `full` (#1712) suppresses the per-line char cap for this call only — the
    default stays truncating for every call that does not ask, which is the
    protection #363 added against one pathological line drowning a batched
    multi-op result.
    """
    _effective, refusal, note = _pattern_gate(pattern)
    if refusal:
        return refusal
    prefix = "" if via_payload else _pattern_read_as_note(pattern, path, "grep")
    return (prefix
            + note
            + _op_grep(pattern, path, limit, context, count_only,
                       no_exclude, no_auto_read, full))


def _grep_limit_and_label(limit: int) -> Tuple[int, str]:
    """Resolve grep's LIMIT into (effective cap, what the count line says).

    `all` (#1328) arrives as the GREP_LIMIT_ALL sentinel and leaves as a cap
    nothing can reach, plus the label `all`. Printing the number instead would
    put `limit 9223372036854775807` on the line a reader uses to decide whether
    a sweep was complete — a cap they then have to recognise to interpret.
    """
    if limit == GREP_LIMIT_ALL:
        return sys.maxsize, _GREP_ALL_TOKEN
    if limit <= 0:
        limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    return limit, str(limit)


def _has_outer_wrapped_unbounded_group(pattern: str) -> bool:
    """True if *pattern* has a `(...)` group followed by an outer `+`/`*`
    whose own content contains an unbounded quantifier (`+`/`*`) at ANY
    nesting depth (#1311 self-review finding).

    A single-level regex over `(...)` can only ever see the FIRST close-paren
    it reaches, so it cannot tell `((a+)?)+` -- where the real danger is the
    OUTER `+` wrapping a group whose own content is itself unbounded two
    levels down -- from `(a+)?` alone, which is safe. #1311's own fix
    narrowed a single-level regex from flagging every trailing `+`/`*`/`?` to
    only `+`/`*`, and that narrowing accidentally stopped catching
    `((a+)?)+` too: the old, over-eager regex only ever caught it as a side
    effect of blanket-refusing the INNER `(a+)?` fragment wherever it
    appeared, never because it understood the outer wrapping. Confirmed
    catastrophic: `((a+)?)+` against 25 `a`s and a non-matching trailer took
    several seconds on this machine.

    This is a small bracket-depth-aware scan instead: for every `(`, find
    its matching `)` by depth, note whether an unescaped `+`/`*` appeared
    ANYWHERE inside (at any nesting depth), and refuse only if the character
    immediately after that matching `)` is `+` or `*`.

    #2535 (gate-3 release-audit round 2): after resolving one group, the scan
    used to jump straight past it (`i = j`), so a nested group buried inside
    an UNQUANTIFIED outer group -- e.g. `(x(a+)+y)`, where the outer `( ... )`
    has no trailing `+`/`*` of its own but the inner `(a+)` does -- was never
    independently examined for its own trailing quantifier. The scan now
    advances one character at a time instead, so every `(` in the pattern,
    at any depth, gets its own matching-close-paren-plus-trailing-quantifier
    check, not only the outermost one at each position.

    Deliberately loose, like the check it replaces: character classes
    (`[...]`) are skipped so a literal `+`/`*`/`(`/`)` inside one is never
    mistaken for a quantifier or a grouping paren, and a backslash-escaped
    `(`/`)` is skipped as two characters so it never contributes to depth.
    Nothing here claims to be a real regex parser -- alternation, lookaround
    and non-capturing groups are not distinguished from a plain group, which
    only widens what gets refused, never what gets missed.
    """
    n = len(pattern)

    def _skip_class(k: int) -> int:
        """Index just past a `[...]` character class opened at *k* - 1."""
        if k < n and pattern[k] == "^":
            k += 1
        if k < n and pattern[k] == "]":
            k += 1
        while k < n and pattern[k] != "]":
            if pattern[k] == "\\":
                k += 1
            k += 1
        return k + 1

    i = 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _skip_class(i + 1)
            continue
        if c == "(":
            depth = 1
            j = i + 1
            has_unbounded = False
            while j < n and depth:
                cj = pattern[j]
                if cj == "\\":
                    j += 2
                    continue
                if cj == "[":
                    j = _skip_class(j + 1)
                    continue
                if cj == "(":
                    depth += 1
                elif cj == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                elif cj in "+*":
                    has_unbounded = True
                j += 1
            if has_unbounded and j < n and pattern[j] in "+*":
                return True
            i += 1
            continue
        i += 1
    return False


def _op_grep(pattern: str, path: str = ".", limit: int = 0,
             context: int = 0, count_only: bool = False,
             no_exclude: bool = False, no_auto_read: bool = False,
             full: bool = False) -> str:
    """Search pattern recursively. Auto-reads small single file on match.

    When context > 0, emits N lines before/after each match in grep -C style:
      match lines:   path:lineno:content  (colon separator)
      context lines: path-lineno-content  (dash separator)
    Non-adjacent groups are separated by --.
    Auto-read is skipped when context > 0 (output already contains context).

    When count_only=True, returns match counts per file instead of content.

    When no_auto_read=True, suppresses the single-small-file auto-read so only
    the matching line(s) are emitted (parity with glob's :no-auto-read flag).

    When full=True (#1712), the per-line char cap (#363) is suppressed for
    both matched and context lines in this call — see `_cap_grep_line` and
    `_grep_disclosure_note`.
    """
    unlimited = limit == GREP_LIMIT_ALL
    limit, limit_label = _grep_limit_and_label(limit)
    if not pattern:
        return "ERROR: empty pattern\n"

    # #150 ReDoS guards. Python's stdlib `re` has no execution timeout, so
    # we reject patterns that are obvious candidates for catastrophic
    # backtracking before they touch any file content.
    if len(pattern) > 1000:
        return f"ERROR: pattern too long ({len(pattern)} > 1000 chars)\n"
    # Nested unbounded quantifiers like `(a+)+`, `(a*)*`, `(.+)*` — the
    # classic ReDoS shape. The OUTER quantifier is what makes it dangerous:
    # `+`/`*` can re-partition the same input across an unbounded number of
    # iterations, each iteration free to re-decide how much of the group's
    # own unbounded content it consumed. `?` bounds the group to at most ONE
    # repetition, so there is no re-partitioning to explore and no
    # catastrophic-backtracking risk -- `(a+)?` was refused here until #1311
    # even though it is bounded, on a pattern reported live: digits, a dot,
    # an alternation, an OPTIONAL group of a dot plus one-or-more
    # lowercase/hyphen characters, a dot, "md". The exception is scoped to
    # each matched group's own outer quantifier, not to the pattern as a
    # whole: `(a+)?b(c+)?` is two independently-bounded groups, and neither
    # one's risk depends on what quantifier the other carries, so both must
    # pass. The check is intentionally loose otherwise; users with a
    # legitimate need can split into simpler greps.
    if _has_outer_wrapped_unbounded_group(pattern):
        return (
            "ERROR: pattern contains nested unbounded quantifiers "
            f"({pattern!r}) — would risk catastrophic backtracking. "
            "Rewrite without `(...+)+`-style nesting.\n"
        )

    # Auto-convert bash grep BRE alternation (\|) to Python regex (|). Single-
    # sourced with op_grep/op_around so the rewrite and the refusal that guards
    # it (#1120) can never drift apart — two hand-written copies is how `around`
    # came to carry the same saturation bug that was filed against `grep`.
    pattern, _ = _bre_alternation_rewrite(pattern)

    # Early exit if path doesn't exist (don't silently return 0 results)
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        # Could be a glob pattern — check if it expands to anything
        from glob import glob as _glob
        if not _glob(path, recursive=True):
            # #1711: a plain PATTERN/PATH swap has no ':' anywhere and both
            # values are ordinary tokens, so `_colon_split_hint` (dispatch,
            # ahead of this) declines — this is the general check that
            # still catches it, off the same evidence `around` uses.
            _swap = _swap_suggest(
                "grep", "PATTERN:PATH", "pattern", pattern, path,
                f"grep:{path}:{pattern}")
            return _path_not_found(path, op="grep", suggest=_swap,
                                   call_prefix=f"grep:{pattern}")

    excl = _get_exclude_paths("grep", no_exclude)

    # RTK delegation — basic grep (no context, no count). Excludes are threaded
    # through as --exclude-dir AND --exclude for single-segment entries (.git/,
    # node_modules/, .env/) and re-applied to whatever comes back. Multi-segment
    # prefixes (e.g. "Dvsi/dvsi-private/libs/") can't be expressed as either;
    # fall through to the native walker in that case.
    # `-E` and the _ERE_UNSAFE gate together (#987). rtk shells out to the
    # system grep, which without `-E` reads a POSIX *BRE*: `|`, `+`, `?`, `(`
    # and `{` are ordinary characters there. `cc|dd` therefore came back as the
    # one line containing a literal pipe — a smaller, confident, wrong answer,
    # and only when the BRE reading happened to match at all, which is why it
    # survived (a BRE that matches nothing exits non-zero and falls through to
    # the native walker, where the result is right). `-E` closes the gap for
    # alternation, groups and quantifiers; the gate declines delegation for the
    # constructs ERE still cannot express, rather than translating them.
    # `all` never delegates (#1328): the delegated report cannot count what it
    # did not collect, so its truncation clause is `total not counted` — the one
    # state a completeness sweep must not come back in. The native walker is the
    # only engine that can say `all` and mean it.
    if (not count_only and context == 0 and not unlimited
            and _rtk_enabled() and _has_rtk()
            and not _ERE_UNSAFE.search(pattern)):
        _, multi = _split_exclude_prefixes(excl)
        if not multi and not _gitignore_residual(path, excl):
            # limit + 1 so the report can tell "exactly N" from "stopped at N"
            # (#448). The extra line is trimmed off before output.
            rtk_args = ["grep", "-rn", "-E", "-m", str(limit + 1)]
            rtk_args.extend(_grep_exclude_flags(excl))
            rtk_args.extend([pattern, path])
            rtk_out = _rtk_run(rtk_args)
            if rtk_out is not None and rtk_out.strip():
                rtk_out, rtk_dropped = _rtk_drop_excluded(rtk_out, excl)
                if not rtk_dropped:
                    # The census is a *callable*, not a value: it is a second
                    # full grep over the tree and must not be paid for on a
                    # complete result, which needs no total (#1771).
                    return _rtk_grep_report(
                        rtk_out, limit,
                        census=lambda: _rtk_grep_census(pattern, path, excl))
                # An excluded file came back anyway — expected whenever the
                # list carries a negation, since those wildcards are withheld
                # from the argv. Printing the filtered lines under the
                # delegated header would leave its count, its `limit + 1`
                # truncation probe and its `?` denominator all describing a
                # result set that no longer exists. Redo the walk natively
                # instead: same filter, honest report, and the two engines
                # answer identically in the one case where it matters (#691).
            # No RTK output — rtk failed, or it ran and matched nothing. Fall
            # through to the native walker either way (#414). A zero result is
            # the ambiguous case #407 exists for, so it must reach the walker
            # and come back with a real scanned count rather than the `?` the
            # delegated report carries. This also lets the zero-hit literal
            # fallback below run.

    # Resolved once and threaded through so a zero-result report can state how
    # many files were actually scanned (#407) — without it, "0 results" is
    # ambiguous between "searched everything, found nothing" and "path/glob
    # resolved to nothing, so nothing was searched".
    hidden_files: List[str] = []
    candidates = _grep_candidates(path, excl, hidden_files)
    scanned = len(candidates)
    hidden = _hidden_suffix(len(hidden_files))

    if count_only:
        counts = _grep_count(pattern, path, limit, excl, candidates=candidates)
        literal_note = ""
        if not counts and _is_regexy(pattern):
            counts = _grep_count(re.escape(pattern), path, limit, excl, candidates=candidates)
            if counts:
                literal_note = _literal_note(pattern, sum(counts.values()))
        total = sum(counts.values())
        file_count = len(counts)
        out = [literal_note,
               f"({total} total matches across {file_count} files{_scanned_suffix(scanned)}{hidden})\n"]
        if total == 0:
            out.append(_shim_facade_note(path))
            # `_grep_recursive`, not `_grep_count`: the probe asks whether the
            # unwrapped pattern matches at all, and `_grep_count` has no early
            # break — it counts every line of every candidate regardless of the
            # limit it is handed, which would make a disclosure cost a second
            # full count of the corpus (PR review, #1435).
            out.append(_quote_pair_note(pattern, lambda inner: _grep_recursive(
                inner, path, 1, excl, candidates=candidates)))
            out.append(_case_insensitive_note(pattern, lambda p: _grep_recursive(
                p, path, 1, excl, candidates=candidates)))
        # `PATH:N` is the shape every grep-like tool uses for PATH:LINE, so a
        # count of 30 read as "one match, at line 30" — the opposite of what
        # the op said, in the op you call *before* deciding whether to look
        # (#988). The unit makes the two unconfusable.
        for fp, cnt in sorted(counts.items()):
            out.append(f"{_fwd(fp)}: {cnt} match{'' if cnt == 1 else 'es'}" + chr(10))
        out.append("\n")
        return "".join(out)

    if context > 0:
        ceiling = _grep_count_ceiling(limit)
        groups = _grep_recursive_context(
            pattern, path, ceiling + 1, context, excl, candidates=candidates)
        literal = False
        if not groups and _is_regexy(pattern):
            groups = _grep_recursive_context(
                re.escape(pattern), path, ceiling + 1, context, excl, candidates=candidates)
            literal = bool(groups)
        total, capped = _grep_total(
            sum(1 for g in groups for line in g if line[2] == "match"), ceiling)
        groups, truncated = _trim_context_groups(groups, limit)
        count = sum(
            1 for g in groups for line in g if line[2] == "match"
        )
        literal_note = _literal_note(pattern, count) if literal else ""
        file_count = len({g[0][0] for g in groups if g})
        out = [literal_note, f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
               f"limit {limit_label}, context {context}"
               f"{_truncation_suffix(truncated, total, capped)})\n"]
        if count == 0:
            out.append(_shim_facade_note(path))
            out.append(_quote_pair_note(
                pattern, lambda inner: _grep_recursive_context(
                    inner, path, 1, context, excl, candidates=candidates)))
            out.append(_case_insensitive_note(
                pattern, lambda p: _grep_recursive_context(
                    p, path, 1, context, excl, candidates=candidates)))
        current_file: str = ""
        first_group = True
        cut: List[Tuple[str, int]] = []
        for group in groups:
            group_file = group[0][0] if group else ""
            if group_file != current_file:
                current_file = group_file
                out.append(f"{current_file}\n")
                first_group = True  # reset separator for new file
            if not first_group:
                out.append("  --\n")
            first_group = False
            for _fp, lineno, kind, content in group:
                if len(content) > _grep_line_cap():
                    cut.append((_fp, lineno))
                capped = _cap_grep_line(content, full)
                if kind == "match":
                    out.append(f"  {lineno}:{capped}\n")
                else:
                    out.append(f"  {lineno}-{capped}\n")
        _note = _grep_full_note(cut) if full else _grep_cut_note(cut)
        if _note:
            out.insert(2, _note)
        out.append("\n")
        return _cap_context_window("".join(out), "grep_around")

    # ceiling + 1 (#448, #1073): a count that equals the limit is ambiguous
    # between "exactly N matches" and "stopped at N", and only looking past the
    # cap settles it. #448 looked exactly one past, which settled the yes/no and
    # left the scope unknown; the bound is the counting ceiling now, so the same
    # walk answers "how many" as well as "were there more".
    #
    # The walk itself is still already paid for — `candidates` above traversed
    # the whole tree to produce `scanned` — so this is never a second walk. What
    # it costs is reading file *contents* further: to the (limit+1)th match
    # before, to the (ceiling+1)th now. Measured on a 67,855-file tree, dense
    # pattern, limit 20: 0.0103s then, 0.05s now, against 10.3s for counting
    # everything and 4.3s for the traversal both of them sit on top of. On an
    # exact result neither stops early at all, which is what proving exactness
    # has always meant here.
    ceiling = _grep_count_ceiling(limit)
    hits = _grep_recursive(pattern, path, ceiling + 1, excl, candidates=candidates)
    literal = False
    if not hits and _is_regexy(pattern):
        hits = _grep_recursive(re.escape(pattern), path, ceiling + 1, excl, candidates=candidates)
        literal = bool(hits)
    truncated = len(hits) > limit
    total, capped = _grep_total(len(hits), ceiling)
    hits = hits[:limit]
    count = len(hits)
    literal_note = _literal_note(pattern, count) if literal else ""
    file_count = len({fp for fp, _, _ in hits})

    out = [literal_note,
           f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
           f"limit {limit_label}{_truncation_suffix(truncated, total, capped)})\n"]
    if count == 0:
        out.append(_shim_facade_note(path))
        out.append(_quote_pair_note(pattern, lambda inner: _grep_recursive(
            inner, path, 1, excl, candidates=candidates)))
        out.append(_case_insensitive_note(pattern, lambda p: _grep_recursive(
            p, path, 1, excl, candidates=candidates)))
    current_file = ""
    cut: List[Tuple[str, int]] = []
    for fp, lineno, content in hits:
        if fp != current_file:
            current_file = fp
            out.append(f"{fp}\n")
        if len(content) > _grep_line_cap():
            cut.append((fp, lineno))
        out.append(f"  {lineno}:{_cap_grep_line(content, full)}\n")
    _note = _grep_full_note(cut) if full else _grep_cut_note(cut)
    if _note:
        out.insert(2, _note)
    out.append("\n")

    # Auto-read: single small file + at least one match → emit full file.
    # Gated on BOTH byte size and line count (#362): a file under the byte cap
    # but with many lines still overshoots context, so cap both dimensions.
    if (not no_auto_read
            and count > 0
            and os.path.isfile(path)
            and os.path.getsize(path) < _get_op_int("read", "max_bytes", MAX_READ_BYTES)):
        line_cap = _get_op_int("read", "max_autoread_lines", MAX_AUTOREAD_LINES)
        if _count_lines(path, on_error=MAX_AUTOREAD_LINES + 1) > line_cap:
            out.append(f"[auto-read skipped: > {line_cap} lines — "
                       f"read:{path}:full to see it]\n")
        else:
            out.append(f"[auto-read: single file < {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes, "
                       "match found]\n")
            # The LIMIT here is this op's own default, never a bound anyone
            # typed — said out loud rather than left to be inferred from a
            # positive integer that looks identical either way (#1820).
            out.append(render_file(path, 0, _get_op_int("read", "max_lines", MAX_READ_LINES),
                                   limit_defaulted=True))

    return "".join(out)


_AROUND_DIR_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "vendor"}
_AROUND_DIR_MAX_FILES = 20


def _grep_line_cap() -> int:
    return _get_op_int("grep", "max_line_chars", MAX_GREP_LINE_CHARS)


def _cap_grep_line(content: str, full: bool = False) -> str:
    """Truncate one grep output line to a char budget (#363).

    Files with pathological single lines (minified JS, a 25KB one-line
    `@extends` PHPDoc) turn a single hit into a screenful. Cap the line and
    say how much was dropped so the reader knows to widen deliberately.
    Configurable via builtin-ops.grep.max_line_chars or
    SUPERTOOL_GREP_MAX_LINE_CHARS.

    `full=True` (#1712) returns `content` untouched — the default cap stays
    the protection for every call that does not ask, and this is the opt-out
    for the one call that does. The caller is still responsible for deciding
    whether the line WOULD have been cut (see `_grep_line_cap`), because that
    is what the disclosure note needs regardless of which shape ran.
    """
    if full:
        return content
    cap = _grep_line_cap()
    if len(content) <= cap:
        return content
    return f"{content[:cap]}… (+{len(content) - cap} chars)"


def _grep_cut_note(cut: List[Tuple[str, int]]) -> str:
    """Name the read that recovers a line `grep` cut (#1489).

    `… (+191 chars)` says a line was cut and nothing about how to get the
    rest, which is what a caller wants precisely when the cut line is the
    `edit` anchor they came for. `read:PATH:N-N` returns it byte-exactly —
    measured against raw bytes on files from 500 to 20000 chars with tabs,
    trailing spaces and unicode (#1489, PR #1576) — so the remedy exists and
    was simply not written down where the truncation happens.

    Above the body rather than under it, for #945's reason: a note that
    arrives after the output it would have replaced is a round-trip late.
    """
    if not cut:
        return ""
    fp, lineno = cut[0]
    plural = "" if len(cut) == 1 else "s"
    return (f"note: {len(cut)} line{plural} cut at {_grep_line_cap()} chars — "
            f"read:PATH:LINE-LINE returns a cut line byte-exactly, e.g. "
            f"read:{fp}:{lineno}-{lineno}\n")


def _grep_full_note(cut: List[Tuple[str, int]]) -> str:
    """Disclosure counterpart to `_grep_cut_note` when `full=True` (#1712).

    Nothing was cut here — `full` suppressed it — so a note built out of
    `_grep_cut_note`'s wording would claim a truncation that did not happen,
    the same "absence read as absence" shape CLAUDE.md's house defect names.
    `cut` still names every line that WOULD have exceeded the cap without
    `full`, because that is the fact worth surfacing: the caller asked for
    the whole line and this says whether asking changed anything.

    Empty `cut` returns "" rather than a note claiming full mode ran for
    nothing — when no line was long enough to matter, `full` genuinely had no
    effect, and staying silent about a non-event is the honest read, not the
    "reads as clean when it did nothing" trap: there is no separate clean
    state to disclose because default output would have looked identical.
    """
    if not cut:
        return ""
    plural = "" if len(cut) == 1 else "s"
    return (f"note: full: {len(cut)} line{plural} would exceed "
            f"{_grep_line_cap()} chars and {'was' if len(cut) == 1 else 'were'} "
            f"returned in full, uncut, because full was requested\n")


def _cap_context_window(text: str, op_name: str) -> str:
    """Cap a context-window op's output at a byte budget (#241).

    around:/grep_around: with a large :N on a file of long (e.g. minified)
    lines can emit hundreds of KB in one op, blowing the caller's context.
    Truncate at the last line boundary within the cap and append a footer
    that points at the narrower tools. Configurable via
    builtin-ops.<op>.max_bytes or SUPERTOOL_<OP>_MAX_BYTES.
    """
    cap = _get_op_int(op_name, "max_bytes", MAX_AROUND_BYTES)
    encoded = text.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= cap:
        return text
    clipped = encoded[:cap].decode("utf-8", errors="ignore")
    # Truncate at the last line boundary so we never cut mid-line. nl == -1
    # means a single line longer than the cap — nothing to trim to, pass the
    # partial through (the footer still flags it).
    nl = clipped.rfind("\n")
    if nl >= 0:
        clipped = clipped[:nl + 1]
    dropped = len(encoded) - len(clipped.encode("utf-8", errors="ignore"))
    return (clipped +
            f"… truncated (~{dropped} more bytes) — narrow context (:N) "
            f"or use between: for the whole symbol\n")


def _around_one_file(regex: "re.Pattern[str]", path: str, n: int) -> str:
    """Render the first match of regex in file at path with n lines context.

    Returns an empty string when the file has no match (caller filters these
    out so dir fan-out only shows hits).
    """
    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    lines = []
    for raw in raw_lines:
        try:
            lines.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            lines.append("<binary line>\n")

    match_lineno = None
    for i, line in enumerate(lines):
        if regex.search(line):
            match_lineno = i
            break

    if match_lineno is None:
        return ""

    total = len(lines)
    start = max(0, match_lineno - n)
    end = min(total, match_lineno + n + 1)

    out = [f"(match at line {match_lineno + 1}, showing lines {start + 1}–{end}, "
           f"{total} lines total)\n"]
    for i in range(start, end):
        marker = "→" if i == match_lineno else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    out.append("\n")
    return "".join(out)


def op_around(pattern: str, path: str, n: int = 10,
               via_payload: bool = False) -> str:
    """around, guarded and disclosed exactly as op_grep is (#1120, #1821).

    Split from the body for the same reason op_grep is: the refusal and the
    rewrite disclosure belong to every caller of the op, and leaving `around`
    with only the refusal reproduced the original asymmetry one level down —
    a rewrite that merely changes the pattern was still invisible here.

    "exactly as op_grep is" was written in #1120 and was not true until #1821:
    the BRE-rewrite disclosure travelled and the `:`-rejoin disclosure did not,
    so `around:mode:.*remind|title:DIR` rejoined the pattern the same way grep
    does, found two files, and named neither half of the split it had just
    guessed at. The PATH slot that does NOT resolve was already refused by
    `_colon_split_hint` in dispatch; this is the other branch, where the guess
    succeeds and the answer is the plausible wrong one.

    `via_payload` mirrors `op_grep`'s (#1825): the @file/@- route takes
    `pattern` verbatim under a `pattern` key, so there is nothing to
    disclose and the note's own remedy names the route already in use.
    """
    _effective, refusal, note = _pattern_gate(pattern)
    if refusal:
        return refusal
    prefix = "" if via_payload else _pattern_read_as_note(pattern, path, "around")
    return (prefix
            + note
            + _op_around(pattern, path, n))


def _op_around(pattern: str, path: str, n: int = 10) -> str:
    """Show N lines before and after the first match of PATTERN in PATH.

    PATH can be a file (first match in that file) or a directory (first
    match per file, skipping files with no match, capped at
    _AROUND_DIR_MAX_FILES). Hidden and heavy dirs (.git, node_modules,
    vendor, …) are skipped during dir walk.
    """
    if not pattern:
        return "ERROR: empty pattern\n"
    # The rewrite is applied here as well as in op_around so a direct call to
    # the private body still runs the pattern the caller meant; the helper is
    # idempotent, and the refusal/disclosure live in the public wrapper.
    pattern, _ = _bre_alternation_rewrite(pattern)
    if not path:
        return "ERROR: empty path\n"

    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if not os.path.isdir(path) and not os.path.isfile(path):
        # #734: `around` is PATTERN:PATH[:N] and its sibling `around_line`
        # is PATH:LINE[:N] — same op-name family, opposite argument order.
        # Swap them and PATH resolves to a line number, which is never a
        # real path but IS a plausible typo. `wrong CWD?` cannot be right
        # here (the cwd was fine), so name the actual mistake instead —
        # never redirect the call itself, only the advice.
        suggest = None
        # `pattern` becomes the PATH slot of the suggested `around_line:`
        # call, and this route reaches an unresolved `pattern` no colon-CLI
        # caller could: `around` was dropped from `_PATH_ARG_POSITIONS`
        # entirely (#1166 -- see `_around_line_delegation`'s own comment),
        # and the colon route's actual gate for this exact swapped-argument
        # shape is `_around_line_delegation`'s `_gate_paths([pattern])`,
        # which refuses `around:/etc/hosts:3` before `op_around` ever runs.
        # The `@payload`/`batch` route applies no containment to `pattern`
        # at all (it is a regex there, not a declared path), so it could
        # reach this branch with an out-of-bounds `pattern` and print a
        # suggestion the colon route's own gate refuses (#1146) -- the same
        # shape as the `git-checkout` hint in #850: never prescribe a
        # command the tool's own sibling rejects. `_containment_error`
        # mirrors the gate `_swap_suggest` (#1711) already applies to its
        # own candidate below.
        if _is_ascii_int(path) and _containment_error([pattern]) is None:
            suggest = (
                "`around` takes PATTERN:PATH[:N] — "
                f"'{path}' was read as the path. Did you mean: "
                f"around_line:{pattern}:{path}[:N]"
            )
        if suggest is None:
            # #1711: the int-path case above is around_line confusion, a
            # different mistake from a plain PATTERN/PATH swap — this is
            # the general case, reachable at any arity including the 3-arg
            # form the int check above cannot cover.
            suggest = _swap_suggest(
                "around", "PATTERN:PATH[:N]", "pattern", pattern, path,
                f"around:{path}:{pattern}[:N]")
        return _path_not_found(path, label="file", suggest=suggest,
                               op="around")

    def _render(rx: "re.Pattern[str]") -> Tuple[str, bool]:
        """Render the around-window for `rx`. Returns (output, matched) so the
        caller can decide whether to retry with an escaped literal pattern."""
        if os.path.isdir(path):
            hits: List[str] = []
            scanned = 0
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in _AROUND_DIR_SKIP]
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    fpath = os.path.join(root, name)
                    scanned += 1
                    rendered = _around_one_file(rx, fpath, n)
                    if rendered and rendered.startswith("ERROR:"):
                        continue
                    if not rendered:
                        continue
                    rel = _fwd(_safe_relpath(fpath, path))
                    hits.append(f"=== {rel} ===\n{rendered}")
                    if len(hits) >= _AROUND_DIR_MAX_FILES:
                        break
                if len(hits) >= _AROUND_DIR_MAX_FILES:
                    break
            if not hits:
                return (f"(no match for {pattern!r} in {path}, "
                        f"scanned {scanned} file(s))\n\n", False)
            header = f"(matched {len(hits)} file(s) under {path}"
            if len(hits) >= _AROUND_DIR_MAX_FILES:
                header += f", capped at {_AROUND_DIR_MAX_FILES}"
            header += f", scanned {scanned})\n"
            return _cap_context_window(header + "".join(hits), "around"), True

        rendered = _around_one_file(rx, path, n)
        if not rendered:
            return (f"(no match for {pattern!r} in {path})\n"
                    + _shim_facade_note(path) + "\n"), False
        return _cap_context_window(rendered, "around"), True

    out_text, matched = _render(regex)
    if not matched and _is_regexy(pattern):
        lit_text, lit_matched = _render(re.compile(re.escape(pattern)))
        if lit_matched:
            return _literal_note(pattern, lit_text.count("=== ") or 1) + lit_text
    if not matched:
        # Above the window, like `_literal_note` — a note explaining a zero is
        # read only if it precedes the thing it explains (#1435).
        quote_note = _quote_pair_note(
            pattern, lambda inner: _render(_compile_lenient(inner))[1])
        if quote_note:
            return quote_note + out_text
    return out_text


def op_between_symbol(symbol: str, path: str) -> str:
    """Return the body of a named function/method/class via tree-sitter.

    SYMBOL is matched against definition node names. First match wins; the
    info line reports total match count when the name is ambiguous.
    """
    if not symbol:
        return "ERROR: empty symbol\n"
    if not path:
        return "ERROR: empty path\n"
    if os.path.isdir(path):
        return (f"ERROR: between only works on single files, not "
                f"directories: {path}\n")
    if not os.path.isfile(path):
        # #1711: same swap check as `grep`/`around` — SYMBOL:PATH shares
        # the shape, and a swap here has no ':' to be caught by dispatch's
        # `_colon_split_hint` either.
        _swap = _swap_suggest(
            "between", "SYMBOL:PATH", "symbol", symbol, path,
            f"between:{path}:{symbol}")
        if _swap:
            return f"ERROR: file not found: {path}\n  {_swap}\n"
        return f"ERROR: file not found: {path}\n"

    if not _has_tree_sitter():
        return ("ERROR: between symbol mode requires tree-sitter "
                "(install tree-sitter-language-pack). "
                "Use 'between:re:START:END:PATH' for regex line slicing.\n")

    ext = os.path.splitext(path)[1].lower()
    lang_name = _TS_LANG_MAP.get(ext)
    if not lang_name:
        return (f"ERROR: tree-sitter does not support extension {ext!r}. "
                "Use 'between:re:START:END:PATH' for regex line slicing.\n")

    found = _ts_find_node(path, lang_name, symbol)
    if found is None and lang_name in _TS_GRAMMAR_FAILED:
        return (f"ERROR: tree-sitter grammar for {ext!r} failed to load "
                f"({_TS_GRAMMAR_FAILED[lang_name]}) - cannot search for "
                f"symbols. Use 'between:re:START:END:PATH' for regex line "
                f"slicing.\n")
    # Retry with modifiers/parens stripped so a signature pasted from source
    # resolves like the bare name would (#363). Exact match still wins.
    normalized = _normalize_symbol_query(symbol)
    if found is None and normalized != symbol:
        found = _ts_find_node(path, lang_name, normalized)
        if found is not None:
            symbol = normalized
    if found is None:
        extra = "" if normalized == symbol else f" (also tried {normalized!r})"
        return (f"ERROR: symbol {symbol!r} not found in {path}{extra}\n"
                + _shim_facade_note(path))
    node, kind, total = found

    start_line = node.start_point[0]
    end_line = node.end_point[0]

    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    total_lines = len(raw_lines)
    end_line = min(end_line, total_lines - 1)

    suffix = f", {total} matches (first shown)" if total > 1 else ""
    out = [f"({kind} {symbol!r}, lines {start_line + 1}–{end_line + 1}, "
           f"{end_line - start_line + 1} lines{suffix})\n"]
    for i in range(start_line, end_line + 1):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        marker = "→" if i == start_line else " "
        out.append(f"{i + 1:>6}{marker}{line}")
    out.append("\n")
    return "".join(out)


def op_between_pattern(start: str, end: str, path: str) -> str:
    """Return inclusive line slice from first line matching START to first
    subsequent line matching END (regex, language-agnostic).
    """
    if not start:
        return "ERROR: empty start pattern\n"
    if not end:
        return "ERROR: empty end pattern\n"
    if not path:
        return "ERROR: empty path\n"
    if os.path.isdir(path):
        return (f"ERROR: between only works on single files, not "
                f"directories: {path}\n")
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    start, start_refusal, start_note = _pattern_gate(start)
    if start_refusal:
        return start_refusal
    end, end_refusal, end_note = _pattern_gate(end)
    if end_refusal:
        return end_refusal
    # _pattern_gate can silently rewrite the caller's pattern (e.g. bash-grep
    # BRE alternation \| -> a plain |), the same disclosure op_grep/op_around
    # print above their result -- carried through every return below so a
    # rewritten start/end is never a silent difference from what was typed.
    gate_notes = start_note + end_note

    try:
        start_re = re.compile(start)
    except re.error:
        start_re = re.compile(re.escape(start))
    try:
        end_re = re.compile(end)
    except re.error:
        end_re = re.compile(re.escape(end))

    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return gate_notes + f"ERROR: could not read {path}: {e}\n"

    lines: List[str] = []
    for raw in raw_lines:
        try:
            lines.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            lines.append("<binary line>\n")

    start_idx: int | None = None
    for i, line in enumerate(lines):
        if start_re.search(line):
            start_idx = i
            break
    if start_idx is None:
        return (gate_notes
                + f"ERROR: start pattern {start!r} not matched in {path}\n"
                + _shim_facade_note(path))

    end_idx: int | None = None
    for i in range(start_idx + 1, len(lines)):
        if end_re.search(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        return (gate_notes
                + f"ERROR: end pattern {end!r} not matched after line "
                f"{start_idx + 1} in {path}\n")

    out = [gate_notes,
           f"(slice lines {start_idx + 1}–{end_idx + 1}, "
           f"{end_idx - start_idx + 1} lines)\n"]
    for i in range(start_idx, end_idx + 1):
        marker = "→" if i in (start_idx, end_idx) else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    out.append("\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rtk_grep_census(pattern: str, path: str,
                     exclude_paths: Tuple[str, ...]) -> Optional[Tuple[int, int]]:
    """`(total matches, files scanned)` for a delegated grep, or None (#1771).

    A second delegated pass — `grep -rc`, the same engine, the same argv minus
    the `-m` cap — because a truncated sweep with no total is the defect this
    whole file is otherwise careful about: a bounded presence read as a
    complete one. `grep` is the op a shipped `claude-jit-context` rule names as
    the replacement for the refused `Grep` tool, so every completeness sweep an
    agent runs lands here, and `(total not counted)` was the one state such a
    sweep must not come back in.

    **Why a count pass and not a re-walk.** #414 declined to compute the
    denominator because producing it meant walking the tree in Python, which is
    exactly what delegation exists to avoid. `-c` is not that walk: it is the
    same C engine over the same tree, and `-rc` names *every* file it scanned,
    zero-match ones included — so one call answers both the numerator and the
    denominator. Measured on this repo (1,159 files, dense pattern): 0.47s for
    the `-m`-capped match pass, 0.65s for the census. It is bought only when the
    match pass came back truncated, and `builtin-ops.grep.count_truncated: 0`
    declines it for a tree where that trade is wrong.

    **Excluded files are subtracted here, not left to the argv.** The default
    exclude list carries negations, and system grep cannot express one, so
    `_grep_exclude_flags` withholds every wildcard entry — a `server.pem` really
    is read by the census. Counting it would put matches in the total that the
    caller was told were not there, and a file in the denominator that nobody
    searched. `_is_excluded` is the same test the native walker applies.

    **Three states, and the third is the point.** Anything this cannot answer
    exactly returns None and the report says the total is unknown: rtk failing
    or timing out, a `-rc` output line that is not `path:N`, or a tree with no
    countable file left. A guessed total is worse than an admitted gap, because
    nothing downstream knows to distrust it.
    """
    if not _get_op_bool("grep", "count_truncated", True):
        return None
    # -H: with a single-file PATH, grep drops the `path:` prefix and the census
    # becomes unparseable — the one shape where declining would be an artefact
    # of the argv rather than of the tree.
    args = ["grep", "-rc", "-H", "-E"]
    args.extend(_grep_exclude_flags(exclude_paths))
    args.extend([pattern, path])
    out = _rtk_run(args)
    if out is None:
        return None
    cwd = os.getcwd()
    total = 0
    scanned = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        fpath, sep, num = line.rpartition(":")
        num = num.strip()
        # _is_ascii_int, not str.isdigit: `int()` happily converts U+0662 and
        # friends, so `isdigit` would turn a line the parser does not actually
        # understand into a confident number (#1727, #1748). Declining is the
        # third state this whole helper exists to protect.
        if not sep or not fpath or not _is_ascii_int(num):
            return None
        if exclude_paths and _is_excluded(_safe_relpath(fpath, cwd), exclude_paths):
            continue
        scanned += 1
        total += int(num)
    if scanned == 0:
        return None
    return total, scanned


def _rtk_grep_report(rtk_out: str, limit: int,
                     census: Optional[Callable[[], Optional[Tuple[int, int]]]] = None
                     ) -> str:
    """Wrap rtk's delegated grep output in supertool's report line (#414).

    rtk emits bare ``path:lineno:content`` lines and no report line at all, so
    a delegated grep — `grep:PATTERN:PATH`, the plainest invocation there is —
    silently dropped the result count, the limit disclosure, and #407's
    scanned-file denominator together.

    The body is passed through verbatim: re-rendering it into supertool's
    grouped layout would mean re-parsing content that may itself contain
    colons, for a cosmetic gain.

    op_grep asks rtk for `limit + 1` matches, so this sees one line past the
    cap whenever more exist (#448). The extra line is dropped and the report
    says so, which keeps the delegated path's completeness disclosure identical
    to the native walker's.

    **The `?` denominator, and where it stopped being acceptable (#1771).** On a
    *complete* delegated result the `?` is not load-bearing and stays: rtk exits
    non-zero when it matches nothing and op_grep falls through on empty output,
    so a delegated report always carries at least one result — and with no
    TRUNCATED marker, every match there is is on the screen, which is the only
    thing a breadth question needed the denominator for. The ambiguous case,
    zero results, always reaches the native walker and a real count.

    On a *truncated* result it was load-bearing and wrong in both halves at
    once: an unknown numerator over an unknown denominator. So `census` — a
    callable, invoked only on truncation, so the complete path keeps the single
    pass it has always had. When it answers, the report carries the exact total
    and a real `scanned N files`, in the native walker's own vocabulary. When it
    declines, `_truncation_suffix` names the total as unknown rather than
    filing it as an aside.

    A census that does not exceed the rows printed under it is refused rather
    than printed. The two passes are separate reads of a tree that can change
    between them, and a total at or below the visible row count is not a
    smaller answer, it is an incoherent one.
    """
    lines = [ln for ln in rtk_out.splitlines() if ln.strip()]
    truncated = len(lines) > limit
    lines = lines[:limit]
    files = set()
    for ln in lines:
        m = re.match(r"^(.+?):\d+:", ln)
        if m:
            files.add(m.group(1))
    total: Optional[int] = None
    reason: Optional[str] = None
    scanned_clause = ", scanned ? files"
    if truncated and census is not None:
        counted = census()
        if counted is None:
            pass
        elif counted[0] <= len(lines):
            # The census ran and was refused. Saying it "returned no total"
            # here would be the same class of wrong receipt this whole change
            # is about, one field over — so the two declines say which.
            reason = (f"the delegated count came back at {counted[0]}, "
                      f"at or below the {len(lines)} rows shown, and was "
                      "refused as incoherent")
        else:
            total, scanned = counted
            scanned_clause = _scanned_suffix(scanned)
    header = (f"({len(lines)} results in {len(files)} files"
              f"{scanned_clause} — delegated to rtk"
              f", limit {limit}"
              f"{_truncation_suffix(truncated, total, reason=reason)})\n")
    return header + "".join(ln + "\n" for ln in lines) + "\n"


def _truncation_suffix(truncated: bool, total: Optional[int] = None,
                       capped: bool = False,
                       reason: Optional[str] = None) -> str:
    """Report format's truncation disclosure (#448, #1073).

    `(1 results in 1 files, scanned 118353 files, limit 1)` reads as an
    exhaustive answer and is not one, which is how a coverage audit concluded a
    class had no test when the test was sitting one match past the cap. The
    marker is only ever emitted when a match past the limit was actually seen,
    so its absence is a positive statement: this count is exact.

    `more matches exist` said the answer was partial without saying how partial
    (#1073). 21 matches and 500 matches produced identical bytes and warrant
    opposite next actions, so the scope is now part of the marker — in three
    states that must not collapse into each other:

    * `N matches total`                  — counted, and this is all of them;
    * `N+ matches total (count capped…)` — counting stopped at the ceiling, so
      the number is a floor rather than a total;
    * `more matches exist, total unknown (…)` — nothing counted, and the
      reason. "We did not count" must not render as "we counted and there are
      some", and it must not render as an aside either (#1771): a caller told
      the answer is partial and not how partial has an unquantified answer, so
      the gap belongs in the sentence rather than in a trailing parenthesis.

    Only the delegated rtk path can reach the third state, and since #1771 it
    reaches it only when the `grep -rc` census could not answer. `reason` says
    which way, because "it never ran" and "it ran and I refused its answer" are
    two different things to do next, and one sentence for both would be a
    receipt misreporting its own mechanism — the defect class this marker was
    built to close, aimed at the marker. The default covers every path on which
    no total came back at all: rtk absent, failing or timing out; output that is
    not `path:N`; a non-ASCII decimal `int()` would have converted; or the pass
    switched off with `builtin-ops.grep.count_truncated: 0`. The caller passes
    its own when the census answered and was rejected.

    The delegated total is exact rather than capped when it arrives: `-c` counts
    in C, so there is no walker to stop early and nothing to clamp.
    """
    if not truncated:
        return ""
    if total is None:
        return (" — TRUNCATED, more matches exist, total unknown ("
                + (reason or "the delegated count pass returned no total")
                + ")")
    if capped:
        return (f" — TRUNCATED, {total}+ matches total "
                f"(count capped at {total})")
    return f" — TRUNCATED, {total} matches total"


def _trim_context_groups(
    groups: List[List[Tuple[str, int, str, str]]], limit: int
) -> Tuple[List[List[Tuple[str, int, str, str]]], bool]:
    """Cut context-mode groups back to `limit` matches; report whether any were cut.

    The over-fetched match may share a group with the last kept one (matches
    within 2*context+1 lines merge into a single window), so the cut has to
    happen inside the group rather than by dropping whole groups — dropping the
    group would take the limit-th match with it. A group left holding only
    context lines is dropped: context without its match is noise.
    """
    kept: List[List[Tuple[str, int, str, str]]] = []
    seen = 0
    truncated = False
    for group in groups:
        lines: List[Tuple[str, int, str, str]] = []
        stopped = False
        for entry in group:
            if entry[2] == "match":
                if seen >= limit:
                    stopped = True
                    break
                seen += 1
            lines.append(entry)
        if any(e[2] == "match" for e in lines):
            kept.append(lines)
        if stopped:
            truncated = True
            break
    return kept, truncated


def _hidden_suffix(hidden: int) -> str:
    """Report format's ", N files hidden by exclude-paths" clause (#691).

    An exclusion that leaves no trace in the output is indistinguishable from a
    file that was not there — the same silent-failure shape the `scanned N`
    denominator and the TRUNCATED marker were both added to close. Credential
    files are the reason the list exists, but "your search skipped something"
    is the caller's to know, and the way back (`no-exclude`) is one flag away.

    Which exclusions count is decided by `_is_disclosable_exclusion`, not by
    file-versus-directory: built-in noise entries stay out of the number so it
    reads zero on the ordinary call. A counter that is never zero is one a
    reader learns to skip, and then the call that fires because a real `.env`
    was hidden looks like all the others.
    """
    if hidden <= 0:
        return ""
    return f", {hidden} files hidden by exclude-paths"


def _grep_count_ceiling(limit: int) -> int:
    """How many matches grep will count before it stops counting (#1073).

    Never below the caller's own LIMIT: a ceiling under the limit would cap the
    total below the number of rows printed underneath it, which is a worse
    render than the one this replaces.
    """
    return max(_get_op_int("grep", "count_ceiling", MAX_GREP_COUNT_CEILING),
               limit)


def _grep_total(seen: int, ceiling: int) -> Tuple[int, bool]:
    """`(total, capped)` for a walk that stopped once it had `ceiling + 1`.

    `seen` may overshoot by more than one in context mode, where matches are
    collected a window at a time — so the reported floor is clamped rather than
    passed through, and a number printed as exact is always one that is.
    """
    return min(seen, ceiling), seen > ceiling


def _scanned_suffix(scanned: int) -> str:
    """Report format's ", scanned N files" clause (#407).

    A zero scanned count means the path/glob resolved to nothing, so the
    zero-result report above it must not read like a completed search.
    """
    if scanned == 0:
        return ", scanned 0 files — nothing matched the path/glob"
    return f", scanned {scanned} files"


def _grep_count(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Return match counts per file as {filepath: count}."""
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    counts: Dict[str, int] = {}
    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)

    for file_path in candidates:
        cnt = 0
        try:
            with open(file_path, "rb") as f:
                for raw in f:
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if regex.search(line):
                        cnt += 1
        except OSError:
            continue
        if cnt > 0:
            counts[file_path] = cnt
    return counts


def _grep_candidates(
    path: str, exclude_paths: Tuple[str, ...] = (),
    hidden: Optional[List[str]] = None,
) -> List[str]:
    """Return list of file paths to search for a given path argument.

    When exclude_paths is provided, directories whose path-relative-to-cwd
    starts with one of the prefixes are pruned at the walk boundary (dirs[:]
    mutation) so their subtrees are never opened. Gitignored directories are
    pruned at the same boundary (#449) — and because the pruning happens
    before the files are collected, the returned length is what op_grep reports
    as `scanned N`, so #407's denominator shrinks with the walk instead of
    counting agent worktrees six times over.

    **Files are filtered too** (#691). This loop used to test the extension and
    nothing else, so `.env` — on the default exclude list since #146 — was read
    and printed like any other file. Excluded files are appended to `hidden`
    when a list is passed, rather than merely counted: op_grep discloses how
    many there were, and anyone questioning that number needs the names.

    A `path` that IS an excluded file is still searched. Naming it is a
    deliberate act and `read` never gated it, so gating it here would buy
    nothing and break the case someone meant.
    """
    candidates: List[str] = []
    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        exts = _grep_file_includes()  # None = all files
        cwd = os.getcwd()
        ignored = _git_ignored_dirs(path) if exclude_paths else frozenset()
        for root, dirs, files in os.walk(path):
            rel_root = _safe_relpath(root, cwd) if exclude_paths else ""
            if exclude_paths:
                dirs[:] = [
                    d for d in dirs
                    if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                    and not _is_git_ignored(rel_root, d, ignored)
                ]
            for name in files:
                if exts is not None and not any(
                        name.endswith(ext.lstrip("*")) for ext in exts):
                    continue
                rel_name = os.path.join(rel_root, name)
                if exclude_paths and _is_excluded(rel_name, exclude_paths):
                    if hidden is not None and _is_disclosable_exclusion(
                            rel_name, exclude_paths):
                        hidden.append(os.path.join(root, name))
                    continue
                candidates.append(os.path.join(root, name))
    return candidates


def _grep_recursive(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> List[Tuple[str, int, str]]:
    """Return up to `limit` matches as (file_path, lineno, content) tuples.

    Filters by common code/doc extensions when walking directories.
    Always searches when `path` is a single file. file_path is normalised
    to forward slashes; the colon in a Windows drive letter (`C:/...`)
    would break a string-joined return shape on path:lineno:content split.
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        # Fall back to literal substring
        regex = re.compile(re.escape(pattern))

    results: List[Tuple[str, int, str]] = []
    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)

    for file_path in candidates:
        if len(results) >= limit:
            break
        try:
            with open(file_path, "rb") as f:
                for lineno, raw in enumerate(f, start=1):
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if regex.search(line):
                        results.append((_fwd(file_path), lineno, line.rstrip()))
                        if len(results) >= limit:
                            break
        except OSError:
            continue
    return results


def _grep_recursive_context(
    pattern: str, path: str, limit: int, context: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> List[List[Tuple[str, int, str, str]]]:
    """Return match groups with surrounding context lines.

    Each group is a list of (file_path, lineno, kind, content) tuples where
    kind is 'match' or 'context'. Groups represent adjacent/overlapping windows
    of lines. Non-adjacent groups are separated in output by --.

    Stops collecting new match groups once `limit` matches have been found.
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)
    groups: List[List[Tuple[str, int, str, str]]] = []
    match_count = 0

    for file_path in candidates:
        if match_count >= limit:
            break
        try:
            with open(file_path, "rb") as f:
                raw_lines = f.read().splitlines(keepends=True)
        except OSError:
            continue

        lines = []
        for raw in raw_lines:
            try:
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r"))
            except Exception:
                lines.append("<binary line>")

        # Collect match indices
        match_indices = [
            i for i, line in enumerate(lines) if regex.search(line)
        ]
        if not match_indices:
            continue

        # Merge overlapping windows into groups
        # A window is [match - context, match + context]
        windows: List[Tuple[int, int]] = []  # (start_idx, end_idx) inclusive
        for mi in match_indices:
            w_start = max(0, mi - context)
            w_end = min(len(lines) - 1, mi + context)
            if windows and w_start <= windows[-1][1] + 1:
                # Overlapping or adjacent — extend
                windows[-1] = (windows[-1][0], max(windows[-1][1], w_end))
            else:
                windows.append((w_start, w_end))

        # Build groups from windows
        match_set = set(match_indices)
        for w_start, w_end in windows:
            if match_count >= limit:
                break
            group: List[Tuple[str, int, str, str]] = []
            for i in range(w_start, w_end + 1):
                kind = "match" if i in match_set else "context"
                group.append((_fwd(file_path), i + 1, kind, lines[i]))
                if kind == "match":
                    match_count += 1
            groups.append(group)

    return groups
