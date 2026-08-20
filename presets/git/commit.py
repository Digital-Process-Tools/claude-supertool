#!/usr/bin/env python3
"""Git commit — stage PATHS (optional) + commit MSG + verifiable receipt.

Receipt always shows:
  - HEAD before/after SHA
  - files committed + +/- lines
  - hook exit code + first error line if pre/post-commit blocks

Surfaces silent rollbacks: if HEAD is unchanged after the call,
that's printed loudly. Replaces the add/commit/log-1 cycle.

Special MSG values:
  --no-edit   Use prepared commit message (MERGE_MSG / CHERRY_PICK_HEAD).
              Only valid when a merge or cherry-pick is in progress.
  amend       REFUSED (#962). There is no amend route here, and committing
              this makes a commit whose subject is the word `amend`. Use
              `git commit --amend` directly, or set
              SUPERTOOL_ALLOW_LITERAL_AMEND=1 for the literal subject.

PATHS are separated by ':::', not by commas and not by spaces. Both input
routes stage: `git-commit:::MSG:::A:::B` and a payload with `paths = [...]`.

A message containing ':' must arrive via `git-commit:::MSG` or the @payload
route: the single-colon CLI tokenizes on ':', so `git-commit:fix: thing`
reaches this script as MSG='fix' plus a PATH of ' thing'. That shape is
REFUSED here rather than re-parsed — see _spilled_message_paths (#751).
"""
from __future__ import annotations

import os
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# And `presets/` itself, for `_untrusted`. Explicitly rather than by leaning on
# `_git_common` having done it first: that made the import order load-bearing,
# and the failure is a `ModuleNotFoundError` at *runtime* on the commit itself.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _untrusted  # noqa: E402  (a hook's stream is somebody else's text — #1475)
from _git_common import (  # noqa: E402
    NOT_A_REPO,
    TIMEOUT_RC,
    _first_error_line,
    _git,
    probe_repo,
    query_open_mr,
    relayed_block,
    repo_label,
    st_hint,
    unanswered_repo_lines,
    use_utf8_stdout,
)

# triple-colon separator handled by supertool; we receive plain argv here.

# The commit itself, which runs whatever the pre-commit hook chain is. Every
# other call in this preset is rev-parse / diff --cached plumbing on the shared
# 10s default; this one carries the 30s the whole module used to assume, and is
# the only place in it where 30s was ever doing work.
_COMMIT_TIMEOUT = 30


def _existing_mr_for_branch(branch: str) -> str:
    """Open MR/PR identifier for `branch` (e.g. !42 / #7), or empty when none.

    Thin formatter over the shared lookup — kept for the post-commit hint.
    """
    mr = query_open_mr(branch)
    if not mr:
        return ""
    prefix = "!" if mr["source"] == "gitlab" else "#"
    return f"{prefix}{mr['iid']}"


