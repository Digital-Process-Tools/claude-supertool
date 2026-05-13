"""Shared helpers for the xml preset. Stdlib-only."""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from typing import Optional


# ---------------------------------------------------------------------------
# Element subclass that accepts dynamic attributes (e.g. _start_line)
# ---------------------------------------------------------------------------

class LineElement(ET.Element):
    """ET.Element subclass with a __dict__ so we can set _start_line."""


# ---------------------------------------------------------------------------
# Expat-based tree builder with per-element line tracking
# ---------------------------------------------------------------------------

class _LineTrackingBuilder:
    """Build an ET tree from expat events, stamping _start_line on each element."""

    def __init__(self) -> None:
        self._stack: list[LineElement] = []
        self.root: Optional[LineElement] = None
        self._parser = expat.ParserCreate()
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._end
        self._parser.CharacterDataHandler = self._text

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        elem = LineElement(tag, attrs)
        elem._start_line = self._parser.CurrentLineNumber
        if self._stack:
            self._stack[-1].append(elem)
        else:
            self.root = elem
        self._stack.append(elem)

    def _end(self, tag: str) -> None:
        self._stack.pop()

    def _text(self, data: str) -> None:
        if not self._stack:
            return
        parent = self._stack[-1]
        if len(parent):
            last = parent[-1]
            last.tail = (last.tail or "") + data
        else:
            parent.text = (parent.text or "") + data

    def parse_file(self, path: str) -> LineElement:
        with open(path, "rb") as fh:
            chunk = fh.read(65536)
            while chunk:
                next_chunk = fh.read(65536)
                self._parser.Parse(chunk, not next_chunk)
                chunk = next_chunk
        if self.root is None:
            raise ET.ParseError("empty document")
        return self.root


# ---------------------------------------------------------------------------
# contains() shim — ElementTree doesn't support it; we emulate it
# ---------------------------------------------------------------------------

_CONTAINS_RE = re.compile(
    r"contains\(\s*(@[\w:.-]+)\s*,\s*'([^']*)'\s*\)"
)


def _apply_contains_filter(
    elems: list[ET.Element], xpath: str
) -> list[ET.Element]:
    """Post-filter a result list for any contains() predicates in the xpath.

    ElementTree only supports a limited XPath subset that excludes contains().
    We strip contains() predicates from the xpath before passing to findall(),
    then re-apply them here as a Python filter.

    Only handles the pattern: contains(@attr, 'value').
    Multiple contains() predicates in the same expression are all applied.
    """
    for m in _CONTAINS_RE.finditer(xpath):
        attr = m.group(1).lstrip("@")
        substring = m.group(2)
        elems = [e for e in elems if substring in (e.get(attr) or "")]
    return elems


def _strip_contains(xpath: str) -> str:
    """Remove contains() predicates from xpath so ET.findall() accepts it."""
    # Replace [...contains(...)...] predicates — remove just the contains() call
    # inside [...]; if it's the only predicate, remove the whole [...] block.
    def _repl_bracket(m: re.Match) -> str:
        inner = m.group(1)
        # Remove all contains(...) calls from inside the bracket
        cleaned = _CONTAINS_RE.sub("", inner).strip()
        # Remove leftover boolean operators
        cleaned = re.sub(r"^\s*(and|or)\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s*(and|or)\s*$", "", cleaned).strip()
        if cleaned:
            return f"[{cleaned}]"
        return ""

    return re.sub(r"\[([^\]]*contains\([^\]]*)\]", _repl_bracket, xpath)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_xml(path: str) -> LineElement:
    """Parse an XML file with line tracking. Exits on error."""
    try:
        builder = _LineTrackingBuilder()
        return builder.parse_file(path)
    except FileNotFoundError:
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        sys.exit(1)
    except (ET.ParseError, expat.ExpatError) as exc:
        sys.stderr.write(f"ERROR: malformed XML in {path}: {exc}\n")
        sys.exit(1)


