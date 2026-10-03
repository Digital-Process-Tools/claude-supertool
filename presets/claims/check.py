#!/usr/bin/env python3


















































from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _repo_target  

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






_HEADING = re.compile(r"^(#{1,6})\s+(.*?)[ \t]*\Z")





_OPEN_DEFECTS = re.compile(r"^open\s+defects?\b", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")

_EXTS = ("py|md|json|toml|yml|yaml|sh|bash|cfg|ini|txt|tsv|xml|html|js|ts|"
         "jsx|tsx|rs|php|rb|go|sql|css|lock|env|service")




_PATH_TOK = re.compile(
    r"^([A-Za-z0-9_.][A-Za-z0-9_./+-]*\.(?:" + _EXTS + r"))(?::(\d+))?\Z")










_OP_TOK = re.compile(r"^([a-z][a-z0-9_-]*):(\S.*)\Z")

_CITATION = re.compile(
    r"(?:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<num>\d{1,6})\b")









_QUOTED_REF = re.compile(
    r'^[\s,]*(?:§|section)?\s*["“‘]([^"”’]{2,200})["”’]')




_PLACEHOLDER_VOCAB = frozenset((
    "N", "NN", "NNN", "NNNN", "PATH", "FILE", "NAME", "OWNER", "REPO", "ID",
    "UUID", "SHA", "BRANCH", "TAG", "SECTION", "VERSION", "ISSUE", "NUMBER"))


_JSON_SCALAR = re.compile(r'^(?:true|false|null|-?\d|["“\[{])', re.IGNORECASE)











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










    lines = [line[:-1] if line.endswith("\r") else line
             for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _read_text(path: Path) -> str:






    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()






def _live_lines(lines: Sequence[str]) -> List[bool]:





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






    name = (this_repo or "").split("/")[-1]
    return name.split("-", 1)[0].lower() + "-" if "-" in name else None


def _resolve_repo(this_repo) -> Optional[str]:







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






class _Tree:


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






def _load_registry(root: Path) -> Dict[str, str]:






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

    box: List[Optional[str]] = []

    def get() -> Optional[str]:
        if not box:
            box.append(thunk())
        return box[0]

    return get


def _repo_slug(root: Path) -> Optional[str]:















    return _repo_target.effective_slug(timeout=15) or None


def _root() -> Path:
    try:
        proc = _run(["git", "rev-parse", "--show-toplevel"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return Path.cwd()
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


















_BOUNDARY = "repository root"

_CONFIG = ".supertool.json"


def _allow_outside_root(root: Path) -> bool:














    if os.environ.get("SUPERTOOL_ALLOW_OUTSIDE_CWD") == "1":
        return True
    try:
        with (root / _CONFIG).open("r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("allow_outside_cwd"))
    except (OSError, ValueError, AttributeError):
        return False


def _containment_refusal(rel: str, target: Path, root: Path) -> Optional[str]:













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
