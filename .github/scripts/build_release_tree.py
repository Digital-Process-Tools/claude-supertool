#!/usr/bin/env python3
"""Build the slim `release` tree from a git ref (#2705, ported from claude-remember#851).

The Anthropic plugin directory fetches a branch and holds any version whose plugin
folder breaks its file rules (every non-image, non-font file under 256 KiB, at most
512 files, no `.gitattributes` with export-ignore/export-subst/filter). `main` keeps
everything; this script produces the tree a `release` branch carries:

1. Every blob of REF is extracted with `git ls-tree` + `git cat-file --batch`. Not
   `git archive`: that only drops paths through `export-ignore`, which the directory
   refuses to validate at all. The working tree is never read, so an uncommitted
   edit cannot leak into a release.
2. The config's deny-list is dropped. Deny, not allow: a path forgotten here ships
   and is caught loudly by check_release_tree.py; a path forgotten in an allow-list
   would vanish from every user's install with no error anywhere. The config's
   `keep` list is a narrow exception to that: exact paths (never a prefix) that
   ship even though a deny entry also matches them -- the one case so far is the
   two files hooks/shipped_rules.py reads at runtime out of the denied `.claude/`
   tree for the shipped jit-context guard rule (#2729).
3. CHANGELOG.md is cut to its latest RELEASED section (an `[Unreleased]` heading is
   skipped even when it has entries), its link definition, and a link to the full
   file on the default branch.
4. Relative links and images in every shipped `.md` file that point at a path the
   deny-list removed are rewritten to absolute URLs on the default branch:
   raw.githubusercontent.com for images, github.com/.../blob for everything else.
5. Every shipped `.py` file has its comments and docstrings stripped (#2731),
   using `tokenize` for comments and `ast` only to locate docstring-statement
   positions -- never `ast.unparse`, which reformats code rather than cutting
   it. A removed line becomes a blank line, so a traceback into the shipped
   file still names the right source line; a function or class whose only
   statement was its docstring gets a `pass` so it keeps parsing. See
   `strip_py()` below for what stays: shebangs, encoding cookies, every
   string literal that is not a bare docstring statement (an f-string, a
   `#` character inside any string, a multi-line string assigned to a name).

Usage:
    build_release_tree.py --ref v0.36.0 --out /tmp/release-tree [--repo .]
                          [--config .github/release-branch.json]

The config (see .github/release-branch.json) carries the repo slug, default branch,
deny-list and budget, so another repository can reuse this file unchanged.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from check_release_tree import gitattributes_offences

DEFAULT_CONFIG = _HERE.parent / "release-branch.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


class BuildError(Exception):
    """A build that must not produce a tree."""


# -- config -----------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("repo", "default_branch", "deny"):
        if key not in cfg:
            raise BuildError(f"{path}: missing required key {key!r}")
    return cfg


def is_denied(path: str, deny: list[str]) -> bool:
    for entry in deny:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def is_kept(path: str, keep: list[str]) -> bool:
    """Is PATH one of the config's exact carve-outs, winning over a denied
    prefix (#2729)? Exact match only -- a prefix here would reopen the same
    "a path forgotten in an allow-list vanishes silently" risk `_deny_why`
    already rejects for the deny-list itself, just on the other side."""
    return path in keep


# -- git --------------------------------------------------------------------------

def _git_env() -> dict:
    # An outer git (a hook, a worktree command) can export GIT_DIR/GIT_INDEX_FILE;
    # inherited, they would point every call below at the wrong repository.
    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                         "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")}


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), *args], input=data,
                       capture_output=True, env=_git_env(), check=False)
    if r.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: "
                         f"{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def _ls_tree(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    """(mode, sha, path) for every entry of COMMIT, recursively."""
    entries = []
    for rec in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, _type, sha = meta.decode().split()
        entries.append((mode, sha, path.decode("utf-8")))
    return entries


def _cat_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    raw = _git(repo, "cat-file", "--batch", data=("\n".join(shas) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for _ in shas:
        nl = raw.index(b"\n", pos)
        sha, kind, size = raw[pos:nl].decode().split()
        if kind != "blob":
            raise BuildError(f"{sha}: expected a blob, got {kind}")
        start = nl + 1
        blobs[sha] = raw[start:start + int(size)]
        pos = start + int(size) + 1
    return blobs


# -- CHANGELOG --------------------------------------------------------------------

_SECTION = re.compile(r"^## \[([^\]]+)\]")
_LINKDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S")


def cut_changelog(text: str, full_url: str) -> str:
    """Keep the preamble, the first RELEASED `## [x]` section and its link
    definition, plus a pointer to the full file. `[Unreleased]` is never the
    section kept -- empty or not, it is not what this version shipped."""
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    preamble = lines[:heads[0]] if heads else lines
    for n, i in enumerate(heads):
        m = _SECTION.match(lines[i])
        if not m or m.group(1).strip().lower() == "unreleased":
            continue
        label = m.group(1)
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        section = lines[i:end]
        while section and (not section[-1].strip() or _LINKDEF.match(section[-1])):
            section.pop()
        defn = re.compile(r"^\s{0,3}\[" + re.escape(label) + r"\]:\s*\S")
        linkdef = next((line for line in lines if defn.match(line)), None)
        while preamble and not preamble[-1].strip():
            preamble = preamble[:-1]
        out = "".join(preamble).rstrip("\n") + "\n\n"
        out += ("This file carries only the latest release. The full history is in "
                f"[CHANGELOG.md on the default branch]({full_url}).\n\n")
        out += "".join(section).rstrip("\n") + "\n"
        if linkdef:
            out += "\n" + linkdef.rstrip("\n") + "\n"
        return out
    raise BuildError("CHANGELOG: no released `## [x.y.z]` section found "
                     "(only [Unreleased], or no sections at all)")


# -- link rewriting ---------------------------------------------------------------

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_INLINE = re.compile(r"(\]\(\s*)(<[^>\n]*>|[^)\s]+)")
_REFDEF = re.compile(r"^(\s{0,3}\[[^\]]+\]:[ \t]*)(<[^>\n]*>|\S+)")
_HTML = re.compile(r"(\b(src|href)\s*=\s*)([\"'])(.*?)\3", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _absolute(target: str, md_dir: str, should_rewrite: Callable[[str], str | None],
              repo: str, ref: str, force_raw: bool = False) -> str | None:
    bracketed = target.startswith("<") and target.endswith(">")
    t = target[1:-1] if bracketed else target
    if not t or t.startswith(("#", "//")) or _SCHEME.match(t):
        return None
    frag = ""
    for sep in ("#", "?"):
        if sep in t:
            t, rest = t.split(sep, 1)
            frag = sep + rest + frag
    path = unquote(t)
    rel = path.lstrip("/") if path.startswith("/") else posixpath.join(md_dir, path)
    rel = posixpath.normpath(rel)
    if rel in (".", "") or rel.startswith("../"):
        return None
    kind = should_rewrite(rel)
    if kind is None:
        return None
    if force_raw or posixpath.splitext(rel)[1].lower() in IMAGE_EXTS:
        url = f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(rel)}"
    else:
        url = f"https://github.com/{repo}/{kind}/{ref}/{quote(rel)}"
    url += frag
    return f"<{url}>" if bracketed else url


def rewrite_links(text: str, md_path: str, should_rewrite: Callable[[str], str | None],
                  repo: str, ref: str) -> tuple[str, int]:
    """Rewrite relative markdown/HTML link targets for which SHOULD_REWRITE(path)
    returns "blob" (a removed file) or "tree" (a removed directory). Fenced code
    and inline code spans are left alone."""
    md_dir = posixpath.dirname(md_path)
    count = 0

    def fix(target: str, force_raw: bool = False) -> str:
        nonlocal count
        new = _absolute(target, md_dir, should_rewrite, repo, ref, force_raw)
        if new is None:
            return target
        count += 1
        return new

    def fix_prose(seg: str) -> str:
        seg = _INLINE.sub(lambda m: m.group(1) + fix(m.group(2)), seg)
        return _HTML.sub(lambda m: m.group(1) + m.group(3)
                         + fix(m.group(4), m.group(2).lower() == "src") + m.group(3), seg)

    out = []
    fence = None
    for line in text.splitlines(keepends=True):
        fm = _FENCE.match(line)
        if fence:
            if fm and fm.group(1) == fence:
                fence = None
            out.append(line)
            continue
        if fm:
            fence = fm.group(1)
            out.append(line)
            continue
        m = _REFDEF.match(line)
        if m:
            line = m.group(1) + fix(m.group(2)) + line[m.end():]
            out.append(line)
            continue
        parts = re.split(r"(`+[^`]*`+)", line)
        out.append("".join(p if p.startswith("`") else fix_prose(p) for p in parts))
    return "".join(out), count


# -- sourced-script inlining (#2732) -----------------------------------------------
#
# COMMAND_SCRIPT_NOT_FOLLOWED ("Scripts the validator couldn't follow") is a
# confirmed hold on a shipped script that `source`s/`.`s another file
# (claude-jit-context's docs/directory-validator.md, playbook step 5). The
# committed tree keeps hooks/python-ladder.sh as its own file -- one
# definition both hooks share, readable on its own -- and this step produces
# a release tree where each consumer carries the ladder's body in place of
# the `source`/`.` line, and the now-unreferenced ladder file does not ship.
#
# A build-time transform of the in-memory tree, never an edit to the
# committed hooks/*.sh: local development keeps the shared file.

_LADDER_VAR_LINE = re.compile(
    r'^LADDER="\$\(cd "\$\(dirname "\$0"\)" && pwd\)/python-ladder\.sh"\n',
    re.MULTILINE)
_LADDER_SHELLCHECK_COMMENT = re.compile(
    r'^[ \t]*# shellcheck source=hooks/python-ladder\.sh\n', re.MULTILINE)
_LADDER_SOURCE_GUARDED = re.compile(
    r'^\. "\$LADDER" 2>/dev/null \|\| decline "the shared interpreter ladder '
    r'could not be sourced"\n', re.MULTILINE)
_LADDER_SOURCE_IF = re.compile(
    r'^if \. "\$LADDER" 2>/dev/null; then\n'
    r'(?P<then>(?:.*\n)*?)'
    r'^else\n'
    r'(?:.*\n)*?'
    r'^fi\n',
    re.MULTILINE)


class LadderInlineError(BuildError):
    """The ladder-inlining step could not recognise a consumer's own
    `source`/`.` call site -- refuse the build rather than ship a hook with
    the ladder's functions undefined (#2732)."""


def inline_python_ladder(contents: dict[str, bytes], config: dict) -> list[str]:
    """Mutate CONTENTS (path -> utf-8 bytes, the same shape `build()` already
    holds) in place: inline the ladder's body into every consumer named by
    config["ladder_inline"], then drop the ladder file itself. Returns the
    list of consumers actually inlined into -- empty only when the config
    carries no `ladder_inline` key at all, which is the one case with
    nothing to do. A config that NAMES a ladder path not present in this
    tree is a different state -- a consumer may still carry the raw
    `source`/`.` line with nothing left to inline -- and raises rather than
    returning the same empty list, so the two states cannot be confused
    (#2732 self-review)."""
    spec = config.get("ladder_inline")
    if not spec:
        return []
    ladder_path = spec["ladder"]
    if ladder_path not in contents:
        # Distinct from "nothing configured" (the `not spec` branch above):
        # this config names a ladder to inline and it is not in this tree.
        # Returning [] here as well would read identically to a genuine
        # no-op, and a consumer that still carries its raw `source`/`.`
        # line would ship that way with nothing saying so (#2732
        # self-review).
        raise LadderInlineError(
            f"{ladder_path}: config/ladder_inline names this path and it is "
            "not in this tree -- a consumer may still carry the raw "
            "source/. line with nothing left to inline")
    ladder_body = contents[ladder_path].decode("utf-8").rstrip("\n") + "\n"
    inlined: list[str] = []
    for rel in spec.get("consumers", []):
        if rel not in contents:
            continue
        text = contents[rel].decode("utf-8")
        # A function as repl, never a string: re.sub treats backslashes in a
        # string replacement as backreferences, and the ladder's own prose
        # is not guaranteed free of them.
        new_text, n_var = _LADDER_VAR_LINE.subn(
            lambda _m: ladder_body, text, count=1)
        if n_var != 1:
            raise LadderInlineError(
                f"{rel}: the ladder variable assignment this step expects to "
                "replace was not found -- #2732's inlining is out of date "
                "with this file")
        new_text, n_comment = _LADDER_SHELLCHECK_COMMENT.subn("", new_text)
        new_text, n_guarded = _LADDER_SOURCE_GUARDED.subn("", new_text)
        new_text, n_if = _LADDER_SOURCE_IF.subn(
            lambda m: m.group("then"), new_text)
        if n_guarded + n_if != 1:
            raise LadderInlineError(
                f"{rel}: the `source`/`.` call site this step expects to "
                "remove was not found (or was found more than once) -- "
                "#2732's inlining is out of date with this file")
        contents[rel] = new_text.encode("utf-8")
        inlined.append(rel)
    contents.pop(ladder_path, None)
    return inlined


# -- release README swap (#2732) ---------------------------------------------------
#
# MCP_FORWARDS_CREDENTIAL_ENV's text-only half: the full README's security
# section (a refused-path example) and command examples read, to the
# directory's own scanner, as credential-handling/attack text. A short,
# variable-free release README swapped in for README.md at build time is
# the documented fix (claude-jit-context's docs/directory-validator.md,
# playbook step 4) -- the full README never reaches the directory at all.

def swap_release_readme(contents: dict[str, bytes], config: dict) -> bool:
    """Replace contents["README.md"] with contents[config["release_readme"]]
    and drop the release-only file from what ships under its own name.
    Returns False only when the config carries no `release_readme` key at
    all -- the one case with nothing to do. A config that NAMES a path not
    present in this tree is a different state -- the full README, with its
    security example and command text, would ship unreplaced and nothing
    would say so -- and raises rather than returning the same False
    (#2732 self-review, the same shape as `inline_python_ladder`'s)."""
    rel = config.get("release_readme")
    if not rel:
        return False
    if rel not in contents:
        raise BuildError(
            f"{rel}: config/release_readme names this path and it is not "
            "in this tree -- README.md would ship unreplaced")
    contents["README.md"] = contents.pop(rel)
    return True


# -- Python stripping (#2731) ------------------------------------------------------

_SHEBANG = re.compile(r"^#!")
_CODING_COOKIE = re.compile(r"coding[:=]\s*([-\w.]+)")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _py_splitlines(text: str) -> list[str]:
    """Split TEXT into physical lines the way CPython's own parser counts
    them for `ast`/`tokenize` line numbers: `\\r\\n`, `\\r` and `\\n` each end
    exactly one line, and nothing else does. `str.splitlines()` additionally
    breaks on U+2028, U+2029, `\\v`, `\\f`, `\\x1c`-`\\x1e` and `\\x85` -- none of
    which the parser treats as a line terminator -- so using it here silently
    misaligns every later line number against a file containing one of
    those (confirmed: presets/_declared_workflows.py:408 carries a U+2028
    inside a string, and `str.splitlines()` miscounted it by one line,
    corrupting an unrelated docstring's span and producing a tokenize error
    two hundred lines away from the real cause)."""
    lines = []
    i, n, start = 0, len(text), 0
    while i < n:
        c = text[i]
        if c == "\r":
            i += 2 if (i + 1 < n and text[i + 1] == "\n") else 1
            lines.append(text[start:i])
            start = i
        elif c == "\n":
            i += 1
            lines.append(text[start:i])
            start = i
        else:
            i += 1
    if start < n:
        lines.append(text[start:n])
    return lines


def _byte_col_to_char(line_no_newline: str, byte_col: int) -> int:
    """ast's col_offset/end_col_offset are UTF-8 BYTE offsets into the
    physical line, not character offsets -- confirmed on this interpreter:
    `ast.parse('x = "\\u65e5\\u672c\\u8a9e"').body[0].end_col_offset` is 15, not
    the 9 characters actually on that line. Indexing a decoded `str` with the
    raw value would slice mid-character on any non-ASCII line (this repo's
    comments and docstrings use em-dashes and arrows throughout). tokenize's
    own token positions are character offsets already and need no such
    conversion -- only the ast-derived docstring spans do."""
    encoded = line_no_newline.encode("utf-8")
    return len(encoded[:byte_col].decode("utf-8", errors="strict"))


def _apply_span(lines: list[str], start_line: int, start_col: int,
                 end_line: int, end_col: int, replacement: str) -> None:
    """Replace the character range [start_line:start_col, end_line:end_col]
    (1-indexed line, 0-indexed column, both already char offsets) with
    REPLACEMENT, keeping whatever text sits before the span on its first
    line or after it on its last line (a docstring sharing a physical line
    with other code via `def f(): "doc"` or a trailing `; stmt`), and
    leaving the line count and every line ending unchanged."""
    if start_line == end_line:
        line = lines[start_line - 1]
        nl = _line_ending(line)
        body = line[:len(line) - len(nl)] if nl else line
        content = body[:start_col] + replacement + body[end_col:]
        lines[start_line - 1] = (content if content.strip() else "") + nl
        return
    first = lines[start_line - 1]
    nl0 = _line_ending(first)
    body0 = first[:len(first) - len(nl0)] if nl0 else first
    head = body0[:start_col] + replacement
    lines[start_line - 1] = (head if head.strip() else "") + nl0
    for ln in range(start_line + 1, end_line):
        lines[ln - 1] = _line_ending(lines[ln - 1]) or "\n"
    last = lines[end_line - 1]
    nl1 = _line_ending(last)
    body1 = last[:len(last) - len(nl1)] if nl1 else last
    tail = body1[end_col:]
    lines[end_line - 1] = (tail if tail.strip() else "") + nl1


def _docstring_spans(tree: ast.Module, lines: list[str], path: str
                      ) -> list[tuple[int, int, int, int, bool]]:
    """(start_line, start_col, end_line, end_col, needs_pass) for every
    module/class/function docstring -- the first statement of a body that
    is a bare string-literal expression -- with columns already converted
    from ast's byte offsets to character offsets against LINES.

    NEEDS_PASS is true whenever blanking the docstring alone would leave a
    bare `;` leading its own logical line, or a suite with no statement at
    all. Two shapes, worked out from what actually follows the docstring's
    own closing position, not from where the docstring started -- an
    earlier version of this function keyed off "is this the compound
    statement's header line", which covers `def f(): \"\"\"doc\"\"\"; return 1`
    but MISSES the far more ordinary `\"\"\"doc\"\"\"; x = 1` sharing an
    otherwise unremarkable line with the statement that follows it, module
    scope included:

    - a single physical line (start_line == end_line): if anything other
      than whitespace follows the docstring on that line, it can only be
      there via `;` (the sole grammar rule for two statements sharing a
      line), so blanking without a `pass` first produces a line starting
      with a bare `;`, which does not parse. `pass` goes right before it:
      `pass; x = 1`, legal at any scope.
    - the docstring is the body's only statement and nothing trails it on
      its own line (module scope needs nothing -- an empty module is
      legal; class/function scope needs `pass` or the suite is empty).

    A multi-line docstring (start_line != end_line) whose CLOSING line
    carries a trailing `; stmt` is a third shape this function refuses
    rather than guesses at: the one thing we KNOW how to re-indent safely
    is a bare `pass` on the span's own start line, and that does not make
    `; stmt` on a different physical line any less of a bare-leading-`;`."""
    spans = []
    nodes: list[ast.AST] = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for node in nodes:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        start_line, end_line = first.lineno, first.end_lineno
        start_no_nl = lines[start_line - 1]
        start_no_nl = start_no_nl[:len(start_no_nl) - len(_line_ending(start_no_nl))]
        end_no_nl = lines[end_line - 1]
        end_no_nl = end_no_nl[:len(end_no_nl) - len(_line_ending(end_no_nl))]
        start_col = _byte_col_to_char(start_no_nl, first.col_offset)
        end_col = _byte_col_to_char(end_no_nl, first.end_col_offset)
        is_module = isinstance(node, ast.Module)
        tail = end_no_nl[end_col:]
        if start_line == end_line:
            needs_pass = bool(tail.strip()) or (not is_module and len(body) == 1)
        else:
            if tail.strip():
                raise BuildError(
                    f"{path}:{end_line}: a multi-line docstring closing on a line "
                    f"that also carries another statement after `;` is not "
                    f"supported by this stripper -- move the docstring onto its "
                    f"own line(s), with nothing else sharing its closing line")
            needs_pass = (not is_module) and len(body) == 1
        spans.append((start_line, start_col, end_line, end_col, needs_pass))
    return spans


def strip_py(source: bytes, path: str) -> bytes:
    """Strip comments and docstrings from PATH's Python source for the
    release tree (#2731). `tokenize` finds comments; `ast` only locates
    docstring-statement positions, never regenerates source the way
    `ast.unparse` would (which reformats code rather than cutting it).
    Every removed line becomes a blank line of the same count, so a
    traceback into the shipped file still names the right source line. A
    function or class whose only statement was its docstring gets a `pass`
    so it still parses; a module left with none is a legal empty file.

    Left alone: the shebang and PEP 263 encoding cookie (lines 1-2), a
    leading UTF-8 BOM (`compile()` on bytes tolerates one; `ast.parse()` on
    the decoded `str` does not -- stripped off before parsing, put back on
    the result, same shape as the shebang/cookie treatment), every string
    literal that is not a bare docstring statement -- an f-string, a `#`
    character inside any string, a multi-line string assigned to a name
    rather than standing alone as a statement (`_shipped_reference.py`'s
    `_BUILTIN_OPS_JSON`) -- because `tokenize` never emits a COMMENT token
    for text inside a string, and `ast` only flags a bare string-expression
    that is the first statement of a module/class/function body.
    """
    bom = source[:3] == b"\xef\xbb\xbf"
    body_bytes = source[3:] if bom else source
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"{path}: not valid UTF-8, cannot strip: {exc}") from None
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise BuildError(f"{path}: does not parse, cannot strip: {exc}") from None

    lines = _py_splitlines(text)
    if not lines:
        return source

    for start_line, start_col, end_line, end_col, needs_pass in _docstring_spans(tree, lines, path):
        replacement = "pass" if needs_pass else ""
        _apply_span(lines, start_line, start_col, end_line, end_col, replacement)

    stripped = "".join(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(stripped).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        raise BuildError(f"{path}: could not tokenize after docstring removal: {exc}") from None

    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        (sr, sc), (er, ec) = tok.start, tok.end
        if sr != er:
            continue  # a COMMENT token never spans lines -- defensive only
        if sr <= 2 and (_SHEBANG.match(tok.string) or _CODING_COOKIE.search(tok.string)):
            continue  # shebang / PEP 263 encoding cookie -- kept verbatim
        _apply_span(lines, sr, sc, er, ec, "")

    result = "".join(lines).encode("utf-8")
    if bom:
        result = b"\xef\xbb\xbf" + result
    try:
        compile(result, path, "exec")
    except SyntaxError as exc:
        raise BuildError(f"{path}: stripped source no longer compiles: {exc}") from None
    return result


# -- build ------------------------------------------------------------------------

def build(repo: Path, ref: str, out: Path, config: dict) -> dict:
    repo, out = Path(repo), Path(out)
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"output directory {out} is not empty -- refusing to write into it")
    try:
        commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except BuildError:
        raise BuildError(f"ref {ref!r} does not resolve to a commit in {repo}") from None

    deny = list(config.get("deny", []))
    keep = list(config.get("keep", []))
    entries = _ls_tree(repo, commit)
    kept, removed = [], []
    for mode, sha, path in entries:
        if is_denied(path, deny) and not is_kept(path, keep):
            removed.append(path)
            continue
        if mode == "160000":
            raise BuildError(f"{path}: submodules are not supported in a release tree")
        if mode == "120000":
            raise BuildError(f"{path}: symlinks are not supported in a release tree")
        kept.append((mode, sha, path))

    blobs = _cat_blobs(repo, sorted({sha for _, sha, _ in kept}))
    contents = {path: blobs[sha] for _, sha, path in kept}

    for path, data in contents.items():
        if posixpath.basename(path) == ".gitattributes":
            bad = gitattributes_offences(data.decode("utf-8", "replace"))
            if bad:
                raise BuildError(f"{path}: {', '.join(bad)} -- the directory stops "
                                 "validating on these attributes")

    slug = config["repo"]
    branch = config.get("link_ref") or config["default_branch"]
    changelog = config.get("changelog")
    if changelog and changelog in contents:
        url = f"https://github.com/{slug}/blob/{config['default_branch']}/{changelog}"
        contents[changelog] = cut_changelog(contents[changelog].decode("utf-8"), url).encode("utf-8")

    ladder_inlined = inline_python_ladder(contents, config)
    if ladder_inlined:
        ladder_path = config["ladder_inline"]["ladder"]
        kept = [(mode, sha, path) for mode, sha, path in kept if path != ladder_path]
        removed.append(ladder_path)

    readme_swapped = swap_release_readme(contents, config)
    if readme_swapped:
        readme_path = config["release_readme"]
        kept = [(mode, sha, path) for mode, sha, path in kept if path != readme_path]
        removed.append(readme_path)

    rewritten: dict[str, int] = {}
    if config.get("rewrite_links", True):
        removed_set = set(removed)
        removed_dirs = {posixpath.dirname(p) for p in removed}
        for p in list(removed_dirs):
            while p:
                p = posixpath.dirname(p)
                removed_dirs.add(p)
        kept_paths = set(contents)
        kept_dirs = set()
        for p in kept_paths:
            d = posixpath.dirname(p)
            while d:
                kept_dirs.add(d)
                d = posixpath.dirname(d)

        def should_rewrite(rel: str) -> str | None:
            if rel in removed_set:
                return "blob"
            if rel in removed_dirs and rel not in kept_dirs and rel not in kept_paths:
                return "tree"
            return None

        for path in sorted(contents):
            if not path.lower().endswith(".md"):
                continue
            new, n = rewrite_links(contents[path].decode("utf-8"), path, should_rewrite, slug, branch)
            if n:
                contents[path] = new.encode("utf-8")
                rewritten[path] = n

    stripped_before = stripped_after = 0
    if config.get("strip_py", True):
        for path in sorted(contents):
            if not path.lower().endswith(".py"):
                continue
            before = contents[path]
            after = strip_py(before, path)
            stripped_before += len(before)
            stripped_after += len(after)
            contents[path] = after

    out.mkdir(parents=True, exist_ok=True)
    for mode, _sha, path in kept:
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents[path])
        if os.name != "nt":
            os.chmod(dest, 0o755 if mode == "100755" else 0o644)

    unused = [e for e in deny if not any(is_denied(p, [e]) for _, _, p in entries)]
    unused_keep = [e for e in keep if not any(p == e for _, _, p in entries)]
    return {"ref": ref, "commit": commit, "kept": len(kept), "removed": removed,
            "rewritten": rewritten, "unused_deny": unused, "unused_keep": unused_keep,
            "ladder_inlined": ladder_inlined, "readme_swapped": readme_swapped,
            "stripped_before_bytes": stripped_before, "stripped_after_bytes": stripped_after}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="git repository to read REF from")
    ap.add_argument("--ref", required=True, help="tag, branch or sha to build")
    ap.add_argument("--out", required=True, help="output directory (absent or empty)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--repo-slug", help="owner/name for absolute links (overrides config)")
    ap.add_argument("--default-branch", help="branch absolute links point at (overrides config)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        if args.repo_slug:
            cfg["repo"] = args.repo_slug
        if args.default_branch:
            cfg["default_branch"] = args.default_branch
        report = build(Path(args.repo), args.ref, Path(args.out), cfg)
    except BuildError as exc:
        print(f"build_release_tree: {exc}", file=sys.stderr)
        return 1
    print(f"built {report['ref']} ({report['commit']}): {report['kept']} files kept, "
          f"{len(report['removed'])} removed by the deny-list")
    before, after = report.get("stripped_before_bytes", 0), report.get("stripped_after_bytes", 0)
    if before:
        print(f"  stripped comments/docstrings: {before} -> {after} bytes of .py "
              f"({before - after} bytes removed, #2731)")
    for path, n in sorted(report["rewritten"].items()):
        print(f"  rewrote {n} link(s) in {path}")
    for entry in report["unused_deny"]:
        print(f"  note: deny entry {entry!r} matched nothing at this ref")
    for entry in report["unused_keep"]:
        print(f"  note: keep entry {entry!r} matched nothing at this ref "
              f"(#2729) -- the carve-out it names may have moved or been removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