def _head_sha() -> str:
    r = _git(["rev-parse", "--short", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""


# Default co-author trailer. Configurable via the git-commit op:
#   .supertool.json -> ops.git-commit.coauthor  (exported as SUPERTOOL_COAUTHOR)
# Set to an empty string / "none" / "off" / "false" to disable.
_DEFAULT_COAUTHOR = "Max <noreply>"
_DISABLE_VALUES = {"", "none", "off", "false", "no", "0"}


def _coauthor_value() -> str:
    """Trailer identity ('Name <email>') or '' when disabled.

    Env SUPERTOOL_COAUTHOR (set from .supertool.json ops.git-commit.coauthor)
    wins; falls back to the built-in default. Same env-over-config convention
    used by the other git/gitlab presets.
    """
    raw = os.environ.get("SUPERTOOL_COAUTHOR")
    val = _DEFAULT_COAUTHOR if raw is None else raw
    return "" if val.strip().lower() in _DISABLE_VALUES else val.strip()


def _with_coauthor(msg: str) -> str:
    """Append a `Co-Authored-By:` trailer when absent and one is configured.

    Skips entirely if the message already carries a `Co-Authored-By:` line
    (case-insensitive) or if the trailer is disabled via config.
    """
    identity = _coauthor_value()
    if not identity:
        return msg
    if any(l.strip().lower().startswith("co-authored-by:")
           for l in msg.splitlines()):
        return msg
    trailer = f"Co-Authored-By: {identity}"
    body = msg.rstrip("\n")
    return f"{body}\n\n{trailer}"


# Characters a caller does not put in a pathspec they typed at this CLI, but
# that prose spilled by the ':' tokenizer always carries. Glob magic (*, ?, [)
# is deliberately absent: `src/*.py` is a legitimate pathspec.
_PROSE_CHARS = (" ", "\t", "\n", "\r", '"', "'")

# The @payload example quotes the message with a triple-single-quote block.
# Built rather than typed so this file stays editable through supertool's own
# TOML payload route, where a literal triple quote would close the block.
_TRIPLE = "'" * 3


def _looks_like_pathspec(tok: str) -> bool:
    """Could *tok* be a pathspec the caller actually typed? (#751)"""
    return bool(tok) and tok == tok.strip() and not any(
        c in tok for c in _PROSE_CHARS
    )


def _known_to_git(path: str, staged_deletions: set) -> bool:
    """Does git recognise *path* as something it could stage?

    On disk, already staged as a deletion (`git rm`, issue #324), or tracked.
    Existence alone is not the test: the deletion case is exactly why a
    'does it resolve to a real file' discriminator cannot be trusted.
    """
    if os.path.exists(path) or path in staged_deletions:
        return True
    r = _git(["ls-files", "--", path])
    return r.returncode == 0 and bool(r.stdout.strip())


def _spilled_message_paths(paths, staged_deletions):
    """PATH args that are neither path-shaped nor known to git (#751).

    An empty result means every PATH is plausibly a path and the call proceeds
    exactly as it always has — including a typo'd path, which stays git's error
    to report rather than something this script reinterprets.
    """
    return [
        p for p in paths
        if not _looks_like_pathspec(p) and not _known_to_git(p, staged_deletions)
    ]


def _arg_separator():
    """What split this call's fields: ':::', ':', or '' for a payload (#946).

    Set by the core as SUPERTOOL_ARG_SEP. Unset means an older core or a
    direct invocation of this script, and ':' is what the refusal below
    assumed unconditionally before this existed — so the default preserves
    the old behaviour rather than inventing a fourth state for it.
    """
    return os.environ.get("SUPERTOOL_ARG_SEP", ":")


def _payload_fields_refusal(msg, paths, spilled, rest):
    """`paths = [...]` held something that is not a path — and nothing split.

    The colon refusal below cannot be reused here even though the test that
    fired is identical. It states that the message "was split on ':'", and
    then rebuilds a message by FUSING the message with the offending entries.
    On the payload route no tokenizer ran: both are assertions about a parse
    that did not happen, and the rebuilt string is a message the caller never
    wrote, handed back under a refusal's authority (#946).

    So this reports what is actually true — the message is intact, some
    `paths` entries are not paths — and declines to guess where they belong.
    """
    lines = [
        "ERROR: %d of %d `paths` entries are not paths — nothing staged, "
        "nothing committed." % (len(spilled), len(paths)),
        "  Nothing was split: the payload route takes its fields as given.",
        "  Parsed as: message=%r (intact)" % (msg,),
    ]
    for p in paths:
        why = " (not a path, and unknown to git)" if p in spilled else ""
        lines.append("             path=%r%s" % (p, why))
    lines += [
        "  `paths` holds pathspecs only. If part of the message ended up",
        "  there, put it back in `message` — this op will not guess which:",
        "    ./supertool 'git-commit:@-' <<'EOF'",
        "    message = " + _TRIPLE + msg + _TRIPLE,
        "    paths = ["
        + ", ".join(_toml_basic(p) for p in rest or ["path/to/file"])
        + "]",
        "    EOF",
    ]
    return lines


def _colon_split_refusal(msg, paths, spilled, sep=":"):
    """The error printed instead of guessing what the caller meant (#751).

    Reconstructs the message the caller almost certainly typed — the leading
    run of spilled segments, rejoined **on the separator that split them** —
    and hands back the routes that carry it intact. Nothing is staged and
    nothing is committed.

    *sep* is not decoration. This rejoined on ':' whatever the route, so a
    message containing ':::' — split by the triple-colon CLI into a message
    plus prose "paths" — came back with its ':::' rewritten to ':'. Both
    suggested repairs carried the rewrite, so pasting either committed bytes
    the caller never wrote, under a refusal that was otherwise correct
    (#946). A refusal whose remedy corrupts the thing it is repairing is
    worse than a refusal carrying no remedy at all.

    When the faithful rebuild contains ':::' there is no colon spelling that
    can carry it — the separator wins — so the payload route leads, because
    it is the one that survives arbitrary content. The single-colon reading
    is still offered under it, because a ':::' typed where a ':' was meant is
    a real and common way to land here (#963 pins that the offered form,
    pasted, commits the subject the caller wanted). What changed is that it
    now SAYS it is rewriting the separator. The defect was never that the
    single-colon reading was offered; it was that it was offered as if it
    were the message that had been typed.

    Both routes are quoted by the helpers rather than by hand. The subject
    here is the caller's own prose, so an apostrophe in it is ordinary
    English and it closed the single-quoted shell word; and `"%s"` is not
    TOML quoting, so a path holding a `"` produced an unparseable payload and
    one holding a `\\` produced a payload that parses as a different path.
    """
    rest = list(paths)
    head = []
    while rest and rest[0] in spilled:
        head.append(rest.pop(0))

    if not sep:
        return "\n".join(_payload_fields_refusal(msg, paths, spilled, rest))

    rebuilt = sep.join([msg] + head)

    lines = [
        "ERROR: commit message was split on %r — nothing staged, nothing "
        "committed." % (sep,),
        "  Parsed as: message=%r" % (msg,),
    ]
    for p in paths:
        why = " (not a path, and unknown to git)" if p in spilled else ""
        lines.append("             path=%r%s" % (p, why))
    def _colon_form(text):
        out = "git-commit:::" + text
        if rest:
            out += ":::" + ":::".join(rest)
        return "    ./supertool " + _sh_quote(out)

    payload = [
        "    ./supertool 'git-commit:@-' <<'EOF'",
        "    message = " + _TRIPLE + rebuilt + _TRIPLE,
        "    paths = ["
        + ", ".join(_toml_basic(p) for p in rest or ["path/to/file"])
        + "]",
        "    EOF",
    ]

    if ":::" in rebuilt:
        lines += [
            "  Your message contains ':::', this op's own field separator, so",
            "  no colon form can carry it unchanged. The payload route can —",
            "  it takes the message as bytes:",
        ]
        lines += payload
        # The single-colon reading, named as a reading rather than handed back
        # as the message. `alt` differs from `rebuilt` only when this call was
        # split on ':::', which is exactly when the rewrite needs disclosing.
        alt = ":".join([msg] + head)
        if alt != rebuilt:
            lines += [
                "  If you meant a single ':' there, this commits %r" % (alt,),
                "  — note that the ':::' becomes ':':",
                _colon_form(alt),
            ]
    else:
        lines += [
            "  supertool's CLI splits this call on %r, so a Conventional" % (sep,),
            "  Commits subject cannot survive it. Use a route that does not",
            "  tokenize:",
            _colon_form(rebuilt),
            "  or, for paths with spaces or a multi-line body:",
        ]
        lines += payload
    return "\n".join(lines)


# Whole-message values that are an instruction in git's vocabulary rather
# than content. Exact match on the stripped, lowercased message — deliberately
# not a "message looks like a subcommand" heuristic, which would drift into
# refusing `revert the revert` and every other legitimate subject that starts
# with a verb git also owns.
_AMEND_WORDS = {"amend", "--amend"}
_ALLOW_LITERAL_AMEND = "SUPERTOOL_ALLOW_LITERAL_AMEND"


def _amend_refusal(msg):
    """`amend` was taken as content when it is unambiguously intent (#962).

    There is no amend route in this op, so the previous behaviour was to make
    a commit whose subject is the word `amend` and print a success receipt for
    it. Undoing that is `git reset --soft HEAD~1`, which is the one git
    operation people get wrong under time pressure.

    This refuses and names the gap. It deliberately does NOT add an amend
    route: that is a feature, it needs its own refusal for an already-pushed
    commit, and it is not something to grow out of the side of a bugfix.
    """
    lit = msg.strip()
    return [
        "ERROR: %r is a git instruction, not a commit message — nothing staged, "
        "nothing committed." % (lit,),
        "  git-commit has no amend route, so this would have made a commit whose",
        "  subject is the word %r, which then has to be undone." % (lit,),
        "  To amend the last commit, use git directly — it is not wrapped here:",
        "      git commit --amend --no-edit           keep the existing message",
        "      git commit --amend -m 'NEW SUBJECT'    replace it",
        "  Do not amend a commit that is already pushed: that rewrites published",
        "  history, and this op has no way to tell you whether it was.",
        "  If you really did mean the literal subject %r:" % (lit,),
        "      %s=1 ./supertool %sgit-commit:::%s%s"
        % (_ALLOW_LITERAL_AMEND, chr(39), lit, chr(39)),
    ]


def _literal_amend_allowed():
    """Is the #962 refusal switched off for this call?

    Same env-over-nothing shape and the same off-vocabulary as the co-author
    trailer, so there is one convention in this file rather than two.
    """
    raw = os.environ.get(_ALLOW_LITERAL_AMEND, "")
    return raw.strip().lower() not in _DISABLE_VALUES


def _add_failure_lines(add, to_add):
    """git add refused a pathspec — say what a well-formed one looks like (#986),
    for the route the caller is on, or say nothing at all (#1489).

    The op advertises that it refuses a mangled pathspec, and it does. What it
    never said is what the separator *is*, so a caller who guessed ',' learned
    only that their guess was wrong — an error that names the fault and not
    the remedy.

    The comma split is offered as a question, not applied as a fact: `a,b.txt`
    is a legal filename, so a token git already knows is never taken apart.

    The split is unbounded — one argument can hold any number of commas — so
    it goes through the same cap as every other remedy in this file. Passing
    only *shown* and letting *total* default made the placeholder branch
    unreachable here, which is how 25 guessed pathspecs went into one
    pasteable line under a docstring promising that cannot happen. Past the
    cap the guesses are listed as well, because the count line claims what
    the reader saw and nothing above it had shown them.

    #1489 — _SEPARATOR_NOTE used to be printed under *every* add failure, on
    both routes. Two things were wrong with that, and two agents paid for each
    in one afternoon:

    - On the payload route the note names the **other** route's convention.
      `paths = "a.txt:::b.txt"` and `paths = ":::--all"` were both answered
      with "Paths are separated by ':::'" — which is exactly what each caller
      had typed, sending them to re-read the thing they got right. That route
      wants a TOML array, and its refusal now says so and offers the split.
    - When no separator is in play at all — one path, spelled wrong — the note
      is a remedy for a fault that did not happen. git already named the fault;
      the third state here is silence, not a reworded guess, because a fresh
      wrong remedy reads as a freshly checked one.
    """
    lines = [
        # `_untrusted.flat`, not the raw stream (#1475): this is line 1 of a
        # refusal at column 0, and git relays a filter/hook's words verbatim.
        "ERROR: git add failed: %s"
        % (_untrusted.flat(add.stderr.strip() or add.stdout.strip()),),
    ]
    if _arg_separator() == "":
        return lines + _payload_route_add_lines(to_add)
    return lines + _colon_route_add_lines(to_add)


def _colon_route_add_lines(to_add):
    """The ':::'-form remedy, emitted only where a separator is plausibly at
    fault — which on this route means a comma (#986, narrowed by #1489).

    A token holding a space never arrives here: it fails `_looks_like_pathspec`
    and `_colon_split_refusal` answers it several frames earlier. So the note's
    "not by spaces" clause is unreachable from this call site, and the only
    guess this branch can honestly make is the comma one.

    The note itself comes from `_colon_remedy`, which every other caller in
    this file relies on for it. Repeating it above the question printed it
    twice in the same refusal — pre-existing, and visible only once the
    unconditional copy stopped hiding one of them.
    """
    split, guessed = [], False
    for p in to_add:
        parts = [x for x in p.split(",") if x]
        if len(parts) > 1 and not _known_to_git(p, set()):
            guessed = True
            split.extend(parts)
        else:
            split.append(p)
    if not guessed:
        return []
    lines = ["  A ',' above is not a separator here. Did you mean:"]
    if len(split) > _LIST_CAP:
        lines += _sample(split)
    lines += _colon_remedy(split[:_LIST_CAP], len(split))
    return lines


# Separators a payload caller plausibly reached for inside one `paths` entry,
# in the order they are tried. ':::' first because it is the measured shape:
# both #1489 sightings had carried the colon form's separator into a TOML
# string. A ',' is the same mistake in the other direction.
_PAYLOAD_JOINS = (":::", ",")


def _payload_route_add_lines(to_add):
    """The `paths = [...]` remedy — this route's own shape, not the other's.

    Nothing was tokenized here, so the entry is exactly what the caller wrote:
    a separator inside one means they spelled an array as a joined string. The
    split is offered as a question and only for an entry git does not already
    know, on the same reasoning as the comma branch — a filename may hold
    either token.

    Empty segments are dropped, which is what makes `":::--all"` come back as
    the one-element array it was meant to be rather than as `["", "--all"]`.

    Every token that was actually split on is named, not the first one found:
    a `paths` holding one ':::'-joined entry and one ','-joined entry would
    otherwise have both taken apart under a line naming only one of them —
    a remedy that is right about the split and wrong about the reason, which
    is the shape this whole change exists to remove.

    `_known_to_git` runs a subprocess, so it is asked only about an entry that
    holds a candidate token at all. The order matters on a long refused list
    and nothing else depends on it.
    """
    split, found = [], []
    for p in to_add:
        tok = ""
        for cand in _PAYLOAD_JOINS:
            if cand in p and [x for x in p.split(cand) if x]:
                if not _known_to_git(p, set()):
                    tok = cand
                break
        if tok:
            if tok not in found:
                found.append(tok)
            split.extend([x for x in p.split(tok) if x])
        else:
            split.append(p)
    if not found:
        return []
    named = " and ".join(repr(t) for t in found)
    one = len(found) == 1
    verb = "is not a separator" if one else "are not separators"
    lines = [
        "  On this route `paths` is a TOML array — one entry per path.",
        "  %s %s inside an entry; %s reached git as part of"
        % (named, verb, "it" if one else "they"),
        "  the pathspec. Did you mean:",
    ]
    if len(split) > _LIST_CAP:
        lines += _sample(split)
    lines += _payload_remedy(split[:_LIST_CAP], len(split), lead=None)
    return lines


# #1137 — the opt-in spelling for "commit exactly the paths you were just
# counted at me". A sentinel in the PATHS slot rather than a new op or a
# trailing flag: it reaches argv through both input routes unchanged, so the
# colon form and the `paths = [...]` payload spell it identically.
#
# It is deliberately NOT `.` (cwd-relative in git, so it means something
# different from a subdirectory) and deliberately not a default. The refusal
# on the bare form is the feature; this only gives the deliberate case a name.
_ALL_TOKEN = "--all"


def _all_with_paths_refusal(paths):
    """`--all` next to a named path — refused rather than resolved either way.

    Both readings are defensible and both are wrong to guess: taken as the
    wider one, the named path was pointless; taken as the narrower one, the
    other dirty files are dropped under a green tick. That second shape is
    the #963 defect, so this declines and prints both spellings.
    """
    others = [p for p in paths if p != _ALL_TOKEN]
    if not others:
        # Every argument was the token — `:::--all:::--all`. There is no
        # narrower list to offer, and _colon_remedy over an empty one renders
        # `git-commit:::MESSAGE:::` with a trailing empty pathspec: a remedy
        # that, pasted, lands back on the refusal it was printed under.
        return [
            "ERROR: %s was given %d times — nothing staged, nothing committed."
            % (_ALL_TOKEN, len(paths)),
            "  It is a single opt-in, not a repeatable flag. Once is enough:",
            "    ./supertool " + _sh_quote("git-commit:::MESSAGE:::" + _ALL_TOKEN),
        ]
    lines = [
        "ERROR: %s was given alongside %d named path(s) — nothing staged, "
        "nothing committed." % (_ALL_TOKEN, len(others)),
        "  %s already means every dirty path, so a path beside it either "
        "adds nothing or narrows it," % (_ALL_TOKEN,),
        "  and this op will not pick which of those you meant.",
        "  Everything dirty:",
        "    ./supertool " + _sh_quote("git-commit:::MESSAGE:::" + _ALL_TOKEN),
        "  Or only what you name:",
    ]
    lines += _colon_remedy(others[:_LIST_CAP], len(others))
    return lines


def _all_ambiguous_refusal(modified, untracked, unknown):
    """git knows a path literally called `--all`, so the token means two
    things (#1137).

    The payload route is no escape hatch here — `paths = ["--all"]` reaches
    the same argv — so the remedy is a spelling git resolves as a path and
    this op does not read as a sentinel.
    """
    lines = [
        "ERROR: %r is both this op's commit-everything opt-in and a path git "
        "knows — nothing staged, nothing committed." % (_ALL_TOKEN,),
        ("  Nothing in the argument says which was meant, and both routes "
         + "spell it the same way."),
        "  To commit the FILE, write it so git reads it as a path:",
        "    ./supertool " + _sh_quote("git-commit:::MESSAGE:::./" + _ALL_TOKEN),
    ]
    if unknown:
        lines += [
            "  To commit everything: the dirty list is UNKNOWN here — "
            "`git status` did not answer (%s)." % (unknown,),
            "  Name the paths yourself: git-commit:::MESSAGE:::PATHS",
        ]
        return lines
    shown = modified[:_LIST_CAP] + untracked[:_LIST_CAP]
    total = len(modified) + len(untracked)
    if not total:
        lines.append("  There is nothing else dirty to commit.")
        return lines
    lines.append("  To commit everything, name the paths instead:")
    lines += _colon_remedy(shown, total)
    return lines


def _expand_all():
    """Resolve `--all` to the concrete set it commits, or say why it could not.

    Returns `(paths, refusal_lines)`. `refusal_lines` non-empty means nothing
    was resolved and the caller must print them and stop — a `git status` that
    did not answer produces two empty lists here, and staging those would be
    an empty commit dressed as a deliberate one (docs/validators.md,
    "Declining instead of guessing").

    **The index is part of the answer, not just the working tree** (#1228).
    `git status`'s unstaged column cannot see a path that is staged and whose
    worktree matches the index, so a fully-staged tree resolved to an EMPTY
    list here — and an empty list left `paths` empty, which under #1228's
    scoping is the *pathless* call: an unscoped `git commit` that swept the
    whole index in with no disclosure at all. That is the defect #1228 exists
    to remove, reachable through the one spelling that promises the opposite
    ("the receipt names every one"). Including the index makes the set
    explicit, so it is scoped, listed, and checkable.

    The index read is required rather than best-effort for the same reason:
    under `--all` the caller typed no list, so a silently short one is a
    receipt that claims completeness it does not have.

    **`--no-renames`, and it is load-bearing (#1865).** Rename detection is on
    by default, so for a staged `git mv a b` this read prints `b` alone — the
    deletion of `a` is folded into a rename entry and its name never appears.
    That silently subtracted from the set the paragraph above had just decided
    to include: `--all` committed the added half of the move, left the deletion
    staged, and landed a tree holding two copies of the file. Half a rename
    compiles, imports, and passes any test that only touches the new path.
    Measured on git 2.46.2 — `--name-only` prints `b`, `--name-only
    --no-renames` prints `a` and `b`.

    Every index read in this file carries the flag for the same reason. The two
    `--diff-filter=D` reads need it most: with detection on, a rename is not
    reported as a deletion at all, so the #324 drop that keeps a gone-from-disk
    path out of `git add` did not fire and naming both halves of a move aborted
    with `fatal: pathspec 'a' did not match any files`.
    """
    modified, untracked, unknown = _worktree_changes()
    if unknown:
        return [], [
            "ERROR: %s could not be resolved — `git status` did not answer "
            "(%s). Nothing staged, nothing committed." % (_ALL_TOKEN, unknown),
            "  What is dirty is UNKNOWN, so there is no list to accept.",
            "  Name the paths explicitly: git-commit:::MESSAGE:::PATHS",
        ]
    idx = _git(["diff", "--cached", "--name-only", "--no-renames", "-z"])
    if idx.returncode != 0:
        # `str.split()` cannot be forged with a separator — `split(None)` uses
        # the *Unicode* whitespace predicate, so U+2028, U+0085 and the
        # vertical tab all fold. What walks through it is C0 non-whitespace,
        # i.e. ESC: an erase-line/cursor-up pair rewrites the receipt above
        # this line (#851). `flat()` discloses it (#1475, corrected in #1569).
        said = _untrusted.flat(" ".join((idx.stderr or "").split())[:120])
        return [], [
            "ERROR: %s could not be resolved — the index could not be read "
            "(%s). Nothing staged, nothing committed."
            % (_ALL_TOKEN, said or "exit %d" % idx.returncode),
            "  Already-staged paths are part of what %s means, so the list "
            "would be incomplete." % (_ALL_TOKEN,),
            "  Name the paths explicitly: git-commit:::MESSAGE:::PATHS",
        ]
    # dict.fromkeys rather than a set: the receipt lists these in order and
    # under `--all` the receipt is the only record of what was chosen.
    return list(dict.fromkeys(modified + untracked + _z_paths(idx.stdout))), []


def _worktree_changes(git_fn=None):
    """One `git status -z` read, split three ways (#1003, #1016).

    Returns `(modified_tracked, untracked, why_unknown)`. `why_unknown` is
    non-empty exactly when git did not answer, and the two lists are then
    meaningless — a caller must render the reason, never the empty lists,
    because empty lists print as "nothing to report" (docs/validators.md,
    "Declining instead of guessing").

    `-z` rather than the default porcelain: git *quotes* a path containing a
    space or a non-ASCII byte in the newline-terminated form, and a quoted
    path pasted back into `git-commit:::MSG:::PATH` is not that path. The `-z`
    stream is unquoted and NUL-separated on every platform, Windows included,
    and it always uses '/' — no separator normalisation is needed or wanted.

    A rename or copy carries its source as a *second* record; that record is
    consumed here rather than parsed as a status line of its own, which would
    invent a modified file out of a path that no longer exists.
    """
    run = _git if git_fn is None else git_fn
    # `-c status.showUntrackedFiles=normal` (#1290/#1295). #1295 classed this
    # site as a render; it is not. `_resolve_all_token` feeds this list to
    # `git add`, so an inherited `status.showUntrackedFiles=no` does not merely
    # print short — `--all` stages and commits the tracked half only, under a
    # receipt naming that subset as the answer to `--all`, indistinguishable
    # from a tree whose untracked half was genuinely empty.
    #
    # Only the display setting is pinned. `core.quotePath` is deliberately left
    # alone: `-z` is unquoted whatever it is set to, and pinning it here would
    # suggest this read depends on something it does not.
    r = run(["-c", "status.showUntrackedFiles=normal",
             "status", "--porcelain=v1", "-z"])
    if r.returncode != 0:
        said = _untrusted.flat(" ".join((r.stderr or "").split())[:120])
        return [], [], said or f"exit {r.returncode}"
    modified, untracked = [], []
    skip_next = False
    for rec in r.stdout.split(chr(0)):
        if not rec:
            continue
        if skip_next:
            skip_next = False
            continue
        if len(rec) < 4:
            continue
        x, y, path = rec[0], rec[1], rec[3:]
        if x in ("R", "C"):
            skip_next = True
        if x == "?":
            untracked.append(path)
        elif y in ("M", "D", "T"):
            modified.append(path)
    return modified, untracked, ""


def _z_paths(stdout):
    """Split a `-z` path stream, dropping only the segments that are empty.

    The trailing NUL gives one empty final segment and that is the whole of
    what needs discarding. Filtering on `p.strip()` instead also discards a
    path made entirely of whitespace, which git permits and POSIX
    filesystems hold — and every caller here feeds either the record of what
    was committed or the list of what must be dropped before `git add`.
    Losing a path in the first is an absence the tool invented; losing it in
    the second re-opens #324 for that path.
    """
    return [p for p in stdout.split(chr(0)) if p != ""]


# How many paths a *single* listing prints before it starts counting instead.
# A caller that prints two capped lists has shown up to 2*_LIST_CAP paths, so
# neither remedy below re-derives "how many did the reader see" from this
# constant: both are handed the list that was actually printed, plus the true
# total. Deriving it was the #963 defect surviving inside the #963 fix.
_LIST_CAP = 20

_SEPARATOR_NOTE = "Paths are separated by ':::' — not by commas, not by spaces."


def _sh_quote(word):
    """One POSIX shell word, safe for a path containing an apostrophe.

    The suggestion lines wrap the op in single quotes, so `it's.txt` closed
    the string early and the pasted command was a shell syntax error. Always
    quoting (rather than `shlex.quote`, which leaves ordinary words bare)
    keeps every existing line byte-identical while fixing this one.
    """
    return chr(39) + word.replace(chr(39), chr(39) + chr(34) + chr(39) + chr(34) + chr(39)) + chr(39)


def _toml_basic(value):
    """One TOML basic string — the `paths = [...]` array is real TOML.

    Paths came out of `git status -z` unquoted on purpose, so that spaces and
    non-ASCII bytes survive (#1003); a `"` or a `\\\\` in one of them then made
    the payload this op suggests unparseable by the payload route it suggests
    it to.
    """
    out = value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))
    for raw, esc in ((chr(10), "n"), (chr(13), "r"), (chr(9), "t")):
        out = out.replace(raw, chr(92) + esc)
    return chr(34) + out + chr(34)


