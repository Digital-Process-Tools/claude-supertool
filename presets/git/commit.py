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

from _git_common import (  # noqa: E402
    _first_error_line,
    _git,
    query_open_mr,
    repo_label,
    st_hint,
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


def _colon_split_refusal(msg, paths, spilled):
    """The error printed instead of guessing what the caller meant (#751).

    Reconstructs the message the caller almost certainly typed — the leading
    run of spilled segments, rejoined on ':' — and hands back both routes that
    carry a ':' intact. Nothing is staged and nothing is committed.

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
    rebuilt = ":".join([msg] + head)

    suggestion = "git-commit:::" + rebuilt
    if rest:
        suggestion += ":::" + ":::".join(rest)

    lines = [
        "ERROR: commit message was split on ':' — nothing staged, nothing committed.",
        "  Parsed as: message=%r" % (msg,),
    ]
    for p in paths:
        why = " (not a path, and unknown to git)" if p in spilled else ""
        lines.append("             path=%r%s" % (p, why))
    lines += [
        "  supertool's single-colon CLI splits on every ':', so a Conventional",
        "  Commits subject cannot survive it. Use a route that does not tokenize:",
        "    ./supertool " + _sh_quote(suggestion),
        "  or, for paths with spaces or a multi-line body:",
        "    ./supertool 'git-commit:@-' <<'EOF'",
        "    message = " + _TRIPLE + rebuilt + _TRIPLE,
        "    paths = ["
        + ", ".join(_toml_basic(p) for p in rest or ["path/to/file"])
        + "]",
        "    EOF",
    ]
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
    """git add refused a pathspec — say what a well-formed one looks like (#986).

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
    """
    lines = [
        "ERROR: git add failed: %s"
        % (add.stderr.strip() or add.stdout.strip(),),
        "  " + _SEPARATOR_NOTE,
    ]
    split, guessed = [], False
    for p in to_add:
        parts = [x for x in p.split(",") if x]
        if len(parts) > 1 and not _known_to_git(p, set()):
            guessed = True
            split.extend(parts)
        else:
            split.append(p)
    if guessed:
        lines.append("  A ',' above is not a separator here. Did you mean:")
        if len(split) > _LIST_CAP:
            lines += _sample(split)
        lines += _colon_remedy(split[:_LIST_CAP], len(split))
    return lines


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
    r = run(["status", "--porcelain=v1", "-z"])
    if r.returncode != 0:
        said = " ".join((r.stderr or "").split())[:120]
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


def _payload_remedy(shown, total=None):
    """The `@-` form, spelled with its `paths` key (#986).

    Pointing only at the colon form answers a caller who may be on the
    payload route precisely *because* their message will not survive
    ':'-tokenization. Both routes stage; both are named here, so the reader
    picks rather than being told they used the wrong one.

    Same *shown*/*total* contract as _colon_remedy, and for the same reason:
    a `paths = [...]` array holding a subset of what was listed is the #963
    defect in TOML.
    """
    total = len(shown) if total is None else total
    if shown and total <= len(shown):
        arr = ", ".join(_toml_basic(p) for p in shown)
    else:
        arr = _toml_basic("path/to/file")
    return [
        "  or, for a multi-line message — this route stages too, via `paths`:",
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


def _nothing_staged_lines():
    """The refusal, with the list the op was already holding (#1003).

    The refusal itself stays: committing files the caller did not name is not
    a default anyone wants. What changes is that the remedy stops being a raw
    `git add -A` guessed from an empty error.
    """
    modified, untracked, unknown = _worktree_changes()
    lines = ["ERROR: nothing staged."]
    if unknown:
        lines.append(f"  What is unstaged is UNKNOWN — `git status` did not "
                     f"answer ({unknown}).")
        lines.append("  Stage explicitly: git-commit:::MESSAGE:::PATHS")
        return lines
    if not modified and not untracked:
        lines.append("  Working tree is clean — there is nothing to stage.")
        return lines
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
    git_dir_res = _git(["rev-parse", "--git-dir"])
    if git_dir_res.returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    head_before = _head_sha()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    if no_edit:
        gd = git_dir_res.stdout.strip()
        in_merge = bool(gd) and (
            os.path.exists(os.path.join(gd, "MERGE_HEAD"))
            or os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD"))
        )
        if not in_merge:
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

    # Stage PATHS if given. A path that's already a staged deletion (gone from
    # disk after `git rm`) would make `git add` abort with "pathspec did not
    # match any files" — so drop those from the add list; their deletion is
    # already staged and will be committed (issue #324). Genuinely-unknown
    # paths stay in the list, so they still error as before.
    if paths:
        deleted = _git(["diff", "--cached", "--diff-filter=D", "--name-only"])
        staged_deletions = {l for l in deleted.stdout.splitlines() if l.strip()}
        # #751 — a PATH that is neither path-shaped nor known to git is far more
        # likely the tail of a ':'-split message than a file. Refuse before
        # anything is staged; do NOT fold it back into the message, because a
        # wrong guess in that direction commits whatever was already staged
        # under a mangled subject and prints a success receipt for it.
        spilled = _spilled_message_paths(paths, staged_deletions)
        if spilled:
            print(_colon_split_refusal(msg, paths, spilled))
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
    staged = _git(["diff", "--cached", "--name-only"])
    if staged.returncode != 0 or not staged.stdout.strip():
        for line in _nothing_staged_lines():
            print(line)
        return 1
    staged_files = [l for l in staged.stdout.splitlines() if l.strip()]

    # Commit
    if no_edit:
        result = _git(["commit", "--no-edit"], timeout=_COMMIT_TIMEOUT)
    else:
        result = _git(["commit", "-m", _with_coauthor(msg)],
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
        for f in staged_files[:20]:
            print(f"  {f}")
        if len(staged_files) > 20:
            print(f"  … {len(staged_files) - 20} more")
        # #1016 — the tick above argues nothing was left out. Say so only when
        # that is established, and say the opposite when it is not.
        for line in _left_behind_lines():
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
    print(f"HEAD after:  {head_after or '?'} ✗")
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    err = _first_error_line(combined)

    if head_after and head_before and head_after == head_before:
        print("Status: COMMIT NOT APPLIED (HEAD unchanged)")
    else:
        print(f"Status: commit returned exit {result.returncode}")

    if err:
        print(f"First error: {err}")
    print("\n--- git output ---")
    print(combined.strip() or "(no output)")
    print("\nBypass hooks (only if intentional): git commit --no-verify -m '...'")
    return result.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
