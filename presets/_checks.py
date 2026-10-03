#!/usr/bin/env python3



















from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Sequence







_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.append(_HERE)

import _untrusted  




NOT_GREEN = "⚠ NOT ALL GREEN"







NO_CHECKS = "none reported — no check runs on this commit"








CHECK_CREATION_GRACE_SECS = 900








NO_PIPELINE = (
    "none reported — whether one is still coming is UNKNOWN. GitLab makes a "
    "pipeline at push time, so a missing one can mean no job matched this ref, "
    "and it can equally mean the head was just pushed or the MR's pipeline is "
    "not attached to this payload. Check the MR's Pipelines tab."
)

PASSED_STATES = frozenset({"SUCCESS"})




FAILED_STATES = frozenset({
    "FAILURE", "FAILED", "ERROR", "STARTUP_FAILURE", "TIMED_OUT",
    "ACTION_REQUIRED",
})


PENDING_STATES = frozenset({
    "IN_PROGRESS", "RUNNING", "QUEUED", "PENDING", "WAITING", "REQUESTED",
    "EXPECTED", "CREATED", "SCHEDULED", "PREPARING",
})





BENIGN_STATES = frozenset({"SKIPPED", "NEUTRAL", "MANUAL"})

_UNKNOWN = "UNKNOWN"




UNKNOWN = _UNKNOWN


def normalize(state: object) -> str:




















    s = _untrusted.flat(str(state or "")).strip().upper()
    return s or _UNKNOWN


def bucket(state: object) -> str:

    s = normalize(state)
    if s in PASSED_STATES:
        return "passed"
    if s in FAILED_STATES:
        return "failed"
    if s in PENDING_STATES:
        return "pending"
    return "other"


def is_red(state: object) -> bool:





    b = bucket(state)
    if b == "failed":
        return True
    return b == "other" and normalize(state) not in BENIGN_STATES


def github_state(check: dict) -> str:






    if not isinstance(check, dict):
        return _UNKNOWN
    for key in ("conclusion", "status", "state"):
        val = check.get(key)
        if val:
            return normalize(val)
    return _UNKNOWN


def github_states(checks: object) -> List[str]:

    if not isinstance(checks, list):
        return []
    return [github_state(c) for c in checks]


_JOB_ID_IN_URL = re.compile(r"/job/([0-9]+)(?:[/?#]|$)")














_CHECK_ID_IN_URL = re.compile(
    r"^https?://[^/]+/[^/]+/[^/]+/runs/([0-9]+)(?:[/?#]|$)")


def github_job_id(check: dict) -> str:













    if not isinstance(check, dict):
        return ""
    m = _JOB_ID_IN_URL.search(str(check.get("detailsUrl") or ""))
    return m.group(1) if m else ""


def github_check_ref(check: dict) -> tuple[str, str]:
























    if not isinstance(check, dict):
        return ("", "")
    url = str(check.get("detailsUrl") or "")
    job = _JOB_ID_IN_URL.search(url)
    if job:
        return ("job", job.group(1))
    run = _CHECK_ID_IN_URL.search(url)
    if run:
        return ("check", run.group(1))
    return ("", "")


def github_named_states(checks: object) -> List[tuple[str, str, str, str]]:









    if not isinstance(checks, list):
        return []
    out: List[tuple[str, str, str, str]] = []
    for c in checks:
        if isinstance(c, dict):
            name = str(c.get("name") or c.get("context") or "?")
            kind, ident = github_check_ref(c)
            out.append((name, github_state(c), kind, ident))
        else:
            out.append(("?", _UNKNOWN, "", ""))
    return out









SUPERSEDED_TERM = "superseded"




SUPERSEDED_NOTE = (
    "A later check run of the same name started after each of these finished, "
    "so GitHub decides the check on the later run and these are not counted "
    "red in the tally above. They are also unretractable — no trigger "
    "withdraws a concluded run — which is why they are named here rather than "
    "dropped (#1792). Two runs of one name whose wall clocks overlap supersede "
    "nothing and both still have to pass (#1640). `gh-job:<id>:fail` reads any "
    "of them."
)


def _leg_window(check: object) -> tuple:







    if not isinstance(check, dict):
        return ("", None, None)
    name = str(check.get("name") or check.get("context") or "?")
    return (name, parse_ts(check.get("startedAt")),
            parse_ts(check.get("completedAt")))


