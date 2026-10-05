#!/usr/bin/env python3




































































from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from typing import Optional



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _untrusted  
from _git_common import (  
    GIT_OUTPUT_HEAD_LINES as _GIT_OUTPUT_HEAD_LINES,
    GIT_OUTPUT_TAIL_LINES as _GIT_OUTPUT_TAIL_LINES,
    NOT_A_REPO,
    TIMEOUT_RC,
    MrLookup,
    _first_error_line,
    _git,
    bounded_lines,
    probe_repo,
    install_dir,
    query_last_mr_result,
    query_open_mr_result,
    reject_fetch_option,
    relayed_block,
    relayed_lines as _relayed_lines,
    repo_label,
    st_hint,
    unanswered_repo_lines,
    use_utf8_stdout,
)














_KNOWN_FLAGS = ("force-with-lease", "no-verify", "watch",
                "set-upstream", "to-upstream")






































_PUSH_TIMEOUT = 300
_PUSH_TIMEOUT_MAX = 1800
























_BUDGET: dict[str, object] = {"seconds": None, "deadline": None,
                              "allowed": None, "source": ""}


def _push_budget() -> int:

    asked = _BUDGET["seconds"]
    return _PUSH_TIMEOUT if asked is None else int(asked)


def _open_push_deadline() -> int:





























    seconds = _push_budget()
    _BUDGET["deadline"] = time.monotonic() + seconds
    _BUDGET["allowed"] = seconds
    return seconds


def _budget_left() -> int:









    deadline = _BUDGET["deadline"]
    if deadline is None:
        return _push_budget()
    return max(0, int(float(deadline) - time.monotonic()))  


def _push_allowed() -> int:







    allowed = _BUDGET["allowed"]
    return _push_budget() if allowed is None else int(allowed)





_RECOVER_TIMEOUT = 120


def _recover_allowance():





















    left = _budget_left()
    if left < _RECOVER_MIN:
        return 0
    return min(_RECOVER_TIMEOUT, left)


def _repush_allowance() -> int:











    left = _budget_left()
    if left < _RECOVER_MIN:
        _BUDGET["allowed"] = 0
        return 0
    _BUDGET["allowed"] = left
    return left






_CHECK_TIMEOUT = 30



















_RECOVER_MIN = _CHECK_TIMEOUT








_RUN = {
    "phase": "not-attempted",   
    "branch": "",
    "remote": "",
    "ref": "",
    "target": "",
    "verdict": False,
}


def _note_landed(branch: str, remote: str, ref: str) -> None:







    _RUN.update({"phase": "landed", "branch": branch, "remote": remote,
                 "ref": ref, "target": f"{remote}/{ref}"})


def _checked_git(args: list[str], label: str = "") -> tuple[
        Optional[subprocess.CompletedProcess[str]], str]:























    cmd = label or "git " + " ".join(args)
    try:
        r = _git(args, timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return None, f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return None, (f"`{cmd}` did not complete — "
                      + _untrusted.flat(r.stderr.strip()))
    if r.returncode != 0:
        why = _untrusted.flat(
            _first_error_line((r.stdout or "") + "\n" + (r.stderr or "")))
        return None, (f"`{cmd}` exited {r.returncode}"
                      + (f" — {why}" if why else ""))
    return r, ""


def _split_flags(argv: list[str]) -> tuple[set[str], list[str]]:









    known: set[str] = set()
    unknown: list[str] = []
    for tok in argv:
        t = tok.strip().lower()
        if not t:
            continue
        if t in _KNOWN_FLAGS:
            known.add(t)
        elif t.startswith(_BUDGET_PREFIX):





            continue
        else:
            unknown.append(tok.strip())
    return known, unknown


def _parse_flags(argv: list[str]) -> set[str]:

    return _split_flags(argv)[0]




_BUDGET_PREFIX = "budget="




_BUDGET_DIGITS = re.compile(r"^-?[0-9]+\Z")


def _parse_budget(argv: list[str]) -> tuple[Optional[int], str]:

























    seen: list[tuple[str, int]] = []
    for tok in argv:
        t = tok.strip().lower()
        if not t.startswith(_BUDGET_PREFIX):
            continue
        raw = t[len(_BUDGET_PREFIX):].strip()







        if not _BUDGET_DIGITS.match(raw):
            return None, (f"`{tok.strip()}` — the budget must be a whole "
                          f"number of seconds"
                          + (f", not `{raw}`" if raw else " and this one is empty"))
        seconds = int(raw)
        if seconds <= 0:
            return None, (f"`{tok.strip()}` — the budget must be a positive "
                          "number of seconds")
        breach = _budget_ceiling_refusal(seconds, f"`{tok.strip()}`")
        if breach:
            return None, breach
        seen.append((tok.strip(), seconds))
    if not seen:
        return None, ""
    values = {s for _tok, s in seen}
    if len(values) > 1:
        listed = ", ".join(f"`{tok}`" for tok, _s in seen)
        return None, (f"two different budgets were asked for ({listed}) — "
                      "pick one. Neither is preferred over the other and "
                      "choosing for you would be the guess this op refuses "
                      "everywhere else.")
    return seen[0][1], ""



























_OP_NAME = "git-push"
_CONFIG_BUDGET_KEY = "budget"
_PRESET_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "git.json")


_UNREADABLE = object()


def _read_json(path: str) -> object:








    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return _UNREADABLE


def _ops_entry(data: object) -> dict:

    if not isinstance(data, dict):
        return {}
    ops = data.get("ops")
    if not isinstance(ops, dict):
        return {}
    entry = ops.get(_OP_NAME)
    return dict(entry) if isinstance(entry, dict) else {}


def _merged_op_entry() -> dict:








    entry = _ops_entry(_read_json(_PRESET_JSON))
    directory = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(directory, ".supertool.json")
        if os.path.isfile(candidate):
            data = _read_json(candidate)
            if data is not _UNREADABLE:
                entry.update(_ops_entry(data))
                return entry
        parent = os.path.dirname(directory)
        if parent == directory:
            return entry
        directory = parent


def _budget_ceiling_refusal(seconds: int, subject: str,
                            entry: Optional[dict] = None) -> str:

























    entry = _merged_op_entry() if entry is None else entry
    cap_key = f"ops.{_OP_NAME}.timeout"
    if seconds > _PUSH_TIMEOUT_MAX:
        return (f"{subject} — the most this op can wait is "
                f"{_PUSH_TIMEOUT_MAX}s. It is not clamped to that: a budget "
                f"has to stay strictly under {cap_key}, because past that cap "
                f"supertool kills this process and a killed push can verify "
                f"nothing (#399) — and {_PUSH_TIMEOUT_MAX}s is separately the "
                f"longest this op will make you wait, which raising {cap_key} "
                f"does not change. Ask for less, or raise both.")
    cap = entry.get("timeout")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return (f"{subject}, but {cap_key} did not read as a positive whole "
                f"number of seconds ({_untrusted.flat(repr(cap))}), so the "
                f"budget could not be checked against it. Refused rather than "
                f"assumed safe: a budget that is not strictly under the op "
                f"timeout is killed by supertool's outer cap, and a killed "
                f"push can verify nothing (#399).")
    if seconds >= cap:
        return (f"{subject} and {cap_key} is {cap}s — the budget must be "
                f"strictly UNDER the op timeout, because past that cap "
                f"supertool kills this process and a killed push can verify "
                f"nothing (#399). Raise {cap_key} above {seconds}s, or ask for "
                f"less than {cap}s.")
    return ""


