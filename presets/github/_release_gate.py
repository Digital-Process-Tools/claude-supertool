#!/usr/bin/env python3
"""`gh-since-tag` — the two numbers the release gate reads, in one call (#1209).

The auto-release trigger is defined in terms of **merged PRs since the last tag**
and **unreleased changelog fragments**. No op answered either, so both were
hand-rolled every tick from two unrelated commands, and on 2026-08-09 the
hand-rolled version printed:

    merged since tag: 0
    unreleased fragments: 7

Zero merges beside seven fragments. The correct answer was 6. The cause was a
**string** comparison: `gh` returns `2026-08-09T16:07:45Z` and
`git show -s --format=%cI` returns `2026-08-09T17:13:43+02:00`, and
`"16:07:45Z" > "17:13:43+02:00"` is False at the second character, so every PR
merged after the tag was filtered out as merged before it. Two clocks, one
lexicographic compare, and a release silently delayed.

So: **every timestamp here is parsed to an instant before it is compared**, and
the two numbers are rendered together precisely because their disagreement is
what caught the bug. `render` states the contradiction as a finding rather than
printing the pair and hoping somebody looks twice.

## Three states everywhere, because the number decides whether a release ships

The boundary:

* ``RESOLVED`` — one defensible tag. The count is a trigger input.
* ``AMBIGUOUS`` — more than one defensible boundary exists. A count is still
  printed *against the named tag*, and it is explicitly **not** a trigger input.
  Silently picking one of two boundaries is how the confident zero happened.
* ``UNRESOLVED`` — no boundary at all. The count is ``?``, never ``0``.

The count:

* ``EXACT`` — the page was not full, every row carried a parsable merge instant,
  and the two sources agree.
* ``LOWER BOUND`` — the page filled. `>=N`, never `N`: a capped page reads as
  fewer merges than there are, which is the same failure one layer along.
* ``UNVERIFIED`` — a row could not be placed in time, or the two sources
  disagree. A doubt is not a number.
* ``UNKNOWN`` — the read did not happen.

## What "the last tag" is, and what happens when that has no clean answer

Stated as decisions, because each of them was a place to guess:

1. **Boundary is an instant, not an ancestry cut** — the tagged *commit's*
   committer date. That is the clock the maintainer's script used and the one
   `mergedAt` can be compared against. An annotated tag's own tagger date is
   deliberately not used: a tag object created an hour after the commit does not
   move what is inside the release.
2. **Not every tag is a release.** Only version-shaped names (`v0.31.0`,
   `0.3.2` — this repo carries both spellings) are candidates for the *default*
   boundary. A `wip-241` tag is disclosed and skipped, not selected. An explicit
   ``gh-since-tag:TAG`` accepts any name.
3. **Tags and the default branch can disagree.** The default boundary must be
   reachable from the default branch. A version-shaped tag that is *newer* and
   *not* reachable — a release cut from a branch — makes the boundary
   ``AMBIGUOUS`` and is named, because both readings are defensible and the two
   give different counts. An older unreachable tag is irrelevant and says
   nothing.
4. **Reachability that could not be measured is not a yes.** If
   `git tag --merged` cannot run, every tag's reachability is unknown and the
   boundary is ``AMBIGUOUS`` — the off-branch rival test is exactly the check
   that did not happen.
5. **An explicit tag is never silently substituted.** A name that does not exist
   is ``UNRESOLVED``; falling back to the newest would answer a question nobody
   asked.

## Two sources of truth, on purpose

The rows come from `gh pr list --search "merged:>INSTANT"`, which is GitHub's
search index. The same window is read a second time from **local git history** —
squash subjects ending `(#N)` on the default branch — and the two sets are
reconciled. A search index that lags returns a short list with no marker, which
is the confident zero wearing a different hat; the local read cannot lag the
same way, and disagreement renders as ``UNVERIFIED`` rather than as a number.

The local read is of **refs as they stand on disk**. Nothing here fetches,
because this op is read-only; a stale `origin/master` is disclosed as the source
rather than corrected.
"""
from __future__ import annotations

import glob as _glob
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _filter_tokens  # noqa: E402  (the one instant parser, shared with gh-prs)
import _untrusted  # noqa: E402  (PR titles and tag names are remote text — #851)

# `json` (above), `_repo_target` and `prs` used to be imported and all three were
# unused — ruff reports them F401 on demand, but no CI step runs `ruff check .`
# (see the note in .github/workflows/tests.yml), and supertool's ruff validator
# only reports errors an edit NEWLY introduces, so imports already dead when
# this module was split out of since_tag.py were invisible to both. The `prs`
# one also cited `read_merged_prs`, a function #1405 deleted. This module is a
# library now — no `main()`, no registry entry — and the argv it once needed
# from `prs` is built by `prs` itself.

# The fields this op renders, which are not the board's. `prs._LIST_FIELDS`
# carries `statusCheckRollup` — dozens of check runs per PR, over a page of up
# to 500 merged ones — and none of it is read here. The shared thing is the
# query; the payload is the caller's (#1411).
# `mergeCommit` is what makes the boundary an identity rather than a clock —
# see `split_tagged_commit`. One `{"oid": "<40hex>"}` per row, against
# `statusCheckRollup`'s dozens of check runs per row, which is the field this
# set exists to stay out of (#1411).
PR_LIST_FIELDS = "number,title,mergedAt,url,mergeCommit"