def _sample(paths, cap=_LIST_CAP):
    """First *cap* paths, indented, plus a count line when there are more."""
    lines = ["    " + p for p in paths[:cap]]
    if len(paths) > cap:
        lines.append(f"    … {len(paths) - cap} more")
    return lines


def _colon_remedy(shown, total=None, message="MESSAGE"):
    """Every path that was printed, or a visible placeholder — never a subset.

    A remedy naming the first three of fifteen paths the reader was *just
    shown* is not an abbreviation. Pasted, it stages three, commits, and
    prints a success receipt; nothing in the line itself says the other
    twelve were dropped. That is a remedy which produces the wrong commit
    while carrying the tool authority a refusal lends it — the same failure
    as a checker reporting ok when it never ran, one layer along (#963).

    *shown* is the list the caller actually printed and *total* how many
    there were; when they differ, naming "all of it" is not available and the
    third state is a placeholder that cannot be mistaken for an answer.
    Dumping 200 pathspecs into a suggestion is its own defect, and dumping
    the first 20 of 200 is the original one.

    Both are parameters rather than something re-derived from _LIST_CAP,
    because a caller printing *two* capped lists has shown up to twice the
    cap. Computing it here announced `20 shown and 10 not shown` over two
    complete lists of 15 — a false claim about the tool's own output, in the
    line that exists to stop exactly that.
    """
    total = len(shown) if total is None else total
    head = "    ./supertool "
    stem = "git-commit:::" + message + ":::"
    if total <= len(shown):
        return [
            head + _sh_quote(stem + ":::".join(shown)),
            "  " + _SEPARATOR_NOTE,
        ]
    return [
        head + _sh_quote(stem + "PATH[:::PATH...]"),
        f"  {total} paths in all, {len(shown)} shown and "
        f"{total - len(shown)} not shown — name the ones you mean.",
        "  " + _SEPARATOR_NOTE,
    ]


