#!/usr/bin/env python3
"""claims:PATH — does this document's references still hold?

A doc that asserts something about the repo can go stale, and a stale line in
a doc that is *loaded* rather than *read* produces the behaviour it describes:
this repo's own maintainer skill said no op rendered a commit's
run list months after `gh-branch:COMMIT_SHA` shipped, and the maintainer
hand-rolled jq in obedience to it. The same file recorded the same failure with
`repo:` before that. (That skill is gone since #1729 — the `oss` plugin's
`/oss:manager` replaced it — and the quote is in `CHANGELOG.md` under #1220.)

This checks **references, not reasoning**, and the boundary is not an
aesthetic choice — it was measured. A probe that flagged issue citations by
issue-state plus an absence-marker word list scored 15 flagged, 2 real (13%),
because past-tense narration of a fixed bug is lexically identical to a
present-tense claim that a hole exists. Three narrower lexical anchors were
measured against this repo's whole doc corpus while building this op and
scored 14%, 11% and 20%. None beat the number that was already rejected. So
there is no lexical lens here. A sentence is never a finding.

Three lenses, all of them mechanical:

``op``       a backticked ``name:...`` token, against the live op registry.
             Only ``key=`` named flags are checked; a bare segment sits in a
             placeholder slot and is a value, not a flag (measured: a
             membership test flagged ``gh-pr:master:status`` and two others,
             all wrong). A head that resolves to no op is *unchecked*, never
             contradicted — 19 such tokens in this repo's docs were skill ids,
             label filters and other tools' namespaces, and none was a stale
             op name.

``path``     a backticked path, optional ``:LINE``, optional section name.
             A missing file under a directory that exists is a claim about
             this repo and it is wrong. A missing file under a directory that
             does not exist is probably the sibling repo's, and answering for
             it would be this tool inventing a verdict.

``issue``    an issue cited under a heading that *declares* it an open defect
             (``# Open defects``, ``# Open defect #1202 — ...``). This is the
             narrow third rule and it carries a verdict where prose cannot,
             because it reads the document's own structural annotation rather
             than guessing at its grammar. Measured on ``.claude/jit-context/``
             — injected automatically at tool-call time, so it lands with more
             authority than a doc someone chose to open — 5 cited open
             defects, 5 closed.

Three states throughout: ``holds``, ``contradicted``, ``couldn't check``. The
third never collapses into either neighbour, and a doc carrying unchecked
references never renders as a clean doc (docs/validators.md, "Declining
instead of guessing").
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _repo_target  # noqa: E402  (the repo this call is about, when not cwd's)

HOLDS = "holds"
CONTRADICTED = "contradicted"
UNCHECKED = "couldn't check"

_ORDER = {CONTRADICTED: 0, UNCHECKED: 1, HOLDS: 2}

_FOOTER = (
    "References only: op names and their named flags, paths, line numbers, "
    "section headings, and issues cited under an \"Open defects\" heading. "
    "Prose and reasoning are not checked — a sentence asserting a gap is not "
    "a reference, and every lexical rule tried for it scored under 21% "
    "precision on this repo (#1220)."
)

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
# `[ \t]*\Z`, not `\s*$` and not `\s*\Z`. `$` matches before a final newline,
# so the old pattern accepted a heading with one glued on (#1188) — but `\Z`
# alone does not fix it, because `\s*` swallows the newline either way and the
# match still succeeds with the same title. Measured. The trailing run is
# narrowed instead, to the only whitespace a markdown heading can actually
# carry: the newline is this op's own record delimiter, never heading text.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)[ \t]*\Z")
# Anchored, because a heading that *mentions* open defects is not a heading
# that declares a list of them. This op's own docs/presets/claims.md carries
# "### `issue` — only under a heading that declares an open defect", and an
# unanchored match turned that whole section into a defect list and reported a
# quoted example issue number as a live stale citation.
_OPEN_DEFECTS = re.compile(r"^open\s+defects?\b", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")

_EXTS = ("py|md|json|toml|yml|yaml|sh|bash|cfg|ini|txt|tsv|xml|html|js|ts|"
         "jsx|tsx|rs|php|rb|go|sql|css|lock|env|service")
# Same reasoning as `_OP_TOK`, and the same anchor. #1188's guard could not see
# this one until #1241 taught it to read a `+` splice: a whole-value test on
# author-controlled bytes does not stop being one because the check cannot read
# it, and for a while this sat three lines from a twin the guard did flag.
_PATH_TOK = re.compile(
    r"^([A-Za-z0-9_.][A-Za-z0-9_./+-]*\.(?:" + _EXTS + r"))(?::(\d+))?\Z")

# `\Z`, and deliberately not an `anchored-ok` waiver -- writing that sentence
# used to trip the guard itself, which demanded a reason for an exit nobody had
# taken; #1241 made the token count only when it opens a comment. This decides
# whether a backticked token is read as an op reference, and its input is the
# contents of a code span — bytes the document's author wrote, not a line this
# op sliced out of a larger text. `_CODE_SPAN` already excludes U+000A and
# `_op_findings` rejects any token holding whitespace, so nothing exploited the
# old `$`; the anchor is what makes that true of the pattern rather than of its
# two callers.
_OP_TOK = re.compile(r"^([a-z][a-z0-9_-]*):(\S.*)\Z")

_CITATION = re.compile(
    r"(?:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<num>\d{1,6})\b")
# Deliberately NOT a general `owner/name` scan of the prose. Measured on
# .claude/jit-context/paths/00-manual/presets-github.md: a slash-scan read
# `dependabot/outside-contributor` and `presets/github` as other repositories
# and demoted two real contradictions to "couldn't check". A third state that
# eats findings is the same defect as a third state that never fires. Only two
# signals are exact enough: the attached `owner/name#N` form, and a sibling
# project named on the line — sibling meaning it shares this repo's own family
# prefix (`claude-supertool` -> `claude-`), which is derived, never hardcoded.

_QUOTED_REF = re.compile(
    r'^[\s,]*(?:§|section)?\s*["“‘]([^"”’]{2,200})["”’]')

# A `NNN`-shaped path component is a naming convention being described, not a
# file being cited. Only consulted for a path that does not exist, so real
# shouty filenames (README.md, SCHEMA.md) are resolved before this ever runs.
_PLACEHOLDER_VOCAB = frozenset((
    "N", "NN", "NNN", "NNNN", "PATH", "FILE", "NAME", "OWNER", "REPO", "ID",
    "UUID", "SHA", "BRANCH", "TAG", "SECTION", "VERSION", "ISSUE", "NUMBER"))

# `ok:true`, `code:"adapter"`, `count:0` are field notation, not op calls.
_JSON_SCALAR = re.compile(r'^(?:true|false|null|-?\d|["“\[{])', re.IGNORECASE)

# `presets/mytools/status.py`, not `scripts/status.py`. The second path is the
# shape being warned against and was never meant to exist; reporting it as a
# broken reference is the tool manufacturing a defect out of a good sentence.
# The trailing character class absorbs markdown emphasis. contributing.md
# writes it plain; docs/presets/claims.md writes it bolded, and without this
# the op reported its own documentation's counter-example as a broken path.
# The trailing run is spaces and tabs, not `\s`, for the reason `_HEADING`
# gives — and it matters more here, because this pattern *suppresses* a
# finding. A run that swallows a newline is a way to make a path go unreported,
# which is the worse direction to be lax in.
_COUNTEREXAMPLE = re.compile(
    r"(?:\bnot|\bnever|\brather than|\binstead of)[*_` \t]*\Z", re.IGNORECASE)


def _is_template(rel: str) -> Optional[str]:
    for component in re.split(r"[/._-]", rel):
        if component.isupper() and (
                component in _PLACEHOLDER_VOCAB
                or (len(component) > 1 and len(set(component)) == 1)):
            return component
        if "<" in component or "{" in component or "*" in component:
            return component
    return None


class Finding(NamedTuple):
    line: int
    lens: str
    state: str
    token: str
    note: str


def _split_lines(text: str) -> List[str]:
    """Split on U+000A only, exactly as `grep -n` and `wc -l` do.

    `str.splitlines()` also breaks on U+2028, U+2029, U+000B, U+000C and
    U+0085. A document carrying any of them would have every line number after
    it reported one too high — and reporting line numbers is most of what this
    op does. #1210 is the same defect in git-diff's two readers.

    A trailing U+000D is dropped per line, so a CRLF file reads the same and
    this stays in step with `_count_lines`, which counts U+000A bytes.
    """
    lines = [line[:-1] if line.endswith("\r") else line
             for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _read_text(path: Path) -> str:
    """Read without universal-newline translation.

    `Path.read_text` rewrites a lone U+000D as U+000A, which would put the
    text side of a line count out of step with `_count_lines`, which counts
    bytes on disk.
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# fences
# --------------------------------------------------------------------------