BOUNDARY_RESOLVED = "RESOLVED"
BOUNDARY_AMBIGUOUS = "AMBIGUOUS"
BOUNDARY_UNRESOLVED = "UNRESOLVED"

COUNT_EXACT = "EXACT"
COUNT_LOWER_BOUND = "LOWER BOUND"
COUNT_UNVERIFIED = "UNVERIFIED"
COUNT_UNKNOWN = "UNKNOWN"

# One page of merged PRs. 100 is gh's own page size; past it gh paginates and
# the wall-clock cost stops being a single call. The cap is disclosed as a lower
# bound rather than raised silently.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

GIT_TIMEOUT = 15
GH_TIMEOUT = 45

# `v0.31.0`, `0.3.2`, `v1.0.0-rc1` — a leading `v` is optional because this
# repository's own history carries both spellings.
VERSION_TAG = re.compile(r"^v?[0-9]+\.[0-9]+(\.[0-9]+)*")

# A squash merge's subject ends `(#1214)`. Anchored at the end on purpose: a
# `Revert (#99) because it broke` names a PR in passing and is not the merge
# reference, so it is reported as unattributed rather than counted.
PR_IN_SUBJECT = re.compile(r"\(#([0-9]+)\)\s*$")

FRAGMENT_NAME = re.compile(
    r"^[0-9]+\.(added|changed|deprecated|removed|fixed|security)(\.|$)",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Instants
# ---------------------------------------------------------------------------

def parse_instant(value: object) -> Optional[datetime]:
    """One clock for two vocabularies, or ``None``.

    `gh` emits a `Z` suffix; `git` emits a numeric offset. `datetime.fromisoformat`
    on the supported floor (3.9) does not accept `Z`, so it is rewritten before
    parsing rather than compared as text. A value that cannot be parsed returns
    ``None`` and every caller treats that as "could not place", never as "old".

    The body lives in `_filter_tokens` since #1411, because `gh-prs` grew a
    `merged-since=` boundary and needs the identical reading. Two parsers over
    the same two spellings is what #1209 was: the whole bug was one comparison
    that did not know the other side's clock.
    """
    return _filter_tokens.parse_iso_instant(value)


def split_tagged_commit(rows, boundary_sha):
    """`(rest, tagged)` — the row whose merge commit IS the tagged commit.

    The boundary is a **commit**. `TAG..BRANCH` excludes it by construction, so
    the local side never sees the PR that produced the tagged commit, and the
    API side has to exclude the same row or the two disagree at every release.

    That exclusion used to be `filter_merged`'s "strictly after", which only
    works when the two clocks agree to the second. They do not: GitHub stamps
    `merged_at` when it records the merge, after the commit has been written.
    Measured across this repository's own releases — tagged commit's committer
    date vs the release PR's `merged_at`:

        v0.30.0  #1161  06:29:26Z  ==  06:29:26Z
        v0.31.0  #1198  15:13:43Z  ==  15:13:43Z
        v0.32.0  #1250  22:35:33Z  <   22:35:34Z    one second later
        v0.33.0  #1289  00:39:57Z  ==  00:39:57Z
        v0.34.0  #1403  13:34:34Z  <   13:34:35Z    one second later

    So the exclusion fired on a coin flip, and on the two flips it lost the
    release PR was counted as merged-since *and* reported absent from local
    history — a structural UNVERIFIED with the count off by one (#1405). The
    tagged commit's sha equalled `merge_commit_sha` in all five.

    Identity, not the clock. Two rows it deliberately does NOT remove:

    * A row carrying no `mergeCommit` — no identity to compare is neither a
      match nor a mismatch, and guessing from the timestamp is the defect this
      function replaces. It stays in, reaches `reconcile`, and renders as the
      disagreement it is.
    * A tagged commit that was **amended** after the merge. Its sha is no
      longer the one the API recorded, the pairing correctly fails, and that
      row keeps its refusal — history was rewritten under the tag and the two
      sources genuinely do disagree.
    """
    if not boundary_sha:
        return list(rows), None
    rest = []
    tagged = None
    for row in rows:
        commit = row.get("mergeCommit")
        oid = commit.get("oid") if isinstance(commit, dict) else None
        if oid and str(oid) == str(boundary_sha):
            if tagged is None:
                tagged = row
            continue
        rest.append(row)
    return rest, tagged


def filter_merged(rows, boundary):
    """`(kept, undated)` — rows merged strictly after `boundary`, in merge order.

    Strictly after, but that is no longer what excludes the tagged commit's own
    PR — `split_tagged_commit` does, by sha, because the two clocks are up to a
    second apart (#1405). What `>` still buys is a local re-check of the server
    filter: `--search merged:>INSTANT` is an optimisation, this comparison is
    the guarantee, and it is the one #1209 was filed about.

    `undated` is the third state. A row whose `mergedAt` will not parse has not
    been shown to be outside the window — it has not been placed at all — and
    dropping it silently is the same shape as the bug this op exists for.
    """
    kept = []
    undated = []
    for row in rows:
        when = parse_instant(row.get("mergedAt"))
        if when is None:
            undated.append(row)
            continue
        if when > boundary:
            kept.append((when, row))
    kept.sort(key=lambda pair: (pair[0], pair[1].get("number") or 0))
    return [row for _when, row in kept], undated


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def _is_version(name: str) -> bool:
    return bool(VERSION_TAG.match(name or ""))


def select_tag(tags, requested: str = ""):
    """`(chosen, state, notes)` — which tag the "since" boundary is, and how sure.

    `tags` are dicts with `name`, `commit_date`, `sha`, `objtype` and
    `reachable` (``True`` / ``False`` / ``None`` for "could not be measured").
    Order does not matter; this function sorts.

    The rules are the five decisions in the module docstring. Nothing here picks
    silently between two defensible answers — that is what ``AMBIGUOUS`` is for.
    """
    notes = []
    by_name = {t.get("name"): t for t in tags}

    if requested:
        chosen = by_name.get(requested)
        if chosen is None:
            # Newest first, not alphabetical: `0.3.0, 0.3.1, 0.3.2` is the
            # least useful eight this repository could offer a caller who has
            # just mistyped a tag.
            recent = sorted(
                tags,
                key=lambda t: (parse_instant(t.get("commit_date"))
                               or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True)
            known = ", ".join(str(t.get("name")) for t in recent[:8]) or "none"
            notes.append(
                f"tag '{_untrusted.flat(requested)}' does not exist on this "
                f"repository, and the newest tag is NOT substituted for it — "
                f"that would answer a question nobody asked. Known tags "
                f"include: {_untrusted.flat(known)}")
            return None, BOUNDARY_UNRESOLVED, notes
        if chosen.get("reachable") is False:
            notes.append(
                f"'{_untrusted.flat(requested)}' is not an ancestor of the "
                f"default branch. The boundary is still its commit's instant, "
                f"so the count answers 'merged after that moment', not "
                f"'merged into the branch after that commit'.")
        elif chosen.get("reachable") is None:
            notes.append(
                f"whether '{_untrusted.flat(requested)}' is reachable from the "
                f"default branch could not be measured.")
        return chosen, BOUNDARY_RESOLVED, notes

    if not tags:
        notes.append("no tag exists on this repository, so there is no "
                     "boundary to count from. This is not a zero.")
        return None, BOUNDARY_UNRESOLVED, notes

    undatable = [t for t in tags if parse_instant(t.get("commit_date")) is None]
    versioned = [t for t in tags if _is_version(str(t.get("name") or ""))]
    if not versioned:
        names = ", ".join(sorted(str(t.get("name")) for t in tags)[:8])
        notes.append(
            "no tag on this repository has a version-shaped name (vN.N.N or "
            f"N.N.N), so none is a release boundary candidate. Tags present: "
            f"{_untrusted.flat(names)}. Name one explicitly with "
            "gh-prs:merged-since=TAG,state=merged.")
        return None, BOUNDARY_UNRESOLVED, notes

    candidates = [t for t in versioned
                  if t.get("reachable") is not False
                  and parse_instant(t.get("commit_date")) is not None]
    if not candidates:
        notes.append(
            "every version-shaped tag is either off the default branch or "
            "carries a date that could not be parsed, so no default boundary "
            "can be chosen. Name one explicitly with "
            "gh-prs:merged-since=TAG,state=merged.")
        return None, BOUNDARY_UNRESOLVED, notes

    candidates.sort(key=lambda t: parse_instant(t.get("commit_date")), reverse=True)
    chosen = candidates[0]
    chosen_at = parse_instant(chosen.get("commit_date"))
    state = BOUNDARY_RESOLVED

    # (a) A version-shaped tag that is newer and off the branch — a release cut
    #     from a branch. Both readings are defensible and they differ.
    for rival in versioned:
        if rival.get("reachable") is not False:
            continue
        rival_at = parse_instant(rival.get("commit_date"))
        if rival_at is None or rival_at <= chosen_at:
            continue
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            f"'{_untrusted.flat(str(rival.get('name')))}' is a newer "
            f"version-shaped tag that is NOT reachable from the default branch "
            f"— a release cut elsewhere. Two boundaries are defensible here and "
            f"they give different counts.")

    # (b) A second version-shaped tag at the same instant on a different commit.
    #     Same instant on the *same* commit is one boundary, not two.
    for twin in candidates[1:]:
        if parse_instant(twin.get("commit_date")) != chosen_at:
            continue
        if twin.get("sha") == chosen.get("sha"):
            continue
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            f"'{_untrusted.flat(str(twin.get('name')))}' shares this tag's "
            f"instant but points at a different commit, so which one the "
            f"release was cut from cannot be read off the dates.")

    # (c) Reachability that could not be measured. The rival test above is the
    #     one that did not run, so its silence is not a clean result.
    if any(t.get("reachable") is None for t in versioned):
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            "tag reachability from the default branch could not be measured, "
            "so a newer tag cut from another branch would not have been seen.")

    # (d) A tag whose date will not parse was excluded from the ordering, which
    #     means it was excluded from "newest" — a claim, not an omission.
    for tag in undatable:
        if not _is_version(str(tag.get("name") or "")):
            continue
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            f"'{_untrusted.flat(str(tag.get('name')))}' is version-shaped "
            f"but its commit date could not be parsed "
            f"({_untrusted.flat(str(tag.get('commit_date')))}), so it took "
            f"no part in choosing the newest tag.")

    # Disclosed, not ambiguous: a non-release name cannot rival a release
    # boundary, but a reader looking at `git tag` will see it above the chosen
    # one and deserves to know it was skipped on purpose.
    for tag in tags:
        if _is_version(str(tag.get("name") or "")):
            continue
        tag_at = parse_instant(tag.get("commit_date"))
        if tag_at is None or tag_at <= chosen_at:
            continue
        notes.append(
            f"'{_untrusted.flat(str(tag.get('name')))}' is newer but is not "
            f"version-shaped, so it is not a release boundary candidate. "
            f"Skipped deliberately, not overlooked.")

    return chosen, state, notes


