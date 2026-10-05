#!/usr/bin/env python3




















































































from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  

import _repo_target  
import _untrusted  
import _auth_probe  
import _status_probe  





DEFAULT_ISSUE_CAP = 400




_PREFIX_RE = re.compile(r"^([A-Za-z0-9_]+[-:/])")



MIN_GROUP = 2

_UNKNOWN = "?"













DEFAULT_TALLY_MAX = 24




SEARCH_CALLS_PER_LABEL = 1
NONE_BUCKET_CALLS = 1



SEARCH_WORKERS = 4







_QUERY_UNSAFE = re.compile('["\r\n]')


def _positive_int(raw: "str | None", default: int) -> int:


    if raw is None:
        raw = ""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _gh(argv: list[str], timeout: int = 30):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _format_error(stderr: str, what: str) -> str:
    s = (stderr or "").lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-labels")





    if _auth_probe.says_not_authenticated(s):
        return "ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes."
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied reading {what}. Check repo access."
    if _status_probe.says_not_found(s):
        return (f"ERROR: {what} not found {_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")

    return (f"ERROR: gh failed reading {what}: "
            f"{_untrusted.flat((stderr or '').strip())}")


def fetch_labels() -> tuple[list[dict] | None, str]:






    try:
        r = _gh(["gh", "api", "--paginate",
                 _repo_target.api_path("labels?per_page=100")])
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"ERROR: gh failed reading labels: {type(exc).__name__}"
    if r.returncode != 0:
        return None, _format_error(r.stderr, "labels")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, "ERROR: invalid JSON from gh api for labels"
    if not isinstance(data, list):
        return None, "ERROR: unexpected shape from gh api for labels"
    return [row for row in data if isinstance(row, dict)], ""