_PAYLOAD_LEAD = "  or, for a multi-line message — this route stages too, via `paths`:"


def _payload_remedy(shown, total=None, lead=_PAYLOAD_LEAD):
    """The `@-` form, spelled with its `paths` key (#986).

    Pointing only at the colon form answers a caller who may be on the
    payload route precisely *because* their message will not survive
    ':'-tokenization. Both routes stage; both are named here, so the reader
    picks rather than being told they used the wrong one.

    Same *shown*/*total* contract as _colon_remedy, and for the same reason:
    a `paths = [...]` array holding a subset of what was listed is the #963
    defect in TOML.

    *lead* is `None` for a caller already standing on this route (#1489):
    offering it as the alternative to somewhere they are not is the misdirect
    one line smaller.
    """
    total = len(shown) if total is None else total
    if shown and total <= len(shown):
        arr = ", ".join(_toml_basic(p) for p in shown)
    else:
        arr = _toml_basic("path/to/file")
    return ([] if lead is None else [lead]) + [
        "    ./supertool " + chr(39) + "git-commit:@-" + chr(39) + " <<" + chr(39) + "EOF" + chr(39),
        "    message = " + _TRIPLE + "MESSAGE" + _TRIPLE,
        "    paths = [" + arr + "]",
        "    EOF",
    ]


