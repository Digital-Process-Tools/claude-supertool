#!/usr/bin/env python3














































from __future__ import annotations

import os
import re
import sys



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _untrusted  
from _git_common import (  
    NOT_A_REPO,
    TIMEOUT_RC,
    _first_error_line,
    _git,
    probe_repo,
    query_last_mr_result,
    query_open_mr,
    relayed_block,
    repo_label,
    st_hint,
    unanswered_repo_lines,
    use_utf8_stdout,
)







_COMMIT_TIMEOUT = 30


def _existing_mr_for_branch(branch: str) -> str:




    mr = query_open_mr(branch)
    if not mr:
        return ""
    prefix = "!" if mr["source"] == "gitlab" else "#"
    return f"{prefix}{mr['iid']}"


def _push_hint_when_no_open_mr(branch: str) -> str:










    base = "./supertool 'git-push' (or ./supertool 'mr:.max/mr.md|TIME|LABELS' for push+MR)"
    last = query_last_mr_result(branch)
    if not last.answered:
        reason = _untrusted.flat(str(last.reason))
        return f"{base} -- whether this branch ever had a request is UNKNOWN ({reason})"
    mr = last.mr
    if not mr:
        return base
    raw_state = mr.get("state")
    state = (raw_state.lower() if isinstance(raw_state, str)
             else "state unknown (the tracker row carried none)")
    sigil = "!" if mr["source"] == "gitlab" else "#"
    return (f"{base} -- {sigil}{mr['iid']} ({state}) was this branch's "
            f"request; these commits are not on it")