def split_arg(arg: str, n: int) -> list[str]:
    """Split arg into exactly n parts.

    Two modes:

    1. **Triple-colon separator mode** (when ':::' is present):
       ':::' is the field delimiter. Split on ':::' to get raw_parts; field[0]
       is raw_parts[0], field[n-1] is raw_parts[-1], and any middle raw_parts
       are joined with ':' to form the middle field(s). This lets a literal
       colon inside an XPath string (e.g. 'foo:::bar' becomes 'foo:bar') survive
       field splitting when ':::' is used as the separator between fields.

    2. **Single-colon mode** (no ':::' present):
       For n=2: split on first ':'.
       For n>=3: field[0] = text before first ':', field[n-1] = text after
       last ':', middle field = everything between. This keeps XPath expressions
       like './/ns:tag' intact in the middle field without mis-splitting.

    Missing fields are padded with empty strings.
    """
    if ":::" in arg:
        raw = arg.split(":::")
        if n == 1:
            return [":".join(raw)]
        if n == 2:
            return [raw[0], ":".join(raw[1:])]
        # n >= 3: first raw_part, middle joined with ':', last raw_part
        if len(raw) >= 3:
            result = [raw[0], ":".join(raw[1:-1]), raw[-1]]
        elif len(raw) == 2:
            result = [raw[0], raw[1], ""]
        else:
            result = [raw[0], "", ""]
        while len(result) < n:
            result.append("")
        return result[:n]

    # No triple-colon — plain single-colon splitting
    if ":" not in arg:
        result: list[str] = [arg]
        while len(result) < n:
            result.append("")
        return result
    if n == 1:
        return [arg]
    if n == 2:
        idx = arg.index(":")
        return [arg[:idx], arg[idx + 1:]]
    # n >= 3: first ':' left boundary, last ':' right boundary
    left = arg.index(":")
    right = arg.rindex(":")
    if left == right:
        result = [arg[:left], arg[left + 1:]]
        while len(result) < n:
            result.append("")
        return result
    field0 = arg[:left]
    fieldn = arg[right + 1:]
    middle = arg[left + 1:right]
    result = [field0, middle, fieldn]
    while len(result) < n:
        result.append("")
    return result[:n]


def element_summary(elem: ET.Element) -> str:
    """One-line summary: 'LINE:TAG @k=v …'"""
    line = getattr(elem, "_start_line", "?")
    attrs = " ".join(f"@{k}={v}" for k, v in elem.attrib.items())
    parts = [f"{line}:{elem.tag}"]
    if attrs:
        parts.append(attrs)
    if elem.text and elem.text.strip():
        text = elem.text.strip().replace("\n", " ")[:80]
        parts.append(f'text="{text}"')
    return " ".join(parts)


def _split_xpath_steps(xpath: str) -> list[str]:
    """Split an xpath into steps on '/' boundaries that are NOT inside [..].

    Examples:
      ".//file[contains(@name,'X')]/line[@count='0']"
      -> [".", "/file[contains(@name,'X')]", "/line[@count='0']"]
    """
    steps: list[str] = []
    buf = ""
    depth = 0
    i = 0
    while i < len(xpath):
        c = xpath[i]
        if c == "[":
            depth += 1
            buf += c
        elif c == "]":
            depth -= 1
            buf += c
        elif c == "/" and depth == 0:
            if buf:
                steps.append(buf)
                buf = ""
            # Detect '//' descendant
            if i + 1 < len(xpath) and xpath[i + 1] == "/":
                buf = "//"
                i += 2
                continue
            buf = "/"
        else:
            buf += c
        i += 1
    if buf:
        steps.append(buf)
    return steps


def safe_xpath(root: ET.Element, xpath: str) -> list[ET.Element]:
    """Run XPath, exit with a human-readable error on bad syntax.

    Applies a contains() shim: ElementTree's XPath subset doesn't support
    contains(). When the xpath has contains() in a non-terminal step, we
    walk the steps in order: for each step we strip contains() so ET can
    parse it, find matches against the current node set, then post-filter
    each level with the contains() predicates that belonged to that step.

    iterparse-optimized fast paths are NOT applied here — this is the
    full-parse fallback used by the xml, xml_attr, xml_count ops.
    """
    if not xpath:
        sys.stderr.write("ERROR: XPath expression is empty\n")
        sys.exit(2)
    try:
        if "contains(" not in xpath:
            return root.findall(xpath)

        steps = _split_xpath_steps(xpath)
        nodes: list[ET.Element] = [root]
        for step in steps:
            if not step or step in (".",):
                continue
            stripped_step = _strip_contains(step)
            # ET.findall on a step needs a valid path — leading '/' means
            # absolute, which findall doesn't accept; convert to relative.
            query = stripped_step
            if query.startswith("//"):
                query = ".//" + query[2:]
            elif query.startswith("/"):
                query = "./" + query[1:]
            next_nodes: list[ET.Element] = []
            seen: set[int] = set()
            for n in nodes:
                for m in n.findall(query):
                    if id(m) not in seen:
                        seen.add(id(m))
                        next_nodes.append(m)
            if "contains(" in step:
                next_nodes = _apply_contains_filter(next_nodes, step)
            nodes = next_nodes
            if not nodes:
                break
        # Drop the synthetic root if it slipped through
        return [n for n in nodes if n is not root]
    except (SyntaxError, ET.ParseError, TypeError, KeyError) as exc:
        sys.stderr.write(f"ERROR: invalid XPath '{xpath}': {exc}\n")
        sys.exit(2)
