"""_supertool_dispatch -- op dispatch, the per-call accumulators and the call log, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_dispatch")` from inside `_supertool.py`, at
the exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "dispatch", ...)` or
`monkeypatch.setattr(supertool, "log_call", ...)` (24 sites across 10 files
for `log_call`, 18 across 6 files for `dispatch`, measured on #2706) keeps
reaching the code it patches.

`_dispatch_impl` rebinds `_DEFER_FORMATTERS`, `_FORMAT_QUEUE`,
`_VALIDATOR_DEFER_QUEUE` and `_VALIDATOR_DEFER_SEEN` with `global` -- those
names are declared outside this part's span and read/rebound again in
`_supertool.py`'s own `main`. Because `_load_part` execs into the same
`globals()` dict every part and the core share, this is a non-issue
mechanically, exactly as for `_supertool_guard.py`'s own functions.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_dispatch` or `python3 _supertool_dispatch.py` gets this
module's own fresh globals(), which has no such name, and refuses with a
clear ImportError rather than failing later with a NameError on the first
name this file assumes `_supertool.py` already defined (os, re, time,
threading, List, Tuple, _NOT_CHECKED, _VALIDATED_FILES, ...).
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_dispatch.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

import threading as _threading
_DISPATCH_STATE = _threading.local()

# Per-op accumulators live on the dispatch frame, not in the process-global
# (#1109). `validate` is in `_PARALLEL_SAFE_OPS`, so under SUPERTOOL_PARALLEL
# six ops append to `_VALIDATED_FILES` / `_NOT_CHECKED` at once — and the footer
# used to be built by snapshotting `len()` at op entry and slicing `[before:]`
# at op exit. That arithmetic is per-op only while exactly one op is appending;
# with six in flight, every footer but one claimed files its op never opened,
# and `with findings` travelled the same way. A lock around the appends would
# have made them orderly and left the slices exactly as wrong — the defect is
# not a missing lock, it is per-op state kept somewhere per-op does not exist.
#
# Two scopes, deliberately, because two readers want two different answers:
#
#   * the FOOTER describes one op. It reads this frame's own list, which is
#     exact by construction — there is no snapshot left to get wrong.
#   * the EXIT CODE describes the whole call. `main` still reads the
#     process-global, so `$SUPERTOOL_REQUIRE_VALIDATORS` keeps firing under
#     parallel dispatch. Giving each op its own list and stopping there would
#     have traded a miscount for a gate that silently stopped gating, which is
#     the louder-bug-for-quieter-bug trade docs/validators.md warns about.
#
# `_acc_pop` is what joins them: every frame flushes into the frame that
# displaced it, and the outermost frame's parent is the process-global. A
# batch's sub-ops append one frame deeper and roll up into the batch's own
# footer, which is what they did before.
_ACC_FLUSH_LOCK = _threading.Lock()


def _acc_not_checked() -> List[str]:
    """This dispatch frame's not-checked names — the call's, outside one.

    The fallback is not decoration: `_drain_validator_queue` runs in `main`
    after every op has returned, with no frame installed, and what it records
    still belongs to the call's exit code.
    """
    buf = getattr(_DISPATCH_STATE, "acc_not_checked", None)
    return _NOT_CHECKED if buf is None else buf


def _acc_validated() -> List[Tuple[str, bool, bool]]:
    """This dispatch frame's validated-file rows — the call's, outside one."""
    buf = getattr(_DISPATCH_STATE, "acc_validated", None)
    return _VALIDATED_FILES if buf is None else buf


def _acc_push() -> Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]:
    """Install fresh per-op lists, returning the ones they displace."""
    prev = (getattr(_DISPATCH_STATE, "acc_not_checked", None),
            getattr(_DISPATCH_STATE, "acc_validated", None))
    _DISPATCH_STATE.acc_not_checked = []
    _DISPATCH_STATE.acc_validated = []
    return prev


def _acc_pop(prev: Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]) -> None:
    """Restore `prev` and flush this frame's rows into it, or into the globals.

    Called from `dispatch`'s `finally`, so an op that raised still hands its
    rows upward instead of stranding them on a thread a pool will reuse.
    """
    mine_not_checked = getattr(_DISPATCH_STATE, "acc_not_checked", None) or []
    mine_validated = getattr(_DISPATCH_STATE, "acc_validated", None) or []
    prev_not_checked, prev_validated = prev
    _DISPATCH_STATE.acc_not_checked = prev_not_checked
    _DISPATCH_STATE.acc_validated = prev_validated
    # Taken unconditionally, and only the outermost hand-off needs it: a parent
    # frame's list is thread-local and cannot be contended, while the
    # process-global is shared by every worker thread of a parallel dispatch,
    # and `list.extend` is not a promise the free-threaded build makes (3.13t+,
    # the same reason the depth counter above is thread-local). Skipping it on
    # the nested path would save an uncontended acquire — tens of nanoseconds,
    # once per frame — in exchange for a branch deciding whether this list is
    # the shared one, which is the kind of thing a later refactor gets wrong
    # silently. Cheap and unconditional beats clever and conditional here.
    with _ACC_FLUSH_LOCK:
        (_NOT_CHECKED if prev_not_checked is None
         else prev_not_checked).extend(mine_not_checked)
        (_VALIDATED_FILES if prev_validated is None
         else prev_validated).extend(mine_validated)


# The six mutation counters share one shape (#1116): `_dispatch_impl` used to
# snapshot the process-global at op entry and subtract at op exit, correct
# only while exactly one mutating op runs at a time — the identical failure
# #1109 fixed for `_NOT_CHECKED`/`_VALIDATED_FILES` above, left standing here
# because it was a different value and bundling it into #1109 would have
# widened a validators fix into the mutation path. It holds today only
# because every mutating op is excluded from `_PARALLEL_SAFE_OPS`, which is a
# reachability argument about a membership list kept for an unrelated reason,
# not a design one: nothing here says "I depend on that list", and nothing in
# that list says "adding a mutating op breaks these counters".
#
# Two scopes, same split as the lists beside it:
#
#   * the FOOTER describes one op. It reads this frame's own delta, which is
#     exact by construction.
#   * `main`'s exit-code checks (`$SUPERTOOL_REQUIRE_VALIDATORS`, the skip and
#     rollback per-call deltas) read the process-global unchanged — every
#     caller of these six IS genuinely call-wide there, so the global stays.
#
# `_CNT_FIELDS` is every reader that exists for the per-op frame value; a
# reader added later that needs the whole call reads the process-global list
# beside each field's definition instead, same as `main` already does.
_CNT_FIELDS = (
    "cnt_mutation", "cnt_write", "cnt_skip", "cnt_reapply", "cnt_rollback",
    "cnt_left_on_disk",
)


def _cnt_push() -> Tuple[int, ...]:
    """Install fresh per-op counter deltas, returning the ones they displace."""
    prev = tuple(getattr(_DISPATCH_STATE, f, 0) for f in _CNT_FIELDS)
    for f in _CNT_FIELDS:
        setattr(_DISPATCH_STATE, f, 0)
    return prev


def _cnt_pop(prev: Tuple[int, ...]) -> None:
    """Restore `prev`, folding this frame's own deltas up into it.

    No lock needed, unlike `_acc_pop`'s flush beside it: each frame's counters
    are its own thread-local ints, summed into a value only this call
    constructs — never a shared object another worker thread could be
    mutating at the same time.
    """
    for f, p in zip(_CNT_FIELDS, prev):
        setattr(_DISPATCH_STATE, f, p + getattr(_DISPATCH_STATE, f, 0))


def _cnt_frame(field: str) -> int:
    """This dispatch frame's own delta for *field* — never the process total."""
    return getattr(_DISPATCH_STATE, field, 0)


def _bump_counter(counter: List[int], field: str, by: int = 1) -> None:
    """Bump the process-global *counter* AND this frame's own delta by *by*.

    The single call site every increment (and the one decrement, in
    `_retract_write`) uses instead of touching `counter[0]` directly and the
    frame field by hand beside it — doing both separately at every call site
    is exactly how one of them drifts, which a self-review of this issue
    caught happening in `_retract_write` itself: it called this function for
    the rollback bump and then still hand-wrote its own two-line write-count
    decrement right beside it.

    `by < 0` floors each side independently at 0 rather than sharing one
    floor check, the same defensive shape `_retract_write`'s own decrement
    had: a global and a frame delta are two different counters, and a
    decrement that floors one while the other is already at 0 must not carry
    it negative regardless of which is which.
    """
    if by < 0:
        if counter[0] > 0:
            counter[0] += by
        if getattr(_DISPATCH_STATE, field, 0) > 0:
            setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)
        return
    counter[0] += by
    setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)