def _left_behind_lines(git_fn=None):
    """What the commit did not include — named, counted, or declined (#1016).

    `[]` means "this run looked and there was nothing to say". It is never
    returned for a check that could not run: silence there is byte-for-byte
    the receipt of a complete commit, printed under a ✓ that already argues
    nothing was left out. That is the same defect one layer along.

    Untracked files are counted, not listed. Nearly every worktree carries
    some, and a list of them under every commit is a list nobody reads on the
    commit that needed it. A modified *tracked* file is the one that means
    "you edited this and did not commit it".

    Counted is not the same as accounted for, which is #1070. Two shapes were
    wrong here, both in the direction that reads as "nothing was left":

    * with modified files present, the count appeared in the header and the
      pasteable remedy one line below named only the modified paths. Pasted,
      it commits a strict subset of what the receipt just accounted for and
      prints its own green tick over that. The remedy now says so — a subset
      is fine, a subset presented as the whole is the #963 defect.
    * with *only* untracked files left, this returned `[]`. A brand-new test
      file, never committed, under `Files committed: N ✓` and no mention of it
      anywhere. Silence is byte-for-byte the render of "nothing was left
      behind", and the drop stays invisible until CI runs a file that is not
      in the tree.

    The fix is disclosure, not a listing: the counted-not-listed decision of
    #1016 is deliberately kept, because a scratch-file dump under every commit
    is how a warning stops being read.
    """
    modified, untracked, unknown = _worktree_changes(git_fn)
    if unknown:
        return [
            f"⚠ Left-behind check SKIPPED — `git status` did not answer ({unknown}).",
            "  This receipt does not say whether anything was left uncommitted.",
        ]
    if not modified and not untracked:
        return []
    if not modified:
        return [
            f"⚠ {len(untracked)} untracked file(s) were NOT included "
            f"(new files are never staged unless you name them).",
            "  Not listed here — see them with: " + st_hint("git-status:full"),
        ]
    extra = f"  ({len(untracked)} untracked, not listed)" if untracked else ""
    lines = [f"⚠ {len(modified)} modified tracked file(s) were NOT included:{extra}"]
    lines += _sample(modified)
    lines.append("  Intentional? If not:")
    lines += _colon_remedy(modified[:_LIST_CAP], len(modified))
    if untracked:
        lines.append(
            f"  The {len(untracked)} untracked file(s) are NOT in the command "
            f"above — name them too if you meant to commit them.")
    return lines