def _live_lines(lines: Sequence[str]) -> List[bool]:
    """Which lines are prose. An unclosed fence swallows the rest of the file.

    A fence is where examples, sample output and other projects' code live.
    Scanning one manufactures findings about text that was never a claim.
    """
    live = [True] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            inside = not inside
            live[i] = False
            continue
        if inside:
            live[i] = False
    return live


# --------------------------------------------------------------------------
# lens: issue citations under a declared open-defects heading
# --------------------------------------------------------------------------

def _open_defect_lines(lines: Sequence[str], live: Sequence[bool]) -> List[bool]:
    flagged = [False] * len(lines)
    depth: Optional[int] = None
    for i, line in enumerate(lines):
        if not live[i]:
            continue
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            if _OPEN_DEFECTS.search(m.group(2)):
                depth = level
                flagged[i] = True
                continue
            if depth is not None and level <= depth:
                depth = None
                continue
        if depth is not None:
            flagged[i] = True
    return flagged


def _looks_like_path(slug: str) -> bool:
    tail = slug.rsplit("/", 1)[-1]
    return "." in tail or slug.count("/") > 1


def _family_prefix(this_repo: Optional[str]) -> Optional[str]:
    """`Digital-Process-Tools/claude-supertool` -> `claude-`.

    With no repo known there is no answer to "another project than which?",
    so sibling detection is off rather than guessed. The attached
    `owner/name#N` form still works; it needs no context.
    """
    name = (this_repo or "").split("/")[-1]
    return name.split("-", 1)[0].lower() + "-" if "-" in name else None