def _config_budget() -> tuple[Optional[int], str]:
















    entry = _merged_op_entry()
    raw = entry.get(_CONFIG_BUDGET_KEY)
    if raw is None:
        return None, ""
    key = f"ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY}"
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, (f"{key} is {_untrusted.flat(repr(raw))} — the budget must "
                      f"be a whole number of seconds, written as a JSON number.")
    if raw <= 0:
        return None, (f"{key} is {raw} — the budget must be a positive number "
                      f"of seconds.")
    breach = _budget_ceiling_refusal(raw, f"{key} is {raw}s", entry)
    if breach:
        return None, breach
    return raw, ""


def _st_hint(arg: str) -> str:




















    return st_hint(arg)


def _upstream_ref() -> tuple[str, str]:












    cmd = "git rev-parse --abbrev-ref --symbolic-full-name @{upstream}"
    try:
        r = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  "@{upstream}"], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _remote_sha(ref: str) -> tuple[str, str]:







    if not ref:
        return "", ""
    cmd = f"git rev-parse --short {ref}"
    try:
        r = _git(["rev-parse", "--short", ref], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _local_head() -> tuple[str, str]:








    r, why = _checked_git(["rev-parse", "HEAD"], "git rev-parse HEAD")
    return ("", why) if r is None else (r.stdout.strip(), "")


def _live_remote_sha(remote: str, ref: str) -> tuple[str, str]:












    if not remote or not ref:
        return "", ""
    cmd = f"git ls-remote {remote} {ref}"
    r, why = _checked_git(["ls-remote", remote, ref], cmd)
    if r is None:
        return "", why
    if r.stdout.strip():
        return r.stdout.split()[0], ""
    return "", f"`{cmd}` returned no ref — the remote does not have {ref}"


def _split_upstream(upstream: str, branch: str,
                    fallback_remote: str) -> tuple[str, str]:











    if "/" in upstream:
        remote, ref = upstream.split("/", 1)
        return remote, ref
    return fallback_remote, branch






_PUSH_REMOTE_KEYS = ("branch.{b}.pushRemote", "remote.pushDefault",
                     "branch.{b}.remote")


def _config_value(key: str) -> tuple[str, str]:






    cmd = f"git config --get {key}"
    try:
        r = _git(["config", "--get", key], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return "", f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return "", f"`{cmd}` did not complete — {r.stderr.strip()}"
    return (r.stdout.strip(), "") if r.returncode == 0 else ("", "")


def _remote_names() -> tuple[list[str], str]:








    cmd = "git remote"
    try:
        r = _git(["remote"], timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        return [], f"`{cmd}` did not complete — {exc}"
    if r.returncode == TIMEOUT_RC:
        return [], f"`{cmd}` did not complete — {r.stderr.strip()}"
    if r.returncode != 0:
        return [], f"`{cmd}` exited {r.returncode} — {r.stderr.strip()}"
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()], ""


def _resolve_push_remote(branch: str) -> tuple[str, str, str]:





























    for key in (k.format(b=branch) for k in _PUSH_REMOTE_KEYS):
        value, why = _config_value(key)
        if why:
            return "", "", why
        if value:
            return value, f"configured in {key}", ""
    names, why = _remote_names()
    if why:
        return "", "", why
    if not names:
        return "", "", "this repository has no remote configured"
    if "origin" in names:
        return "origin", "the remote named origin", ""
    if len(names) == 1:
        return names[0], "the only remote in this repository", ""
    return "", "", (f"this repository has {len(names)} remotes and none of "
                    f"them is named origin: {', '.join(names)}")


def _refuse_unresolved_remote(branch: str, why: str) -> int:













    print(f"ERROR: cannot determine which remote to push {branch} to — {why}")
    print("Nothing was pushed. Name the remote once, either way:")
    print("  git push -u <remote> HEAD")
    print(f"  git config branch.{branch}.remote <remote>   "
          "# then re-run git-push")
    _result(f"NOT PUSHED - no push attempted (cannot determine the push "
            f"remote for {branch}: {why})")
    return 1


def _refuse_mismatched_upstream(branch: str, remote_name: str,
                                remote_ref: str) -> int:



































    print(f"# git-push on {branch}")
    print(f"Upstream: {remote_name}/{remote_ref} — a different branch, "
          f"not {branch} itself")
    print(f"ERROR: {branch}'s upstream is {remote_name}/{remote_ref}. A "
          "bare push here is ambiguous — this is the exact state that "
          "makes git itself refuse with 'the upstream branch of your "
          "current branch does not match the name of your current "
          "branch', not a remote rejection.")
    print("Nothing was pushed. Name the target once:")
    print(f"  {_st_hint('git-push:set-upstream')}"
          f"   # push {branch} under its own name, tracking "
          f"{remote_name}/{branch} (the usual first push)")
    print(f"  {_st_hint('git-push:to-upstream')}"
          f"    # push onto {remote_name}/{remote_ref} on purpose, if that "
          f"is the real target")
    _result(f"NOT PUSHED - no push attempted ({branch}'s upstream is "
            f"{remote_name}/{remote_ref}, a different branch — ambiguous "
            f"target, nothing pushed)")
    return 1


def _refuse_conflicting_targets(branch: str, remote_name: str,
                                remote_ref: str) -> int:










    print(f"# git-push on {branch}")
    print("ERROR: :set-upstream and :to-upstream name different targets — "
          f"{remote_name}/{branch} and {remote_name}/{remote_ref}. Asking "
          "for both is not a push this op can order by precedence.")
    print("Nothing was pushed. Pick one:")
    print(f"  {_st_hint('git-push:set-upstream')}"
          f"   # push {branch} under its own name")
    print(f"  {_st_hint('git-push:to-upstream')}"
          f"    # push onto {remote_name}/{remote_ref}")
    _result(f"NOT PUSHED - no push attempted (:set-upstream and :to-upstream "
            f"name different targets: {remote_name}/{branch} vs "
            f"{remote_name}/{remote_ref})")
    return 2





_NFF_SUMMARIES = ("non-fast-forward", "fetch first",
                  "tip of your current branch is behind")


def _ref_line(push_stdout: str, ref: str) -> tuple[str, str]:






    want = ref.rsplit("/", 1)[-1]
    for line in push_stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        if parts[1].split(":", 1)[-1].rsplit("/", 1)[-1] != want:
            continue
        return parts[0], parts[2].strip()
    return "", ""


def _push_outcome(push_stdout: str, ref: str) -> tuple[str, str, str]:














    flag, summary = _ref_line(push_stdout, ref)
    if not flag:
        return "unknown", "", "git reported no per-ref status line for it"
    if "[new branch]" in summary:
        return "created", "", ""
    if flag == "=" or "[up to date]" in summary:
        return "uptodate", "", ""
    if flag == "+":
        if "..." in summary:
            return "forced", summary.split("...", 1)[0].strip(), ""
        return "unknown", "", ("git reported a forced update of it without the "
                               f"SHA it overwrote (`{summary}`)")
    if ".." in summary:
        return "updated", summary.split("..", 1)[0].strip(), ""
    return "unknown", "", f"git's per-ref summary for it was `{summary}`"


def _forced_update_old_sha(push_stdout: str, ref: str) -> str:















    kind, old, _ = _push_outcome(push_stdout, ref)
    return old if kind == "forced" else ""


def _ref_status(push_stdout: str, ref: str) -> str:
























    flag, summary = _ref_line(push_stdout, ref)
    return summary if flag == "!" else ""


def _is_non_fast_forward(push_stdout: str, ref: str) -> bool:






    status = _ref_status(push_stdout, ref)
    if not status.lower().startswith("[rejected]"):
        return False
    low = status.lower()
    return any(marker in low for marker in _NFF_SUMMARIES)


def _result(verdict: str) -> None:














    _RUN["verdict"] = True
    print(f"[result] {verdict}")


def _push_verdict(moved: bool, branch: str, remote: str, ref: str,
                  tracking_sha: str, ncommits: str,
                  force_note: str = "") -> None:

















    live, live_why = _live_remote_sha(remote, ref)
    head, head_why = _local_head()
    target = f"{remote}/{ref}"
    if live:
        sha = live[:7]
        if not head:
            note = ("verified against the remote; the comparison with local "
                    f"HEAD could not be made — {head_why}")
        elif live == head:
            note = "verified"
        else:
            note = "verified, but remote != local HEAD"
    else:
        sha = tracking_sha or "unknown"
        note = f"unverified - {live_why or 'remote did not answer ls-remote'}"
    if moved:
        extra = f", {ncommits} commit(s)" if ncommits else ""
        _result(f"PUSHED  {branch} -> {target} @ {sha}  ({note}{extra})"
                f"{force_note}")
    else:
        _result(f"NOT PUSHED - already up to date  {branch} -> {target} "
                f"@ {sha}  ({note}){force_note}")


def _mr_lookup(branch: str) -> MrLookup:

    return query_open_mr_result(branch)


def _open_mr_line(mr: Optional[dict]) -> str:






    if not mr:
        return ""
    target = _untrusted.flat(str(mr.get("target", "?")))
    if mr["source"] == "gitlab":
        pipe = mr.get("pipeline") or "triggered"
        if mr.get("pipeline_id"):
            pipe += f" #{mr['pipeline_id']}"
        line = f"MR !{mr['iid']} → {target} | pipeline: {pipe}"
        if mr.get("pipeline_url"):
            line += f"\n  {mr['pipeline_url']}"
        return line
    return f"PR #{mr['iid']} → {target} | checks triggered"


def _mr_conflict_line(mr: Optional[dict]) -> str:







    if not mr or mr.get("merge_status") not in (
            "cannot_be_merged", "conflict", "broken_status"):
        return ""
    target = _untrusted.flat(str(mr.get("target") or "target"))
    return (f"⚠ MR conflicts with {target} — "
            f"won't merge until rebased/resolved")


def _mr_unknown_line(lookup: MrLookup) -> str:












    if lookup.answered:
        return ""
    return (f"⚠ MR/PR LOOKUP DID NOT RUN — {lookup.reason}" + chr(10) +
            "  Whether this branch has an open MR/PR is UNKNOWN — this receipt "
            "is not saying there is none, and the mergeability and stale-base "
            "checks below are missing for the same reason. Settle it: "
            + _st_hint("git-status"))


def _dead_mr_lines(lookup: MrLookup, branch: str) -> list[str]:

















    last = query_last_mr_result(branch)
    if not last.answered:
        return [f"MR: none open for this branch -- whether it ever had one "
                f"is UNKNOWN ({last.reason})"]
    mr = last.mr
    if not mr:
        return ["MR: none -- new branch, nothing tracking it"]
    target = _untrusted.flat(str(mr.get("target", "?")))
    raw_state = mr.get("state")
    state = raw_state.lower() if isinstance(raw_state, str) else None
    when = mr.get("merged_at") or mr.get("closed_at")
    if state is None:
        when_clause = "state unknown (the tracker row carried none)"
    else:
        when_clause = f"{state} {when.split('T')[0]}" if when else state
    sigil = "!" if mr["source"] == "gitlab" else "#"
    return [
        "MR: none open for this branch",
        f"  {sigil}{mr['iid']} ({when_clause}, target {target}) was its MR "
        f"-- these commits are NOT on {target}",
    ]


def _watch_target(mr: Optional[dict]) -> Optional[tuple[str, str]]:

    if not mr or mr.get("iid") in (None, "?"):
        return None
    source = "gitlab-mr" if mr["source"] == "gitlab" else "github-pr"
    return source, str(mr["iid"])


def _prepush_hook_state(flags: set[str]) -> tuple[str, str]:


















    if "no-verify" in flags:
        return "none", "--no-verify was passed, so git skipped the local hook"
    r, why = _checked_git(["rev-parse", "--git-path", "hooks/pre-push"],
                          "git rev-parse --git-path hooks/pre-push")
    if r is None:
        return "unknown", why
    path = r.stdout.strip()
    if not path:
        return "unknown", ("`git rev-parse --git-path hooks/pre-push` "
                           "answered with nothing")






    if os.path.isfile(path) and os.access(path, os.X_OK):
        return "runs", path
    return "none", f"no executable pre-push hook at {path}"








_HOOK_HEAD_LINES = 3
_HOOK_TAIL_LINES = 12








def _split_hook_stdout(push_stdout: str) -> tuple[list[str], bool]:

























    lines = _untrusted.split_lines(push_stdout)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("To "):
            return lines[:i], True
    return lines, False







_PUSH_EPILOGUE_RE = re.compile(r"^error:\s*failed to push some refs\b",
                               re.IGNORECASE)


def _push_error_line(push_stdout: str, push_stderr: str) -> str:































    hook_lines, _delimited = _split_hook_stdout(push_stdout or "")
    all_stdout = _untrusted.split_lines(push_stdout or "")
    git_lines = (all_stdout[len(hook_lines):]
                 + _untrusted.split_lines(push_stderr or ""))
    lf = chr(10)
    specific = lf.join(ln for ln in git_lines
                       if not _PUSH_EPILOGUE_RE.match(ln.strip()))
    return (_first_error_line(specific)
            or _first_error_line(lf.join(hook_lines))
            or _first_error_line(lf.join(git_lines)))


def _bounded_hook_lines(lines: list[str], head: int = _HOOK_HEAD_LINES,
                        tail: int = _HOOK_TAIL_LINES) -> list[str]:







    return bounded_lines(lines, head, tail)


def _report_prepush_hook(push_stdout: str, push_stderr: str,
                         flags: set[str], relay: bool = True) -> None:


























    state, detail = _prepush_hook_state(flags)
    if state == "none":
        print(f"Pre-push hook: none ran - {detail}. "
              "Nothing gated this push locally.")
    elif state == "unknown":
        print(f"Pre-push hook: whether one ran is UNKNOWN - {detail}. "
              "This receipt is not saying none did.")
    else:
        print(f"Pre-push hook: ran ({detail})")
    if not relay:
        return
    out_lines, delimited = _split_hook_stdout(push_stdout or "")
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    err_lines = _untrusted.split_lines(push_stderr or "")
    while err_lines and not err_lines[-1].strip():
        err_lines.pop()
    if not out_lines and not err_lines:
        if state == "runs":
            print("  It printed nothing, so this receipt cannot say which arm "
                  "it took - the hook's own disclosure is the only evidence of "
                  "that, and there was none.")
        return
    if out_lines and not delimited:
        print("  git printed no `To` header for this push, so where its own "
              "output starts is UNKNOWN - the lines below are relayed "
              "unattributed.")
    for ln in _relayed_lines(_bounded_hook_lines(out_lines)):
        print(f"| {ln}")
    if err_lines:






        print("  stderr for this push, provenance UNKNOWN - the hook, git and "
              "the remote all write here and nothing marks the boundary:")
        for ln in _relayed_lines(_bounded_hook_lines(err_lines)):
            print(f"> {ln}")


def _repo_root() -> str:

    return install_dir()


def _watch_argv(source: str, iid: str) -> tuple[list[str], str]:











    root = _repo_root()
    arg = f"watch:{source}:{iid}"
    wrapper = os.path.join(root, "supertool")
    if os.path.isfile(wrapper) and os.access(wrapper, os.X_OK):
        return [wrapper, arg], wrapper
    entry = os.path.join(root, "supertool.py")
    if os.path.isfile(entry):
        return [sys.executable, entry, arg], f"{sys.executable} {entry}"
    return [], f"no runnable supertool at {root} (neither ./supertool nor supertool.py)"


_WATCH_START_BUDGET = 20.0


def _spawn_watch(source: str, iid: str) -> tuple[Optional[bool], str]:


























    argv, how = _watch_argv(source, iid)
    if not argv:
        return False, how
    try:
        proc = subprocess.Popen(argv, cwd=_repo_root(),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"{how} ({exc})"
    try:
        out, _ = proc.communicate(timeout=_WATCH_START_BUDGET)
    except subprocess.TimeoutExpired:
        return None, (f"{how} had not finished after {_WATCH_START_BUDGET:g}s "
                      f"and was left running — whether it started a watcher "
                      f"is UNKNOWN")
    if proc.returncode != 0:
        said = next((ln.strip() for ln in (out or "").splitlines() if ln.strip()),
                    "and said nothing")
        return False, f"{how} exited {proc.returncode}: {said}"
    return True, how


def _uncommitted_leftovers() -> tuple[Optional[list[str]], str]:





























    r, why = _checked_git(["-c", "status.showUntrackedFiles=normal",
                           "status", "--porcelain"], "git status --porcelain")
    if r is None:
        return None, why
    return [ln for ln in r.stdout.splitlines() if ln.strip()], ""


def _discarded_by_force(old_remote_sha: str) -> tuple[Optional[list[str]], str]:














    if not old_remote_sha:
        return None, "no pre-push SHA recorded for the remote branch"
    r, why = _checked_git(
        ["log", "--format=%h %an: %s", old_remote_sha, "--not", "HEAD"],
        f"git log {old_remote_sha} --not HEAD")
    if r is None:
        return None, why






    return [_untrusted.visible(ln)
            for ln in _untrusted.split_lines(r.stdout) if ln.strip()], ""


def _report_discard_unknown(target: str, why: str, look: str) -> str:











    print(f"⚠ DISCARD CHECK DID NOT RUN — {why}")
    print(f"  {target} was force-updated. Whether that destroyed commits that "
          "were on the remote — possibly someone else's — is UNKNOWN here: "
          "the check could not run, and --force-with-lease does not answer it "
          "either (a current lease still discards commits you never saw).")
    print(f"  Look before you walk away:  {look}")
    return (f" - DISCARD CHECK DID NOT RUN: whether this force-push destroyed "
            f"commits on {target} is UNKNOWN")


def _force_aftermath(old_remote_sha: str, push_stdout: str,
                     remote: str, ref: str) -> str:












    target = f"{remote}/{ref}"
    old = old_remote_sha or _forced_update_old_sha(push_stdout, ref)
    if not old:
        flag, _ = _ref_line(push_stdout, ref)
        if flag and flag != "+":
            return ""
        why = (f"git reported a forced update of {target} without the SHA it "
               "overwrote" if flag else
               f"no pre-push SHA for {target} (no upstream configured) and git "
               "reported no per-ref status line for it")
        return _report_discard_unknown(target, why, f"git reflog show {target}")

    commits, why = _discarded_by_force(old)
    if commits is None:
        return _report_discard_unknown(target, why,
                                       f"git log {old} --not HEAD")
    if not commits:
        return ""
    print(f"Force discarded {len(commits)} remote commit(s) — now off the branch:")
    for line in commits[:_INCOMING_CAP]:
        print(f"  {line}")
    if len(commits) > _INCOMING_CAP:
        print(f"  … +{len(commits) - _INCOMING_CAP} more")
    return (f" - FORCE-DISCARDED {len(commits)} remote commit(s) "
            f"(recover: git reflog show {target})")


def _stale_base_advisory(target: str, remote: str) -> None:


















    ref = f"{remote}/{target}"












    shown_target = _untrusted.flat(str(target))
    shown_ref = f"{remote}/{shown_target}"
    cmd = f"git rev-list --count HEAD..{shown_ref}"







    try:
        cnt = _git(["rev-list", "--count", f"HEAD..{ref}"],
                   timeout=_CHECK_TIMEOUT)
    except OSError as exc:
        print(f"⚠ STALE-BASE CHECK DID NOT RUN — `{cmd}` did not complete ({exc})")
        print(f"  How far behind {shown_ref} you are is UNKNOWN — this "
              f"receipt is not saying your base is fresh. Settle it: {cmd}")
        return
    if cnt.returncode == TIMEOUT_RC:
        print(f"⚠ STALE-BASE CHECK DID NOT RUN — `{cmd}` did not complete "
              f"({cnt.stderr.strip()})")
        print(f"  How far behind {shown_ref} you are is UNKNOWN — this "
              f"receipt is not saying your base is fresh. Settle it: {cmd}")
        return
    if cnt.returncode != 0 or not cnt.stdout.strip().isdigit():
        print(f"⚠ stale-base check skipped — {shown_ref} does not resolve "
              f"locally, so how far behind the target you are is UNKNOWN "
              f"(enable it: git fetch {remote} {shown_target})")
        return
    behind = int(cnt.stdout.strip())
    if behind:
        print(f"⚠ {behind} commit(s) behind {shown_ref} — "
              "consider rebasing (stale base under review)")


def _watch_op_shipped() -> bool:



    presets = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.isfile(os.path.join(presets, "watch.json"))


def _watch_advisory(lookup: MrLookup, flags: set[str]) -> None:



















    if not _watch_op_shipped():
        if "watch" in flags:
            print("⚠ :watch requested, but the watch op is not in this build "
                  "(directory install); install supertool-cli@dpt-plugins for it. "
                  "Nothing is being watched.")
        return
    if not lookup.answered and "watch" in flags:
        print(f"⚠ :watch requested, but whether this branch has an open MR/PR "
              f"is UNKNOWN — {lookup.reason}. Nothing is being watched, and "
              "this is not saying there is nothing to watch.")
        print("Once you know the number: "
              + _st_hint("watch:gitlab-mr:<iid>")
              + " (or watch:github-pr:<number>)")
        return
    wt = _watch_target(lookup.mr)
    if "watch" not in flags:
        if wt:
            print("Watch pipeline: " + _st_hint(f"watch:{wt[0]}:{wt[1]}"))
        return
    if not wt:
        print("⚠ :watch requested, but there is no open MR/PR for this branch "
              "yet — nothing to watch. Open one, then: "
              + _st_hint("watch:gitlab-mr:<iid>"))
        return
    source, iid = wt
    started, how = _spawn_watch(source, iid)
    if started is True:
        print("Watching → notifies on pipeline finish/fail "
              "(unwatch: " + _st_hint(f"unwatch:{source}:{iid}") + ")")
        return
    if started is None:
        print(f"⚠ :watch requested — whether a watcher is running is UNKNOWN "
              f"— {how}")
        print("  This receipt is not saying one exists, and not saying one "
              "does not. Settle it: " + _st_hint("watches"))
        print("If none is listed: " + _st_hint(f"watch:{source}:{iid}"))
        return
    print(f"⚠ :watch requested but the watcher could not be started — {how}")



    print("Run it yourself: " + _st_hint(f"watch:{source}:{iid}"))


def _post_push_advisories(lookup: MrLookup, flags: set[str],
                          remote: str, branch: str) -> None:










    unknown = _mr_unknown_line(lookup)
    if unknown:



        print(unknown)
    mr = lookup.mr
    if not mr and lookup.answered:




        for ln in _dead_mr_lines(lookup, branch):
            print(ln)
    conflict = _mr_conflict_line(mr)
    if conflict:
        print(conflict)

    target = mr.get("target") if mr else ""
    if target and target != "?":
        _stale_base_advisory(target, remote)




    leftovers, why = _uncommitted_leftovers()
    if leftovers is None:
        print(f"⚠ UNCOMMITTED-CHANGES CHECK DID NOT RUN — {why}")
        print("  Whether this push left work behind in the working tree is "
              "UNKNOWN — this receipt is not saying the tree is clean. "
              "Settle it: " + _st_hint("git-status:full"))
    elif leftovers:
        print(f"⚠ {len(leftovers)} change(s) NOT in this push (uncommitted) — "
              "list them: " + _st_hint("git-status:full"))

    _watch_advisory(lookup, flags)


def _report_first_seen_remote(remote_after: str, push_stdout: str, ref: str,
                              target: str) -> tuple[bool, str, list[str]]:
























    kind, old, why = _push_outcome(push_stdout, ref)
    if kind == "created":
        return True, "", [f"Remote now at {remote_after} (branch created)"]
    if kind == "uptodate":
        return False, "", [f"Remote at {remote_after} — already up to date, "
                           "ref unchanged"]
    if kind == "forced":
        return True, "", [f"Remote {old} → {remote_after} (force-updated — the "
                          "branch already existed on the remote and was "
                          "overwritten)"]
    if kind == "updated":
        return True, "", [f"Remote {old} → {remote_after} (the branch already "
                          "existed on the remote)"]
    return True, (" - PRE-PUSH REMOTE STATE UNKNOWN: whether this push created "
                  f"{target} or overwrote it is not established"), [
        f"⚠ Remote now at {remote_after} — what it pointed at BEFORE this "
        "push is UNKNOWN",
        f"  No pre-push SHA was recorded (@{{upstream}} did not resolve) and "
        f"{why}. That is not evidence of a branch creation: the branch may "
        "have already existed and been overwritten.",
        f"  Settle it: git reflog show {target}",
    ]


def _ahead_behind_line() -> None:















    cmd = "git rev-list --left-right --count HEAD...@{upstream}"
    ab, why = _checked_git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cmd)
    if ab is None:
        print(f"⚠ vs upstream: UNKNOWN — {why}")
        return
    parts = ab.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        print(f"⚠ vs upstream: UNKNOWN — `{cmd}` answered "
              f"`{ab.stdout.strip()}`, which is not an ahead/behind pair")
        return
    ahead, behind = int(parts[0]), int(parts[1])
    if ahead or behind:
        print(f"vs upstream: ahead {ahead}, behind {behind}")
    else:
        print("vs upstream: in sync")


def _pushed_commit_count(before: str, after: str) -> tuple[str, str]:











    cmd = f"git rev-list --count {before}..{after}"
    r, why = _checked_git(["rev-list", "--count", f"{before}..{after}"], cmd)
    if r is None:
        return "", why
    n = r.stdout.strip()
    if not n.isdigit():
        return "", f"`{cmd}` answered `{n}`, which is not a count"
    return n, ""


def _success_receipt(branch: str, remote_before: str, upstream: str,
                     flags: set[str], fallback_remote: str,
                     force_note: str = "", push_stdout: str = "",
                     status_suffix: str = "") -> None:
























    body: list[str] = []
    if not upstream:
        upstream, up_why = _upstream_ref()
        if up_why:
            body.append(f"⚠ UPSTREAM LOOKUP DID NOT RUN — {up_why}")
            body.append(f"  The remote named below falls back to "
                        f"{fallback_remote}/{branch} and may not be this "
                        "branch's real upstream (#642), so the stale-base "
                        "check and the verified SHA below may be about the "
                        "wrong ref.")
    remote_name, remote_ref = _split_upstream(upstream, branch,
                                              fallback_remote)
    remote_after, after_why = _remote_sha(upstream)
    moved, ncommits, unknown_note = True, "", ""
    if remote_before and remote_after and remote_before != remote_after:
        ncommits, cnt_why = _pushed_commit_count(remote_before, remote_after)
        if cnt_why:




            body.append(f"Remote {remote_before} → {remote_after} — how many "
                        f"commit(s) that is: UNKNOWN ({cnt_why})")
        else:
            body.append(f"Remote {remote_before} → {remote_after} "
                        f"({ncommits} commit(s))")
    elif not remote_before and remote_after:
        moved, unknown_note, lines = _report_first_seen_remote(
            remote_after, push_stdout, remote_ref,
            f"{remote_name}/{remote_ref}")
        body += lines
    elif remote_before and remote_after and remote_before == remote_after:
        moved = False
        body.append("Already up to date — nothing to push")
    else:


        remote_after = ""
        body.append("Pushed — remote ref not locally resolvable for a "
                    "before/after diff"
                    + (f" ({after_why})" if after_why else ""))










    print(("Status: pushed ✓" if moved
           else "Status: nothing to push ✓ — the remote ref already matched")
          + status_suffix)
    for ln in body:
        print(ln)

    _ahead_behind_line()

    lookup = _mr_lookup(branch)
    mr_line = _open_mr_line(lookup.mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(lookup, flags, remote_name, branch)
    _push_verdict(moved, branch, remote_name, remote_ref, remote_after,
                  ncommits, force_note + unknown_note)


def _report_hook_pushed(head_before: str, head_after: str,
                        remote: str, ref: str, remote_sha: str,
                        branch: str, flags: set[str]) -> None:

    _note_landed(branch, remote, ref)
    if head_after != head_before:
        print("Status: PUSHED (pre-push hook amended HEAD) ✓")
        print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
    else:
        print("Status: pushed ✓ (pre-push hook exited non-zero; "
              "remote already matches HEAD)")
    print(f"Remote {remote}/{ref} now at {remote_sha[:7]}")


    lookup = _mr_lookup(branch)
    mr_line = _open_mr_line(lookup.mr)
    if mr_line:
        print(mr_line)
    _post_push_advisories(lookup, flags, remote, branch)
    _result(f"PUSHED  {branch} -> {remote}/{ref} @ {remote_sha[:7]}  "
            "(verified - pre-push hook pushed it, remote matches HEAD)")


def _budget_advice() -> str:

















    source = _BUDGET["source"]
    if source == f"ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY}":
        where = (f"That budget is `ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY}` in "
                 f"this repository's .supertool.json — raise it there, or "
                 f"override this one call with `git-push:budget=SECONDS`")
    elif source == ":budget":
        where = ("That budget is the `git-push:budget=SECONDS` this call "
                 "passed — pass a bigger one")
    else:
        where = (f"That budget is _PUSH_TIMEOUT in presets/git/push.py — ask "
                 f"for more of it with `git-push:budget=SECONDS`, or state "
                 f"this repository's own default once as "
                 f"`ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY}` in .supertool.json "
                 f"(#1631), which the flag still overrides")
    return (
        f"{where} (up to {_PUSH_TIMEOUT_MAX}s), "
        f"which is the right lever when a pre-push hook runs a suite. It is "
        f"NOT ops.git-push.timeout: that op-level cap bounds the whole "
        f"process, raising it alone will not move this one, and this budget "
        f"has to stay strictly under it or a push killed by the outer cap can "
        f"verify nothing (#399).")


def _report_push_timeout(branch: str, head_before: str,
                         remote: str, ref: str, flags: set[str]) -> int:


















    head_after, _head_why = _local_head()
    live, live_why = _live_remote_sha(remote, ref)
    allowed = _push_allowed()
    print(f"Push exceeded its {allowed}s budget — asking the remote what landed…")
    if allowed != _push_budget():




        print(f"That {allowed}s is what remained of the {_push_budget()}s you "
              "asked for, after the first attempt and the rebase — :budget is a "
              "deadline for this op's pushing, not a fresh clock per attempt.")
    if live and head_after and live == head_after:
        _note_landed(branch, remote, ref)
        print("Status: pushed ✓ (push timed out locally; remote ref matches HEAD)")
        if head_after != head_before:
            print(f"Local HEAD rewritten {head_before[:7]} → {head_after[:7]}")
        print(f"Remote {remote}/{ref} now at {live[:7]}")
        print(f"Push outlasted its {allowed}s budget (slow pre-push hook "
              "or transfer), so the receipt above is only what fit in the "
              "time. The push landed — re-run `git-push` for the full receipt "
              "(it will report already up to date).")
        print(_budget_advice())
        lookup = _mr_lookup(branch)
        mr_line = _open_mr_line(lookup.mr)
        if mr_line:
            print(mr_line)
        _post_push_advisories(lookup, flags, remote, branch)
        _result(f"PUSHED  {branch} -> {remote}/{ref} @ {live[:7]}  "
                "(verified - push timed out locally, remote matches HEAD)")
        return 0
    print("Status: PUSH TIMED OUT ✗ — remote ref does NOT match local HEAD")
    print(f"local HEAD {head_after[:7] or 'unknown'} | "
          f"remote {remote}/{ref} at {live[:7] or 'unknown'}"
          + (f" ({live_why})" if not live and live_why else ""))
    hook_state, hook_detail = _prepush_hook_state(flags)
    if hook_state == "runs":
        print(f"A local pre-push hook runs before anything is sent "
              f"({hook_detail}), so some or all of that {allowed}s may "
              "have been local — a remote that has not moved is exactly what a "
              "push still inside its own hook looks like. This repo's hook "
              "runs the full suite when the destination is master/main (#1242).")
    elif hook_state == "none":
        print(f"No local pre-push hook ran ({hook_detail}), so the "
              f"{allowed}s was the push itself.")
    else:
        print(f"Whether a local pre-push hook ran is UNKNOWN — {hook_detail}. "
              "This receipt is not saying none did.")
    if hook_state != "none":







        print("What the hook printed before the clock expired is NOT part of "
              "this receipt: the push was killed and its output went with it. "
              "No relay here is not a hook that stayed silent.")
    print("The push may still be in flight — `git fetch` and re-check before "
          "retrying; do NOT force-push on a timeout alone.")
    print(_budget_advice())
    _result(f"NOT PUSHED - UNVERIFIED  {branch} -> {remote}/{ref} - push timed "
            f"out and the remote does not match local HEAD "
            f"(remote {live[:7] or 'unknown'}, HEAD {head_after[:7] or 'unknown'})")
    return 1


def _rebase_state() -> str:













    paths: list[str] = []
    for name in ("rebase-merge", "rebase-apply"):
        r = _git(["rev-parse", "--git-path", name], timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown"
        paths.append(r.stdout.strip())
    return "in-progress" if any(os.path.exists(p) for p in paths) else "not-started"


def _report_budget_spent(stage: str, branch: str, target: str,
                         rebased: bool) -> int:




















    left = _budget_left()
    if left:
        print(f"Status: NOT PUSHED ✗ — {left}s of the {_push_budget()}s push "
              f"budget were left when {stage} was due to start, under the "
              f"{_RECOVER_MIN}s minimum")
        print(f"It was NOT attempted, and that is deliberate: nothing "
              f"completes in {left}s, so launching it would have reported a "
              f"timeout whose cause was this budget rather than the remote.")
    else:
        print(f"Status: NOT PUSHED ✗ — the {_push_budget()}s push budget was "
              f"spent before {stage} could start")
    if rebased:
        print(f"Your branch is REBASED onto {target} — the rebase ran and was "
              "clean, and nothing was pushed. Your commits are replayed, not "
              "lost, and your tree is not where you left it.")
        print("Retry `git-push` — it is a fast-forward now, and it gets a "
              "fresh budget.")
    else:
        print("Your working tree is unchanged and your branch is where it "
              "was. Nothing was pushed.")
    print(_budget_advice())
    remainder = (f"only {left}s of the {_push_budget()}s budget was left"
                 if left else f"the {_push_budget()}s budget was gone")
    if rebased:
        _result(f"NOT PUSHED - BUDGET SPENT  {branch} -> {target} - rebased "
                f"onto {target} cleanly, then {remainder} before the re-push; "
                "retry `git-push`")
    else:
        _result(f"NOT PUSHED - BUDGET SPENT  {branch} -> {target} - "
                f"{remainder} before the recovery {stage}; working tree "
                "unchanged")
    return 1


def _report_recovery_timeout(stage: str, branch: str, target: str,
                             allowed=None) -> int:











    stage_up = stage.upper()










    budget_said = "its budget" if allowed is None else f"its {allowed}s budget"
    budget_tag = "" if allowed is None else f" ({allowed}s)"
    state = _rebase_state()
    print(f"Status: {stage_up} TIMED OUT ✗ — exceeded {budget_said} "
          f"while recovering the non-fast-forward push")
    if state == "in-progress":
        print("Your worktree has a REBASE IN PROGRESS — git paused it and the "
              "clock ran out before it finished. Nothing was pushed.")
        print("Inspect: " + _st_hint("git-conflicts"))
        print("Then decide:")
        print("  • finish it — resolve if needed, then `git rebase --continue`")
        print("  • undo it — `git rebase --abort` (back to before the push, "
              "nothing changed)")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT{budget_tag}  "
                f"{branch} -> {target} - REBASE IN PROGRESS: finish with "
                "`git rebase --continue` or undo with `git rebase --abort`")
    elif state == "not-started":
        print("No rebase is in progress — the working tree is unchanged and "
              "your branch is where it was.")




        print("Retry. If this repo genuinely needs more than "
              + (f"{allowed}s " if allowed is not None else "the clock it got ")
              + f"to {stage}, the budget is _RECOVER_TIMEOUT in "
              "presets/git/push.py — or, when the push deadline was the "
              "tighter of the two, `git-push:budget=SECONDS`. Raising "
              "ops.git-push.timeout alone will not move either.")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT{budget_tag}  "
                f"{branch} -> {target} - no rebase started, working tree "
                "unchanged")
    else:
        print("Could NOT determine whether a rebase is in progress — git did "
              "not answer. Your worktree may or may not be paused mid-rebase.")
        print("Check before anything else: `git status`")
        _result(f"NOT PUSHED - {stage_up} TIMED OUT{budget_tag}  "
                f"{branch} -> {target} - rebase state UNKNOWN, run "
                "`git status` before retrying")
    return 1


_INCOMING_CAP = 5


def _incoming_commits(ref: str) -> tuple[list[str], int, int]:








    log = _git(["log", "--format=%h %an: %s", f"HEAD..{ref}"])



    incoming = [_untrusted.visible(ln)
                for ln in _untrusted.split_lines(log.stdout) if ln.strip()]
    mine = _git(["rev-list", "--count", f"{ref}..HEAD"])
    ahead = int(mine.stdout.strip()) if mine.returncode == 0 and mine.stdout.strip().isdigit() else 0
    return incoming, len(incoming), ahead


def _recover_by_rebase(branch: str, remote_before: str, upstream: str,
                       remote_name: str, remote_ref: str, flags: set[str]) -> int:









    target = f"{remote_name}/{remote_ref}"






    refuse = reject_fetch_option(remote_name, remote_ref)
    if refuse:
        print(f"Status: PUSH REJECTED ✗ — {refuse}")
        _result(f"NOT PUSHED - REJECTED  {branch} -> {target} - {refuse}")
        return 1



    fetch_budget = _recover_allowance()
    if not fetch_budget:
        return _report_budget_spent("the recovery fetch", branch, target,
                                    rebased=False)
    print(f"Remote moved ahead — fetching to rebase onto {target}…")
    fetched = _git(["fetch", remote_name, remote_ref], timeout=fetch_budget)
    if fetched.returncode == TIMEOUT_RC:
        return _report_recovery_timeout("fetch", branch, target, fetch_budget)
    if fetched.returncode != 0:
        combined = (fetched.stdout or "") + "\n" + (fetched.stderr or "")
        print(f"Status: PUSH REJECTED ✗ — fetch of {target} failed, cannot rebase")
        err = _untrusted.flat(_first_error_line(combined))
        if err:
            print(f"First error: {err}")
        print("Hint: remote unreachable or ref gone — check connectivity, then retry.")
        _result(f"NOT PUSHED - REJECTED (non-fast-forward)  {branch} -> {target} "
                "- fetch failed, could not rebase")
        return fetched.returncode or 1







    rebase_target = "FETCH_HEAD"
    incoming, behind, ahead = _incoming_commits(rebase_target)
    if behind:
        print(f"Remote added {behind} commit(s) you lack; replaying {ahead} of yours:")
        for ln in incoming[:_INCOMING_CAP]:
            print(f"  {ln}")
        if behind > _INCOMING_CAP:
            print(f"  … +{behind - _INCOMING_CAP} more")

    rebase_budget = _recover_allowance()
    if not rebase_budget:
        return _report_budget_spent("the rebase", branch, target,
                                    rebased=False)
    rebase = _git(["rebase", rebase_target], timeout=rebase_budget)
    if rebase.returncode == TIMEOUT_RC:
        return _report_recovery_timeout("rebase", branch, target, rebase_budget)
    if rebase.returncode != 0:


        unmerged = _git(["diff", "--name-only", "--diff-filter=U"])
        files = [f for f in unmerged.stdout.splitlines() if f.strip()]
        combined = (rebase.stdout or "") + "\n" + (rebase.stderr or "")
        if not files:
            _git(["rebase", "--abort"])  
            print(f"Status: PUSH REJECTED ✗ — rebase onto {target} could not start")
            err = _untrusted.flat(_first_error_line(combined))
            if err:
                print(f"First error: {err}")





            print("")
            for ln in relayed_block(combined):
                print(ln)
            _result(f"NOT PUSHED - REJECTED (non-fast-forward)  {branch} -> "
                    f"{target} - rebase could not start")
            return rebase.returncode


        print("Status: REBASE PAUSED ✗ — conflict (remote and local both changed):")
        for f in files:
            print(f"  {f}")
        print("Inspect: " + _st_hint("git-conflicts")
              + "  — every conflict block + abort hint")
        if behind:
            print("Before you force: that would discard the remote commit(s) listed "
                  "above — check the author first.")
        print("Then decide:")
        print("  • keep both — resolve, then `git rebase --continue` && `git-push`")
        print("  • cancel — `git rebase --abort` (back to before the push, nothing changed)")
        print("  • force yours over remote — `git rebase --abort`, then `git-push:force-with-lease`")
        _result(f"NOT PUSHED - REBASE PAUSED (conflict in {len(files)} file(s))  "
                f"{branch} -> {target} - resolve then `git rebase --continue`, "
                "or `git rebase --abort`")
        return 1

    print("Rebase clean — pushing rebased work")



    push_args = ["push", "--porcelain"]
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not upstream:
        push_args += ["-u", remote_name, "HEAD"]
    elif remote_ref != branch:




        push_args += [remote_name, f"HEAD:{remote_ref}"]
    repush_budget = _repush_allowance()
    if not repush_budget:
        return _report_budget_spent("the re-push", branch, target,
                                    rebased=True)
    result = _git(push_args, timeout=repush_budget)
    if result.returncode == TIMEOUT_RC:
        return _report_push_timeout(branch, _local_head()[0],
                                    remote_name, remote_ref, flags)
    if result.returncode != 0:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        print("Status: PUSH REJECTED ✗ (after rebase)")
        err = _untrusted.flat(_first_error_line(combined))
        if err:
            print(f"First error: {err}")



        _report_prepush_hook(result.stdout or "", result.stderr or "", flags,
                             relay=False)



        print("")
        for ln in relayed_block(combined):
            print(ln)
        _result(f"NOT PUSHED - REJECTED after a clean rebase  {branch} -> {target}")
        return result.returncode






    _report_prepush_hook(result.stdout or "", result.stderr or "", flags)





    _note_landed(branch, remote_name, remote_ref)
    _success_receipt(branch, remote_before, upstream, flags, remote_name,
                     push_stdout=result.stdout or "",
                     status_suffix=" (rebased onto remote)")
    return 0


def _crash_receipt(exc: BaseException) -> int:

























    print("\n--- receipt crash ---")
    traceback.print_exc(file=sys.stdout)
    detail = f"{exc.__class__.__name__}: {exc}"
    if _RUN["phase"] == "landed":
        print("The push itself LANDED — the remote moved. What broke is the "
              "receipt describing it, so any check below the crash point never "
              "ran and nothing here is claiming otherwise.")
        print("Re-run `git-push` for the full receipt (it will report already "
              "up to date).")
        if not _RUN["verdict"]:
            _result(f"PUSHED  {_RUN['branch']} -> {_RUN['target']} @ unknown  "
                    f"(RECEIPT INCOMPLETE - git-push crashed after the push "
                    f"landed: {detail})")
        return 0
    if _RUN["phase"] == "attempted":
        print("The push was started and git-push crashed before it could "
              "establish what reached the remote.")
        if not _RUN["verdict"]:





            settle = (f"git ls-remote {_RUN['remote']} {_RUN['ref']}"
                      if _RUN["remote"] and _RUN["ref"] else "git ls-remote")
            _result(f"NOT PUSHED - UNVERIFIED  {_RUN['branch'] or 'branch'} -> "
                    f"{_RUN['target'] or 'remote'} - git-push crashed mid-push "
                    f"({detail}); whether anything landed is UNKNOWN - settle "
                    f"it: {settle}")
        return 1
    if not _RUN["verdict"]:
        _result("NOT PUSHED - no push attempted (git-push crashed before "
                f"pushing: {detail})")
    return 1


def main() -> int:








    use_utf8_stdout()
    _RUN.update({"phase": "not-attempted", "branch": "", "remote": "",
                 "ref": "", "target": "", "verdict": False})
    _BUDGET.update({"seconds": None, "deadline": None, "allowed": None,
                    "source": ""})
    try:
        return _push_op()
    except Exception as exc:  
        return _crash_receipt(exc)


def _push_op() -> int:





    inside, why = probe_repo(_git)
    if inside is None:
        for line in unanswered_repo_lines(why):
            print(line)
        _result("NOT PUSHED - no push attempted (could not tell whether this "
                "is a git repository - the probe did not answer)")
        return 1
    if not inside:
        print(NOT_A_REPO)
        _result("NOT PUSHED - no push attempted (not inside a git repository)")
        return 1

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not branch or branch == "HEAD":
        print("ERROR: detached HEAD — checkout a branch before pushing.")
        _result("NOT PUSHED - no push attempted (detached HEAD - checkout a "
                "branch first)")
        return 1

    flags, unknown = _split_flags(sys.argv[1:])
    if unknown:




        listed = ", ".join(unknown)
        print(f"ERROR: unknown flag(s): {listed}")
        print(f"Accepted: {', '.join(_KNOWN_FLAGS)}, budget=SECONDS")
        print("Nothing was pushed. A flag this op cannot honour is refused "
              "rather than silently dropped — re-run without it, or fix the "
              "spelling.")
        _result(f"NOT PUSHED - no push attempted (unknown flag(s): {listed}; "
                f"accepted: {', '.join(_KNOWN_FLAGS)}, budget=SECONDS)")
        return 2

    budget, budget_why = _parse_budget(sys.argv[1:])
    if budget_why:



        print(f"ERROR: unusable :budget — {budget_why}")
        print(f"Default is {_PUSH_TIMEOUT}s; the most this op can wait is "
              f"{_PUSH_TIMEOUT_MAX}s. Nothing was pushed.")
        _result(f"NOT PUSHED - no push attempted (unusable :budget — "
                f"{budget_why})")
        return 2
    _BUDGET["source"] = ":budget"
    if budget is None:




        budget, config_why = _config_budget()
        if config_why:
            print(f"ERROR: unusable push budget in .supertool.json — "
                  f"{config_why}")
            print(f"Default is {_PUSH_TIMEOUT}s; the most this op can wait is "
                  f"{_PUSH_TIMEOUT_MAX}s. Nothing was pushed.")
            _result(f"NOT PUSHED - no push attempted (unusable "
                    f"ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY} — {config_why})")
            return 2
        _BUDGET["source"] = ("" if budget is None
                             else f"ops.{_OP_NAME}.{_CONFIG_BUDGET_KEY}")
    _BUDGET["seconds"] = budget

    upstream, upstream_why = _upstream_ref()









    retarget_from = ""
    if upstream:
        _tracked_ref = _split_upstream(upstream, branch, "")[1]
        if _tracked_ref != branch:
            if {"set-upstream", "to-upstream"} <= flags:
                return _refuse_conflicting_targets(
                    branch, _split_upstream(upstream, branch, "")[0],
                    _tracked_ref)
            if "set-upstream" in flags:
                retarget_from, upstream = upstream, ""
    has_upstream = bool(upstream)





    push_remote, chosen_how = "", ""
    if not has_upstream:
        push_remote, chosen_how, cannot_tell = _resolve_push_remote(branch)
        if not push_remote:
            return _refuse_unresolved_remote(branch, cannot_tell)
    remote_name, remote_ref = _split_upstream(upstream, branch, push_remote)






























    refuse = reject_fetch_option(remote_name, remote_ref)
    if refuse:
        print(f"Status: PUSH REJECTED ✗ — {refuse}")
        print("Nothing was pushed. A real remote or branch never starts with "
              "`-`; git refuses to create one. Look for it in `git remote -v` "
              "and in the branch.*.pushRemote / remote.pushDefault / "
              "branch.*.remote config keys this op reads.")
        _result(f"NOT PUSHED - REJECTED  {branch} -> {remote_name}/"
                f"{remote_ref} - {refuse}")
        return 1


    remote_before, before_why = (_remote_sha(upstream) if has_upstream
                                 else ("", ""))
    if has_upstream and remote_ref != branch and "to-upstream" not in flags:





        return _refuse_mismatched_upstream(branch, remote_name, remote_ref)
    head_before, _head_before_why = _local_head()

    print(f"# git-push on {branch}")



    print(f"Repo: {repo_label()}")
    if has_upstream:
        print(f"Upstream: {upstream}" + (f" @ {remote_before}" if remote_before else ""))
        if before_why:
            print(f"⚠ Pre-push remote SHA UNKNOWN — {before_why}")
        if remote_ref != branch:
            print(f"  :to-upstream — pushing onto {remote_name}/{remote_ref} "
                  f"on purpose, not onto {remote_name}/{branch}")
    elif retarget_from:
        print(f"Upstream: {retarget_from} — inherited, not this branch. "
              f":set-upstream retargets it to {remote_name}/{branch} "
              f"({chosen_how})")
    elif upstream_why:



        print(f"⚠ UPSTREAM LOOKUP DID NOT RUN — {upstream_why}")
        print("  Treating this as 'no upstream' and setting one on push, to "
              f"{remote_name} ({chosen_how}). If {branch} already tracks "
              "something else, this push will retarget it.")
    else:
        print(f"Upstream: none — setting on first push to "
              f"{remote_name}/{branch} ({chosen_how})")
    if flags:
        print(f"Flags: {', '.join(sorted(flags))}")
    if _BUDGET["seconds"] is not None:




        print(f"Push budget: {_push_budget()}s ({_BUDGET['source']} — default "
              f"is {_PUSH_TIMEOUT}s)")




    push_args = ["push", "--porcelain"]
    if "force-with-lease" in flags:
        push_args.append("--force-with-lease")
    if "no-verify" in flags:
        push_args.append("--no-verify")
    if not has_upstream:
        push_args += ["-u", remote_name, "HEAD"]
    elif remote_ref != branch:



        push_args += [remote_name, f"HEAD:{remote_ref}"]
    _RUN.update({"phase": "attempted", "branch": branch,
                 "remote": remote_name, "ref": remote_ref,
                 "target": f"{remote_name}/{remote_ref}"})
    result = _git(push_args, timeout=_open_push_deadline())
    if result.returncode == TIMEOUT_RC:
        return _report_push_timeout(branch, head_before,
                                    remote_name, remote_ref, flags)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:




        head_after, _ = _local_head()
        live, _ = _live_remote_sha(remote_name, remote_ref)
        if live and head_after and live == head_after:



            _report_prepush_hook(result.stdout or "", result.stderr or "",
                                 flags)
            _report_hook_pushed(head_before, head_after,
                                 remote_name, remote_ref, live, branch, flags)
            return 0



        if (_is_non_fast_forward(result.stdout or "", remote_ref)
                and "force-with-lease" not in flags):
            try:
                return _recover_by_rebase(branch, remote_before, upstream,
                                          remote_name, remote_ref, flags)
            except subprocess.TimeoutExpired:




                return _report_recovery_timeout(
                    "rebase recovery", branch, f"{remote_name}/{remote_ref}")




        status = _ref_status(result.stdout or "", remote_ref)
        low = status.lower()






        reached_remote = bool(status)
        verdict = "REJECTED" if reached_remote else "STOPPED BEFORE THE REMOTE"
        print("Status: PUSH REJECTED ✗" if reached_remote
              else "Status: PUSH STOPPED BEFORE THE REMOTE ✗")
        err = _untrusted.flat(_push_error_line(result.stdout or "",
                                               result.stderr or ""))
        if err:
            print(f"First error: {err}")
        if "stale info" in low:


            print("Hint: the lease is stale — remote moved since you last fetched. "
                  "`git fetch` to review the new commits, then retry "
                  "`git-push:force-with-lease`.")
        elif low.startswith("[remote rejected]") or low.startswith("[remote failure]"):
            print("Hint: rejected by a server-side rule (protected branch / hook), "
                  "not a divergence — check branch protection or the hook output "
                  "above. A rebase will not help.")
        elif status:
            print(f"Hint: git rejected {remote_name}/{remote_ref} — {status}")
        else:




            print(f"Hint: git reported no ref status for {remote_name}/{remote_ref} "
                  "— the push was stopped before it reached the remote (local "
                  "pre-push hook, or transport). Not a divergence: a rebase "
                  "would not help. The output below is what stopped it; "
                  "`git-push:no-verify` skips a local hook.")






        _report_prepush_hook(result.stdout or "", result.stderr or "", flags,
                             relay=False)




        print("")
        for ln in relayed_block(combined):
            print(ln)
        _result(f"NOT PUSHED - {verdict}  {branch} -> {remote_name}/{remote_ref}"
                + (f" - {err}" if err else ""))
        return result.returncode



    _report_prepush_hook(result.stdout or "", result.stderr or "", flags)



    _note_landed(branch, remote_name, remote_ref)
    force_note = ""
    if "force-with-lease" in flags:





        force_note = _force_aftermath(remote_before, result.stdout or "",
                                      remote_name, remote_ref)
    _success_receipt(branch, remote_before, upstream, flags, remote_name,
                     force_note, push_stdout=result.stdout or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