def fetch_counts(cap: int) -> tuple[dict[str, int] | None, bool, int]:











    try:
        r = _gh(["gh", "issue", "list", *_repo_target.gh_args(),
                 "--state", "open", "--limit", str(cap), "--json", "labels"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, False, 0
    if r.returncode != 0:
        return None, False, 0
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, False, 0
    if not isinstance(rows, list):
        return None, False, 0
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for lab in row.get("labels") or []:
            if isinstance(lab, dict):
                name = str(lab.get("name") or "")
                if name:
                    counts[name] = counts.get(name, 0) + 1



    return counts, len(rows) >= cap, len(rows)


def parse_args(argv) -> tuple[str, str]:







    toks = [str(a) for a in argv if str(a) != ""]
    if not toks:
        return "", ""
    if len(toks) > 1:
        return "", (f"ERROR: gh-labels takes at most one argument, got "
                    f"{len(toks)}: {' '.join(repr(t) for t in toks)}. "
                    f"Syntax: gh-labels[:tally=PREFIX]")
    tok = toks[0]
    if not tok.startswith("tally="):
        return "", (f"ERROR: unrecognised argument {tok!r}. The only argument "
                    f"is `tally=PREFIX` — one label family's open/closed "
                    f"counts plus the issues carrying none of it. Bare "
                    f"`gh-labels` is the vocabulary.")
    prefix = tok[len("tally="):].strip()
    if not prefix:
        return "", ("ERROR: `tally=` needs a label prefix, e.g. "
                    "`tally=cohort-`. The prefix is not assumed: this repo "
                    "spells it `priority-high`, claude-remember spells it "
                    "`priority:high`.")
    if _QUERY_UNSAFE.search(prefix):
        return "", (f"ERROR: refusing the prefix {prefix!r} — a quote or "
                    f"newline would end the quoted term in the search query "
                    f"and the remainder would be read as query syntax.")
    return prefix, ""


def family_members(names, prefix: str) -> list[str]:

    return sorted(str(n) for n in names if str(n).startswith(prefix))


def search_query(repo: str, state: str, within=(), without=()) -> str:






    parts = [f"repo:{repo}", "is:issue", f"is:{state}"]
    parts += [f'label:"{n}"' for n in within]
    parts += [f'-label:"{n}"' for n in without]
    return " ".join(parts)


def search_count(query: str) -> int | None:






    try:
        r = _gh(["gh", "api", "-X", "GET", "search/issues",
                 "-f", f"q={query}", "-f", "per_page=1",
                 "--jq", ".total_count"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return int((r.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def fetch_open_issue_rows(cap: int) -> tuple[list[dict] | None, bool]:






    try:
        r = _gh(["gh", "issue", "list", *_repo_target.gh_args(),
                 "--state", "open", "--limit", str(cap),
                 "--json", "number,labels"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, False
    if r.returncode != 0:
        return None, False
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(rows, list):
        return None, False
    return [row for row in rows if isinstance(row, dict)], len(rows) >= cap


def multi_labelled(rows, members) -> list[tuple[int, list[str]]]:





    family = set(members)
    out: list[tuple[int, list[str]]] = []
    for row in rows or []:
        got = sorted({str(lab.get("name") or "") for lab in row.get("labels") or []
                      if isinstance(lab, dict)} & family)
        if len(got) > 1:
            try:
                number = int(row.get("number"))
            except (TypeError, ValueError):
                continue
            out.append((number, got))
    return sorted(out)


def open_counts(rows: list[dict] | None, members) -> dict[str, int] | None:












    if rows is None:
        return None
    family = set(members)
    counts: dict[str, int] = {str(name): 0 for name in members}
    counts[""] = 0
    for row in rows:
        got = {str(lab.get("name") or "") for lab in row.get("labels") or []
               if isinstance(lab, dict)} & family
        if not got:
            counts[""] += 1
        for name in got:
            counts[name] = counts.get(name, 0) + 1
    return counts


def cell(n: object) -> str:
    return _UNKNOWN if n is None else str(n)


def frozen_cell(open_n: object, closed_n: object, capped: bool = False) -> str:












    if open_n is None or closed_n is None:
        return _UNKNOWN
    total = int(open_n) + int(closed_n)
    return f">={total}" if capped else str(total)


def repo_name() -> str:







    return _repo_target.effective_slug(timeout=15)


def group_of(name: str) -> str:

    m = _PREFIX_RE.match(name or "")
    return m.group(1) if m else ""


def grouped(names: list[str], min_group: int = MIN_GROUP) -> list[tuple[str, list[str]]]:

    tally: dict[str, list[str]] = {}
    for name in names:
        tally.setdefault(group_of(name), []).append(name)
    families = sorted(
        (g, sorted(members)) for g, members in tally.items()
        if g and len(members) >= min_group
    )
    loose = sorted(
        n for g, members in tally.items() for n in members
        if not g or len(members) < min_group
    )
    out: list[tuple[str, list[str]]] = list(families)
    if loose:
        out.append(("", loose))
    return out


def count_text(counts: dict[str, int] | None, capped: bool, name: str) -> str:

    if counts is None:
        return _UNKNOWN
    n = counts.get(name, 0)
    return f">={n}" if capped else str(n)


def tally_main(prefix: str, rows: list[dict], target: str) -> int:

    where = f" — {target}" if target else ""
    print(f"# Label tally — `{_untrusted.flat(prefix)}`{where}")
    if not target:
        print("ERROR: the repository could not be named, and every count here "
              "is a `repo:` query — there is nothing to ask about. Name it "
              "with a leading repo:OWNER/NAME op.")
        return 1

















    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", target):
        print(f"ERROR: refusing the repo target {target!r} — it is "
              f"interpolated into the search query unquoted (`repo:{{repo}}`, "
              f"not `repo:\"{{repo}}\"`), so any character outside "
              f"owner/name's own alphabet (letters, digits, `.`, `_`, `-`, "
              f"one `/`) could add extra search syntax. This shape check runs "
              f"instead of a quote/newline check because a plain SPACE is "
              f"already enough to inject a term here.")
        return 1

    names = [str(r.get("name") or "") for r in rows]
    members = family_members(names, prefix)
    if not members:
        print(f"no labels on this repository start with "
              f"`{_untrusted.flat(prefix)}`. The label list was read "
              f"successfully and no name matches — this is a statement about "
              f"the vocabulary, NOT a board on which every issue is "
              f"unlabelled. The spelling is not portable: this repo uses "
              f"`priority-high`, claude-remember uses `priority:high`. "
              f"`gh-labels` lists what does exist.")
        return 0








    unsafe = [n for n in members if _QUERY_UNSAFE.search(n)]
    if unsafe:
        shown = ", ".join(repr(n) for n in unsafe[:5])
        print(f"ERROR: {len(unsafe)} label(s) in this family carry a quote or "
              f"newline and cannot be put in a search term: {shown}. Counting "
              f"the rest would silently omit them from every row and from the "
              f"NONE bucket, which is a wrong burn-down that looks right. "
              f"Rename them, or narrow the prefix past them.")
        return 1

    cap = _positive_int(os.environ.get("GH_LABELS_TALLY_MAX"), DEFAULT_TALLY_MAX)
    if len(members) > cap:
        calls = len(members) * SEARCH_CALLS_PER_LABEL + NONE_BUCKET_CALLS
        print(f"ERROR: {len(members)} labels start with "
              f"`{_untrusted.flat(prefix)}`, past the {cap} this op will "
              f"query. The open column is free of this — it is one issue "
              f"listing for the whole family — but `closed` is still "
              f"{SEARCH_CALLS_PER_LABEL} search call per label plus "
              f"{NONE_BUCKET_CALLS} for the NONE bucket, so this family costs "
              f"{calls} search calls against an API that allows 30 a minute "
              f"and shares them with everything else in the same minute. The "
              f"board's later closed cells would read `?` because the limiter "
              f"cut in — a partial read in the shape of a complete one. "
              f"Narrow the prefix, or raise "
              f"GH_LABELS_TALLY_MAX={len(members)} accepting that cost.")
        return 1





    issue_cap = _positive_int(os.environ.get("GH_LABELS_ISSUE_CAP"), DEFAULT_ISSUE_CAP)
    open_rows, rows_capped = fetch_open_issue_rows(issue_cap)
    opens = open_counts(open_rows, members)







    jobs: list[tuple[tuple[str, str], str]] = [
        ((name, "closed"), search_query(target, "closed", within=[name]))
        for name in members]
    jobs.append((("", "closed"),
                 search_query(target, "closed", without=members)))

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(SEARCH_WORKERS, len(jobs))) as pool:
        counts = dict(zip([k for k, _ in jobs],
                          pool.map(search_count, [q for _, q in jobs])))

    none_label = f"no {prefix} label"
    width = max([len(_untrusted.flat(n)) for n in members] + [len(none_label)])

    unread = sum(1 for v in counts.values() if v is None)
    print(f"Family: {len(members)} label(s) whose name starts with "
          f"`{_untrusted.flat(prefix)}`. The prefix is a repo convention "
          f"inferred from the names — GitHub has no prefix field.")




    if opens is None:
        print("Open: UNKNOWN — the open-issue list could not be read, so no "
              "row's open cell was filled. `?` is 'not looked at', never 0; "
              "a 0 there reads as a cohort that is finished.")
    elif rows_capped:
        print(f"Open: a FLOOR (`>=N`), over the first {issue_cap} open issues "
              f"read — the cap bit, so a row may hold more and the floor "
              f"carries into `frozen`. Raise GH_LABELS_ISSUE_CAP=N for an "
              f"exact column.")
    else:
        print(f"Open: exact, over all {len(open_rows)} open issues, tallied "
              f"from one listing rather than a search per label (#1628). "
              f"Issues only — pull requests are not in it.")
    if unread:
        print(f"Closed: {unread} of {len(counts)} cells are UNKNOWN — that "
              f"many search queries did not answer. `?` is 'not looked at', "
              f"never 0, and it poisons the `frozen` sum on its row rather "
              f"than being added as zero. The open column above is unaffected: "
              f"it did not come from the search API.")
    else:
        print("Closed: GitHub's search API, one query per label plus one for "
              "the NONE bucket, `is:issue` — pull requests are excluded. "
              "`frozen` is open+closed.")
    print()
    print(_untrusted.flat_note("label names"))
    print(f"  {'label':<{width}}  {'open':>6} {'closed':>7} {'frozen':>7}")
    for name in members:
        o = None if opens is None else opens.get(name, 0)
        c = counts.get((name, "closed"))
        print(f"  {_untrusted.flat(name):<{width}}  "
              f"{count_text(opens, rows_capped, name):>6} "
              f"{cell(c):>7} {frozen_cell(o, c, rows_capped):>7}")
    o = None if opens is None else opens.get("", 0)
    c = counts.get(("", "closed"))
    print(f"  {none_label:<{width}}  "
          f"{count_text(opens, rows_capped, ''):>6} {cell(c):>7} "
          f"{frozen_cell(o, c, rows_capped):>7}")
    print()
    print(f"The `{none_label}` row is the one a per-label listing cannot show: "
          f"issues carrying no label of this family at all. Its closed cell "
          f"counts everything ever closed without one, including issues closed "
          f"before the family existed — a total, not a burn-down.")

    if open_rows is None:
        print(f"Multi-label: UNKNOWN — the open-issue list could not be read, "
              f"so whether any issue carries more than one "
              f"`{_untrusted.flat(prefix)}` label was not checked. An issue in "
              f"two cohorts is a filing error that makes both rows above "
              f"wrong.")
    else:
        offenders = multi_labelled(open_rows, members)
        scope = (f"the first {len(open_rows)} open issues read (the "
                 f"GH_LABELS_ISSUE_CAP cap bit)" if rows_capped
                 else f"all {len(open_rows)} open issues")
        if offenders:
            print(f"Multi-label: {len(offenders)} of {scope} carry more than "
                  f"one `{_untrusted.flat(prefix)}` label. That is a filing "
                  f"error, not a row — each one is counted once per label "
                  f"above, so the rows do not sum to the board:")
            for number, got in offenders:
                got_flat = ", ".join(_untrusted.flat(g) for g in got)
                print(f"  #{number} — {got_flat}")
        else:
            print(f"Multi-label: none of {scope} carry more than one "
                  f"`{_untrusted.flat(prefix)}` label, so each row above "
                  f"counts a disjoint set.")
    return 0


def main() -> int:
    use_utf8_stdout()
    prefix, arg_err = parse_args(sys.argv[1:])
    if arg_err:
        print(arg_err)
        return 1

    rows, err = fetch_labels()
    if rows is None:
        print(err)
        return 1

    if prefix:
        return tally_main(prefix, rows, repo_name())

    target = repo_name()
    where = f" — {target}" if target else " — repository UNKNOWN (gh could not name it)"
    if not rows:
        print(f"# Labels{where}")
        print("no labels are defined on this repository. The list was read "
              "successfully and it is empty — this is not a failed read.")
        return 0

    cap = _positive_int(os.environ.get("GH_LABELS_ISSUE_CAP"), DEFAULT_ISSUE_CAP)
    counts, capped, n_issues = fetch_counts(cap)

    by_name = {str(r.get("name") or "?"): r for r in rows}
    names = list(by_name)

    print(f"# Labels{where} — {len(names)} defined")
    if counts is None:
        print("Counts: UNKNOWN — the open-issue list could not be read, so no "
              "label's usage is established. `?` below is 'not looked at', not "
              "'not used'; do not read it as a reason to retire anything.")
    elif capped:
        print(f"Counts: open issues carrying the label, over the first {cap} "
              f"read — the cap bit, so every count is a FLOOR (`>=N`) and a "
              f"`>=0` may still be in use. Raise GH_LABELS_ISSUE_CAP=N.")
    else:
        print(f"Counts: open ISSUES carrying the label, over all {n_issues} "
              f"of them — exact for issues. Pull requests are NOT counted "
              f"(`gh issue list` excludes them), so a `0` means 'on no open "
              f"issue', which is not the same as unused: a label applied only "
              f"to open PRs reads 0 here.")
    print("Groups below are inferred from the name prefix (a repo convention); "
          "GitHub has no prefix concept and the spelling differs per repo.")
    print()
    print(_untrusted.flat_note("label names and descriptions"))

    width = max(len(n) for n in names)
    for group, members in grouped(names):
        print()
        print(f"## {group or 'ungrouped'} ({len(members)})")
        for name in members:
            desc = str(by_name[name].get("description") or "")
            cell = count_text(counts, capped, name)
            line = f"  {_untrusted.flat(name):<{width}}  {cell:>5}"
            if desc:
                line += f"  {_untrusted.flat(desc)}"
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
