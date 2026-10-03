#!/usr/bin/env python3































































from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  

LISTED = "listed"
ABSENT = "not listed"
SKIPPED = "skipped"

MANIFEST_PATH = ".claude-plugin/marketplace.json"
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"


FINDING_MARK = "❯"

UNPINNED_NOTE = (
    "unpinned -- the entry carries no sha, so it tracks the repository default "
    "branch and every release reaches users at once"
)




BUMP_PR_LIMIT = 30

GH_TIMEOUT = 60
GIT_TIMEOUT = 20
CLAUDE_TIMEOUT = 60


class Catalogue(NamedTuple):
    key: str
    repo: str
    note: str


CATALOGUES: Tuple[Catalogue, ...] = (
    Catalogue(
        "official",
        "anthropics/claude-code",
        "the catalogue Claude Code ships with",
    ),
    Catalogue(
        "community",
        "anthropics/claude-plugins-community",
        "community submissions, bumped by an automated workflow",
    ),
)


class CatalogueReport(NamedTuple):
    key: str
    repo: str
    state: str
    reason: Optional[str]
    plugin_count: Optional[int]
    pinned_sha: Optional[str]
    rows: List[Tuple[str, str]]






def parse_marketplace(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:







    if raw is None or not raw.strip():
        return None, "empty response body -- nothing was returned to parse"
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return None, "response is not JSON (%s)" % (exc.__class__.__name__,)
    if not isinstance(doc, dict):
        return None, "response is JSON but not an object (%s)" % type(doc).__name__
    if "plugins" not in doc and "content" in doc and "encoding" in doc:
        return None, (
            "the GitHub contents API returned its JSON envelope instead of the "
            "file: encoding=%r, content is %d bytes, size field says %s. The API "
            "declines files over ~1 MB and reports it only in these fields. Ask "
            "for it with the raw media type."
            % (doc.get("encoding"), len(doc.get("content") or ""), doc.get("size"))
        )
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        return None, (
            "no 'plugins' array in the document -- this does not look like a "
            "marketplace manifest"
        )
    return doc, None


def find_entry(doc: Optional[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:



    if not doc:
        return None
    for entry in doc.get("plugins") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def pin_sha(entry: Optional[Dict[str, Any]]) -> Optional[str]:






    if not entry:
        return None
    source = entry.get("source")
    if not isinstance(source, dict):
        return None
    sha = source.get("sha")
    return sha if isinstance(sha, str) and sha.strip() else None


def bump_query(name: str) -> str:







    return "bump(%s) in:title" % name


def distance_skip_reason(asked: str, local: Optional[str]) -> Optional[str]:







    if local is None:
        return (
            "no %s here, so there is no local plugin to measure the pin against"
            % PLUGIN_MANIFEST
        )
    if asked != local:
        return (
            "'%s' is not this repository's plugin ('%s') -- its pin points into "
            "a repository this clone does not hold" % (asked, local)
        )
    return None


def local_plugin(root: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:

    path = Path(root) / PLUGIN_MANIFEST
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, None, "no %s in %s" % (PLUGIN_MANIFEST, root)
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, None, "%s is not valid JSON" % PLUGIN_MANIFEST
    if not isinstance(doc, dict) or not doc.get("name"):
        return None, None, "%s declares no plugin name" % PLUGIN_MANIFEST
    version = doc.get("version")
    return doc["name"], version if isinstance(version, str) else None, None








def _run(argv: Sequence[str], timeout: int) -> Tuple[Optional[int], str, str]:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return None, "", "%s is not installed here" % argv[0]
    except subprocess.TimeoutExpired:
        return None, "", "%s timed out after %ds" % (argv[0], timeout)
    except OSError as exc:
        return None, "", "%s could not be run (%s)" % (argv[0], exc.__class__.__name__)
    return proc.returncode, proc.stdout, proc.stderr


def fetch_catalogue(repo: str, run: Callable[..., Any] = _run) -> Tuple[Optional[str], Optional[str]]:







    rc, out, err = run(
        [
            "gh", "api",
            "repos/%s/contents/%s" % (repo, MANIFEST_PATH),
            "-H", "Accept: application/vnd.github.raw",
        ],
        GH_TIMEOUT,
    )
    if rc is None:
        return None, err
    if rc != 0:
        detail = (err or out or "").strip().splitlines()
        return None, "gh api failed (exit %d): %s" % (rc, detail[0] if detail else "no output")
    return out, None


def fetch_bump_prs(
    repo: str, name: str, run: Callable[..., Any] = _run
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    rc, out, err = run(
        [
            "gh", "pr", "list",
            "--repo", repo,
            "--state", "all",
            "--search", bump_query(name),
            "--limit", str(BUMP_PR_LIMIT),
            "--json", "number,title,state,mergedAt,createdAt",
        ],
        GH_TIMEOUT,
    )
    if rc is None:
        return None, err
    if rc != 0:
        return None, "gh pr list failed (exit %d)" % rc
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return None, "gh pr list returned output that is not JSON"
    return rows if isinstance(rows, list) else [], None


def git_version_at(root: Path, sha: str, run: Callable[..., Any] = _run) -> Tuple[Optional[str], Optional[str]]:






    rc, kind, _ = run(["git", "-C", str(root), "cat-file", "-t", sha], GIT_TIMEOUT)
    if rc is None:
        return None, "git is not available here"
    if rc != 0 or kind.strip() != "commit":
        return None, "commit %s is not in this clone (fetch it to resolve)" % sha[:8]

    rc, out, _ = run(
        ["git", "-C", str(root), "show", "%s:%s" % (sha, PLUGIN_MANIFEST)], GIT_TIMEOUT
    )
    if rc is None:
        return None, "git is not available here"
    if rc != 0:
        return None, (
            "commit %s is in this clone but carries no %s -- the catalogue is "
            "pinned to a commit that predates the manifest, or to another tree"
            % (sha[:8], PLUGIN_MANIFEST)
        )
    try:
        doc = json.loads(out)
    except ValueError:
        return None, "%s at %s is not valid JSON" % (PLUGIN_MANIFEST, sha[:8])
    version = doc.get("version") if isinstance(doc, dict) else None
    return (version if isinstance(version, str) else None), None


def git_line(root: Path, args: Sequence[str], run: Callable[..., Any] = _run) -> Optional[str]:
    rc, out, _ = run(["git", "-C", str(root)] + list(args), GIT_TIMEOUT)
    if rc != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0].strip()


def git_tag_count(root: Path, rev: str, run: Callable[..., Any] = _run) -> Optional[int]:
    rc, out, _ = run(["git", "-C", str(root), "tag", "--merged", rev], GIT_TIMEOUT)
    if rc != 0:
        return None
    return len([ln for ln in out.splitlines() if ln.strip()])







_TRANSLIT = {
    "→": "->", "←": "<-", "—": "--", "–": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ", "✓": "ok", "✗": "x",
}


def _ascii(text: str) -> str:










    for src, dst in _TRANSLIT.items():
        text = text.replace(src, dst)
    return text.encode("ascii", "replace").decode("ascii")


def validate_gate(root: Path, run: Callable[..., Any] = _run) -> Tuple[str, str]:






    rc, out, err = run(["claude", "plugin", "validate", str(root)], CLAUDE_TIMEOUT)
    if rc is None:
        return SKIPPED, err
    body = (out or "") + (err or "")
    findings = []
    for ln in body.splitlines():
        text = ln.strip()
        if text.startswith(FINDING_MARK):
            text = text[len(FINDING_MARK):].strip()
            if text:
                findings.append(text)
    summary = _ascii("; ".join(findings))[:400]
    if rc == 0:
        return "ok", summary or "no findings"
    return "refused", summary or "exit %d, no findings parsed" % rc






def exit_status(reports: Sequence[CatalogueReport]) -> int:

    return 1 if any(r.state == SKIPPED for r in reports) else 0


def render(
    plugin: str,
    version: Optional[str],
    head: Optional[str],
    reports: Sequence[CatalogueReport],
    gate: Optional[Tuple[str, str]],
) -> str:
    lines: List[str] = []
    ident = plugin + (" " + version if version else "")
    lines.append("plugin-marketplace -- %s, local HEAD %s" % (ident, head or "unknown"))
    lines.append("")

    for rep in reports:
        lines.append("%-10s %s" % (rep.key, rep.repo))
        if rep.state == SKIPPED:
            lines.append("  %-9s %s -- %s" % ("state", SKIPPED, rep.reason or "no reason given"))
            lines.append("  %-9s %s" % ("", "not read; this says nothing about the plugin either way"))
        elif rep.state == ABSENT:
            pop = "%d plugins" % rep.plugin_count if rep.plugin_count is not None else "the catalogue"
            lines.append("  %-9s %s -- read %s, none named '%s'" % ("state", ABSENT, pop, plugin))
            lines.append("  %-9s %s" % ("", "this needs a submission, not a bump"))
        else:
            pop = " among %d plugins" % rep.plugin_count if rep.plugin_count is not None else ""
            lines.append("  %-9s %s%s" % ("state", LISTED, pop))
        for label, text in rep.rows:
            lines.append("  %-9s %s" % (label, text))
        lines.append("")

    if gate is not None:
        status, detail = gate
        lines.append("gate       claude plugin validate -- %s%s" % (status, (": " + detail) if detail else ""))
        if status != SKIPPED:
            lines.append(
                "           read the working tree; the catalogue's automation "
                "validates the sha it is about to pin"
            )
        lines.append("")

    counts = [
        (sum(1 for r in reports if r.state == LISTED), LISTED),
        (sum(1 for r in reports if r.state == ABSENT), ABSENT),
        (sum(1 for r in reports if r.state == SKIPPED), SKIPPED),
    ]



    parts = ["%d %s" % (n, label) for n, label in counts if n]
    lines.append(
        "[result] %d catalogue(s) -- %s" % (len(reports), ", ".join(parts) or "nothing to report")
    )
    return "\n".join(lines)






def collect(
    root: Path,
    plugin: str,
    local_name: Optional[str],
    local_version: Optional[str],
    run: Callable[..., Any] = _run,
) -> List[CatalogueReport]:
    reports: List[CatalogueReport] = []
    skip_distance = distance_skip_reason(plugin, local_name)

    for cat in CATALOGUES:
        raw, reason = fetch_catalogue(cat.repo, run=run)
        if raw is None:
            reports.append(CatalogueReport(cat.key, cat.repo, SKIPPED, reason, None, None, []))
            continue
        doc, reason = parse_marketplace(raw)
        if doc is None:
            reports.append(CatalogueReport(cat.key, cat.repo, SKIPPED, reason, None, None, []))
            continue

        count = len(doc.get("plugins") or [])
        entry = find_entry(doc, plugin)
        if entry is None:
            reports.append(CatalogueReport(cat.key, cat.repo, ABSENT, None, count, None, []))
            continue

        sha = pin_sha(entry)
        rows: List[Tuple[str, str]] = []
        if sha is None:
            rows.append(("pinned", UNPINNED_NOTE))
        else:
            rows.extend(_pin_rows(root, sha, local_version, skip_distance, run))

        rows.extend(_bump_rows(cat.repo, plugin, run))
        reports.append(CatalogueReport(cat.key, cat.repo, LISTED, None, count, sha, rows))
    return reports


def _pin_rows(
    root: Path,
    sha: str,
    local_version: Optional[str],
    skip_distance: Optional[str],
    run: Callable[..., Any],
) -> List[Tuple[str, str]]:
    if skip_distance is not None:
        return [
            ("pinned", sha[:8]),
            ("distance", "%s -- %s" % (SKIPPED, skip_distance)),
        ]

    at_version, why = git_version_at(root, sha, run=run)
    when = git_line(root, ["log", "-1", "--format=%cs", sha], run=run)
    stamp = "  ".join(x for x in (sha[:8], ("v" + at_version) if at_version else None, when) if x)
    rows = [("pinned", stamp or sha[:8])]
    if at_version is None and why:
        rows.append(("", "manifest version at that sha: %s -- %s" % (SKIPPED, why)))
        return rows

    ahead = git_line(root, ["rev-list", "--count", "%s..HEAD" % sha], run=run)
    tags_head = git_tag_count(root, "HEAD", run=run)
    tags_pin = git_tag_count(root, sha, run=run)
    bits = []
    if ahead is not None:
        bits.append("%s commits" % ahead)
    if tags_head is not None and tags_pin is not None:
        bits.append("%d releases" % (tags_head - tags_pin))
    if at_version and local_version:
        bits.append("%s -> %s" % (at_version, local_version))
    if bits:
        rows.append(("distance", ", ".join(bits)))
    else:
        rows.append(("distance", "%s -- git could not measure it here" % SKIPPED))
    return rows


def keep_own_bumps(prs: Sequence[Dict[str, Any]], plugin: str) -> List[Dict[str, Any]]:




















    prefix = "bump(%s):" % plugin
    mine = [p for p in prs if isinstance(p, dict) and (p.get("title") or "").strip().startswith(prefix)]
    return sorted(mine, key=lambda p: p.get("createdAt") or "", reverse=True)


def _bump_rows(repo: str, plugin: str, run: Callable[..., Any]) -> List[Tuple[str, str]]:
    prs, reason = fetch_bump_prs(repo, plugin, run=run)
    query = bump_query(plugin)
    if prs is None:
        searched = ("", "searched: %s (limit %d)" % (query, BUMP_PR_LIMIT))
        return [("bump PRs", "%s -- %s" % (SKIPPED, reason)), searched]

    returned = len(prs)



    capped = returned == BUMP_PR_LIMIT
    if capped:
        searched = ("", "searched: %s (limit %d)" % (query, BUMP_PR_LIMIT))
    else:
        searched = ("", "searched: %s" % query)

    prs = keep_own_bumps(prs, plugin)
    rows_extra: List[Tuple[str, str]] = []
    if returned != len(prs):
        rows_extra.append((
            "",
            "kept %d of %d returned: GitHub search tokenizes, so 'bump(%s)' also "
            "matches bump(%s-other) and any title holding both words. Only titles "
            "starting '%s:' are this plugin's."
            % (len(prs), returned, plugin, plugin, "bump(%s)" % plugin),
        ))
    if not prs:
        if capped:




            none_row = (
                "bump PRs",
                "none in the first %d, and the search filled its limit, so "
                "more may exist" % BUMP_PR_LIMIT,
            )
        else:
            none_row = ("bump PRs", "none found")
        return [none_row, searched] + rows_extra
    latest = prs[0]
    when = (latest.get("mergedAt") or latest.get("createdAt") or "")[:10]





    title = _ascii(_untrusted.flat(latest.get("title") or "")).strip()
    row = "%d found, latest #%s %s %s -- %s" % (
        len(prs),
        latest.get("number"),
        latest.get("state"),
        when,
        title,
    )
    rows = [("bump PRs", row), searched] + rows_extra
    if any(p.get("state") == "OPEN" for p in prs):
        rows.append(("", "an OPEN bump PR is waiting on review in that catalogue"))
    return rows


USAGE = (
    "ERROR: plugin-marketplace answers 'did this release reach anyone?' for a "
    "Claude Code plugin, and it needs to know which plugin.\n"
    "  There is no %s here, so there is nothing to read a name and a version from.\n"
    "  Run it from a plugin repository, or name one: plugin-marketplace:NAME\n"
    "  (with :NAME the catalogue rows still render; the distance to local HEAD is\n"
    "  skipped with its reason, because that pin points into another repository.)\n"
)


def repo_root(start: Path, run: Callable[..., Any] = _run) -> Path:
    rc, out, _ = run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], GIT_TIMEOUT)
    if rc == 0 and out.strip():
        return Path(out.strip())
    return Path(start)


def main(
    argv: Sequence[str],
    root: Optional[Path] = None,
    run: Callable[..., Any] = _run,
) -> int:
    here = Path(root) if root is not None else repo_root(Path.cwd(), run=run)
    local_name, local_version, _ = local_plugin(here)

    asked = argv[0].strip() if argv and argv[0].strip() else None
    plugin = asked or local_name
    if plugin is None:
        sys.stderr.write(USAGE % PLUGIN_MANIFEST)
        return 2

    reports = collect(here, plugin, local_name, local_version, run=run)
    gate = None
    if plugin == local_name:
        gate = validate_gate(here, run=run)

    version = local_version if plugin == local_name else None
    head = git_line(here, ["rev-parse", "--short", "HEAD"], run=run)
    if any(label == "bump PRs" for rep in reports for label, _ in rep.rows):




        sys.stdout.write(
            _untrusted.flat_note("bump PR titles", source="the catalogue's tracker") + "\n"
        )
    sys.stdout.write(render(plugin, version, head, reports, gate) + "\n")
    return exit_status(reports)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