# Parsed at module scope, so a bad value here used to raise during *import* and
# take down every op in the tool, most of which have nothing to do with dispatch
# depth. The widest blast radius of the #654 class, for the smallest knob.
_DISPATCH_MAX_DEPTH = _env_int("SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1)


def dispatch(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:
    """Parse 'op:arg1:arg2:...' and route to the matching op function.

    *pre_parsed*, when given, is an already-structured (parts, replace_all)
    tuple — the same shape `_at_file_to_parts` produces from a JSON payload.
    Callers (batch sub-ops) pass it to bypass BOTH the `:::` re-tokenization
    and the shell-escape decoding, so content containing `:::` or backslashes
    survives verbatim — exactly as a standalone `edit:@file` call behaves.

    Traversal ops (grep, glob, tree, map) support an optional :::no-exclude
    suffix that bypasses all exclude-paths for that one call.
    Example: 'grep:pattern:vendor/:10:::no-exclude'

    Mutating ops (edit, replace, replace_lines, paste, vim) additionally
    accept an @file route:
    Example: 'edit:@.max/e1.json'  — reads {"path","old","new"} from file.
    Use '@-' to read JSON payload from stdin.

    The 'batch' op runs multiple ops from a JSON file:
    Example: 'batch:@.max/ops.json'
    Payload: array of {"op":"X",...fields} OR
             {"continue_on_error":true,"ops":[...]}

    A self-referencing batch (or any op chain) is bounded by
    SUPERTOOL_DISPATCH_MAX_DEPTH (default 32) — exceeding returns a clean
    ERROR string instead of a Python RecursionError. Depth counter is
    threading.local so concurrent calls from worker threads or free-
    threaded CPython (3.13t+) don't share/corrupt each other's count.
    """
    depth = getattr(_DISPATCH_STATE, "depth", 0)
    # Cleared by the OUTERMOST frame only: a batch sub-op that refuses has to
    # be able to set a flag its parent still carries when it returns.
    if depth == 0:
        _DISPATCH_STATE.call_failed = False
    if depth >= _DISPATCH_MAX_DEPTH:
        _mark_op_failure()
        return (
            f"ERROR: dispatch recursion limit ({_DISPATCH_MAX_DEPTH}) exceeded "
            f"— check for a self-referencing batch payload\n"
        )
    _DISPATCH_STATE.depth = depth + 1
    _acc_prev = _acc_push()
    _cnt_prev = _cnt_push()
    try:
        out = _dispatch_impl(arg, pre_parsed)
        # The edit ops read with surrogateescape and echo the buffer in their
        # receipts, so a receipt can hold lone surrogates that no UTF-8 stream
        # can encode. Sanitised once, at the outermost frame, because every
        # consumer (CLI stdout, the MCP server, a batch sub-op's caller) hits
        # the same wall (#1059).
        return _display_safe(out) if depth == 0 else out
    finally:
        _DISPATCH_STATE.depth = depth
        _acc_pop(_acc_prev)
        _cnt_pop(_cnt_prev)
        # An op outside the read-only set may have moved the index without
        # moving any file's mtime — `git-commit` is the everyday case — so the
        # repo-wide status snapshot cannot speak for the next op (#1126).
        # Keyed off the same predicate as parallel dispatch because it asks the
        # same question, and an unrecognised or custom op is unsafe by default.
        if not _is_parallel_safe(arg):
            _path_meta_bulk_drop()
        # _FORMATTER_SKIPS is module-level and drained on the normal return
        # path. An exception escaping _dispatch_impl skips that drain, and the
        # next top-level call would report skips belonging to a call that
        # already died. The outermost frame owns the reset either way.
        if depth == 0:
            _FORMATTER_SKIPS.clear()


def dispatch_verdict(
    arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None
) -> "Tuple[str, bool]":
    """`dispatch`, plus the structural answer to "did this call refuse".

    The verdict is set by whichever frame produced the refusal — this one, or
    a batch sub-op nested under it — and read back off the same thread. It is
    never re-derived from the string returned here (#1291).
    """
    # Establish the flag before reading it, rather than inheriting one.
    #
    # `_call_failed()` is a pure read of a thread-local, and the only clear
    # lived inside `dispatch` at depth 0 — a function the two lines below
    # record as one callers REPLACE. When they do, the clear never runs and
    # this returns whatever last refused on this thread: `master` went red on
    # macOS only at df34db5, and the batch tally said "all 2 refused" in words
    # about two ops that had not (#1359).
    #
    # Before the call, never after: a sub-op refusing at any depth must still
    # reach the parent's verdict, which is the whole reason only depth 0
    # clears it inside `dispatch`. That arrangement is unchanged — this adds
    # the same reset one level out, where the reader of the bit lives.
    #
    # The sibling counters in `_main` — `_SKIP_COUNT`, `_ROLLBACK_COUNT`,
    # `_NOT_CHECKED` — are all read as per-call deltas for exactly this
    # reason (#680). This bit was the one member of the group with neither.
    _DISPATCH_STATE.call_failed = False

    # One positional argument when there is nothing else to pass, because
    # `main` has always called `dispatch(arg)` and both the tests and the MCP
    # layer monkeypatch it with that arity. Widening the call here would have
    # been a compatibility break bought for nothing.
    out = dispatch(arg) if pre_parsed is None else dispatch(arg, pre_parsed)
    return out, _call_failed()


def _depth1_call_footer(op: str, body: str) -> str:
    """`[result]`/`[branch: X]` footer for a depth<=1 call (#381, #990).

    Factored out of the tail of `_dispatch_impl` so the `op:@-` payload route
    can carry it too (#1158): that route used to `return` its body directly,
    before dispatch ever reached this block, so `validate:@-` silently
    dropped the summary line `validate:PATH` prints for the identical file
    and identical config.

    The six mutation counters used to arrive as `*_before` snapshots the
    caller took at op entry, subtracted here against the process-global at
    exit — correct only while exactly one mutating op runs at a time (#1116,
    the same failure #1109 fixed for the two lists beside it). They are read
    straight off this dispatch frame's own delta instead: `dispatch` installed
    it before `_dispatch_impl` ran, and every nested sub-op (a batch's own
    children) has already folded its share up into this frame by the time
    a depth-1 caller gets here, the same way the two lists do.
    """
    if getattr(_DISPATCH_STATE, "depth", 1) > 1:
        return body
    # This frame's own rows (#1109), not a slice of the process-global.
    # `dispatch` installed them before `_dispatch_impl` ran, so the lists
    # are always present here; `or ()` declines to fall back to the global,
    # because a footer built from every op's rows is the defect, not a
    # degraded reading of it.
    _not_checked_slice = list(
        getattr(_DISPATCH_STATE, "acc_not_checked", None) or ())
    _validated_slice = list(
        getattr(_DISPATCH_STATE, "acc_validated", None) or ())
    _mutation_delta = _cnt_frame("cnt_mutation")
    if not (op in _OP_TARGETS or _mutation_delta
            or _not_checked_slice or _validated_slice):
        return body
    _result = _result_line(_mutation_delta,
                           _cnt_frame("cnt_write"),
                           _cnt_frame("cnt_skip"),
                           _cnt_frame("cnt_reapply"),
                           _not_checked_slice,
                           _cnt_frame("cnt_rollback"),
                           _validated_slice,
                           _cnt_frame("cnt_left_on_disk"))
    # A batch says its count twice, and the leading copy is the load-bearing
    # one. The footer is separated from the per-op results by a validators
    # block long enough that `tail` lands on `git-status : ok` and reads as
    # success -- the exact half of #984 that #1018 marked `Part of` and did
    # not build. Only `batch`: a single op's receipt is three lines with the
    # footer already adjacent, and a duplicate there is noise rather than a
    # signal.
    if op == "batch":
        body = _result + body
    body += _result
    body += _branch_line()
    return body


def _dispatch_impl(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:
    """Body of dispatch — separated so the recursion guard stays minimal."""
    # Strip :::no-exclude before splitting so it doesn't interfere with arg parsing
    no_exclude = arg.endswith(_NO_EXCLUDE_SUFFIX)
    if no_exclude:
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]

    # `_flat_field`, not the raw arg (#1019). An op string carries paths, and a
    # path is whatever the filesystem accepted — `validate:` walks a tree, a
    # batch payload supplies one, and a repo that takes contributed fixtures is
    # named by strangers. `str.splitlines()` breaks on ten separators (#886), so
    # `a<U+2028>[result] 1 op run, 0 writes` wrote a second, forged marker line
    # at column 0 above every genuine one. Not `_flat_cell`: that is the *row*
    # variant, which strips and bounds; a header names what was asked for and
    # must not silently drop a trailing space or truncate a long path.
    header = (f"--- {_flat_field(arg, disclose_newline=True)}"
              f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")

    # `op:::FIELD:::FIELD:::...` — triple-colon mode for write ops with
    # arbitrary `:` in content. Only triggers when the op name is followed
    # immediately by `:::`. Existing `:::no-exclude` (suffix, stripped above)
    # and `read:PATH:::grep=` (mid-arg) keep working under single-colon parsing.
    _at_file_replace_all: bool = False
    _at_file_used: bool = False
    # How this call's fields were separated, for the ops that have to know:
    # ':::', ':', or '' when nothing was tokenized at all because the fields
    # arrived structured. Three states rather than two — a payload's fields
    # were never split, and a refusal that says they were is a claim about a
    # parse that did not run (#946).
    _arg_sep: str = ""
    if pre_parsed is not None:
        # Batch sub-op: parts already structured from a JSON payload (via
        # _at_file_to_parts). Skip ALL string tokenization and reuse the
        # @file semantics — literal bytes, no `:::` split, no escape decode.
        # This is what routes `:::`-containing content through unharmed.
        parts, _at_file_replace_all = pre_parsed
        _at_file_used = True
        op = parts[0] if parts else ""
    else:
        # `op:::FIELD:::FIELD:::...` — triple-colon mode for write ops with
        # arbitrary `:` in content. Only triggers when the op name is followed
        # immediately by `:::`. Existing `:::no-exclude` (suffix, stripped above)
        # and `read:PATH:::grep=` (mid-arg) keep working under single-colon parsing.
        import re as _re
        triple_match = _re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):::", arg)
        if triple_match:
            parts = arg.split(":::")
            _arg_sep = ":::"
        else:
            parts = _split_arg(arg)
            _arg_sep = ":"
        op = parts[0] if parts else ""

    # Read-only gate (#1787), the earliest point `op` is known on every path
    # -- built-in, preset/project, and every batch sub-op that recurses back
    # through this same function (with one exception: a batch sub-op whose
    # own name is in `_READ_OP_AT_FIELDS` is dispatched straight to the op
    # function and never re-enters here -- inert today because every name in
    # that set is `read-only`, pinned by
    # tests/test_read_only_declared_1787.py's own census). Fires before any
    # argument shape is even looked at (a mixed-tree pair, an @-payload
    # route, stdin) because none of that matters once the answer is "do not
    # run this op at all": there is no more-specific refusal for this call to
    # lose to, unlike the mixed-tree gate below which deliberately waits
    # until immediately before stdin is touched.
    #
    # Gated on `_op_is_recognized(op)`, not on `op` alone -- self-review
    # caught this (#1787): an unrecognised/typo'd name is not `read-only`
    # either, so a naive gate declined it as a manufactured `!`-class op with
    # a remedy ("unset SUPERTOOL_READ_ONLY") that cannot fix a name that does
    # not exist. `_op_gated_by_mixed_tree_write_check` two dozen lines below
    # carries the identical carve-out for the identical reason (#1878): an
    # unrecognised name must fall through to "unknown operation" unchanged.
    if _read_only_declared() and _op_is_recognized(op):
        _read_only_cls = _op_safety_class(op)
        if _read_only_cls != "read-only":
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _read_only_decline(op, _read_only_cls))

    def _op_gated_by_mixed_tree_write_check() -> bool:
        """True for the same two classes `_resolve_custom_op`'s own #678
        check gates downstream: a write-class builtin (`_OP_SAFETY_BUILTIN`),
        or any op this project's config actually declares under "ops". An
        unrecognized op name must NOT match, so it still falls through to
        "unknown operation" rather than being swallowed as a mixed-tree
        decline it was never going to earn (#1878).
        """
        if _OP_SAFETY_BUILTIN.get(op) == "writes":
            return True
        _ops_cfg = _load_config().get("ops")
        return isinstance(_ops_cfg, dict) and op in _ops_cfg

    # Read-op @payload route — 'grep:@file' / 'around:@-' etc. (#625).
    #
    # Gated on the reference actually resolving ('@-', or an existing file)
    # rather than on the leading '@' alone: `grep:@Override:src/` is a real and
    # common search, and must keep meaning what it always meant. A pattern that
    # merely starts with '@' therefore falls through untouched — only a genuine
    # payload reference is intercepted.
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and op in _READ_OP_AT_FIELDS
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))
            # Resolvable only under the moved-to root: still a payload reference,
            # so route it in and let _load_at_file explain which root was searched
            # rather than falling through to a bare "file not found: @…" (#672).
            or os.path.isfile(parts[1][1:])
            # Resolvable under neither root, but payload-shaped: a lone `@….toml`
            # / `@….json` argument. Routing it in only changes which error is
            # printed — an unresolvable reference reads no file either way — and
            # it is the case that most needs the two roots named. Extension-gated
            # so `grep:@Override:src/` keeps falling through as the search it is.
            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.toml or {op}:@-). Put fields in the "
                f"payload, not on the colon CLI.\n"
            ))
        try:
            _read_payload = _load_at_file(parts[1], note=False)
        except ValueError as _e:
            _mark_op_failure()
            return header + _take_payload_warnings() + f"ERROR: {_e}\n"
        # The warnings lead the body, so the verdict is taken from the op's
        # own answer rather than from whatever ends up first on the line.
        _read_warnings = _take_payload_warnings()
        # No `*_before` snapshot needed here any more (#1116): nothing in
        # `_READ_OP_AT_FIELDS` mutates, and `_depth1_call_footer` now reads
        # this dispatch frame's own counter delta directly rather than a
        # subtraction against a point this branch used to have to capture --
        # without which `validate:@-` printed neither `[result]` nor
        # `[branch: X]`, the two lines `validate:PATH` ends on, because this
        # branch returned before dispatch ever reached the footer block.
        _read_body = _read_op_from_payload(
            op, _read_payload, no_exclude=no_exclude)
        if _op_body_failed(_read_body):
            _mark_op_failure()
        _read_body = _depth1_call_footer(op, _read_body)
        return header + _read_warnings + _read_body

    # @file route — 'op:@path' or 'op:@-' (stdin).
    # Load JSON, rebuild parts list, then fall through to the normal handlers.
    # Applies to mutating ops that have ':::' fields in their syntax string,
    # OR (#1165) to a preset op with no named-field registry at all, via the
    # generic `args`-list route below -- the escape hatch for a preset whose
    # colon syntax is mode-based rather than a flat field list
    # (`gh-job:ID:grep:PATTERN`), which could never earn a `:::` entry in
    # `_AT_FILE_REGISTRY` no matter how its syntax string were written.
    _at_file_named_fields = _at_file_fields(op)
    # Gated on the reference actually resolving, mirroring the read-op
    # @payload route's own gate a few dozen lines up -- NOT on the leading
    # '@' alone. A named-field op (`edit`, `git-commit`, ...) never has a
    # legitimate single-token literal call to begin with (its colon form
    # always needs multiple ':::' fields), so intercepting on '@' alone
    # never collided with one. A preset op can: `gh-mentions:@octocat` is a
    # plausible real call whose first argument is a literal string that
    # happens to start with '@' (self-review caught this -- unguarded, this
    # route silently turned that into a "file not found" refusal where the
    # call used to just run, the same class of regression the read-op gate
    # was written to avoid for `grep:@Override:src/`).
    # Excludes any op whose own `repo_target` mode already starts with
    # "payload" (#2408): `gh-issue-create`, `gh-issue-comment`,
    # `gh-pr-create`, `gh-pr-edit` and `gl-issue-create` all take an
    # `@FILE`/`@-` payload today, but it is a from-scratch named-field
    # payload their own preset script parses and validates (title, body,
    # labels, ...) -- not the `args`-list shape this generic route
    # produces. None of these five ever earned a `:::` entry in
    # `_AT_FILE_REGISTRY` (their syntax strings are `op:@FILE | op:@-`,
    # with no field list to derive one from), so before this exclusion
    # `_at_file_named_fields` was empty for all five and three of them --
    # `gh-issue-create`, `gh-pr-create` and `gl-issue-create`, the ones
    # called as bare `op:@FILE`/`op:@-` with nothing ahead of the
    # payload -- fell straight into the generic `args`-only route added
    # by #1165 for `gh-job`-shaped ops that have no named-field
    # convention at all, refusing every real call with "unknown field(s)
    # title -- accepted: args" (dc431bc9, #2403). The other two,
    # `gh-issue-comment:ID:@FILE` and `gh-pr-edit:ID:@FILE`, were never
    # actually reachable by this route regardless of this bug: the route
    # gates on `parts[1].startswith("@")` a few lines below, and for
    # these two `parts[1]` is the issue/PR number, not the payload
    # reference, so the generic route's own precondition never held for
    # them (verified directly against dc431bc9 unmodified, not assumed
    # from the issue text, which had named `gh-pr-edit` as a fourth
    # broken op -- it was not). They are excluded here anyway, on the
    # same `repo_target` signal, so a later change to either op's
    # calling convention cannot silently fall into the generic route.
    # `repo_target` starting with "payload" is this codebase's own
    # existing signal for "takes its fields from its own payload dict,
    # parsed by the preset script" and is what distinguishes these five
    # from a genuine `args`-only op like `gh-job`, whose `repo_target`
    # is `True` ("op" mode).
    _generic_preset_route = (
        not _at_file_named_fields
        and _op_is_preset_op(op)
        and not _repo_target_modes().get(op, "").startswith("payload")
        and len(parts) >= 2
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))
            or os.path.isfile(parts[1][1:])
            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    )
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and (_at_file_named_fields or _generic_preset_route)
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.json or {op}:@-). Put fields in the "
                f"JSON/TOML payload, not on the colon CLI."
                + _at_file_payload_hint(op) + chr(10)
            ))
        # #1878 -- gated to fire ONLY here, immediately before `_load_at_file`
        # can drain stdin, rather than unconditionally at the top of the
        # function. A first version of this fix ran the same check the
        # instant `op` was known, ahead of `_gate_paths`/the extra-colon-
        # token refusal too -- which changed which refusal wins for a
        # MIXED-TREE call that ALSO uses the plain colon-CLI (no payload at
        # all): `edit:::old:::new:::/etc/passwd` under a mix used to answer
        # `ERROR: path escapes cwd` and started answering the generic
        # mixed-tree `SKIPPED` instead, silently -- a more specific,
        # actionable refusal replaced by a less specific one for a call that
        # was never going to touch stdin. Caught in self-review, not by any
        # test (#1878's own repro is `@-`/`@file` only). Scoping the early
        # exit to "about to call `_load_at_file`" -- after the `len(parts) >
        # 2` refusal above, which never touches stdin either -- restores the
        # original precedence for every refusal that does not need the
        # payload, and still declines before stdin is touched for the one
        # that does.
        if _op_gated_by_mixed_tree_write_check():
            _mixed_early = _mixed_tree_pair()
            if _mixed_early is not None and not _mixed_tree_allowed():
                _bump_counter(_SKIP_COUNT, "cnt_skip")
                return _receipt(header, _mixed_tree_decline(op, _mixed_early))
        try:
            payload = _load_at_file(parts[1])
            if _at_file_named_fields:
                parts, _at_file_replace_all = _at_file_to_parts(op, payload)
            else:
                parts, _at_file_replace_all = _at_file_to_parts_generic(
                    op, payload)
            _at_file_used = True
            # The colon prefix got us here; the FIELDS came from the payload
            # and no separator touched them.
            _arg_sep = ""
        except ValueError as _e:
            # A payload that would not load, or one that loaded without the
            # fields the op needs. Both leave the caller knowing their call was
            # wrong and not what a right one looks like — #1003 for the whole
            # reasoning, and for the commit message that was mangled instead.
            # The payload warnings drain first (#1027): a doubled backslash is
            # a plausible cause of the very failure being reported, so it
            # belongs above the error rather than after the remedy.
            return (header + _take_payload_warnings() + f"ERROR: {_e}"
                    + _at_file_payload_hint(op) + chr(10))

    # When parts come from @file (JSON/TOML payload), they hold literal
    # bytes — backslashes and newlines must NOT be reinterpreted as shell-
    # style escapes. Only colon-CLI input needs `_decode_escapes`.
    _dec = (lambda s: s) if _at_file_used else _decode_escapes

    # `@-` reached a VALUE field rather than the reference slot (#1776). Ahead
    # of every handler, because the whole point is that no write happens: this
    # used to fall through and be written to disk as content.
    if not _at_file_used:
        _stdin_ref = _stdin_ref_in_value_field(op, parts)
        if _stdin_ref:
            return _receipt(header, _stdin_ref)

    # Published for the preset subprocess launcher, which is several frames
    # down and receives only `parts`. Set on every frame rather than once per
    # call: a batch sub-op arrives through its own frame with its own route.
    _ARG_SEP[0] = _arg_sep

    # A content-heavy mutating op echoes its old and new strings in the header
    # and then again in the diff underneath. Rebuild the header from the parsed
    # fields once the arguments are long enough for that to cost real tokens —
    # the diff below is the useful part and already shows what changed (#384).
    # A batch sub-op arrives with `arg` joined from its parts, which is exactly
    # the case the issue was filed about.
    #
    # Deferred, not applied here: on FAILURE no diff renders, and the verbatim
    # header is then the only surviving copy of what the caller sent. Eliding
    # it would take the reproduction material away at the one moment it is
    # needed. So the compact form is computed now, while `parts` is in hand,
    # and swapped in at the end only if the op succeeded.
    #
    # Not for a payload-sourced sub-op (`pre_parsed`): its `arg` is already the
    # honest `op:@payload → target` header the batch loop synthesized, and
    # eliding THAT would replace a truthful line with a summary of fields the
    # caller never typed on a colon CLI. The swap below is also gated on the op
    # having written, which for a payload op meant a FAILING one fell back to
    # the flattened lie — at the one moment a reader is reconstructing what
    # happened. #644.
    _compact_header = ""
    if pre_parsed is None and len(arg) > _HEADER_ARG_MAX:
        _compact_header = _compact_header_arg(op, parts, _arg_sep)
    # None until a custom op runs in this frame; then its exit status. Read
    # rather than sniffed off the receipt, for the reason stated at the swap
    # below — a preset writes no file, so `_WRITE_COUNT` cannot speak for it.
    _custom_op_ok: Optional[bool] = None

    # Every non-empty token past the last slot the op reads is refused, not
    # dropped (#1582, #1345). Ahead of the containment gate and of every op
    # branch, because a call carrying an argument nobody read is not a call
    # that should reach the filesystem at all — and because the drop is at its
    # worst on the ops that succeed anyway.
    #
    # `pre_parsed` is exempt: a payload's fields were never tokenized, and the
    # batch loader already refuses an unknown field by name. Asking a slot
    # question about a parse that did not run is #946's defect.
    if pre_parsed is None:
        _extra_toks = _extra_colon_tokens(op, parts)
        if _extra_toks:
            return _receipt(header, _extra_colon_tokens_refusal(
                op, _extra_toks, _extra_token_remedy(op, parts, _extra_toks)))

    # The table is `_PATH_ARG_POSITIONS`, at module scope. It was a local
    # literal here until #1285 — which is exactly why nothing noticed it naming
    # `blame`, an op the dispatcher had stopped accepting three months earlier.
    _path_slots = [_pos for _pos in _PATH_ARG_POSITIONS.get(op, ())
                   if _pos < len(parts)]
    _containment, _gated = _gate_paths(parts[_pos] for _pos in _path_slots)
    if _containment:
        return _receipt(header, _containment)
    # Write the gate's own expansion back into the slot the op will read, so
    # the string opened is the string checked (#1300). `parts` may be the
    # caller's list on the `pre_parsed` route, so copy before mutating.
    if any(_gated[_i] != parts[_pos] for _i, _pos in enumerate(_path_slots)):
        parts = list(parts)
        for _i, _pos in enumerate(_path_slots):
            parts[_pos] = _gated[_i]

    # #1942 -- #678's guard declined a preset/custom op outright under a
    # mixed core/tree pair, but a built-in WRITE op only got the stderr
    # warning `main()` prints once and kept running to completion: the
    # write itself always lands on the right file (`_safe_path` resolves
    # against `os.getcwd()` regardless of which core answered), but the
    # CODE that answered -- validators, formatters, hooks -- was the other
    # tree's, and the receipt read exactly like a correct one. Gate the
    # same class `_OP_SAFETY_BUILTIN` already names as "writes", at the
    # same chokepoint every path argument already passes through, rather
    # than adding a second list that can drift from the first (#1285's
    # own lesson, about this exact table).
    #
    # #1878 added an EARLIER instance of this same check, gated to fire only
    # when a payload route is about to drain stdin (see the `@file route`
    # block above) -- so a call that reaches here already survived that one
    # (or never triggered it, having no payload). This one still has to run:
    # it is what declines a plain colon-CLI write-class call
    # (`edit:::old:::new:::path`, no `@-`/`@file` at all) under a mixed
    # tree, and moving it earlier unconditionally was tried and reverted in
    # self-review -- it silently replaced a more specific `_gate_paths` /
    # extra-colon-token refusal with this generic one for a call that was
    # never going to touch stdin either way.
    if _op_gated_by_mixed_tree_write_check():
        _mixed = _mixed_tree_pair()
        if _mixed is not None and not _mixed_tree_allowed():
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _mixed_tree_decline(op, _mixed))

    try:
        if op == "read":
            path = parts[1] if len(parts) > 1 else ""
            offset = 0
            limit = 0
            force_full = False
            range_form = False
            if len(parts) > 2 and parts[2]:
                if parts[2] in ("full", "raw"):
                    force_full = True
                elif _READ_RANGE_RE.fullmatch(parts[2]):
                    r_start, r_end = (int(x) for x in parts[2].split("-"))
                    if r_start < 1:
                        return _receipt(
                            header, "ERROR: read range START must be >= 1\n")
                    if r_end < r_start:
                        return _receipt(header, (
                            f"ERROR: read range END ({r_end}) is before "
                            f"START ({r_start})\n"
                        ))
                    offset = r_start - 1
                    limit = r_end - r_start + 1
                    range_form = True
                else:
                    offset = int(parts[2])
            if len(parts) > 3 and parts[3]:
                if parts[3] in ("full", "raw"):
                    force_full = True
                elif parts[3].startswith("grep="):
                    pass  # picked up by the filter scan below
                elif range_form:
                    return _receipt(header, (
                        f"ERROR: read:PATH:START-END takes no LIMIT "
                        f"(got {parts[3]!r}) — the range already bounds it\n"
                    ))
                else:
                    limit = int(parts[3])
            # The filter can land in any trailing slot: parts[4] for the
            # documented `read:PATH:::grep=` (the `:::` yields two empty parts),
            # parts[3] when a range consumed only one slot. Scan rather than
            # index, so every spelling reaches the same place.
            grep_filter = ""
            for _tok in parts[3:]:
                if _tok.startswith("grep="):
                    grep_filter = _tok[5:]
                    break
            body = op_read(path, offset, limit, grep_filter, force_full,
                           range_form)
        elif op == "grep":
            # #1690: path=/file= extracted before anything else touches
            # `parts`, so every downstream peel (LIMIT/CONTEXT, the trailing-
            # extras refusal) sees the same shape it always did.
            parts, _kw_path = _extract_path_kw(parts)
            # Before the parse, and off the same peel the parse uses: a third
            # trailing integer is peeled and never read, so `grep:PAT:PATH:5:3:2`
            # ran limit 5 / context 3 and dropped the `2` — exit 0, no note, a
            # well-formed answer to a question nobody typed (#1345). The
            # identical argument closed the `all` case beside it in #1328;
            # `_grep_peeled_extras` stands aside for that one, which says more.
            _grep_extra = _grep_peeled_extras(parts)
            if _grep_extra:
                return _receipt(header, _extra_colon_tokens_refusal(
                    "grep", _grep_extra,
                    "Slot order is grep:PATTERN:PATH:LIMIT:CONTEXT — nothing "
                    "follows CONTEXT."))
            pattern, path, limit, context, count_only, no_auto_read, full = \
                _parse_grep_args(parts)
            if _kw_path is not None:
                path = _kw_path
            # Ahead of the hint, not after it: `_colon_split_hint` stats the
            # path to decide whether to fire, and a stat of an outside file is
            # itself the existence oracle this gate exists to close.
            _contained, (path,) = _gate_paths([path])
            if _contained:
                return _receipt(header, _contained)
            if limit == 0:
                return _receipt(header, _grep_zero_limit())
            if limit == GREP_LIMIT_ALL_MISPLACED:
                return _receipt(header, _GREP_ALL_OUTSIDE_LIMIT_SLOT)
            # Before the generic hint, because that one keys off "the path
            # does not exist" and `.` always exists — so the one reading in
            # this family that does NOT fail loudly was the one it declined
            # to diagnose (#1417).
            _widened = _absorbed_path_hint("grep", pattern, path,
                                           keys=("pattern",))
            if _widened:
                return _receipt(header, _widened)
            _hint = _colon_split_hint("grep", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude, no_auto_read=no_auto_read,
                           full=full)
        elif op == "grep_around":
            # grep_around:PATTERN:PATH[:N[:LIMIT]] — every match with N lines
            # context. Sane defaults for "show me how everyone uses this".
            # #1690: path=/file= extracted first, same as grep/around — the
            # fixed N/LIMIT slots below then land on whatever token is left,
            # which is exactly what removing one positional buys.
            parts, _kw_path = _extract_path_kw(parts)
            if _kw_path is not None:
                # The generic dispatch-level gate (above, keyed off
                # `_PATH_ARG_POSITIONS["grep_around"] = (2,)`) already ran —
                # against the RAW parts, before this branch's own
                # `_extract_path_kw` call. It checked the literal token
                # `path=/whatever`, which never escapes cwd itself (it is
                # not an absolute path, just a string starting with
                # 'path='), and passed. The real value is only known now,
                # so it gets its own gate here — self-review caught this as
                # a real containment bypass, not a hypothetical one: without
                # this, `grep_around:PAT:path=/etc/passwd` read straight
                # through where `grep_around:PAT:/etc/passwd` was refused.
                _contained, (_kw_path,) = _gate_paths([_kw_path])
                if _contained:
                    return _receipt(header, _contained)
                # Re-splice at the fixed PATH slot rather than re-deriving
                # every downstream index: `_grep_around_numeric_refusal` and
                # the N/LIMIT slots below all read `parts` positionally, and
                # a keyword path REMOVES a slot rather than filling it, so
                # putting it back where positional parsing expects it keeps
                # every one of those reads correct unchanged.
                parts = parts[:2] + [_kw_path] + parts[2:]
            ga_pattern = parts[1] if len(parts) > 1 else ""
            ga_path = parts[2] if len(parts) > 2 and parts[2] else "."
            if len(parts) > 3 and parts[3] == _GREP_ALL_TOKEN:
                return _receipt(header, _GREP_AROUND_ALL_IN_N_SLOT)
            # Both numeric slots, before either int() runs (#1826). A colon in
            # the PATTERN pushes its own tail into the N slot, and int() raised
            # through dispatch as `invalid literal for int() with base 10` —
            # the interpreter's sentence about the caller's search term, naming
            # neither the cause nor an escape.
            _ga_bad = _grep_around_numeric_refusal(parts, ga_pattern, ga_path)
            if _ga_bad:
                return _receipt(header, _ga_bad)
            ga_context = int(parts[3]) if len(parts) > 3 and parts[3] else 3
            ga_limit_tok = parts[4] if len(parts) > 4 and parts[4] else ""
            if ga_limit_tok == _GREP_ALL_TOKEN:
                ga_limit = GREP_LIMIT_ALL
            else:
                ga_limit = int(ga_limit_tok) if ga_limit_tok else 10
            if ga_limit == 0:
                return _receipt(header, _grep_zero_limit())
            body = op_grep(ga_pattern, ga_path, ga_limit, ga_context,
                           count_only=False, no_exclude=no_exclude)
        elif op == "wc":
            path = parts[1] if len(parts) > 1 else ""
            body = op_wc(path)
        elif op == "glob":
            pattern = parts[1] if len(parts) > 1 else ""
            no_auto_read = len(parts) > 2 and parts[2] == "no-auto-read"
            # The slot-count check upstream cannot see this one: parts[2] is a
            # slot glob READS, it just compares it to one literal and discards
            # anything else. Same class, one level in — a token that changed
            # nothing, on a call that answered anyway (#1582).
            if len(parts) > 2 and parts[2] and not no_auto_read:
                return _receipt(header, (
                    f"ERROR: glob: {parts[2]!r} is not a value that slot "
                    f"takes — glob:PATTERN[:no-auto-read], and "
                    f"`no-auto-read` is the only one." + chr(10)
                    + "  Compared against that one literal and discarded when "
                      "it did not match, until #1582: a token that changed "
                      "nothing, on a call that answered anyway." + chr(10)))
            body = op_glob(pattern, no_exclude=no_exclude, no_auto_read=no_auto_read)
        elif op == "ls":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            body = op_ls(path)
        elif op == "tail":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_tail(path, n)
        elif op == "head":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_head(path, n)
        elif op == "check":
            preset = parts[1] if len(parts) > 1 else ""
            path = parts[2] if len(parts) > 2 and parts[2] else ""
            body = op_check(preset, path)
        elif op == "gc":
            mode = parts[1] if len(parts) > 1 else ""
            kind = parts[2] if len(parts) > 2 else ""
            body = op_gc(mode, kind)
        elif op == "around":
            # #1690: same path=/file= extraction as grep, ahead of the peel
            # `_parse_around_args` does.
            parts, _kw_path = _extract_path_kw(parts)
            pattern, path, n = _parse_around_args(parts)
            if _kw_path is not None:
                path = _kw_path
            # Ahead of the delegation and the hint: both stat the path to
            # decide whether to fire, and that stat is the oracle. The #1135
            # guard inside the delegation covers the OTHER slot — parts[1],
            # once promotion has turned it into a filename — and still runs.
            _contained, (path,) = _gate_paths([path])
            if _contained:
                return _receipt(header, _contained)
            _delegated = _around_line_delegation(pattern, path, n)
            if _delegated:
                return _receipt(header, _delegated)
            _hint = _colon_split_hint("around", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_around(pattern, path, n)
        elif op == "map":
            path = parts[1] if len(parts) > 1 else "."
            body = op_map(path, no_exclude=no_exclude)
        elif op == "diff":
            path1 = parts[1] if len(parts) > 1 else ""
            path2 = parts[2] if len(parts) > 2 else ""
            body = op_diff(path1, path2)
        elif op == "stat":
            path = parts[1] if len(parts) > 1 else ""
            body = op_stat(path)
        elif op == "around_line":
            path = parts[1] if len(parts) > 1 else ""
            line = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            n = int(parts[3]) if len(parts) > 3 and parts[3] else 10
            body = op_around_line(path, line, n)
        elif op == "between":
            if len(parts) >= 2 and parts[1] == "re":
                # Pattern mode opt-in: between:re:START:END:PATH
                # 're:' is reserved as the mode marker — never falls through
                # to symbol mode, even if arg count is wrong, since 're' as
                # a symbol name is highly unlikely and silent fallthrough
                # produces misleading "file not found" errors when single-
                # letter args trip the Windows drive-letter merge in
                # _split_arg.
                if len(parts) >= 5:
                    start_pat = parts[2]
                    end_pat = parts[3]
                    path = ":".join(parts[4:])
                    _contained, (path,) = _gate_paths([path])
                    if _contained:
                        return _receipt(header, _contained)
                    # between:re rejoins RIGHTWARD, so a ':' in START or END
                    # steals from the path rather than from the pattern — the
                    # opposite of grep/around, and the reason it needs its own
                    # hint (#625).
                    _hint = _colon_split_hint(
                        "between", f"{start_pat}:{end_pat}", path,
                        keys=("start", "end"),
                        # The default prefix would be `between:START:END`,
                        # dropping the `re:` marker that selects this mode —
                        # a printed repair nobody can run.
                        call_prefix=f"between:re:{start_pat}:{end_pat}",
                        # #1972 review: op_between_pattern (below) has no
                        # swap-suggest fallback of its own, unlike around/
                        # grep/between-symbol -- declining here on the new
                        # "leading resolves as a real file" branch would
                        # hand the call to a body with nothing more to say,
                        # trading a real (if generic) diagnostic for none.
                        swap_fallback=False,
                    )
                    if _hint:
                        return _receipt(header, _hint)
                    body = op_between_pattern(start_pat, end_pat, path)
                else:
                    body = ("ERROR: between:re: requires START:END:PATH "
                            f"(got {len(parts) - 2} args after 're')\n")
            elif len(parts) >= 3:
                # Symbol mode: between:SYMBOL:PATH
                # Join middle parts on ':' so a qualified name stays ONE
                # symbol instead of re-reading the call as re: mode. It does
                # not make `Foo::bar` resolve: the query is compared literally
                # against a definition's own name node, which is `bar` in PHP,
                # C++ and Ruby alike (measured, #1163).
                symbol = ":".join(parts[1:-1])
                path = parts[-1]
                # Ahead of the hints, not after them: both stat the path to
                # decide whether to fire, and a stat of an outside file is
                # itself the existence oracle this gate exists to close.
                _contained, (path,) = _gate_paths([path])
                if _contained:
                    return _receipt(header, _contained)
                _range_hint = _between_numeric_hint(parts)
                if _range_hint:
                    return _receipt(header, _range_hint)
                _hint = _colon_split_hint("between", symbol, path,
                                          keys=("symbol",))
                if _hint:
                    return _receipt(header, _hint)
                body = op_between_symbol(symbol, path)
            else:
                body = ("ERROR: between requires SYMBOL:PATH or "
                        "re:START:END:PATH\n")
        elif op == "tree":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            d = int(parts[2]) if len(parts) > 2 and parts[2] else 3
            body = op_tree(path, d, exclude_paths=_get_exclude_paths("tree", no_exclude))
        elif op in ("replace", "replace_dry"):
            old_str = _dec(parts[1] if len(parts) > 1 else "")
            new_str = _dec(parts[2] if len(parts) > 2 else "")
            rpath = parts[3] if len(parts) > 3 and parts[3] else "."
            dry = op == "replace_dry"
            body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, rpath, dry=dry))
        elif op == "edit":
            old_str = _dec(parts[1] if len(parts) > 1 else "")
            new_str = _dec(parts[2] if len(parts) > 2 else "")
            epath = parts[3] if len(parts) > 3 else ""
            if _at_file_replace_all:
                body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, epath or "."))
            else:
                body = _run_with_validators(op, parts, lambda: op_edit(old_str, new_str, epath))
        elif op == "replace_lines":
            rl_path = parts[1] if len(parts) > 1 else ""
            try:
                rl_start = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                rl_end = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            except ValueError:
                body = "ERROR: replace_lines START/END must be integers\n"
            else:
                # CONTENT may legitimately contain ':' — rejoin remaining parts
                rl_content = _dec(":".join(parts[4:]) if len(parts) > 4 else "")
                body = _run_with_validators(op, parts, lambda: op_replace_lines(rl_path, rl_start, rl_end, rl_content))
        elif op == "paste":
            p_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            p_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_paste(p_path, p_content))
        elif op == "append":
            a_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            a_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_append(a_path, a_content))
        elif op == "vim":
            vim_path = parts[1] if len(parts) > 1 else ""
            vim_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = _run_with_validators(op, parts, lambda: op_vim(vim_path, vim_script))
        elif op == "json-set":
            # json-set:@file / json-set:@- only (#1822) -- its 'set' field
            # is a table (dotted-key -> value), not a scalar, so it cannot
            # go through the flat @file field-mapping route every other
            # write op uses (_at_file_to_parts / _AT_FILE_BUILTIN_DEFAULTS).
            # Same shape as "batch" just above: read the raw payload dict
            # here, validate it by hand, then build the two-element `parts`
            # _OP_TARGETS["json-set"] already expects.
            js_ref = parts[1] if len(parts) > 1 else ""
            if not js_ref.startswith("@"):
                body = (
                    f"ERROR: json-set takes an @file/@- payload, not colon "
                    f"arguments (got {js_ref!r}) -- write json-set:@- with "
                    f"'path' and 'set' fields in the payload.\n"
                )
            else:
                try:
                    js_payload = _load_at_file(js_ref)
                except ValueError as _je:
                    body = f"ERROR: {_je}\n"
                else:
                    if not isinstance(js_payload, dict):
                        body = (
                            f"ERROR: @file payload for op 'json-set' must be "
                            f"a JSON/TOML object with 'path' and 'set' "
                            f"fields, got {type(js_payload).__name__}\n"
                        )
                    else:
                        js_lower = {str(k).lower(): v for k, v in js_payload.items()}
                        js_unknown = sorted(set(js_lower) - {"path", "set"})
                        if js_unknown:
                            body = (
                                f"ERROR: @file payload for op 'json-set' has "
                                f"unknown field(s) {_flat_keys(js_unknown)} "
                                f"-- accepted: path, set\n"
                            )
                        elif "path" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'path'\n"
                            )
                        elif "set" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'set'\n"
                            )
                        elif not isinstance(js_lower["set"], dict):
                            body = (
                                f"ERROR: @file payload field 'set' for op "
                                f"'json-set' must be a table mapping dotted "
                                f"field paths to values, got "
                                f"{type(js_lower['set']).__name__}\n"
                            )
                        else:
                            js_path = str(js_lower["path"])
                            js_fields = js_lower["set"]
                            js_parts = ["json-set", js_path]
                            body = _run_with_validators(
                                "json-set", js_parts,
                                lambda: op_json_set(js_path, js_fields))
        elif op == "batch":
            # batch:@file — run multiple ops from a JSON file.
            # Payload: bare array of {"op":"X",...} objects, OR wrapper object
            # {"continue_on_error": bool, "ops": [...]}.
            # Default: continue_on_error=True (keep running after a failed op).
            ref = parts[1] if len(parts) > 1 else ""
            if not ref.startswith("@"):
                # Both grammars, because a nested batch reaches here from a
                # TOML payload where `batch:@ops.json` is not a thing the
                # caller can type — the field is what they can set, and the
                # old message never named one (#1407).
                body = (
                    "ERROR: batch takes an @file reference, not a bare path"
                    + (f" (got {ref!r})" if ref else "")
                    + " — write `batch:@ops.toml` in the colon form, or "
                    + 'path = "@ops.toml" inside a payload. The leading @ is '
                    + "what marks the value as a file to read the ops from, "
                    + "and it is required in both forms.\n"
                )
            else:
                try:
                    raw_payload = _load_at_file(ref)
                except ValueError as _be:
                    body = f"ERROR: {_be}\n"
                else:
                    # Normalise to (continue_on_error, ops_list)
                    if isinstance(raw_payload, list):
                        batch_ops = raw_payload
                        continue_on_error = True
                    elif isinstance(raw_payload, dict):
                        if "ops" not in raw_payload and [
                            k for k in raw_payload if k != "continue_on_error"
                        ]:
                            if isinstance(raw_payload.get("op"), str) and raw_payload["op"]:
                                # A dict carrying its own 'op' key is unambiguous
                                # — it is one op's fields, not a mistyped batch
                                # wrapper — so a batch of one runs it rather than
                                # refusing (#1026 item 3). The `_BATCH_WRAPPER_KEYS`
                                # check below is what still refuses a genuine
                                # wrapper typo (e.g. misspelt continue_on_error
                                # alongside a real 'ops' array); this branch never
                                # reaches it because 'ops' is absent here.
                                batch_ops = [raw_payload]
                                continue_on_error = True
                            else:
                                # Mirror of the single-op-route misroute (#468): this
                                # looks like one op's own fields (e.g. old/new/path),
                                # not a batch wrapper — say so instead of silently
                                # running zero ops.
                                batch_ops = None  # signal: already set body
                                body = (
                                    "ERROR: this payload has no 'ops' array — it looks "
                                    "like a single op's fields. Use 'OP:@file' (e.g. "
                                    "'edit:@file') for a single op, or wrap it as "
                                    '{"ops": [...]} for batch.\n'
                                )
                        else:
                            _wrapper_unknown = sorted(
                                k for k in raw_payload
                                if str(k).lower() not in _BATCH_WRAPPER_KEYS)
                            if _wrapper_unknown:
                                batch_ops = None  # signal: already set body
                                body = (
                                    "ERROR: unknown key(s) "
                                    + _flat_keys(_wrapper_unknown)
                                    + " at the top level of this batch payload "
                                    + "— accepted: "
                                    + ", ".join(sorted(_BATCH_WRAPPER_KEYS))
                                    + ". Refused rather than dropped (#1551): "
                                    + "a misspelt `continue_on_error` ran the "
                                    + "rest of the batch past a failed op "
                                    + "while the payload said to stop, and "
                                    + "nothing in the receipt disagreed."
                                    + chr(10)
                                )
                            else:
                                batch_ops = raw_payload.get("ops", [])
                                continue_on_error = bool(raw_payload.get("continue_on_error", True))
                    else:
                        batch_ops = []
                        continue_on_error = True
                        body = (
                            "ERROR: batch @file must be a JSON array or object "
                            f"with 'ops' key, got {type(raw_payload).__name__}\n"
                        )
                        batch_ops = None  # signal: already set body

                    if batch_ops is not None:
                        if not isinstance(batch_ops, list):
                            body = "ERROR: batch 'ops' must be a JSON array\n"
                        else:
                            _cap = _get_op_int("batch", "max_ops", MAX_BATCH_OPS)
                            _cap_exceeded = len(batch_ops) > _cap
                            if _cap_exceeded:
                                body = (
                                    f"ERROR: batch size {len(batch_ops)} exceeds "
                                    f"max_ops cap ({_cap}). Override via "
                                    f"`ops.batch.max_ops` in .supertool.json or split "
                                    f"into smaller batches.\n"
                                )
                                batch_ops = []
                                _snap_err = ""
                            else:
                                # Snapshot mode: reorder replace_lines ops on
                                # the same file bottom-up so caller line
                                # numbers refer to the original file state,
                                # not the file as mutated by earlier ops.
                                batch_ops, _snap_err = _reorder_batch_for_snapshot(batch_ops)
                                if _snap_err:
                                    body = f"ERROR: {_snap_err}\n"
                                    batch_ops = []
                            results: List[str] = []
                            # A batch is one logical change: defer per-op formatters
                            # (no_unused_imports etc.) so an import added by op N survives
                            # until op N+1's usage lands. main()'s len(argv)>1 guard never
                            # fires for a lone batch:@file arg, so own the defer locally —
                            # unless already inside a deferred multi-arg call, where main()
                            # owns the queue. Issue #291.
                            global _DEFER_FORMATTERS, _FORMAT_QUEUE
                            global _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN
                            _batch_owns_defer = not _DEFER_FORMATTERS
                            if _batch_owns_defer:
                                _DEFER_FORMATTERS = True
                                _FORMAT_QUEUE = {}
                                _VALIDATOR_DEFER_QUEUE = []
                                _VALIDATOR_DEFER_SEEN = set()
                            try:
                                for _item in batch_ops:
                                    if not isinstance(_item, dict):
                                        err = f"ERROR: each batch op must be a JSON object, got {type(_item).__name__}\n"
                                        results.append(err)
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue
                                    _sub_op = _item.get("op", "")
                                    if not _sub_op:
                                        err = "ERROR: batch op missing 'op' field\n"
                                        results.append(err)
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue
                                    # Build the arg string from the op + its fields,
                                    # using the @file→parts machinery for mutating ops
                                    # (preserves validators) and plain dispatch for others.
                                    _sub_pre_parsed = None
                                    if _sub_op in _READ_OP_AT_FIELDS:
                                        # Read op with its own payload route (#625).
                                        # Dispatch straight to the op, exactly as a
                                        # standalone `grep:@-` does: its pattern or
                                        # symbol is precisely what a colon join cannot
                                        # survive, and re-serializing it here undid the
                                        # reason the payload route was built. #644.
                                        _read_payload_fields = {
                                            str(_k): _v for _k, _v in _item.items()
                                            if str(_k).lower() != "op"
                                        }
                                        _read_target = str(
                                            _read_payload_fields.get("path", "") or ""
                                        )
                                        # Dispatched straight to the op, so
                                        # no frame exists to take the verdict.
                                        # Taken here instead, off the op's own
                                        # return value (#1291).
                                        _read_body = _read_op_from_payload(
                                            _sub_op, _read_payload_fields
                                        )
                                        if _op_body_failed(_read_body):
                                            _mark_op_failure()
                                        _sub_result = (
                                            "--- "
                                            + _payload_header_arg(_sub_op, _read_target)
                                            + " ---\n"
                                            + _read_body
                                        )
                                        results.append(_sub_result)
                                        if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                            _sub_result.split("\n")[1].startswith("ERROR")
                                        ):
                                            break
                                        continue
                                    if _at_file_fields(_sub_op):
                                        try:
                                            _sub_parts, _sub_replace_all = _at_file_to_parts(_sub_op, _item)
                                        except ValueError as _ve:
                                            err = f"ERROR: {_ve}\n"
                                            results.append(err)
                                            _mark_op_failure()
                                            if not continue_on_error:
                                                break
                                            continue
                                        # replace_all: true on an edit op → promote to replace
                                        if _sub_replace_all and _sub_op == "edit":
                                            _sub_parts[0] = "replace"
                                        # Route the ALREADY-structured parts straight through
                                        # dispatch via pre_parsed — do NOT re-serialize to a
                                        # `:::` string, which would re-tokenize and corrupt
                                        # content that itself contains `:::` (issue #252).
                                        # A readable colon summary is used only for the header.
                                        _sub_pre_parsed = (_sub_parts, _sub_replace_all)
                                        # The header gets the same treatment as the
                                        # parts: NOT re-serialized to a colon string.
                                        # `":".join(_sub_parts)` produced a header that
                                        # parsed as a different op — see
                                        # _payload_header_arg. #644.
                                        _sub_field_names = _at_file_fields(_sub_op)
                                        _sub_target = ""
                                        if "path" in _sub_field_names:
                                            _sub_path_idx = _sub_field_names.index("path") + 1
                                            if len(_sub_parts) > _sub_path_idx:
                                                _sub_target = _sub_parts[_sub_path_idx]
                                        _sub_arg = _payload_header_arg(
                                            _sub_parts[0], _sub_target
                                        )
                                    else:
                                        # Op with neither a mutating nor a read payload
                                        # route. Fields are placed by declared order, or
                                        # the op declines — never by alphabetical key
                                        # order, which is not any op's argument order and
                                        # dispatched a different op outright. #644.
                                        _fields, _order_err = _ordered_batch_fields(_sub_op, _item)
                                        if _order_err:
                                            results.append(_order_err)
                                            _mark_op_failure()
                                            if not continue_on_error:
                                                break
                                            continue
                                        _sub_arg = ":".join([_sub_op] + _fields) if _fields else _sub_op
                                    _sub_result = dispatch(_sub_arg, pre_parsed=_sub_pre_parsed)
                                    results.append(_sub_result)
                                    # NOT the call's verdict — that was taken
                                    # inside the frame above and is already in
                                    # `_call_failed()`. This is `continue_on_
                                    # error`'s own question, and it is still
                                    # answered by line-indexing the rendered
                                    # string, so a sub-op argument holding a
                                    # newline puts the header on line 1 and the
                                    # batch runs on. Same mechanism as #1291
                                    # and deliberately not folded into it:
                                    # `_call_failed()` cannot tell `ERROR` from
                                    # `FAIL`, so reusing it here would silently
                                    # widen what stops a batch. Left for its
                                    # own decision.
                                    if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                        _sub_result.split("\n")[1].startswith("ERROR")
                                    ):
                                        break
                            finally:
                                if _batch_owns_defer:
                                    _DEFER_FORMATTERS = False
                            # Only override `body` with joined results when no
                            # upstream error fired (cap rejection, snapshot reorder).
                            if not _snap_err and not _cap_exceeded:
                                body = "".join(results)
                                if _batch_owns_defer:
                                    body += _drain_format_queue()
                                    body += _drain_validator_queue()
        elif op == "payload-lint":
            body = op_payload_lint(parts[1] if len(parts) > 1 else "")
        elif op == "validate":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate:PATH:verbose  or  validate:PATH:tool1,tool2:verbose
            #   list form: validate:f1,f2,...:tool1,tool2:verbose  (commas in PATH)
            v_verbose = "verbose" in parts[1:]
            v_parts = [p for p in parts[1:] if p != "verbose"]
            v_path = v_parts[0] if len(v_parts) > 0 else ""
            v_tools = [t for t in (v_parts[1].split(",") if len(v_parts) > 1 and v_parts[1] else []) if t]
            v_files = [f for f in v_path.split(",") if f]
            if len(v_files) > 1:
                # Position 1 is the comma-joined blob, which the generic gate
                # above cannot read — so the individual files are checked here,
                # through the same helper.
                _v_contained, v_files = _gate_paths(v_files)
                if _v_contained:
                    return _receipt(header, _v_contained)
                body = op_validate_multi(v_files, v_tools or None, verbose=v_verbose)
            else:
                body = op_validate(v_path, v_tools or None, verbose=v_verbose)
        elif op == "format":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format:PATH:verbose  or  format:PATH:tool1,tool2:verbose
            f_verbose = "verbose" in parts[1:]
            f_parts = [p for p in parts[1:] if p != "verbose"]
            f_path = f_parts[0] if len(f_parts) > 0 else ""
            f_tools = [t for t in (f_parts[1].split(",") if len(f_parts) > 1 and f_parts[1] else []) if t]
            body = op_format(f_path, f_tools or None, verbose=f_verbose)
        elif op == "validate_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate_staged:verbose  or  validate_staged::tool1,tool2:verbose
            vs_verbose = "verbose" in parts[1:]
            vs_parts = [p for p in parts[1:] if p != "verbose"]
            vs_tools = [t for t in (vs_parts[0].split(",") if len(vs_parts) > 0 and vs_parts[0] else []) if t]
            body = op_validate_staged(vs_tools or None, verbose=vs_verbose)
        elif op == "format_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format_staged:verbose  or  format_staged::tool1,tool2:verbose
            fs_verbose = "verbose" in parts[1:]
            fs_parts = [p for p in parts[1:] if p != "verbose"]
            fs_tools = [t for t in (fs_parts[0].split(",") if len(fs_parts) > 0 and fs_parts[0] else []) if t]
            body = op_format_staged(fs_tools or None, verbose=fs_verbose)
        elif op == "resolve":
            rs_symbol = parts[1] if len(parts) > 1 else ""
            rs_from_file = parts[2] if len(parts) > 2 else None
            body = op_resolve(rs_symbol, rs_from_file)
        elif op == "diag":
            body = op_diag(parts[1] if len(parts) > 1 else "")
        elif op == "hover":
            body = op_hover(parts[1] if len(parts) > 1 else "",
                            parts[2] if len(parts) > 2 else "")
        elif op == "rename":
            body = op_rename(parts[1] if len(parts) > 1 else "",
                             parts[2] if len(parts) > 2 else "",
                             parts[3] if len(parts) > 3 else "")
        elif op == "workspace":
            ws_path = parts[1] if len(parts) > 1 else ""
            body = op_workspace(ws_path)
        elif op == "help":
            body = op_help(parts[1] if len(parts) > 1 else "")
        elif op == "guard":
            # The whole remainder is the command, colons and all: a shell
            # command is not a colon-delimited op arg and re-splitting it
            # would hand the matcher a different command than the one the
            # user typed.
            header = ""
            # op_guard is defined in _supertool_guard.py (#2706), loaded by
            # _load_part() into this module's own globals() before this line
            # ever runs -- genuinely bound at call time, but ruff lints this
            # file standalone and cannot see a name defined in a part, hence
            # the F821 silenced here rather than for the whole file (which
            # would lose real-undefined-name coverage over everything else
            # in it). The real check for a core call into a part-only name
            # (this direction) or a part call into a core-only name (the
            # other direction) is tests/test_part_loader_concatenated_ruff_2706.py,
            # which lints core+parts concatenated and sees both directions.
            body = op_guard(":".join(parts[1:]))  # noqa: F821
        elif op == "doctor":
            # Meta-op, markdown headers of its own — same treatment as
            # `version`/`registry`.
            header = ""
            body = op_doctor(parts[1] if len(parts) > 1 else "")
        elif op == "init":
            # Meta-op, markdown-ish preview/receipt of its own — same treatment
            # as `doctor`, not the --- op:args --- header every ordinary op gets.
            header = ""
            body = op_init(parts[1] if len(parts) > 1 else "")
        elif op == "registry":
            # Meta-op, markdown header — same treatment as `ops`, whose
            # listing this one answers the provenance half of.
            header = ""
            body = op_registry(parts[1] if len(parts) > 1 else "")
        elif op in ("introduction", "output-format", "ops", "ops-compact", "version"):
            # Meta-ops use markdown headers instead of --- header ---
            header = ""
            if op == "introduction":
                body = op_introduction()
            elif op == "output-format":
                body = op_output_format()
            elif op == "version":
                body = op_version()
            else:
                # `ops:gh-labels` used to discard its argument in silence and
                # print all 47KB — an unrecognised token dropped rather than
                # refused, in the op whose subject is which tokens exist
                # (#1231). `ops` and `ops-compact` share one arm rather than
                # one each: the first pass fixed `ops` and left `ops-compact`
                # swallowing the same token one `elif` over, which is how a
                # refusal that exists in one of two twinned branches reads as
                # a refusal that exists.
                ops_arg = parts[1] if len(parts) > 1 else ""
                if not ops_arg:
                    body = op_ops(compact=(op == "ops-compact"))
                elif ops_arg == "roster" and op == "ops":
                    body = op_ops_roster()
                elif ops_arg == "session" and op == "ops":
                    body = op_ops_session()
                elif ops_arg == "full" and op == "ops":
                    # What bare `ops` was before #1774 made signatures the
                    # default. Named on the default listing's own footer, with
                    # the byte count it is asking the caller to spend.
                    body = op_ops(full=True)
                elif ops_arg.startswith("grep=") and op == "ops":
                    # #1318 — the one filter, scoped to bare `ops` the same
                    # way roster/session/full are: `ops-compact:grep=X` still
                    # falls to the refusal below, naming this token.
                    #
                    # NOT ops_arg[len("grep="):] -- ops_arg is parts[1], one
                    # `_split_arg` token, and a pattern containing ':' (the
                    # exact shape a search for `read:PATH`-style syntax is)
                    # would be silently cut at the first one with no error
                    # (#1318 review). arg is the raw, unsplit dispatch string,
                    # so partitioning it once on the literal 'grep=' recovers
                    # every colon the caller typed.
                    grep_pattern = arg.partition("grep=")[2]
                    if not grep_pattern:
                        body = ("ERROR: `ops:grep=` needs a pattern after "
                                "`grep=` — `ops:grep=PATTERN`.\n")
                    else:
                        body = op_ops_filter(grep_pattern)
                else:
                    body = _ops_argument_refusal(ops_arg, op)
        else:
            # Fallthrough: try custom ops, then aliases
            custom = _resolve_custom_op(op, parts)
            if custom is not None:
                body = custom
                _custom_op_ok = _CUSTOM_OP_OK[0]
            else:
                alias = _resolve_alias(op, parts)
                if alias is not None:
                    body = alias
                else:
                    body = _unknown_op_message(op)
    except (ValueError, IndexError) as e:
        body = f"ERROR: argument parsing: {e}\n"

    # The verdict, taken here and nowhere else. `body` is what the op RETURNED
    # and the header has not been prepended yet, so the boundary this used to
    # go looking for is not in question. Everything below only decorates the
    # receipt — payload warnings lead it, a batch's `[result]` leads it — and
    # each of those pushes the verdict token off the line a body scan reads.
    #
    # A preset op is not a second population. `_resolve_custom_op` records
    # `result.returncode == 0` in `_CUSTOM_OP_OK` at the subprocess, and the
    # `FAIL (…)` line is rendered FROM that boolean rather than the other way
    # round. Where it stays None the op never reached a child — a timeout, an
    # OSError, a malformed `ops` entry — and the string it returned is then
    # the only statement in existence about what happened.
    if _custom_op_ok is not None:
        if not _custom_op_ok:
            _mark_op_failure()
    elif _op_body_failed(body):
        _mark_op_failure()

    # Fire read-op notifiers (mutating ops already fire inside _run_with_validators)
    try:
        _notify_read_op(op, parts)
    except Exception:
        pass  # observation must never break the call

    # Payload-parse notes lead the body: they are about bytes an op has already
    # written, so they have to be readable without scrolling past the receipt
    # that claims those bytes are fine. Depth-gated because a batch parses its
    # payload once, in this frame, before any sub-op runs.
    if _PAYLOAD_WARNINGS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body = _take_payload_warnings() + body

    # Backstop for `_PAYLOAD_PY_ESCAPE_ADVISORY` (#2493): `_atomic_write`
    # pops its own entry the moment the matching path is written, but a
    # write that never happens -- the op errors before reaching
    # `_atomic_write`, or targets a different path than the one the payload
    # named -- would otherwise leave a stale entry for a LATER, unrelated
    # write in the same warm-daemon process to inherit. Same depth gate as
    # `_PAYLOAD_WARNINGS` above, for the same reason: a batch parses its
    # payload once, in the outer frame.
    if _PAYLOAD_PY_ESCAPE_ADVISORY and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        _PAYLOAD_PY_ESCAPE_ADVISORY.clear()

    if _WRITE_WARNINGS:
        body += "".join(w[1] for w in _WRITE_WARNINGS)
        _WRITE_WARNINGS.clear()

    if _FORMATTER_SKIPS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body += (
            "[formatters] skipped: " + ", ".join(_FORMATTER_SKIPS)
            + " — no config for it in the edited file's repo (#393)\n"
        )
        _FORMATTER_SKIPS.clear()

    # Swap in the compact header only if the op actually wrote — see the note
    # where it was built. The test is the write counter, not an ERROR prefix on
    # the receipt: `op_replace`'s zero-match returns "(0 occurrences of 'x'
    # found)", which is a failure that says nothing about being one, and that
    # is precisely the case where the caller needs the verbatim `old` back.
    # A preset op writes no file through `_atomic_write`, so the write counter
    # is silent for it and its own exit status is the only success signal
    # available. Same rule as above rather than a looser one: taken from a
    # status code, never from the prose of the receipt being summarised.
    if _compact_header and (_cnt_frame("cnt_write") > 0
                            or _custom_op_ok is True):
        header = (f"--- {_flat_field(_compact_header, disclose_newline=True)}"
                  f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")
    # Right file, wrong branch is silent until commit time, and supertool is
    # the thing that knows (#381). Success and failure both get it — a failed
    # edit is the exact moment a wrong-branch hypothesis should be available,
    # instead of being reached for only after re-reading the file.
    #
    # Once per call, never per sub-op: a batch runs each of its ops through this
    # same function recursively, so an unguarded footer would print the branch
    # once per edit — 50 identical lines for a 50-edit batch, which is the
    # opposite of the handful of tokens this is meant to cost. The batch itself
    # carries the single footer, and only when it actually mutated something.
    #
    # The "did anything get written" test is the write counter, not a flag set
    # in the batch loop: `"batch"` is not in `_OP_TARGETS`, so a mutation buried
    # in an INNER batch never propagated outward and a nested batch reported no
    # branch at all (#392). The counter is bumped at `_atomic_write`, which every
    # mutating op passes through however deeply it is nested.
    #
    # `[result]` goes directly above it, under the same gate: the branch line
    # must stay the last line (#381's tests assert `endswith`), and a footer
    # that only appears when a branch happens to exist would go missing outside
    # a repo — which is the exact shape #621 is about.
    # A read-only op that ran validators can still carry the one thing the
    # footer exists to carry: a checker that did not check (#969). `validate`
    # is not in `_OP_TARGETS` and mutates nothing, so it was gated out of the
    # summary line entirely -- `_depth1_call_footer` (factored out for #1158,
    # so the `validate:@-` payload route could carry the identical footer)
    # applies the same gate.
    body = _depth1_call_footer(op, body)

    return header + body


# Read-op extractors → (path, line_start, line_end). Used by _notify_read_op.
# Each entry maps `op` to a function that takes the parsed `parts` list and
# returns either (path, line, line_end) or None if the op doesn't have a
# meaningful single-file/range to notify on.
def _read_target_around_line(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    # around_line:PATH:LINE[:N]   default N=10
    if len(parts) < 3:
        return None
    path = parts[1]
    try:
        line = int(parts[2])
    except (TypeError, ValueError):
        return None
    n = 10
    if len(parts) > 3:
        try: n = int(parts[3])
        except (TypeError, ValueError): pass
    return (path, max(1, line - n), line + n)


def _read_target_read(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Lines to highlight for `read:PATH[:OFFSET:LIMIT|:START-END|:full]`.

    Both grammars, and both were wrong (#1417, adjacent to the disclosure fix):

    * `:OFFSET:LIMIT` returned `OFFSET .. OFFSET+LIMIT-1`, one line above the
      window `read` actually renders — OFFSET read as a start line, which is the
      whole subject of #1138, here in the code that decides where the editor
      puts the cursor. `docs/notifiers.md` documented the off-by-one faithfully,
      which is why it survived: the doc and the code agreed with each other and
      neither agreed with the op.
    * `:START-END` fell through to no range at all, so the form the docs tell
      callers to prefer was the one form that got no highlight — and it is the
      one needing no arithmetic.

    A zero or negative LIMIT computed an END before its START (`10:0` gave
    `10 .. 9`). No range is an honest answer there; a backwards one is not.
    """
    if len(parts) < 2:
        return None
    path = parts[1]
    if len(parts) == 3 and _READ_RANGE_RE.fullmatch(parts[2]):
        start, end = (int(x) for x in parts[2].split("-"))
        if start < 1 or end < start:
            return (path, None, None)
        return (path, start, end)
    if len(parts) >= 4:
        try:
            offset = int(parts[2]); limit = int(parts[3])
        except (TypeError, ValueError):
            return (path, None, None)
        if offset < 0 or limit <= 0:
            return (path, None, None)
        return (path, offset + 1, offset + limit)
    return (path, None, None)


def _read_target_file_only(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    if len(parts) < 2:
        return None
    return (parts[1], None, None)


def _read_target_between(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    # between:SYMBOL:PATH (resolve range via tree-sitter)
    # between:re:START:END:PATH (regex — line numbers unknown without re-running)
    if len(parts) < 3:
        return None
    if parts[1] == "re" and len(parts) >= 5:
        # Regex variant — return file only, no precomputable range
        return (parts[4], None, None)
    symbol = _normalize_symbol_query(parts[1])
    path = parts[2]
    if not _has_tree_sitter():
        return (path, None, None)
    ext = os.path.splitext(path)[1].lower()  # keep the leading dot — that's the key
    lang = _TS_LANG_MAP.get(ext)
    if not lang:
        return (path, None, None)
    found = _ts_find_node(path, lang, symbol)
    if found is None:
        return (path, None, None)
    node, _kind, _total = found
    start_line = node.start_point[0] + 1  # 0-indexed → 1-indexed
    end_line = node.end_point[0] + 1
    return (path, start_line, end_line)


_READ_OP_TARGETS: Dict[str, Any] = {
    "around_line": _read_target_around_line,
    "read":        _read_target_read,
    "between":     _read_target_between,
    "map":         _read_target_file_only,
    "tail":        _read_target_file_only,
    "head":        _read_target_file_only,
    "wc":          _read_target_file_only,
    "stat":        _read_target_file_only,
}


def _notify_read_op(op: str, parts: List[str]) -> None:
    """Fire notifiers for read ops (mutating ops fire inside _run_with_validators)."""
    extractor = _READ_OP_TARGETS.get(op)
    if not extractor:
        return
    target = extractor(parts)
    if target is None:
        return
    path, line_start, line_end = target
    if not path:
        return
    _run_notifiers(op, path, line=line_start, line_end=line_end)


def caller_tag() -> str:
    """Build a short caller identity string for the log line.

    Claude Code doesn't expose session_id in env to Bash tools (it only
    appears in hook stdin payloads). The best session-stable proxy we have
    is PPID — the parent bash's PID stays the same within one Claude Code
    session, so grouping by ppid gives per-session totals.
    """
    user = os.environ.get("USER", "?")
    ppid = os.getppid()
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "?")
    return f"user={user} ppid={ppid} entry={entry}"


def log_call(args: List[str], out_bytes: int) -> None:
    """Append timestamped call log with caller id + output size.

    The ops count and out_bytes let post-analysis compute per-call cost and
    estimate round-trips saved vs a naive (one-op-per-call) baseline.
    """
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta = f"ops={len(args)} out={out_bytes}b"
            f.write(f"{timestamp} | {caller_tag()} | {meta} | {' '.join(args)}\n")
    except OSError:
        pass  # Logging is best-effort