def github_superseded(checks: object) -> List[bool]:

































    if not isinstance(checks, list):
        return []
    windows = [_leg_window(c) for c in checks]
    by_name: dict = {}
    for i, (name, _started, _done) in enumerate(windows):
        by_name.setdefault(name, []).append(i)

    out = [False] * len(windows)
    for idxs in by_name.values():
        if len(idxs) < 2:
            continue
        for i in idxs:
            done = windows[i][2]
            if done is None:
                continue



            out[i] = any(windows[j][1] is not None and windows[j][1] > done
                         for j in idxs if j != i)
    return out


def github_superseded_count(checks: object) -> int:

    return sum(1 for flag in github_superseded(checks) if flag)


def github_live_states(checks: object) -> List[str]:






    return [s for s, sup in zip(github_states(checks), github_superseded(checks))
            if not sup]


def github_named_live(checks: object) -> List[tuple]:

    return [e for e, sup in zip(github_named_states(checks),
                                github_superseded(checks)) if not sup]


def github_named_superseded(checks: object) -> List[tuple]:

    return [e for e, sup in zip(github_named_states(checks),
                                github_superseded(checks)) if sup]











NAMED_CAP = 5


def named_disclosure(
    entries: Sequence[tuple[str, str, str, str]], cap: int = NAMED_CAP,
    label_prefix: str = "",
) -> List[str]:






































    groups: dict[str, list[tuple[str, str, str]]] = {}
    for name, state, kind, ident in entries:
        b = bucket(state)
        if b in ("passed", "pending"):
            continue
        label = "failed" if b == "failed" else _label(state)
        groups.setdefault(label, []).append(
            (_untrusted.flat(str(name)), _untrusted.flat(str(kind)),
             _untrusted.flat(str(ident))))

    lines: List[str] = []
    for label in sorted(groups):
        items = groups[label]
        shown = items[:cap]
        parts = [f"{n} ({k} #{i})" if i and k else n for n, k, i in shown]
        text = ", ".join(parts)
        if len(items) > cap:
            text += f", +{len(items) - cap} more"
        lines.append(f"  {label_prefix}{label}: {text}")
    return lines


def superseded_disclosure(entries: Sequence[tuple[str, str, str, str]],
                          cap: int = NAMED_CAP) -> List[str]:









    lines = named_disclosure(entries, cap, label_prefix="superseded ")
    if lines:
        lines.append(f"  {SUPERSEDED_NOTE}")
    return lines


def label(state: str) -> str:

















    return normalize(state).lower().replace(",", ";")



_label = label







INCOMPLETE_MARK = "⚠ INCOMPLETE"






UNVERIFIED_MARK = "⚠ TALLY UNVERIFIED"


def _legs(n: int) -> str:


    return "leg" if n == 1 else "legs"


def shortfall(found: int, declared: int | None,
              missing: Sequence[str] = (),
              cap: int = NAMED_CAP,
              reason: str = "") -> tuple[str, List[str]]:





































    missing = [_untrusted.flat(str(n)) for n in missing]
    reason = _untrusted.flat(reason)

    if declared is None:
        because = f" ({reason})" if reason else ""
        return (UNVERIFIED_MARK, [
            f"  unverified: {found} {_legs(found)} read, but how many the run declares "
            f"could not be established{because}, so whether these are all of "
            "them is UNKNOWN. Count by hand with "
            "`gh run view <run-id> --json jobs` before treating this as a "
            "merge signal."
        ])

    if declared <= found:
        return ("", [])

    gap = declared - found
    marker = f"{INCOMPLETE_MARK} — {found} of {declared} legs read"
    named = [n for n in missing if n]
    if named:
        shown = ", ".join(named[:cap])
        if len(named) > cap:
            shown += f", +{len(named) - cap} more"
        detail = f"not read: {shown}"
    else:
        detail = (f"not read: {gap} {_legs(gap)} the run declares "
                  "are absent, names UNKNOWN")
    return (marker, [
        f"  {detail} — this tally describes {found} of {declared} legs and is "
        "not a merge signal. GitHub re-creates check runs during a partial "
        "re-run, so re-running the op usually settles it."
    ])