# ---------------------------------------------------------------------------
# The count's state
# ---------------------------------------------------------------------------

def count_state(*, kept: int, limit: int, undated: int, unreconciled: int,
                page: Optional[int] = None):
    """`(state, text)` for the number the release trigger reads.

    `page` is how many rows `gh` actually returned; `kept` is how many survived
    the boundary filter. **The cap is a property of the page, not of what
    survived it.** Measuring it on `kept` means a full page thinned by even one
    row renders `EXACT`, which is a confident wrong number on a truncated read —
    this op's own bug, one layer down. It defaults to `kept` only so a caller
    with a single number cannot silently get the looser check.

    Ordered worst-first. A capped page beaten by a reconciliation gap still
    reports the gap, because `>=N` reads as "at least this many" and a
    disagreement means the tool does not know that either.
    """
    rows = kept if page is None else page
    if undated or unreconciled:
        return COUNT_UNVERIFIED, f"{kept} (UNVERIFIED)"
    if rows >= limit:
        return COUNT_LOWER_BOUND, f">={kept}"
    return COUNT_EXACT, str(kept)


def page_note(*, page: int, limit: int) -> str:
    """One line about the page itself, independent of the count's state.

    `page` is the row count `gh` returned, before the boundary filter — see
    `count_state`. A page is full or it is not; how many of its rows survived
    a later filter says nothing about whether a second page exists.

    `count_state` ranks a reconciliation gap above a cap, which is right — a
    disagreement is worse than a known-short list. But ranking one above the
    other must not delete it: a full page is a fact about the read, and the
    caller who sees ``UNVERIFIED`` still needs to know the list is truncated.
    """
    if page >= limit:
        return (f"merged PRs: gh search index, page limit {limit} — PAGE FULL, "
                f"so this is a lower bound and more may exist. Raise it with "
                f"per=N.")
    return f"merged PRs: gh search index, page limit {limit}"


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------

