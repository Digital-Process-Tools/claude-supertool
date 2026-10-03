#!/usr/bin/env python3


















































































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

import _declared_workflows  
import _filter_tokens  
import _untrusted  


















PR_LIST_FIELDS = "number,title,mergedAt,url,mergeCommit"

BOUNDARY_RESOLVED = "RESOLVED"
BOUNDARY_AMBIGUOUS = "AMBIGUOUS"
BOUNDARY_UNRESOLVED = "UNRESOLVED"

COUNT_EXACT = "EXACT"
COUNT_LOWER_BOUND = "LOWER BOUND"
COUNT_UNVERIFIED = "UNVERIFIED"
COUNT_UNKNOWN = "UNKNOWN"




DEFAULT_LIMIT = 100
MAX_LIMIT = 500

GIT_TIMEOUT = 15
GH_TIMEOUT = 45



VERSION_TAG = re.compile(r"^v?[0-9]+\.[0-9]+(\.[0-9]+)*")




PR_IN_SUBJECT = re.compile(r"\(#([0-9]+)\)\s*$")

FRAGMENT_NAME = re.compile(
    r"^[0-9]+\.(added|changed|deprecated|removed|fixed|security)(\.|$)",
    re.IGNORECASE)






def parse_instant(value: object) -> Optional[datetime]:















    return _filter_tokens.parse_iso_instant(value)


def split_tagged_commit(rows, boundary_sha):









































    if not boundary_sha:
        return list(rows), None, False
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
    return rest, tagged, True


def filter_merged(rows, boundary):












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






def _is_version(name: str) -> bool:
    return bool(VERSION_TAG.match(name or ""))


def select_tag(tags, requested: str = ""):
















    notes = []
    by_name = {t.get("name"): t for t in tags}

    if requested:


        wanted = requested
        if wanted.startswith("refs/tags/"):
            wanted = wanted[len("refs/tags/"):]
        chosen = by_name.get(wanted)
        if chosen is None:



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



    if any(t.get("reachable") is None for t in versioned):
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            "tag reachability from the default branch could not be measured, "
            "so a newer tag cut from another branch would not have been seen.")



    for tag in undatable:
        if not _is_version(str(tag.get("name") or "")):
            continue
        state = BOUNDARY_AMBIGUOUS
        notes.append(
            f"'{_untrusted.flat(str(tag.get('name')))}' is version-shaped "
            f"but its commit date could not be parsed "
            f"({_untrusted.flat(str(tag.get('commit_date')))}), so it took "
            f"no part in choosing the newest tag.")




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






def count_state(*, kept: int, limit: int, undated: int, unreconciled: int,
                page: Optional[int] = None):













    rows = kept if page is None else page
    if undated or unreconciled:
        return COUNT_UNVERIFIED, f"{kept} (UNVERIFIED)"
    if rows >= limit:
        return COUNT_LOWER_BOUND, f">={kept}"
    return COUNT_EXACT, str(kept)


def page_note(*, page: int, limit: int) -> str:











    if page >= limit:
        return (f"merged PRs: gh search index, page limit {limit} — PAGE FULL, "
                f"so this is a lower bound and more may exist. Raise it with "
                f"per=N.")
    return f"merged PRs: gh search index, page limit {limit}"






def count_fragments(directory: str):








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






def numbers_from_subjects(subjects):







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







    return (sorted(set(api_numbers) - set(git_numbers)),
            sorted(set(git_numbers) - set(api_numbers)))






def repo_target_refusal(target, tag: str = "") -> str:

























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






def _run(argv, timeout):






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


            "commit_date": deref_date or own_date,
            "tag_date": own_date if objtype == "tag" else "",




            "sha": (deref_sha or own_sha)[:7],
            "full_sha": (deref_sha or own_sha),
            "reachable": (None if reachable_names is None
                          else name in reachable_names),
        })
    note = ("" if reachable_names is not None
            else "tag reachability could not be measured")
    return tags, note


def read_subjects(tag_name: str, branch_ref: str):

    if not branch_ref:
        return None, "no default branch ref to walk"
    ok, out, reason = _run(
        ["git", "log", "--format=%s", f"{tag_name}..{branch_ref}"], GIT_TIMEOUT)
    if not ok:
        return None, reason
    return _untrusted.split_lines(out), ""






def render(*, boundary_state, chosen, notes, rows, undated, count_text,
           count_state, fragments, only_api, only_git, unattributed, sources):







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

    out.extend(unplaced_note(undated))

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







class Boundary(NamedTuple):











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






    ok, out, _reason = _run(["git", "rev-parse", "--show-toplevel"], GIT_TIMEOUT)
    root = out.strip() if ok and out.strip() else os.getcwd()
    return os.path.join(root, "changelog.d")