ZERO_TIME_YEAR = 1970


def parse_ts(value: object) -> float | None:












    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year < ZERO_TIME_YEAR:
        return None
    return dt.timestamp()


def pending_disclosure(stamps: Sequence[object],
                       now_epoch: float | None = None) -> tuple[str, List[str]]:













































    if not stamps:
        return ("", [])

    import time

    now = time.time() if now_epoch is None else now_epoch
    ages = [now - ts for ts in (parse_ts(s) for s in stamps) if ts is not None]
    unreadable = len(stamps) - len(ages)
    total = len(stamps)

    if not ages:
        return ("oldest pending age UNKNOWN", [
            f"  pending: no pending {_legs(total)} carries a start time, so "
            "how long the pending set has been outstanding is UNKNOWN. The "
            "PR's Checks tab has the timestamps."
        ])

    note = f"oldest pending {_duration(int(max(ages)))}"
    if unreadable:
        return (note, [
            f"  pending: {unreadable} of {total} pending {_legs(total)} carry "
            "no start time, so the age above is a floor — the true oldest may "
            "be older, and by how much is UNKNOWN."
        ])
    return (note, [])


def github_pending_stamps(checks: object) -> List[object]:








    if not isinstance(checks, list):
        return []
    out: List[object] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        if bucket(github_state(c)) == "pending":
            out.append(c.get("startedAt"))
    return out


