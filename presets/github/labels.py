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

Usage:
    gh-labels                     the vocabulary, grouped, with open counts
    repo:OWNER/NAME gh-labels     another repo's vocabulary
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def main() -> int:
    rows, err = fetch_labels()
    if rows is None:
        print(err)
        return 1

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