def count_fragments(directory: str):
    """`(count, by_section, note)`. `count` is ``None`` when it was not read.

    An absent or unreadable `changelog.d/` is not an empty one. `README.md` is
    the directory's own documentation and is not a fragment; a `.md` whose name
    does not parse into a section is counted under `?` rather than dropped,
    because a fragment the release will pick up is a fragment whatever it is
    called.
    """
    if not os.path.isdir(directory):
        return None, {}, (f"changelog.d was not read ({directory} is not a "
                          f"directory) — this is not a count of zero")
    try:
        names = sorted(os.path.basename(p)
                       for p in _glob.glob(os.path.join(directory, "*.md")))
    except OSError as exc:
        return None, {}, (f"changelog.d could not be listed ({exc}) — this is "
                          f"not a count of zero")

    sections = {}
    total = 0
    for name in names:
        if name.lower() == "readme.md":
            continue
        total += 1
        match = FRAGMENT_NAME.match(name)
        key = match.group(1).lower() if match else "?"
        sections[key] = sections.get(key, 0) + 1
    return total, sections, ""


# ---------------------------------------------------------------------------
# The second source of truth
# ---------------------------------------------------------------------------

def numbers_from_subjects(subjects):
    """`(numbers, unattributed)` from squash-merge commit subjects.

    A subject with no trailing `(#N)` is a direct push, a merge commit, or a
    revert naming a PR mid-sentence. None of those is a merged PR, and all of
    them are reported: a commit nothing could attribute is exactly the row that
    makes the two sources disagree for an innocent reason.
    """
    numbers = set()
    unattributed = []
    for subject in subjects:
        match = PR_IN_SUBJECT.search(subject or "")
        if match:
            numbers.add(int(match.group(1)))
        elif str(subject or "").strip():
            unattributed.append(subject)
    return numbers, unattributed


def reconcile(api_numbers, git_numbers):
    """`(only_api, only_git)` — the two sources' disagreement, both directions.

    Both directions matter and they mean opposite things. Only-in-API is a PR
    the local refs have not seen (stale clone, or merged into another branch);
    only-in-git is a merge the search index did not return, which is the
    confident zero's own failure mode.
    """
    return (sorted(set(api_numbers) - set(git_numbers)),
            sorted(set(git_numbers) - set(api_numbers)))


# ---------------------------------------------------------------------------
# repo: targeting — refused, because only half of it could ever be honoured
# ---------------------------------------------------------------------------