def _staged_elsewhere(git_fn=None):
    """Everything in the index, whoever staged it — `[]` when it could not read.

    Used by the pre-commit refusal only, to add a disclosure to a message it is
    already printing. A failed read therefore costs a line of detail and never
    invents one: the refusal itself is correct either way.

    `_still_staged_lines` deliberately does NOT go through this. It is the
    whole of what it prints, so a failed read there has to be loud, and it
    checks the return code itself.
    """
    run = _git if git_fn is None else git_fn
    res = run(["diff", "--cached", "--name-only", "--no-renames", "-z"])
    if res.returncode != 0:
        return []
    return _z_paths(res.stdout)


def _still_staged_lines(git_fn=None):
    """What was staged and is NOT in the commit — read from the index (#1228).

    The one check in this file computed against the commit rather than against
    the working tree, and the only one that can see a staged-only path: a file
    staged by another process and never touched in the worktree is invisible
    to `git status`'s unstaged column, so `_left_behind_lines` cannot report
    it and never could.

    Silence here means the index came back empty, which after a commit is the
    receipt of a complete one. A read that failed is therefore said out loud
    rather than returned as `[]` — the same rule as every other check in this
    file, and the reason the #1228 incident was survivable at all was that a
    receipt contradicted itself out loud.
    """
    run = _git if git_fn is None else git_fn
    left = run(["diff", "--cached", "--name-only", "--no-renames", "-z"])
    if left.returncode != 0:
        said = _untrusted.flat(" ".join((left.stderr or "").split())[:120])
        return [
            "⚠ Still-staged check SKIPPED — `git diff --cached` did not answer "
            f"({said or 'exit ' + str(left.returncode)}).",
            "  This receipt does not say whether anything stayed in the index.",
        ]
    paths = _z_paths(left.stdout)
    if not paths:
        return []
    lines = [
        f"⚠ {len(paths)} path(s) were already staged and are NOT in this commit:"
    ]
    lines += _sample(paths)
    lines.append("  git-commit commits the paths you name and leaves the rest "
                 "of the index alone.")
    lines.append("  They are still staged. To commit them too:")
    lines += _colon_remedy(paths[:_LIST_CAP], len(paths))
    return lines