def _resolve_repo(this_repo) -> Optional[str]:
    """`this_repo` may be a string or a thunk that costs a `gh` call.

    It is resolved here, inside the issue lens, and nowhere else — so a
    document with no open-defects heading makes no network call at all, which
    is what docs/presets/claims.md and the index row both promise. Resolving
    it eagerly in main() broke that promise on every invocation.
    """
    return this_repo() if callable(this_repo) else this_repo


def _foreign_repo_mentioned(line: str, this_repo) -> Optional[str]:
    prefix = _family_prefix(_resolve_repo(this_repo))
    if not prefix:
        return None
    own = (_resolve_repo(this_repo) or "").split("/")[-1].lower()
    pattern = r"\b" + re.escape(prefix) + r"[a-z0-9][a-z0-9-]*\b"
    for m in re.finditer(pattern, line, re.IGNORECASE):
        if m.group(0).lower() != own:
            return m.group(0)
    return None


def _issue_findings(idx: int, line: str, this_repo,
                    issue_state: Callable[[int], Tuple[Optional[str], str]],
                    ) -> List[Finding]:
    out: List[Finding] = []
    foreign = _foreign_repo_mentioned(line, this_repo)
    for m in _CITATION.finditer(line):
        token = "#" + m.group("num")
        slug = m.group("slug")
        if slug and not _looks_like_path(slug):
            out.append(Finding(idx + 1, "issue", UNCHECKED, token,
                               "cites another repository's tracker (%s)" % slug))
            continue
        if slug:
            continue
        if foreign:
            out.append(Finding(idx + 1, "issue", UNCHECKED, token,
                               "the line names another repository (%s), so the "
                               "number may not be this tracker's" % foreign))
            continue
        state, reason = issue_state(int(m.group("num")))
        if state is None:
            out.append(Finding(idx + 1, "issue", UNCHECKED, token,
                               "tracker not readable: %s" % (reason or "unknown")))
        elif state.upper() == "OPEN":
            out.append(Finding(idx + 1, "issue", HOLDS, token,
                               "open, as the heading claims"))
        else:
            out.append(Finding(idx + 1, "issue", CONTRADICTED, token,
                               "listed under an open-defects heading, but the "
                               "tracker says %s" % state.upper()))
    return out