def _head_sha() -> str:
    r = _git(["rev-parse", "--short", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""





_DEFAULT_COAUTHOR = "Max <noreply>"
_DISABLE_VALUES = {"", "none", "off", "false", "no", "0"}


def _coauthor_value() -> str:






    raw = os.environ.get("SUPERTOOL_COAUTHOR")
    val = _DEFAULT_COAUTHOR if raw is None else raw
    return "" if val.strip().lower() in _DISABLE_VALUES else val.strip()


def _with_coauthor(msg: str) -> str:





    identity = _coauthor_value()
    if not identity:
        return msg
    if any(l.strip().lower().startswith("co-authored-by:")
           for l in msg.splitlines()):
        return msg
    trailer = f"Co-Authored-By: {identity}"
    body = msg.rstrip("\n")
    return f"{body}\n\n{trailer}"





_PROSE_CHARS = (" ", "\t", "\n", "\r", '"', "'")




_TRIPLE = "'" * 3







_NO_VERIFY_TOKEN = "--no-verify"
















_MESSAGE_WHITESPACE_OK = "\n\t\r"


def _surrogateescaped_c1_byte(ch: str):
















    o = ord(ch)
    if 0xDC80 <= o <= 0xDC9F:
        return o - 0xDC00
    return None


def _control_byte_hazard(msg: str):





















    for i, ch in enumerate(msg):
        if ch in _MESSAGE_WHITESPACE_OK:
            continue
        if _untrusted._is_control(ch):
            kind = ("line separator" if ch == "\u2028" else
                    "paragraph separator" if ch == "\u2029" else
                    "control byte")
            return i, _untrusted.visible(ch), kind
        orig = _surrogateescaped_c1_byte(ch)
        if orig is not None:
            return i, "0x%02X (an invalid-UTF-8 byte)" % orig, "control byte"
    return None


def _control_byte_refusal(msg: str, index: int, display: str, kind: str):















    if kind == "control byte":
        cause = (
            "  This is never something a caller means to commit. A shell "
            "quoting form like $'...' turns escape TEXT (\\x1b) into a real "
            "byte like this one, and some invocation paths (an interactive "
            "pty) then read it as a control sequence and silently drop part "
            "of the line -- which is how a PATH named right after it can "
            "vanish before this parser ever runs."
        )
    else:
        cause = (
            "  This is never something a caller means to commit -- it is an "
            "invisible line break, not the ordinary newline a message body "
            "already uses, and usually arrives pasted from a web page or a "
            "document editor without anyone seeing it land."
        )
    return [
        "ERROR: MESSAGE holds a raw %s (%s at character %d) -- refused "
        "before anything was staged, nothing committed (#2592)."
        % (kind, display, index),
        cause,
        "  Parsed as: message=%r (intact -- this call reached commit.py "
        "whole)" % (msg,),
        "  Remove the stray %s and retype the message; it belongs in no "
        "commit message, on either route." % (kind,),
    ]











_LEAKED_KEY_RE = re.compile(r"^\s*(?:(paths)\s*=\s*\[|(message)\s*=)")


def _leaked_key_hazard(msg: str):


















    first_line = msg.split("\n", 1)[0]
    m = _LEAKED_KEY_RE.match(first_line)
    if m is None:
        return None


    return "paths = [" if m.group(1) else "message = "













_TRAILING_PATHS_RE = re.compile(r"^[ \t]*paths[ \t]*=[ \t]*\[")


def _trailing_paths_hazard(msg: str, paths):


    if paths:
        return False
    body_lines = [ln for ln in msg.splitlines() if ln.strip()]
    if not body_lines:
        return False
    return _TRAILING_PATHS_RE.match(body_lines[-1]) is not None


def _trailing_paths_refusal(msg: str):


    hint = st_hint("git-commit:@-")
    lines = [
        "ERROR: the commit message's last line looks like a `paths = "
        "[...]` payload field -- refused before anything was staged, "
        "nothing committed (#2667).",
        "  No PATHS were given on the argument list either, which is the "
        "shape a `message = @rest` tail takes when it swallows a `paths` "
        "line meant as its own field, rather than as part of the message.",
        "  Parsed as: message=%r (intact -- this call reached commit.py "
        "whole)" % (msg,),
        "  Put `paths = [...]` BEFORE the `message = @rest` marker line, "
        "as its own header field, not after it:",
    ]
    if hint.startswith("("):
        lines.append("  " + hint)
    else:
        lines.append("    " + hint + " <<'EOF'")
        lines.append("    paths = [\"a.py\"]")
        lines.append("    message = @rest")
        lines.append("    your subject line")
        lines.append("    EOF")
    return lines


def _leaked_key_refusal(msg: str, marker: str):

    hint = st_hint("git-commit:@-")
    lines = [
        "ERROR: the commit subject contains %r -- refused before anything "
        "was staged, nothing committed (#2656)." % (marker,),
        "  This is never a subject a caller means to commit -- it is a "
        "payload key (`paths = [...]` or `message = ...`) that landed in "
        "MESSAGE instead of being read as its own field.",
        "  Parsed as: message=%r (intact -- this call reached commit.py "
        "whole)" % (msg,),
        "  Re-send the payload with the key on its own line, outside the "
        "message value:",
    ]
    if hint.startswith("("):






        lines.append("  " + hint)
        lines.append("  The payload shape, once a runnable supertool is "
                      "found:")
    else:
        lines.append("    " + hint + " <<'EOF'")
    lines.append("    message = " + _TRIPLE + "<subject>" + _TRIPLE)
    lines.append("    paths = [\"path/to/file\"]")
    if not hint.startswith("("):
        lines.append("    EOF")
    return lines


def _no_verify_ambiguous_refusal():









    return [
        "ERROR: %r is both this op's hook-skip opt-in and a path git "
        "knows — nothing staged, nothing committed." % (_NO_VERIFY_TOKEN,),
        ("  Nothing in the argument says which was meant, and both routes "
         + "spell it the same way."),
        "  To commit the FILE, write it so git reads it as a path:",
        "    " + st_hint("git-commit:::MESSAGE:::./" + _NO_VERIFY_TOKEN),
        "  To skip hooks too, name it explicitly alongside the sentinel:",
        "    " + st_hint("git-commit:::MESSAGE:::./" + _NO_VERIFY_TOKEN
                          + ":::" + _NO_VERIFY_TOKEN),
    ]


def _looks_like_pathspec(tok: str) -> bool:

    return bool(tok) and tok == tok.strip() and not any(
        c in tok for c in _PROSE_CHARS
    )


def _known_to_git(path: str, staged_deletions: set) -> bool:






    if os.path.exists(path) or path in staged_deletions:
        return True
    r = _git(["ls-files", "--", path])
    return r.returncode == 0 and bool(r.stdout.strip())


def _spilled_message_paths(paths, staged_deletions):






    return [
        p for p in paths
        if not _looks_like_pathspec(p) and not _known_to_git(p, staged_deletions)
    ]


def _arg_separator():







    return os.environ.get("SUPERTOOL_ARG_SEP", ":")


def _payload_fields_refusal(msg, paths, spilled, rest):












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







_AMEND_WORDS = {"amend", "--amend"}
_ALLOW_LITERAL_AMEND = "SUPERTOOL_ALLOW_LITERAL_AMEND"


def _amend_refusal(msg):











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







    raw = os.environ.get("SUPERTOOL_ALLOW_LITERAL_AMEND", "")
    return raw.strip().lower() not in _DISABLE_VALUES


def _add_failure_lines(add, to_add):

































    lines = [


        "ERROR: git add failed: %s"
        % (_untrusted.flat(add.stderr.strip() or add.stdout.strip()),),
    ]
    if _arg_separator() == "":
        return lines + _payload_route_add_lines(to_add)
    return lines + _colon_route_add_lines(to_add)


def _colon_route_add_lines(to_add):













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






_PAYLOAD_JOINS = (":::", ",")


def _payload_route_add_lines(to_add):





















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










_ALL_TOKEN = "--all"


def _all_with_paths_refusal(paths):







    others = [p for p in paths if p != _ALL_TOKEN]
    if not others:




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





        said = _untrusted.flat(" ".join((idx.stderr or "").split())[:120])
        return [], [
            "ERROR: %s could not be resolved — the index could not be read "
            "(%s). Nothing staged, nothing committed."
            % (_ALL_TOKEN, said or "exit %d" % idx.returncode),
            "  Already-staged paths are part of what %s means, so the list "
            "would be incomplete." % (_ALL_TOKEN,),
            "  Name the paths explicitly: git-commit:::MESSAGE:::PATHS",
        ]


    return list(dict.fromkeys(modified + untracked + _z_paths(idx.stdout))), []


def _worktree_changes(git_fn=None):


















    run = _git if git_fn is None else git_fn










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










    return [p for p in stdout.split(chr(0)) if p != ""]







_LIST_CAP = 20

_SEPARATOR_NOTE = "Paths are separated by ':::' — not by commas, not by spaces."


def _sh_quote(word):







    return chr(39) + word.replace(chr(39), chr(39) + chr(34) + chr(39) + chr(34) + chr(39)) + chr(39)


def _toml_basic(value):







    out = value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))
    for raw, esc in ((chr(10), "n"), (chr(13), "r"), (chr(9), "t")):
        out = out.replace(raw, chr(92) + esc)
    return chr(34) + out + chr(34)