def _nothing_staged_lines(named=0):
    """The refusal, with the list the op was already holding (#1003).

    The refusal itself stays: committing files the caller did not name is not
    a default anyone wants. What changes is that the remedy stops being a raw
    `git add -A` guessed from an empty error.

    #1155 — the opening line now names the CAUSE rather than the symptom.
    `nothing staged` is true and useless: it describes the index, when what
    the caller did was omit the argument that fills it. Three callers in one
    day read it, concluded the payload route's `paths` key was the missing
    ingredient, and were wrong — the colon route stages identically, including
    for a multi-line message.

    It has to be the FIRST line specifically. The core `--- op ---` header
    replays the whole message above this block, so a long commit message puts
    everything after line one below the fold, which is where the diagnosis was.

    *named* is how many PATHS the call actually carried, so the two arms stay
    distinguishable: "you named none" and "the ones you named held nothing"
    are different mistakes and only one of them is about the argument list.
    """
    modified, untracked, unknown = _worktree_changes()
    if unknown:
        return [
            "ERROR: nothing staged, and what is unstaged is UNKNOWN — "
            f"`git status` did not answer ({unknown}).",
            "  Stage explicitly: git-commit:::MESSAGE:::PATHS",
        ]
    # #1228 — this arm exists because the scoped check above made it reachable.
    # A path staged by another process with a clean worktree is invisible to
    # `git status`'s unstaged column, so every list below is empty and the
    # refusal read "the working tree is clean, so there is nothing to commit"
    # over a non-empty index. That is this repo's standing defect: an absence
    # produced by where the tool looked, printed as an absence in the world.
    elsewhere = _staged_elsewhere()
    if named and elsewhere:
        lines = [
            f"ERROR: nothing staged — the {named} path(s) you named held no "
            f"changes to stage.",
            f"  {len(elsewhere)} other path(s) ARE staged, and git-commit "
            "commits only the paths you name:",
        ]
        lines += _sample(elsewhere)
        lines.append("  To commit the index exactly as it stands, name no "
                     "paths at all:")
        lines.append("    ./supertool " + _sh_quote("git-commit:::MESSAGE"))
        if modified or untracked:
            lines.append("  Or name what you meant:")
            shown = modified[:_LIST_CAP] + untracked[:_LIST_CAP]
            lines += _colon_remedy(shown, len(modified) + len(untracked))
        return lines
    if not modified and not untracked:
        return [
            ("ERROR: nothing staged — the working tree is clean, so there is "
             + "nothing to commit."),
        ]
    if named:
        lines = [
            f"ERROR: nothing staged — the {named} path(s) you named held no "
            f"changes to stage.",
            "  These are the ones that do:",
        ]
    else:
        lines = [
            ("ERROR: no PATHS were given — git-commit never stages for you, "
             + "so nothing staged."),
            "  Name what to commit. These are dirty right now:",
        ]
    if modified:
        lines.append(f"  Modified tracked ({len(modified)}):")
        lines += _sample(modified)
    if untracked:
        lines.append(f"  Untracked ({len(untracked)}):")
        lines += _sample(untracked)
    # The two lists above are capped independently, so what the reader saw is
    # not min(_LIST_CAP, len(modified) + len(untracked)). Hand both remedies
    # the printed list and the true total rather than letting them guess.
    shown = modified[:_LIST_CAP] + untracked[:_LIST_CAP]
    total = len(modified) + len(untracked)
    lines.append("  Commit the ones you mean, by name:")
    lines += _colon_remedy(shown, total)
    lines += _payload_remedy(shown, total)
    # #1137 — the count above is the op's own answer to "which paths?", and
    # without this line accepting it meant rebuilding the list by hand from a
    # raw `git status --porcelain`. Named last, under the by-name remedies,
    # because the deliberate whole-tree commit is the rarer of the two.
    lines += [
        f"  Or take all {total} of them deliberately — the receipt names "
        f"every one:",
        "    ./supertool " + _sh_quote("git-commit:::MESSAGE:::" + _ALL_TOKEN),
    ]
    return lines


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: commit.py MSG [PATH ...]")
        return 1

    msg = sys.argv[1]
    paths = sys.argv[2:]
    no_edit = msg.strip() == "--no-edit"

    if not no_edit and not msg.strip():
        print("ERROR: commit message is empty.")
        return 1


    # One `rev-parse --git-dir`, not two (#1126). The repository check below and
    # the MERGE_HEAD probe further down were asking git the identical question
    # microseconds apart and throwing away one of the two answers. Unlike
    # `merge.py`'s head_before/head_after — which look identical and are
    # deliberately not — nothing between these two points can move the git dir,
    # so there is one question here and it is now asked once.
    # Three states (#1858): the probe answering "no" and the probe not
    # answering at all are different facts, and this op is about to WRITE. The
    # git dir travels with the answer so the merge probe below still reads it
    # from one call rather than two.
    inside, git_dir = probe_repo(_git)
    if inside is None:
        for line in unanswered_repo_lines(git_dir):
            print(line)
        print("  Nothing was staged and nothing was committed.")
        return 1
    if not inside:
        print(NOT_A_REPO)
        return 1

    head_before = _head_sha()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    # Computed unconditionally now, not only under --no-edit: #1228 scopes the
    # commit to a pathspec, `git commit -- PATH` is a *partial* commit, and git
    # refuses those outright mid-merge (`fatal: cannot do a partial commit
    # during a merge`, exit 128). A merge commit is whole-index by
    # construction, so the scoping stands down there rather than turning a
    # working call into a fatal one.
    _gd = git_dir
    in_merge = bool(_gd) and (
        os.path.exists(os.path.join(_gd, "MERGE_HEAD"))
        or os.path.exists(os.path.join(_gd, "CHERRY_PICK_HEAD"))
    )
    if no_edit and not in_merge:
        print("ERROR: --no-edit requires a merge or cherry-pick in progress "
              "(no MERGE_HEAD/CHERRY_PICK_HEAD found).")
        return 1

    print(f"# git-commit on {branch}")
    # Printed before anything is staged, so it is on the receipt whether the
    # commit lands, is refused by a hook, or finds nothing staged (#692).
    print(f"Repo: {repo_label()}")
    print(f"HEAD before: {head_before}")

    # #962, under #692's contract: every refusal path stamps repo and branch
    # first. This one sends the reader to a raw `git commit --amend`, so which
    # checkout and which branch is the first thing they need and the one thing
    # this op knows and they may not. Still before anything is staged.
    if (not no_edit and msg.strip().lower() in _AMEND_WORDS
            and not _literal_amend_allowed()):
        for line in _amend_refusal(msg):
            print(line)
        return 1

    # #1137 — resolve the opt-in before anything else reads PATHS, so the
    # sentinel never reaches `git add` as a pathspec (which is how it failed
    # before: `fatal: pathspec '--all' did not match any files`). From here
    # down, `paths` is a list of real paths whichever route produced it.
    named = len(paths)
    all_used = False
    if _ALL_TOKEN in paths:
        if len(paths) > 1:
            for line in _all_with_paths_refusal(paths):
                print(line)
            return 1
        # The real staged-deletion set, not an empty one: a file named `--all`
        # that has been `git rm`'d is gone from disk *and* from `ls-files`, so
        # an empty set here answers "git does not know it" for the one state
        # where the ambiguity is live and the deletion is about to be
        # committed under a sentinel reading of its own name (#324's shape).
        gone = _git(["diff", "--cached", "--diff-filter=D", "--name-only",
                     "--no-renames", "-z"])
        gone_set = set(_z_paths(gone.stdout))
        if _known_to_git(_ALL_TOKEN, gone_set):
            mod, untr, unk = _worktree_changes()
            for line in _all_ambiguous_refusal(mod, untr, unk):
                print(line)
            return 1
        paths, refusal = _expand_all()
        if refusal:
            for line in refusal:
                print(line)
            return 1
        # An empty expansion is a clean tree, not a failure: fall through to
        # the staged check so it lands on the one refusal that says so.
        all_used = bool(paths)
        named = len(paths)

    # Stage PATHS if given. A path that's already a staged deletion (gone from
    # disk after `git rm`) would make `git add` abort with "pathspec did not
    # match any files" — so drop those from the add list; their deletion is
    # already staged and will be committed (issue #324). Genuinely-unknown
    # paths stay in the list, so they still error as before.
    if paths:
        # `-z` for the same reason the receipt read below uses it, and this
        # one is load-bearing rather than cosmetic: without it core.quotepath
        # renders `café.txt` as a quoted, octal-escaped string that never
        # equals the path the caller typed, so the staged deletion is not
        # dropped from the add list and `git add` aborts on a file that is
        # gone from disk — #324, alive for every non-ASCII path.
        deleted = _git(["diff", "--cached", "--diff-filter=D", "--name-only",
                        "--no-renames", "-z"])
        staged_deletions = set(_z_paths(deleted.stdout))
        # #751 — a PATH that is neither path-shaped nor known to git is far more
        # likely the tail of a ':'-split message than a file. Refuse before
        # anything is staged; do NOT fold it back into the message, because a
        # wrong guess in that direction commits whatever was already staged
        # under a mangled subject and prints a success receipt for it.
        spilled = _spilled_message_paths(paths, staged_deletions)
        if spilled:
            print(_colon_split_refusal(msg, paths, spilled,
                                       _arg_separator()))
            return 1
        to_add = [p for p in paths if p not in staged_deletions]
        if to_add:
            add = _git(["add", "--"] + to_add)
            if add.returncode != 0:
                for line in _add_failure_lines(add, to_add):
                    print(line)
                return 1
        print(f"Staged: {len(paths)} path(s)")

    # Pre-commit staged check
    # `-z` for the same reason _worktree_changes uses it: the newline form
    # runs paths through core.quotepath, so an accented filename reaches the
    # receipt as octal escapes inside literal quotes, which is not the path
    # and does not paste back as one. That was survivable while the caller
    # had typed the list; under `--all` they typed nothing and this listing
    # is the only record of what was committed (#1137).
    # #1228 — scoped to the paths that were named, for the same reason the
    # commit below is. Unscoped, a foreign staged path makes the index look
    # non-empty when the caller's own paths hold nothing, so this op's refusal
    # ("the N path(s) you named held no changes to stage") is replaced by
    # git's `nothing added to commit`, exit 1, under a header that has already
    # printed `Staged: 1 path(s)`.
    scope = ["--"] + paths if (paths and not in_merge) else []
    staged = _git(["diff", "--cached", "--name-only", "--no-renames", "-z"] + scope)
    # `TIMEOUT_RC` before the fold, and this is the worst-placed instance of
    # #1858's class in the file: `git add` has ALREADY run, twenty lines up.
    # A stall here therefore reaches `_nothing_staged_lines`, which re-derives
    # from fresh calls and — with the paths now staged and the worktree
    # consequently clean — prints `ERROR: nothing staged — the working tree is
    # clean, so there is nothing to commit.` over an index this very op just
    # filled. Not a missing section: a positive false claim about the world,
    # made by the op that caused the state it is denying.
    #
    # Found by the #1858 audit, not by the sweep, because it does not print
    # `not inside a git repository` and so matched no grep the sweep ran. It is
    # in scope for the same issue: its work item is every `returncode != 0`
    # with a `TIMEOUT_RC` in reach, not every copy of one sentence.
    if staged.returncode == TIMEOUT_RC:
        print("ERROR: could not tell what is staged — `git diff --cached` did "
              "not answer (%s)."
              % _untrusted.flat((staged.stderr or "").strip()
                                or "exit %d" % TIMEOUT_RC))
        print("  NO COMMIT WAS MADE. The index was not read, so this is not a "
              "claim that it is empty.")
        if paths:
            print("  Anything this call staged is STILL STAGED. Re-run, or "
                  "inspect with: " + st_hint("git-status"))
        return 1
    if staged.returncode != 0 or not staged.stdout.strip():
        for line in _nothing_staged_lines(named):
            print(line)
        return 1
    staged_files = _z_paths(staged.stdout)

    # Commit — with the pathspec, so the commit is the paths that were named
    # and nothing else (#1228). Without it, `git commit` takes the WHOLE index:
    # on 2026-08-09 a staged revert left by a review agent rode into commit
    # 3123343 and silently un-did 139 lines of a production fix, while the
    # worktree still held the correct file and the tests still passed against
    # the worktree. A commit that removes the fix and keeps the tests green is
    # the one shape nothing downstream can catch.
    #
    # The pathless call is deliberately left alone: `git-commit:::MESSAGE` with
    # no PATHS is the spelling that means "commit what I staged by hand", and
    # it still does. What changes is that naming paths now means what the
    # signature always said it meant.
    if no_edit:
        result = _git(["commit", "--no-edit"], timeout=_COMMIT_TIMEOUT)
    else:
        result = _git(["commit", "-m", _with_coauthor(msg)] + scope,
                      timeout=_COMMIT_TIMEOUT)
    head_after = _head_sha()

    if result.returncode == 0 and head_after and head_after != head_before:
        new_sha = head_after
        print(f"HEAD after:  {new_sha} ✓")
        # Files + line stats from new commit
        stat = _git(["show", "--shortstat", "--format=", new_sha])
        if stat.returncode == 0 and stat.stdout.strip():
            print(stat.stdout.strip().splitlines()[-1].strip())
        print(f"Files committed: {len(staged_files)}")
        # #1137 asked for a receipt that records which 24 paths went in. The
        # 20-cap is right when the caller typed the list and already has it;
        # under `--all` the caller typed nothing, so the receipt is the only
        # record of what was chosen and a capped one is a subset presented as
        # the whole.
        listed = staged_files if all_used else staged_files[:20]
        for f in listed:
            print(f"  {f}")
        if len(staged_files) > len(listed):
            print(f"  … {len(staged_files) - len(listed)} more")
        # #1016 — the tick above argues nothing was left out. Say so only when
        # that is established, and say the opposite when it is not.
        for line in _left_behind_lines():
            print(line)
        # #1228's second half. The block above is computed against the WORKING
        # TREE, and that is why the original incident printed a receipt naming
        # the two files it had just committed: the worktree still held the
        # correct content, so they read as uncommitted. Scoping the commit
        # fixes the lie in that direction and opens a new one — a staged-only
        # path is now correctly excluded and was, until this line, excluded
        # silently. This is computed against the COMMIT: whatever is still in
        # the index afterwards is exactly what did not go in.
        for line in _still_staged_lines():
            print(line)
        # Next-step hint
        upstream_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if upstream_res.returncode == 0 and upstream_res.stdout.strip():
            existing = _existing_mr_for_branch(branch)
            if existing:
                print(f"Next: git push (updates {existing})")
            else:
                print("Next: ./supertool 'git-push' (or ./supertool 'mr:.max/mr.md|TIME|LABELS' for push+MR)")
        else:
            print("Next: git push -u origin HEAD (no upstream set)")
        return 0

    # Failure path — could be hook block, validation, or silent rollback
    for line in _failure_receipt(result, head_before, head_after):
        print(line)
    return result.returncode or 1


