#!/usr/bin/env python3
"""What can I tag this with? — a repo's label vocabulary, with usage (#998).

Every triage or release-planning run opens with this question, because every
later decision depends on the answer and `gh issue edit --add-label` gives no
useful protection against a name that does not exist. With no op for it the
answer came from a hand-rolled `gh label list --json name -q '.[].name' | grep`
— the pipeline shape `#454` exists because of — paid on the first call of every
run, by every fresh agent, forever.

**The vocabulary is not portable and that is why it needs discovering rather
than remembering.** `claude-supertool` spells them `priority-high`;
`claude-remember` spells them `priority:high` and has no `lane-*` family at all.
An agent carrying one repo's spelling into the other mislabels or silently
no-ops, and neither failure announces itself.

**Counts, because a bare list under-answers the question it is asked.** "Which
labels exist" is rarely the end of it: a label nobody uses is a label to retire,
and one carrying half the board is the axis the reader actually sorts on.
Counts are open issues carrying the label.

Three states for a count, never two — this repo's house rule
(`docs/validators.md` §"Declining instead of guessing"):

* every open issue was read → a count is exact, and ``0`` genuinely means dead;
* the enumeration hit its cap → counts are a **floor**, rendered ``>=N``, with
  the cap and its knob named. A label used only by the 301st of 300 issues
  would otherwise read as unused, and "unused" is the input to deleting it;
* the enumeration could not be read at all → every count is ``?``. Never ``0``:
  "I did not look" and "nothing found" are the same character otherwise, which
  is the defect this repository keeps paying for.

**Grouping is inferred, and says so.** `priority-*` / `lane-*` is how the list
is read, but a prefix is a convention of whoever created the labels, not a
GitHub feature — there is no API field for it. So the split is derived from the
names, only where at least two labels share a prefix (a family of one is not a
family), and the header states that it was derived.

**`tally=PREFIX` answers the burn-down instead of the vocabulary** (#1084).
The rolling-cohort rule in `/opensource-manager` turns on one comparison — *is
each cohort smaller than the last?* — and it is a group-by over one label
family, which nothing rendered. So every tick produced the same thirty
characters of `gh issue list --json labels -q 'group_by'`, rewritten from
scratch each session and unwritable by a fresh agent.

Two things the per-label listing above cannot give it, and both carry the
decision:

* **the NONE bucket.** This op counts labels that exist. The number the freeze
  rule turns on is how many open issues carry *no* label of the family — the
  ones that escaped it — and that is invisible to a per-label listing by
  construction. Same reason `gh-issues:nomilestone` is a flag rather than an
  absence in a milestone listing.
* **the closed half.** `cohort-1 frozen 72 open 48` is the line the maintainer
  reports, and open counts alone leave the denominator hand-rolled — an op that
  does not answer the question it was filed for. So `frozen` is open+closed.

The tally's numbers come from GitHub's **search** API, one query per cell,
rather than from the enumeration above: `is:issue is:closed label:X` is exact
and cheap, where enumerating every closed issue would hit a cap and render a
*floor* as a denominator — which makes a burn-down look better than it is. The
sum is only a sum when both sides were read: a cell that did not answer is `?`,
and `?` poisons `frozen` rather than counting as zero.

The prefix is a parameter and never assumed: `claude-remember` spells priority
`priority:high` and has no `lane-*` family at all, so a family with no labels
must read as *no labels in this family* and not as an all-NONE board.

Usage:
    gh-labels                     the vocabulary, grouped, with open counts
    gh-labels:tally=cohort-       one family's open/closed/frozen, + NONE
    repo:OWNER/NAME gh-labels     another repo's vocabulary
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)

import _repo_target  # noqa: E402  (the repo this call is about, when not cwd's)
import _untrusted  # noqa: E402  (label names and descriptions are remote text)

# How many open issues are enumerated for the counts. One call, bounded: the
# question is "what can I tag with", and an op answering it must not turn into
# a walk of the whole tracker. Past this the counts are a floor and say so,
# which is a bounded honest answer rather than a slow exact one.
DEFAULT_ISSUE_CAP = 400

# Separators a label prefix is spelled with. `-` is this repo, `:` is
# claude-remember, `/` is common elsewhere. Not configurable: it is a display
# grouping, and a wrong guess costs a heading, not a decision.
_PREFIX_RE = re.compile(r"^([A-Za-z0-9_]+[-:/])")

# A prefix has to be shared to be a family. One `solo-thing` does not make
# `solo-` a group; it makes a heading over a single row, which is noise.
MIN_GROUP = 2

_UNKNOWN = "?"

# Labels per family, not calls: a family costs `2N + 2` search calls and
# GitHub's search API allows 30 a minute, so 14 labels is 30 calls and the
# bound is arithmetic rather than taste. It was 30 for one commit, which is 62
# calls — the op rate-limiting itself into a board whose second half reads `?`,
# which is a partial read wearing the shape of a complete one and the exact
# failure the refusal exists to prevent. Raise it knowing what it buys.
DEFAULT_TALLY_MAX = 14

# What the bound is really about. Named so the refusal can show its working
# instead of asserting a number.
SEARCH_CALLS_PER_LABEL = 2
NONE_BUCKET_CALLS = 2

# Small on purpose, for the same limiter. The realistic family is three to
# seven labels.
SEARCH_WORKERS = 4

# What may appear in a `tally=` prefix. A label name is remote text that lands
# inside a double-quoted search term, so a `"` would end the term and the rest
# would be read as query syntax — `label:"x" OR is:pr` is a different question
# with a plausible-looking answer. Refused rather than escaped: GitHub's search
# grammar has no documented escape for a quote inside a term, so there is
# nothing to escape it *to*.
_QUERY_UNSAFE = re.compile('["\r\n]')


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
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
    if "401" in s or "unauthorized" in s or "not logged in" in s:
        return "ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied reading {what}. Check repo access."
    if "404" in s or "not found" in s:
        return (f"ERROR: {what} not found {_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")
    return f"ERROR: gh failed reading {what}: {(stderr or '').strip()}"


def fetch_labels() -> tuple[list[dict] | None, str]:
    """`(rows, error)` — the repo's label set, or a sentence saying why not.

    `--paginate` rather than a single page: a repo past 100 labels would
    otherwise be silently truncated, and a truncated vocabulary is exactly the
    input that makes an agent invent a name.
    """
    try:
        r = _gh(["gh", "api", "--paginate",
                 _repo_target.api_path("labels?per_page=100")])
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
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
    """`(counts, capped, n_issues)` — `(None, False, 0)` when unread.

    `None` is the whole point of the return shape. An empty dict and an
    unreadable enumeration are the same object otherwise, and they render as
    "every label is unused" and "I could not look" respectively — opposite
    facts, one of which is an argument for deleting labels.

    Every spawn failure is caught, including the `FileNotFoundError` Windows
    raises where POSIX often does not (#997): this call is an enrichment, and
    an enrichment that escapes takes the answer down with it.
    """
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
    # `>=` rather than `==`: reading a full page as a complete one is the
    # optimistic half of this bug, and it costs nothing to treat an exactly
    # full read as possibly truncated.
    return counts, len(rows) >= cap, len(rows)


def parse_args(argv) -> tuple[str, str]:
    """`(prefix, error)`. `("", "")` is the plain vocabulary render.

    An unrecognised token is REFUSED rather than dropped, the rule `gh-issues`
    and `gh-prs` already carry: a full vocabulary printed for a caller who
    asked for one family reads as the answer to the question they asked, and
    nothing in it says the argument was ignored.
    """
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
    """The labels in one family, sorted. Empty is a real answer, not an error."""
    return sorted(str(n) for n in names if str(n).startswith(prefix))


def search_query(repo: str, state: str, within=(), without=()) -> str:
    """One search-API question.

    `is:issue` on every one of them: `gh issue list` excludes pull requests and
    the counts above say so, and a tally that quietly included them would not
    be comparable with the vocabulary render it sits next to.
    """
    parts = [f"repo:{repo}", "is:issue", f"is:{state}"]
    parts += [f'label:"{n}"' for n in within]
    parts += [f'-label:"{n}"' for n in without]
    return " ".join(parts)


def search_count(query: str) -> int | None:
    """`total_count` for one query, or `None` when it did not answer.

    `None` rather than 0, all the way to the cell. A rate-limited or forbidden
    search and a family nobody has used render as the same character otherwise,
    and one of them is an argument that a cohort is finished.
    """
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
    """`(rows, capped)` — open issues with their labels, `None` when unread.

    Separate from `fetch_counts` because the tally needs issue *numbers* to
    name a filing error, and because leaving the vocabulary path's call
    byte-identical means this change cannot move a number nobody asked it to.
    """
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
    """Issues carrying more than one label of the family.

    A filing error, not a row to join with a comma. Silently counting such an
    issue in two cohorts makes both burn-downs wrong and neither of them say so.
    """
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


def cell(n: object) -> str:
    return _UNKNOWN if n is None else str(n)


def frozen_cell(open_n: object, closed_n: object) -> str:
    """open+closed, and `?` the moment either side is unread.

    The whole reason the tally exists is that `frozen` is the denominator a
    human is asked to trust over weeks. A sum that silently treats an unread
    cell as zero is a partial read rendering as a total — this repository's
    most-filed defect, arriving on the one number nobody would re-derive.
    """
    if open_n is None or closed_n is None:
        return _UNKNOWN
    return str(int(open_n) + int(closed_n))


def repo_name() -> str:
    """`owner/repo` for the header, `""` when it could not be established.

    Named rather than assumed. This op exists because the vocabulary is a
    property of *a repository* and differs between them — a list printed under
    a header that does not say which repo it came from is the same trap one
    layer out, and the `repo:OWNER/NAME` form makes the cwd a bad default guess.
    """
    target = _repo_target.target()
    if target:
        return target
    try:
        r = _gh(["gh", "repo", "view", "--json", "nameWithOwner"], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    return str(d.get("nameWithOwner") or "") if isinstance(d, dict) else ""


def group_of(name: str) -> str:
    """The label's prefix family, or `""` when it has none."""
    m = _PREFIX_RE.match(name or "")
    return m.group(1) if m else ""


def grouped(names: list[str], min_group: int = MIN_GROUP) -> list[tuple[str, list[str]]]:
    """`[(group, names)]`, families first by name, then the ungrouped rest."""
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
    """The count cell, in the vocabulary of whichever of the three states holds."""
    if counts is None:
        return _UNKNOWN
    n = counts.get(name, 0)
    return f">={n}" if capped else str(n)


def tally_main(prefix: str, rows: list[dict], target: str) -> int:
    """One family's burn-down. `rows` is the already-fetched label list."""
    where = f" — {target}" if target else ""
    print(f"# Label tally — `{_untrusted.flat(prefix)}`{where}")
    if not target:
        print("ERROR: the repository could not be named, and every count here "
              "is a `repo:` query — there is nothing to ask about. Name it "
              "with a leading repo:OWNER/NAME op.")
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

    # Guarding the caller's prefix was not enough: the *label names* are the
    # remote half, and GitHub permits a `"` in one. A name carrying a quote
    # closes the term and the remainder is read as query syntax — `label:"x"
    # OR is:pr` is a different question returning a plausible number. Refused,
    # loudly, rather than escaped: GitHub's search grammar documents no escape
    # for a quote inside a term, so there is nothing to escape it to, and a
    # silently dropped label would take a whole cohort out of the burn-down.
    unsafe = [n for n in members if _QUERY_UNSAFE.search(n)]
    if unsafe:
        shown = ", ".join(repr(n) for n in unsafe[:5])
        print(f"ERROR: {len(unsafe)} label(s) in this family carry a quote or "
              f"newline and cannot be put in a search term: {shown}. Counting "
              f"the rest would silently omit them from every row and from the "
              f"NONE bucket, which is a wrong burn-down that looks right. "
              f"Rename them, or narrow the prefix past them.")
        return 1

    cap = _env_int("GH_LABELS_TALLY_MAX", DEFAULT_TALLY_MAX)
    if len(members) > cap:
        calls = len(members) * SEARCH_CALLS_PER_LABEL + NONE_BUCKET_CALLS
        print(f"ERROR: {len(members)} labels start with "
              f"`{_untrusted.flat(prefix)}`, past the {cap} this op will "
              f"query. That family would cost {calls} search calls "
              f"({SEARCH_CALLS_PER_LABEL} per label plus "
              f"{NONE_BUCKET_CALLS} for the NONE bucket) against an API that "
              f"allows 30 a minute, so the board's later cells would read `?` "
              f"because the limiter cut in — a partial read in the shape of a "
              f"complete one. Narrow the prefix, or raise "
              f"GH_LABELS_TALLY_MAX={len(members)} accepting that cost.")
        return 1

    # Keyed by a `(label, state)` tuple rather than a joined string: a label
    # name is remote text and may contain whichever separator character one
    # picks, so a join is a collision waiting for a label to be renamed.
    jobs: list[tuple[tuple[str, str], str]] = []
    for name in members:
        jobs.append(((name, "open"),
                     search_query(target, "open", within=[name])))
        jobs.append(((name, "closed"),
                     search_query(target, "closed", within=[name])))
    jobs.append((("", "open"), search_query(target, "open", without=members)))
    jobs.append((("", "closed"),
                 search_query(target, "closed", without=members)))

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(SEARCH_WORKERS, len(jobs))) as pool:
        counts = dict(zip([k for k, _ in jobs],
                          pool.map(search_count, [q for _, q in jobs])))

    issue_cap = _env_int("GH_LABELS_ISSUE_CAP", DEFAULT_ISSUE_CAP)
    open_rows, rows_capped = fetch_open_issue_rows(issue_cap)

    none_label = f"no {prefix} label"
    width = max([len(_untrusted.flat(n)) for n in members] + [len(none_label)])

    unread = sum(1 for v in counts.values() if v is None)
    print(f"Family: {len(members)} label(s) whose name starts with "
          f"`{_untrusted.flat(prefix)}`. The prefix is a repo convention "
          f"inferred from the names — GitHub has no prefix field.")
    if unread:
        print(f"Counts: {unread} of {len(counts)} cells are UNKNOWN — that "
              f"many search queries did not answer. `?` is 'not looked at', "
              f"never 0, and it poisons the `frozen` sum on its row rather "
              f"than being added as zero.")
    else:
        print("Counts: GitHub's search API, one query per cell, `is:issue` — "
              "pull requests are excluded from every number here. `frozen` is "
              "open+closed.")
    print()
    print(_untrusted.flat_note("label names"))
    print(f"  {'label':<{width}}  {'open':>6} {'closed':>7} {'frozen':>7}")
    for name in members:
        o = counts.get((name, "open"))
        c = counts.get((name, "closed"))
        print(f"  {_untrusted.flat(name):<{width}}  {cell(o):>6} "
              f"{cell(c):>7} {frozen_cell(o, c):>7}")
    o = counts.get(("", "open"))
    c = counts.get(("", "closed"))
    print(f"  {none_label:<{width}}  {cell(o):>6} {cell(c):>7} "
          f"{frozen_cell(o, c):>7}")
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

    cap = _env_int("GH_LABELS_ISSUE_CAP", DEFAULT_ISSUE_CAP)
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