def _sample(paths, cap=_LIST_CAP):

    lines = ["    " + p for p in paths[:cap]]
    if len(paths) > cap:
        lines.append(f"    … {len(paths) - cap} more")
    return lines


def _colon_remedy(shown, total=None, message="MESSAGE"):





















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










    run = _git if git_fn is None else git_fn
    res = run(["diff", "--cached", "--name-only", "--no-renames", "-z"])
    if res.returncode != 0:
        return []
    return _z_paths(res.stdout)


def _still_staged_lines(git_fn=None):














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





















    modified, untracked, unknown = _worktree_changes()
    if unknown:
        return [
            "ERROR: nothing staged, and what is unstaged is UNKNOWN — "
            f"`git status` did not answer ({unknown}).",
            "  Stage explicitly: git-commit:::MESSAGE:::PATHS",
        ]






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



    shown = modified[:_LIST_CAP] + untracked[:_LIST_CAP]
    total = len(modified) + len(untracked)
    lines.append("  Commit the ones you mean, by name:")
    lines += _colon_remedy(shown, total)
    lines += _payload_remedy(shown, total)




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



















    no_verify = _NO_VERIFY_TOKEN in paths
    no_verify_would_empty = no_verify and all(
        p == _NO_VERIFY_TOKEN for p in paths
    )
    if no_verify:
        paths = [p for p in paths if p != _NO_VERIFY_TOKEN]

    if not no_edit and not msg.strip():
        print("ERROR: commit message is empty.")
        return 1




    if not no_edit:
        hazard = _control_byte_hazard(msg)
        if hazard is not None:
            for line in _control_byte_refusal(
                    msg, hazard[0], hazard[1], hazard[2]):
                print(line)
            return 1







    if not no_edit:
        marker = _leaked_key_hazard(msg)
        if marker is not None:
            for line in _leaked_key_refusal(msg, marker):
                print(line)
            return 1





        if _trailing_paths_hazard(msg, paths):
            for line in _trailing_paths_refusal(msg):
                print(line)
            return 1











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


    print(f"Repo: {repo_label()}")
    print(f"HEAD before: {head_before}")









    if no_verify_would_empty:
        gone = _git(["diff", "--cached", "--diff-filter=D", "--name-only",
                     "--no-renames", "-z"])
        if gone.returncode != 0:




            said = _untrusted.flat(
                " ".join((gone.stderr or "").split())[:120])
            print("ERROR: %s could not be resolved — the index could not be "
                  "read (%s). Nothing staged, nothing committed."
                  % (_NO_VERIFY_TOKEN, said or "exit %d" % gone.returncode))
            print("  Whether a file named %r is a staged deletion is exactly "
                  "what decides the ambiguity below, and it is now unknown."
                  % (_NO_VERIFY_TOKEN,))
            return 1
        gone_set = set(_z_paths(gone.stdout))
        if _known_to_git(_NO_VERIFY_TOKEN, gone_set):
            for line in _no_verify_ambiguous_refusal():
                print(line)
            return 1





    if no_verify:
        print("HOOKS SKIPPED — --no-verify was passed; "
              "pre-commit/commit-msg hooks did not run.")





    if (not no_edit and msg.strip().lower() in _AMEND_WORDS
            and not _literal_amend_allowed()):
        for line in _amend_refusal(msg):
            print(line)
        return 1





    named = len(paths)
    all_used = False
    if _ALL_TOKEN in paths:
        if len(paths) > 1:
            for line in _all_with_paths_refusal(paths):
                print(line)
            return 1





        gone = _git(["diff", "--cached", "--diff-filter=D", "--name-only",
                     "--no-renames", "-z"])
        if gone.returncode != 0:





            said = _untrusted.flat(
                " ".join((gone.stderr or "").split())[:120])
            print("ERROR: %s could not be resolved — the index could not be "
                  "read (%s). Nothing staged, nothing committed."
                  % (_ALL_TOKEN, said or "exit %d" % gone.returncode))
            print("  Whether a file named %r is a staged deletion is exactly "
                  "what decides the ambiguity below, and it is now unknown."
                  % (_ALL_TOKEN,))
            return 1
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


        all_used = bool(paths)
        named = len(paths)






    if paths:






        deleted = _git(["diff", "--cached", "--diff-filter=D", "--name-only",
                        "--no-renames", "-z"])
        staged_deletions = set(_z_paths(deleted.stdout))





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














    scope = ["--"] + paths if (paths and not in_merge) else []
    staged = _git(["diff", "--cached", "--name-only", "--no-renames", "-z"] + scope)













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













    verify_flag = ["--no-verify"] if no_verify else []
    if no_edit:
        result = _git(["commit"] + verify_flag + ["--no-edit"],
                      timeout=_COMMIT_TIMEOUT)
    else:
        result = _git(["commit"] + verify_flag
                      + ["-m", _with_coauthor(msg)] + scope,
                      timeout=_COMMIT_TIMEOUT)
    head_after = _head_sha()

    if result.returncode == 0 and head_after and head_after != head_before:
        new_sha = head_after
        print(f"HEAD after:  {new_sha} ✓")

        stat = _git(["show", "--shortstat", "--format=", new_sha])
        if stat.returncode == 0 and stat.stdout.strip():
            print(stat.stdout.strip().splitlines()[-1].strip())
        print(f"Files committed: {len(staged_files)}")





        listed = staged_files if all_used else staged_files[:20]
        for f in listed:
            print(f"  {f}")
        if len(staged_files) > len(listed):
            print(f"  … {len(staged_files) - len(listed)} more")


        for line in _left_behind_lines():
            print(line)








        for line in _still_staged_lines():
            print(line)

        upstream_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if upstream_res.returncode == 0 and upstream_res.stdout.strip():
            existing = _existing_mr_for_branch(branch)
            if existing:
                print(f"Next: git push (updates {existing})")
            else:




                print(f"Next: {_push_hint_when_no_open_mr(_untrusted.flat(branch))}")
        else:
            print("Next: git push -u origin HEAD (no upstream set)")
        return 0


    for line in _failure_receipt(result, head_before, head_after):
        print(line)
    return result.returncode or 1


def _failure_receipt(result, head_before: str, head_after: str) -> list:

















    combined = (result.stdout or "") + chr(10) + (result.stderr or "")
    err = _first_error_line(combined)  
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



    lines.append("Bypass hooks (only if intentional): add --no-verify, e.g. "
                 + st_hint("git-commit:::MESSAGE:::--no-verify")
                 + " (or no_verify = true on the @payload route)")
    return lines


if __name__ == "__main__":
    sys.exit(main())
