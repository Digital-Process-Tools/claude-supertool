#!/usr/bin/env python3
"""Open a pull request from a payload, and hand back what you need next (#950).

The payload route, not the colon CLI, for the same reason `gh-issue-create` has
one: a PR body is long markdown with colons, code fences and quotes, and none of
that survives `:`-tokenisation.

**The base branch is never defaulted.** `head` comes from the current branch and
`repo` from the origin remote, because both are unambiguous facts about where
you are standing. A base is not: `master` and a release branch are equally
plausible from the same cwd, and a wrong base silently retargets the merge into
a branch nobody was reviewing against. An unresolvable base is a refusal.

The receipt answers what the second call always was:

* **base and head as resolved**, and where each came from, so a wrong default is
  visible now rather than after the merge;
* **whether any checks actually started.** Zero checks renders exactly like "not
  yet pending" and is a completely different fact — a workflow that never fired
  never will, and waiting for it costs a PR its first life;
* **the issues the body links**, parsed with the same
  `_checks.closing_issue_refs` `gh-pr` uses, echoed back so a malformed `Closes`
  line is caught here instead of discovered after the merge — PR #908 shipped
  with one and nothing anywhere raised an error.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _checks  # noqa: E402
import _remote_default as _rd  # noqa: E402
import _repo_target  # noqa: E402
import _payload_keys  # noqa: E402  (unrecognised-key refusal, shared with the other three @payload ops -- #2123)
import _untrusted  # noqa: E402
import _digits  # noqa: E402  (the one ASCII-digit test — #1727)
import _publish_safety  # noqa: E402  (#2100 -- the forge-write disclosure marker)

# GitHub's own run-creation latency, measured in #585 and reused by `gh-branch`
# rather than re-guessed. Inside it, zero runs means "still coming"; past it,
# whether any workflow covers this ref is UNKNOWN — a path filter and a workflow
# that never fired look identical from here.
CHECK_CREATION_GRACE = 900

NO_CHECKS_YET = "no checks yet"
CHECKS_READ = "checks read"
CHECKS_UNKNOWN = "unknown"

# The escape hatch for #1967, named to match the one `paste`/`edit` already
# ship for the same shape (`literal_backslashes = true`, #1096) rather than
# invent a second vocabulary for the same decision.
LITERAL_BS_KEY = "literal_backslashes"

# The escape hatch for #1838 -- a `Part of #N` pull request that genuinely
# closes nothing. Named for what it records (a deliberate decision), not for
# what it disables, so a template carrying it unconditionally reads wrong on
# sight rather than looking like ordinary configuration.
NO_CLOSE_KEY = "no_close"

# Every key this op reads from a payload. Checked against the payload before
# anything is created (#2123) -- a key outside this set is refused rather
# than silently dropped. No alias vocabulary here: `body`/`body_file` are
# this op's own native names and nothing else in this repo calls a pull
# request body anything different.
ACCEPTED_KEYS = {
    "repo", "title", "base", "head", "body", "body_file", "draft", "labels",
    "assignees", "reviewers", "milestone", LITERAL_BS_KEY, NO_CLOSE_KEY,
}
ALIASES: dict = {}

_LITERAL_BS_QUOTE = re.compile(r'\\"')


def literal_backslash_quotes(text: str) -> List[tuple[int, int]]:
    """Every backslash-quote pair in a decoded body -- (1-based line, 1-based
    column) each.

    The doubled-backslash detector `paste`/`edit` already ship (#1087, #1096)
    scans a payload's own *source* for an even backslash run, because a TOML
    literal block processes no escapes and what is typed is what lands. A
    published PR body is different: `gh-pr-create` reads `body` through
    `json.load`/`tomllib`, which DOES process escapes, so the defect this
    issue is about is not in the payload source -- it is in what the escaping
    decoded to. A JSON author who means a literal quote writes a single
    escaped quote and gets a bare quote; one who (mistakenly) doubles the
    backslash first gets a real backslash immediately followed by a real
    quote in the decoded string -- #1967's own example: an `ev.get(...)`
    snippet with a stray backslash in front of each quote. So this scans the
    DECODED string for that two-character sequence, not the payload's raw
    text.
    """
    hits: List[tuple[int, int]] = []
    for m in _LITERAL_BS_QUOTE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_no = text.count("\n", 0, m.start()) + 1
        hits.append((line_no, m.start() - line_start + 1))
    return hits


def literal_backslash_quote_refusal(hits: List[tuple[int, int]]) -> str:
    """The refusal `main()` prints -- named occurrences, and the way out."""
    n = len(hits)
    shown = ", ".join(f"line {ln}, col {c}" for ln, c in hits[:5])
    more = f" (+{n - 5} more)" if n > 5 else ""
    plural = "occurrence" if n == 1 else "occurrences"
    return (
        f"ERROR: body carries {n} literal backslash-quote {plural} at "
        f"{shown}{more} -- a JSON string only needs an escaped quote for a "
        f"literal quote character; a doubled backslash before it decodes to "
        f"a real backslash followed by a real quote, and that almost never "
        f"belongs in prose. If this is the mistake, remove the extra "
        f"backslash and re-send. If the run is meant as written (a shell "
        f"snippet, a regex, a Windows path with a trailing quote), set "
        f"{LITERAL_BS_KEY} = true at the top level of the payload to "
        f"publish it unchanged.")


def no_closing_reference_refusal() -> str:
    """The refusal `main()` prints when the body closes nothing and nobody
    said that was deliberate. `no_close = true` is the escape -- a
    Part-of-#N pull request is a real, recurring case this repo's own merge
    gates protect by name (#1838), so this refusal must stay openable
    rather than becoming a gate every payload template disables.
    """
    return (
        "ERROR: body has no working closing reference (Closes #N), so "
        "merging this pull request as written would close nothing. Add "
        "Closes #N if that is wrong -- it is far cheaper now than after "
        "the merge. If this pull request genuinely closes nothing (a "
        "Part-of-#N pull request, or unrelated work), set "
        f"{NO_CLOSE_KEY} = true at the top level of the payload to say so "
        "and publish anyway.")


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def validate(payload: dict) -> str | None:
    """The refusals, before anything is created. None means the payload is fine."""
    if not payload.get("repo"):
        return ("ERROR: payload missing required field: repo — and it could not "
                "be resolved from the origin remote either.")
    if not payload.get("title"):
        return "ERROR: payload missing required field: title"
    if not payload.get("base"):
        return ("ERROR: payload missing required field: base. A base branch is "
                "never guessed by this op: `master` and a release branch are "
                "equally plausible from the same cwd, and a wrong base silently "
                "retargets the merge into a branch nobody reviewed against. "
                "Set base = \"<branch>\" in the payload.")
    if payload.get("body") and payload.get("body_file"):
        return "ERROR: payload has both body and body_file — use one"
    if payload.get("head") and payload["head"] == payload["base"]:
        return (f"ERROR: base and head are both {payload['base']!r} — a branch "
                f"cannot be merged into itself. Name the branch the change is "
                f"on as head.")
    return None


def resolve_head(payload: dict, current_branch: str,
                 err: str) -> tuple[str, str, str]:
    """`(head, source, error)`. Defaulted from the branch you are on, or refused.

    The branch you are standing on is an unambiguous fact, so it is a legitimate
    default — but only when it was actually read. A detached HEAD, or a git call
    that did not answer, is refused rather than substituted, because the failure
    mode of guessing here is a PR opened from the wrong branch.
    """
    explicit = str(payload.get("head") or "").strip()
    if explicit:
        return (explicit, "payload", "")
    if err or not current_branch:
        return ("", "", (
            f"ERROR: no head given and the current branch could not be read "
            f"({err or 'detached HEAD'}), so this op has nothing to open a PR "
            f"from. Set head = \"<branch>\" in the payload."))
    return (current_branch, "current branch", "")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def checks_section(rollup, age_secs, sha: str,
                   read_error: str = "") -> tuple[List[str], str]:
    """`(lines, state)` — and zero is never rendered as pending.

    Three states, not two. A rollup that came back with entries is read. A
    rollup that came back empty is an established zero, which is a different
    sentence from "a check is pending" and is the one that costs a PR its first
    life: the reader waits for a run that no workflow will ever create. A rollup
    that did not come back at all is UNKNOWN and is neither of those.
    """
    short = sha[:7] if sha else "?"

    if not isinstance(rollup, list):
        return ([
            f"  {CHECKS_UNKNOWN}: the check rollup did not come back "
            f"({read_error or 'no detail'}), so whether anything started on "
            f"{short} is UNKNOWN. That is not zero checks. Read it: "
            f"`gh-pr:<number>:status`",
        ], CHECKS_UNKNOWN)

    if rollup:
        # `summarize_github`, not `summarize(github_states(rollup))` (#1804):
        # a check run a later run of the same name replaced is not a live
        # failure — same discriminator #1792 gave the merge gate, so this
        # post-create summary and `gh-pr:N:status` cannot disagree about the
        # PR just created. `github_named_live`/`github_named_superseded`
        # split the disclosure the same way `gh-pr:N` does: what is still
        # red, and what stopped deciding anything but is named rather than
        # dropped.
        lines = [f"  {_checks.summarize_github(rollup)}"]
        named = [(_untrusted.flat(n), s, k, i)
                 for n, s, k, i in _checks.github_named_live(rollup)]
        lines.extend(_checks.named_disclosure(
            [e for e in named if _checks.bucket(e[1]) != "passed"]))
        superseded_named = [(_untrusted.flat(n), s, k, i)
                            for n, s, k, i in
                            _checks.github_named_superseded(rollup)]
        lines.extend(_checks.superseded_disclosure(superseded_named))
        return (lines, CHECKS_READ)

    if age_secs is None:
        return ([
            f"  zero check runs on {short}, and when the head commit landed "
            f"could not be established, so whether one is still coming is "
            f"UNKNOWN. Nothing is pending — nothing has been created.",
        ], NO_CHECKS_YET)

    if int(age_secs) <= CHECK_CREATION_GRACE:
        return ([
            f"  zero check runs on {short}. Nothing is pending — nothing has "
            f"been created yet. The head commit is {int(age_secs)}s old, inside "
            f"the ~{CHECK_CREATION_GRACE // 60}min window in which a first run "
            f"has always appeared, so one is still expected.",
        ], NO_CHECKS_YET)

    return ([
        f"  zero check runs on {short}, and the head commit is past the "
        f"~{CHECK_CREATION_GRACE // 60}min window in which a first run normally "
        f"appears. Whether any workflow covers this ref is UNKNOWN — a path "
        f"filter and a workflow that never fired look identical from here. Do "
        f"not wait for a run that may not exist; check the repo's Actions tab.",
    ], NO_CHECKS_YET)


def result_line(number: str, checks_state: str, issue_note: str) -> str:
    """One line, no newline, that survives `| tail -1`."""
    if checks_state == CHECKS_UNKNOWN:
        checks = "check state unknown (not read)"
    elif checks_state == NO_CHECKS_YET:
        checks = "no checks created yet"
    else:
        checks = "checks read"
    tail = f" — {issue_note}" if issue_note else ""
    return f"[result] PR #{number} opened; {checks}{tail}"


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def _gh(args: List[str], timeout: int = 30):
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _gh_json(args: List[str], timeout: int = 30) -> tuple[object, str]:
    try:
        r = _gh(args, timeout=timeout)
    except FileNotFoundError:
        return (None, "gh not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        return (None, "gh timed out")
    except OSError as e:
        return (None, f"gh could not be run: {e}")
    if r.returncode != 0:
        # The same `_gh_json` as `pr_merge.py`, with the same defect and the
        # same fix (#1648): `split_lines` decides the boundary so the server
        # cannot pick the segment with a U+2028, `flat()` keeps it to one line.
        # `read_err` reaches `checks_section` and is rendered above this op's
        # own `[result] no PR created`.
        tail = _untrusted.split_lines((r.stderr or r.stdout).strip())
        return (None, _untrusted.flat(tail[-1]) if tail
                else f"gh exited {r.returncode}")
    try:
        return (json.loads(r.stdout or "null"), "")
    except json.JSONDecodeError:
        return (None, "gh returned invalid JSON")


def _current_branch() -> tuple[str, str]:
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return ("", f"git did not answer: {e}")
    if r.returncode != 0:
        return ("", (r.stderr or "").strip() or "git rev-parse failed")
    name = (r.stdout or "").strip()
    if not name or name == "HEAD":
        return ("", "detached HEAD")
    return (name, "")


def _load_payload(path: str) -> dict:
    if path.startswith("@"):
        path = path[1:]
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(
        encoding="utf-8")
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    if tomllib is None:
        raise ValueError("TOML payload requires Python 3.11+ or `pip install tomli`")
    return tomllib.loads(raw)


def _age_secs(iso: str):
    from datetime import datetime, timezone
    try:
        when = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return int((datetime.now(timezone.utc) - when).total_seconds())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:  # noqa: C901
    use_utf8_stdout()
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    path = raw_arg[1:] if raw_arg.startswith("@") else raw_arg
    if not path:
        print("ERROR: gh-pr-create needs a payload — "
              "gh-pr-create:@FILE, or gh-pr-create:@- to read it from "
              "stdin (JSON or TOML with title/body/base).")
        return 1
    if path != "-" and Path(path).is_dir():
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1

    try:
        payload = _load_payload(raw_arg)
    except FileNotFoundError:
        print(f"ERROR: payload file not found: {path}")
        return 1
    except IsADirectoryError:
        print(f"ERROR: payload path is a directory, not a file: {path}")
        return 1
    except PermissionError as e:
        print(f"ERROR: permission denied reading payload: {path} — {e}")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: failed to parse payload: {e} "
              f"(expected JSON or TOML with title/body/base)")
        return 1

    key_err = _payload_keys.check(payload, ACCEPTED_KEYS, ALIASES, "gh-pr-create")
    if key_err:
        print(key_err)
        return 1

    repo_conflict, repo_source = _repo_target.resolve_or_conflict(payload, "gh-pr-create")
    if repo_conflict:
        print(repo_conflict)
        return 1
    if not payload.get("repo"):
        auto = _rd.resolve("github_repo", "github.com")
        if auto:
            payload["repo"] = auto
            repo_source = "cwd remote / config default"

    branch, branch_err = _current_branch()
    head, head_source, err = resolve_head(payload, branch, branch_err)
    if err:
        print(err)
        return 1
    payload["head"] = head

    err = validate(payload)
    if err:
        if "base" in err and "missing" in err:
            data, _e = _gh_json(["repo", "view", "--json", "defaultBranchRef"]
                                + _repo_target.gh_args(), timeout=20)
            if isinstance(data, dict):
                name = ((data.get("defaultBranchRef") or {}) or {}).get("name")
                if name:
                    err += (f" This repository's default branch is {name!r} — "
                            f"set it explicitly if that is what you meant.")
        print(err)
        return 1

    repo = str(payload["repo"])
    base = str(payload["base"])
    title = str(payload["title"])
    body_file = payload.get("body_file")

    if body_file:
        if Path(body_file).is_dir():
            print(f"ERROR: body_file is a directory, not a file: {body_file}")
            return 1
        try:
            content = Path(body_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: body_file not found: {body_file}")
            return 1
        except PermissionError as e:
            print(f"ERROR: permission denied reading body_file: {body_file} — {e}")
            return 1
    else:
        content = str(payload.get("body") or "")

    if not payload.get(LITERAL_BS_KEY):
        hits = literal_backslash_quotes(content)
        if hits:
            print(literal_backslash_quote_refusal(hits))
            return 1

    refs = _checks.closing_issue_refs(content)
    if not refs and not payload.get(NO_CLOSE_KEY):
        print(no_closing_reference_refusal())
        return 1

    # #2100: appended AFTER the closing-reference parse above, never before --
    # the marker text carries no "Closes" substring, but the order is a real
    # constraint the issue names explicitly (a marker parsed as part of the
    # gate would be a coincidence to rely on, not a guarantee).
    content, disclosure_state = _publish_safety.apply_forge_disclosure(content)

    cmd = ["pr", "create", "--repo", repo, "--base", base, "--head", head,
           "--title", title]
    tmp_body: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                         delete=False) as f:
            f.write(content)
            tmp_body = f.name
        cmd += ["--body-file", tmp_body]
        if payload.get("draft"):
            cmd.append("--draft")
        for key, flag in (("labels", "--label"), ("assignees", "--assignee"),
                          ("reviewers", "--reviewer")):
            values = payload.get(key) or []
            if values:
                cmd += [flag, ",".join(str(v) for v in values)]
        if payload.get("milestone"):
            cmd += ["--milestone", str(payload["milestone"])]

        try:
            result = _gh(cmd, timeout=60)
        except FileNotFoundError:
            print("ERROR: gh not found — install from https://cli.github.com")
            return 1
        except subprocess.TimeoutExpired:
            print("ERROR: gh timed out creating the PR")
            return 1
    finally:
        if tmp_body and os.path.exists(tmp_body):
            os.unlink(tmp_body)

    if result.returncode != 0:
        print(f"ERROR: gh pr create failed (exit {result.returncode})")
        # One line above this op's own `[result]`, so an unflattened body does
        # not merely reach column 0 — it sorts above a real verdict this op
        # wrote itself. The writer of the text is the GitHub API (#1606).
        print(_untrusted.flat((result.stderr or result.stdout).strip()))
        print(f"[result] no PR created ({head} -> {base} in {repo})")
        return 1

    url = ""
    for line in (result.stdout or "").splitlines():
        if "/pull/" in line:
            # Flattened at the seam, not at each print. `str.splitlines()`
            # consumes every line SEPARATOR, so no line here can be forged and
            # #1652's argument holds — but it does not consume ESC, and this
            # value is written at column 0 six times: `URL:`, `PR:` and the
            # three `## Next` commands via `number`, and this op's own
            # `[result]` verdict. That last one is the line #851/#853 exist to
            # protect, and `number` inherits whatever the URL carried (#1660).
            url = _untrusted.flat(line.strip())
            break
    number = url.rstrip("/").split("/")[-1] if url else "?"

    repo_note = (f"  (repo from {repo_source})"
                 if repo_source not in ("", "payload") else "")
    print(f"# gh-pr-create — {repo}{repo_note}")
    print(f"PR:   #{number}  {_untrusted.flat(title)}")
    print(f"URL:  {url or '(not returned by gh)'}")
    print(f"Base: {base}  (from payload — never defaulted)")
    print(f"Head: {head}  (from {head_source})")
    print(f"Disclosure: {disclosure_state}")
    print()

    # ---- did anything actually start? -----------------------------------
    checks_state = CHECKS_UNKNOWN
    check_lines: List[str] = []
    # ASCII digits, the same test `run.py`'s `refuse_run_id` applies to a run id
    # (#1727). `number` is parsed out of `gh`'s own stdout, so a non-ASCII digit
    # here means `gh` printed one and the blast radius is low — but the loose
    # spelling sitting next to the strict one is what gets copied, and it was.
    if _digits.is_ascii_int(number):
        data, read_err = _gh_json(
            ["pr", "view", number, "--repo", repo, "--json",
             "statusCheckRollup,headRefOid,createdAt"], timeout=30)
        if isinstance(data, dict):
            check_lines, checks_state = checks_section(
                data.get("statusCheckRollup"),
                _age_secs(str(data.get("createdAt") or "")),
                str(data.get("headRefOid") or ""))
        else:
            check_lines, checks_state = checks_section(
                None, None, "", read_error=read_err)
    else:
        check_lines, checks_state = checks_section(
            None, None, "", read_error="gh returned no PR number to read back")

    print("## Checks")
    for line in check_lines:
        print(line)
    print()

    # ---- what will this close? -------------------------------------------
    # `refs` was already computed above, before creation -- the refusal at
    # #1838 depends on it. Not re-parsed here so the receipt and the gate
    # can never disagree about what the same body carries.
    print("## Linked issues (parsed from the body you just submitted)")
    print(f"  {_checks.linked_issue_line(refs)}")
    if refs:
        issue_note = f"links {', '.join(refs)}"
        print("  Verified again at merge time by `gh-pr-merge` — a body that "
              "reads correctly is not the same as a reference GitHub bound.")
    elif payload.get(NO_CLOSE_KEY):
        issue_note = f"no closing reference ({NO_CLOSE_KEY} acknowledged)"
        print(f"  No closing keyword in the body — {NO_CLOSE_KEY} = true in "
              "the payload acknowledged this pull request closes nothing "
              "deliberately.")
    else:
        # Unreachable in the ordinary path: the refusal above already
        # stopped a body with no reference and no NO_CLOSE_KEY from
        # reaching `gh pr create`. Left as a real branch, not an assert,
        # because `refs`/`NO_CLOSE_KEY` are read from mutable state and a
        # future edit that reorders the two checks should not silently
        # start opening these PRs unlabelled.
        issue_note = "no closing reference in the body"
        print("  No closing keyword in the body, so merging this will close "
              "nothing. Add `Closes #N` now if that is wrong — it is far "
              "cheaper than noticing after the merge.")
    print()

    print("## Next")
    print(f"  Status:  `gh-pr:{number}:status`")
    print(f"  Watch:   `gh-pr:{number}`")
    print(f"  Merge:   `gh-pr-merge:{number}` (previews the gate; "
          f"`|force` to actually merge)")
    print()
    print(result_line(number, checks_state, issue_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