# --------------------------------------------------------------------------
# lens: paths, line numbers, headings
# --------------------------------------------------------------------------

class _Tree:
    """Basename index, built once and only if a bare basename shows up."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: Optional[Dict[str, List[Path]]] = None

    def index(self) -> Dict[str, List[Path]]:
        if self._index is None:
            found: Dict[str, List[Path]] = {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames
                               if d not in (".git", "node_modules", "__pycache__")]
                for name in filenames:
                    found.setdefault(name, []).append(Path(dirpath) / name)
            self._index = found
        return self._index


def _count_lines(path: Path) -> Optional[int]:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _line_verdict(idx: int, token: str, target: Path,
                  lineno: Optional[str]) -> Finding:
    if not lineno:
        return Finding(idx + 1, "path", HOLDS, token, "exists")
    total = _count_lines(target)
    if total is None:
        return Finding(idx + 1, "path", UNCHECKED, token, "file is not readable")
    if int(lineno) > total:
        return Finding(idx + 1, "path", CONTRADICTED, token,
                       "file has %d lines" % total)
    return Finding(idx + 1, "path", HOLDS, token, "line exists")


def _path_findings(idx: int, token: str, rel: str, lineno: Optional[str],
                   root: Path, tree: _Tree) -> Tuple[List[Finding], Optional[Path]]:
    # A Windows drive-letter prefix is absolute too. A backslash path never
    # reaches here — _PATH_TOK's character class excludes it — so the drive
    # letter is the only POSIX-blind case the guard was missing.
    if (rel.startswith("/") or ".." in rel.split("/")
            or re.match(r"^[A-Za-z]:", rel)):
        return ([Finding(idx + 1, "path", UNCHECKED, token,
                         "path leaves the repository root")], None)
    if "/" in rel:
        target = root.joinpath(*rel.split("/"))
        if target.is_file():
            return ([_line_verdict(idx, token, target, lineno)], target)
        head = rel.rsplit("/", 1)[0]
        placeholder = _is_template(rel)
        if placeholder:
            return ([Finding(idx + 1, "path", UNCHECKED, token,
                             "names no file — %s reads as a placeholder in a "
                             "naming convention" % placeholder)], None)
        if root.joinpath(*head.split("/")).is_dir():
            return ([Finding(idx + 1, "path", CONTRADICTED, token,
                             "no such file, though %s exists" % head)], None)
        return ([Finding(idx + 1, "path", UNCHECKED, token,
                         "not in this repository, and neither is its "
                         "directory, so this may be another project's path")],
                None)
    # A basename that also sits at the repository root is that file. Without
    # this, `README.md` in a doc resolved to 16 candidates and went unchecked,
    # which is the third state used as a shrug rather than as a verdict.
    at_root = root / rel
    if at_root.is_file():
        return ([_line_verdict(idx, token, at_root, lineno)], at_root)
    hits = tree.index().get(rel, [])
    if len(hits) == 1:
        return ([_line_verdict(idx, token, hits[0], lineno)], hits[0])
    if not hits:
        return ([Finding(idx + 1, "path", UNCHECKED, token,
                         "no file of that name in this repository; a bare "
                         "basename can belong to any project the doc "
                         "discusses")], None)
    return ([Finding(idx + 1, "path", UNCHECKED, token,
                     "%d files share this name, so the reference is "
                     "ambiguous" % len(hits))], None)


def _normalise_heading(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _quote_finding(idx: int, target: Path, lineno: int, wanted: str) -> Finding:
    """`docs/validators.md:650, "No verdict never rolls back an edit"`.

    A line number alone only proves the file is long enough — and 650 was, so
    the reference read as holding while pointing at an unrelated table row.
    With a quotation beside it the reference becomes checkable *by content*
    without reading meaning: either that text is on that line or it is not,
    and if it moved, the new line number is the finding.
    """
    try:
        body = _split_lines(_read_text(target))
    except OSError:
        return Finding(idx + 1, "quote", UNCHECKED, wanted,
                       "target file is not readable")
    want = _normalise_heading(wanted)
    if 1 <= lineno <= len(body) and want in _normalise_heading(body[lineno - 1]):
        return Finding(idx + 1, "quote", HOLDS, wanted, "quoted text is on that line")
    elsewhere = [n for n, line in enumerate(body, 1)
                 if want in _normalise_heading(line)]
    if elsewhere:
        return Finding(idx + 1, "quote", CONTRADICTED, wanted,
                       "quoted text is at %s:%s, not :%d"
                       % (target.name, ",".join(str(n) for n in elsewhere[:3]),
                          lineno))
    return Finding(idx + 1, "quote", CONTRADICTED, wanted,
                   "no line of %s contains that text" % target.name)


def _heading_finding(idx: int, target: Path, wanted: str) -> Finding:
    try:
        body = _split_lines(_read_text(target))
    except OSError:
        return Finding(idx + 1, "heading", UNCHECKED, wanted,
                       "target file is not readable")
    want = _normalise_heading(wanted)
    for line in body:
        m = _HEADING.match(line)
        if m and _normalise_heading(m.group(2)) == want:
            return Finding(idx + 1, "heading", HOLDS, wanted, "heading exists")
    return Finding(idx + 1, "heading", CONTRADICTED, wanted,
                   "no such heading in %s" % target.name)


# --------------------------------------------------------------------------
# lens: op names and named flags
# --------------------------------------------------------------------------

def _op_findings(idx: int, token: str, registry: Dict[str, str]) -> List[Finding]:
    if any(ch.isspace() for ch in token):
        return []
    m = _OP_TOK.match(token)
    if not m:
        return []
    head, tail = m.group(1), m.group(2)
    if tail.startswith("//") or _JSON_SCALAR.match(tail):
        return []
    if head not in registry:
        return [Finding(idx + 1, "op", UNCHECKED, head,
                        "resolves to no op in this project's registry; skill "
                        "ids, label filters and other tools' namespaces look "
                        "the same, so this is not called a stale op name")]
    syntax = registry[head]
    out: List[Finding] = []
    for segment in tail.split(":"):
        for part in segment.split(","):
            if "=" not in part:
                continue
            key = part.split("=", 1)[0].strip()
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
                continue
            if re.search(r"\b" + re.escape(key) + r"\s*=", syntax):
                out.append(Finding(idx + 1, "op", HOLDS, "%s:%s=" % (head, key),
                                   "flag is in the signature"))
            else:
                out.append(Finding(idx + 1, "op", CONTRADICTED,
                                   "%s:%s=" % (head, key),
                                   "flag %s= is not in the signature: %s"
                                   % (key, syntax)))
    if not out:
        out.append(Finding(idx + 1, "op", HOLDS, head, "op exists"))
    return out


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def scan(text: str, *, root, registry: Dict[str, str],
         issue_state: Callable[[int], Tuple[Optional[str], str]],
         this_repo=None) -> List[Finding]:
    root = Path(root)
    tree = _Tree(root)
    lines = _split_lines(text)
    live = _live_lines(lines)
    defects = _open_defect_lines(lines, live)
    out: List[Finding] = []
    for idx, line in enumerate(lines):
        if not live[idx]:
            continue
        for m in _CODE_SPAN.finditer(line):
            token = m.group(1).strip()
            if _COUNTEREXAMPLE.search(line[:m.start()]):
                continue
            pm = _PATH_TOK.match(token)
            if pm:
                found, target = _path_findings(
                    idx, token, pm.group(1), pm.group(2), root, tree)
                out.extend(found)
                if target is not None:
                    qm = _QUOTED_REF.match(line[m.end():])
                    if qm and pm.group(2):
                        out.append(_quote_finding(idx, target,
                                                  int(pm.group(2)), qm.group(1)))
                    elif qm and target.suffix == ".md":
                        out.append(_heading_finding(idx, target, qm.group(1)))
                continue
            out.extend(_op_findings(idx, token, registry))
        if defects[idx]:
            out.extend(_issue_findings(idx, line, this_repo, issue_state))
    out.sort(key=lambda f: (f.line, _ORDER[f.state], f.lens, f.token))
    return out


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(path: str, findings: Sequence[Finding]) -> str:
    counts = {HOLDS: 0, CONTRADICTED: 0, UNCHECKED: 0}
    for f in findings:
        counts[f.state] += 1
    lines = ["%s: holds %d | contradicted %d | couldn't check %d"
             % (path, counts[HOLDS], counts[CONTRADICTED], counts[UNCHECKED])]
    if counts[UNCHECKED] and not counts[CONTRADICTED]:
        lines.append("NOT A CLEAN DOC: %d reference(s) could not be checked — "
                     "an unread reference is not a verified one."
                     % counts[UNCHECKED])
    for state, title in ((CONTRADICTED, "CONTRADICTED"),
                         (UNCHECKED, "COULDN'T CHECK")):
        rows = [f for f in findings if f.state == state]
        if not rows:
            continue
        lines.append("")
        lines.append(title)
        for f in rows:
            lines.append("  L%-5d %-8s %s — %s" % (f.line, f.lens, f.token, f.note))
    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_registry(root: Path) -> Dict[str, str]:
    """Op name -> syntax, from .supertool.json plus the presets beside it.

    An empty registry is a state, not a clean answer: with no config the op
    lens cannot run, and main() says that out loud instead of letting the
    absence read as "every op reference resolved".
    """
    registry: Dict[str, str] = {}
    sources = [root / ".supertool.json"] + sorted((root / "presets").glob("*.json"))
    for source in sources:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for section in ("builtin-ops", "ops", "aliases"):
            entries = data.get(section)
            if not isinstance(entries, dict):
                continue
            for name, info in entries.items():
                if not isinstance(info, dict):
                    continue
                syntax = str(info.get("syntax", name))
                # Key by the *syntax head*, not the JSON key, and merge. An op
                # can hold several entries under different keys — `read` and
                # `read-grep` both document `read:`, and keying by JSON key
                # hid `read:PATH:::grep=PATTERN` so `read:grep=` was reported
                # as a flag that does not exist. It is the documented form.
                head = syntax.split(":", 1)[0].split("[", 1)[0].strip() or name
                registry[head] = (registry[head] + " " + syntax
                                  if head in registry else syntax)
    return registry


def _run(argv: List[str], timeout: int = 20):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _issue_state_reader(repo):
    cache: Dict[int, Tuple[Optional[str], str]] = {}

    def read(number: int) -> Tuple[Optional[str], str]:
        if number in cache:
            return cache[number]
        slug = _resolve_repo(repo)
        argv = ["gh", "issue", "view", str(number), "--json", "state"]
        if slug:
            argv += ["--repo", slug]
        result: Tuple[Optional[str], str]
        try:
            proc = _run(argv)
        except FileNotFoundError:
            result = (None, "gh is not installed here")
        except subprocess.TimeoutExpired:
            result = (None, "gh timed out")
        except OSError as exc:
            result = (None, "gh could not be run (%s)" % exc.__class__.__name__)
        else:
            if proc.returncode != 0:
                detail = [d for d in (proc.stderr or "").strip().splitlines() if d]
                result = (None, detail[0] if detail
                          else "gh exited %d" % proc.returncode)
            else:
                try:
                    state = str(json.loads(proc.stdout).get("state") or "")
                except ValueError:
                    state = ""
                if state:
                    result = (state, "")
                else:
                    result = (None, "gh returned no usable state field")
        cache[number] = result
        return result

    return read


def _memo(thunk: Callable[[], Optional[str]]) -> Callable[[], Optional[str]]:
    """One `gh repo view` at most, and only if an issue citation asks for it."""
    box: List[Optional[str]] = []

    def get() -> Optional[str]:
        if not box:
            box.append(thunk())
        return box[0]

    return get


def _repo_slug(root: Path) -> Optional[str]:
    """`OWNER/NAME` for the repo an issue citation should be looked up in.

    `None`, not `""`, and that is preserved rather than defended: every
    consumer here tests it with `if slug:`, so the two were already the same
    answer at this site — which is why #1701's two-state
    `_repo_target.effective_slug` loses nothing by arriving underneath it.

    The hand-rolled version this replaced read `os.environ["SUPERTOOL_REPO"]`
    directly and so skipped `target()`'s rule that a blank export is absence,
    not an empty target. A whitespace-only value became the slug, reached
    `gh issue view --repo "   "`, and every issue citation in the document
    rendered "couldn't check" against gh's complaint — while the cwd, which
    could have answered, was never asked. `root` is unused and kept: two test
    files monkeypatch this as a one-argument callable.
    """
    return _repo_target.effective_slug(timeout=15) or None


def _root() -> Path:
    try:
        proc = _run(["git", "rev-parse", "--show-toplevel"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return Path.cwd()
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


#: The boundary this op enforces, named once so the code and the refusal agree.
#:
#: `claims` resolves a relative argument against the git toplevel, and its path
#: lens has always answered `path leaves the repository root` for a document
#: that cites `/etc/hosts` or climbs out with `..`. The repository root is
#: therefore the boundary this op already owned — it simply never applied it to
#: the path it reads *from*, only to the paths it reads *about* (#1283).
#:
#: It is deliberately NOT the core's boundary, which is the cwd. The two differ
#: whenever `claims` is called from a subdirectory, where a root-relative
#: argument is the documented way to name a document and resolves above cwd.
#: Picking cwd here would break the op's own resolution rule; picking root and
#: not saying so would leave the next reader assuming the core's. The core's
#: cwd rule stays underneath as defence in depth for the day preset ops reach
#: `_PATH_ARG_POSITIONS` — this check is the op enforcing the boundary it owns,
#: not a substitute for the chokepoint.
_BOUNDARY = "repository root"

_CONFIG = ".supertool.json"


def _allow_outside_root(root: Path) -> bool:
    """The core's two opt-outs, honoured here so the refusal does not lie.

    `_safe_path` accepts `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` or an
    `allow_outside_cwd` key in `.supertool.json`, and the message below names
    both. An op that printed that sentence and then ignored the knob would be
    the stale-claim defect this op exists to catch, written by this op. Read
    from the root's config rather than by walking up from cwd — that is the
    config this run already reads its registry from.

    Wrapped, and failing CLOSED: a config that cannot be parsed is not an
    opt-out. `_safe_path` wraps the same read to stop a broken file raising
    out of a path check; the other half of that is that the exception must
    not be read as permission.
    """
    if os.environ.get("SUPERTOOL_ALLOW_OUTSIDE_CWD") == "1":
        return True
    try:
        with (root / _CONFIG).open("r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("allow_outside_cwd"))
    except (OSError, ValueError, AttributeError):
        return False


def _containment_refusal(rel: str, target: Path, root: Path) -> Optional[str]:
    """`None` when `target` is inside `root`, else the ERROR line to print.

    Resolved, not lexical. The doc-facing lens in `_path_findings` can be
    lexical because it never opens the file it judges; this one decides
    whether a read happens, so a symlink inside the repo pointing at
    `/etc/hosts` has to be refused on where its bytes are rather than on how
    its name is spelled.

    Runs BEFORE the `is_file` test on purpose. `no such file` for one outside
    path and a rendered document for another answers exactly the question the
    boundary refuses — the render leaks reference-shaped substrings of the
    file, but the existence answer alone is already a read.
    """
    if "\x00" in rel:
        return "ERROR: path contains a NUL byte\n"
    if _allow_outside_root(root):
        return None
    try:
        resolved = os.path.realpath(str(target))
        base = os.path.realpath(str(root))
    except (OSError, ValueError) as exc:
        return ("ERROR: cannot resolve %s (%s)\n"
                % (rel, exc.__class__.__name__))
    resolved_cmp = os.path.normcase(resolved)
    base_cmp = os.path.normcase(base)
    if resolved_cmp == base_cmp or resolved_cmp.startswith(base_cmp + os.sep):
        return None
    return (
        # %r for the two user-derived paths, matching `_safe_path`'s rule of
        # not echoing arbitrary input back raw. %s for the root: it is the
        # tool's own resolved path, and on Windows a %r doubles every
        # separator, so a caller comparing the printed root against a real one
        # would never match (#1283).
        "ERROR: path escapes the %s: %r (resolved to %r, root %s).\n"
        "       claims reads documents in this repository, so its boundary is "
        "the %s —\n"
        "       wider than the core's cwd boundary when you call it from a "
        "subdirectory.\n"
        '       To allow: set SUPERTOOL_ALLOW_OUTSIDE_CWD=1 (env), or add '
        '`"allow_outside_cwd": true` to .supertool.json.\n'
        % (_BOUNDARY, rel, resolved, base, _BOUNDARY)
    )


def main(argv: Sequence[str]) -> int:
    if not argv or not argv[0].strip():
        sys.stderr.write("ERROR: usage claims:PATH — PATH is a text file in "
                         "this repository, read as markdown.\n")
        return 2
    root = _root()
    rel = argv[0].strip()
    target = Path(rel) if os.path.isabs(rel) else root.joinpath(*rel.split("/"))
    # Containment first — see `_containment_refusal`. An out-of-boundary path
    # must get one answer whether or not it exists (#1283).
    refusal = _containment_refusal(rel, target, root)
    if refusal:
        sys.stderr.write(refusal)
        return 2
    if not target.is_file():
        sys.stderr.write("ERROR: no such file: %s\n" % rel)
        return 2
    try:
        text = _read_text(target)
    except OSError as exc:
        sys.stderr.write("ERROR: cannot read %s (%s)\n"
                         % (rel, exc.__class__.__name__))
        return 2
    registry = _load_registry(root)
    repo = _memo(lambda: _repo_slug(root))
    findings = scan(text, root=root, registry=registry,
                    issue_state=_issue_state_reader(repo), this_repo=repo)
    if not registry:
        findings.insert(0, Finding(0, "op", UNCHECKED, "(registry)",
                                   "no .supertool.json here, so no op "
                                   "reference in this document was checked"))
    sys.stdout.write(render(rel.replace(os.sep, "/"), findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