def _failure_receipt(result, head_before: str, head_after: str) -> list:
    """The whole refused-commit render, as lines — hook block or rollback.

    A function rather than five `print`s inside `main` so the render can be
    driven with a hostile child stream and no git repository (#1475). What it
    renders is a **pre-commit hook's** stdout and stderr: code the operator
    installed, so no escalation on its own, but the same seam a remote reaches
    on `git-push` and the same three lines a consumer anchors at column 0.

    The transcript goes through `_git_common.relayed_block`, which is the same
    seam `git-push`'s three dumps use. This docstring used to *claim* that —
    "exactly as `push._relayed_lines` does it" — while restating half of its
    body inline, and the half it dropped was the `> ` prefix: a hook's lines
    sat at column 0 under the header, so one printing `Status: COMMITTED` wrote
    a line no consumer can tell from ours (#1569). The seam moved into
    `_git_common` in the same change, because the reason the copy existed at
    all was that `push.py` is not importable from here.
    """
    combined = (result.stdout or "") + chr(10) + (result.stderr or "")
    err = _first_error_line(combined)  # flattened at the seam (#1475)
    lines = [f"HEAD after:  {head_after or '?'} ✗"]
    if head_after and head_before and head_after == head_before:
        lines.append("Status: COMMIT NOT APPLIED (HEAD unchanged)")
    else:
        lines.append(f"Status: commit returned exit {result.returncode}")
    if err:
        lines.append(f"First error: {err}")
    lines.append("")
    lines.extend(relayed_block(combined))
    lines.append("")
    lines.append("Bypass hooks (only if intentional): "
                 "git commit --no-verify -m '...'")
    return lines


if __name__ == "__main__":
    sys.exit(main())