def summarize(states: Sequence[str] | Iterable[str],
              pending_note: str = "", superseded: int = 0) -> str:

































    tokens = [normalize(s) for s in states]
    n_superseded = max(0, int(superseded or 0))
    total = len(tokens) + n_superseded
    if total == 0:
        return NO_CHECKS

    buckets = Counter(bucket(t) for t in tokens)
    n_pending = buckets.get('pending', 0)
    parts = [
        f"{buckets.get('passed', 0)} passed",
        f"{buckets.get('failed', 0)} failed",
        f"{n_pending} pending",
    ]
    leftovers = Counter(_label(t) for t in tokens if bucket(t) == "other")
    for label, count in sorted(leftovers.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{count} {label}")
    if n_superseded:
        parts.append(f"{n_superseded} {SUPERSEDED_TERM}")

    line = f"{total} total: " + ", ".join(parts)
    if not tokens or buckets.get("passed", 0) != len(tokens):
        line += f" {NOT_GREEN}"
    if n_pending and pending_note:
        line += f" — {pending_note}"
    return line


def summarize_github(checks: object, with_age: bool = False) -> str:






    note = ""
    if with_age:
        note, _lines = pending_disclosure(github_pending_stamps(checks))
    return summarize(github_live_states(checks), note,
                     superseded=github_superseded_count(checks))


def github_pending_lines(checks: object) -> List[str]:







    _note, lines = pending_disclosure(github_pending_stamps(checks))
    return lines


def all_green(states: Sequence[str] | Iterable[str]) -> bool:

    tokens = [normalize(s) for s in states]
    return bool(tokens) and all(bucket(t) == "passed" for t in tokens)


def _duration(secs: int) -> str:

    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"





_TERMINAL_PR_STATES = {
    "MERGED": "again",
    "CLOSED": "unless it is reopened",
}


def absence(pr_state: object, age_secs: int | None,
            grace_secs: int = CHECK_CREATION_GRACE_SECS,
            mergeable: object = None) -> tuple[str, str]:





































    if str(mergeable or "").strip().upper() == "CONFLICTING":
        return (
            "none, and none will be created until the conflict is resolved — "
            "mergeable state is CONFLICTING, so GitHub cannot build "
            "refs/pull/N/merge for a pull_request run to execute against. "
            "Rebase — waiting will not change this.",
            " — no checks, and none will be created (mergeable is CONFLICTING)"
            " — rebase",
        )

    state = normalize(pr_state)
    window = f"~{max(1, grace_secs // 60)}min"

    if age_secs is None:
        return (
            "none reported — no check runs on this commit, and whether one is "
            "still coming is UNKNOWN: could not establish when the head commit "
            "landed. Check the PR's Checks tab.",
            " — no checks reported, and whether any are coming is UNKNOWN",
        )

    age = _duration(age_secs)

    if age_secs <= grace_secs:
        return (
            f"none yet — head commit {age} old, inside the {window} window in "
            "which a first run has always appeared; a run is still expected",
            " — no checks yet, a run is still expected",
        )

    if state in _TERMINAL_PR_STATES:
        tail = _TERMINAL_PR_STATES[state]
        return (
            f"none, and none will be created — head commit {age} old and still "
            f"zero runs, and the PR is {state}, so no pull_request event will "
            f"fire for this ref {tail}. Waiting will not change this.",
            f" — no checks, and none will be created (PR is {state})",
        )

    return (
        f"none reported — head commit {age} old and still zero runs, past the "
        f"{window} window in which a first run normally appears; the PR is "
        f"{state}, so an event could still fire and whether any workflow covers "
        "this ref is UNKNOWN. Check the PR's Checks tab.",
        " — no checks reported, and whether any are coming is UNKNOWN",
    )





_FULL_SHA = re.compile(r"^[0-9a-f]{40}\Z")


def is_full_sha(value: object) -> bool:









    return bool(_FULL_SHA.match(str(value or "").strip().lower()))


def head_relation(local_sha: object, pr_head_sha: object,
                  number: object = None) -> str:
















    local = str(local_sha or "").strip().lower()
    remote = str(pr_head_sha or "").strip().lower()

    if is_full_sha(local) and is_full_sha(remote):
        if local == remote:
            return ""
        pointer = f"gh-pr:{number}" if str(number or "").strip() not in ("", "?") else "gh-pr"
        return (
            f"Checks commit: PR head {remote[:7]} — NOT your local HEAD "
            f"{local[:7]}. The Checks line above is about the PR's head commit, "
            f"not the commit you are standing on. `{pointer}` for that commit."
        )

    l_disp = local[:7] if is_full_sha(local) else "unestablished"
    r_disp = remote[:7] if is_full_sha(remote) else "unestablished"
    return (
        f"Checks commit: PR head {r_disp}, local HEAD {l_disp} — whether the "
        "Checks line above is about the commit you are standing on is UNKNOWN."
    )


















NO_CLOSING_REF = (
    "none declared in the body — no closing keyword (Closes/Fixes/Resolves "
    "#N) bound to an issue number. A bare #N mention is not a closing "
    "reference to GitHub and is not reported as one here; a link made through "
    "the PR's Development panel is not in the body and is invisible to this "
    "line."
)







_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"













_CLOSING_REF = re.compile(
    r"\b" + _CLOSING_KEYWORD + r"\b[ \t]*:?[ \t]*(?:"
    r"https?://github\.com/(?P<u_owner>[\w.\-]+)/(?P<u_repo>[\w.\-]+)/issues/(?P<u_num>\d+)"
    r"|(?P<x_owner>[\w.\-]+)/(?P<x_repo>[\w.\-]+)#(?P<x_num>\d+)"
    r"|(?:GH-|#)(?P<num>\d+)"
    r")",
    re.IGNORECASE,
)




















_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_FENCED = re.compile(r"^[ \t]*(```+|~~~+)[\s\S]*?(?:^[ \t]*\1[ \t]*$|\Z)",
                     re.MULTILINE)
_CODE_SPAN = re.compile(r"(`+)[\s\S]*?\1")


def _strip_unhonoured(text: str) -> str:

    for pattern in (_HTML_COMMENT, _FENCED, _CODE_SPAN):
        text = pattern.sub("\n", text)
    return text


def closing_issue_refs(body: object) -> List[str]:





















    text = _strip_unhonoured(str(body or ""))
    if not text:
        return []

    refs: List[str] = []
    for m in _CLOSING_REF.finditer(text):
        if m.group("num"):
            ref = f"#{m.group('num')}"
        elif m.group("x_num"):
            ref = f"{m.group('x_owner')}/{m.group('x_repo')}#{m.group('x_num')}"
        else:
            ref = f"{m.group('u_owner')}/{m.group('u_repo')}#{m.group('u_num')}"
        if ref not in refs:
            refs.append(ref)
    return refs


def linked_issue_line(refs: Sequence[str]) -> str:







    if not refs:
        return f"Issue: {NO_CLOSING_REF}"
    label = "Issue" if len(refs) == 1 else "Issues"
    return f"{label}: {', '.join(refs)}"