def default_workflows_dir() -> str:



    ok, out, _reason = _run(["git", "rev-parse", "--show-toplevel"], GIT_TIMEOUT)
    root = out.strip() if ok and out.strip() else os.getcwd()
    return os.path.join(root, _declared_workflows.WORKFLOW_DIR)


def not_gated_by_push_workflows(directory=None):






























    directory = directory if directory is not None else default_workflows_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        return None, f"{directory} could not be listed ({exc})"
    out = []
    unreadable = []
    for fname in names:
        if not fname.lower().endswith((".yml", ".yaml")):
            continue
        path = os.path.join(directory, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            unreadable.append(fname)
            continue
        triggers = _declared_workflows.parse_triggers(text)
        if _declared_workflows.is_push_triggered(triggers) is False:
            out.append({
                "name": _declared_workflows.parse_name(text, fname),
                "triggers": triggers,
            })
    out.sort(key=lambda w: w["name"])
    note = ("could not open " + ", ".join(sorted(unreadable))) if unreadable else ""
    return out, note


def merge_order_rows(rows) -> list:








    out = [_untrusted.flat_note("PR titles"), _untrusted.open_marker()]
    for row in rows:
        out.append(f"  #{row.get('number')}  "
                   f"{_untrusted.flat(str(row.get('mergedAt') or ''))}  "
                   f"{_untrusted.flat(str(row.get('title') or ''))}")
    out.append(_untrusted.close_marker())
    return out


def unplaced_note(undated) -> list:








    if not undated:
        return []
    return [
        "",
        f"UNPLACED: {len(undated)} merged PR(s) carry a mergedAt this op could "
        f"not parse, so they were neither counted nor ruled out: "
        + ", ".join("#%s" % r.get("number") for r in undated),
    ]


def not_applicable_note() -> list:







    return [
        "release gate: NOT APPLICABLE — the boundary is a date, not a tag.",
        "  The local-history cross-check walks TAG..BRANCH and has no tag to "
        "walk from; the changelog.d count is a statement about a release. "
        "Neither ran, and neither is being reported as clean.",
        "  For the release gate: gh-prs:merged-since=v0.34.0,state=merged",
    ]


def gate_exit(boundary_state: str, count_state: str) -> int:













    if boundary_state == BOUNDARY_RESOLVED and count_state == COUNT_EXACT:
        return 0
    return 1


def assess(*, rows, boundary, per_page, fetched, narrowed_by=(),
           repo_targeted=False, changelog_dir=None, workflows_dir=None):


















    sources = list(boundary.sources)
    notes = list(boundary.notes)
    tag_name = str((boundary.tag or {}).get("name") or "")


    rest, tagged, compared = split_tagged_commit(rows, boundary.sha)
    if not compared:
        sources.append(
            "boundary PR: NOT COMPARED — the boundary resolved to a tag "
            "carrying no commit sha, so no row's merge commit was tested "
            "against anything. This is NOT 'no row matched': the comparison "
            "did not run, and the slice below rests on the clock alone")
    elif tagged is not None:
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


    directory = default_changelog_dir() if changelog_dir is None else changelog_dir
    frag_count, frag_sections, frag_note = count_fragments(directory)
    if frag_count is None:
        sources.append(f"changelog.d: NOT READ — {frag_note}")
    else:
        sources.append(f"changelog.d: READ — {frag_count} fragment(s) under "
                       f"{_untrusted.flat(directory)}")





    excluded, wf_note = not_gated_by_push_workflows(workflows_dir)
    if excluded is None:
        sources.append(f"release scope: NOT READ — {wf_note}")
    else:
        if wf_note:
            sources.append(
                f"release scope: {wf_note} — the set named below may be "
                f"incomplete")
        if excluded:
            wf_names = ", ".join(f"`{_untrusted.flat(w['name'])}`" for w in excluded)
            sources.append(
                f"release scope: {len(excluded)} workflow(s) in "
                f"{_declared_workflows.WORKFLOW_DIR} never produce a run "
                f"from a push, so this release is NOT gated by them: "
                f"{wf_names}")
            for w in excluded:
                triggers = w["triggers"] or []







                if any(str(t).startswith("pull_request") for t in triggers):
                    sources.append(
                        f"  · `{_untrusted.flat(w['name'])}` is "
                        f"pull_request-only — it already checked every PR "
                        f"that produced this delta before it merged, so "
                        f"nothing here is unchecked, only unrepeated at the "
                        f"tag")
                else:
                    sources.append(
                        f"  · `{_untrusted.flat(w['name'])}` triggers on "
                        f"{', '.join(_untrusted.flat(str(t)) for t in triggers) or 'nothing a push reaches'} "
                        f"— a regression only it would catch is UNMEASURED "
                        f"at release time and is caught, if at all, by its "
                        f"own schedule, not by this gate")

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