def repo_target_refusal(target, tag: str = "") -> str:
    """The refusal message for a `repo:OWNER/NAME` target, or ``""``.

    `gh-prs` follows a repo target because everything it reads comes from the
    API. The release gate does not: the boundary tag, the default-branch ref,
    the commit-subject cross-check and `changelog.d/` are all **local** reads
    of the cwd's clone, and only the merged-PR list takes `--repo`.

    The fold did not delete this hazard, it moved it onto the op with the most
    callers — so the refusal is now scoped to the one filter that reaches the
    clone. `merged-since=<date>` under a target is untouched and fully
    honoured; only `merged-since=<tag>` is refused.

    Half a target is worse than none, and it is this op's own defect wearing a
    different hat. Measured before this refusal existed, with
    `repo:Digital-Process-Tools/claude-remember` from a claude-supertool
    worktree:

        boundary: RESOLVED — tag v0.31.0 at 39372ab   <- claude-supertool
        merged since tag: 0                            <- claude-remember
        unreleased fragments: 14                       <- claude-supertool

    Three numbers about two repositories under one header, and the headline was
    a confident zero. Refusing names the reason and the way to get the answer;
    rendering it would not.
    """
    value = str(target or "").strip()
    if not value:
        return ""
    flat = _untrusted.flat(value)
    named = _untrusted.flat(str(tag or "")) or "a tag"
    return "\n".join([
        f"ERROR: merged-since={named} cannot be answered for '{flat}' under a "
        f"repo: target, and half of the question will not be answered instead.",
        f"  Only the PR list can follow the target. The boundary tag, the "
        f"default branch, the git-history cross-check and changelog.d are "
        f"local reads of THIS clone, so the rows would be '{flat}'s measured "
        f"against a tag belonging to this repository — which renders as an "
        f"ordinary number and is not one.",
        "  Two ways through: run it from inside that repository's clone, or "
        "pass the boundary as a date (merged-since=YYYY-MM-DD), which reads "
        "nothing local and follows the target whole.",
    ])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _run(argv, timeout):
    """`(ok, stdout, reason)`. A tool that would not start is not a clean read.

    `FileNotFoundError` is the Windows shape of a missing binary
    (`[WinError 2]`) and it does not necessarily raise on POSIX, so it is caught
    here rather than allowed to escape past the three-state reporting (#997).
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8",
                              errors="replace")
    except FileNotFoundError:
        return False, "", f"{argv[0]} is not installed or not on PATH"
    except OSError as exc:
        return False, "", f"{argv[0]} could not be run ({exc})"
    except subprocess.TimeoutExpired:
        return False, "", f"{argv[0]} timed out after {timeout}s"
    if proc.returncode != 0:
        detail = _untrusted.split_lines((proc.stderr or proc.stdout or "").strip())
        return False, "", (detail[0] if detail else
                           f"{argv[0]} exited {proc.returncode}")
    return True, proc.stdout, ""


def default_branch_ref():
    """`(ref, note)` — the ref the boundary's reachability is measured against.

    Preference order is remote-tracking first, because the question is about the
    published branch and a local `master` can be behind or ahead of it. Every
    outcome names the ref it chose; a reachability answer is only as good as the
    ref it was asked about.
    """
    for candidate in ("refs/remotes/origin/HEAD", "refs/remotes/origin/master",
                      "refs/remotes/origin/main", "refs/heads/master",
                      "refs/heads/main"):
        ok, out, _reason = _run(
            ["git", "rev-parse", "--verify", "--quiet", candidate], GIT_TIMEOUT)
        if ok and out.strip():
            return candidate, (f"default branch: {candidate} (local ref as it "
                               f"stands; this op does not fetch)")
    ok, out, reason = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           GIT_TIMEOUT)
    if ok and out.strip():
        return out.strip(), (f"default branch: could not be identified; fell "
                             f"back to HEAD ({_untrusted.flat(out.strip())})")
    return "", f"default branch: UNKNOWN ({reason})"


def read_tags(branch_ref: str):
    """`(tags, note)`. Reachability is ``None`` when it could not be measured."""
    fmt = ("%(refname:short)\t%(objecttype)\t"
           "%(*committerdate:iso-strict)\t%(committerdate:iso-strict)\t"
           "%(*objectname)\t%(objectname)")
    ok, out, reason = _run(
        ["git", "for-each-ref", "--format", fmt, "refs/tags"], GIT_TIMEOUT)
    if not ok:
        return None, f"tags could not be read ({reason})"

    reachable_names = None
    if branch_ref:
        ok_m, out_m, _r = _run(["git", "tag", "--merged", branch_ref],
                               GIT_TIMEOUT)
        if ok_m:
            reachable_names = {line.strip()
                               for line in _untrusted.split_lines(out_m)
                               if line.strip()}

    tags = []
    for line in _untrusted.split_lines(out):
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 6:
            parts.append("")
        name, objtype, deref_date, own_date, deref_sha, own_sha = parts[:6]
        tags.append({
            "name": name,
            "objtype": objtype,
            # The tagged COMMIT's date, not the tag object's. An annotated tag
            # created an hour later does not move what is inside the release.
            "commit_date": deref_date or own_date,
            "tag_date": own_date if objtype == "tag" else "",
            # Both spellings, and neither is decoration. `sha` is what renders;
            # `full_sha` is what `split_tagged_commit` compares against the
            # API's `mergeCommit.oid`, and seven characters are an abbreviation
            # for a reader, not an identity for a comparison.
            "sha": (deref_sha or own_sha)[:7],
            "full_sha": (deref_sha or own_sha),
            "reachable": (None if reachable_names is None
                          else name in reachable_names),
        })
    note = ("" if reachable_names is not None
            else "tag reachability could not be measured")
    return tags, note


def read_subjects(tag_name: str, branch_ref: str):
    """`(subjects, reason)` — commit subjects on the branch since the tag."""
    if not branch_ref:
        return None, "no default branch ref to walk"
    ok, out, reason = _run(
        ["git", "log", "--format=%s", f"{tag_name}..{branch_ref}"], GIT_TIMEOUT)
    if not ok:
        return None, reason
    return _untrusted.split_lines(out), ""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(*, boundary_state, chosen, notes, rows, undated, count_text,
           count_state, fragments, only_api, only_git, unattributed, sources):
    """The whole render, as a list of lines. Pure, so the tests can read it.

    The two numbers sit next to each other because their contradiction is what
    caught the original bug, and the contradiction is stated rather than left
    for the reader to notice — nobody looks twice at two numbers that agree, and
    the one time they disagreed it was luck that anybody did.
    """
    frag_count, frag_sections, frag_note = fragments
    out = ["# Release gate", ""]

    if chosen is None:
        out.append(f"boundary: {boundary_state} — no tag could be chosen")
    else:
        out.append(
            f"boundary: {boundary_state} — tag "
            f"{_untrusted.flat(str(chosen.get('name')))} at "
            f"{_untrusted.flat(str(chosen.get('sha')))}, commit instant "
            f"{_untrusted.flat(str(chosen.get('commit_date')))}")
        tag_date = str(chosen.get("tag_date") or "")
        if tag_date and tag_date != str(chosen.get("commit_date") or ""):
            out.append(
                f"          (annotated; the tag object was created "
                f"{_untrusted.flat(tag_date)} — the boundary is the COMMIT "
                f"instant above)")
    for note in notes:
        out.append(f"  ! {note}")
    for source in sources:
        out.append(f"  · {source}")

    out.append("")
    out.append(f"merged since tag: {count_text}  [{count_state}]")
    if boundary_state == BOUNDARY_AMBIGUOUS:
        out.append("  ! the boundary is AMBIGUOUS, so this count is NOT a "
                   "release-trigger input. Name a tag explicitly.")
    if boundary_state == BOUNDARY_UNRESOLVED:
        out.append("  ! no boundary, so there is no count. `?` is the answer, "
                   "not `0`.")

    if frag_count is None:
        out.append(f"unreleased fragments: ?  [UNKNOWN] — {frag_note}")
    else:
        spread = ", ".join(f"{k}:{v}" for k, v in sorted(frag_sections.items()))
        out.append(f"unreleased fragments: {frag_count}"
                   + (f"  ({spread})" if spread else ""))

    if (count_state == COUNT_EXACT and count_text == "0"
            and frag_count not in (None, 0)):
        out.append("")
        out.append(
            f"CONTRADICTION: zero merges since the tag, but {frag_count} "
            f"unreleased fragment(s) exist. Fragments arrive by merging PRs, so "
            f"these two cannot both be right. #1209 was filed from exactly this "
            f"pair — the zero was wrong. Do not fire or skip a release on this.")

    if only_api or only_git:
        out.append("")
        out.append("RECONCILE: the search index and local git history disagree.")
        if only_git:
            out.append("  in local history, absent from the API: "
                       + ", ".join("#%d" % n for n in only_git))
        if only_api:
            out.append("  in the API, absent from local history: "
                       + ", ".join("#%d" % n for n in only_api))

    if undated:
        out.append("")
        out.append(f"UNPLACED: {len(undated)} merged PR(s) carry a mergedAt "
                   f"this op could not parse, so they were neither counted nor "
                   f"ruled out: "
                   + ", ".join("#%s" % r.get("number") for r in undated))

    if unattributed:
        out.append("")
        out.append(f"note: {len(unattributed)} commit(s) on the branch carry no "
                   f"trailing (#N) and are not attributable to a PR — direct "
                   f"pushes or merge commits. They are excluded from the "
                   f"git-side set, which is why it can legitimately be shorter.")

    if rows:
        out.append("")
        out.append(_untrusted.flat_note("PR titles"))
        out.append(_untrusted.open_marker())
        for row in rows:
            out.append(f"  #{row.get('number')}  "
                       f"{_untrusted.flat(str(row.get('mergedAt') or ''))}  "
                       f"{_untrusted.flat(str(row.get('title') or ''))}")
        out.append(_untrusted.close_marker())
    elif boundary_state == BOUNDARY_RESOLVED and count_state == COUNT_EXACT:
        out.append("")
        out.append("No PR has merged since this tag. This is a measured zero: "
                   "the boundary resolved, the page was not capped, every row "
                   "parsed, and both sources agree.")

    return out



# ---------------------------------------------------------------------------
# The boundary, resolved — what `gh-prs:merged-since=TAG` calls
# ---------------------------------------------------------------------------

class Boundary(NamedTuple):
    """One resolved release boundary, or the reason there is not one.

    `refusal` is non-empty exactly when `state` is not ``RESOLVED``, and
    `stamp` is empty in the same cases. That pairing is the contract the fold
    rests on: **a filter value may be refused, but it may never be picked
    between.** `gh-since-tag` could print a count against one of two defensible
    boundaries and label it AMBIGUOUS, because it owned the whole render. A
    value on somebody else's listing cannot — the board underneath it would be
    a real board, correctly built, answering a question nobody asked.
    """

    state: str
    tag: Optional[dict]
    instant: Optional[datetime]
    sha: str
    stamp: str
    branch_ref: str
    notes: list
    sources: list
    refusal: str


def _no_boundary(state, notes, sources, branch_ref, refusal, tag=None):
    return Boundary(state=state, tag=tag, instant=None, sha="", stamp="",
                    branch_ref=branch_ref, notes=list(notes),
                    sources=list(sources), refusal=refusal)


def _refusal_block(headline, notes):
    lines = [f"ERROR: {headline}"]
    for note in notes:
        lines.append(f"  ! {note}")
    return "\n".join(lines)


def resolve_boundary(requested: str = "") -> Boundary:
    """Resolve `merged-since=TAG` to a second-precision instant, in three states.

    The three states are `select_tag`'s and the fold does not change them —
    what changed is who they are reported to. ``AMBIGUOUS`` used to print a
    count with a warning attached; here it refuses outright, because the thing
    downstream is a listing rather than a verdict and a listing has no way to
    carry "this is not a trigger input".

    Local reads only, and the same ones the deleted op made: `git rev-parse`
    for the default branch, `for-each-ref` + `tag --merged` for the tags. It
    still never fetches — a stale `origin/master` is disclosed as the source
    rather than corrected.
    """
    sources = []
    branch_ref, branch_note = default_branch_ref()
    sources.append(branch_note)

    tags, tag_note = read_tags(branch_ref)
    if tags is None:
        return _no_boundary(
            BOUNDARY_UNRESOLVED, [tag_note], sources, branch_ref,
            _refusal_block(
                "merged-since= names a tag and the tag list could not be read, "
                "so there is no boundary. This is not a count of zero.",
                [tag_note]))
    if tag_note:
        sources.append(tag_note)

    chosen, state, notes = select_tag(tags, requested)
    if chosen is None:
        return _no_boundary(
            state, notes, sources, branch_ref,
            _refusal_block(
                "merged-since= could not be resolved to a boundary, and the "
                "newest tag is NOT substituted for one that does not resolve.",
                notes))

    instant = parse_instant(chosen.get("commit_date"))
    if instant is None:
        note = (f"the tag's commit date "
                f"({_untrusted.flat(str(chosen.get('commit_date')))}) could not "
                f"be parsed, so there is no instant to count from.")
        return _no_boundary(
            BOUNDARY_UNRESOLVED, notes + [note], sources, branch_ref,
            _refusal_block("merged-since= resolved to a tag with no usable "
                           "instant.", notes + [note]),
            tag=chosen)

    if state == BOUNDARY_AMBIGUOUS:
        name = _untrusted.flat(str(chosen.get("name")))
        return _no_boundary(
            state, notes, sources, branch_ref,
            _refusal_block(
                f"merged-since= — more than one boundary is defensible here, "
                f"and they give different counts. A filter value may be "
                f"refused but may not be picked between, so no board is "
                f"printed. The newest reachable candidate is '{name}'; name "
                f"the one you mean explicitly.",
                notes),
            tag=chosen)

    return Boundary(
        state=state, tag=chosen, instant=instant,
        sha=str(chosen.get("full_sha") or ""),
        stamp=instant.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        branch_ref=branch_ref, notes=notes, sources=sources, refusal="")


def default_changelog_dir() -> str:
    """`changelog.d` at the repo root, not at the cwd.

    The gate is routinely reached from a worktree subdirectory, and a relative
    `changelog.d` there resolves to nothing — which `count_fragments` would
    correctly call UNKNOWN, but pointlessly.
    """
    ok, out, _reason = _run(["git", "rev-parse", "--show-toplevel"], GIT_TIMEOUT)
    root = out.strip() if ok and out.strip() else os.getcwd()
    return os.path.join(root, "changelog.d")


def merge_order_rows(rows) -> list:
    """The merged slice's own render — number, merge instant, title.

    Not the triage board. `statusCheckRollup` on a merged PR is historical,
    `reviewDecision` is spent and `mergeable` is null, so the board's columns
    are noise here and the field set that feeds them is a cost the release gate
    was deliberately kept out of (#1411). What a merged slice wants instead is
    the merge instant and merge order, which the board has never rendered.
    """
    out = [_untrusted.flat_note("PR titles"), _untrusted.open_marker()]
    for row in rows:
        out.append(f"  #{row.get('number')}  "
                   f"{_untrusted.flat(str(row.get('mergedAt') or ''))}  "
                   f"{_untrusted.flat(str(row.get('title') or ''))}")
    out.append(_untrusted.close_marker())
    return out


def not_applicable_note() -> list:
    """The gate's own third state, on a boundary that is a date rather than a tag.

    Printed rather than omitted. A footer silent about a check that did not run
    is indistinguishable from one where the check passed, and that reading is
    this codebase's most-filed defect — here it would let `merged-since=FRIDAY`
    be mistaken for a release verdict.
    """
    return [
        "release gate: NOT APPLICABLE — the boundary is a date, not a tag.",
        "  The local-history cross-check walks TAG..BRANCH and has no tag to "
        "walk from; the changelog.d count is a statement about a release. "
        "Neither ran, and neither is being reported as clean.",
        "  For the release gate: gh-prs:merged-since=v0.34.0,state=merged",
    ]


def gate_exit(boundary_state: str, count_state: str) -> int:
    """`0` only when the boundary RESOLVED **and** the count is EXACT.

    The deleted op's contract, kept across the fold because it is the release
    trigger's actual answer and a script can gate on it. It was very nearly
    dropped here: an ordinary `gh-prs` board always exits 0, and inheriting
    that would have put the strongest available statement of "go" next to
    `merged since tag: 5 (UNVERIFIED)` — a number the tool has just finished
    saying it cannot verify. `LOWER BOUND` is not permission either: a capped
    page reads as fewer merges than there are.

    Only the tag-boundary slice has a verdict, so only it consults this. Every
    other shape of `gh-prs` still exits 0.
    """
    if boundary_state == BOUNDARY_RESOLVED and count_state == COUNT_EXACT:
        return 0
    return 1


def assess(*, rows, boundary, per_page, fetched, narrowed_by=(),
           repo_targeted=False, changelog_dir=None):
    """`(kept, lines, exit_code)` — the slice, the gate footer, and the verdict.

    **Every conditional read states whether it ran.** `gh-prs` is this repo's
    most-called op and this function is the only thing on it that touches the
    local clone, so the failure to guard against is not a wrong number — it is
    a check that quietly did not happen, reading as a check that passed. Three
    are conditional here and each appears in the footer in every case: the
    boundary-row exclusion, the local-history cross-check, and `changelog.d`.

    A cross-check that did **not** run counts as unreconciled, so the count
    renders ``UNVERIFIED`` rather than ``EXACT``. That is the point of the
    state: ``EXACT`` means two sources agreed, and "I did not look" is not
    agreement.
    """
    sources = list(boundary.sources)
    notes = list(boundary.notes)
    tag_name = str((boundary.tag or {}).get("name") or "")

    # 1. The boundary row. Identity, not the clock — see `split_tagged_commit`.
    rest, tagged = split_tagged_commit(rows, boundary.sha)
    if tagged is not None:
        sources.append(
            f"boundary PR: #{tagged.get('number')} merged AS the tagged commit "
            f"{_untrusted.flat(str((boundary.tag or {}).get('sha')))} — inside "
            f"the release, not after it. Excluded by identity rather than by "
            f"clock: GitHub stamps mergedAt after writing the commit, so the "
            f"two are up to a second apart (#1405)")
    else:
        sources.append(
            "boundary PR: none — no returned row's merge commit is the tagged "
            "commit. Expected when the release was tagged on a direct push, or "
            "when the tag is older than this page")

    kept, undated = filter_merged(rest, boundary.instant)
    sources.append(page_note(page=fetched, limit=per_page))

    # 2. The cross-check. Four outcomes, and three of them are "did not run".
    only_api, only_git, unattributed = [], [], []
    if repo_targeted:
        sources.append(
            "cross-check: DID NOT RUN — a repo: target means the rows are "
            "another repository's while the git history here is this clone's. "
            "Two populations do not reconcile")
        unreconciled = 1
    elif narrowed_by:
        sources.append(
            f"cross-check: DID NOT RUN — {', '.join(sorted(narrowed_by))} "
            f"narrows the API side only, so a gap against full local history "
            f"would be an artefact of the filter rather than a finding")
        unreconciled = 1
    else:
        subjects, subj_reason = read_subjects(tag_name, boundary.branch_ref)
        if subjects is None:
            sources.append(
                f"cross-check: DID NOT RUN — {_untrusted.flat(subj_reason)}. "
                f"The count rests on one source and is not verified")
            unreconciled = 1
        else:
            git_numbers, unattributed = numbers_from_subjects(subjects)
            only_api, only_git = reconcile(
                {r.get("number") for r in kept if r.get("number") is not None},
                git_numbers)
            unreconciled = len(only_api) + len(only_git)
            sources.append(
                f"cross-check: RAN and "
                f"{'DISAGREED' if unreconciled else 'AGREED'} — "
                f"{len(git_numbers)} PR reference(s) in "
                f"{_untrusted.flat(tag_name)}.."
                f"{_untrusted.flat(boundary.branch_ref)}")

    # 3. changelog.d. Read, or named as not read — never an implied zero.
    directory = default_changelog_dir() if changelog_dir is None else changelog_dir
    frag_count, frag_sections, frag_note = count_fragments(directory)
    if frag_count is None:
        sources.append(f"changelog.d: NOT READ — {frag_note}")
    else:
        sources.append(f"changelog.d: READ — {frag_count} fragment(s) under "
                       f"{_untrusted.flat(directory)}")

    state, text = count_state(kept=len(kept), limit=per_page,
                              undated=len(undated), unreconciled=unreconciled,
                              page=fetched)
    lines = render(
        boundary_state=boundary.state, chosen=boundary.tag, notes=notes,
        rows=kept, undated=undated, count_text=text, count_state=state,
        fragments=(frag_count, frag_sections, frag_note),
        only_api=only_api, only_git=only_git, unattributed=unattributed,
        sources=sources)
    return kept, lines, gate_exit(boundary.state, state)
